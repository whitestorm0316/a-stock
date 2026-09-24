#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
22_v2_event_study.py — V2 事件研究(第一阶段: 只做 Event Study, 不构建策略)

对 9 类 MACD 事件, 按 19 个维度分组, 统计未来 1/5/10/20/40/60 日的
  Mean / Median / 胜率 / P10 / P25 / P75 / P90
以及市场调整超额(观测加权 + 日加权)、按日聚类 t 统计量、按日 Bootstrap 95% CI。

口径约定(严格防未来, 与 V1 一致):
  个股未来收益 fwd{H} = 开盘_{T+1+H} / 开盘_{T+1} - 1
  市场未来收益 mk{H}  = 同一交易日全部 clean 标的 fwd{H} 的等权均值
                        (与个股完全同口径, 避免收盘/开盘错配)
  日加权超额 = 先对交易日求超额均值, 再对交易日取平均 (与 t / Bootstrap 同口径)
  观测加权超额 = 对全部事件直接取平均 (受事件多的小盘股主导, 仅作对照)
  → 结论以【日加权】为准。

实现: 用 pandas groupby 向量化, 每个维度一次分组, 避免逐 cell 循环。

产出:
  output/v2_events_summary.csv   9 事件 + 基准的总体统计
  output/v2_events_by_dim.csv    19 维度 × 分组 × 6 期的长表(含分位数)
"""
import os
import time
import numpy as np
import pandas as pd
from scipy import stats

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

HZ = [1, 5, 10, 20, 40, 60]
PCTS = [0.10, 0.25, 0.75, 0.90]
MIN_LIST = 120
MIN_N = 200
NBOOT = 300
SEED = 20260923
t0 = time.time()


def log(m):
    print(f"[{time.time()-t0:7.1f}s] {m}", flush=True)


def qbin_by_date(vals, dates, nbins=5):
    """按交易日的截面分位分箱, 返回 0..nbins-1 的【整数箱号】

    ⚠️ 修复: 旧版直接返回 rank(pct=True) 的百分位浮点, 调用方却用
       `gv == gi` (gi=0..4) 比较, 导致每个交易日只有 rank=1.0 的那只
       股票能匹配到 gi=1, 每个维度只输出一行 Q2 —— 分组证据完全失效。
    """
    s = pd.Series(np.asarray(vals, dtype=np.float64))
    r = s.groupby(dates).rank(pct=True, method="average")
    p = r.values
    b = np.floor(p * nbins)
    b = np.minimum(b, nbins - 1.0)
    b[~np.isfinite(p)] = np.nan
    return b


def absbin(vals, edges, labels):
    """按绝对阈值分箱(用于用户指定的固定区间, 如过去20日收益 6 档)"""
    v = np.asarray(vals, dtype=np.float64)
    b = np.digitize(v, edges).astype(np.float64)
    b[~np.isfinite(v)] = np.nan
    return b


def clustered_t(daily_means):
    n = len(daily_means)
    if n < 5:
        return np.nan, np.nan, n
    m = float(np.mean(daily_means))
    sd = float(np.std(daily_means, ddof=1))
    if not np.isfinite(sd) or sd <= 0:
        return np.nan, m, n
    return m / (sd / np.sqrt(n)), m, n


def boot_ci(daily_means, n_boot=NBOOT, seed=SEED):
    n = len(daily_means)
    if n < 10:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    mu = daily_means[idx].mean(axis=1)
    return float(np.percentile(mu, 2.5)), float(np.percentile(mu, 97.5))


def stat_one(df, mask, label, dim, grp):
    """对一个子集算全套统计; 返回 dict

    用户第十八节明确要求: 每种信号必须输出
      Mean / Median / Win Rate / P10 / P25 / P50 / P75 / P90
    且覆盖 1/5/10/20/40/60 全部持有期 (旧版只算了 3 个持有期, 且漏了 P50)。
    """
    sub = df.loc[mask]
    n = len(sub)
    r = {"label": label, "dim": dim, "grp": grp, "n": n}
    if n < MIN_N:
        return r
    d = sub["_d"].values
    for H in HZ:
        v = sub[f"fwd{H}"].values
        ok = np.isfinite(v)
        if ok.sum() < MIN_N:
            continue
        vv = v[ok]
        r[f"mean{H}"] = float(vv.mean())
        r[f"median{H}"] = float(np.median(vv))
        r[f"win{H}"] = float((vv > 0).mean())
        q = np.percentile(vv, [10, 25, 50, 75, 90])
        r[f"p10_{H}"], r[f"p25_{H}"], r[f"p50_{H}"], r[f"p75_{H}"], r[f"p90_{H}"] = \
            [float(x) for x in q]
    # 超额 / t / CI (1/5/20/60)
    for H in (1, 5, 20, 60):
        ex = sub[f"fwd{H}"].values - sub[f"mk{H}"].values
        ok = np.isfinite(ex)
        if ok.sum() < MIN_N:
            continue
        r[f"ex{H}"] = float(ex[ok].mean())              # 观测加权
        tmp = pd.Series(ex[ok]).groupby(d[ok]).mean()   # 按日等权
        dm = tmp.values
        t, m, nd = clustered_t(dm)
        if np.isfinite(t):
            r[f"t{H}"] = float(t)
        r[f"exw{H}"] = float(m)                          # 日加权
        r[f"ndays{H}"] = int(nd)
        lo, hi = boot_ci(dm)
        r[f"lo{H}"], r[f"hi{H}"] = lo, hi
    return r


def main():
    cols = ["thscode", "date", "is_st_now", "days_since_list", "ret", "size_grp",
            "golden_cross", "death_cross", "hist_cross_up", "hist_up3", "hist_up5",
            "gc_above0", "gc_below0",
            "hist", "hist_z", "dhist1_z",
            "st_slowimp", "st_fastflip", "st_decay",
            "dif_pos", "dif_neg",
            "vr20m", "rvol10", "rvol20", "rvol60", "volz20", "volz_bin",
            "vt_5_20", "vt_10_20", "vt_20_60", "dvt5", "vchg1", "vchg5",
            "upvol_r10", "upvol_r20", "ud_vol20",
            "stA10", "stB10", "stC10", "stD10",
            "stA20", "stB20", "stC20", "stD20",
            "px_ma20_pct", "px_ma60_pct", "px_ma120_pct",
            "px_h20", "px_h60", "px_h120",
            "ret5", "ret20", "ret60", "atr_pct", "hv20",
            "rs_mkt5", "rs_mkt20", "rs_mkt60", "rs_ind20", "rs_sz20"] + [f"fwd{H}" for H in HZ]
    df = pd.read_parquet(os.path.join(PROC, "v2_panel.parquet"), columns=cols)
    log(f"读入 {df.shape[0]:,} 行")

    m = (~df["is_st_now"].fillna(True)) & (df["days_since_list"].fillna(0) >= MIN_LIST) \
        & df["fwd20"].notna() & df["ret"].notna() & df["fwd60"].notna()
    df = df.loc[m].reset_index(drop=True)
    df["_d"] = df["date"].values
    log(f"clean 样本 {len(df):,} 行 / {df['thscode'].nunique():,} 只 / "
        f"{df['date'].nunique():,} 个交易日")

    # 市场前向收益(同口径)
    for H in HZ:
        df[f"mk{H}"] = df.groupby("_d")[f"fwd{H}"].transform("mean")
    log("市场基准(前向, 同口径)完成")

    # ---------------- 分组维度定义
    log("计算截面分位分组...")
    G, LAB = {}, {}
    def add(name, vals, labs, nbins=5):
        G[name] = qbin_by_date(np.asarray(vals, dtype=np.float64), df["_d"], nbins)
        LAB[name] = labs

    def add_abs(name, vals, edges, labs):
        G[name] = absbin(vals, edges, labs)
        LAB[name] = labs

    def add_raw(name, vals, labs):
        G[name] = np.asarray(vals, dtype=np.float64)
        LAB[name] = labs

    Q5 = lambda: ["Q1(最低)", "Q2", "Q3", "Q4", "Q5(最高)"]
    add("VR20", df["vr20m"], Q5())
    add("RVOL20", df["rvol20"], Q5())
    add("RVOL10", df["rvol10"], Q5())
    add("RVOL60", df["rvol60"], Q5())
    add("VOLZ", df["volz20"], Q5())
    # 用户 2.5: Volume Z-Score 分箱 < -1 / -1~0 / 0~1 / 1~2 / >2
    # (volz_bin 已是 digitize 出来的离散 0..4, 不能再做分位分箱)
    add_raw("VOLZ_BIN", df["volz_bin"], ["Z<-1", "-1~0", "0~1", "1~2", "Z>2"])
    add("VT520", df["vt_5_20"], Q5())
    add("VT1020", df["vt_10_20"], Q5())
    add("VT2060", df["vt_20_60"], Q5())
    add("VT_CHG", df["dvt5"], Q5())
    add("VCHG1", df["vchg1"], Q5())
    add("VCHG5", df["vchg5"], Q5())
    add("UPVOL_R", df["upvol_r20"], Q5())
    add("UPVOL_R10", df["upvol_r10"], Q5())
    add("UD_VOL", df["ud_vol20"], Q5())
    add("PX_MA20", df["px_ma20_pct"], Q5())
    add("PX_MA60", df["px_ma60_pct"], Q5())
    add("PX_MA120", df["px_ma120_pct"], Q5())
    add("PX_H20", df["px_h20"], Q5())
    add("PX_H60", df["px_h60"], Q5())
    add("PX_H120", df["px_h120"], Q5())
    add("RET5", df["ret5"], Q5())
    # 用户第九节: 过去20日收益分 6 档 < -20% / -20~-10% / -10~0% / 0~10% / 10~20% / >20%
    add_abs("RET20_ABS", df["ret20"], [-0.20, -0.10, 0.0, 0.10, 0.20],
            ["<-20%", "-20~-10%", "-10~0%", "0~10%", "10~20%", ">20%"])
    add("RET20", df["ret20"], Q5())
    add("RET60", df["ret60"], Q5())
    add("ATR_PCT", df["atr_pct"], Q5())
    add("HV20", df["hv20"], Q5())
    add("RS_MKT5", df["rs_mkt5"], Q5())
    add("RS_MKT20", df["rs_mkt20"], Q5())
    add("RS_MKT60", df["rs_mkt60"], Q5())
    add("RS_IND20", df["rs_ind20"], Q5())
    add("RS_SZ20", df["rs_sz20"], Q5())
    add("HIST_Z", df["hist_z"], Q5())
    add("DHIST1_Z", df["dhist1_z"], Q5())
    # 用户第十五节: 市值分 10 组 (用 size_grp 原值 0..9, 不做分位分箱)
    add_raw("SIZE_D", df["size_grp"], [f"规模D{int(i)+1}" for i in range(10)])
    # Volume Structure 主导状态(过去10日中 A/B/C/D 计数最多者)
    ST = np.vstack([np.nan_to_num(df[f"st{c}10"].values, nan=-1.0) for c in "ABCD"])
    dom = np.argmax(ST, axis=0).astype(np.float64)
    dom[ST.max(axis=0) < 0] = np.nan
    G["VOL_STRUCT"] = dom
    LAB["VOL_STRUCT"] = ["A缩量涨为主", "B放量涨为主", "C缩量跌为主", "D放量跌为主"]
    # 用户第四节: 过去20日四状态计数最多者
    ST20 = np.vstack([np.nan_to_num(df[f"st{c}20"].values, nan=-1.0) for c in "ABCD"])
    dom20 = np.argmax(ST20, axis=0).astype(np.float64)
    dom20[ST20.max(axis=0) < 0] = np.nan
    G["VOL_STRUCT20"] = dom20
    LAB["VOL_STRUCT20"] = ["A20缩量涨", "B20放量涨", "C20缩量跌", "D20放量跌"]
    # 用户第七节: 金叉类型(下跌后/横盘后/上涨后) —— 用过去20日收益 + 距MA20 划分
    gc = df["golden_cross"].values == 1
    r20 = df["ret20"].values
    pma = df["px_ma20_pct"].values
    typ = np.full(len(df), np.nan)
    typ[gc & (r20 <= -0.05)] = 0            # A 下跌后金叉
    typ[gc & (r20 > -0.05) & (r20 < 0.05)] = 1   # B 横盘后金叉
    typ[gc & (r20 >= 0.05)] = 2             # C 上涨后金叉
    G["GC_TYPE"] = typ
    LAB["GC_TYPE"] = ["A下跌后金叉", "B横盘后金叉", "C上涨后金叉"]
    # 用户第十节: 低/中/高波动
    add_abs("HV20_3", df["hv20"], [0.25, 0.45], ["低波动", "中波动", "高波动"])
    # 用户第二节: MACD 绝对位置
    add_raw("MACD_POS", np.where(df["dif_pos"].values == 1, 0.0,
                                 np.where(df["dif_neg"].values == 1, 1.0, np.nan)),
            ["DIF>0", "DIF<0"])
    for k in G:
        df["_g_" + k] = G[k]
    log(f"分组维度 {len(G)} 个 完成")

    # ---------------- 事件
    EV = {
        "E1_MACD金叉": df["golden_cross"].values == 1,
        "E2_柱连3日改善": df["hist_up3"].values == 1,
        "E3_柱连5日改善": df["hist_up5"].values == 1,
        "E4_0轴下金叉": df["gc_below0"].values == 1,
        "E5_0轴上金叉": df["gc_above0"].values == 1,
        "E6_慢改善未翻红": df["st_slowimp"].values == 1,
        "E7_快速翻红": df["st_fastflip"].values == 1,
        "E8_高位衰减": df["st_decay"].values == 1,
        "E9_MACD死叉": df["death_cross"].values == 1,
    }
    for k, v in EV.items():
        log(f"  {k}: {int(np.nansum(v)):,}")

    # ---------------- 总体
    log("计算总体统计...")
    rows = [stat_one(df, np.ones(len(df), bool), "【基准】全样本", "-", "-")]
    for nm, mk in EV.items():
        rows.append(stat_one(df, np.asarray(mk), nm, "-", "-"))
    sumdf = pd.DataFrame(rows)
    sumdf.to_csv(os.path.join(OUT, "v2_events_summary.csv"), index=False)
    log(f"-> v2_events_summary.csv ({len(sumdf)} 行)")

    # ---------------- 按维度分组 (只对核心事件, 控制规模)
    # ⚠️ 性能: 旧版对每个 cell 做一次 10M 行布尔索引(1600 次) -> 跑不完。
    #    改为「事件子集 -> 按 cell 排序 -> 连续切片」的 numpy 实现。
    CORE_EV = ["E1_MACD金叉", "E2_柱连3日改善", "E3_柱连5日改善",
               "E4_0轴下金叉", "E5_0轴上金叉", "E7_快速翻红",
               "E8_高位衰减", "E9_MACD死叉"]
    DIMS = list(LAB.keys())
    HZ_EX = [1, 5, 20, 60]
    HI_EX = [HZ.index(h) for h in HZ_EX]
    log(f"按维度分组(向量化) — {len(CORE_EV)} 事件 × {len(DIMS)} 维度 ...")

    out = []
    for ev in CORE_EV:
        em = np.asarray(EV[ev])
        ei = np.flatnonzero(em)
        if len(ei) == 0:
            continue
        # 事件子集的 fwd / mk 矩阵 (n_ev, k)
        F = df.iloc[ei][[f"fwd{H}" for H in HZ]].values.astype(np.float64)
        M = df.iloc[ei][[f"mk{H}" for H in HZ_EX]].values.astype(np.float64)
        DD = df["_d"].values[ei]
        for dim in DIMS:
            labs = LAB[dim]
            cc = df["_g_" + dim].values[ei].astype(np.float64)
            cc = np.where(np.isfinite(cc), cc, -1.0).astype(np.int64)
            order = np.argsort(cc, kind="stable")
            ccs = cc[order]
            Fs, Ms, Ds = F[order], M[order], DD[order]
            bd = np.flatnonzero(np.concatenate([[True], ccs[1:] != ccs[:-1]]))
            bd = np.append(bd, len(ccs))
            for bi in range(len(bd) - 1):
                a, b = bd[bi], bd[bi + 1]
                code = int(ccs[a])
                if code < 0 or (b - a) < MIN_N or code >= len(labs):
                    continue
                r = {"label": ev, "dim": dim, "grp": labs[code], "n": b - a}
                for hi, H in enumerate(HZ):
                    v = Fs[a:b, hi]
                    ok = np.isfinite(v)
                    if ok.sum() < MIN_N:
                        continue
                    vv = v[ok]
                    r[f"mean{H}"] = float(vv.mean())
                    r[f"median{H}"] = float(np.median(vv))
                    r[f"win{H}"] = float((vv > 0).mean())
                    q = np.percentile(vv, [10, 25, 50, 75, 90])
                    (r[f"p10_{H}"], r[f"p25_{H}"], r[f"p50_{H}"],
                     r[f"p75_{H}"], r[f"p90_{H}"]) = [float(x) for x in q]
                for mi, (H, hi) in enumerate(zip(HZ_EX, HI_EX)):
                    e = Fs[a:b, hi] - Ms[a:b, mi]
                    ok = np.isfinite(e)
                    if ok.sum() < MIN_N:
                        continue
                    r[f"ex{H}"] = float(e[ok].mean())
                    dm = pd.Series(e[ok]).groupby(Ds[a:b][ok]).mean().values
                    t, m, nd = clustered_t(dm)
                    if np.isfinite(t):
                        r[f"t{H}"] = float(t)
                    r[f"exw{H}"] = float(m)
                    r[f"ndays{H}"] = int(nd)
                out.append(r)
        log(f"  {ev} 完成 ({len(out)} 行累计)")
    dimdf = pd.DataFrame(out)
    dimdf.to_csv(os.path.join(OUT, "v2_events_by_dim.csv"), index=False)
    log(f"-> v2_events_by_dim.csv ({len(dimdf)} 行)")

    # ---------------- 打印
    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 60)
    print("\n" + "=" * 110)
    print("【总体】9 类事件 + 基准   (exw20=日加权20日超额, 结论以它为准)")
    print("=" * 110)
    show = ["label", "n", "mean1", "mean5", "mean20", "mean60", "win20",
            "p10_20", "p90_20", "ex20", "exw20", "t20", "lo20", "hi20"]
    print(sumdf[[c for c in show if c in sumdf.columns]].to_string(
        index=False, float_format=lambda x: f"{x:.5f}"))

    print("\n" + "=" * 110)
    print("【E1 MACD金叉】各维度分组 · 20日绝对收益 / 日加权超额 / t")
    print("=" * 110)
    e1 = dimdf[dimdf["label"] == "E1_MACD金叉"]
    c2 = ["dim", "grp", "n", "mean20", "median20", "win20", "exw20", "t20"]
    print(e1[[c for c in c2 if c in e1.columns]].to_string(
        index=False, float_format=lambda x: f"{x:.5f}"))

    print("\n" + "=" * 110)
    print("【筛选】日加权20日超额 > 0 且 t > 1.0 的分组 (只列这些进入下一阶段)")
    print("=" * 110)
    if "exw20" not in dimdf.columns:
        print("  (无 exw20 列)")
    else:
        cand = dimdf[(dimdf["exw20"] > 0) & (dimdf["t20"] > 1.0) & (dimdf["n"] >= 500)]
        if cand.empty:
            print("  (无) —— 没有任何 (事件 × 维度分组) 组合满足 正超额 + t>1.0")
        else:
            print(cand[["label", "dim", "grp", "n", "mean20", "exw20", "t20"]]
                  .sort_values("exw20", ascending=False).to_string(
                      index=False, float_format=lambda x: f"{x:.5f}"))

    # 额外: 列出 日加权20日超额最高的 30 个分组(不做显著性门槛), 便于观察分布
    if "exw20" in dimdf.columns:
        print("\n" + "=" * 110)
        print("【参考】日加权20日超额 Top-30 分组 (不论显著性, 仅看谁最接近正超额)")
        print("=" * 110)
        top = dimdf.dropna(subset=["exw20"]).sort_values("exw20", ascending=False).head(30)
        print(top[["label", "dim", "grp", "n", "mean20", "median20", "win20", "exw20", "t20"]]
              .to_string(index=False, float_format=lambda x: f"{x:.5f}"))

    log("done")


if __name__ == "__main__":
    main()
