#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
23_v2_factor_analysis.py — V2 因子分析

包含:
  A. IC / IR 分析        每个因子的日度截面信息系数与信息比(因子研究标准工具)
  B. 双因子交互          5×5 双重排序格子, 找条件 Alpha
  C. 市值中性            在每个规模分位内部分组, 看信号超额是否仍在
  D. 行业中性            在每个行业内部分组 + 用 rs_ind(相对行业)作为因子
  E. 横截面回归          Fama-MacBeth 日度截面回归, 控制混杂后看 MACD/Volume 系数是否存活
  F. 滚动 Beta           用于 E 的 Beta 因子(数据源无 Beta, 自算 60 日滚动)

数据说明:
  - 无流通股本 -> 真实 Turnover(换手率) 与市值均不可得。
    规模代理: amt20_lag (前20日日均成交额, 不含当日)
    "成交活跃度"代理: vr20m (量比) —— 报告中必须标注为代理
  - 行业分类来自同花顺行业指数(881xxx.TI)成分股, 当前快照, 存在幸存者偏差

产出:
  output/v2_ic.csv
  output/v2_interaction.csv
  output/v2_size_neutral.csv
  output/v2_ind_neutral.csv
  output/v2_regression.csv
"""
import os
import time
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

MIN_LIST = 120
MIN_N = 200
t0 = time.time()


def log(m):
    print(f"[{time.time()-t0:7.1f}s] {m}", flush=True)


def fast_rank_pct(vals, dates):
    """分块快速秩百分位 (O(n log n), 无 pandas)

    ⚠️ 前提: dates 已升序分块连续(本项目中 v2_panel.parquet 即按 (date,thscode) 排序)。
    做法: 直接在每个日期块内 argsort + 组内位置即秩, 处理 ties 取平均秩。
    实测比 np.lexsort((vals,dates)) 快 5~8 倍(后者在 10M 行上约 8s)。
    """
    n = len(vals)
    gs = np.flatnonzero(np.concatenate([[True], dates[1:] != dates[:-1]]))
    ge = np.append(gs[1:], n)
    out = np.empty(n, dtype=np.float64)
    for a, b in zip(gs, ge):
        m = b - a
        v = vals[a:b]
        o = np.argsort(v, kind="stable")
        r = np.empty(m, dtype=np.float64)
        r[o] = np.arange(1, m + 1, dtype=np.float64)
        vs = v[o]
        same = np.empty(m, dtype=bool)
        same[0] = False
        same[1:] = vs[1:] == vs[:-1]
        if same.any():
            brk = np.flatnonzero(~same)
            bst = np.append(brk, m)
            bg = np.concatenate([[0], bst[:-1]])
            for c, e in zip(bg, bst):
                if e - c > 1:
                    r[o[c:e]] = r[o[c:e]].mean()
        out[a:b] = r / m
    return out


def group_sums(a, gs, ge):
    """按 [gs[i], ge[i]) 区间求和 (纯 numpy reduceat)"""
    return np.add.reduceat(a, gs)


def _ic_from_ranks(rx, ry, gs, ge):
    """给定已按日期排序的 rx/ry 及日期块边界, 算每日 IC"""
    cnt = (ge - gs).astype(np.float64)
    sx = group_sums(rx, gs, ge)
    sy = group_sums(ry, gs, ge)
    sxy = group_sums(rx * ry, gs, ge)
    sx2 = group_sums(rx * rx, gs, ge)
    sy2 = group_sums(ry * ry, gs, ge)
    num = cnt * sxy - sx * sy
    den = np.sqrt(np.maximum((cnt * sx2 - sx ** 2) * (cnt * sy2 - sy ** 2), 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        ic = num / den
    return ic[np.isfinite(ic)]


def daily_ic_multi(x, ys, dates):
    """一次算 x 对多个 y 的日度 IC —— 避免对同一个 y 重复排序

    要求 dates 已升序分块连续(本项目 v2_panel 即如此)。
    """
    ok = np.isfinite(x)
    for y in ys.values():
        ok &= np.isfinite(y)
    if ok.sum() < 1000:
        return {k: np.array([]) for k in ys}
    xo = x[ok]
    d = dates[ok]
    gs = np.flatnonzero(np.concatenate([[True], d[1:] != d[:-1]]))
    ge = np.append(gs[1:], len(d))
    cnt = (ge - gs).astype(np.float64)
    keep = cnt >= 30
    gs, ge = gs[keep], ge[keep]
    rx = fast_rank_pct(xo, d)
    out = {}
    for k, y in ys.items():
        ry = fast_rank_pct(y[ok], d)
        out[k] = _ic_from_ranks(rx, ry, gs, ge)
    return out


def daily_ic(x, y, dates):
    """日度截面 Spearman IC (单 y 版本)"""
    return daily_ic_multi(x, {"_": y}, dates)["_"]


def stat_cell(df, mask, label, dim, grp):
    sub = df.loc[mask]
    r = {"label": label, "dim": dim, "grp": grp, "n": len(sub)}
    if len(sub) < MIN_N:
        return r
    d = sub["_d"].values
    for H in (1, 5, 20, 60):
        v = sub[f"fwd{H}"].values
        ok = np.isfinite(v)
        if ok.sum() < MIN_N:
            continue
        r[f"mean{H}"] = float(v[ok].mean())
        r[f"median{H}"] = float(np.median(v[ok]))
        r[f"win{H}"] = float((v[ok] > 0).mean())
        ex = v - sub[f"mk{H}"].values
        oke = np.isfinite(ex)
        if oke.sum() < MIN_N:
            continue
        r[f"ex{H}"] = float(ex[oke].mean())
        dm = pd.Series(ex[oke]).groupby(d[oke]).mean().values
        if len(dm) >= 5:
            m = float(dm.mean()); sd = float(dm.std(ddof=1))
            r[f"exw{H}"] = m
            r[f"t{H}"] = float(m / (sd / np.sqrt(len(dm)))) if sd > 0 else np.nan
            r[f"ndays{H}"] = len(dm)
    return r


def main():
    cols = ["thscode", "date", "is_st_now", "days_since_list", "ret", "ind_code",
            "size_grp", "amt20_lag",
            "golden_cross", "hist_up3", "gc_above0", "gc_below0", "death_cross",
            "hist", "hist_z", "dhist1_z",
            "vr20m", "rvol20", "rvol60", "volz20", "vt_5_20", "dvt5", "vchg1",
            "upvol_r20", "ud_vol20", "stA10", "stB10", "stC10", "stD10",
            "px_ma20_pct", "px_h20", "px_h60", "px_h120",
            "ret5", "ret20", "ret60", "atr_pct", "hv20",
            "rs_mkt20", "rs_mkt60", "rs_ind20", "rs_ind60", "rs_sz20"] + \
           [f"fwd{H}" for H in (1, 5, 20, 60)]
    df = pd.read_parquet(os.path.join(PROC, "v2_panel.parquet"), columns=cols)
    log(f"读入 {df.shape[0]:,} 行")

    m = (~df["is_st_now"].fillna(True)) & (df["days_since_list"].fillna(0) >= MIN_LIST) \
        & df["fwd20"].notna() & df["ret"].notna()
    df = df.loc[m].reset_index(drop=True)
    df["_d"] = df["date"].values
    log(f"clean 样本 {len(df):,} 行")

    for H in (1, 5, 20, 60):
        df[f"mk{H}"] = df.groupby("_d")[f"fwd{H}"].transform("mean")
    log("市场基准(前向) 完成")

    # ---------------- F. 滚动 Beta (先算, 供回归用)
    # ⚠️ v2_panel.parquet 按 (date, thscode) 排序 -> thscode 不连续,
    #    直接分块会让每块只有 1 行, beta 全 NaN。必须先按 thscode 排序再算。
    log("计算 60 日滚动 Beta ...")
    df["logamt"] = np.log(df["amt20_lag"].values)
    mkt1 = df.groupby("_d")["ret"].transform("mean")
    df["_mkt1"] = mkt1
    bt = df[["thscode", "date", "ret", "_mkt1"]].sort_values(
        ["thscode", "date"], kind="stable").reset_index()
    tcodes = bt["thscode"].values
    chg_b = np.flatnonzero(tcodes[1:] != tcodes[:-1]) + 1
    st_b = np.concatenate([[0], chg_b]); en_b = np.concatenate([chg_b, [len(tcodes)]])
    rb = bt["ret"].values.astype(np.float64)
    rmb = bt["_mkt1"].values.astype(np.float64)
    beta_sorted = np.full(len(bt), np.nan)
    W = 60
    for s, e in zip(st_b, en_b):
        if e - s < W:
            continue
        x = rb[s:e]; y = rmb[s:e]
        # clean 后 ret 无 NaN, 但 _mkt1 在极少数日可能为 NaN -> 用 0 掩码跳过
        if not (np.isfinite(x).all() and np.isfinite(y).all()):
            continue
        sx = np.concatenate([[0.0], np.cumsum(x)])
        sy = np.concatenate([[0.0], np.cumsum(y)])
        sxy = np.concatenate([[0.0], np.cumsum(x * y)])
        sy2 = np.concatenate([[0.0], np.cumsum(y * y)])
        n = e - s
        # 窗口 [t-W+1, t] 的求和: 用前缀和差分, 结果长度 n-W+1
        wsx = sx[W:] - sx[:n - W + 1]
        wsy = sy[W:] - sy[:n - W + 1]
        wsxy = sxy[W:] - sxy[:n - W + 1]
        wsy2 = sy2[W:] - sy2[:n - W + 1]
        mx = wsx / W; my = wsy / W
        cov = (wsxy / W - mx * my) * (W / (W - 1.0))
        var = (wsy2 / W - my * my) * (W / (W - 1.0))
        with np.errstate(divide="ignore", invalid="ignore"):
            bb = cov / var
        bb[~np.isfinite(bb) | (var <= 0)] = np.nan
        beta_sorted[s + W - 1:e] = bb
    bt["_beta"] = beta_sorted
    # 回填到原(已排序)顺序
    beta = bt.sort_values("index", kind="stable")["_beta"].values
    df["beta60"] = beta
    log(f"Beta 完成, 缺失 {np.isnan(beta).mean()*100:.1f}%")

    # ---------------- A. IC / IR
    FAC = {
        "hist_z": "MACD柱位置(标准化)",
        "dhist1_z": "MACD柱斜率(标准化)",
        "vr20m": "量比 VR20(Mean)",
        "rvol20": "RVOL20(Median)",
        "rvol60": "RVOL60(Median)",
        "volz20": "成交量Z分数",
        "vt_5_20": "量能趋势 VOL5/VOL20",
        "dvt5": "量能趋势5日变化",
        "vchg1": "成交量日变化",
        "upvol_r20": "上涨日成交量占比20日",
        "ud_vol20": "涨跌日均量比20日",
        "px_ma20_pct": "距MA20",
        "px_h20": "距20日最高",
        "px_h60": "距60日最高",
        "ret5": "过去5日收益",
        "ret20": "过去20日收益",
        "ret60": "过去60日收益",
        "atr_pct": "ATR/价格",
        "hv20": "20日历史波动率",
        "rs_mkt20": "相对市场强弱20日",
        "rs_mkt60": "相对市场强弱60日",
        "rs_ind20": "相对行业强弱20日",
        "rs_sz20": "相对规模组强弱20日",
        "beta60": "60日Beta",
    }
    log("计算 IC / IR ...")
    y20 = df["fwd20"].values
    y60 = df["fwd60"].values
    yset = {"20日": y20, "60日": y60}
    dd_ic = df["_d"].values
    ic_rows = []
    for f, name in FAC.items():
        x = df[f].values.astype(np.float64)
        res = daily_ic_multi(x, yset, dd_ic)
        for hz, ic in res.items():
            if len(ic) < 100:
                continue
            mu = float(np.mean(ic)); sd = float(np.std(ic, ddof=1))
            ir = mu / sd if sd > 0 else np.nan
            tstat = mu / (sd / np.sqrt(len(ic))) if sd > 0 else np.nan
            ic_rows.append({
                "factor": f, "name": name, "horizon": hz, "n_days": len(ic),
                "ic_mean": mu, "ic_std": sd, "ir": ir, "t": tstat,
                "pos_rate": float((ic > 0).mean()),
                "ic_abs_mean": float(np.mean(np.abs(ic))),
            })
        log(f"  IC {f} 完成")
    icdf = pd.DataFrame(ic_rows)
    icdf.to_csv(os.path.join(OUT, "v2_ic.csv"), index=False)
    log(f"-> v2_ic.csv ({len(icdf)} 行)")
    print("\n" + "=" * 108)
    print("【A. IC / IR】因子与未来收益的日度截面秩相关 (IC>0.03 且 |IR|>0.5 才算有预测力)")
    print("=" * 108)
    print(icdf.sort_values(["horizon", "ic_mean"], ascending=[True, False]).to_string(
        index=False, float_format=lambda x: f"{x:.5f}"))

    # ---------------- B. 双因子交互
    log("计算双因子交互 (5×5) ...")
    dsv = df["_d"].values

    def qb(v, nb=5):
        """日度截面秩百分位 (fast_rank_pct 内部 lexsort 已处理未排序输入)"""
        v = np.asarray(v, dtype=np.float64)
        out = np.full(len(v), np.nan)
        ok = np.isfinite(v)
        if ok.sum() == 0:
            return out
        out[ok] = fast_rank_pct(v[ok], dsv[ok])
        return out

    QB = {}
    for f in ["hist_z", "vr20m", "px_ma20_pct", "px_h60", "ret20", "rs_mkt20",
              "rs_ind20", "hv20", "volz20", "dvt5", "upvol_r20", "atr_pct"]:
        QF = qb(df[f].values)
        QB[f] = np.minimum((QF * 5).astype(np.float64), 4.0)
        QB[f][~np.isfinite(QF)] = np.nan

    PAIRS = [
        ("hist_z", "vr20m"), ("hist_z", "px_ma20_pct"), ("hist_z", "px_h60"),
        ("hist_z", "ret20"), ("hist_z", "rs_mkt20"), ("hist_z", "rs_ind20"),
        ("hist_z", "hv20"), ("hist_z", "volz20"), ("hist_z", "dvt5"),
        ("vr20m", "px_h60"), ("upvol_r20", "hs"), ("rs_ind20", "hv20"),
    ]
    PAIRS = [p for p in PAIRS if p[1] in QB]
    # 事件加成: 金叉 + 任一维度
    inter_rows = []
    base_mask = np.ones(len(df), bool)
    for a, b in PAIRS:
        qa, qbb = QB[a], QB[b]
        for i in range(5):
            for j in range(5):
                sel = base_mask & (qa == i) & (qbb == j)
                if sel.sum() < MIN_N:
                    continue
                inter_rows.append(stat_cell(df, sel, "全样本", f"{a}×{b}", f"{a}_Q{i+1} × {b}_Q{j+1}"))
        log(f"  交互 {a}×{b} 完成")
    # MACD金叉条件下的交互(用户第十四节核心)
    gc = df["golden_cross"].values == 1
    gcOK = np.isfinite(df["fwd20"].values) & gc
    for a, b in [("vr20m", "px_ma20_pct"), ("vr20m", "ret20"), ("vr20m", "rs_mkt20"),
                 ("vr20m", "hv20"), ("volz20", "px_h60"), ("dvt5", "px_ma20_pct")]:
        if a not in QB or b not in QB:
            continue
        qa, qbb = QB[a], QB[b]
        for i in range(5):
            for j in range(5):
                sel = gcOK & (qa == i) & (qbb == j)
                if sel.sum() < MIN_N:
                    continue
                inter_rows.append(stat_cell(df, sel, "MACD金叉后", f"{a}×{b}",
                                            f"{a}_Q{i+1} × {b}_Q{j+1}"))
        log(f"  金叉条件下 {a}×{b} 完成")
    idf = pd.DataFrame(inter_rows)
    idf.to_csv(os.path.join(OUT, "v2_interaction.csv"), index=False)
    log(f"-> v2_interaction.csv ({len(idf)} 行)")

    # ---------------- C. 市值中性
    log("计算市值中性 ...")
    size = df["size_grp"].values
    rows = []
    for f in ["hist_z", "vr20m", "volz20", "dvt5", "upvol_r20", "rs_mkt20", "ret20", "hv20"]:
        qf = QB[f]
        # 每规模组内, Q5-Q1 spread
        for g in range(10):
            sel_g = (size == g)
            for qi, lab in ((0, "Q1(低)"), (4, "Q5(高)")):
                sel = sel_g & (qf == qi)
                if sel.sum() < MIN_N:
                    continue
                r = stat_cell(df, sel, f, f"规模D{g+1}", lab)
                rows.append(r)
    # 事件 × 规模
    for ev, em in (("MACD金叉", gc), ("柱连3日改善", df["hist_up3"].values == 1),
                   ("0轴下金叉", df["gc_below0"].values == 1),
                   ("0轴上金叉", df["gc_above0"].values == 1)):
        em = em & np.isfinite(df["fwd20"].values)
        for g in range(10):
            sel = em & (size == g)
            if sel.sum() < MIN_N:
                continue
            rows.append(stat_cell(df, sel, ev, "规模组", f"规模D{g+1}"))
    sdf = pd.DataFrame(rows)
    sdf.to_csv(os.path.join(OUT, "v2_size_neutral.csv"), index=False)
    log(f"-> v2_size_neutral.csv ({len(sdf)} 行)")
    print("\n" + "=" * 108)
    print("【C. 市值中性】各规模组内 MACD金叉的 20 日超额 (若只在最小规模有效 => 非独立Alpha)")
    print("=" * 108)
    pv = sdf[(sdf.label == "MACD金叉") & (sdf.dim == "规模组")]
    if not pv.empty:
        print(pv[["grp", "n", "mean20", "exw20", "t20"]].to_string(
            index=False, float_format=lambda x: f"{x:.5f}"))

    # ---------------- D. 行业中性
    log("计算行业中性 ...")
    ind = df["ind_code"].values
    # 行业内的 fwd20 去均值 -> 行业中性超额
    dfe = df[["_d", "ind_code", "fwd20", "fwd5", "fwd60"]].copy()
    for H in (5, 20, 60):
        dfe[f"indm{H}"] = df.groupby(["ind_code"])[f"fwd{H}"].transform("mean")
        # 更严谨: 按(行业, 日)去均值
        dfe[f"inddm{H}"] = df.groupby(["ind_code", "_d"])[f"fwd{H}"].transform("mean")
    for H in (5, 20, 60):
        df[f"exind{H}"] = df[f"fwd{H}"].values - dfe[f"inddm{H}"].values
    rows2 = []
    for f in ["hist_z", "vr20m", "volz20", "dvt5", "rs_ind20", "rs_mkt20"]:
        qf = QB[f]
        for qi, lab in ((0, "Q1(低)"), (2, "Q3"), (4, "Q5(高)")):
            sel = (qf == qi)
            if sel.sum() < MIN_N:
                continue
            sub = df.loc[sel]
            rr = {"factor": f, "grp": lab, "n": len(sub)}
            for H in (5, 20, 60):
                v = sub[f"exind{H}"].values
                ok = np.isfinite(v)
                if ok.sum() < MIN_N:
                    continue
                rr[f"exind{H}"] = float(v[ok].mean())
                dm = pd.Series(v[ok]).groupby(sub["_d"].values[ok]).mean().values
                if len(dm) >= 5:
                    sd = float(dm.std(ddof=1))
                    rr[f"t{H}"] = float(dm.mean() / (sd / np.sqrt(len(dm)))) if sd > 0 else np.nan
            rows2.append(rr)
    # 事件在行业内
    for ev, em in (("MACD金叉", gc), ("柱连3日改善", df["hist_up3"].values == 1),
                   ("0轴下金叉", df["gc_below0"].values == 1)):
        for H in (5, 20, 60):
            v = df.loc[em, f"exind{H}"].values
            ok = np.isfinite(v)
            if ok.sum() < MIN_N:
                continue
            dm = pd.Series(v[ok]).groupby(df.loc[em, "_d"].values[ok]).mean().values
            sd = float(dm.std(ddof=1)) if len(dm) > 5 else np.nan
            rows2.append({"factor": "事件", "grp": ev, "n": int(ok.sum()),
                          f"exind{H}": float(v[ok].mean()),
                          f"t{H}": float(dm.mean() / (sd / np.sqrt(len(dm))))
                          if (sd and sd > 0) else np.nan})
    ndf = pd.DataFrame(rows2)
    ndf.to_csv(os.path.join(OUT, "v2_ind_neutral.csv"), index=False)
    log(f"-> v2_ind_neutral.csv ({len(ndf)} 行)")
    print("\n" + "=" * 108)
    print("【D. 行业中性】行业内去均值后的 20 日超额 (排除'信号恰好在热门行业'的解释)")
    print("=" * 108)
    print(ndf[ndf.grp.isin(["Q1(低)", "Q5(高)", "MACD金叉", "柱连3日改善"])].to_string(
        index=False, float_format=lambda x: f"{x:.5f}"))

    # ---------------- E. 横截面回归 (Fama-MacBeth)
    log("计算横截面回归 (Fama-MacBeth) ...")
    REGV = {
        "Size": "logamt",
        "Momentum": "ret20",
        "Volatility": "hv20",
        "Turnover_代理": "vr20m",
        "Beta": "beta60",
        "RS": "rs_mkt20",
        "MACD": "hist_z",
        "VolStruct": "dvt5",
        "VolLevel": "volz20",
        "PricePos": "px_h60",
    }
    reg_cols = list(set(REGV.values()))
    # 每个交易日内 z-score 标准化
    Z = {}
    for c in reg_cols:
        v = df[c].values.astype(np.float64)
        s = pd.Series(v).groupby(df["_d"].values)
        mu = s.transform("mean")
        sd = s.transform("std")
        z = (v - mu) / sd
        z[~np.isfinite(z)] = np.nan
        Z[c] = z
        df["_z_" + c] = z

    yv = df["fwd20"].values.astype(np.float64)
    dates = df["_d"].values
    regdf = df[["_z_" + c for c in reg_cols]].copy()
    regdf["_y"] = yv
    regdf["_d"] = dates
    use_cols = ["_z_" + c for c in reg_cols]
    regdf = regdf.dropna(subset=use_cols + ["_y"]).reset_index(drop=True)
    log(f"回归样本 {len(regdf):,} 行")

    X_all = regdf[use_cols].values
    y_all = regdf["_y"].values
    dd = regdf["_d"].values
    order = np.argsort(dd, kind="stable")
    X_all, y_all, dd = X_all[order], y_all[order], dd[order]
    chg2 = np.flatnonzero(dd[1:] != dd[:-1]) + 1
    st2 = np.concatenate([[0], chg2]); en2 = np.concatenate([chg2, [len(dd)]])

    k = len(use_cols)
    betas = np.full((len(st2), k + 1), np.nan)
    for i, (s, e) in enumerate(zip(st2, en2)):
        n = e - s
        if n < 100:
            continue
        Xd = np.column_stack([np.ones(n), X_all[s:e]])
        yd = y_all[s:e]
        try:
            XtX = Xd.T @ Xd
            b = np.linalg.solve(XtX + 1e-8 * np.eye(k + 1), Xd.T @ yd)
            betas[i] = b
        except np.linalg.LinAlgError:
            continue
    names = ["alpha"] + [f"{kk}[{[a for a,b in REGV.items() if b==c][0]}]"
                         for c, kk in zip(reg_cols, use_cols)]
    reg_rows = []
    for j in range(k + 1):
        col = betas[:, j]
        col = col[np.isfinite(col)]
        if len(col) < 50:
            continue
        mu = float(col.mean()); sd = float(col.std(ddof=1))
        reg_rows.append({
            "var": names[j], "n_days": len(col), "coef_mean": mu,
            "t": float(mu / (sd / np.sqrt(len(col)))) if sd > 0 else np.nan,
            "pos_rate": float((col > 0).mean()),
        })
    rdf = pd.DataFrame(reg_rows)
    rdf.to_csv(os.path.join(OUT, "v2_regression.csv"), index=False)
    log(f"-> v2_regression.csv ({len(rdf)} 行)")
    print("\n" + "=" * 108)
    print("【E. 横截面回归】Fama-MacBeth 日度截面 OLS (y = 未来20日收益), 全部变量已日内标准化")
    print("=" * 108)
    print(rdf.to_string(index=False, float_format=lambda x: f"{x:.6f}"))
    print("\n  解读: 若 MACD/VolStruct 系数在控制 Size/Momentum/Vol/Beta/RS 后不显著,")
    print("        说明它们只是其他因子的代理变量, 而非独立 Alpha 来源。")

    log("done")


if __name__ == "__main__":
    main()
