#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
35_factor_ic.py —— V3B 第九节 + 第十节

第九节：候选因子池（不给权重，全部实测）
  价格类  : ret5/10/20/40/60, px_ma20/60/120_pct
  趋势类  : MA5/MA20/MA60 斜率（自算）
  MACD类  : dif, dea, hist, hist_z, dhist1, dhist3, hist_upstreak
  量能类  : rvol20(VOL/VOL20), vt_5_20(VOL5/VOL20), vt_20_60, vr20m, dvt5
  相对强弱: rs_mkt20, rs_mkt60, rs_sz20
  波动类  : hv20, atr_pct
  市场类  : 市场Close/MA60-1, 市场MA20/MA60-1, Breadth, 市场HV20

第十节：单因子 IC / Rank IC / IC 均值 / ICIR / 分组收益 / Fama-MacBeth 多因子回归
  重点：控制其他变量之后，哪些因子仍然有效

产出：
  output/v3b_ic.csv         单因子 IC 汇总
  output/v3b_ic_decile.csv  单因子分组收益
  output/v3b_fmb.csv        Fama-MacBeth 多因子回归
  output/v3b_run_35.log
"""
import os
import time
import numpy as np
import pandas as pd
import v3b_lib as L

HZ = [5, 10, 20]
LOGF = open(os.path.join(L.OUT, "v3b_run_35.log"), "w", encoding="utf-8")
_t0 = time.time()


def log(m):
    line = f"[{time.time()-_t0:7.1f}s] {m}"
    print(line, flush=True)
    LOGF.write(line + "\n")
    LOGF.flush()


def _rank_within(v, d, nd):
    """块内平均秩（1 起）。

    ⚠️⚠️ 必须用 **双键排序** `np.lexsort((v, d))`（主键块号 d，次键值 v），
        使同一块的元素在排序后**相邻**，才能用「全局位置 − 块起点」得到块内序号。

    历史 bug（已修）：曾用单键 `np.argsort(v, kind="stable")`。
        单键排序只保证全局按 v 升序，**同一块的元素分散在各处**，
        此时 `pos = arange(n) - off[d]` 会算出**负数**（实测 −5584），
        得到的秩与 pandas 相差 |diff| 达 5.8e3，而小规模测试恰好因块内 v 单调
        而看不出问题 —— 典型的「小样本通过、大样本崩坏」。
        验证方法：`maxdiff vs pandas.Series(v).groupby(d).rank(method="average")`
        必须为 0。单键实现会给出 5.8e3 量级的偏差。

    ⚠️ `pos`/`sums` 必须用 int64：float64 在 n≈1e7 时对整段累加会产生漂移，
        使「全并列块的平均秩」偏离理论值 (n+1)/2，从而在常数因子上
        凭空造出伪 IC。
    """
    n = len(v)
    # 双键排序（主键 d，次键 v）：用「先按 v 稳定排，再按 d 稳定排」实现。
    # 语义与 np.lexsort((v, d)) 严格等价（已用 array_equal 验证），
    # 但实测 1e7 行快 2.8 倍：lexsort 10.09s vs 两次 stable argsort 3.57s。
    # ⚠️ 不能用 np.argsort(d*K+v) 这类复合键 —— 对大浮点值会因精度丢失而并列错乱。
    o1 = np.argsort(v, kind="stable")
    order = o1[np.argsort(d[o1], kind="stable")]
    ds = d[order]
    vs = v[order]
    cntg = np.bincount(ds, minlength=nd)                 # 长度 max(nd, max(ds)+1)
    nb = len(cntg)
    starts_b = np.zeros(nb + 1, np.int64)
    starts_b[1:] = np.cumsum(cntg)
    pos = np.arange(n, dtype=np.int64) - starts_b[ds]   # 块内 0 起序号（整数）
    newrun = np.empty(n, bool)
    newrun[0] = True
    if n > 1:
        newrun[1:] = (ds[1:] != ds[:-1]) | (vs[1:] != vs[:-1])
    run_id = np.cumsum(newrun) - 1                       # 0 起段编号，长度 n
    nrun = int(run_id[-1]) + 1
    idx_first = np.flatnonzero(newrun)                   # 每段首元素的全局位
    # 段内 pos 之和：段首位置已知，用 reduceat 对 pos 做整数分段求和（O(n) 且精确）
    run_sum = np.add.reduceat(pos, idx_first)            # int64 精确
    run_cnt = np.diff(np.append(idx_first, n)).astype(np.int64)
    avg = run_sum.astype(np.float64) / run_cnt.astype(np.float64) + 1.0
    out = np.empty(n, np.float64)
    out[order] = avg[run_id]
    return out


def ic_from_ranks(rf, d, nd, ry, cnt):
    """给定「因子的块内秩 rf」与「y 的块内秩 ry」，算逐日 Rank IC。

    ⚠️ 性能关键：`rf` 由调用方**按因子缓存一次**，供 3 个持有期复用。
        若每次都在此函数内重算 `_rank_within(f, ...)`，等于对 1000 万行
        做 3 次双键排序（每次 ~4s）→ 96 次调用累积到 1 小时以上。
        解耦后总排序次数 = 因子数（32），而非「因子数 × 持有期数」（96）。

    ⚠️ 护栏：要求当日因子有效秩的方差足够大，否则该日 IC 置 nan。
        横截面常数因子（市场级 Breadth / 市场距MA60 / 市场HV20）块内秩恒为
        (n+1)/2，残差应为 0；若排序或累加有浮点误差，会造出伪 IC 把一个
        零信息因子顶到榜首（实测曾出现 +0.0869 / t+5.52 的假榜首）。
    """
    sf = np.bincount(d, weights=rf, minlength=nd)
    sy = np.bincount(d, weights=ry, minlength=nd)
    mf = sf / np.maximum(cnt, 1)
    my = sy / np.maximum(cnt, 1)
    a = rf - mf[d]
    b = ry - my[d]
    sxy = np.bincount(d, weights=a * b, minlength=nd)
    sxx = np.bincount(d, weights=a * a, minlength=nd)
    syy = np.bincount(d, weights=b * b, minlength=nd)
    with np.errstate(invalid="ignore", divide="ignore"):
        ic = sxy / np.sqrt(sxx * syy)
    sxx_var = sxx / np.maximum(cnt, 1)
    v = (cnt >= 50) & (sxx_var >= 1.0) & np.isfinite(ic)
    return ic[v]


def daily_rank_ic_pre(f, d, nd, ry, cnt):
    """兼容旧签名：内部先算 rf（不推荐在大循环里用，会重复排序）"""
    return ic_from_ranks(_rank_within(f, d, nd), d, nd, ry, cnt)


def daily_rank_ic(f, y, day_idx, min_n=50):
    """逐日 Spearman Rank IC（单次调用版，供外部使用）"""
    ok = np.isfinite(f) & np.isfinite(y)
    if ok.sum() < min_n:
        return np.array([])
    d = day_idx[ok]
    nd = int(day_idx.max()) + 1
    cnt = np.bincount(d, minlength=nd).astype(np.float64)
    ry = _rank_within(np.asarray(y[ok], np.float64), d, nd)
    return daily_rank_ic_pre(np.asarray(f[ok], np.float64), d, nd, ry, cnt)


def main():
    cols = ["ret5", "ret10", "ret20", "ret40", "ret60",
            "px_ma20_pct", "px_ma60_pct", "px_ma120_pct",
            "dif", "dea", "hist", "hist_z", "dhist1", "dhist3", "hist_upstreak",
            "rvol20", "vr20m", "vt_5_20", "vt_20_60", "dvt5",
            "rs_mkt20", "rs_mkt60", "rs_sz20",
            "hv20", "atr_pct", "close_price", "open_price", "size_grp"] + \
           [f"fwd{H}" for H in HZ]
    df = L.load_clean(cols)
    C = L.ctx(df)
    log(f"clean {C['n']:,} 行 / {len(C['starts']):,} 只 / {C['n_days']:,} 交易日")

    oret, oret_sig = L.oret_from(df, C)
    mk_d = L.market_oret(oret, C)
    FWD = {H: df[f"fwd{H}"].values.astype(np.float64) for H in HZ}

    cl = df["close_price"].values.astype(np.float64)
    ma5 = L.roll_mean(cl, C, 5)
    ma20 = L.roll_mean(cl, C, 20)
    ma60 = L.roll_mean(cl, C, 60)
    # 斜率：MA 相对前一日的变化率
    sl5 = ma5 / L.roll_shift_mean(cl, C, 5, 1) - 1.0
    sl20 = ma20 / L.roll_shift_mean(cl, C, 20, 1) - 1.0
    sl60 = ma60 / L.roll_shift_mean(cl, C, 60, 1) - 1.0

    # 市场因子（自建）
    nav = np.cumprod(1.0 + np.nan_to_num(mk_d))
    mma20 = pd.Series(nav).rolling(20).mean().values
    mma60 = pd.Series(nav).rolling(60).mean().values
    m_above60 = np.where(np.isfinite(mma60), nav / mma60 - 1.0, np.nan)
    m_ma20v60 = np.where(np.isfinite(mma60) & (mma60 > 0), mma20 / mma60 - 1.0, np.nan)
    mhv20 = pd.Series(np.nan_to_num(mk_d)).rolling(20).std().values * np.sqrt(252)
    up = (oret > 0)
    dd = C["day_idx"]
    br_sum = np.bincount(dd[up & np.isfinite(oret)], minlength=C["n_days"])
    br_cnt = np.bincount(dd[np.isfinite(oret)], minlength=C["n_days"])
    breadth = np.where(br_cnt > 0, br_sum / np.maximum(br_cnt, 1), np.nan)

    D = C["day_idx"]
    FACTORS = [
        ("ret5", "过去5日涨幅", df["ret5"].values.astype(np.float64), "价格"),
        ("ret10", "过去10日涨幅", df["ret10"].values.astype(np.float64), "价格"),
        ("ret20", "过去20日涨幅", df["ret20"].values.astype(np.float64), "价格"),
        ("ret40", "过去40日涨幅", df["ret40"].values.astype(np.float64), "价格"),
        ("ret60", "过去60日涨幅", df["ret60"].values.astype(np.float64), "价格"),
        ("px_ma20_pct", "距MA20", df["px_ma20_pct"].values.astype(np.float64), "价格"),
        ("px_ma60_pct", "距MA60", df["px_ma60_pct"].values.astype(np.float64), "价格"),
        ("px_ma120_pct", "距MA120", df["px_ma120_pct"].values.astype(np.float64), "价格"),
        ("sl5", "MA5斜率", sl5, "趋势"),
        ("sl20", "MA20斜率", sl20, "趋势"),
        ("sl60", "MA60斜率", sl60, "趋势"),
        ("dif", "DIF", df["dif"].values.astype(np.float64), "MACD"),
        ("dea", "DEA", df["dea"].values.astype(np.float64), "MACD"),
        ("hist", "MACD柱", df["hist"].values.astype(np.float64), "MACD"),
        ("hist_z", "MACD柱Z", df["hist_z"].values.astype(np.float64), "MACD"),
        ("dhist1", "MACD柱1日变化", df["dhist1"].values.astype(np.float64), "MACD"),
        ("dhist3", "MACD柱3日变化", df["dhist3"].values.astype(np.float64), "MACD"),
        ("hist_upstreak", "MACD柱连续改善天数", df["hist_upstreak"].values.astype(np.float64), "MACD"),
        ("rvol20", "VOL/VOL20", df["rvol20"].values.astype(np.float64), "量能"),
        ("vr20m", "VR20", df["vr20m"].values.astype(np.float64), "量能"),
        ("vt_5_20", "VOL5/VOL20", df["vt_5_20"].values.astype(np.float64), "量能"),
        ("vt_20_60", "VOL20/VOL60", df["vt_20_60"].values.astype(np.float64), "量能"),
        ("dvt5", "5日量能趋势", df["dvt5"].values.astype(np.float64), "量能"),
        ("rs_mkt20", "相对市场20日强弱", df["rs_mkt20"].values.astype(np.float64), "相对强弱"),
        ("rs_mkt60", "相对市场60日强弱", df["rs_mkt60"].values.astype(np.float64), "相对强弱"),
        ("rs_sz20", "相对同规模20日强弱", df["rs_sz20"].values.astype(np.float64), "相对强弱"),
        ("hv20", "20日历史波动", df["hv20"].values.astype(np.float64), "波动"),
        ("atr_pct", "ATR占比", df["atr_pct"].values.astype(np.float64), "波动"),
        # ⚠️ 以下 4 个是「市场级」时间序列，在横截面上逐日恒定 → 无截面区分度。
        #    逐日截面 RankIC 按构造无定义（护栏会置 nan）；它们的正确用法是
        #    「市场环境过滤器」（见第 6 节 v3b_market.csv），不是横截面因子。
        ("m_above60", "市场距MA60", m_above60[D], "市场(无截面)"),
        ("m_ma20v60", "市场MA20/MA60-1", m_ma20v60[D], "市场(无截面)"),
        ("breadth", "Breadth", breadth[D], "市场(无截面)"),
        ("mhv20", "市场HV20", mhv20[D], "市场(无截面)"),
    ]
    log(f"因子池 {len(FACTORS)} 个")

    # ============================================== 第十节 A：单因子 IC
    log("\n第十节A：单因子 Rank IC")
    # 预计算各持有期 y 的块内秩（y 只依赖 H，可复用给全部 32 个因子）
    # ⚠️ ry 只在 y 有效行上计算，因子 f 的有效行是其子集，必须用 okf[yok] 取子集
    YR = {}
    for H in HZ:
        y = FWD[H]
        yok = np.isfinite(y)
        dH = D[yok]
        ndH = C["n_days"]
        cntH = np.bincount(dH, minlength=ndH).astype(np.float64)
        ry_all = _rank_within(y[yok].astype(np.float64), dH, ndH)
        YR[H] = (yok, ndH, cntH, ry_all)
        log(f"  y 块内秩预计算完成 H={H}")

    icrows = []
    for fn, cn, fv, grp in FACTORS:
        rec = dict(factor=fn, factor_cn=cn, group=grp)
        # ★ 每个因子只算一次块内秩，供 3 个持有期复用（避免 3 倍重复排序）
        fok = np.isfinite(fv)
        rf_all = _rank_within(fv[fok].astype(np.float64), D[fok], C["n_days"])
        rf_full = np.full(len(fv), np.nan, np.float64)
        rf_full[fok] = rf_all
        for H in HZ:
            yok, ndH, cntH, ry_all = YR[H]
            okf = fok & yok
            if okf.sum() < 50:
                continue
            ry = ry_all[okf[yok]]
            rf = rf_full[okf]                    # 复用已算好的因子秩
            ic = ic_from_ranks(rf, D[okf], ndH, ry, cntH)
            if len(ic) == 0:
                continue
            sd = ic.std(ddof=1)
            rec[f"ic{H}"] = float(ic.mean())
            rec[f"icir{H}"] = float(ic.mean() / sd) if sd > 0 else np.nan
            rec[f"ic_pos{H}"] = float((ic > 0).mean())
            rec[f"n_day{H}"] = int(len(ic))
            rec[f"t{H}"] = float(ic.mean() / (sd / np.sqrt(len(ic)))) if sd > 0 else np.nan
        icrows.append(rec)
        log(f"  IC 完成 {cn}")
    icdf = pd.DataFrame(icrows)
    icdf.to_csv(os.path.join(L.OUT, "v3b_ic.csv"), index=False)
    log(f"-> v3b_ic.csv ({len(icdf)} 行)")

    log("\n摘要 9 —— 单因子 IC（H=20）")
    log(f"    {'因子':<22}{'类别':<8}{'IC':>9}{'ICIR':>8}{'t':>8}{'IC>0占比':>10}")
    for r in icdf.sort_values("ic20", key=lambda s: s.abs(), ascending=False).itertuples():
        log(f"    {r.factor_cn:<22}{r.group:<8}{r.ic20:>+9.4f}{r.icir20:>8.3f}"
            f"{r.t20:>8.2f}{r.ic_pos20*100:>9.1f}%")

    # ============================================== 第十节 B：分组收益
    log("\n第十节B：单因子分组收益（十分位，H=20 超额）")
    drows = []
    CH20 = L.chain_fwd(mk_d, C, 20)
    for fn, cn, fv, grp in FACTORS:
        b = L.decile(fv, C, 10)
        for k in range(1, 11):
            m = np.isfinite(b) & (b == k)
            res = L.exw_t(FWD[20][m], CH20[D][m], D[m])
            if res:
                drows.append(dict(factor=fn, factor_cn=cn, group=grp, decile=k,
                                  n=res["n"], exw=res["exw"], t=res["t"]))
        log(f"  分组完成 {cn}")
    ddf = pd.DataFrame(drows)
    ddf.to_csv(os.path.join(L.OUT, "v3b_ic_decile.csv"), index=False)
    log(f"-> v3b_ic_decile.csv ({len(ddf)} 行)")

    log("\n摘要 9b —— 各因子 D1 / D5 / D10 超额（H=20）")
    log(f"    {'因子':<22}{'D1':>11}{'D5':>11}{'D10':>11}{'形状':>10}")
    for fn, cn, fv, grp in FACTORS:
        s = ddf[ddf.factor == fn].sort_values("decile")
        if len(s) < 10:
            continue
        v = s.exw.values
        shape = "单调递减" if v[0] > v[-1] and v[0] == v.max() else \
                ("单调递增" if v[0] < v[-1] and v[-1] == v.max() else
                 ("倒U(中位峰)" if v.argmax() in (3, 4, 5, 6) else "其他"))
        log(f"    {cn:<22}{v[0]*100:>+10.3f}%{v[4]*100:>+10.3f}%{v[9]*100:>+10.3f}%{shape:>10}")

    # ============================================== 第十节 C：Fama-MacBeth
    log("\n第十节C：Fama-MacBeth 多因子横截面回归")
    # 只用「超跌子样本」（ret20 D2~D6）内做，因为这才是研究对象
    b20 = L.decile(df["ret20"].values.astype(np.float64), C, 10)
    base_S = np.isfinite(b20) & (b20 >= 2) & (b20 <= 6)

    # ⚠️ 共线性处理：
    #   rs_mkt20 = ret20 − mkt_r20，而 mkt_r20 在横截面上是常数 → ret20 与 rs_mkt20 完全共线，
    #   同时放入会让 lstsq 给出 ±5e5 量级的爆炸系数（实测）。
    #   同理 rs_mkt60 ≡ ret60（差一个当日常数）。故只保留 rs_sz20（相对同规模，非同义）。
    FMB_F = [
        ("ret20", "过去20日涨幅"), ("ret60", "过去60日涨幅"),
        ("px_ma60_pct", "距MA60"), ("px_ma120_pct", "距MA120"),
        ("sl20", "MA20斜率"), ("sl60", "MA60斜率"),
        ("hist_z", "MACD柱Z"), ("dhist3", "MACD柱3日变化"),
        ("hist_upstreak", "MACD柱连续改善天数"),
        ("rvol20", "VOL/VOL20"), ("vt_5_20", "VOL5/VOL20"),
        ("rs_sz20", "相对同规模20日强弱"),
        ("hv20", "20日历史波动"),
    ]
    names = [n for _, n in FMB_F]
    fmap = {fn: fv for fn, cn, fv, grp in FACTORS}
    Xall = np.column_stack([fmap[fn] for fn, _ in FMB_F])

    nd = C["n_days"]
    coefs = {H: [] for H in HZ}
    for t in range(nd):
        sel = base_S & (D == t)
        idx = np.flatnonzero(sel)
        if len(idx) < 60:
            continue
        X = Xall[idx]
        good = np.isfinite(X).all(axis=1)
        for H in HZ:
            y = FWD[H][idx]
            good2 = good & np.isfinite(y)
            if good2.sum() < 60:
                continue
            Xs = X[good2]; ys = y[good2]
            # 截面标准化
            Xs = (Xs - Xs.mean(0)) / np.where(Xs.std(0) > 0, Xs.std(0), 1.0)
            ys = (ys - ys.mean()) / (ys.std() if ys.std() > 0 else 1.0)
            # 岭正则（极小 λ），防止残余共线性导致系数爆炸
            XtX = Xs.T @ Xs + 1e-6 * np.eye(Xs.shape[1])
            Xty = Xs.T @ ys
            try:
                beta = np.linalg.solve(XtX, Xty)
            except np.linalg.LinAlgError:
                continue
            coefs[H].append(beta)
    frows = []
    for H in HZ:
        A = np.array(coefs[H])
        if len(A) < 30:
            continue
        mu = A.mean(0)
        se = A.std(0, ddof=1) / np.sqrt(len(A))
        for i, nm in enumerate(names):
            frows.append(dict(H=H, factor=FMB_F[i][0], factor_cn=nm,
                              coef=float(mu[i]), se=float(se[i]),
                              t=float(mu[i] / se[i]) if se[i] > 0 else np.nan,
                              n_day=len(A)))
    fdf = pd.DataFrame(frows)
    fdf.to_csv(os.path.join(L.OUT, "v3b_fmb.csv"), index=False)
    log(f"-> v3b_fmb.csv ({len(fdf)} 行)")

    log("\n摘要 10 —— Fama-MacBeth（超跌子样本内，H=20；截面标准化系数）")
    log(f"    {'因子':<22}{'标准化系数':>12}{'t':>8}")
    for r in fdf[fdf.H == 20].sort_values("coef", key=lambda s: s.abs(),
                                          ascending=False).itertuples():
        log(f"    {r.factor_cn:<22}{r.coef:>+12.5f}{r.t:>8.2f}")

    log("完成")


if __name__ == "__main__":
    main()
