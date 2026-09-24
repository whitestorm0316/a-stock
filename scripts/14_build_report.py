#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
14_build_report.py —— 生成最终 HTML 研究报告

输入: output/chart_data.json (12 脚本产出) + output/chart_extra.json (13 脚本产出)
输出: output/A股_MACD量能策略回测研究报告.html

设计: 数据以 const D={...} 内联注入; 图表代码手写, 套用 ECharts 骨架;
      交付前必须对抽取出的内联 JS 跑 node --check。
"""
import json
import os
import math

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "output")


def clean(o):
    """NaN/Inf -> None, numpy -> python"""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    if isinstance(o, float):
        if math.isnan(o) or math.isinf(o):
            return None
        return round(o, 8)
    if hasattr(o, "item"):
        try:
            return clean(o.item())
        except Exception:
            return o
    return o


def load():
    d = json.load(open(os.path.join(OUT, "chart_data.json")))
    e = json.load(open(os.path.join(OUT, "chart_extra.json")))
    return d, e


def trim_main(rows):
    """主结果表: 只保留报告需要的列, 缩短字段名"""
    keep = {
        "strategy": "s", "universe": "u", "period": "p",
        "cagr": "cagr", "total_return": "tr", "max_drawdown": "mdd",
        "ann_vol": "vol", "sharpe": "sharpe", "calmar": "calmar",
        "win_rate": "wr", "payoff_ratio": "payoff", "profit_factor": "pf",
        "wr_x_payoff": "wxp", "n_trades": "ntr", "trades_per_year": "tpy",
        "avg_hold_days": "hold", "avg_win_pct": "awin", "avg_loss_pct": "aloss",
        "expectancy": "expc", "max_consec_loss": "mcl", "max_consec_win": "mcw",
        "total_fee": "fee", "fee_to_pnl": "feepnl", "avg_positions": "apos",
        "n_signals": "nsig", "years": "years",
    }
    out = []
    for r in rows:
        o = {}
        for k, nk in keep.items():
            if k in r:
                o[nk] = r[k]
        out.append(o)
    return out


def trim_events(rows):
    out = []
    for r in rows:
        out.append({
            "label": r.get("label"),
            "n": r.get("n"),
            "abs": r.get("abs"),
            "ex": r.get("ex"),
            "exw": r.get("exw"),
            "t": r.get("t"),
            "lo": r.get("lo"),
            "hi": r.get("hi"),
            "win": r.get("win"),
        })
    return out


def main():
    d, e = load()

    D = {
        "equity": d["equity_curves"],
        "dd": d["drawdown_curves"],
        "yearly": d["yearly_returns"],
        "monthly": d["monthly_heatmap"],
        "fwd": d["signal_fwd_dist"],
        "vr": d["volratio_curve"],
        "hslope": d["histslope_curve"],
        "sens": d["sensitivity"],
        "score": d["score_curve"],
        "score_th": d["score_threshold"],
        "regime": d["regime_bars"],
        "period": d["period_bars"],
        "main": trim_main(d["main_results"]),
        "events": trim_events(d["rigorous_events"]),
        "cost": d["reconcile_cost"],
        "diag": d["diag_daily_random"],
        "layer": e["layer_table"],
        "hs300sig": e["hs300_signal"],
        "randbench": e["random_bench"],
        "geoarith": e["geo_arith"],
    }
    D = clean(D)

    payload = json.dumps(D, ensure_ascii=False, separators=(",", ":"))
    print(f"payload {len(payload)/1024:.1f} KB")

    tpl_path = os.path.join(BASE, "scripts", "report_template.html")
    html = open(tpl_path, encoding="utf-8").read()
    html = html.replace("/*__DATA__*/", payload)

    outp = os.path.join(OUT, "A股_MACD量能策略回测研究报告.html")
    with open(outp, "w", encoding="utf-8") as f:
        f.write(html)
    print("written:", outp, f"{len(html)/1024:.1f} KB")


if __name__ == "__main__":
    main()
