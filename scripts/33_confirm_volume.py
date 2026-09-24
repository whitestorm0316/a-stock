#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
33_confirm_volume.py —— V3B 第三节 + 第四节 + 第五节

第三节：加入「反转确认」而不是预测底部
  基线有**两个**（因为第一节已证明最优分位依赖时间尺度）：
    基线 S（短）: ret20 处于 D2~D6
    基线 L（长）: ret40 D1 ∪ ret60 D1 ∪ 距MA60 D1 的并集（"深度超跌"）
  在基线上分别叠加 8 个确认信号 A~H，测未来 1/3/5/10/20/60 日。

第四节：量能角色 —— 量能能否作为「反转确认」
  D1: Volume/VOL20 的 9 档阈值网格（在深度超跌基线上）
  D2: 「缩量下跌 → 放量上涨」专项

第五节：三阶段结构「先超跌 → 卖压衰竭 → 反转」

产出：
  output/v3b_confirm.csv       8 信号 × 2 基线 × 6 持有期
  output/v3b_vol_grid.csv      9 档阈值 × 2 基线 × 6 持有期
  output/v3b_stage3.csv        三阶段结构
  output/v3b_run_33.log
"""
import os
import time
import numpy as np
import pandas as pd
import v3b_lib as L

HZ = [1, 5, 10, 20, 40, 60]
LOGF = open(os.path.join(L.OUT, "v3b_run_33.log"), "w", encoding="utf-8")
_t0 = time.time()


def log(m):
    line = f"[{time.time()-_t0:7.1f}s] {m}"
    print(line, flush=True)
    LOGF.write(line + "\n")
    LOGF.flush()


def main():
    cols = ["ret5", "ret10", "ret20", "ret40", "ret60",
            "px_ma20_pct", "px_ma60_pct", "px_ma120_pct",
            "hist", "hist_z", "dif", "dea", "hist_upstreak",
            "volume", "rvol20", "vr20m", "volz20", "amt20_lag",
            "open_price", "close_price", "high_price", "low_price"] + \
           [f"fwd{H}" for H in HZ]
    df = L.load_clean(cols)
    C = L.ctx(df)
    log(f"clean {C['n']:,} 行 / {len(C['starts']):,} 只 / {C['n_days']:,} 交易日")

    oret, oret_sig = L.oret_from(df, C)
    mk_d = L.market_oret(oret, C)
    CHAIN = {H: L.chain_fwd(mk_d, C, H) for H in HZ}
    FWD = {H: df[f"fwd{H}"].values.astype(np.float64) for H in HZ}
    log("市场基准 完成")

    cl = df["close_price"].values.astype(np.float64)
    op = df["open_price"].values.astype(np.float64)
    hi = df["high_price"].values.astype(np.float64)
    lo = df["low_price"].values.astype(np.float64)

    # ---------------- 自算均线（面板无 MA5/MA10 水平与斜率）
    ma5 = L.roll_mean(cl, C, 5)
    ma10 = L.roll_mean(cl, C, 10)
    ma20 = L.roll_mean(cl, C, 20)
    ma5_prev = L.roll_shift_mean(cl, C, 5, 1)     # 前一日 MA5 → 斜率
    ma10_prev = L.roll_shift_mean(cl, C, 10, 1)
    ma20_prev = L.roll_shift_mean(cl, C, 20, 1)
    log("自算 MA5/MA10/MA20 与斜率 完成")

    # ---------------- 基线
    b20 = L.decile(df["ret20"].values.astype(np.float64), C, 10)
    b40 = L.decile(df["ret40"].values.astype(np.float64), C, 10)
    b60 = L.decile(df["ret60"].values.astype(np.float64), C, 10)
    bm60 = L.decile(df["px_ma60_pct"].values.astype(np.float64), C, 10)

    base_S = np.isfinite(b20) & (b20 >= 2) & (b20 <= 6)          # ret20 D2~D6（用户规格）
    base_L = (b40 == 1) | (b60 == 1) | (bm60 == 1)               # 任一深度超跌
    base_L = base_L & np.isfinite(b40) & np.isfinite(b60) & np.isfinite(bm60)
    log(f"基线 S (ret20 D2~D6)      {base_S.sum():,} 行 ({base_S.mean()*100:.1f}%)")
    log(f"基线 L (深度超跌并集)      {base_L.sum():,} 行 ({base_L.mean()*100:.1f}%)")

    # ---------------- 8 个确认信号
    up1 = cl > L.shift_block(cl, C, 1)                            # 今日涨
    up2 = up1 & (L.shift_block(cl, C, 1) > L.shift_block(cl, C, 2))   # 连续2日涨

    SIGS = [
        ("A Close>MA5", cl > ma5),
        ("B MA5向上", ma5 > ma5_prev),
        ("C 连续2日上涨", up2),
        ("D MACD柱连续3日改善", df["hist_upstreak"].values.astype(np.float64) >= 3),
        ("E MACD柱翻红", (df["hist"].values.astype(np.float64) > 0) &
                         (L.shift_block(df["hist"].values.astype(np.float64), C, 1) <= 0)),
        ("F DIF>DEA", df["dif"].values.astype(np.float64) > df["dea"].values.astype(np.float64)),
        ("G Close站上MA10", cl > ma10),
        ("H Close站上MA20", cl > ma20),
    ]

    def ev(mask, tag, H_extra=HZ):
        st, r_daily, cnt = L.state_nav(mask, oret_sig, C)
        rec = dict(tag=tag, n=int(mask.sum()),
                   nav_cagr=st["cagr"], nav_mdd=st["mdd"], nav_sharpe=st["sharpe"],
                   nav_ann_arith=st["ann_arith"], avg_holdings=st["avg_holdings"])
        for H in H_extra:
            mk = CHAIN[H][C["day_idx"]][mask]
            res = L.exw_t(FWD[H][mask], mk, C["day_idx"][mask])
            ts = L.trade_stats(FWD[H][mask])
            if res and ts:
                rec[f"exw{H}"] = res["exw"]
                rec[f"t{H}"] = res["t"]
                rec[f"mean{H}"] = ts["mean"]
                rec[f"med{H}"] = ts["median"]
                rec[f"win{H}"] = ts["win"]
                rec[f"pf{H}"] = ts["pf"]
                rec[f"payoff{H}"] = ts["payoff"]
        return rec, r_daily

    # ============================================================ 第三节
    log("\n第三节：8 个反转确认信号 × 2 个基线")
    rows, yrows = [], []
    for bn, base in [("S ret20 D2~D6", base_S), ("L 深度超跌并集", base_L)]:
        r0, rd0 = ev(base, f"{bn} | 无确认(基线)")
        rows.append(r0)
        yrows += L.yearly_table(rd0, C, f"{bn} | 无确认(基线)")
        for sn, s in SIGS:
            m = base & np.asarray(s, bool)
            m = m & np.isfinite(m) if m.dtype != bool else m
            r, rd = ev(m, f"{bn} | {sn}")
            rows.append(r)
            yrows += L.yearly_table(rd, C, f"{bn} | {sn}")
    cdf = pd.DataFrame(rows)
    cdf.to_csv(os.path.join(L.OUT, "v3b_confirm.csv"), index=False)
    pd.DataFrame(yrows).to_csv(os.path.join(L.OUT, "v3b_confirm_year.csv"), index=False)
    log(f"-> v3b_confirm.csv ({len(cdf)} 行)")

    log("\n摘要 3 —— 反转确认信号（H=20 超额，按基线分组）")
    for bn in ["S ret20 D2~D6", "L 深度超跌并集"]:
        s = cdf[cdf.tag.str.startswith(bn)].copy()
        s["sig"] = s.tag.str.split("|").str[-1].str.strip()
        b0 = s[s.sig == "无确认(基线)"]
        log(f"  --- 基线 {bn} ---")
        if len(b0):
            log(f"    {'无确认(基线)':<24} n={int(b0.n.iloc[0]):>9,}  "
                f"exw20={b0.exw20.iloc[0]*100:+.3f}% (t{b0.t20.iloc[0]:+.2f})  "
                f"exw5={b0.exw5.iloc[0]*100:+.3f}% (t{b0.t5.iloc[0]:+.2f})  "
                f"exw60={b0.exw60.iloc[0]*100:+.3f}% (t{b0.t60.iloc[0]:+.2f})")
        for r in s[s.sig != "无确认(基线)"].sort_values("exw20", ascending=False).itertuples():
            log(f"    {r.sig:<24} n={r.n:>9,}  "
                f"exw20={r.exw20*100:+.3f}% (t{r.t20:+.2f})  "
                f"exw5={r.exw5*100:+.3f}% (t{r.t5:+.2f})  "
                f"exw60={r.exw60*100:+.3f}% (t{r.t60:+.2f})")

    # ============================================================ 第四节
    log("\n第四节：量能作为反转确认")
    vol = df["volume"].values.astype(np.float64)
    rv20 = df["rvol20"].values.astype(np.float64)      # volume / VOL20
    rv20_lag3 = L.shift_block(rv20, C, 3)

    THR = [0.5, 0.7, 0.9, 1.0, 1.1, 1.2, 1.5, 2.0, 2.5]
    vrows = []
    for bn, base in [("S ret20 D2~D6", base_S), ("L 深度超跌并集", base_L)]:
        r0, _ = ev(base, f"{bn} | 基线")
        vrows.append(r0)
        for t in THR:
            m = base & np.isfinite(rv20) & (rv20 >= t)
            r, _ = ev(m, f"{bn} | 量比>={t}")
            vrows.append(r)
        for t in THR:
            m = base & np.isfinite(rv20) & (rv20 <= t)
            r, _ = ev(m, f"{bn} | 量比<={t}")
            vrows.append(r)
    # 「缩量下跌 → 放量上涨」专项
    shrink = np.isfinite(rv20_lag3) & (rv20_lag3 < 0.8)
    expand = np.isfinite(rv20) & (rv20 > 1.2)
    bull = cl > op
    vrows.append(ev(base_S & shrink & expand & bull, "S | 缩量下跌→放量上涨")[0])
    vrows.append(ev(base_L & shrink & expand & bull, "L | 缩量下跌→放量上涨")[0])
    vrows.append(ev(base_S & shrink & expand, "S | 缩量→放量(不限阳线)")[0])
    vrows.append(ev(base_L & shrink & expand, "L | 缩量→放量(不限阳线)")[0])
    vrows.append(ev(base_S & expand & bull, "S | 放量上涨(不限前期缩量)")[0])
    vrows.append(ev(base_L & expand & bull, "L | 放量上涨(不限前期缩量)")[0])
    # 连续3日量比渐降（卖压衰竭）
    rv1 = rv20
    rv2 = L.shift_block(rv20, C, 1)
    rv3 = L.shift_block(rv20, C, 2)
    decay3 = np.isfinite(rv1) & np.isfinite(rv2) & np.isfinite(rv3) & \
        (rv1 < rv2) & (rv2 < rv3)
    vrows.append(ev(base_S & decay3, "S | 连续3日量比渐降")[0])
    vrows.append(ev(base_L & decay3, "L | 连续3日量比渐降")[0])

    vdf = pd.DataFrame(vrows)
    vdf.to_csv(os.path.join(L.OUT, "v3b_vol_grid.csv"), index=False)
    log(f"-> v3b_vol_grid.csv ({len(vdf)} 行)")

    log("\n摘要 4 —— 量比阈值网格（H=20，基线 L 深度超跌）")
    s = vdf[vdf.tag.str.startswith("L 深度超跌并集")]
    for r in s.itertuples():
        nm = r.tag.split("|")[-1].strip()
        log(f"    {nm:<28} n={r.n:>9,}  exw5={r.exw5*100:+.3f}%(t{r.t5:+.2f})  "
            f"exw20={r.exw20*100:+.3f}%(t{r.t20:+.2f})  exw60={r.exw60*100:+.3f}%(t{r.t60:+.2f})")
    log("\n摘要 4b —— 缩量→放量 专项（两个基线）")
    for r in vdf[vdf.tag.str.contains("缩量|放量")].itertuples():
        log(f"    {r.tag:<34} n={r.n:>9,}  exw5={r.exw5*100:+.3f}%(t{r.t5:+.2f})  "
            f"exw10={r.exw10*100:+.3f}%(t{r.t10:+.2f})  exw20={r.exw20*100:+.3f}%(t{r.t20:+.2f})")

    # ============================================================ 第五节
    log("\n第五节：三阶段结构")
    # 阶段1：超跌（两个口径都测）
    # 阶段2：卖压衰竭 = 过去 3~5 日量能渐降 且 价格不创新低
    lo5 = L.roll_mean(lo, C, 5)
    lo5_prev = L.roll_shift_mean(lo, C, 5, 1)
    no_new_low = np.isfinite(lo5) & np.isfinite(lo5_prev) & (lo5 >= lo5_prev)
    vt = df["vr20m"].values.astype(np.float64)      # 已是 volume / VOL20 口径
    decay = np.isfinite(vt) & (vt < 1.0) & (L.shift_block(vt, C, 1) < 1.0)
    decay5 = np.isfinite(vt) & (L.roll_mean(vt, C, 5) < 1.0)
    srow = []
    for bn, base in [("S", base_S), ("L", base_L)]:
        p1 = base
        p2a = p1 & decay & no_new_low
        p2b = p1 & decay5 & no_new_low
        # 阶段3：反转确认 = Close>MA5 且 量比>1.2 且 MACD柱改善
        p3 = cl > ma5
        p3 &= np.isfinite(rv20) & (rv20 > 1.2)
        p3 &= np.isfinite(df["hist_upstreak"].values.astype(np.float64)) & \
              (df["hist_upstreak"].values.astype(np.float64) >= 2)
        srow.append(ev(p1, f"{bn} | 阶段1 超跌")[0])
        srow.append(ev(p2a, f"{bn} | 阶段1+2a 量能渐降+不创新低")[0])
        srow.append(ev(p2b, f"{bn} | 阶段1+2b 5日低量+不创新低")[0])
        srow.append(ev(p1 & p3, f"{bn} | 阶段1+3 直接反转确认")[0])
        srow.append(ev(p2a & p3, f"{bn} | 阶段1+2a+3 完整三阶段")[0])
        srow.append(ev(p2b & p3, f"{bn} | 阶段1+2b+3 完整三阶段")[0])
    sdf = pd.DataFrame(srow)
    sdf.to_csv(os.path.join(L.OUT, "v3b_stage3.csv"), index=False)
    log(f"-> v3b_stage3.csv ({len(sdf)} 行)")

    log("\n摘要 5 —— 三阶段结构")
    log(f"    {'变体':<36}{'n':>10}{'exw5':>9}{'t':>7}{'exw20':>9}{'t':>7}{'exw60':>9}{'t':>7}{'win20':>8}{'PF20':>7}")
    for r in sdf.itertuples():
        log(f"    {r.tag:<36}{r.n:>10,}{r.exw5*100:>8.3f}%{r.t5:>7.2f}"
            f"{r.exw20*100:>8.3f}%{r.t20:>7.2f}{r.exw60*100:>8.3f}%{r.t60:>7.2f}"
            f"{r.win20*100:>7.1f}%{r.pf20:>7.3f}")

    log("完成")


if __name__ == "__main__":
    main()
