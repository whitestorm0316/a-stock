#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
36_strategy_final.py —— V3B 第十一节 + 第十二节 + 第十三节

第十一节：8 个用户指定策略 + 反向修正版，严格比较
第十二节：寻找「最简单有效」—— 3~5 个条件优先
第十三节：最终对比表 + 四段分期（2015~2020 / 2021~2022 / 2023~2024 / 2025~2026）
          + 样本外（IS=2015~2020，OOS=2021~2026）

两套口径同时给出：
  nav_*  : 日度再平衡理想化（与第 1~10 节可比）
  impl_* : 实际执行（T日信号 → T+1开盘买 → 持有 H 日 → T+1+H 开盘卖，含 0.3% 双边成本）

产出：
  output/v3b_strategy8.csv    8 个用户策略
  output/v3b_strategy_simple.csv  简单策略网格（1~6 条件）
  output/v3b_segment.csv      四段分期
  output/v3b_run_36.log
"""
import os
import time
import numpy as np
import pandas as pd
import v3b_lib as L

HZ = [5, 10, 20]
HOLD = 20
LOGF = open(os.path.join(L.OUT, "v3b_run_36.log"), "w", encoding="utf-8")
_t0 = time.time()


def log(m):
    line = f"[{time.time()-_t0:7.1f}s] {m}"
    print(line, flush=True)
    LOGF.write(line + "\n")
    LOGF.flush()


SEGS = [("2015-2020", 2015, 2020), ("2021-2022", 2021, 2022),
        ("2023-2024", 2023, 2024), ("2025-2026", 2025, 2026)]


def add_periods(rec, net, C, prefix="impl"):
    """按四段分期 + IS/OOS 计算净值序列的累计收益（用于可交易口径）"""
    yrs = pd.DatetimeIndex(C["ud"]).year.values
    for sn, y0, y1 in SEGS:
        s = (yrs >= y0) & (yrs <= y1)
        rec[f"{prefix}_seg_{sn}"] = float(np.prod(1.0 + net[s]) - 1.0) if s.sum() >= 20 else np.nan
    rec[f"{prefix}_is"] = float(np.prod(1.0 + net[yrs <= 2020]) - 1.0)
    rec[f"{prefix}_oos"] = float(np.prod(1.0 + net[yrs >= 2021]) - 1.0)
    return rec


def main():
    cols = ["ret20", "ret40", "ret60", "px_ma60_pct", "px_ma120_pct",
            "hist", "hist_z", "hist_upstreak", "dif", "dea",
            "rvol20", "vr20m", "rs_mkt20", "rs_sz20", "size_grp",
            "hv20", "close_price", "open_price", "can_buy_open"] + \
           [f"fwd{H}" for H in [1, 5, 10, 20, 40, 60]]
    df = L.load_clean(cols)
    C = L.ctx(df)
    log(f"clean {C['n']:,} 行 / {len(C['starts']):,} 只 / {C['n_days']:,} 交易日")

    oret, oret_sig = L.oret_from(df, C)
    mk_d = L.market_oret(oret, C)
    CHAIN = {H: L.chain_fwd(mk_d, C, H) for H in [1, 5, 10, 20, 40, 60]}
    FWD = {H: df[f"fwd{H}"].values.astype(np.float64) for H in [1, 5, 10, 20, 40, 60]}

    cl = df["close_price"].values.astype(np.float64)
    op = df["open_price"].values.astype(np.float64)
    ma5 = L.roll_mean(cl, C, 5)
    ma10 = L.roll_mean(cl, C, 10)
    can_buy = df["can_buy_open"].values.astype(bool)
    buy_open = np.where(can_buy, op, np.nan)
    sell_open = L.build_sell_open(op, df["can_sell_open"].values.astype(bool), C)

    # ---------- 市场环境（自建）
    nav = np.cumprod(1.0 + np.nan_to_num(mk_d))
    mma60 = pd.Series(nav).rolling(60).mean().values
    m_up = nav > mma60
    mhv20 = pd.Series(np.nan_to_num(mk_d)).rolling(20).std().values * np.sqrt(252)
    mhv_med = np.nanmedian(mhv20)
    D = C["day_idx"]

    # ---------- 分位
    b20 = L.decile(df["ret20"].values.astype(np.float64), C, 10)
    b40 = L.decile(df["ret40"].values.astype(np.float64), C, 10)
    b60 = L.decile(df["ret60"].values.astype(np.float64), C, 10)
    bm60 = L.decile(df["px_ma60_pct"].values.astype(np.float64), C, 10)
    brs = L.decile(df["rs_sz20"].values.astype(np.float64), C, 10)
    sg = df["size_grp"].values.astype(np.float64)

    deep = np.isfinite(b40) & np.isfinite(b60) & np.isfinite(bm60) & \
        ((b40 == 1) | (b60 == 1) | (bm60 == 1))
    d2d6 = np.isfinite(b20) & (b20 >= 2) & (b20 <= 6)
    small = np.isfinite(sg) & (sg <= 2)            # 最小 30%
    tiny = np.isfinite(sg) & (sg == 0)             # 最小 10%
    rs_weak = np.isfinite(brs) & (brs <= 3)
    rs_strong = np.isfinite(brs) & (brs >= 8)

    # ---------- 8 个用户指定策略
    A = cl > ma5
    B = np.isfinite(df["hist_upstreak"].values.astype(np.float64)) & \
        (df["hist_upstreak"].values.astype(np.float64) >= 3)
    Cc = np.isfinite(df["rvol20"].values.astype(np.float64)) & \
        (df["rvol20"].values.astype(np.float64) > 1.2)
    E = np.isfinite(df["hist"].values.astype(np.float64)) & \
        (df["hist"].values.astype(np.float64) > 0)
    G = cl > ma10

    STRATS8 = [
        ("1 纯价格超跌", deep),
        ("2 +MA5反转(Close>MA5)", deep & A),
        ("3 +MACD改善(hist>0)", deep & E),
        ("4 +放量反转(量比>1.2)", deep & Cc),
        ("5 +相对强势(rs_sz20强)", deep & rs_strong),
        ("6 +市场趋势(市场Close>MA60)", deep & m_up[D]),
        ("7 +MACD改善+放量", deep & E & Cc),
        ("8 +MACD改善+放量+市场趋势", deep & E & Cc & m_up[D]),
        # —— 反向修正版（依据第 3~8 节实测方向）
        ("9 [修正] 深度超跌+小市值30%", deep & small),
        ("10 [修正] 深度超跌+小市值10%", deep & tiny),
        ("11 [修正] 深度超跌+市场Close<MA60", deep & (~m_up[D])),
        ("12 [修正] 深度超跌+市场高波动", deep & (mhv20[D] > mhv_med)),
        ("13 [修正] 深度超跌+rs_sz20弱", deep & rs_weak),
        ("14 [修正] 深度超跌+小市值+熊市", deep & small & (~m_up[D])),
        ("15 [修正] 深度超跌+小市值+熊市+rs弱", deep & small & (~m_up[D]) & rs_weak),
    ]

    def impl(sig, tag):
        """实际执行：T日信号 → T+1开盘买 → 持有 HOLD 日 → T+1+HOLD 开盘卖，含成本"""
        r, ed, pos = L.simulate_hold(sig, buy_open, sell_open, C, HOLD)
        net, cnt = L.portfolio_nav(sig, D, C["n_days"], oret_sig,
                                   np.full(len(sig), HOLD, np.float64), C)
        st = L.ann_stats(net, C["n_days"])
        ts = L.trade_stats(r, cost=L.RT_COST)
        return st, ts, r, ed

    rows = []
    for nm, sig in STRATS8:
        sig = np.asarray(sig, bool)
        pst, rd, cnt = L.state_nav(sig, oret_sig, C)
        ist, ts, r, ed = impl(sig, nm)
        net, _ = L.portfolio_nav(sig, D, C["n_days"], oret_sig,
                                 np.full(len(sig), HOLD, np.float64), C)
        rec = dict(strategy=nm, n=int(sig.sum()),
                   nav_cagr=pst["cagr"], nav_mdd=pst["mdd"], nav_sharpe=pst["sharpe"],
                   nav_ann_arith=pst["ann_arith"], avg_holdings=pst["avg_holdings"],
                   impl_cagr=ist["cagr"], impl_mdd=ist["mdd"], impl_sharpe=ist["sharpe"],
                   impl_ann_arith=ist["ann_arith"],
                   n_trade=ts["n"] if ts else 0,
                   win=ts["win"] if ts else np.nan,
                   payoff=ts["payoff"] if ts else np.nan,
                   pf=ts["pf"] if ts else np.nan,
                   mean=ts["mean"] if ts else np.nan,
                   med=ts["median"] if ts else np.nan)
        for H in [1, 5, 10, 20, 40, 60]:
            res = L.exw_t(FWD[H][sig], CHAIN[H][D][sig], D[sig])
            if res:
                rec[f"exw{H}"] = res["exw"]; rec[f"t{H}"] = res["t"]
        add_periods(rec, net, C, "impl")
        rows.append(rec)
    sdf = pd.DataFrame(rows)
    sdf.to_csv(os.path.join(L.OUT, "v3b_strategy8.csv"), index=False)
    log(f"-> v3b_strategy8.csv ({len(sdf)} 行)")

    log("\n摘要 11 —— 8 个用户指定策略 + 反向修正版（impl = 实际执行口径，含成本）")
    log(f"    {'策略':<32}{'n':>9}{'CAGR':>8}{'MDD':>8}{'Sharpe':>8}"
        f"{'胜率':>7}{'盈亏比':>8}{'PF':>7}{'exw20':>9}{'t':>7}{'IS':>9}{'OOS':>9}")
    for r in sdf.itertuples():
        log(f"    {r.strategy:<32}{r.n:>9,}{r.impl_cagr*100:>7.1f}%{r.impl_mdd*100:>7.1f}%"
            f"{r.impl_sharpe:>8.2f}{r.win*100:>6.1f}%{r.payoff:>8.2f}{r.pf:>7.2f}"
            f"{r.exw20*100:>8.3f}%{r.t20:>7.2f}{r.impl_is*100:>8.1f}%{r.impl_oos*100:>8.1f}%")

    log("\n摘要 11b —— 四段分期（impl 实际执行口径，含成本）")
    log(f"    {'策略':<32}" + "".join(f"{s[0]:>12}" for s in SEGS))
    for r in sdf.to_dict("records"):
        log(f"    {r['strategy']:<32}" +
            "".join(f"{r['impl_seg_'+s[0]]*100:>11.1f}%" for s in SEGS))

    # ============================================== 第十二节 最简单有效
    log("\n第十二节：寻找「最简单有效」—— 条件数 1~6 网格")
    CONDS = [
        ("C1 深度超跌(ret40/60/距MA60 D1)", deep),
        ("C2 ret20 D1~D3", np.isfinite(b20) & (b20 <= 3)),
        ("C3 距MA60 D1", np.isfinite(bm60) & (bm60 == 1)),
        ("C4 小市值30%", small),
        ("C5 小市值10%", tiny),
        ("C6 市场Close<MA60", ~m_up[D]),
        ("C7 市场HV20>中位", mhv20[D] > mhv_med),
        ("C8 rs_sz20弱(D1~D3)", rs_weak),
        ("C9 距MA120 D1", np.isfinite(L.decile(df["px_ma120_pct"].values.astype(np.float64), C, 10)) &
         (L.decile(df["px_ma120_pct"].values.astype(np.float64), C, 10) == 1)),
        ("C10 20日波动 hv20 高", np.isfinite(df["hv20"].values.astype(np.float64)) &
         (df["hv20"].values.astype(np.float64) > np.nanmedian(df["hv20"].values.astype(np.float64)))),
    ]
    CM = {n: np.asarray(m, bool) for n, m in CONDS}
    GRID = [
        ("K1 距MA60 D1", ["C3 距MA60 D1"]),
        ("K2 距MA60 D1 + 小市值30%", ["C3 距MA60 D1", "C4 小市值30%"]),
        ("K3 距MA60 D1 + 小市值30% + 熊市", ["C3 距MA60 D1", "C4 小市值30%", "C6 市场Close<MA60"]),
        ("K4 深度超跌 + 小市值30%", ["C1 深度超跌(ret40/60/距MA60 D1)", "C4 小市值30%"]),
        ("K5 深度超跌 + 小市值30% + 熊市", ["C1 深度超跌(ret40/60/距MA60 D1)", "C4 小市值30%",
                                          "C6 市场Close<MA60"]),
        ("K6 深度超跌 + 小市值30% + 熊市 + rs弱",
         ["C1 深度超跌(ret40/60/距MA60 D1)", "C4 小市值30%", "C6 市场Close<MA60",
          "C8 rs_sz20弱(D1~D3)"]),
        ("K7 深度超跌 + 小市值10% + 熊市 + rs弱",
         ["C1 深度超跌(ret40/60/距MA60 D1)", "C5 小市值10%", "C6 市场Close<MA60",
          "C8 rs_sz20弱(D1~D3)"]),
        ("K8 深度超跌 + 小市值10% + 熊市 + rs弱 + 高波动",
         ["C1 深度超跌(ret40/60/距MA60 D1)", "C5 小市值10%", "C6 市场Close<MA60",
          "C8 rs_sz20弱(D1~D3)", "C7 市场HV20>中位"]),
        ("K9 距MA60 D1 + 熊市 + rs弱",
         ["C3 距MA60 D1", "C6 市场Close<MA60", "C8 rs_sz20弱(D1~D3)"]),
        ("K10 深度超跌 + 熊市", ["C1 深度超跌(ret40/60/距MA60 D1)", "C6 市场Close<MA60"]),
        ("K11 ret20 D1~D3 + 小市值30% + 熊市",
         ["C2 ret20 D1~D3", "C4 小市值30%", "C6 市场Close<MA60"]),
        ("K12 距MA120 D1 + 小市值30% + 熊市",
         ["C9 距MA120 D1", "C4 小市值30%", "C6 市场Close<MA60"]),
    ]
    krows = []
    for nm, cs in GRID:
        m = np.ones(C["n"], bool)
        for c in cs:
            m &= CM[c]
        pst, rd, cnt = L.state_nav(m, oret_sig, C)
        ist, ts, r, ed = impl(m, nm)
        net, _ = L.portfolio_nav(m, D, C["n_days"], oret_sig,
                                 np.full(len(m), HOLD, np.float64), C)
        rec = dict(strategy=nm, ncond=len(cs), n=int(m.sum()),
                   nav_cagr=pst["cagr"], nav_mdd=pst["mdd"], nav_sharpe=pst["sharpe"],
                   impl_cagr=ist["cagr"], impl_mdd=ist["mdd"], impl_sharpe=ist["sharpe"],
                   win=ts["win"] if ts else np.nan, payoff=ts["payoff"] if ts else np.nan,
                   pf=ts["pf"] if ts else np.nan, n_trade=ts["n"] if ts else 0,
                   avg_holdings=pst["avg_holdings"])
        for H in [1, 5, 10, 20, 40, 60]:
            res = L.exw_t(FWD[H][m], CHAIN[H][D][m], D[m])
            if res:
                rec[f"exw{H}"] = res["exw"]; rec[f"t{H}"] = res["t"]
        add_periods(rec, net, C, "impl")
        rec["active_day_pct"] = float((net != 0).mean())
        krows.append(rec)
    kdf = pd.DataFrame(krows)
    kdf.to_csv(os.path.join(L.OUT, "v3b_strategy_simple.csv"), index=False)
    log(f"-> v3b_strategy_simple.csv ({len(kdf)} 行)")

    log("\n摘要 12 —— 简单策略网格（按条件数排序）")
    log(f"    {'策略':<44}{'条件':>5}{'n':>11}{'CAGR':>8}{'MDD':>8}{'Sharpe':>8}"
        f"{'胜率':>7}{'PF':>7}{'exw20':>9}{'t':>7}{'IS':>9}{'OOS':>9}")
    for r in kdf.sort_values(["ncond", "impl_sharpe"], ascending=[True, False]).itertuples():
        log(f"    {r.strategy:<44}{r.ncond:>5}{r.n:>11,}{r.impl_cagr*100:>7.1f}%"
            f"{r.impl_mdd*100:>7.1f}%{r.impl_sharpe:>8.2f}{r.win*100:>6.1f}%{r.pf:>7.2f}"
            f"{r.exw20*100:>8.3f}%{r.t20:>7.2f}{r.impl_is*100:>8.1f}%{r.impl_oos*100:>8.1f}%")

    log("\n摘要 12b —— 简单策略 四段分期（impl 实际执行口径，含成本）")
    log(f"    {'策略':<44}" + "".join(f"{s[0]:>12}" for s in SEGS))
    for r in kdf.sort_values(["ncond", "impl_sharpe"], ascending=[True, False]).to_dict("records"):
        log(f"    {r['strategy']:<44}" +
            "".join(f"{r['impl_seg_'+s[0]]*100:>11.1f}%" for s in SEGS))

    # 合并输出分期表
    kdf["kind"] = "simple"
    sdf["kind"] = "user8"
    both = pd.concat([sdf, kdf], ignore_index=True)
    both.to_csv(os.path.join(L.OUT, "v3b_segment.csv"), index=False)
    log(f"-> v3b_segment.csv ({len(both)} 行)")

    # 可交易性检查
    log("\n可交易性检查（信号当日 can_buy_open 为真的比例）")
    for nm, sig in [("深度超跌", deep), ("深度超跌+小市值30%", deep & small),
                    ("K3 距MA60D1+小市值30%+熊市",
                     CM["C3 距MA60 D1"] & CM["C4 小市值30%"] & CM["C6 市场Close<MA60"])]:
        s = np.asarray(sig, bool)
        log(f"    {nm:<34} 信号数={s.sum():>9,}  可买比例={can_buy[s].mean()*100:.2f}%")

    log("完成")


if __name__ == "__main__":
    main()
