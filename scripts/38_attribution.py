#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
38_attribution.py —— V3B 最终归因：小市值是「反转 alpha」还是「规模 beta」？

这是决定最终结论的关键对照。若「距MA60 D1 + 小市值」的收益主要来自小市值本身
（即「小市值 单独」也有相近收益），则反转 alpha 是伪装的规模 beta。

产出：output/v3b_attribution.csv, v3b_run_38.log
"""
import os
import time
import numpy as np
import pandas as pd
import v3b_lib as L

LOGF = open(os.path.join(L.OUT, "v3b_run_38.log"), "w", encoding="utf-8")
_t0 = time.time()


def log(m):
    line = f"[{time.time()-_t0:7.1f}s] {m}"
    print(line, flush=True)
    LOGF.write(line + "\n")
    LOGF.flush()


def main():
    df = L.load_clean(["px_ma60_pct", "px_ma120_pct", "ret20", "ret40", "ret60",
                       "size_grp", "open_price", "can_buy_open", "can_sell_open",
                       "close_price"])
    C = L.ctx(df)
    D = C["day_idx"]
    log(f"clean {C['n']:,} 行 / {len(C['starts']):,} 只 / {C['n_days']:,} 交易日")

    oret, oret_sig = L.oret_from(df, C)
    op = df["open_price"].values.astype(np.float64)
    buy = np.where(df["can_buy_open"].values.astype(bool), op, np.nan)
    sell = L.build_sell_open(op, df["can_sell_open"].values.astype(bool), C)
    sg = df["size_grp"].values.astype(np.float64)
    bm60 = L.decile(df["px_ma60_pct"].values.astype(np.float64), C, 10)
    bm120 = L.decile(df["px_ma120_pct"].values.astype(np.float64), C, 10)

    nav = np.cumprod(1.0 + np.nan_to_num(L.market_oret(oret, C)))
    mma60 = pd.Series(nav).rolling(60).mean().values
    m_up = nav > mma60
    yrs = pd.DatetimeIndex(C["ud"]).year.values

    def run(m, h=20):
        m = np.asarray(m, bool)
        r, ed, p = L.simulate_hold(m, buy, sell, C, h)
        net, cnt = L.portfolio_nav(m, D, C["n_days"], oret_sig,
                                   np.full(len(m), float(h)), C)
        st = L.ann_stats(net, C["n_days"])
        ts = L.trade_stats(r, cost=L.RT_COST)
        yr = {}
        for y in range(2015, 2027):
            k = yrs == y
            yr[y] = float(np.prod(1.0 + net[k]) - 1.0) if k.sum() >= 20 else np.nan
        return st, ts, yr, net

    CASES = [
        ("A 全市场(等权)", np.ones(C["n"], bool)),
        ("B 小市值30% 单独(无超跌)", sg <= 2),
        ("C 小市值10% 单独(无超跌)", sg == 0),
        ("D 距MA60 D1 单独(全市值)", np.isfinite(bm60) & (bm60 == 1)),
        ("E 距MA60 D1 + 小市值30%", np.isfinite(bm60) & (bm60 == 1) & (sg <= 2)),
        ("F E + 熊市", np.isfinite(bm60) & (bm60 == 1) & (sg <= 2) & (~m_up[D])),
        ("G 小市值30% + 熊市(无超跌)", (sg <= 2) & (~m_up[D])),
        ("H 距MA60 D1 + 中市值(30-70%)",
         np.isfinite(bm60) & (bm60 == 1) & (sg >= 3) & (sg <= 6)),
        ("I 距MA60 D1 + 大市值(70-90%)",
         np.isfinite(bm60) & (bm60 == 1) & (sg >= 7) & (sg <= 8)),
        ("J 距MA120 D1 + 小市值30% + 熊市",
         np.isfinite(bm120) & (bm120 == 1) & (sg <= 2) & (~m_up[D])),
    ]
    rows = []
    log(f"\n{'变体':<40}{'CAGR':>8}{'MDD':>8}{'Sharpe':>8}{'胜率':>7}{'PF':>6}"
        f"{'单笔均值':>9}{'交易数':>9}")
    for nm, m in CASES:
        st, ts, yr, net = run(m)
        log(f"{nm:<40}{st['cagr']*100:>7.1f}%{st['mdd']*100:>7.1f}%{st['sharpe']:>8.2f}"
            f"{ts['win']*100:>6.1f}%{ts['pf']:>6.2f}{ts['mean']*100:>8.2f}%{ts['n']:>9,}")
        rec = dict(variant=nm, n=int(np.sum(m)), cagr=st["cagr"], mdd=st["mdd"],
                   sharpe=st["sharpe"], ann_arith=st["ann_arith"],
                   win=ts["win"], pf=ts["pf"], payoff=ts["payoff"],
                   mean=ts["mean"], n_trade=ts["n"])
        for y in range(2015, 2027):
            rec[f"y{y}"] = yr[y]
        rows.append(rec)
    adf = pd.DataFrame(rows)
    adf.to_csv(os.path.join(L.OUT, "v3b_attribution.csv"), index=False)
    log(f"\n-> v3b_attribution.csv ({len(adf)} 行)")

    log("\n=== 逐年（impl 实际执行，含 0.3% 成本，%）===")
    log(f"{'变体':<40}" + "".join(f"{y:>7}" for y in range(2015, 2027)))
    for r in adf.to_dict("records"):
        log(f"{r['variant']:<40}" +
            "".join(f"{r[f'y{y}']*100:>6.0f}% " if np.isfinite(r[f"y{y}"]) else "     - "
                    for y in range(2015, 2027)))

    # 关键判断：小市值 beta 贡献 vs 超跌增量
    log("\n=== 归因判断 ===")
    d = {r["variant"]: r for r in adf.to_dict("records")}
    b = d["B 小市值30% 单独(无超跌)"]["cagr"]
    e = d["E 距MA60 D1 + 小市值30%"]["cagr"]
    dd = d["D 距MA60 D1 单独(全市值)"]["cagr"]
    log(f"  小市值30% 单独                CAGR = {b*100:+.1f}%")
    log(f"  距MA60 D1 单独(全市值)        CAGR = {dd*100:+.1f}%")
    log(f"  两者叠加                      CAGR = {e*100:+.1f}%")
    log(f"  叠加 − 小市值单独             = {(e-b)*100:+.1f} pp  ← 超跌的增量")
    log(f"  叠加 − 距MA60单独             = {(e-dd)*100:+.1f} pp  ← 小市值的增量")
    log(f"  全A等权基准                   CAGR = {d['A 全市场(等权)']['cagr']*100:+.1f}%")

    log("\n完成")


if __name__ == "__main__":
    main()
