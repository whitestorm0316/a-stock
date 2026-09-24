#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拉取 A 股财务报告「真实披露日」，用于动态（无前视偏差）财务筛选。

数据源分工
----------
1. 东方财富 数据中心 RPT_PUBLIC_BS_APPOIN
   → 提供 SECUCODE / REPORT_DATE(报告期末) / ACTUAL_PUBLISH_DATE(实际披露日) / QUARTER
   → 这是**权威披露日**，覆盖全市场，可按 REPORT_YEAR 分页全量拉取。

2. 同花顺 fuyao API /api/a-share/financials/income-statements
   → 提供 operating_income（营业收入）/ parent_holder_net_profit（归母净利润）
   → 注意：其 report_date_ms 字段是上游批量回填戳，**不可用作披露日**，仅取金额。

产出
----
data/raw/fin_announce.parquet   披露日表（thscode, report_end, publish_date, quarter）
data/raw/fin_income.parquet     利润表（thscode, report_end, operating_income, parent_holder_net_profit）
data/processed/fin_panel.parquet  合并后的财务面板（按 thscode + report_end 唯一）

用法
----
python scripts/fetch_fin_announce.py --step announce   # 只拉披露日
python scripts/fetch_fin_announce.py --step income    # 只拉利润表
python scripts/fetch_fin_announce.py --step merge     # 合并
python scripts/fetch_fin_announce.py --step all       # 全流程
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
PROC = os.path.join(ROOT, "data", "processed")
os.makedirs(PROC, exist_ok=True)

FUYAO_KEY = os.environ.get("FUYAO_API_KEY", "sk-fuyao-LcCu-ioaupkOIvh4ucQ_Ab6wJhefxdQG")
FUYAO_BASE = "https://fuyao.aicubes.cn"

EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}

# 覆盖回测区间：2014 年报到 2026 年
YEARS = list(range(2014, 2027))


def _post(url: str, data: dict[str, Any], headers: dict[str, str], retries: int = 4,
          timeout: int = 45) -> dict[str, Any]:
    """带重试的 POST（东财接口偶发 9501/超时）。"""
    body = urllib.parse.urlencode(data).encode()
    last: Exception | None = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.6 * (k + 1))
    raise RuntimeError(f"请求失败 {url}: {last}")


def _get(url: str, headers: dict[str, str], retries: int = 4, timeout: int = 45) -> dict[str, Any]:
    last: Exception | None = None
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.6 * (k + 1))
    raise RuntimeError(f"请求失败 {url}: {last}")


# ---------------------------------------------------------------- 披露日
EM_COLS = ("SECUCODE,SECURITY_CODE,REPORT_DATE,ACTUAL_PUBLISH_DATE,"
           "APPOINT_PUBLISH_DATE,REPORT_TYPE_NAME,QUARTER,REPORT_YEAR,IS_PUBLISH")


def fetch_announce_year(year: int, page_size: int = 500) -> pd.DataFrame:
    """拉取某一年度全部 A 股的报告披露信息（分页）。"""
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = {
            "reportName": "RPT_PUBLIC_BS_APPOIN",
            "columns": EM_COLS,
            "filter": f'(REPORT_YEAR="{year}")(SECURITY_TYPE_CODE="058001001")',
            "pageNumber": page,
            "pageSize": page_size,
            "sortColumns": "REPORT_DATE,SECURITY_CODE",
            "sortTypes": "-1,1",
            "source": "WEB",
            "client": "WEB",
        }
        d = _post(EM_URL, payload, EM_HEADERS)
        res = d.get("result")
        if not res or not res.get("data"):
            break
        rows.extend(res["data"])
        pages = res.get("pages") or 1
        if page >= pages:
            break
        page += 1
        time.sleep(0.12)          # 温和限速
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    print(f"  [announce] {year}: {len(df)} 行 ({page} 页)", flush=True)
    return df


def step_announce() -> pd.DataFrame:
    print("[1/3] 拉取财报披露日（东方财富）...", flush=True)
    frames = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fetch_announce_year, y): y for y in YEARS}
        for f in as_completed(futs):
            y = futs[f]
            try:
                d = f.result()
                if len(d):
                    frames.append(d)
            except Exception as e:  # noqa: BLE001
                print(f"  !! {y} 失败: {e}", flush=True)
    if not frames:
        raise SystemExit("披露日拉取失败：无数据")
    df = pd.concat(frames, ignore_index=True)

    # 标准化
    out = pd.DataFrame({
        "thscode": df["SECUCODE"].astype(str).str.strip(),
        "report_end": pd.to_datetime(df["REPORT_DATE"], errors="coerce"),
        "publish_date": pd.to_datetime(
            df["ACTUAL_PUBLISH_DATE"].fillna(df["APPOINT_PUBLISH_DATE"]), errors="coerce"),
        "quarter": df["QUARTER"].astype(str).str.strip().str.lower(),
        "report_type": df["REPORT_TYPE_NAME"].astype(str),
    })
    out = out.dropna(subset=["thscode", "report_end", "publish_date"])
    out = out[out["thscode"].str.contains(r"\.(SH|SZ|BJ)$", regex=True, na=False)]
    # 同一 (股票, 报告期) 保留最早披露日（首次公开即已知）
    out = out.sort_values("publish_date").drop_duplicates(["thscode", "report_end"], keep="first")
    out = out.reset_index(drop=True)
    out.to_parquet(os.path.join(RAW, "fin_announce.parquet"), index=False)
    print(f"[1/3] 完成：{len(out)} 条披露记录，覆盖 "
          f"{out['thscode'].nunique()} 只股票", flush=True)
    return out


# ---------------------------------------------------------------- 利润表
# 区间模式窗口上限 10 年 → 拆成两段覆盖 2014-01 ~ 2027-01
_WINDOWS = [
    (1388505600000, 1704038400000),   # 2014-01-01 ~ 2024-01-01
    (1704038400000, 1798761600000),   # 2024-01-01 ~ 2027-01-01
]


def fetch_income_one(thscode: str) -> list[dict[str, Any]]:
    """拉取单只股票全部季报利润表（时间区间模式，分两段规避 10 年窗口上限）。"""
    items: list[dict[str, Any]] = []
    for start, end in _WINDOWS:
        url = (f"{FUYAO_BASE}/api/a-share/financials/income-statements?"
               + urllib.parse.urlencode({
                   "thscode": thscode,
                   "period": "quarterly",
                   "start": start,
                   "end": end,
               }))
        try:
            d = _get(url, {"X-api-key": FUYAO_KEY})
        except Exception:  # noqa: BLE001
            continue
        if d.get("code") != 0 or not d.get("data"):
            continue
        items.extend(d["data"].get("item") or [])
        time.sleep(0.05)
    return items


def step_income(limit: int | None = None, workers: int = 8) -> pd.DataFrame:
    print("[2/3] 拉取利润表（同花顺 fuyao）...", flush=True)
    # 从 v2 面板取全部 thscode
    import pyarrow.parquet as pq
    panel_path = os.path.join(PROC, "v2_panel.parquet")
    codes = pq.read_table(panel_path, columns=["thscode"]).column("thscode").unique().to_pylist()
    codes = sorted({str(c) for c in codes if str(c).endswith((".SH", ".SZ", ".BJ"))})
    if limit:
        codes = codes[:limit]
    print(f"  待拉取 {len(codes)} 只股票", flush=True)

    rows: list[dict[str, Any]] = []
    done = 0
    cache_path = os.path.join(RAW, "fin_income.parquet")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_income_one, c): c for c in codes}
        for f in as_completed(futs):
            code = futs[f]
            done += 1
            try:
                for it in f.result():
                    rows.append({
                        "thscode": code,
                        "report_end": it.get("period_end_ms"),
                        "operating_income": it.get("operating_income"),
                        "parent_holder_net_profit": it.get("parent_holder_net_profit"),
                        "net_profit": it.get("net_profit"),
                        "fiscal_year": it.get("fiscal_year"),
                        "fiscal_period": it.get("fiscal_period"),
                    })
            except Exception as e:  # noqa: BLE001
                print(f"  !! {code}: {e}", flush=True)
            if done % 200 == 0:
                print(f"  {done}/{len(codes)} ... 已收 {len(rows)} 行", flush=True)
    df = pd.DataFrame(rows)
    if len(df) == 0:
        raise SystemExit("利润表拉取失败：无数据")
    # ⚠️ 同花顺 period_end_ms 是 Asia/Shanghai 零点，直接按 UTC 转会偏 1 天
    #    （2023-12-31 期末 → 2023-12-30）。改用 fiscal_year + fiscal_period 归一季末，
    #    彻底规避时区/日差，保证与东财 REPORT_DATE 严格对齐。
    df["fiscal_year"] = df["fiscal_year"].astype("Int64")
    df["fiscal_period"] = df["fiscal_period"].astype(str)
    _QP = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 4}
    df["q"] = df["fiscal_period"].map(_QP).astype("Int64")
    df = df[df["q"].notna()].copy()
    qe_m = df["q"].map({1: 3, 2: 6, 3: 9, 4: 12}).astype(int)
    df["report_end"] = pd.to_datetime(
        dict(year=df["fiscal_year"].astype(int), month=qe_m.values, day=1)) \
        + pd.offsets.MonthEnd(0)
    df = df.drop_duplicates(["thscode", "report_end"]).reset_index(drop=True)
    df.to_parquet(cache_path, index=False)
    print(f"[2/3] 完成：{len(df)} 行，覆盖 {df['thscode'].nunique()} 只股票", flush=True)
    return df


# ---------------------------------------------------------------- 合并
# 季度 → 报告期末（月-日）
_Q_MD = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def _q_of(report_end: pd.Timestamp) -> int:
    m = report_end.month
    return {3: 1, 6: 2, 9: 3, 12: 4}.get(m, 0)


def step_merge() -> pd.DataFrame:
    print("[3/3] 合并披露日 + 利润表 ...", flush=True)
    an = pd.read_parquet(os.path.join(RAW, "fin_announce.parquet"))
    inc = pd.read_parquet(os.path.join(RAW, "fin_income.parquet"))
    inc["report_end"] = pd.to_datetime(inc["report_end"])
    an["report_end"] = pd.to_datetime(an["report_end"])

    # 只保留标准报告期末（3/6/9/12 月末），剔除异常期
    inc = inc[inc["report_end"].dt.month.isin([3, 6, 9, 12])].copy()

    m = inc.merge(an[["thscode", "report_end", "publish_date"]],
                  on=["thscode", "report_end"], how="left")

    q = m["report_end"].dt.month.map({3: 1, 6: 2, 9: 3, 12: 4}).astype("int8")
    yr = m["report_end"].dt.year

    # 兜底披露日：法定披露截止日（Q1→5/1, Q2→9/1, Q3→11/1, Q4→次年5/1）
    legal = pd.to_datetime(
        np.where(q == 1, yr.astype(str) + "-05-01",
                 np.where(q == 2, yr.astype(str) + "-09-01",
                          np.where(q == 3, yr.astype(str) + "-11-01",
                                   (yr + 1).astype(str) + "-05-01"))))
    pub = pd.to_datetime(m["publish_date"])
    # 缺失 → 法定截止日；早于期末（异常）→ 法定截止日
    bad = pub.isna() | (pub < m["report_end"])
    pub = pub.where(~bad, pd.Series(legal, index=m.index))
    m["publish_date"] = pub

    m["q"] = q.values
    # 来源标记：真实披露日 vs 法定截止日兜底
    actual_keys = set(zip(an["thscode"], an["report_end"]))
    m["src"] = np.where(
        [(a, b) in actual_keys and not bl for (a, b), bl in zip(zip(m["thscode"], m["report_end"]), bad)],
        "actual", "legal")

    m = m.sort_values(["thscode", "report_end"]).reset_index(drop=True)
    out = os.path.join(PROC, "fin_panel.parquet")
    m.to_parquet(out, index=False)

    n_act = int((m["src"] == "actual").sum())
    print(f"[3/3] 完成：{len(m)} 行 → {out}", flush=True)
    print(f"      真实披露日 {n_act} 行 ({n_act/len(m):.1%})，"
          f"法定截止日兜底 {len(m)-n_act} 行", flush=True)
    print(f"      覆盖 {m['thscode'].nunique()} 只股票，"
          f"报告期 {m['report_end'].min():%Y-%m} ~ {m['report_end'].max():%Y-%m}", flush=True)
    return m
    return m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", default="all",
                    choices=["announce", "income", "merge", "all"])
    ap.add_argument("--limit", type=int, default=None, help="income 步骤限制股票数（调试）")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    t0 = time.time()
    if a.step in ("announce", "all"):
        step_announce()
    if a.step in ("income", "all"):
        step_income(limit=a.limit, workers=a.workers)
    if a.step in ("merge", "all"):
        step_merge()
    print(f"\n耗时 {time.time()-t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
