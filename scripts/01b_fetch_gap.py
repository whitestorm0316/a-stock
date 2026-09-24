#!/usr/bin/env python3
"""
01b_fetch_gap.py — 并行补齐 2014-12 ~ 2016-09 的历史日K(不复权)

dump 提供的 10 年窗口始于 2016-09-21, 为覆盖用户要求的 2015-01-01 起点,
逐个 thscode 调用 historical 接口补齐早期数据。
"""
import os
import json
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://fuyao.aicubes.cn"
KEY = os.environ.get("FUYAO_KEY", "sk-fuyao-LcCu-ioaupkOIvh4ucQ_Ab6wJhefxdQG")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
OUT = os.path.join(RAW, "gap_2015.parquet")

START = 1417363200000      # 2014-12-01
END = 1474761600000        # 2016-09-25
CONC = 12


def fetch_one(code, retries=4):
    url = (f"{BASE}/api/a-share/prices/historical?thscode={code}"
           f"&interval=1d&start={START}&end={END}&adjust=none")
    req = urllib.request.Request(url, headers={"X-api-key": KEY})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                d = json.loads(r.read().decode())
            if d.get("code") == 0:
                return code, d["data"]["item"]
            if d.get("code") == 3002:      # 数据未就绪
                return code, []
            if d.get("code") == 2003:
                return code, None
            time.sleep(0.6 * (i + 1))
        except Exception:
            time.sleep(0.8 * (i + 1))
    return code, []


def main():
    tk = json.load(open(os.path.join(RAW, "tickers_ashare.json")))
    if isinstance(tk, dict):
        tk = tk["data"]["item"]
    codes = [t["thscode"] for t in tk]
    print(f"fetching gap for {len(codes)} stocks ...")

    rows = []
    t0 = time.time()
    done = 0
    err = 0
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        futs = {ex.submit(fetch_one, c): c for c in codes}
        for fut in as_completed(futs):
            code, items = fut.result()
            done += 1
            if items is None:
                err += 1
            else:
                for it in items:
                    rows.append((code, it["date_ms"], it["open_price"],
                                 it["high_price"], it["low_price"],
                                 it["close_price"], it["volume"], it["turnover"]))
            if done % 500 == 0:
                el = time.time() - t0
                print(f"  {done}/{len(codes)}  rows={len(rows):,}  {el:.0f}s  "
                      f"({done/el:.1f} req/s)  err={err}", flush=True)

    import pandas as pd
    df = pd.DataFrame(rows, columns=["thscode", "date_ms", "open_price",
                                     "high_price", "low_price", "close_price",
                                     "volume", "turnover"])
    df.to_parquet(OUT, index=False, compression="zstd")
    print(f"saved {OUT}  {len(df):,} rows  {len(df)/1e6:.1f}M  "
          f"in {time.time()-t0:.0f}s  (unavailable: {err})")


if __name__ == "__main__":
    main()
