#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
52_bull_robust.py —— 牛市结论的稳健性检验

为什么必须做
------------
上一轮（51）得到两个反直觉结论：
  · 「牛市买入」整体不如「熊市买入」（CAGR 18% vs 30%）
  · 牛市里换 pick（大市值/成交额）**更差**，小市值/超跌反而好

但 A股 2015-2026 只经历过有限的几段牛市。任何「牛市结论」都可能只是
被 2019Q1 / 2020H2 / 2024Q4 这几段带出来的 —— 那就是**过拟合到少数样本**。

本脚本做三件事：
  1. **分年度**看「牛市策略 vs 熊市策略」谁赢 —— 若只在少数年份赢，不可信；
  2. **逐段牛市**拆解（用 50 号脚本的 MA120 区间定义）；
  3. **随机对照**：把 pick 换成 rand 跑 N 次，看真实规则的超额是否落在分布之外。

输出：reports/52_bull_robust.md
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PORT") or 8770)
BASE = f"http://127.0.0.1:{PORT}"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
OUT = os.path.join(ROOT, "reports", "52_bull_robust.md")


def api(path, body=None):
    url = BASE + path
    req = (urllib.request.Request(url) if body is None else
           urllib.request.Request(url, data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"}))
    with _opener.open(req, timeout=900) as r:
        return json.loads(r.read().decode())


POOL = {"mode": "all", "boards": ["MAIN", "CHINEXT", "STAR"], "exchanges": ["SH", "SZ"]}
CAP = dict(max_pos=10, max_new=3)


def P(**kw):
    p = dict(px_ma60_min=1, px_ma60_max=1, size_min=0, size_max=2, hold=20,
             mkt_index="all", mkt_ma=60, pick="deep",
             delist={"financial": True, "loss2y": False, "penny": True},
             delist_rev_floor=100000000, delist_main_2024=True,
             delist_penny_days=10)
    p.update(CAP)
    p.update(kw)
    return p


def bt(p):
    return api("/api/backtest", {"params": p, "pool": POOL})


def yearly(d):
    """capacity.yearly → {year: ret}"""
    c = d.get("capacity") or {}
    return {int(y["year"]): y["ret"] for y in (c.get("yearly") or [])}


def main():
    t0 = time.time()
    L = ["# 牛市结论稳健性检验\n"]

    print("=" * 96)
    print("  1) 分年度：牛市策略 vs 熊市策略")
    print("=" * 96)
    bear = bt(P(mkt_state="bear"))
    bull = bt(P(mkt_state="bull"))
    yb, yu = yearly(bear), yearly(bull)

    print(f"{'年':<8}{'熊市策略':>12}{'牛市策略':>12}{'差值':>12}  谁赢")
    L.append("## 1. 分年度对比（账户资金口径年度收益）\n")
    L.append("| 年份 | 熊市策略 | 牛市策略 | 差 | 谁赢 |")
    L.append("|---|---|---|---|---|")
    win_b = win_u = 0
    for y in sorted(set(yb) | set(yu)):
        a, b = yb.get(y), yu.get(y)
        if a is None or b is None:
            continue
        if a > b:
            win_b += 1
            who = "熊市 ✅"
        else:
            win_u += 1
            who = "牛市"
        print(f"{y:<8}{a*100:>11.2f}%{b*100:>11.2f}%{(a-b)*100:>11.2f}pp  {who}")
        L.append(f"| {y} | {a*100:+.2f}% | {b*100:+.2f}% | {(b-a)*100:+.2f}pp | {who} |")
    print(f"\n熊市策略赢 {win_b} 年 / 牛市策略赢 {win_u} 年")
    L.append(f"\n**熊市策略赢 {win_b} 年 / 牛市策略赢 {win_u} 年。**\n")

    # ---------------- 2) pick 规则的随机对照 ----------------
    # ⚠️ 这里**不能**自己传 pick="rand" 去采样 —— 引擎的口径是：
    #    `pick="rand"` 表示「我自己就是随机基准」，**不会**再附随机分布；
    #    只有传**真实规则**时，引擎才跑 N 个种子的随机对照，把
    #    rand_mean / rand_sd / rand_pctile 一起回传（见 engine.capacity 的 n_sim 分支）。
    #    我第一次写错成循环调 pick="rand"，结果 12 次拿到完全相同的一个数
    #    （种子固定），标准差 0 → z 值 nan，白跑。
    print()
    print("=" * 96)
    print("  2) 随机对照（由引擎内置：传真实规则时自动附随机分布）")
    print("=" * 96)
    L.append("## 2. 随机对照（引擎内置多种子）\n")
    L.append("| 规则 | cap_cagr | 随机均值 | 随机标准差 | z | 分位 | 判定 |")
    L.append("|---|---|---|---|---|---|---|")
    print(f"{'规则':<12}{'cap_cagr':>10}{'随机均值':>10}{'随机SD':>9}"
          f"{'z':>8}{'分位':>8}  判定")
    for pk, nm in (("deep", "最超跌"), ("small", "小市值"), ("big", "大市值"),
                   ("amount", "成交额")):
        d = bt(P(mkt_state="bull", pick=pk))
        c = d.get("capacity") or {}
        v = c.get("cap_cagr")
        mu = c.get("rand_mean")
        sd = c.get("rand_sd")
        pct = c.get("rand_pctile")
        ns = c.get("n_sim")
        if v is None or mu is None:
            print(f"{nm:<12} 无随机对照（n_sim={ns}）")
            continue
        z = (v - mu) / sd if sd and sd > 1e-9 else float("nan")
        if z > 2:
            verdict = "显著优于随机 ✅"
        elif z < -2:
            verdict = "显著差于随机 ⚠️"
        else:
            verdict = "与随机无差异"
        print(f"{nm:<12}{v*100:>9.2f}%{mu*100:>9.2f}%{sd*100:>8.2f}%"
              f"{z:>8.2f}{(pct*100 if pct is not None else float('nan')):>7.1f}%  {verdict}")
        L.append(f"| {nm} | {v*100:+.2f}% | {mu*100:.2f}% | {sd*100:.2f}% | "
                 f"{z:+.2f} | {(pct*100 if pct is not None else float('nan')):.1f}% | "
                 f"{verdict} |")
    L.append(f"\n> 随机模拟次数 n_sim = {ns}（引擎默认）。"
             "分位 = 真实规则的结果在随机分布中的位置。\n")

    # ---------------- 3) 牛市区间的收益贡献 ----------------
    print()
    print("=" * 96)
    print("  3) 牛市 vs 非牛市：收益到底从哪来")
    print("=" * 96)
    for nm, d in (("熊市策略", bear), ("牛市策略", bull),
                  ("不择时", bt(P(mkt_state="any")))):
        c = d.get("capacity") or {}
        print(f"  {nm:<10} CAGR {c.get('cap_cagr',0)*100:>7.2f}%  "
              f"MDD {c.get('cap_mdd',0)*100:>7.2f}%  "
              f"Sharpe {c.get('cap_sharpe',0):>5.2f}  "
              f"利用率 {c.get('avg_invested',0)*100:>6.1f}%  "
              f"空仓 {c.get('empty_pct',0)*100:>6.1f}%")

    L.append("\n## 3. 三档择时对比\n")
    L.append("| 策略 | CAGR | MDD | Sharpe | 资金利用率 | 空仓比例 |")
    L.append("|---|---|---|---|---|---|")
    for nm, d in (("熊市买入（现策略）", bear), ("牛市买入", bull),
                  ("不择时（任何市况）", bt(P(mkt_state="any")))):
        c = d.get("capacity") or {}
        L.append(f"| {nm} | {c.get('cap_cagr',0)*100:+.2f}% | "
                 f"{c.get('cap_mdd',0)*100:+.2f}% | {c.get('cap_sharpe',0):.2f} | "
                 f"{c.get('avg_invested',0)*100:.1f}% | {c.get('empty_pct',0)*100:.1f}% |")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"\n报告已写：{OUT}")
    print(f"总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
