#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
25_v2_benchmark.py — V2 基准比较 + 交易成本敏感性 + 研究漏斗

用户第二十二节: 9 个基准比较
  B1 买入持有沪深300 / B2 中证500 / B3 中证1000
  B4 随机选股 / B5 仅Momentum / B6 仅Volume / B7 仅MACD / B8 MACD+Volume / B9 V2最终模型
  必须回答: V2 到底增加了多少真正的增量信息?

用户第二十三节: 交易成本敏感性 (佣金/印花税/滑点/涨跌停/ST/新股)
用户第二十四节: 研究漏斗 —— 每一步的样本数量变化 + 平均收益变化

⚠️ 数据边界(必须披露):
  指数历史K线只有 2023-01 起 -> 指数 B&H 只能做 2023-2026 段;
  2015-2022 段的"指数"用 clean 样本自建等权市场组合替代(口径一致, 但非官方指数)。

产出:
  output/v2_benchmark.csv
  output/v2_cost_sens.csv
  output/v2_funnel.csv
"""
import os
import time
import json
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
RAW = os.path.join(ROOT, "data", "raw")
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

MIN_LIST = 120
HZ = [1, 5, 10, 20, 40, 60]
t0 = time.time()


def log(m):
    print(f"[{time.time()-t0:7.1f}s] {m}", flush=True)


def block_bounds(codes):
    chg = np.flatnonzero(codes[1:] != codes[:-1]) + 1
    return (np.concatenate([[0], chg]), np.concatenate([chg, [len(codes)]]), chg)


def shift_fwd(a, chg, n, k):
    out = np.full(n, np.nan, dtype=np.float64)
    idx = np.arange(n - k)
    ok = np.ones(n - k, dtype=bool)
    if k > 0:
        bound = np.zeros(n, dtype=np.int64); bound[chg] = 1
        csum = np.cumsum(bound)
        ok = csum[idx] == csum[idx + k]
    out[idx[ok]] = a[idx[ok] + k]
    return out


def exw_t(ret, mk, dates, min_n=50):
    ok = np.isfinite(ret) & np.isfinite(mk)
    if ok.sum() < min_n:
        return np.nan, np.nan, int(ok.sum())
    e = ret[ok] - mk[ok]
    dm = pd.Series(e).groupby(dates[ok]).mean().values
    if len(dm) < 5:
        return np.nan, np.nan, int(ok.sum())
    sd = float(dm.std(ddof=1))
    t = float(dm.mean() / (sd / np.sqrt(len(dm)))) if sd > 0 else np.nan
    return float(dm.mean()), t, int(ok.sum())


def main():
    cols = ["thscode", "date", "is_st_now", "days_since_list", "ret", "size_grp",
            "open_price", "can_buy_open", "can_sell_open",
            "golden_cross", "gc_below0", "gc_above0", "hist_up3",
            "hist", "hist_z", "vr20m", "rvol20", "volz20", "vt_5_20", "dvt5",
            "upvol_r20", "px_ma20_pct", "px_h60", "ret20", "hv20",
            "rs_mkt20", "rs_ind20"] + [f"fwd{H}" for H in HZ]
    df = pd.read_parquet(os.path.join(PROC, "v2_panel.parquet"), columns=cols)
    log(f"读入 {df.shape[0]:,} 行")

    m = (~df["is_st_now"].fillna(True)) & (df["days_since_list"].fillna(0) >= MIN_LIST) \
        & df["ret"].notna() & df["fwd20"].notna() & df["fwd60"].notna()
    df = df.loc[m].reset_index(drop=True)
    df = df.sort_values(["thscode", "date"], kind="stable").reset_index(drop=True)
    log(f"clean {len(df):,} 行 / {df['thscode'].nunique():,} 只 / {df['date'].nunique():,} 日")

    codes = df["thscode"].values
    starts, ends, chg = block_bounds(codes)
    n = len(df)
    d = df["date"].values
    yr = pd.to_datetime(d).year.values

    # 市场基准(同口径)
    mk = {}
    for H in HZ:
        mk[H] = pd.Series(df[f"fwd{H}"].values).groupby(d).transform("mean").values
    # 市场日收益序列(等权, 用于指数替代)
    mkt1 = pd.Series(df["ret"].values).groupby(d).mean()
    mkt_nav = (1.0 + mkt1).cumprod()

    # ================================================================
    # 第二十四节: 研究漏斗 —— 每步样本数 + 平均收益
    # ================================================================
    log("研究漏斗 ...")
    # ⚠️ 阈值只允许用训练段(2015-2022)估计, 严禁用全样本 —— 否则是前视偏差
    TR = yr <= 2022
    HV_MED_TR = float(np.nanmedian(df["hv20"].values[TR]))
    log(f"训练段(2015-2022) hv20 中位数 = {HV_MED_TR:.5f} (用于低波动阈值, 避免前视)")
    FUN = [
        ("① 全部股票(clean)", np.ones(n, bool)),
        ("② MACD事件(金叉)", df["golden_cross"].values == 1),
        ("③ + Volume Structure(量温和)", (df["golden_cross"].values == 1)
         & (df["vt_5_20"].values < 1.2)),
        ("④ + Price Position(不过热)", (df["golden_cross"].values == 1)
         & (df["vt_5_20"].values < 1.2) & (np.abs(df["px_ma20_pct"].values) < 0.05)),
        ("⑤ + Momentum(过去20日<10%)", (df["golden_cross"].values == 1)
         & (df["vt_5_20"].values < 1.2) & (np.abs(df["px_ma20_pct"].values) < 0.05)
         & (df["ret20"].values < 0.10)),
        ("⑥ + Volatility(低波动)", (df["golden_cross"].values == 1)
         & (df["vt_5_20"].values < 1.2) & (np.abs(df["px_ma20_pct"].values) < 0.05)
         & (df["ret20"].values < 0.10) & (df["hv20"].values < HV_MED_TR)),
        ("⑦ + Relative Strength(RS>0)", (df["golden_cross"].values == 1)
         & (df["vt_5_20"].values < 1.2) & (np.abs(df["px_ma20_pct"].values) < 0.05)
         & (df["ret20"].values < 0.10) & (df["hv20"].values < HV_MED_TR)
         & (df["rs_mkt20"].values > 0)),
    ]
    # 市值中性: 在⑦基础上限定非最小规模组
    base7 = FUN[-1][1]
    sg = df["size_grp"].values
    FUN.append(("⑧ + 市值中性(排除D1)", base7 & (sg > 0)))
    # 行业中性: 在⑧基础上用相对行业强弱
    FUN.append(("⑨ + 行业中性(RS_ind>0)", base7 & (sg > 0) & (df["rs_ind20"].values > 0)))
    # 样本外: ⑨ 限定 2025-2026
    FUN.append(("⑩ + 样本外(2025-2026)", base7 & (sg > 0) & (df["rs_ind20"].values > 0)
                & (yr >= 2025)))

    frows = []
    prev_n, prev_e = None, None
    for name, mask in FUN:
        r = df["fwd20"].values[mask]
        e = r - mk[20][mask]
        ew, tt, nn = exw_t(r, mk[20][mask], d[mask], min_n=30)
        ok = np.isfinite(r)
        frows.append({
            "step": name, "n": int(mask.sum()),
            "n_ok": int(ok.sum()),
            "mean20": float(r[ok].mean()) if ok.sum() else np.nan,
            "win20": float((r[ok] > 0).mean()) if ok.sum() else np.nan,
            "exw20": ew, "t20": tt,
            "d_n": (int(mask.sum()) - prev_n) if prev_n is not None else None,
            "d_exw": (ew - prev_e) if (prev_e is not None and np.isfinite(ew)) else None,
        })
        prev_n, prev_e = int(mask.sum()), ew
    fdf = pd.DataFrame(frows)
    fdf.to_csv(os.path.join(OUT, "v2_funnel.csv"), index=False)
    log(f"-> v2_funnel.csv ({len(fdf)} 行)")
    pd.set_option("display.width", 220)
    print("\n" + "=" * 118)
    print("【研究漏斗】每加一个条件后的样本数变化与日加权20日超额变化")
    print("=" * 118)
    print(fdf.to_string(index=False, float_format=lambda x: f"{x:.5f}"))

    # ================================================================
    # 第二十一节: 简单版本(3~5 条件) + 参数邻域稳定性
    # ================================================================
    log("简单版本 + 邻域稳定性 ...")
    GC = df["golden_cross"].values == 1
    VT = df["vt_5_20"].values
    PX = np.abs(df["px_ma20_pct"].values)
    R20 = df["ret20"].values
    HV = df["hv20"].values
    RSM = df["rs_mkt20"].values

    SIMPLE = [
        ("S3a 金叉+量温和+不过热",
         GC & (VT < 1.2) & (PX < 0.05)),
        ("S3b 金叉+量温和+没大涨",
         GC & (VT < 1.2) & (R20 < 0.10)),
        ("S4a 金叉+量温和+不过热+没大涨",
         GC & (VT < 1.2) & (PX < 0.05) & (R20 < 0.10)),
        ("S4b 金叉+量温和+不过热+RS>0",
         GC & (VT < 1.2) & (PX < 0.05) & (RSM > 0)),
        ("S5 金叉+量温和+不过热+没大涨+RS>0",
         GC & (VT < 1.2) & (PX < 0.05) & (R20 < 0.10) & (RSM > 0)),
    ]
    # 邻域稳定性: 逐个把关键阈值上下浮动, 看结论是否翻转(用户第一节原则①)
    NEIGH = [
        ("vt_5_20 < 1.1", GC & (VT < 1.1) & (PX < 0.05) & (R20 < 0.10) & (RSM > 0)),
        ("vt_5_20 < 1.2 [基准]", GC & (VT < 1.2) & (PX < 0.05) & (R20 < 0.10) & (RSM > 0)),
        ("vt_5_20 < 1.3", GC & (VT < 1.3) & (PX < 0.05) & (R20 < 0.10) & (RSM > 0)),
        ("|距MA20| < 0.03", GC & (VT < 1.2) & (PX < 0.03) & (R20 < 0.10) & (RSM > 0)),
        ("|距MA20| < 0.05 [基准]", GC & (VT < 1.2) & (PX < 0.05) & (R20 < 0.10) & (RSM > 0)),
        ("|距MA20| < 0.08", GC & (VT < 1.2) & (PX < 0.08) & (R20 < 0.10) & (RSM > 0)),
        ("ret20 < 0.05", GC & (VT < 1.2) & (PX < 0.05) & (R20 < 0.05) & (RSM > 0)),
        ("ret20 < 0.10 [基准]", GC & (VT < 1.2) & (PX < 0.05) & (R20 < 0.10) & (RSM > 0)),
        ("ret20 < 0.15", GC & (VT < 1.2) & (PX < 0.05) & (R20 < 0.15) & (RSM > 0)),
        ("rs_mkt20 > -0.02", GC & (VT < 1.2) & (PX < 0.05) & (R20 < 0.10) & (RSM > -0.02)),
        ("rs_mkt20 > 0 [基准]", GC & (VT < 1.2) & (PX < 0.05) & (R20 < 0.10) & (RSM > 0)),
        ("rs_mkt20 > 0.02", GC & (VT < 1.2) & (PX < 0.05) & (R20 < 0.10) & (RSM > 0.02)),
    ]
    srows = []
    for name, mask in SIMPLE:
        ew, tt, nn = exw_t(df["fwd20"].values[mask], mk[20][mask], d[mask], min_n=30)
        r = df["fwd20"].values[mask]
        srows.append({"model": name, "kind": "简单版本", "n": int(mask.sum()),
                      "mean20": float(np.nanmean(r)) if mask.sum() else np.nan,
                      "exw20": ew, "t20": tt})
    for name, mask in NEIGH:
        ew, tt, nn = exw_t(df["fwd20"].values[mask], mk[20][mask], d[mask], min_n=30)
        r = df["fwd20"].values[mask]
        srows.append({"model": name, "kind": "邻域稳定性", "n": int(mask.sum()),
                      "mean20": float(np.nanmean(r)) if mask.sum() else np.nan,
                      "exw20": ew, "t20": tt})
    sdf = pd.DataFrame(srows)
    sdf.to_csv(os.path.join(OUT, "v2_simple_neighborhood.csv"), index=False)
    log(f"-> v2_simple_neighborhood.csv ({len(sdf)} 行)")
    print("\n" + "=" * 118)
    print("【简单版本 + 邻域稳定性】用户第二十一节: 最多 3~5 条件, 简单优先")
    print("=" * 118)
    print(sdf.to_string(index=False, float_format=lambda x: f"{x:.5f}"))

    # ================================================================
    # 第二十二节: 9 个基准
    # ================================================================
    log("基准比较 ...")
    # 指数 B&H (仅 2023+ 可用)
    idx_nav = {}
    for tag, fn in [("B1_沪深300", "index_hs300"), ("B2_中证500", "index_zz500"),
                    ("B3_中证1000", "index_zz1000")]:
        js = json.load(open(os.path.join(RAW, fn + ".json")))
        s = pd.Series({pd.Timestamp(x["date_ms"], unit="ms").normalize(): x["close_price"]
                       for x in js}).sort_index()
        idx_nav[tag] = s

    def bh_stats(nav, a=2023, b=2026, H=20):
        """指数 B&H: 年化 / 最大回撤"""
        s = nav[(nav.index.year >= a) & (nav.index.year <= b)]
        if len(s) < 100:
            return np.nan, np.nan, 0
        ret = s.pct_change().dropna()
        years = len(ret) / 252.0
        cagr = (s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1
        dd = (s / s.cummax() - 1).min()
        return float(cagr), float(dd), len(s)

    # 策略型基准: 等权持有一篮子满足条件的股票, 20 日轮动, 计成本
    RT_COST = 0.0054

    def port_stats(sig, H=20, cost=RT_COST, a=2015, b=2026):
        """简化组合: 每个交易日等权买入全部信号股, 持有 H 日
        收益 = 信号日 fwd{H} 的等权均值; 年化按复利折算; 扣除每次轮动的往返成本"""
        sel = sig & (yr >= a) & (yr <= b)
        if sel.sum() < 200:
            return None
        r = df[f"fwd{H}"].values[sel]
        dd_ = d[sel]
        # 按日等权
        s = pd.Series(r).groupby(dd_).mean()
        # 年化(几何): 每 H 日一轮 -> 每年 252/H 轮
        per = float(np.mean(s))
        nper = 252.0 / H
        gross = (1.0 + per) ** nper - 1.0
        net = (1.0 + per - cost) ** nper - 1.0
        e = r - mk[H][sel]
        ew, tt, nn = exw_t(r, mk[H][sel], dd_, min_n=50)
        return {
            "n_sig": int(sel.sum()), "n_days": len(s),
            "mean_H": per, "cagr_gross": float(gross), "cagr_net": float(net),
            "exw_H": ew, "t_H": tt,
            "win": float((r > 0).mean()),
        }

    brows = []
    for tag, nav in idx_nav.items():
        c, ddv, nn = bh_stats(nav)
        brows.append({"bench": tag, "type": "B&H指数(2023-2026)", "n_sig": nn,
                      "mean_H": np.nan, "cagr_gross": c, "cagr_net": c,
                      "exw_H": np.nan, "t_H": np.nan, "win": np.nan,
                      "note": "指数K线仅2023起, 2015-2022不可得"})
    # 自建等权市场(全期)
    c_mkt = (mkt_nav.iloc[-1] / mkt_nav.iloc[0]) ** (252.0 / len(mkt_nav)) - 1
    brows.append({"bench": "B0_自建全A等权(2015-2026)", "type": "自建市场", "n_sig": n,
                  "mean_H": np.nan, "cagr_gross": float(c_mkt), "cagr_net": float(c_mkt),
                  "exw_H": 0.0, "t_H": np.nan, "win": np.nan,
                  "note": "clean样本等权净值, 口径与个股fwd一致"})

    # B4 随机选股(固定种子, 每天随机抽 100 只)
    rng = np.random.default_rng(20260923)
    rnd = np.zeros(n, bool)
    dser = pd.Series(np.arange(n))
    for _, gidx in dser.groupby(pd.Series(d)):
        gi = gidx.values
        rnd[rng.choice(gi, size=min(100, len(gi)), replace=False)] = True
    # B5~B9
    SIGS = [
        ("B4_随机选股", rnd),
        ("B5_仅Momentum(ret20 Top20%)", None),
        ("B6_仅Volume(vt_5_20 Top20%)", None),
        ("B7_仅MACD(hist_z Top20%)", None),
        ("B8_MACD+Volume(hist_z&vt_5_20 Top20%)", None),
        ("B9_V2最终模型(漏斗⑨)", FUN[-2][1]),
    ]
    # Top20% 按日截面
    def top20(v):
        rk = pd.Series(v).groupby(d).rank(pct=True).values
        return rk >= 0.80
    SIGS[1] = ("B5_仅Momentum(ret20 Top20%)", top20(df["ret20"].values))
    SIGS[2] = ("B6_仅Volume(vt_5_20 Top20%)", top20(df["vt_5_20"].values))
    SIGS[3] = ("B7_仅MACD(hist_z Top20%)", top20(df["hist_z"].values))
    SIGS[4] = ("B8_MACD+Volume(hist_z&vt_5_20 Top20%)",
               top20(df["hist_z"].values) & top20(df["vt_5_20"].values))

    for tag, sig in SIGS:
        st = port_stats(sig)
        if st is None:
            brows.append({"bench": tag, "type": "策略", "n_sig": 0,
                          "note": "样本不足"})
            continue
        st["bench"] = tag; st["type"] = "策略(20日轮动)"
        st["note"] = ""
        brows.append(st)

    bdf = pd.DataFrame(brows)
    bdf.to_csv(os.path.join(OUT, "v2_benchmark.csv"), index=False)
    log(f"-> v2_benchmark.csv ({len(bdf)} 行)")
    print("\n" + "=" * 118)
    print("【基准比较】V2 到底增加了多少增量信息? (exw_H = 日加权 H 日超额)")
    print("=" * 118)
    show = [c for c in ["bench", "type", "n_sig", "n_days", "mean_H", "cagr_gross",
                        "cagr_net", "exw_H", "t_H", "win", "note"] if c in bdf.columns]
    print(bdf[show].to_string(index=False, float_format=lambda x: f"{x:.5f}"))

    # ================================================================
    # 第二十三节: 交易成本敏感性
    # ================================================================
    log("交易成本敏感性 ...")
    COSTS = [0.0, 0.001, 0.0027, 0.0054, 0.008, 0.012, 0.02]
    crows = []
    for tag, sig in SIGS:
        for c in COSTS:
            st = port_stats(sig, cost=c)
            if st is None:
                continue
            crows.append({"bench": tag, "roundtrip_cost": c,
                          "cagr_gross": st["cagr_gross"], "cagr_net": st["cagr_net"],
                          "exw_H": st["exw_H"]})
    cdf = pd.DataFrame(crows)
    cdf.to_csv(os.path.join(OUT, "v2_cost_sens.csv"), index=False)
    log(f"-> v2_cost_sens.csv ({len(cdf)} 行)")
    if not cdf.empty:
        pv = cdf.pivot_table(index="bench", columns="roundtrip_cost",
                             values="cagr_net")
        print("\n" + "=" * 118)
        print("【交易成本敏感性】净年化 vs 单次往返成本 (0% / 0.1% / 0.27% / 0.54%(基准) / 0.8% / 1.2% / 2%)")
        print("=" * 118)
        print(pv.to_string(float_format=lambda x: f"{x:.4f}"))
        print("\n  基准成本 0.54% = 佣金双边万2.5 + 印花税卖出千0.5 + 过户费万0.1 + 滑点双边千1")
        print("  注: 未模拟的项 —— 涨停无法买入/跌停无法卖出/停牌/退市/ST/新股/股票池动态变化")

    # ================================================================
    # 附: 指数 B&H 的 2023-2026 段与 V2 模型同期对比
    # ================================================================
    print("\n" + "=" * 118)
    print("【同期对比】2023-2026 段: 指数 B&H vs V2 最终模型")
    print("=" * 118)
    st23 = port_stats(FUN[-2][1], a=2023, b=2026)
    for tag, nav in idx_nav.items():
        c, ddv, nn = bh_stats(nav)
        print(f"  {tag:<12} cagr={c*100:+7.2f}%  mdd={ddv*100:7.2f}%  n={nn}")
    if st23:
        print(f"  {'V2最终模型':<12} cagr(净)={st23['cagr_net']*100:+7.2f}%  "
              f"exw20={st23['exw_H']*100:+.3f}%  t={st23['t_H']:+.2f}  n={st23['n_sig']}")

    log("done")


if __name__ == "__main__":
    main()
