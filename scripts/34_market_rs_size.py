#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
34_market_rs_size.py —— V3B 第六节 + 第七节 + 第八节

第六节：市场环境过滤
  ⚠️ 指数原始数据仅 2023 起（902 行），故全部从面板自建：
     - 等权市场净值 → MA20 / MA60
     - Breadth = 当日上涨股票数 / 有效股票数
     - 指数 HV20 = 等权市场日收益的 20 日滚动标准差 × sqrt(252)
  对比「个股超跌 + 市场 Close>MA60」vs「+ 市场 Close<MA60」

第七节：相对强弱
  rs_mkt20 = ret20 − 全市场等权 ret20   （绝对 + 相对）
  rs_sz20  = ret20 − 同 size_grp 组等权 ret20
  重点研究「绝对超跌 + 相对强势」

第八节：市值
  size_grp 0..9（0 = 最小市值 10%）
  5 组：最小10% / 10-30% / 30-70% / 70-90% / 最大10%
  检验「剔除最小 10%」是否真改善稳定性

产出：
  output/v3b_market.csv, v3b_market_year.csv
  output/v3b_rs.csv, v3b_size.csv
  output/v3b_run_34.log
"""
import os
import time
import numpy as np
import pandas as pd
import v3b_lib as L

HZ = [1, 5, 10, 20, 40, 60]
LOGF = open(os.path.join(L.OUT, "v3b_run_34.log"), "w", encoding="utf-8")
_t0 = time.time()


def log(m):
    line = f"[{time.time()-_t0:7.1f}s] {m}"
    print(line, flush=True)
    LOGF.write(line + "\n")
    LOGF.flush()


def main():
    cols = ["ret20", "ret40", "ret60", "px_ma60_pct", "rs_mkt20", "rs_mkt60",
            "rs_sz20", "rs_sz60", "size_grp", "hv20", "open_price", "close_price"] + \
           [f"fwd{H}" for H in HZ]
    df = L.load_clean(cols)
    C = L.ctx(df)
    log(f"clean {C['n']:,} 行 / {len(C['starts']):,} 只 / {C['n_days']:,} 交易日")

    oret, oret_sig = L.oret_from(df, C)
    mk_d = L.market_oret(oret, C)
    CHAIN = {H: L.chain_fwd(mk_d, C, H) for H in HZ}
    FWD = {H: df[f"fwd{H}"].values.astype(np.float64) for H in HZ}
    log("市场基准 完成")

    b20 = L.decile(df["ret20"].values.astype(np.float64), C, 10)
    b40 = L.decile(df["ret40"].values.astype(np.float64), C, 10)
    b60 = L.decile(df["ret60"].values.astype(np.float64), C, 10)
    bm60 = L.decile(df["px_ma60_pct"].values.astype(np.float64), C, 10)
    base_S = np.isfinite(b20) & (b20 >= 2) & (b20 <= 6)
    base_L = ((b40 == 1) | (b60 == 1) | (bm60 == 1)) & \
        np.isfinite(b40) & np.isfinite(b60) & np.isfinite(bm60)

    def ev(mask, tag):
        st, r_daily, cnt = L.state_nav(mask, oret_sig, C)
        rec = dict(tag=tag, n=int(mask.sum()),
                   nav_cagr=st["cagr"], nav_mdd=st["mdd"], nav_sharpe=st["sharpe"],
                   nav_ann_arith=st["ann_arith"], avg_holdings=st["avg_holdings"])
        for H in HZ:
            res = L.exw_t(FWD[H][mask], CHAIN[H][C["day_idx"]][mask], C["day_idx"][mask])
            ts = L.trade_stats(FWD[H][mask])
            if res and ts:
                rec[f"exw{H}"] = res["exw"]; rec[f"t{H}"] = res["t"]
                rec[f"mean{H}"] = ts["mean"]; rec[f"med{H}"] = ts["median"]
                rec[f"win{H}"] = ts["win"]; rec[f"pf{H}"] = ts["pf"]
                rec[f"payoff{H}"] = ts["payoff"]
        return rec, r_daily

    # ==================================================== 第六节 市场环境
    log("\n第六节：市场环境（全部自建）")
    # 等权市场净值
    nav = np.concatenate([[1.0], np.cumprod(1.0 + np.nan_to_num(mk_d))])[1:]
    mnav = pd.Series(nav)
    mma20 = mnav.rolling(20).mean().values
    mma60 = mnav.rolling(60).mean().values
    m_above60 = nav > mma60
    m_ma20_up = mma20 > mma60
    # 市场 HV20
    mhv20 = pd.Series(np.nan_to_num(mk_d)).rolling(20).std().values * np.sqrt(252)
    mhv_med = np.nanmedian(mhv20)
    # Breadth：当日上涨股票占比
    up = (oret > 0)
    dd = C["day_idx"]
    br_sum = np.bincount(dd[up & np.isfinite(oret)], minlength=C["n_days"])
    br_cnt = np.bincount(dd[np.isfinite(oret)], minlength=C["n_days"])
    breadth = np.where(br_cnt > 0, br_sum / np.maximum(br_cnt, 1), np.nan)
    br_med = np.nanmedian(breadth)
    log(f"  市场 Close>MA60 的交易日占比 {np.nanmean(m_above60)*100:.1f}%")
    log(f"  市场 MA20>MA60 的交易日占比 {np.nanmean(m_ma20_up)*100:.1f}%")
    log(f"  市场 HV20 中位数 {mhv_med*100:.1f}%   Breadth 中位数 {br_med*100:.1f}%")

    D = C["day_idx"]
    regimes = [
        ("市场 Close>MA60", m_above60[D]),
        ("市场 Close<MA60", ~m_above60[D]),
        ("市场 MA20>MA60", m_ma20_up[D]),
        ("市场 MA20<MA60", ~m_ma20_up[D]),
        ("市场 HV20 > 中位", mhv20[D] > mhv_med),
        ("市场 HV20 < 中位", mhv20[D] < mhv_med),
        ("Breadth > 中位", breadth[D] > br_med),
        ("Breadth < 中位", breadth[D] < br_med),
    ]
    mrows, myrows = [], []
    for bn, base in [("S ret20 D2~D6", base_S), ("L 深度超跌", base_L)]:
        r0, rd0 = ev(base, f"{bn} | 全环境")
        mrows.append(r0); myrows += L.yearly_table(rd0, C, f"{bn} | 全环境")
        for rn, rm in regimes:
            m = base & np.asarray(rm, bool)
            r, rd = ev(m, f"{bn} | {rn}")
            mrows.append(r); myrows += L.yearly_table(rd, C, f"{bn} | {rn}")
    mdf = pd.DataFrame(mrows)
    mdf.to_csv(os.path.join(L.OUT, "v3b_market.csv"), index=False)
    pd.DataFrame(myrows).to_csv(os.path.join(L.OUT, "v3b_market_year.csv"), index=False)
    log(f"-> v3b_market.csv ({len(mdf)} 行)")

    log("\n摘要 6 —— 市场环境过滤（H=20 / H=5 超额）")
    for bn in ["S ret20 D2~D6", "L 深度超跌"]:
        log(f"  --- {bn} ---")
        for r in mdf[mdf.tag.str.startswith(bn)].itertuples():
            nm = r.tag.split("|")[-1].strip()
            log(f"    {nm:<22} n={r.n:>9,}  exw5={r.exw5*100:+.3f}%(t{r.t5:+.2f})  "
                f"exw20={r.exw20*100:+.3f}%(t{r.t20:+.2f})  CAGR={r.nav_cagr*100:+.2f}%")

    # ==================================================== 第七节 相对强弱
    log("\n第七节：相对强弱（绝对超跌 + 相对强势）")
    rsm20 = df["rs_mkt20"].values.astype(np.float64)
    rsz20 = df["rs_sz20"].values.astype(np.float64)
    rsm60 = df["rs_mkt60"].values.astype(np.float64)
    drsm20 = L.decile(rsm20, C, 10)
    drsz20 = L.decile(rsz20, C, 10)
    drsm60 = L.decile(rsm60, C, 10)

    rrows = []
    for bn, base in [("S ret20 D2~D6", base_S), ("L 深度超跌", base_L)]:
        rrows.append(ev(base, f"{bn} | 基线")[0])
        for dn, dd_ in [("rs_mkt20", drsm20), ("rs_sz20", drsz20), ("rs_mkt60", drsm60)]:
            for lo, hi, lab in [(1, 1, "D1 最弱"), (1, 3, "D1~D3 弱"),
                                (4, 7, "D4~D7 中"), (8, 10, "D8~D10 强")]:
                m = base & np.isfinite(dd_) & (dd_ >= lo) & (dd_ <= hi)
                rrows.append(ev(m, f"{bn} | {dn} {lab}")[0])
        # 绝对超跌 + 相对强势（同时看）
        m = base & np.isfinite(drsm20) & (drsm20 >= 8)
        rrows.append(ev(m, f"{bn} | 绝对超跌+rs_mkt20 强")[0])
        m = base & np.isfinite(drsz20) & (drsz20 >= 8)
        rrows.append(ev(m, f"{bn} | 绝对超跌+rs_sz20 强")[0])
    rdf = pd.DataFrame(rrows)
    rdf.to_csv(os.path.join(L.OUT, "v3b_rs.csv"), index=False)
    log(f"-> v3b_rs.csv ({len(rdf)} 行)")

    log("\n摘要 7 —— 相对强弱（H=20 超额）")
    for bn in ["S ret20 D2~D6", "L 深度超跌"]:
        log(f"  --- {bn} ---")
        for r in rdf[rdf.tag.str.startswith(bn)].itertuples():
            nm = r.tag.split("|")[-1].strip()
            log(f"    {nm:<28} n={r.n:>9,}  exw5={r.exw5*100:+.3f}%(t{r.t5:+.2f})  "
                f"exw20={r.exw20*100:+.3f}%(t{r.t20:+.2f})  exw60={r.exw60*100:+.3f}%(t{r.t60:+.2f})")

    # ==================================================== 第八节 市值
    log("\n第八节：市值分位")
    sg = df["size_grp"].values.astype(np.float64)
    GROUPS = [("最小10%", sg == 0), ("10-30%", (sg >= 1) & (sg <= 2)),
              ("30-70%", (sg >= 3) & (sg <= 6)), ("70-90%", (sg >= 7) & (sg <= 8)),
              ("最大10%", sg == 9)]
    srows = []
    for bn, base in [("S ret20 D2~D6", base_S), ("L 深度超跌", base_L)]:
        srows.append(ev(base, f"{bn} | 全市值")[0])
        for gn, gm in GROUPS:
            srows.append(ev(base & gm, f"{bn} | {gn}")[0])
        srows.append(ev(base & (sg >= 1), f"{bn} | 剔除最小10%")[0])
    sdf = pd.DataFrame(srows)
    sdf.to_csv(os.path.join(L.OUT, "v3b_size.csv"), index=False)
    log(f"-> v3b_size.csv ({len(sdf)} 行)")

    log("\n摘要 8 —— 市值分组（H=20 超额）")
    for bn in ["S ret20 D2~D6", "L 深度超跌"]:
        log(f"  --- {bn} ---")
        for r in sdf[sdf.tag.str.startswith(bn)].itertuples():
            nm = r.tag.split("|")[-1].strip()
            log(f"    {nm:<16} n={r.n:>9,}  exw5={r.exw5*100:+.3f}%(t{r.t5:+.2f})  "
                f"exw20={r.exw20*100:+.3f}%(t{r.t20:+.2f})  CAGR={r.nav_cagr*100:+.2f}%  "
                f"MDD={r.nav_mdd*100:.1f}%  Sharpe={r.nav_sharpe:+.2f}")

    # 市场环境逐年（判断是否解决 2017/2018 问题）
    log("\n摘要 6b —— 市场过滤后的逐年（L 深度超跌，毛收益）")
    for nm in ["L 深度超跌 | 全环境", "L 深度超跌 | 市场 Close>MA60",
               "L 深度超跌 | 市场 Close<MA60", "L 深度超跌 | Breadth > 中位"]:
        s = pd.DataFrame(myrows)
        s = s[s.variant == nm]
        if not len(s):
            continue
        log(f"    {nm:<30} " + " ".join(f"{int(r.year)}:{r.ret*100:+.0f}%" for r in s.itertuples()))

    log("完成")


if __name__ == "__main__":
    main()
