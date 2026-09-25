#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
99_daily_update.py —— 每日增量更新原始数据（追加式，不重新下载 dump）

为什么不用「重新下载 dump 覆盖」
------------------------------
01_fetch_data.py 拉的是**滚动 10 年窗口**：构建时数据为 2016-09-26 ~ 2026-09-24。
明年窗口起点会滑到约 2017-09，而 gap_2015.parquet 只补到 2016-09-23，
中间会凭空消失约一年 → 所有 MA120 / rolling 60/120 特征在边界处全废。
因此必须**只追加、从不替换**，本地永久保留完整历史。

另外注意：01_fetch_data.py 的 step_dump() 有「文件已存在就 skip」的逻辑，
直接重跑它什么都不会做——想强制刷新得先删文件，容易误伤，所以不用它。

设计要点：为什么是「独立增量文件」而不是「重写 dump」
--------------------------------------------------
新增 K 线写进独立的 daily_k_incr.parquet（每天只有几千行，极小），
再由 02_build_dataset.py 用「和 gap_2015.parquet 完全一样的方式」合并进来。
这样有四个好处：
  1. 永不重写 181MB 的 dump → 不会因为中断/断电损坏原始数据
  2. 永不重新下载 dump → 彻底规避上面说的滚动窗口漂移
  3. dedupe keep="last" 让增量覆盖旧值 → 可以修复「昨天那条没走完的 K」
  4. 内存占用极小，不用全量读入 10M 行再写回

本脚本做什么
------------
  1. tickers_ashare.json      每次刷新（捕捉新股 IPO）
  2. adj_factors.parquet      每次全量重下（0.3MB，很便宜；新增分红/送转会影响历史复权价）
  3. daily_k_incr.parquet     按「每只股票各自的最后一根 K」增量追加
                              新股（本地无记录）自动拉全历史，不会被漏掉
  4. 可选 --rebuild           连带跑 02_build_dataset.py + 21_build_v2_features.py
  5. 可选 --industry          连带跑 20_fetch_industry.py（新股的行业归属；建议每周跑一次）

⚠ 必须先停掉选股器服务
----------------------
服务常驻内存约 7GB，而 02/21 重建本身就要吃不少内存。
实测同时跑会因可用内存不足失败。流程务必是：停服务 → 更新 → 重启服务。

用法
----
  python scripts/99_daily_update.py                 # 只更原始数据（约 4 分钟）
  python scripts/99_daily_update.py --rebuild       # 原始数据 + 重建面板（约 11 分钟）
  python scripts/99_daily_update.py --rebuild --industry
  python scripts/99_daily_update.py --since 2026-09-01   # 手动指定起点，回补某段区间

之后重启服务（首次加载约 80~100 秒）：
  python app/server.py 8770

实测基准（全市场 5578 只，2026-09-25 实测）
--------------------------------------
  步骤 1~3 原始数据更新：626s ≈ **10.5 分钟**
    · tickers + adj_factors 刷新        ~10s
    · 逐个标的增量拉取                   ~615s（约 9~10 req/s，受服务端限流）
      ⚠ 前 300 只跑到 26 req/s，但持续请求会被限流到 9~10 req/s，
        所以别用小样本外推，全量就是 10 分钟左右
  02_build_dataset.py：约 3 分钟   → panel.parquet  2.2 GB
  21_build_v2_features.py：约 3.5 分钟 → v2_panel.parquet 3.8 GB
  服务首次加载：约 80~100 秒

  合计一次完整更新约 18 分钟。

幂等性已验证：重复运行时「新增 0 行」，重复跑不会污染数据。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

# 中文 Windows 控制台是 GBK：只 reconfigure 成 utf-8 仍会在 emoji（⚠）上崩，
# 必须带 errors="replace"，且 stderr 也要处理。统一交给 scripts/_console.py。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _console import bootstrap, mark  # noqa: E402
bootstrap()

BASE = "https://fuyao.aicubes.cn"
KEY = os.environ.get("FUYAO_KEY", "sk-fuyao-LcCu-ioaupkOIvh4ucQ_Ab6wJhefxdQG")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
SCRIPTS = os.path.join(ROOT, "scripts")
os.makedirs(RAW, exist_ok=True)

DAILY_K = os.path.join(RAW, "daily_k_10y.parquet")
INCR = os.path.join(RAW, "daily_k_incr.parquet")   # 增量K线（本脚本产出，02 负责合并）
ADJ = os.path.join(RAW, "adj_factors.parquet")
TICKERS = os.path.join(RAW, "tickers_ashare.json")

# 个股在本地一根 K 都没有时，从这天开始拉全历史（与 gap_2015 起点一致）
BASE_START_MS = int(pd.Timestamp("2014-12-01").timestamp() * 1000)
CONC = 12

DUMP_COLS = ["thscode", "currency", "interval", "adjusted", "date_ms",
             "open_price", "high_price", "low_price", "close_price",
             "volume", "turnover"]


def api_json(path, params=None, retries=4, timeout=60):
    url = BASE + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, headers={"X-api-key": KEY})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(2 * (i + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求失败 {url}: {last}")


def download(url, dest, timeout=300):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    return os.path.getsize(dest)


# ---------------------------------------------------------------- 步骤 1/2/3
def refresh_tickers():
    """每次刷新：捕捉新股。注意 01 写的是 list，下游 02/01b/20 都兼容 list。"""
    all_items, offset = [], 0
    while True:
        r = api_json("/api/meta/tickers/list",
                     {"asset_type": "a-share", "limit": 10000, "offset": offset})
        if r.get("code") != 0:
            print(f"[tickers] 失败: {r.get('message')}")
            return None
        items = r["data"]["item"]
        all_items.extend(items)
        if len(items) < 10000:
            break
        offset += 10000
    json.dump(all_items, open(TICKERS, "w"), ensure_ascii=False)
    print(f"[1/3] tickers 已刷新：{len(all_items)} 只")
    return all_items


def refresh_adj_factors():
    """每次全量重下：新增分红/送转会改变历史复权价，面板必须重建。"""
    r = api_json("/api/dump/market-dumps/adjustment-factors/download-url")
    if r.get("code") != 0:
        print(f"[adj] 失败: {r.get('message')}")
        return False
    size = download(r["data"]["presigned_url"], ADJ)
    print(f"[2/3] adj_factors 已刷新：{size/1e6:.1f} MB")
    return True


def _last_dates() -> dict:
    """返回 {thscode: last_date_ms}，同时看 dump 与增量文件。只读两列，省内存。"""
    frames = []
    for p in (DAILY_K, INCR):
        if os.path.exists(p):
            frames.append(pd.read_parquet(p, columns=["thscode", "date_ms"]))
    if not frames:
        print("[3/3] !! daily_k_10y.parquet 与增量文件都不存在，将走全量拉取路径")
        return {}
    t = pd.concat(frames, ignore_index=True)
    print(f"[3/3] 本地日K {len(t):,} 行")
    return t.groupby("thscode", sort=False)["date_ms"].max().to_dict()


def _fetch_one(code, start_ms, end_ms):
    url = (f"{BASE}/api/a-share/prices/historical?thscode={code}"
           f"&interval=1d&start={start_ms}&end={end_ms}&adjust=none")
    req = urllib.request.Request(url, headers={"X-api-key": KEY})
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                d = json.loads(r.read().decode())
            if d.get("code") == 0:
                return code, d["data"]["item"]
            if d.get("code") in (3002, 2003):   # 数据未就绪 / 无此标的
                return code, []
            time.sleep(0.6 * (i + 1))
        except Exception:  # noqa: BLE001
            time.sleep(0.8 * (i + 1))
    return code, []


def _ms_to_date(ms):
    return pd.to_datetime(ms, unit="ms", utc=True) \
             .tz_convert("Asia/Shanghai").tz_localize(None).date()


def incremental_fetch(since_ms=None):
    last = _last_dates()
    tickers = json.load(open(TICKERS))
    codes = sorted({t["thscode"] for t in tickers
                    if str(t["thscode"]).endswith((".SH", ".SZ", ".BJ"))})

    end_ms = int(time.time() * 1000)
    # 每只股票各自的起点：有记录 → 从最后一根 K 起（含当天，去重时以后到的为准）
    #                     无记录（新股）→ BASE_START_MS，拉全历史，避免被漏掉
    jobs = []
    n_new = 0
    for c in codes:
        if since_ms is not None:
            start = since_ms
        elif c in last:
            start = int(last[c])
        else:
            start = BASE_START_MS
            n_new += 1
        jobs.append((c, start))

    print(f"     待更新 {len(jobs)} 只（其中新股/无本地记录 {n_new} 只拉全历史）")
    if n_new:
        print(f"     {mark('⚠', '!')} 新股全历史较慢，属正常一次性开销，之后每只都只增量")

    rows, done, err = [], 0, 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        futs = {ex.submit(_fetch_one, c, s, end_ms): c for c, s in jobs}
        for f in as_completed(futs):
            code, items = f.result()
            done += 1
            if items is None:
                err += 1
                continue
            for it in items:
                rows.append((code, it.get("date_ms"), it.get("open_price"),
                             it.get("high_price"), it.get("low_price"),
                             it.get("close_price"), it.get("volume"),
                             it.get("turnover")))
            if done % 500 == 0:
                el = time.time() - t0
                print(f"     {done}/{len(jobs)}  新收 {len(rows):,} 行  "
                      f"{el:.0f}s ({done/el:.1f} req/s)", flush=True)

    if not rows:
        print("     没有拉取到任何新数据（可能尚未出新交易日）")
        return None

    inc = pd.DataFrame(rows, columns=["thscode", "date_ms", "open_price",
                                      "high_price", "low_price", "close_price",
                                      "volume", "turnover"])
    # currency/interval/adjusted 三列：dump 原文件为 "CNY"/"1d"/"none"
    inc["currency"] = "CNY"
    inc["interval"] = "1d"
    inc["adjusted"] = "none"
    inc = inc[DUMP_COLS]
    inc["date_ms"] = inc["date_ms"].astype("int64")
    return inc


def merge_into_incr(inc: pd.DataFrame) -> int:
    """把增量 K 线并入 daily_k_incr.parquet（小文件，永远舍不得重写 dump）。

    同一 (thscode, date_ms) 保留后到的那条 → 可修复「昨天那条没走完的 K」。
    """
    if os.path.exists(INCR):
        old = pd.read_parquet(INCR)
        n_before = len(old)
        out = pd.concat([old, inc], ignore_index=True)
        del old
        out = out.drop_duplicates(subset=["thscode", "date_ms"], keep="last")
    else:
        n_before = 0
        out = inc.drop_duplicates(subset=["thscode", "date_ms"], keep="last")
    out = out.sort_values(["thscode", "date_ms"], kind="stable").reset_index(drop=True)
    out = out[DUMP_COLS]

    # 先写临时文件再原子替换：中途失败不会损坏增量文件
    tmp = INCR + ".tmp"
    out.to_parquet(tmp, index=False, compression="zstd")
    os.replace(tmp, INCR)

    dates = sorted({_ms_to_date(int(x)) for x in out["date_ms"]})
    print(f"     增量文件 {len(out):,} 行（新增 {len(out)-n_before:,} 行）"
          f"  最新交易日 {dates[-1] if dates else '-'}")
    return len(out) - n_before


# ---------------------------------------------------------------- 主流程
def run_py(name, args=()):
    print(f"\n===== {name} {' '.join(args)} =====", flush=True)
    t0 = time.time()
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, name), *args],
                       cwd=ROOT)
    print(f"----- {name} 耗时 {time.time()-t0:.0f}s (exit={r.returncode}) -----")
    return r.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true",
                    help="连跑 02 + 21 重建面板（约 7 分钟）")
    ap.add_argument("--industry", action="store_true",
                    help="刷新行业映射（新股归属；建议每周一次）")
    ap.add_argument("--since", default=None,
                    help="手动指定起点日期 YYYY-MM-DD，用于回补某段区间")
    a = ap.parse_args()

    t0 = time.time()
    print("=" * 70)
    print("  A股数据每日增量更新")
    print("=" * 70)

    since_ms = int(pd.Timestamp(a.since).timestamp() * 1000) if a.since else None

    refresh_tickers()
    refresh_adj_factors()

    print("[3/3] 增量拉取日K ...")
    inc = incremental_fetch(since_ms)
    if inc is not None:
        merge_into_incr(inc)
    print(f"[3/3] 完成，耗时 {time.time()-t0:.0f}s")

    if a.industry:
        run_py("20_fetch_industry.py")

    if a.rebuild:
        if run_py("02_build_dataset.py") != 0:
            print("!! 02 失败，已中止"); return 1
        if run_py("21_build_v2_features.py") != 0:
            print("!! 21 失败，已中止"); return 1
        print("\n" + "=" * 70)
        print("  数据已就绪，重启服务后生效：")
        print("      python app/server.py 8770")
        print("=" * 70)
    else:
        print("\n未加 --rebuild，面板尚未重建。需要执行：")
        print("    python scripts/02_build_dataset.py")
        print("    python scripts/21_build_v2_features.py")
        print("    python app/server.py 8770")
    return 0


if __name__ == "__main__":
    sys.exit(main())
