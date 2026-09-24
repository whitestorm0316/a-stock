#!/usr/bin/env python3
"""
12_build_chart_data.py — 汇总所有回测/统计输出, 生成 HTML 报告用的图表数据 JSON

产出 output/chart_data.json, 包含第十三节要求的 10 类图表所需数据:
  1  equity_curves      各策略净值曲线
  2  drawdown_curves    最大回撤曲线
  3  yearly_returns     年度收益柱状图
  4  monthly_heatmap    月度收益热力图
  5  signal_fwd_dist    MACD信号后未来收益分布
  6  volratio_curve     VolumeRatio 与未来收益关系
  7  histslope_curve    MACD柱变化与未来收益关系
  8  sens_*             参数敏感性热力图
  9  score_curve        不同 Score 对应未来收益
  10 regime_bars        牛熊震荡分层表现
"""
import os
import json
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "output")


def jnum(x):
    """把 numpy 类型转成 JSON 可序列化, NaN -> None"""
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        x = float(x)
    if isinstance(x, float) and not np.isfinite(x):
        return None
    return x


def clean(obj):
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.integer, np.floating)):
        return jnum(obj)
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return obj


def main():
    data = {}

    # ---------------- 1/2/3/4: 净值、回撤、年度、月度 ----------------
    eq = pd.read_parquet(os.path.join(OUT, "equity_curves.parquet"))
    eq.index = pd.DatetimeIndex(eq.index)
    # 只保留全A池 (避免曲线过多), 并归一化到 1.0
    allA_cols = [c for c in eq.columns if c.endswith("_allA")]
    hs_cols = [c for c in eq.columns if c.endswith("_hs300")]
    pick = allA_cols if allA_cols else list(eq.columns)
    sub = eq[pick].copy()
    sub = sub / sub.iloc[0]
    # 采样: 每周一个点, 减小 JSON 体积
    sub_w = sub.resample("W").last().dropna(how="all")
    data["equity_curves"] = {
        "dates": [d.strftime("%Y-%m-%d") for d in sub_w.index],
        "series": {c.replace("_allA", ""): [jnum(v) for v in sub_w[c]]
                   for c in sub_w.columns},
        "note": "全A股票池, 归一化到1.0, 每周采样; 含全部交易成本",
    }
    # 回撤曲线
    dd = sub / sub.cummax() - 1
    dd_w = dd.resample("W").last().dropna(how="all")
    data["drawdown_curves"] = {
        "dates": [d.strftime("%Y-%m-%d") for d in dd_w.index],
        "series": {c.replace("_allA", ""): [jnum(v * 100) for v in dd_w[c]]
                   for c in dd_w.columns},
        "note": "单位 %",
    }
    # 年度收益
    yr = {}
    for c in sub.columns:
        s = sub[c].dropna()
        ye = s.resample("YE").last()
        prev = s.iloc[0]
        r = {}
        for d, v in ye.items():
            r[str(d.year)] = jnum(v / prev - 1)
            prev = v
        yr[c.replace("_allA", "")] = r
    data["yearly_returns"] = {
        "years": sorted({y for r in yr.values() for y in r}),
        "series": yr,
        "note": "单位 小数 (0.1 = +10%)",
    }
    # 月度热力图 (用策略D与G, 以及全A等权基准)
    hm = {}
    for c in sub.columns:
        s = sub[c].dropna()
        me = s.resample("ME").last()
        prev = pd.Series([s.iloc[0]], index=[s.index[0]])
        mr = pd.concat([prev, me]).pct_change().dropna()
        hm[c.replace("_allA", "")] = {
            f"{d.year}-{d.month:02d}": jnum(v) for d, v in mr.items()}
    data["monthly_heatmap"] = {"series": hm, "note": "单位 小数"}

    # ---------------- 5: 信号后未来收益分布 ----------------
    ea = pd.read_csv(os.path.join(OUT, "event_study_A.csv"))
    rows = []
    for _, r in ea.iterrows():
        rows.append({
            "label": r["label"],
            "n": int(r["n"]),
            "mean": [jnum(r[f"mean{H}"]) for H in (1, 5, 10, 20, 60)],
            "win": [jnum(r[f"win{H}"]) for H in (1, 5, 10, 20, 60)],
            "t": [jnum(r[f"t{H}"]) for H in (1, 5, 10, 20, 60)],
        })
    data["signal_fwd_dist"] = {"horizons": [1, 5, 10, 20, 60], "rows": rows,
                              "note": "收益单位 小数; t 为普通 t 统计量(未聚类)"}

    # ---------------- 6: VolumeRatio 与未来收益 ----------------
    vr = pd.read_csv(os.path.join(OUT, "event_study_C_volratio.csv"))
    data["volratio_curve"] = {
        "bins": [str(b) for b in vr["vr_bin"]],
        "n": [int(v) for v in vr["n"]],
        "mean": {f"H{H}": [jnum(v) for v in vr[f"mean{H}"]] for H in (1, 5, 10, 20, 60)},
        "note": "无条件样本(全市场可交易), 收益单位 小数",
    }

    # ---------------- 7: MACD柱变化与未来收益 ----------------
    hs = pd.read_csv(os.path.join(OUT, "event_study_D_histslope.csv"))
    data["histslope_curve"] = {
        "bins": [str(b) for b in hs["hist_slope_bin"]],
        "n": [int(v) for v in hs["n"]],
        "mean": {f"H{H}": [jnum(v) for v in hs[f"mean{H}"]] for H in (1, 5, 10, 20, 60)},
        "note": "hist_slope = 当日MACD柱 - 前一日MACD柱",
    }

    # ---------------- 8: 参数敏感性 ----------------
    sens = {}
    for key, fn in (("macd", "sens_macd.csv"), ("vr", "sens_vr.csv"),
                    ("volma", "sens_volma.csv"), ("trend", "sens_trend.csv"),
                    ("pullback", "sens_pullback.csv")):
        p = os.path.join(OUT, fn)
        if not os.path.exists(p):
            continue
        d = pd.read_csv(p)
        sens[key] = {"columns": list(d.columns),
                     "rows": [{k: (str(v) if isinstance(v, str) else jnum(v))
                               for k, v in r.items()} for r in d.to_dict("records")]}
    data["sensitivity"] = sens

    # ---------------- 9: Score 与未来收益 ----------------
    p = os.path.join(OUT, "score_monotonicity.csv")
    if os.path.exists(p):
        sc = pd.read_csv(p)
        data["score_curve"] = {
            "bins": [str(b) for b in sc["score_bin"]],
            "n": [int(v) for v in sc["n"]],
            "score_mean": [jnum(v) for v in sc["score_mean"]],
            "abs": {f"H{H}": [jnum(v) for v in sc[f"abs{H}"]] for H in (5, 10, 20, 60)},
            "ex": {f"H{H}": [jnum(v) for v in sc[f"ex{H}"]] for H in (5, 10, 20, 60)},
        }
    p = os.path.join(OUT, "score_threshold.csv")
    if os.path.exists(p):
        st = pd.read_csv(p)
        data["score_threshold"] = [
            {"threshold": r["threshold"], "n": int(r["n"]), "pct": jnum(r["pct"]),
             "ex5": jnum(r["ex5"]), "ex10": jnum(r["ex10"]),
             "ex20": jnum(r["ex20"]), "ex60": jnum(r["ex60"]),
             "win20": jnum(r["win20"])}
            for r in st.to_dict("records")]

    # ---------------- 10: 牛熊震荡分层 ----------------
    er = pd.read_csv(os.path.join(OUT, "event_by_regime.csv"))
    cols = [c for c in er.columns if not c.endswith("_n")]
    data["regime_bars"] = {
        "regimes": [c for c in cols if c != "label"],
        "rows": [{"label": r["label"],
                  "vals": [jnum(r[c]) for c in cols if c != "label"]}
                 for r in er.to_dict("records")],
        "note": "20日超额收益, 单位 小数",
    }
    # 分期
    ep = pd.read_csv(os.path.join(OUT, "event_by_period.csv"))
    pcols = [c for c in ep.columns if not c.endswith("_t") and c != "label"]
    data["period_bars"] = {
        "periods": pcols,
        "rows": [{"label": r["label"],
                  "vals": [jnum(r[c]) for c in pcols]}
                 for r in ep.to_dict("records")],
        "note": "20日超额收益, 单位 小数",
    }

    # ---------------- 主回测结果表 ----------------
    mr = pd.read_parquet(os.path.join(OUT, "main_results.parquet"))
    keep = ["strategy", "strategy_desc", "universe", "period", "cagr",
            "total_return", "max_drawdown", "ann_vol", "sharpe", "calmar",
            "win_rate", "payoff_ratio", "profit_factor", "wr_x_payoff",
            "n_trades", "trades_per_year", "avg_hold_days", "avg_win_pct",
            "avg_loss_pct", "expectancy", "max_consec_loss", "max_consec_win",
            "total_fee", "fee_to_pnl", "avg_positions", "n_signals"]
    keep = [c for c in keep if c in mr.columns]
    mr2 = mr[keep].copy()
    for c in mr2.columns:
        if mr2[c].dtype.kind == "f":
            mr2[c] = mr2[c].round(6)
    data["main_results"] = [
        {k: (jnum(v) if not isinstance(v, str) else v) for k, v in r.items()}
        for r in mr2.to_dict("records")]

    # ---------------- 严格事件研究表 ----------------
    rg = pd.read_csv(os.path.join(OUT, "rigorous_event_study.csv"))
    hz = [1, 5, 10, 20, 60]
    data["rigorous_events"] = [
        {"label": r["label"], "n": int(r["n"]),
         "abs": [jnum(r[f"abs{H}"]) for H in hz],
         "ex": [jnum(r[f"ex{H}"]) for H in hz],
         "exw": [jnum(r[f"exw{H}"]) for H in hz],
         "t": [jnum(r[f"t{H}"]) for H in hz],
         "lo": [jnum(r[f"lo{H}"]) for H in hz],
         "hi": [jnum(r[f"hi{H}"]) for H in hz],
         "win": [jnum(r[f"win{H}"]) for H in hz]}
        for r in rg.to_dict("records")]

    # ---------------- 对账 / 隔离实验 ----------------
    for key, fn in (("reconcile_cost", "reconcile_cost.csv"),
                    ("diag_selection_exit", "diag_selection_exit.csv"),
                    ("diag_daily_random", "diag_daily_random.csv")):
        p = os.path.join(OUT, fn)
        if os.path.exists(p):
            d = pd.read_csv(p)
            data[key] = [{k: (v if isinstance(v, str) else jnum(v))
                          for k, v in r.items()} for r in d.to_dict("records")]

    p = os.path.join(OUT, "chart_extra.json")
    if os.path.exists(p):
        data.update(json.load(open(p)))

    out = os.path.join(OUT, "chart_data.json")
    with open(out, "w") as f:
        json.dump(clean(data), f, ensure_ascii=False, separators=(",", ":"))
    sz = os.path.getsize(out)
    print(f"saved {out}  ({sz/1024:.0f} KB)")
    for k, v in data.items():
        if isinstance(v, dict):
            print(f"  {k}: keys={list(v.keys())[:8]}")
        elif isinstance(v, list):
            print(f"  {k}: {len(v)} rows")


if __name__ == "__main__":
    main()
