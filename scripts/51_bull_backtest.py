#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
51_bull_backtest.py —— 牛市策略候选的**组合层回测对比**

为什么要单独跑组合层（而不是只看单因子 IC）
--------------------------------------------
单因子 IC 是「逐日横截面」的统计量，说明「同一天里，A 比 B 好」。
但**能不能赚钱**是另一回事：还要看
  · 择时（牛市里也不是天天涨）
  · 持仓集中度（10 只 vs 全部）
  · 换手与容量（超跌股常停牌/一字板，买不进）
所以本脚本直接用引擎跑真实组合回测。

对比组（全部限定「只在牛市建仓」，即 mkt_state=bull）
------------------------------------------------------
  基准：熊市策略（mkt_state=bear, 距MA60 D1 + 小市值30%）—— 对照组
  牛市候选 1：反转（距MA60 D1，即原策略信号，只改择时为 bull）
  牛市候选 2：动量（距MA60 最强那一档）
  牛市候选 3：大市值优先（pick=big）
  牛市候选 4：成交额优先（pick=amount，流动性最好的）
  牛市候选 5：行业动量（所属行业20日涨幅靠前）

⚠️ 全部用**同一套容量口径**（10 只 / 日 3 只）对比，否则又是不可比的数字。
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

# 反代必须绕过（本机有 HTTP_PROXY）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path, body=None):
    url = BASE + path
    if body is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
    with _opener.open(req, timeout=600) as r:
        return json.loads(r.read().decode())


POOL_ALL = {"mode": "all", "boards": ["MAIN", "CHINEXT", "STAR"],
            "exchanges": ["SH", "SZ"]}

# 容量口径统一：10 只 / 日 3 只（与 U3 方案一致，实盘可执行）
CAP = dict(max_pos=10, max_new=3, pick="deep")

BASE_P = dict(
    px_ma60_min=1, px_ma60_max=1,
    size_min=0, size_max=2,
    mkt_state="bear", hold=20, mkt_index="all", mkt_ma=60,
    delist={"financial": True, "loss2y": False, "penny": True},
    delist_rev_floor=100000000, delist_main_2024=True, delist_penny_days=10,
)


def P(**kw):
    p = dict(BASE_P)
    p.update(CAP)
    p.update(kw)
    return p


CASES = [
    ("① 熊市·超跌反转（现策略，对照）", P(mkt_state="bear")),
    ("② 牛市·超跌反转（仅改择时）", P(mkt_state="bull")),
    ("③ 牛市·距MA60 最强档", P(mkt_state="bull", px_ma60_min=-1, px_ma60_max=-1)),
    ("④ 牛市·大市值优先", P(mkt_state="bull", pick="big")),
    ("⑤ 牛市·成交额优先", P(mkt_state="bull", pick="amount")),
    ("⑥ 牛市·小市值优先", P(mkt_state="bull", pick="small")),
    ("⑦ 牛市·不限市值(全市场)", P(mkt_state="bull", size_min=None, size_max=None)),
    ("⑧ 牛市·不限市值+成交额优先", P(mkt_state="bull", size_min=None, size_max=None,
                                  pick="amount")),
    ("⑨ 任何市况·超跌反转（不择时）", P(mkt_state="any")),
]


def fmt(v, d=2):
    """百分比格式化；None → 破折号。"""
    return "—" if v is None else f"{v*100:.{d}f}%"


def fnum(v, d=2):
    return "—" if v is None else f"{v:.{d}f}"


def main():
    print("=" * 100)
    print("  牛市策略候选 · 组合层回测（容量口径统一：10 只 / 日 3 只）")
    print("=" * 100)
    rows = []
    for name, p in CASES:
        t0 = time.time()
        try:
            d = api("/api/backtest", {"params": p, "pool": POOL_ALL})
        except Exception as e:  # noqa: BLE001
            print(f"{name:<34} 失败：{e}")
            continue
        c = d.get("capacity") or {}
        row = dict(
            name=name,
            n_sig=d.get("n_signal"),
            n_trade=d.get("n_trade"),
            cagr=c.get("cap_cagr"),
            mdd=c.get("cap_mdd"),
            sharpe=c.get("cap_sharpe"),
            util=c.get("avg_invested"),
            avg_pos=c.get("avg_pos"),
            empty=c.get("empty_pct"),
            win=d.get("win"),
            pf=d.get("pf"),
            hold=c.get("n_hold"),
        )
        rows.append(row)
        print(f"  {name:<32} CAGR {fmt(row['cagr']):>8}  MDD {fmt(row['mdd']):>8}  "
              f"Sharpe {fnum(row['sharpe']):>5}  利用率 {fmt(row['util']):>7}  "
              f"空仓 {fmt(row['empty']):>7}  ({time.time()-t0:.0f}s)")

    print()
    print("=" * 100)
    print("  明细表")
    print("=" * 100)
    hdr = (f"{'方案':<32}{'信号':>10}{'建仓':>6}{'CAGR':>9}{'MDD':>9}"
           f"{'Sharpe':>8}{'利用率':>9}{'空仓':>8}{'胜率':>8}{'PF':>7}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['name']:<32}{r['n_sig'] or 0:>10,}{r['hold'] or 0:>6}"
              f"{fmt(r['cagr']):>9}{fmt(r['mdd']):>9}{fnum(r['sharpe']):>8}"
              f"{fmt(r['util']):>9}{fmt(r['empty']):>8}{fmt(r['win']):>8}"
              f"{fnum(r['pf']):>7}")

    out = os.path.join(ROOT, "reports", "51_bull_backtest.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"\n明细已写：{out}")


if __name__ == "__main__":
    main()
