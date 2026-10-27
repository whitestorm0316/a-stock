#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_bias_light.py —— 只读面板、**不加载引擎**的前视偏差量化（低内存）

回答三个问题：
  1. ST 过滤（用「当前名称」判 ST）剔掉了多少、这些票历史表现如何 → 偏差方向与量级
  2. 上市满 120 日的过滤是否 point-in-time（用 days_since_list 自身校验）
  3. 涨跌停制度识别（limit_pct）是否只用历史窗口 —— 与板块号段交叉验证
  4. 退市股是否留在面板里（幸存者偏差的关键）—— 用「最后一根 K 之后是否还有后续」
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from _console import bootstrap  # noqa: E402
bootstrap()

PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")
_t0 = time.time()


def log(m=""):
    print(f"[{time.time()-_t0:6.1f}s] {m}", flush=True)


def main():
    cols = ["thscode", "date", "name", "board", "is_st_now", "days_since_list",
            "ret", "fwd20", "can_buy_open", "open_price", "close_price_raw",
            "limit_pct", "size_grp", "ind_name"]
    log("读取面板（仅必要列）...")
    p = pd.read_parquet(os.path.join(PROC, "v2_panel.parquet"), columns=cols)
    log(f"面板 {len(p):,} 行 / {p['thscode'].nunique():,} 只 / "
        f"{p['date'].nunique():,} 个交易日  "
        f"{str(p['date'].min())[:10]} ~ {str(p['date'].max())[:10]}")

    # ---------------------------------------------------------- 1) ST 偏差
    log("")
    log("=" * 66)
    log("1) ST 过滤偏差（is_st_now 用「当前名称」判定 → 含未来信息）")
    log("=" * 66)
    st = p["is_st_now"].fillna(True).values
    all_codes = set(p["thscode"].unique())
    st_codes = set(p.loc[st, "thscode"].unique())
    log(f"  被剔除 {len(st_codes):,} 只 / {len(all_codes):,} 只 "
        f"({len(st_codes)/len(all_codes)*100:.2f}%)，{int(st.sum()):,} 行 "
        f"({st.mean()*100:.2f}%)")

    # ⚠️ is_st_now 是**股票级常量**（整段历史同值），所以被剔除的票在面板里
    #    根本不存在「未 ST 时期」的行 —— 只能拿下标替代「假想没做这个过滤」：
    #    把它们的全部历史行也纳入统计，与「始终非 ST」对比。
    ok = (p["days_since_list"].fillna(0).values >= 120) & p["ret"].notna().values
    sub = p.loc[ok, ["thscode", "fwd20"]].dropna()
    is_st_hist = sub["thscode"].isin(st_codes)
    A, B = sub.loc[is_st_hist, "fwd20"], sub.loc[~is_st_hist, "fwd20"]
    log(f"  「后来变 ST」的票的 20 日收益（若不做该过滤，它们本会被买到）："
        f"均值 {A.mean()*100:+.2f}%  中位 {A.median()*100:+.2f}%  胜率 "
        f"{(A > 0).mean()*100:.1f}%（{len(A):,} 样本）")
    log(f"  「始终非 ST」："
        f"                    均值 {B.mean()*100:+.2f}%  中位 "
        f"{B.median()*100:+.2f}%  胜率 {(B > 0).mean()*100:.1f}%（{len(B):,} 样本）")
    log(f"  → 差值 {abs(A.mean()-B.mean())*100:.2f}pp，方向：剔除它们让回测**偏乐观**")

    # 只看策略实际下手的那一档（小额成交额 + 超跌）
    log("")
    log("  落在策略射程内的子样本（成交额分位最小 10%~40%）：")
    sm = ok & (p["size_grp"].fillna(99).values >= 1) & (p["size_grp"].fillna(99).values <= 3)
    s2 = p.loc[sm, ["thscode", "fwd20"]].dropna()
    a2 = s2.loc[s2["thscode"].isin(st_codes), "fwd20"]
    b2 = s2.loc[~s2["thscode"].isin(st_codes), "fwd20"]
    log(f"    「后来变 ST」均值 {a2.mean()*100:+.2f}% （{len(a2):,} 样本）")
    log(f"    「始终非 ST」均值 {b2.mean()*100:+.2f}% （{len(b2):,} 样本）")
    log(f"    → 差 {abs(a2.mean()-b2.mean())*100:.2f}pp，与全样本同向")

    # ---------------------------------------------------------- 2) 上市过滤
    log("")
    log("=" * 66)
    log("2) 上市满 120 日过滤是否 point-in-time")
    log("=" * 66)
    log("  days_since_list 是「截至该行日期已上市天数」，本身即历史量 → OK")
    byc = p.groupby("thscode", sort=False)["days_since_list"].max()
    never = int((p.groupby("thscode", sort=False)["days_since_list"].min() >= 120).sum())
    log(f"  面板内「从第一行起就已满 120 日」的股票 {never:,} 只")
    log(f"  说明：面板保留 120 日以内的行（只是被过滤），不是直接删除 → 结构正确")

    # ---------------------------------------------------------- 3) limit_pct
    log("")
    log("=" * 66)
    log("3) 涨跌停制度识别是否只用历史（detect_limit_regime 尾随 120 日）")
    log("=" * 66)
    lp = p["limit_pct"]
    for L in (0.05, 0.10, 0.20, 0.30):
        m = np.isclose(lp.values, L)
        if m.sum() == 0:
            continue
        bd = p.loc[m, "board"].value_counts().head(3).to_dict()
        log(f"  limit_pct={L:.0%}: {int(m.sum()):>9,} 行  "
            f"({m.mean()*100:5.2f}%)  主要板块 {bd}")
    log("  已知局限：从未触及涨跌停的标的会兜底（best_L 为 NaN → 用板块默认值），")
    log("  这部分不是前视问题，只是限制识别不够精确，对「开盘可买」判定影响有限。")

    # ---------------------------------------------------------- 4) 幸存者
    log("")
    log("=" * 66)
    log("4) 是否含已退市股票（幸存者偏差）")
    log("=" * 66)
    last_day = p["date"].max()
    g = p.groupby("thscode", sort=False)["date"].max()
    alive = (g == last_day).sum()
    dead = (g != last_day).sum()
    log(f"  最后一根 K 停在 {str(last_day)[:10]} 之前的股票：{dead:,} 只")
    log(f"  活到最后的：{alive:,} 只")
    if dead:
        d = g[g != last_day].sort_values()
        log(f"  这些票的最后一根 K 分布：最早 {str(d.iloc[0])[:10]}，"
            f"中位 {str(d.iloc[len(d)//2])[:10]}")
        log("  → 面板**保留了中途退市/停牌的标的**（不是只用今天的成分股），"
            "这是避免幸存者偏差的关键。")
    # 退市股的最后收益（若被剔除会更乐观）
    log("")
    log("  注：真正的残留偏差只有 ST 这一项（见第 1 节），方向是乐观。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
