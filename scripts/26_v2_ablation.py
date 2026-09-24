#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
26_v2_ablation.py — V2 Ablation Test (用户第十四节)

完整模型:
    FULL = MACD金叉 + 过去20日涨幅<10% + Close接近MA20 + VOL5/VOL20<1.2 + RS20>0
逐个去掉一个条件, 观察日加权20日超额如何变化, 判断真正贡献 Alpha 的变量。

若去掉某条件后超额显著改善 -> 该条件是负贡献(应该去掉);
若去掉后超额崩溃 -> 该条件是核心贡献。

产出: output/v2_ablation.csv
"""
import os
import time
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")

MIN_LIST = 120
t0 = time.time()


def log(m):
    print(f"[{time.time()-t0:7.1f}s] {m}", flush=True)


def exw_t(ret, mk, dates, min_n=30):
    ok = np.isfinite(ret) & np.isfinite(mk)
    if ok.sum() < min_n:
        return np.nan, np.nan, int(ok.sum()), np.nan, np.nan
    e = ret[ok] - mk[ok]
    dm = pd.Series(e).groupby(dates[ok]).mean().values
    if len(dm) < 5:
        return np.nan, np.nan, int(ok.sum()), np.nan, np.nan
    sd = float(dm.std(ddof=1))
    t = float(dm.mean() / (sd / np.sqrt(len(dm)))) if sd > 0 else np.nan
    # Bootstrap CI
    rng = np.random.default_rng(20260923)
    idx = rng.integers(0, len(dm), size=(300, len(dm)))
    mu = dm[idx].mean(axis=1)
    return float(dm.mean()), t, int(ok.sum()), float(np.percentile(mu, 2.5)), float(np.percentile(mu, 97.5))


def main():
    cols = ["thscode", "date", "is_st_now", "days_since_list", "ret",
            "golden_cross", "gc_below0", "gc_above0", "hist_up3",
            "hist_z", "dhist1_z", "vr20m", "volz20", "vt_5_20", "dvt5",
            "upvol_r20", "px_ma20_pct", "px_h60", "ret20", "hv20",
            "rs_mkt20", "rs_ind20", "size_grp", "ind_code", "fwd20", "fwd60"]
    df = pd.read_parquet(os.path.join(PROC, "v2_panel.parquet"), columns=cols)
    log(f"读入 {df.shape[0]:,} 行")

    m = (~df["is_st_now"].fillna(True)) & (df["days_since_list"].fillna(0) >= MIN_LIST) \
        & df["ret"].notna() & df["fwd20"].notna() & df["fwd60"].notna()
    df = df.loc[m].reset_index(drop=True)
    d = df["date"].values
    log(f"clean {len(df):,} 行")

    mk20 = pd.Series(df["fwd20"].values).groupby(d).transform("mean").values
    mk60 = pd.Series(df["fwd60"].values).groupby(d).transform("mean").values

    gc = df["golden_cross"].values == 1
    ret20 = df["ret20"].values
    pma20 = df["px_ma20_pct"].values
    vt = df["vt_5_20"].values
    rsm = df["rs_mkt20"].values

    C = {
        "MACD": gc,
        "Momentum(ret20<10%)": ret20 < 0.10,
        "PricePos(|距MA20|<5%)": np.abs(pma20) < 0.05,
        "Volume(vt_5_20<1.2)": vt < 1.2,
        "RS(rs_mkt20>0)": rsm > 0,
    }
    FULL = np.ones(len(df), bool)
    for v in C.values():
        FULL &= v

    rows = []
    # ⚠️ 修复: 「去掉条件 k」= 其余所有条件的交集(不含 k), 而不是 FULL & ~k
    #    (后者对 MACD 必然为 0, 因为 FULL 已要求 MACD 成立)
    keys = list(C.keys())
    models = [("FULL 完整模型", FULL)]
    for k in keys:
        m = np.ones(len(df), bool)
        for k2 in keys:
            if k2 != k:
                m &= C[k2]
        models.append((f"- {k}", m))
    models.append(("仅 MACD金叉", gc))
    for k in keys:
        models.append((f"仅 {k}", C[k]))

    for name, mask in models:
        r = df["fwd20"].values[mask]
        ew, tt, nn, lo, hi = exw_t(r, mk20[mask], d[mask])
        ok = np.isfinite(r)
        rows.append({
            "model": name, "n": int(mask.sum()),
            "mean20": float(r[ok].mean()) if ok.sum() else np.nan,
            "median20": float(np.median(r[ok])) if ok.sum() else np.nan,
            "win20": float((r[ok] > 0).mean()) if ok.sum() else np.nan,
            "p10_20": float(np.percentile(r[ok], 10)) if ok.sum() else np.nan,
            "p90_20": float(np.percentile(r[ok], 90)) if ok.sum() else np.nan,
            "exw20": ew, "t20": tt, "lo20": lo, "hi20": hi,
        })
    adf = pd.DataFrame(rows)
    adf.to_csv(os.path.join(OUT, "v2_ablation.csv"), index=False)
    log(f"-> v2_ablation.csv ({len(adf)} 行)")

    pd.set_option("display.width", 220)
    print("\n" + "=" * 118)
    print("【Ablation Test】逐个去掉条件, 观察日加权20日超额变化")
    print("=" * 118)
    print(adf.to_string(index=False, float_format=lambda x: f"{x:.5f}"))
    print("\n  判读:")
    print("   * 若 FULL 的 exw20 本身<=0 且不显著 -> 组合无效, ablation 只是确认")
    print("   * 若去掉某条件后 exw20 明显改善 -> 该条件是负贡献")
    print("   * 若去掉某条件后 exw20 崩溃 -> 该条件是核心贡献")
    print("   * 若各单条件都不显著 -> 不存在稳定条件 Alpha")

    # 各条件的边际贡献(单条件 vs 全样本)
    base = exw_t(df["fwd20"].values, mk20, d)[0]
    print(f"\n  全样本基准 exw20 = {base*100:+.4f}%")
    for k, v in C.items():
        e = exw_t(df["fwd20"].values[v], mk20[v], d[v])[0]
        print(f"    {k:<28} 单条件 exw20 = {e*100:+.4f}%  (相对全样本 {((e-base)*100):+.4f}pp)")

    log("done")


if __name__ == "__main__":
    main()
