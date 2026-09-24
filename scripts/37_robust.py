#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
37_robust.py —— V3B 第十二节（续）：简单策略的稳健性检验

对最终候选策略做四类检验：
  1. 参数邻域：距MA60 的分位阈值在 D1~D3 上扫；小市值 30%/20%/10%；市场阈值 ±5%
  2. 逐年 + 四段分期 + 样本外（IS 2015-2020 / OOS 2021-2026）
  3. 交易成本敏感性：0.0% / 0.15% / 0.3% / 0.5% / 1.0% 双边
  4. 持有期敏感性：5 / 10 / 20 / 40 / 60 日
  5. 可交易性：剔除 can_buy_open=False、跌停顺延
  6. 与「剔除最小10%」的对照（回答用户第 8 节的质疑）

产出：
  output/v3b_robust.csv, v3b_robust_year.csv
  output/v3b_run_37.log
"""
import os
import time
import numpy as np
import pandas as pd
import v3b_lib as L

LOGF = open(os.path.join(L.OUT, "v3b_run_37.log"), "w", encoding="utf-8")
_t0 = time.time()
SEGS = [("2015-2020", 2015, 2020), ("2021-2022", 2021, 2022),
        ("2023-2024", 2023, 2024), ("2025-2026", 2025, 2026)]


def log(m):
    line = f"[{time.time()-_t0:7.1f}s] {m}"
    print(line, flush=True)
    LOGF.write(line + "\n")
    LOGF.flush()


def main():
    cols = ["ret20", "ret40", "ret60", "px_ma60_pct", "px_ma120_pct",
            "rs_sz20", "size_grp", "hv20", "open_price", "close_price",
            "can_buy_open", "can_sell_open"] + [f"fwd{H}" for H in [1, 5, 10, 20, 40, 60]]
    df = L.load_clean(cols)
    C = L.ctx(df)
    log(f"clean {C['n']:,} 行 / {len(C['starts']):,} 只 / {C['n_days']:,} 交易日")

    oret, oret_sig = L.oret_from(df, C)
    mk_d = L.market_oret(oret, C)
    FWD = {H: df[f"fwd{H}"].values.astype(np.float64) for H in [1, 5, 10, 20, 40, 60]}
    CHAIN = {H: L.chain_fwd(mk_d, C, H) for H in [1, 5, 10, 20, 40, 60]}

    op = df["open_price"].values.astype(np.float64)
    can_buy = df["can_buy_open"].values.astype(bool)
    buy_open = np.where(can_buy, op, np.nan)
    sell_open = L.build_sell_open(op, df["can_sell_open"].values.astype(bool), C)
    D = C["day_idx"]

    nav = np.cumprod(1.0 + np.nan_to_num(mk_d))
    mma60 = pd.Series(nav).rolling(60).mean().values
    m_up = nav > mma60
    mhv20 = pd.Series(np.nan_to_num(mk_d)).rolling(20).std().values * np.sqrt(252)

    bm60 = L.decile(df["px_ma60_pct"].values.astype(np.float64), C, 10)
    bm120 = L.decile(df["px_ma120_pct"].values.astype(np.float64), C, 10)
    b40 = L.decile(df["ret40"].values.astype(np.float64), C, 10)
    b60 = L.decile(df["ret60"].values.astype(np.float64), C, 10)
    b20 = L.decile(df["ret20"].values.astype(np.float64), C, 10)
    brs = L.decile(df["rs_sz20"].values.astype(np.float64), C, 10)
    sg = df["size_grp"].values.astype(np.float64)

    def run(sig, hold=20, cost=L.RT_COST):
        sig = np.asarray(sig, bool)
        r, ed, pos = L.simulate_hold(sig, buy_open, sell_open, C, hold)
        net, cnt = L.portfolio_nav(sig, D, C["n_days"], oret_sig,
                                   np.full(len(sig), float(hold), np.float64), C,
                                   rt_cost=cost)
        st = L.ann_stats(net, C["n_days"])
        ts = L.trade_stats(r, cost=cost)
        return st, ts, net

    # ============================================ 1. 参数邻域
    log("\n【1】参数邻域扫描")
    rows = []
    for th in [1, 2, 3]:
        for sz, szn in [(0.10, "10%"), (0.20, "20%"), (0.30, "30%")]:
            for mkt, mk_n in [(True, "熊市"), (False, "不限")]:
                m = np.isfinite(bm60) & (bm60 <= th) & (sg <= int(round(sz * 10)))
                if mkt:
                    m = m & (~m_up[D])
                st, ts, net = run(m)
                yrs = pd.DatetimeIndex(C["ud"]).year.values
                rec = dict(MA60th=th, size=szn, market=mk_n, n=int(m.sum()),
                           cagr=st["cagr"], mdd=st["mdd"], sharpe=st["sharpe"],
                           win=ts["win"] if ts else np.nan, pf=ts["pf"] if ts else np.nan,
                           n_trade=ts["n"] if ts else 0)
                for sn, y0, y1 in SEGS:
                    s = (yrs >= y0) & (yrs <= y1)
                    rec[f"seg_{sn}"] = float(np.prod(1.0 + net[s]) - 1.0) if s.sum() >= 20 else np.nan
                rec["is_ret"] = float(np.prod(1.0 + net[yrs <= 2020]) - 1.0)
                rec["oos_ret"] = float(np.prod(1.0 + net[yrs >= 2021]) - 1.0)
                rec["neg_seg"] = int(sum(rec[f"seg_{sn}"] < 0 for sn, _, _ in SEGS))
                rows.append(rec)
    rdf = pd.DataFrame(rows)
    rdf.to_csv(os.path.join(L.OUT, "v3b_robust.csv"), index=False)
    log(f"-> v3b_robust.csv ({len(rdf)} 行)")
    log(f"    {'MA60阈':>7}{'市值':>6}{'市场':>6}{'n':>10}{'CAGR':>8}{'MDD':>8}{'Sharpe':>8}"
        f"{'胜率':>7}{'PF':>6}{'IS':>9}{'OOS':>9}{'负段数':>7}")
    for r in rdf.sort_values(["MA60th", "size", "market"]).to_dict("records"):
        log(f"    {'D1~D%d' % r['MA60th']:>7}{r['size']:>6}{r['market']:>6}{r['n']:>10,}"
            f"{r['cagr']*100:>7.1f}%{r['mdd']*100:>7.1f}%{r['sharpe']:>8.2f}"
            f"{r['win']*100:>6.1f}%{r['pf']:>6.2f}"
            f"{r['is_ret']*100:>8.1f}%{r['oos_ret']*100:>8.1f}%{r['neg_seg']:>7}")

    # ============================================ 2. 市场阈值敏感性
    log("\n【2】市场环境阈值敏感性（距MA60 D1~D2 + 小市值30%）")
    base = np.isfinite(bm60) & (bm60 <= 2) & (sg <= 2)
    m_dist = np.where(np.isfinite(mma60) & (mma60 > 0), nav / mma60 - 1.0, np.nan)[D]
    for d in [-0.05, -0.02, 0.0, 0.02, 0.05]:
        m = base & np.isfinite(m_dist) & (m_dist < d)
        st, ts, net = run(m)
        log(f"    市场距MA60 < {d*100:+.0f}%   n={int(m.sum()):>9,}  CAGR={st['cagr']*100:>6.1f}%"
            f"  MDD={st['mdd']*100:>6.1f}%  Sharpe={st['sharpe']:>5.2f}  交易={ts['n'] if ts else 0:,}")

    # ============================================ 3. 成本敏感性
    log("\n【3】交易成本敏感性（距MA60 D1~D2 + 小市值30% + 熊市）")
    m = np.isfinite(bm60) & (bm60 <= 2) & (sg <= 2) & (~m_up[D])
    for c in [0.0, 0.0015, 0.003, 0.005, 0.010]:
        st, ts, net = run(m, cost=c)
        log(f"    双边成本 {c*100:>4.2f}%  CAGR={st['cagr']*100:>6.1f}%  MDD={st['mdd']*100:>6.1f}%"
            f"  Sharpe={st['sharpe']:>5.2f}  单笔均值={ts['mean']*100:>+6.2f}%  PF={ts['pf']:>5.2f}")

    # ============================================ 4. 持有期敏感性
    log("\n【4】持有期敏感性（距MA60 D1~D2 + 小市值30% + 熊市）")
    for h in [5, 10, 20, 40, 60]:
        r, ed, pos = L.simulate_hold(m, buy_open, sell_open, C, h)
        net, cnt = L.portfolio_nav(m, D, C["n_days"], oret_sig,
                                   np.full(len(m), float(h), np.float64), C)
        st = L.ann_stats(net, C["n_days"])
        ts = L.trade_stats(r, cost=L.RT_COST)
        log(f"    持有 {h:>3} 日  CAGR={st['cagr']*100:>6.1f}%  MDD={st['mdd']*100:>6.1f}%"
            f"  Sharpe={st['sharpe']:>5.2f}  单笔均值={ts['mean']*100:>+6.2f}%  PF={ts['pf']:>5.2f}")

    # ============================================ 5. 逐年
    log("\n【5】候选策略逐年（impl 实际执行，含 0.3% 成本）")
    CANDS = [
        ("距MA60 D1 + 小市值30%", np.isfinite(bm60) & (bm60 == 1) & (sg <= 2)),
        ("距MA60 D1~D2 + 小市值30% + 熊市", m),
        ("距MA60 D1~D3 + 小市值30% + 熊市",
         np.isfinite(bm60) & (bm60 <= 3) & (sg <= 2) & (~m_up[D])),
        ("距MA120 D1 + 小市值30% + 熊市",
         np.isfinite(bm120) & (bm120 == 1) & (sg <= 2) & (~m_up[D])),
        ("[对照] 距MA60 D1 剔除最小10%",
         np.isfinite(bm60) & (bm60 == 1) & (sg >= 1)),
        ("[对照] 距MA60 D1 全市值", np.isfinite(bm60) & (bm60 == 1)),
        ("[对照] 全A等权(市场)", np.ones(C["n"], bool)),
    ]
    yrows = []
    for nm, sig in CANDS:
        st, ts, net = run(sig)
        yrs = pd.DatetimeIndex(C["ud"]).year.values
        rec = dict(strategy=nm, n=int(np.sum(sig)), cagr=st["cagr"], mdd=st["mdd"],
                   sharpe=st["sharpe"], win=ts["win"] if ts else np.nan,
                   pf=ts["pf"] if ts else np.nan)
        line = []
        for y in range(2015, 2027):
            s = yrs == y
            v = float(np.prod(1.0 + net[s]) - 1.0) if s.sum() >= 20 else np.nan
            rec[f"y{y}"] = v
            line.append(f"{y}:{v*100:+.0f}%" if np.isfinite(v) else f"{y}:  -")
        log(f"    {nm:<32} " + " ".join(line))
        for sn, y0, y1 in SEGS:
            s = (yrs >= y0) & (yrs <= y1)
            rec[f"seg_{sn}"] = float(np.prod(1.0 + net[s]) - 1.0) if s.sum() >= 20 else np.nan
        rec["is_ret"] = float(np.prod(1.0 + net[yrs <= 2020]) - 1.0)
        rec["oos_ret"] = float(np.prod(1.0 + net[yrs >= 2021]) - 1.0)
        npos = int(sum(rec[f"y{y}"] > 0 for y in range(2015, 2027) if np.isfinite(rec[f"y{y}"])))
        nval = int(sum(np.isfinite(rec[f"y{y}"]) for y in range(2015, 2027)))
        rec["pos_year"] = f"{npos}/{nval}"
        yrows.append(rec)
    ydf = pd.DataFrame(yrows)
    ydf.to_csv(os.path.join(L.OUT, "v3b_robust_year.csv"), index=False)
    log(f"-> v3b_robust_year.csv ({len(ydf)} 行)")

    log("\n【5b】候选策略汇总")
    log(f"    {'策略':<32}{'n':>10}{'CAGR':>8}{'MDD':>8}{'Sharpe':>8}{'胜率':>7}{'PF':>6}"
        f"{'正年数':>8}{'IS':>9}{'OOS':>9}")
    for r in ydf.to_dict("records"):
        log(f"    {r['strategy']:<32}{r['n']:>10,}{r['cagr']*100:>7.1f}%{r['mdd']*100:>7.1f}%"
            f"{r['sharpe']:>8.2f}{r['win']*100:>6.1f}%{r['pf']:>6.2f}{r['pos_year']:>8}"
            f"{r['is_ret']*100:>8.1f}%{r['oos_ret']*100:>8.1f}%")

    # ============================================ 6. 回撤与空仓
    log("\n【6】空仓天数占比与容量检查")
    for nm, sig in CANDS[:5]:
        s = np.asarray(sig, bool)
        net, cnt = L.portfolio_nav(s, D, C["n_days"], oret_sig,
                                   np.full(len(s), 20.0, np.float64), C)
        log(f"    {nm:<32} 信号数={s.sum():>10,}  持仓日占比={(cnt>0).mean()*100:>5.1f}%"
            f"  日均持仓数={cnt[cnt>0].mean():>7.0f}  可买比例={can_buy[s].mean()*100:.2f}%")

    log("完成")


if __name__ == "__main__":
    main()
