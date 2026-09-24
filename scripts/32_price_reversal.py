#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
32_price_reversal.py —— V3B 第一节 + 第二节：纯价格反转十分位研究

用户第一节：ret5/10/20/40/60 + Close/MA20-1 + Close/MA60-1 + Close/MA120-1
  逐日截面分十分位（D1 = 最弱 10%），统计未来 1/5/10/20 日收益的
  均值 / 中位数 / t 值 / 胜率 / 盈亏比 / Profit Factor / 最大回撤。
  回答：哪个超跌分位的未来收益最高？

用户第二节：重点研究 D2~D6（中等程度超跌），测试 ret20 / ret60 / px_ma60 的组合
  回答：中等程度超跌是否比极端超跌更容易发生反转？

产出：
  output/v3b_price_decile.csv    8 个维度 × 10 分位 × 4 持有期 的完整统计
  output/v3b_d2d6_combo.csv      D2~D6 组合测试
  output/v3b_run_32.log
"""
import os
import time
import numpy as np
import pandas as pd
import v3b_lib as L

HZ = [1, 5, 10, 20]
LOGF = open(os.path.join(L.OUT, "v3b_run_32.log"), "w", encoding="utf-8")
_t0 = time.time()


def log(m):
    line = f"[{time.time()-_t0:7.1f}s] {m}"
    print(line, flush=True)
    LOGF.write(line + "\n")
    LOGF.flush()


DIMS = [
    ("ret5", "过去5日涨幅"),
    ("ret10", "过去10日涨幅"),
    ("ret20", "过去20日涨幅"),
    ("ret40", "过去40日涨幅"),
    ("ret60", "过去60日涨幅"),
    ("px_ma20_pct", "距MA20"),
    ("px_ma60_pct", "距MA60"),
    ("px_ma120_pct", "距MA120"),
]


def main():
    cols = [d[0] for d in DIMS] + [f"fwd{H}" for H in [1, 5, 10, 20, 40, 60]] + \
           ["hist_z", "volz20", "hv20", "amt20_lag"]
    df = L.load_clean(cols)
    C = L.ctx(df)
    log(f"clean {C['n']:,} 行 / {len(C['starts']):,} 只 / {C['n_days']:,} 交易日")

    oret, oret_sig = L.oret_from(df, C)      # oret_sig = 可交易口径（T+1开盘→T+2开盘）
    mk_d = L.market_oret(oret, C)
    CHAIN = {H: L.chain_fwd(mk_d, C, H) for H in [1, 5, 10, 20, 40, 60]}
    log("日度链式市场基准 完成")

    FWD = {H: df[f"fwd{H}"].values.astype(np.float64) for H in [1, 5, 10, 20, 40, 60]}

    # 口径自检：可交易口径 vs 信号前一日
    f1 = FWD[1]
    log(f"[口径自检] 全样本  fwd1均值={np.nanmean(f1)*100:+.4f}%  "
        f"oret均值={np.nanmean(oret)*100:+.4f}%  oret_sig均值={np.nanmean(oret_sig)*100:+.4f}%")
    log(f"[口径自检] 全市场日收益 mean={np.nanmean(mk_d)*100:+.4f}%  "
        f"年化几何={((1+np.nan_to_num(mk_d)).prod()**(252/C['n_days'])-1)*100:+.2f}%")

    # ============================================================ 第一节
    log("第一节：8 个价格维度 × 十分位")
    rows, navrows = [], []
    for col, cn in DIMS:
        v = df[col].values.astype(np.float64)
        b = L.decile(v, C, 10)
        ok_b = np.isfinite(b)
        log(f"  {cn} ({col})  有效 {ok_b.sum():,}")
        for k in range(1, 11):
            mask = ok_b & (b == k)
            # 状态组合净值（日度再平衡，可交易口径 oret_sig）→ MDD / Sharpe
            st, r_daily, cnt = L.state_nav(mask, oret_sig, C)
            for H in HZ:
                res = L.exw_t(FWD[H][mask], CHAIN[H][C["day_idx"]][mask], C["day_idx"][mask])
                ts = L.trade_stats(FWD[H][mask])
                if res is None or ts is None:
                    continue
                rows.append(dict(dim=col, dim_cn=cn, decile=k, H=H,
                                 n=res["n"], n_day=res["n_day"],
                                 mean=ts["mean"], median=ts["median"],
                                 exw=res["exw"], t=res["t"],
                                 win=ts["win"], payoff=ts["payoff"], pf=ts["pf"],
                                 p10=ts["p10"], p90=ts["p90"],
                                 nav_cagr=st["cagr"], nav_mdd=st["mdd"],
                                 nav_sharpe=st["sharpe"],
                                 nav_ann_arith=st["ann_arith"],
                                 avg_holdings=st["avg_holdings"]))
    ddf = pd.DataFrame(rows)
    ddf.to_csv(os.path.join(L.OUT, "v3b_price_decile.csv"), index=False)
    log(f"-> v3b_price_decile.csv ({len(ddf)} 行)")

    # 摘要：各维度哪一分位超额最高
    log("\n摘要 1 —— 各维度 H=20 超额最高的分位（检验「哪个超跌分位最好」）")
    for col, cn in DIMS:
        s = ddf[(ddf.dim == col) & (ddf.H == 20)].sort_values("decile")
        if not len(s):
            continue
        best = s.loc[s.exw.idxmax()]
        log(f"  {cn:<14} 最优 D{int(best.decile)}={best.exw*100:+.3f}%(t{best.t:+.1f})   "
            f"D1={s[s.decile==1].exw.iloc[0]*100:+.3f}%(t{s[s.decile==1].t.iloc[0]:+.1f})   "
            f"D10={s[s.decile==10].exw.iloc[0]*100:+.3f}%(t{s[s.decile==10].t.iloc[0]:+.1f})")

    # ============================================================ 第二节
    log("\n第二节：D2~D6 组合测试")
    r20 = df["ret20"].values.astype(np.float64)
    r60 = df["ret60"].values.astype(np.float64)
    pm60 = df["px_ma60_pct"].values.astype(np.float64)

    b20 = L.decile(r20, C, 10)
    b60 = L.decile(r60, C, 10)
    bm60 = L.decile(pm60, C, 10)

    def band(b, lo, hi):
        return np.isfinite(b) & (b >= lo) & (b <= hi)

    COMBOS = [
        # —— 短周期（动量型）：预期 D1 无效
        ("A1 ret5 D1 (最弱)", band(L.decile(df["ret5"].values, C, 10), 1, 1)),
        # —— ret20（过渡区）：D2~D6 vs D1
        ("A2 ret20 D1 (最极端)", band(b20, 1, 1)),
        ("A3 ret20 D2~D6", band(b20, 2, 6)),
        ("A4 ret20 D3~D5", band(b20, 3, 5)),
        ("A5 ret20 D2~D3", band(b20, 2, 3)),
        # —— 中长周期（反转型）：D1 应最优
        ("B1 ret40 D1 (最极端)", band(L.decile(df["ret40"].values, C, 10), 1, 1)),
        ("B2 ret40 D2~D6", band(L.decile(df["ret40"].values, C, 10), 2, 6)),
        ("C1 ret60 D1 (最极端)", band(b60, 1, 1)),
        ("C2 ret60 D2~D6", band(b60, 2, 6)),
        ("C3 ret60 D2~D3", band(b60, 2, 3)),
        # —— 价格位置：D1 应最优
        ("D1 距MA60 D1 (最极端)", band(bm60, 1, 1)),
        ("D2 距MA60 D2~D6", band(bm60, 2, 6)),
        ("D3 距MA120 D1", band(L.decile(df["px_ma120_pct"].values, C, 10), 1, 1)),
        ("D4 距MA120 D2~D6", band(L.decile(df["px_ma120_pct"].values, C, 10), 2, 6)),
        # —— 用户规格组合
        ("E1 ret20 D2~D6 + Close<MA60", band(b20, 2, 6) & (pm60 < 0)),
        ("E2 ret20 D2~D6 + 距MA60<-10%", band(b20, 2, 6) & (pm60 < -0.10)),
        ("E3 ret20 D2~D6 + 距MA60<-20%", band(b20, 2, 6) & (pm60 < -0.20)),
        ("E4 ret40 D1 + 距MA60 D1", band(L.decile(df["ret40"].values, C, 10), 1, 1) & band(bm60, 1, 1)),
        ("E5 ret60 D1 + 距MA60 D1", band(b60, 1, 1) & band(bm60, 1, 1)),
        ("E6 ret40 D1 + ret60 D1 + 距MA60 D1",
         band(L.decile(df["ret40"].values, C, 10), 1, 1) & band(b60, 1, 1) & band(bm60, 1, 1)),
        # —— 对照组
        ("F1 三者皆 D8~D10 (全最强)",
         band(b20, 8, 10) & band(b60, 8, 10) & band(bm60, 8, 10)),
    ]
    crows, cyrows = [], []
    for name, mask in COMBOS:
        st, r_daily, cnt = L.state_nav(mask, oret_sig, C)
        rec = dict(combo=name, n=int(mask.sum()),
                   nav_cagr=st["cagr"], nav_mdd=st["mdd"], nav_sharpe=st["sharpe"],
                   nav_ann_arith=st["ann_arith"], avg_holdings=st["avg_holdings"])
        for H in [1, 5, 10, 20, 60]:
            mk = CHAIN[H][C["day_idx"]][mask]
            res = L.exw_t(FWD.get(H, df[f"fwd{H}"].values.astype(np.float64))[mask], mk,
                          C["day_idx"][mask])
            ts = L.trade_stats(df[f"fwd{H}"].values.astype(np.float64)[mask])
            if res and ts:
                rec[f"exw{H}"] = res["exw"]
                rec[f"t{H}"] = res["t"]
                rec[f"mean{H}"] = ts["mean"]
                rec[f"med{H}"] = ts["median"]
                rec[f"win{H}"] = ts["win"]
                rec[f"pf{H}"] = ts["pf"]
                rec[f"payoff{H}"] = ts["payoff"]
        crows.append(rec)
        cyrows += L.yearly_table(r_daily, C, name)
    cdf = pd.DataFrame(crows)
    cdf.to_csv(os.path.join(L.OUT, "v3b_d2d6_combo.csv"), index=False)
    pd.DataFrame(cyrows).to_csv(os.path.join(L.OUT, "v3b_d2d6_year.csv"), index=False)
    log(f"-> v3b_d2d6_combo.csv ({len(cdf)} 行), v3b_d2d6_year.csv")

    log("\n摘要 2 —— D2~D6 各组合（H=20）")
    log(f"  {'组合':<34}{'n':>9}{'CAGR':>9}{'算术年化':>10}{'MDD':>9}{'Sharpe':>8}{'exw20':>9}{'t':>7}{'win':>7}{'PF':>7}")
    for r in cdf.sort_values("exw20", ascending=False).itertuples():
        log(f"  {r.combo:<34}{r.n:>9,}{r.nav_cagr*100:>8.2f}%{r.nav_ann_arith*100:>9.2f}%"
            f"{r.nav_mdd*100:>8.1f}%{r.nav_sharpe:>8.2f}{r.exw20*100:>8.3f}%{r.t20:>7.2f}"
            f"{r.win20*100:>6.1f}%{r.pf20:>7.3f}")

    log("完成")


if __name__ == "__main__":
    main()
