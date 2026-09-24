#!/usr/bin/env python3
"""
01_fetch_data.py — 获取同花顺金融API数据并落地本地数据集

产出:
  data/raw/daily_k_10y.parquet      全市场10年日K(不复权)
  data/raw/adj_factors.parquet      复权因子事件流
  data/raw/tickers_ashare.json      A股标的清单(含上市日期)
  data/raw/index_constituents.json  指数成分股(当前快照)
  data/meta.json                    数据元信息
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = "https://fuyao.aicubes.cn"
KEY = os.environ.get("FUYAO_KEY", "sk-fuyao-LcCu-ioaupkOIvh4ucQ_Ab6wJhefxdQG")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
os.makedirs(RAW, exist_ok=True)


def api_get(path, params=None, retries=4):
    url = BASE + path
    if params:
        q = "&".join(f"{k}={v}" for k, v in params.items())
        url += "?" + q
    req = urllib.request.Request(url, headers={"X-api-key": KEY})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 * (i + 1))
                continue
            raise
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"failed: {url}")


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    return os.path.getsize(dest)


def step_dump(dump_path, out_name, label):
    """获取预签名链接并立即下载"""
    dest = os.path.join(RAW, out_name)
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        print(f"[skip] {label} 已存在 {os.path.getsize(dest)/1e6:.1f} MB")
        return
    r = api_get(dump_path)
    if r.get("code") != 0:
        print(f"[FAIL] {label}: {r.get('code')} {r.get('message')}")
        return
    url = r["data"]["presigned_url"]
    print(f"[dl] {label} ...")
    size = download(url, dest)
    print(f"[ok] {label} -> {out_name}  {size/1e6:.1f} MB")


def main():
    t0 = time.time()
    meta = {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    # ---- 1. 全市场10年日K ----
    step_dump("/api/dump/market-dumps/daily-k/download-url",
              "daily_k_10y.parquet", "全市场10年日K")

    # ---- 2. 复权因子 ----
    step_dump("/api/dump/market-dumps/adjustment-factors/download-url",
              "adj_factors.parquet", "复权因子")

    # ---- 3. A股标的清单 ----
    tk_path = os.path.join(RAW, "tickers_ashare.json")
    if not os.path.exists(tk_path):
        all_items = []
        offset = 0
        while True:
            r = api_get("/api/meta/tickers/list",
                        {"asset_type": "a-share", "limit": 10000, "offset": offset})
            if r.get("code") != 0:
                print("tickers err", r)
                break
            items = r["data"]["item"]
            all_items.extend(items)
            if len(items) < 10000:
                break
            offset += 10000
        json.dump(all_items, open(tk_path, "w"), ensure_ascii=False)
        print(f"[ok] A股标的 {len(all_items)} 条")
    else:
        all_items = json.load(open(tk_path))
        print(f"[skip] A股标的 {len(all_items)} 条")
    meta["n_tickers"] = len(all_items)

    # ---- 4. 指数成分股 ----
    idx_path = os.path.join(RAW, "index_constituents.json")
    indices = {
        "hs300": "000300.SH",
        "zz500": "000905.SH",
        "zz1000": "000852.SH",
    }
    if not os.path.exists(idx_path):
        out = {}
        for name, code in indices.items():
            r = api_get("/api/a-share-index/constituents/ths-stock-list",
                        {"thscode": code})
            if r.get("code") == 0:
                out[name] = [i["thscode"] for i in r["data"]["item"]]
                print(f"[ok] {name} ({code}) 成分 {len(out[name])} 只")
            else:
                print(f"[FAIL] {name}: {r.get('message')}")
                out[name] = []
            time.sleep(0.5)
        json.dump(out, open(idx_path, "w"), ensure_ascii=False)
    else:
        out = json.load(open(idx_path))
        print(f"[skip] 指数成分 {[ (k,len(v)) for k,v in out.items() ]}")

    # ---- 5. 交易日历(通过上证指数K线推导) ----
    cal_path = os.path.join(RAW, "trading_calendar.json")
    if not os.path.exists(cal_path):
        start_ms = 1420070400000   # 2015-01-01
        end_ms = int(time.time() * 1000)
        r = api_get("/api/a-share-index/prices/historical",
                    {"thscode": "000001.SH", "interval": "1d",
                     "start": start_ms, "end": end_ms})
        if r.get("code") == 0:
            dates = [i["date_ms"] for i in r["data"]["item"]]
            json.dump(dates, open(cal_path, "w"))
            print(f"[ok] 交易日历 {len(dates)} 天")
            meta["n_trading_days"] = len(dates)
        else:
            print("[FAIL] calendar", r.get("message"))
    else:
        dates = json.load(open(cal_path))
        print(f"[skip] 交易日历 {len(dates)} 天")
        meta["n_trading_days"] = len(dates)

    # ---- 6. 指数K线(用于市场环境分层) ----
    for name, code in indices.items():
        p = os.path.join(RAW, f"index_{name}.json")
        if os.path.exists(p):
            print(f"[skip] index_{name}")
            continue
        r = api_get("/api/a-share-index/prices/historical",
                    {"thscode": code, "interval": "1d",
                     "start": 1420070400000, "end": int(time.time() * 1000)})
        if r.get("code") == 0:
            json.dump(r["data"]["item"], open(p, "w"))
            print(f"[ok] index_{name} {len(r['data']['item'])} bars")

    meta["elapsed_sec"] = round(time.time() - t0, 1)
    json.dump(meta, open(os.path.join(ROOT, "data", "meta.json"), "w"),
              ensure_ascii=False, indent=2)
    print(f"\n完成, 耗时 {meta['elapsed_sec']}s")


if __name__ == "__main__":
    main()
