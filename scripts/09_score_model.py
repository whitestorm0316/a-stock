#!/usr/bin/env python3
"""
09_score_model.py — 组合评分模型与单调性检验

评分维度 (总分 0-100):
  MACD   0-30 : MACD柱位置/斜率/金叉
  量能   0-25 : VolumeRatio / VOL5-VOL20
  趋势   0-20 : Close vs MA20/MA60, MA20 vs MA60
  突破   0-15 : 距20/60日高点
  回撤   0-10 : 距MA20的位置

关键: 不假设高分一定好, 用数据检验分数与未来收益是否单调。
      若单调性不成立, 明确给出"不应使用该评分模型"的结论。
"""
import os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")
HORIZONS = [5, 10, 20, 60]


def _block_bounds(codes):
    """面板按 thscode 分块连续, 返回每块 [start, end) 边界"""
    chg = np.flatnonzero(codes[1:] != codes[:-1]) + 1
    return np.concatenate([[0], chg]), np.concatenate([chg, [len(codes)]])


def _shift_blocked(x, starts, ends, k):
    """分块 shift(k): k>0 取历史, k<0 取未来; 越界为 nan"""
    out = np.full_like(x, np.nan)
    for s, e in zip(starts, ends):
        n = e - s
        if k >= 0:
            if n > k:
                out[s + k:e] = x[s:e - k]
        else:
            m = -k
            if n > m:
                out[s:e - m] = x[s + m:e]
    return out


def main():
    print("loading ...")
    cols = ["thscode", "date", "close_price", "open_price", "turnover",
            "hist", "hist_prev", "hist_slope", "dif", "dea",
            "vol_ratio", "vol5_vol20", "ma5", "ma20", "ma60", "ma120",
            "px_ma20_pct", "dist_high20", "dist_high60", "dist_low20",
            "days_since_list", "is_st_now", "board"]
    d = pd.read_parquet(os.path.join(PROC, "panel.parquet"), columns=cols)
    d = d[(d.days_since_list >= 120) & (~d.is_st_now)
          & (d.turnover >= 20_000_000) & np.isfinite(d.close_price)].copy()
    d = d.sort_values(["thscode", "date"]).reset_index(drop=True)
    print(f"  tradable: {len(d):,}")

    # 未来收益必须按"股票分块内位置"对齐: T+1 开盘买入 → T+1+H 开盘卖出
    codes = d["thscode"].values
    starts, ends = _block_bounds(codes)
    op = d["open_price"].values.astype(np.float64)
    o1 = _shift_blocked(op, starts, ends, -1)
    for H in HORIZONS:
        d[f"r{H}"] = _shift_blocked(op, starts, ends, -(H + 1)) / o1 - 1.0

    c = d["close_price"].values
    ma20 = d["ma20"].values
    ma60 = d["ma60"].values
    ma120 = d["ma120"].values
    hist = d["hist"].values
    hp = d["hist_prev"].values
    hslope = d["hist_slope"].values
    dif = d["dif"].values
    dea = d["dea"].values
    vr = d["vol_ratio"].values
    v520 = d["vol5_vol20"].values
    dh20 = d["dist_high20"].values
    dh60 = d["dist_high60"].values
    dl20 = d["dist_low20"].values

    # ================= 打分 =================
    def clip01(x, lo, hi):
        return np.clip((x - lo) / (hi - lo), 0, 1)

    # MACD 0-30
    macd_score = (
        np.where(hist > 0, 10, 0)                                   # 柱在零轴上方
        + 10 * clip01(hslope, 0, np.nanpercentile(hslope, 90))      # 斜率
        + 10 * np.where((dif > dea), 1, 0)                          # DIF>DEA
    )
    # 量能 0-25
    vol_score = (
        15 * clip01(vr, 0.8, 2.0)                                   # 当日量比
        + 10 * clip01(v520, 0.8, 1.5)                               # 5日/20日量能
    )
    # 趋势 0-20
    trend_score = (
        7 * np.where(c > ma20, 1, 0)
        + 7 * np.where(c > ma60, 1, 0)
        + 6 * np.where(ma20 > ma60, 1, 0)
    )
    # 突破 0-15
    brk_score = (
        10 * clip01(dh20, -0.15, 0) + 5 * clip01(dh60, -0.25, 0)
    )
    # 回撤位置 0-10 (越接近MA20越好)
    dd_score = 10 * (1 - clip01(np.abs(d["px_ma20_pct"].values), 0, 0.15))

    d["score_macd"] = macd_score
    d["score_vol"] = vol_score
    d["score_trend"] = trend_score
    d["score_brk"] = brk_score
    d["score_dd"] = dd_score
    d["score"] = (macd_score + vol_score + trend_score + brk_score + dd_score)

    print(f"\n  score 分布: 均值{d['score'].mean():.1f} 中位{d['score'].median():.1f} "
          f"标准差{d['score'].std():.1f}")
    print(f"  分位: 10%={d['score'].quantile(.1):.0f} 25%={d['score'].quantile(.25):.0f} "
          f"50%={d['score'].quantile(.5):.0f} 75%={d['score'].quantile(.75):.0f} "
          f"90%={d['score'].quantile(.9):.0f}")

    # ================= 单调性检验 =================
    print("\n" + "=" * 96)
    print("单调性检验: 按分数分组, 观察未来收益 (T+1开盘买入→T+1+H开盘卖出)")
    print("=" * 96)

    # 市场调整
    for H in HORIZONS:
        d[f"r{H}_ex"] = d[f"r{H}"] - d.groupby("date")[f"r{H}"].transform("mean")

    bins = [0, 20, 30, 40, 50, 60, 70, 80, 90, 101]
    d["score_bin"] = pd.cut(d["score"], bins=bins, right=False)
    rows = []
    for b, sub in d.groupby("score_bin", observed=True):
        rec = {"score_bin": str(b), "n": len(sub),
               "score_mean": sub["score"].mean()}
        for H in HORIZONS:
            rec[f"abs{H}"] = sub[f"r{H}"].mean()
            rec[f"ex{H}"] = sub[f"r{H}_ex"].mean()
        rows.append(rec)
    sb = pd.DataFrame(rows)

    print("\n--- 绝对收益 (%) ---")
    disp = sb[["score_bin", "n", "score_mean"] + [f"abs{H}" for H in HORIZONS]].copy()
    for H in HORIZONS:
        disp[f"abs{H}"] = (disp[f"abs{H}"] * 100).round(3)
    disp["score_mean"] = disp["score_mean"].round(1)
    print(disp.to_string(index=False))

    print("\n--- 超额收益 vs 同日全市场 (%) ---")
    disp2 = sb[["score_bin", "n"] + [f"ex{H}" for H in HORIZONS]].copy()
    for H in HORIZONS:
        disp2[f"ex{H}"] = (disp2[f"ex{H}"] * 100).round(3)
    print(disp2.to_string(index=False))

    # 秩相关检验单调性
    print("\n--- 单调性检验 (Spearman 秩相关: 分数 vs 未来超额收益) ---")
    from scipy.stats import spearmanr
    mono = {}
    for H in HORIZONS:
        s = sb.dropna(subset=[f"ex{H}"])
        rho, p = spearmanr(s["score_mean"], s[f"ex{H}"])
        mono[H] = (rho, p)
        verdict = "单调" if (rho > 0.7 and p < 0.05) else (
            "弱单调" if rho > 0.4 else "不单调")
        print(f"  {H:>2}日: Spearman rho={rho:+.3f}, p={p:.4f}  -> {verdict}")

    print("\n--- 关键阈值检验: Score>=X 的表现 ---")
    rows = []
    for th in [30, 40, 50, 60, 70, 80, 90]:
        sub = d[d["score"] >= th]
        rec = {"threshold": f">={th}", "n": len(sub),
               "pct": len(sub) / len(d)}
        for H in HORIZONS:
            rec[f"ex{H}"] = sub[f"r{H}_ex"].mean()
            rec[f"win{H}"] = (sub[f"r{H}"] > 0).mean()
        rows.append(rec)
    th_df = pd.DataFrame(rows)
    disp3 = th_df.copy()
    for H in HORIZONS:
        disp3[f"ex{H}"] = (disp3[f"ex{H}"] * 100).round(3)
        disp3[f"win{H}"] = (disp3[f"win{H}"] * 100).round(1)
    disp3["pct"] = (disp3["pct"] * 100).round(1)
    print(disp3.to_string(index=False))

    print("\n--- 分数与信号强度交叉验证: 高分区是否优于低分区 ---")
    for th_lo, th_hi in [(50, 70), (60, 80), (70, 90)]:
        a = d[d["score"] >= th_hi]
        b = d[(d["score"] >= th_lo) & (d["score"] < th_hi)]
        print(f"  Score>={th_hi} vs [{th_lo},{th_hi}): "
              + "  ".join(
                  f"{H}d 高{(a[f'r{H}_ex'].mean())*100:+.3f}% vs 低{(b[f'r{H}_ex'].mean())*100:+.3f}%"
                  for H in [10, 20]))

    sb.to_csv(os.path.join(OUT, "score_monotonicity.csv"), index=False)
    th_df.to_csv(os.path.join(OUT, "score_threshold.csv"), index=False)

    # ================= 分期稳健性 =================
    print("\n" + "=" * 96)
    print("评分模型分期稳健性 (20日超额收益, %)")
    print("=" * 96)
    periods = {"2015-2018": ("2015-01-01", "2018-12-31"),
               "2019-2022": ("2019-01-01", "2022-12-31"),
               "2023-2024": ("2023-01-01", "2024-12-31"),
               "2025-2026": ("2025-01-01", "2026-12-31")}
    rows = []
    for th in [50, 60, 70, 80]:
        rec = {"threshold": f">={th}"}
        for pn, (lo, hi) in periods.items():
            m = ((d["date"] >= pd.Timestamp(lo)) & (d["date"] <= pd.Timestamp(hi))
                 & (d["score"] >= th))
            rec[pn] = d.loc[m, "r20_ex"].mean()
        rows.append(rec)
        print(f"  Score>={th}: " + "  ".join(
            f"{pn} {rec[pn]*100:+.3f}%" for pn in periods))
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "score_by_period.csv"), index=False)

    print("\n结论要点: 若 Spearman rho 不显著为正, 评分模型不应使用")


if __name__ == "__main__":
    main()
