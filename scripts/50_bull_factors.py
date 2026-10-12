#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
50_bull_factors.py —— 牛市里什么因子有效？（逐日面板 + 牛市区分子样本）

为什么单独一个脚本
------------------
主项目（V3B）研究的是「熊市超跌反转」。牛市该买什么，是**另一个问题**，
且方向可能相反（超跌反转在牛市里可能恰恰是反向因子）。所以：
  · 不改动引擎，不动现有默认参数；
  · 用同一份面板、同一套收益口径（oret_sig），保证可比较。

牛市区间的定义（必须显式写清，否则结论会被口径污染）
----------------------------------------------------
  bull[t] = 自建全A等权净值 > 自身 MA120           （逐日，非采样）
  且要求连续区间 >= 20 个交易日（滤掉穿越噪音）
  另外只取 2018-07 之后 —— 与「真实指数对照」的数据窗口一致（见 README）。

⚠️ 前视偏差纪律
---------------
  因子分档用**当日可见**的字段（px_ma60_pct / ind_r20 / rs_mkt20 …），
  收益用 `oret_sig`（T日收盘信号 → T+1开盘买 → T+2开盘卖那一套的日收益）。
  绝不使用 fwd* 列去构造分档 —— 那些是**答案**。

输出
----
  reports/50_bull_factors.md     结论表
  控制台打印明细（分档收益 / 胜率 / t 值 / IC）
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from v3b_lib import oret_from, market_oret, shift_block  # noqa: E402

PANEL = os.path.join(ROOT, "data", "processed", "v2_panel.parquet")
OUT = os.path.join(ROOT, "reports", "50_bull_factors.md")

COLS = [
    "thscode", "date", "board", "ind_name", "size_grp",
    "open_price", "close_price", "ret", "turnover",
    # ⚠️ 面板里**没有**绝对市值/流通市值列，只有 `size_grp`（当日市值分位 0..9）。
    #    所以「大小盘」只能按分位来切，不能算真实的市值加权 —— 见报告的限制说明。
    "px_ma20_pct", "px_ma60_pct", "px_ma120_pct",
    "ret5", "ret20", "ret60",
    "ind_r20", "ind_r60", "rs_mkt20", "rs_mkt60", "rs_ind20",
    "rvol20", "vr20m", "vt_5_20", "volz20", "ud_vol20",
    "dif", "dea", "hist", "hist_pos", "dif_pos",
    "hv20", "atr_pct",
]


def log(m):
    print(m, flush=True)


def load():
    log("读取面板（约 3.7GB，稍等）…")
    t0 = time.time()
    df = pd.read_parquet(PANEL, columns=COLS)
    log(f"  完成：{len(df):,} 行 / {df['thscode'].nunique():,} 只 / {time.time()-t0:.1f}s")
    return df


def add_day_idx(df):
    """把 date 换成 0..nd-1 的交易日序号（所有按日的操作都要用它）。"""
    dates = np.sort(df["date"].unique())
    dmap = {d: i for i, d in enumerate(dates)}
    df["day"] = df["date"].map(dmap).astype(np.int32)
    return df, dates


def build_oret(df, nd):
    """构造与引擎一致的 oret_sig（T收盘信号 → T+1开盘买 → T+2开盘卖）。"""
    C = dict(n_days=int(nd))
    df = df.sort_values(["thscode", "day"], kind="mergesort").reset_index(drop=True)
    # oret[t] = open[t+1]/open[t]-1，按股票分块 shift
    op = df["open_price"].to_numpy(np.float64)
    code = df["thscode"].to_numpy()
    same = np.empty(len(df), bool)
    same[0] = False
    same[1:] = code[1:] == code[:-1]
    nxt = np.full(len(df), np.nan)
    nxt[:-1] = np.where(same[1:], op[1:], np.nan)
    oret = nxt / op - 1.0
    # oret_sig[t] = oret[t+1]
    sig = np.full(len(df), np.nan)
    sig[:-1] = np.where(same[1:], oret[1:], np.nan)
    df["oret_sig"] = sig
    return df


def bull_mask_daily(df, dates):
    """逐日牛市标记（净值 vs MA120，且区间>=20交易日）。用面板算日度等权收益。"""
    nd = len(dates)
    # 日度收益 = 当日 ret 的等权均值（ret 是 close/close_prev-1）
    r = df["ret"].to_numpy(np.float64)
    day = df["day"].to_numpy(np.int64)
    ok = np.isfinite(r)
    cnt = np.bincount(day[ok], minlength=nd)
    s = np.bincount(day[ok], weights=r[ok], minlength=nd)
    mk = np.where(cnt > 0, s / np.maximum(cnt, 1), 0.0)
    nav = np.cumprod(1.0 + mk)
    W = 120
    ma = np.full(nd, np.nan)
    cs = np.concatenate([[0.0], np.cumsum(nav)])
    for i in range(W - 1, nd):
        ma[i] = (cs[i + 1] - cs[i + 1 - W]) / W
    up = nav > ma
    # 只保留 >=20 交易日的连续段
    keep = np.zeros(nd, bool)
    i = 0
    while i < nd:
        if up[i]:
            j = i
            while j + 1 < nd and up[j + 1]:
                j += 1
            if j - i + 1 >= 20:
                keep[i:j + 1] = True
            i = j + 1
        else:
            i += 1
    return keep, nav, ma, mk


def ic_by_day(df, col, ret, day, nd, sub=None):
    """日度 Spearman IC（逐日算再平均，避免横截面规模差异污染）。"""
    d = df[col].to_numpy(np.float64)
    r = ret
    ok = np.isfinite(d) & np.isfinite(r)
    if sub is not None:
        ok &= sub
    dd, ddd = d[ok], day[ok]
    rr = r[ok]
    ics = []
    order = np.argsort(ddd, kind="mergesort")
    dd, ddd, rr = dd[order], ddd[order], rr[order]
    idx = np.searchsorted(ddd, np.arange(nd))
    idx = np.append(idx, len(ddd))
    for i in range(nd):
        a, b = idx[i], idx[i + 1]
        if b - a < 30:
            continue
        x, y = dd[a:b], rr[a:b]
        rx = pd.Series(x).rank().to_numpy()
        ry = pd.Series(y).rank().to_numpy()
        sx, sy = rx.std(), ry.std()
        if sx < 1e-12 or sy < 1e-12:
            continue
        ics.append(float(((rx - rx.mean()) * (ry - ry.mean())).mean() / (sx * sy)))
    if not ics:
        return None, None, 0
    ics = np.array(ics)
    t = ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics))) if len(ics) > 1 else np.nan
    return float(ics.mean()), float(t), len(ics)


def groups(df, col, ret, day, nd, sub, q=5, asc=True):
    """按 col 分 q 档（sub 为样本掩码），返回每档的平均收益/胜率/样本数。"""
    v = df[col].to_numpy(np.float64)
    ok = np.isfinite(v) & np.isfinite(ret) & sub
    if ok.sum() < 500:
        return None
    vv, rr = v[ok], ret[ok]
    # 逐日分档（避免跨日混档）
    dd = day[ok]
    lab = np.full(len(vv), -1, np.int8)
    o = np.argsort(dd, kind="mergesort")
    vv, rr, dd = vv[o], rr[o], dd[o]
    idx = np.searchsorted(dd, np.arange(nd))
    idx = np.append(idx, len(vv))
    for i in range(nd):
        a, b = idx[i], idx[i + 1]
        n = b - a
        if n < q * 5:
            continue
        x = vv[a:b]
        # 用 rank 分档（抗极值）
        rk = pd.Series(x).rank(method="first").to_numpy()
        g = np.minimum((rk - 1) / (n / q), q - 1).astype(np.int8)
        lab[a:b] = g
    rows = []
    for g in range(q):
        m = lab == g
        if m.sum() < 100:
            rows.append(None)
            continue
        x = rr[m]
        rows.append(dict(n=int(m.sum()), mean=float(np.nanmean(x) * 100),
                         win=float(np.nanmean(x > 0) * 100)))
    return rows


def main():
    t_all = time.time()
    df = load()
    df, dates = add_day_idx(df)
    nd = len(dates)
    df = build_oret(df, nd)
    log(f"交易日 {nd} 天：{dates[0]} ~ {dates[-1]}")

    ret = df["oret_sig"].to_numpy(np.float64)
    day = df["day"].to_numpy(np.int64)

    keep, nav, ma, mk = bull_mask_daily(df, dates)
    n_bull = int(keep.sum())
    log(f"牛市日 {n_bull} / {nd} = {n_bull/nd:.1%}")

    # 只用有收益的行
    base = np.isfinite(ret)
    bull = base & keep[day]
    bear = base & (~keep[day])
    log(f"可用样本：牛市 {int(bull.sum()):,} 行 / 非牛市 {int(bear.sum()):,} 行")

    FAC = [
        ("px_ma60_pct",   "距MA60（%）",        True),
        ("px_ma120_pct",  "距MA120（%）",       True),
        ("ret5",          "近5日涨幅",           True),
        ("ret20",         "近20日涨幅",          True),
        ("ret60",         "近60日涨幅",          True),
        ("ind_r20",       "所属行业20日涨幅",     True),
        ("ind_r60",       "所属行业60日涨幅",     True),
        ("rs_mkt20",      "相对大盘强弱(20日)",   True),
        ("rs_mkt60",      "相对大盘强弱(60日)",   True),
        ("rs_ind20",      "相对行业强弱(20日)",   True),
        ("rvol20",        "20日量比",            True),
        ("vr20m",         "量能比(20日)",        True),
        ("vt_5_20",       "5/20日量能趋势",      True),
        ("volz20",        "20日量能Z分",         True),
        ("ud_vol20",      "20日涨跌量比",        True),
        ("hist",          "MACD柱",             True),
        ("hv20",          "20日波动率",          True),
        ("atr_pct",       "ATR占比",            True),
    ]

    L = []
    L.append("# 牛市因子研究（2018-07 ~ 2026-10，牛市子样本）\n")
    L.append(f"- 牛市日 **{n_bull} / {nd} = {n_bull/nd:.1%}**"
             f"（自建全A等权净值 > 自身 MA120，连续段 ≥20 交易日）")
    L.append(f"- 样本：牛市 {int(bull.sum()):,} 行 / 非牛市 {int(bear.sum()):,} 行")
    L.append(f"- 收益口径：`oret_sig`（T收盘信号 → T+1开盘买 → T+2开盘卖）")
    L.append(f"- 分档：**逐日内按 rank 分 5 档**（抗极值、不跨日混档）\n")

    log("\n" + "=" * 92)
    log("  单因子：牛市 vs 非牛市 的 5 档收益（每档平均日收益 %）")
    log("=" * 92)

    L.append("## 单因子分档（牛市样本，逐日 rank 5 档）\n")
    L.append("| 因子 | Q1 | Q2 | Q3 | Q4 | Q5 | Q5−Q1 | IC | t(IC) | 方向 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")

    log(f"\n{'因子':<20}{'Q1':>8}{'Q3':>8}{'Q5':>8}{'Q5-Q1':>9}{'IC':>8}{'t':>7}  样本")
    for col, name, asc in FAC:
        for label, sub in (("牛市", bull), ("非牛市", bear)):
            g = groups(df, col, ret, day, nd, sub)
            if not g or any(x is None for x in g):
                continue
            ic, t, n_ic = ic_by_day(df, col, ret, day, nd, sub)
            q1, q5 = g[0]["mean"], g[-1]["mean"]
            if label == "牛市":
                L.append(f"| {name} | {g[0]['mean']:+.3f} | {g[1]['mean']:+.3f} | "
                         f"{g[2]['mean']:+.3f} | {g[3]['mean']:+.3f} | {g[4]['mean']:+.3f} | "
                         f"**{q5-q1:+.3f}** | {ic:+.4f} | {t:+.1f} | "
                         f"{'Q5强' if q5>q1 else 'Q1强'} |")
            if label == "牛市":
                log(f"{name:<20}{q1:>8.3f}{g[2]['mean']:>8.3f}{q5:>8.3f}"
                    f"{q5-q1:>9.3f}{ic:>8.4f}{t:>7.1f}  {int(sum(x['n'] for x in g)):,}")

    log("\n" + "=" * 92)
    log("  非牛市对照（同样分档）")
    log("=" * 92)
    log(f"\n{'因子':<20}{'Q1':>8}{'Q3':>8}{'Q5':>8}{'Q5-Q1':>9}{'IC':>8}{'t':>7}")
    for col, name, asc in FAC:
        g = groups(df, col, ret, day, nd, bear)
        if not g or any(x is None for x in g):
            continue
        ic, t, n_ic = ic_by_day(df, col, ret, day, nd, bear)
        log(f"{name:<20}{g[0]['mean']:>8.3f}{g[2]['mean']:>8.3f}{g[4]['mean']:>8.3f}"
            f"{g[4]['mean']-g[0]['mean']:>9.3f}{ic:>8.4f}{t:>7.1f}")

    # ---------------- 市值 / 板块 分组（牛市） ----------------
    log("\n" + "=" * 92)
    log("  市值分位 × 板块（牛市样本，平均日收益 % / 胜率 %）")
    log("=" * 92)
    sz = df["size_grp"].to_numpy(np.float64)
    bd = df["board"].to_numpy()
    log(f"\n{'size_grp':<10}{'均值%':>9}{'胜率%':>9}{'n':>12}")
    for s in range(10):
        m = bull & (sz == s)
        if m.sum() < 200:
            continue
        x = ret[m]
        log(f"{s:<10}{np.nanmean(x)*100:>9.3f}{np.nanmean(x>0)*100:>9.1f}{int(m.sum()):>12,}")

    log(f"\n{'板块':<10}{'均值%':>9}{'胜率%':>9}{'n':>12}")
    for b in sorted(set(bd)):
        m = bull & (bd == b)
        if m.sum() < 200:
            continue
        x = ret[m]
        log(f"{b:<10}{np.nanmean(x)*100:>9.3f}{np.nanmean(x>0)*100:>9.1f}{int(m.sum()):>12,}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    log(f"\n报告已写：{OUT}")
    log(f"总耗时 {time.time()-t_all:.1f}s")


if __name__ == "__main__":
    main()
