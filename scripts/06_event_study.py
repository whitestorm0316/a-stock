#!/usr/bin/env python3
"""
06_event_study.py — 事件研究 (与回测引擎完全独立)

直接检验: 特定信号出现后, 未来 1/5/10/20/60 日的收益分布。
这是用户需求第十二节的核心, 也是验证引擎结果合理性的独立标尺。

注意: fwd_retN = close[t+N]/close[t] - 1, 即信号日收盘持有到 N 日后的收盘。
      实盘按 T+1 开盘成交, 差异约为"次日跳空"部分, 会在结果中单列。
"""
import os
import json
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

HORIZONS = [1, 5, 10, 20, 60]


def load():
    cols = ["thscode", "date", "close_price", "open_price", "ret", "turnover",
            "dif", "dea", "hist", "hist_prev", "hist_cross_up", "golden_cross",
            "death_cross", "vol_ratio", "vol5_vol20", "vol10_vol20", "vol5_vol60",
            "ma5", "ma20", "ma60", "px_ma20_pct", "dist_high20", "dist_low20",
            "dv_bull", "dv_bull_recent", "days_since_list", "is_st_now",
            "hist_slope", "limit_pct", "board",
            "fwd_ret1", "fwd_ret5", "fwd_ret10", "fwd_ret20", "fwd_ret60",
            "high20_prev"]
    p = pd.read_parquet(os.path.join(PROC, "panel.parquet"), columns=cols)
    return p


def summarize(df, mask, label, horizons=HORIZONS, base=None):
    """计算 mask 子集在多个持有期上的收益统计"""
    sub = df[mask]
    n = len(sub)
    out = {"label": label, "n": int(n)}
    if n == 0:
        return out
    for h in horizons:
        v = sub[f"fwd_ret{h}"].values
        v = v[np.isfinite(v)]
        if len(v) == 0:
            out[f"n{h}"] = 0
            continue
        out[f"mean{h}"] = float(np.mean(v))
        out[f"med{h}"] = float(np.median(v))
        out[f"std{h}"] = float(np.std(v))
        out[f"win{h}"] = float(np.mean(v > 0))
        out[f"n{h}"] = int(len(v))
        # t 统计量 (检验均值是否显著非零)
        out[f"t{h}"] = float(np.mean(v) / (np.std(v, ddof=1) / np.sqrt(len(v))))
        # 超额: 相对同期全市场等权基准
        if base is not None:
            b = base.get(h)
            if b is not None:
                out[f"excess{h}"] = float(np.mean(v) - b)
    return out


def market_base(df):
    """全市场各持有期平均收益 (作为基准)"""
    base = {}
    for h in HORIZONS:
        v = df[f"fwd_ret{h}"].values
        v = v[np.isfinite(v)]
        base[h] = float(np.mean(v)) if len(v) else np.nan
    return base


def main():
    print("loading panel ...")
    df = load()
    print(f"  {len(df):,} rows")

    # 基础可交易过滤 (与回测一致的可比样本)
    tradable = ((df["days_since_list"] >= 120) & (~df["is_st_now"])
                & (df["turnover"] >= 20_000_000)
                & np.isfinite(df["close_price"]))
    d = df[tradable].copy()
    print(f"  tradable rows: {len(d):,}")

    base = market_base(d)
    print("  全市场基准平均收益:")
    for h in HORIZONS:
        print(f"    {h:>2}d: {base[h]*100:+.3f}%")

    vr = d["vol_ratio"].values
    v5v20 = d["vol5_vol20"].values
    hu = d["hist_cross_up"].values.astype(bool)
    gc = d["golden_cross"].values.astype(bool)
    dc = d["death_cross"].values.astype(bool)
    dvb = d["dv_bull"].values.astype(bool)
    dvbr = d["dv_bull_recent"].values.astype(bool)
    c = d["close_price"].values
    ma20 = d["ma20"].values
    ma60 = d["ma60"].values
    hist = d["hist"].values

    print("\n" + "=" * 78)
    print("A. 核心组合: MACD柱由负转正 × 成交量")
    print("=" * 78)
    events = [
        ("MACD翻红 (全部)", hu),
        ("MACD翻红 + 缩量 (VR<0.8)", hu & (vr < 0.8)),
        ("MACD翻红 + 平量 (0.8<=VR<1.1)", hu & (vr >= 0.8) & (vr < 1.1)),
        ("MACD翻红 + 温和放量 (1.1<=VR<1.5)", hu & (vr >= 1.1) & (vr < 1.5)),
        ("MACD翻红 + 大幅放量 (VR>=1.5)", hu & (vr >= 1.5)),
        ("MACD翻红 + VR>1.1", hu & (vr > 1.1)),
        ("MACD翻红 + VR>1.0", hu & (vr > 1.0)),
        ("MACD翻红 + VR>1.2", hu & (vr > 1.2)),
        ("MACD翻红 + VR>2.0", hu & (vr > 2.0)),
        ("MACD金叉 + 放量 (VR>1.1)", gc & (vr > 1.1)),
        ("MACD金叉 + 缩量 (VR<1.0)", gc & (vr < 1.0)),
        ("MACD死叉 (反向对照)", dc),
    ]
    rows = []
    for lab, m in events:
        rows.append(summarize(d, m, lab, base=base))
    ev = pd.DataFrame(rows)
    show = ["label", "n"] + [f"{k}{h}" for h in (1, 5, 10, 20, 60)
                             for k in ("mean",)]
    disp = ev[["label", "n"] + [f"mean{h}" for h in HORIZONS]].copy()
    for h in HORIZONS:
        disp[f"mean{h}"] = (disp[f"mean{h}"] * 100).round(2)
    print(disp.to_string(index=False))
    print("\n  显著性 (t 统计量):")
    print(ev[["label"] + [f"t{h}" for h in HORIZONS]].round(2).to_string(index=False))

    print("\n" + "=" * 78)
    print("B. 底背离 × 成交量")
    print("=" * 78)
    ev2 = []
    for lab, m in [
        ("底背离发生日", dvb),
        ("底背离 + 缩量 (VR<1.0)", dvb & (vr < 1.0)),
        ("底背离 + 放量 (VR>1.1)", dvb & (vr > 1.1)),
        ("底背离 + 翻红确认 (右侧)", dvbr & hu),
        ("底背离 + 翻红 + 放量", dvbr & hu & (vr > 1.1)),
    ]:
        ev2.append(summarize(d, m, lab, base=base))
    e2 = pd.DataFrame(ev2)
    dp2 = e2[["label", "n"] + [f"mean{h}" for h in HORIZONS]].copy()
    for h in HORIZONS:
        dp2[f"mean{h}"] = (dp2[f"mean{h}"] * 100).round(2)
    print(dp2.to_string(index=False))

    print("\n" + "=" * 78)
    print("C. Volume Ratio 分档 → 未来收益 (全部样本, 不限定MACD)")
    print("=" * 78)
    bins = [0, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 100]
    d["vr_bin"] = pd.cut(vr, bins=bins)
    rows3 = []
    for b, sub in d.groupby("vr_bin", observed=True):
        r = {"vr_bin": str(b), "n": len(sub)}
        for h in HORIZONS:
            v = sub[f"fwd_ret{h}"].values
            v = v[np.isfinite(v)]
            r[f"mean{h}"] = float(np.mean(v)) if len(v) else np.nan
        rows3.append(r)
    e3 = pd.DataFrame(rows3)
    dp3 = e3.copy()
    for h in HORIZONS:
        dp3[f"mean{h}"] = (dp3[f"mean{h}"] * 100).round(2)
    print(dp3.to_string(index=False))

    print("\n" + "=" * 78)
    print("D. MACD柱变化 → 未来收益")
    print("=" * 78)
    hs = d["hist_slope"].values
    d["hs_bin"] = pd.cut(hs, bins=[-np.inf, -0.05, -0.01, 0, 0.01, 0.05, np.inf])
    rows4 = []
    for b, sub in d.groupby("hs_bin", observed=True):
        r = {"hist_slope_bin": str(b), "n": len(sub)}
        for h in HORIZONS:
            v = sub[f"fwd_ret{h}"].values
            v = v[np.isfinite(v)]
            r[f"mean{h}"] = float(np.mean(v)) if len(v) else np.nan
        rows4.append(r)
    e4 = pd.DataFrame(rows4)
    dp4 = e4.copy()
    for h in HORIZONS:
        dp4[f"mean{h}"] = (dp4[f"mean{h}"] * 100).round(2)
    print(dp4.to_string(index=False))

    print("\n" + "=" * 78)
    print("E. 条件叠加 (逐层加条件, 检验边际贡献)")
    print("=" * 78)
    conds = [
        ("① MACD翻红", hu),
        ("② +VR>1.1", hu & (vr > 1.1)),
        ("③ +Close>MA20", hu & (vr > 1.1) & (c > ma20)),
        ("④ +MA20>MA60", hu & (vr > 1.1) & (c > ma20) & (ma20 > ma60)),
        ("⑤ +Close>MA60", hu & (vr > 1.1) & (c > ma20) & (ma20 > ma60) & (c > ma60)),
        ("⑥ +DIF>0", hu & (vr > 1.1) & (c > ma20) & (ma20 > ma60) & (c > ma60) & (d["dif"].values > 0)),
        ("⑦ +突破20日高", hu & (vr > 1.1) & (c > ma20) & (ma20 > ma60) & (c > ma60) & (d["dif"].values > 0) & (c > d["high20_prev"].values)),
    ]
    rows5 = []
    for lab, m in conds:
        rows5.append(summarize(d, m, lab, base=base))
    e5 = pd.DataFrame(rows5)
    dp5 = e5[["label", "n"] + [f"mean{h}" for h in HORIZONS]].copy()
    for h in HORIZONS:
        dp5[f"mean{h}"] = (dp5[f"mean{h}"] * 100).round(2)
    print(dp5.to_string(index=False))

    # 保存
    ev.to_csv(os.path.join(OUT, "event_study_A.csv"), index=False)
    e2.to_csv(os.path.join(OUT, "event_study_B_divergence.csv"), index=False)
    e3.to_csv(os.path.join(OUT, "event_study_C_volratio.csv"), index=False)
    e4.to_csv(os.path.join(OUT, "event_study_D_histslope.csv"), index=False)
    e5.to_csv(os.path.join(OUT, "event_study_E_layered.csv"), index=False)
    json.dump({"market_base": base}, open(os.path.join(OUT, "market_base.json"), "w"))
    print("\nsaved event study CSVs to output/")


if __name__ == "__main__":
    main()
