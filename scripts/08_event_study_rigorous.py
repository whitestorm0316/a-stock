#!/usr/bin/env python3
"""
08_event_study_rigorous.py — 严格事件研究 (核心统计检验)

方法学要点 (修正朴素事件研究的偏差):
  1. 同期市场调整: 信号在时间上聚集(牛市中信号密集), 必须减去"同一交易日全市场等权收益",
     否则牛市期间的信号会被误认为有正超额收益。
  2. 横截面标准化: 计算 (个股收益 - 当日全市场等权收益), 再做统计检验。
  3. 按日聚类的 t 统计量: 先按日求均值, 再对日均值序列做 t 检验, 处理横截面相关性。
  4. Bootstrap 置信区间: 按日重抽样。
  5. 样本期分层: 训练/验证/样本外分别统计。
"""
import os
import json
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")
HORIZONS = [1, 5, 10, 20, 60]
RNG = np.random.default_rng(20260923)


def load():
    cols = ["thscode", "date", "close_price", "open_price", "ret", "turnover",
            "hist", "hist_prev", "hist_cross_up", "hist_slope", "golden_cross",
            "death_cross", "vol_ratio", "vol5_vol20", "vol10_vol20", "vol5_vol60",
            "ma5", "ma20", "ma60", "dif", "dea", "px_ma20_pct",
            "dv_bull", "dv_bull_recent", "days_since_list", "is_st_now",
            "high20_prev", "dist_high20", "board"]
    p = pd.read_parquet(os.path.join(PROC, "panel.parquet"), columns=cols)
    return p


def add_fwd(df):
    """未来收益 (T+1 开盘买入 → T+1+H 开盘卖出, 可实现的持有期收益)"""
    df = df.sort_values(["thscode", "date"]).reset_index(drop=True)
    g = df.groupby("thscode", sort=False)["open_price"]
    o1 = g.shift(-1)
    for H in HORIZONS:
        df[f"r{H}"] = g.shift(-(H + 1)) / o1 - 1.0
    # 同时保留 close-to-close 版本用于对比
    gc = df.groupby("thscode", sort=False)["close_price"]
    for H in HORIZONS:
        df[f"c{H}"] = gc.shift(-H) / df["close_price"] - 1.0
    return df


def market_adj(df):
    """横截面市场调整: 减去同一交易日全市场等权收益"""
    for H in HORIZONS:
        for pre in ("r", "c"):
            col = f"{pre}{H}"
            mkt = df.groupby("date")[col].transform("mean")
            df[f"{col}_ex"] = df[col] - mkt
    return df


def daily_clustered_t(sub):
    """按日聚类的 t 统计量: 对每日横截面均值序列做 t 检验"""
    if len(sub) == 0:
        return np.nan, np.nan, 0
    dm = sub.groupby("date")["ex"].mean().dropna()
    if len(dm) < 3:
        return np.nan, np.nan, len(dm)
    t = dm.mean() / (dm.std(ddof=1) / np.sqrt(len(dm)))
    return float(t), float(dm.mean()), int(len(dm))


def boot_ci(sub, n_boot=400):
    """按日 bootstrap 置信区间"""
    if len(sub) == 0:
        return np.nan, np.nan
    dm = sub.groupby("date")["ex"].mean().dropna()
    if len(dm) < 10:
        return np.nan, np.nan
    v = dm.values
    idx = RNG.integers(0, len(v), size=(n_boot, len(v)))
    means = v[idx].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def analyze(d, mask, label, horizons=HORIZONS):
    """对给定信号 mask 做完整统计"""
    sub = d[mask]
    out = {"label": label, "n": int(len(sub))}
    if len(sub) == 0:
        return out
    for H in horizons:
        s = sub[["date", f"r{H}", f"r{H}_ex"]].dropna()
        s = s.rename(columns={f"r{H}_ex": "ex"})
        out[f"n{H}"] = len(s)
        if len(s) == 0:
            continue
        out[f"abs{H}"] = float(sub[f"r{H}"].mean())
        out[f"ex{H}"] = float(s["ex"].mean())
        t, dmean, nd = daily_clustered_t(s)
        out[f"t{H}"] = t
        # 日加权超额 = t 检验与 Bootstrap 所用的同一口径 (按交易日等权)
        out[f"exw{H}"] = dmean
        out[f"ndays{H}"] = nd
        lo, hi = boot_ci(s)
        out[f"lo{H}"] = lo
        out[f"hi{H}"] = hi
        out[f"win{H}"] = float((sub[f"r{H}"] > 0).mean())
    return out


def main():
    print("loading & preparing ...")
    d = load()
    d = d[(d.days_since_list >= 120) & (~d.is_st_now)
          & (d.turnover >= 20_000_000) & np.isfinite(d.close_price)].copy()
    print(f"  tradable: {len(d):,}")
    d = add_fwd(d)
    d = market_adj(d)
    print(f"  fwd returns & market adjustment done")

    vr = d["vol_ratio"].values
    v520 = d["vol5_vol20"].values
    hu = d["hist_cross_up"].values.astype(bool)
    gc = d["golden_cross"].values.astype(bool)
    dc = d["death_cross"].values.astype(bool)
    dvb = d["dv_bull"].values.astype(bool)
    dvbr = d["dv_bull_recent"].values.astype(bool)
    c = d["close_price"].values
    ma20 = d["ma20"].values
    ma60 = d["ma60"].values
    dif = d["dif"].values
    h20p = d["high20_prev"].values
    hs = d["hist_slope"].values

    events = [
        ("【基准】全市场可交易样本", np.ones(len(d), dtype=bool)),
        ("MACD翻红 (不限量能)", hu),
        ("MACD翻红 + 缩量 VR<0.8", hu & (vr < 0.8)),
        ("MACD翻红 + VR 0.8-1.1", hu & (vr >= 0.8) & (vr < 1.1)),
        ("MACD翻红 + VR 1.1-1.5", hu & (vr >= 1.1) & (vr < 1.5)),
        ("MACD翻红 + VR>=1.5", hu & (vr >= 1.5)),
        ("MACD翻红 + VR>1.1", hu & (vr > 1.1)),
        ("MACD翻红 + VR>1.2", hu & (vr > 1.2)),
        ("MACD翻红 + VR>2.0", hu & (vr > 2.0)),
        ("MACD金叉 + 放量 VR>1.1", gc & (vr > 1.1)),
        ("MACD金叉 + 缩量 VR<1.0", gc & (vr < 1.0)),
        ("MACD死叉", dc),
        ("底背离", dvb),
        ("底背离 + 缩量", dvb & (vr < 1.0)),
        ("底背离 + 放量 VR>1.1", dvb & (vr > 1.1)),
        ("底背离 + 翻红确认", dvbr & hu),
        ("MACD翻红+VR>1.1+Close>MA20", hu & (vr > 1.1) & (c > ma20)),
        ("... +MA20>MA60", hu & (vr > 1.1) & (c > ma20) & (ma20 > ma60)),
        ("... +DIF>0", hu & (vr > 1.1) & (c > ma20) & (ma20 > ma60) & (dif > 0)),
        ("... +突破20日高", hu & (vr > 1.1) & (c > ma20) & (ma20 > ma60)
         & (dif > 0) & (c > h20p)),
    ]

    print("\n" + "=" * 100)
    print("核心统计检验 (T+1开盘买入→T+1+H开盘卖出; ex = 相对同日全市场等权超额)")
    print("=" * 100)
    rows = [analyze(d, m, lab) for lab, m in events]
    r = pd.DataFrame(rows)

    print("\n--- 绝对收益 (%) ---")
    a = r[["label", "n"] + [f"abs{H}" for H in HORIZONS]].copy()
    for H in HORIZONS:
        a[f"abs{H}"] = (a[f"abs{H}"] * 100).round(3)
    print(a.to_string(index=False))

    print("\n--- 超额收益 vs 同日全市场 (%) ---")
    b = r[["label", "n"] + [f"ex{H}" for H in HORIZONS]].copy()
    for H in HORIZONS:
        b[f"ex{H}"] = (b[f"ex{H}"] * 100).round(3)
    print(b.to_string(index=False))

    print("\n--- 按日聚类 t 统计量 (|t|>2 视为显著) ---")
    t = r[["label"] + [f"t{H}" for H in HORIZONS]].copy()
    print(t.round(2).to_string(index=False))

    print("\n--- Bootstrap 95% 置信区间 (20日超额, %) ---")
    ci = r[["label", "ex20", "exw20", "lo20", "hi20", "ndays20"]].copy()
    for cc in ("ex20", "exw20", "lo20", "hi20"):
        ci[cc] = (ci[cc] * 100).round(3)
    ci["显著"] = np.where((ci["lo20"] > 0) | (ci["hi20"] < 0), "是", "否")
    ci = ci.rename(columns={"ex20": "ex20_观测加权", "exw20": "ex20_日加权"})
    print(ci.to_string(index=False))
    print("  注: t 值与 Bootstrap CI 均基于'日加权'口径; 二者必须与 ex20_日加权 一起解读。")

    r.to_csv(os.path.join(OUT, "rigorous_event_study.csv"), index=False)

    # ---------------- 分期检验 ----------------
    print("\n" + "=" * 100)
    print("分期检验 (20日超额收益, %) — 检验稳定性")
    print("=" * 100)
    periods = {"2015-2018": ("2015-01-01", "2018-12-31"),
               "2019-2022": ("2019-01-01", "2022-12-31"),
               "2023-2024": ("2023-01-01", "2024-12-31"),
               "2025-2026": ("2025-01-01", "2026-12-31")}
    key_events = [("MACD翻红", hu), ("MACD翻红+VR>1.1", hu & (vr > 1.1)),
                  ("MACD翻红+缩量", hu & (vr < 0.8)),
                  ("底背离+翻红", dvbr & hu)]
    rows = []
    for lab, m in key_events:
        rec = {"label": lab}
        for pn, (lo, hi) in periods.items():
            mm = m & (d["date"] >= pd.Timestamp(lo)).values & (d["date"] <= pd.Timestamp(hi)).values
            st = analyze(d, mm, lab, horizons=[20])
            rec[pn] = st.get("ex20", np.nan)
            rec[pn + "_t"] = st.get("t20", np.nan)
        rows.append(rec)
        print(f"  {lab}: " + "  ".join(
            f"{pn} {rec[pn]*100:+.3f}%(t={rec[pn+'_t']:.1f})" for pn in periods))
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "event_by_period.csv"), index=False)

    # ---------------- 市场环境分层 ----------------
    print("\n" + "=" * 100)
    print("市场环境分层 (20日超额收益, %)")
    print("=" * 100)
    reg = pd.read_parquet(os.path.join(OUT, "market_regimes.parquet"))
    d2 = d.merge(reg[["regime", "vol_regime"]].reset_index().rename(
        columns={"index": "date"}), on="date", how="left")
    rows = []
    for lab, m0 in key_events:
        rec = {"label": lab}
        for rn in ["牛市", "熊市", "震荡"]:
            mm = m0 & (d2["regime"] == rn).values
            st = analyze(d2, mm, lab, horizons=[20])
            rec[rn] = st.get("ex20", np.nan)
            rec[rn + "_n"] = st.get("n", 0)
        for vn in ["高波动", "低波动"]:
            mm = m0 & (d2["vol_regime"] == vn).values
            st = analyze(d2, mm, lab, horizons=[20])
            rec[vn] = st.get("ex20", np.nan)
        rows.append(rec)
        print(f"  {lab}: " + "  ".join(
            f"{k} {rec[k]*100:+.3f}%" for k in ["牛市", "熊市", "震荡", "高波动", "低波动"]))
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "event_by_regime.csv"), index=False)

    print(f"\nsaved. key finding: 见上方 t 统计量与置信区间")


if __name__ == "__main__":
    main()
