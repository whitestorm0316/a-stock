#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
27_build_v2_report.py —— 生成 V2 HTML 研究报告（25 节结构）

输入: output/v2_*.csv
输出: output/A股_MACD量能_V2因子研究报告.html

设计: 数据以 const D={...} 内联注入; 图表代码手写, 套用 ECharts 骨架;
      交付前必须对抽取出的内联 JS 跑 node --check + 运行时冒烟测试。
"""
import json
import math
import os

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "output")
HZ = [1, 5, 10, 20, 40, 60]


def clean(o):
    """NaN/Inf -> None, numpy -> python"""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, float):
        if math.isnan(o) or math.isinf(o):
            return None
        return round(o, 8)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        v = float(o)
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, 8)
    if hasattr(o, "item"):
        try:
            return clean(o.item())
        except Exception:
            return o
    return o


def rdf(name):
    p = os.path.join(OUT, name)
    if not os.path.exists(p):
        print(f"  !! 缺失 {name}")
        return pd.DataFrame()
    return pd.read_csv(p)


def sub(df, **kw):
    """按等值条件筛选并挑选列, 输出 list[dict]"""
    if df.empty:
        return []
    m = pd.Series(True, index=df.index)
    for k, v in kw.items():
        if isinstance(v, (list, tuple, set)):
            m &= df[k].isin(v)
        else:
            m &= df[k] == v
    return df.loc[m].to_dict("records")


def main():
    D = {}

    # ---------- 1. 事件总览 ----------
    ev = rdf("v2_events_summary.csv")
    D["ev_summary"] = clean(ev.to_dict("records"))

    # ---------- 2. 事件 × 分组（挑选关键维度/事件，控制体积）----------
    ed = rdf("v2_events_by_dim.csv")
    KEEP_DIM = ["VR20", "RVOL20", "RVOL10", "RVOL60", "VOLZ", "VOLZ_BIN",
                "VT520", "VT1020", "VT2060", "VCHG5", "VCHG1",
                "UPVOL_R", "UD_VOL", "VOL_STRUCT20", "VOL_STRUCT",
                "PX_MA20", "PX_MA60", "PX_MA120", "PX_H20", "PX_H60", "PX_H120",
                "RET5", "RET20", "RET20_ABS", "RET60",
                "ATR_PCT", "HV20", "HV20_3",
                "RS_MKT5", "RS_MKT20", "RS_MKT60", "RS_IND20", "RS_SZ20",
                "HIST_Z", "DHIST1_Z", "MACD_POS", "SIZE_D", "GC_TYPE"]
    cols_keep = ["label", "dim", "grp", "n"] + [f"{p}_{h}" for p in
                 ("mean", "median", "win", "p10", "p25", "p50", "p75", "p90") for h in HZ] \
                + [f"{p}{h}" for p in ("exw", "t") for h in (1, 5, 20, 60)] \
                + ["ndays20", "lo20", "hi20"]
    cols_keep = [c for c in cols_keep if c in ed.columns]
    edk = ed[ed["dim"].isin(KEEP_DIM)][cols_keep]
    D["ev_dim"] = clean(edk.to_dict("records"))
    print(f"  ev_dim: {len(edk)} 行")

    # ---------- 3. IC/IR ----------
    D["ic"] = clean(rdf("v2_ic.csv").to_dict("records"))

    # ---------- 4. 双因子交互 ----------
    D["inter"] = clean(rdf("v2_interaction.csv").to_dict("records"))

    # ---------- 5. 市值中性 ----------
    sn = rdf("v2_size_neutral.csv")
    D["size"] = clean(sn[sn["dim"] == "规模组"].to_dict("records"))

    # ---------- 6. 行业中性 ----------
    D["ind"] = clean(rdf("v2_ind_neutral.csv").to_dict("records"))

    # ---------- 7. 横截面回归 ----------
    D["reg"] = clean(rdf("v2_regression.csv").to_dict("records"))

    # ---------- 8. Entry × Exit ----------
    ee = rdf("v2_entry_exit.csv")
    D["ee"] = clean(ee.to_dict("records"))
    # 网格矩阵(便于画热力图)
    D["ee_entries"] = sorted(ee["entry"].unique().tolist())
    D["ee_exits"] = sorted(ee["exit"].unique().tolist())

    # ---------- 9. Walk-Forward ----------
    D["wf"] = clean(rdf("v2_walkforward.csv").to_dict("records"))

    # ---------- 10. 样本外 ----------
    oo = rdf("v2_oos.csv")
    D["oos"] = clean(oo.to_dict("records"))
    # 训练段选最优 exit 的三段对比
    if not oo.empty:
        tr = oo[oo["seg"] == "训练2015-2022"]
        best = tr.sort_values("exw", ascending=False).groupby("entry").head(1)[["entry", "exit"]]
        mm = oo.merge(best, on=["entry", "exit"], how="inner")
        D["oos_best"] = clean(mm.to_dict("records"))

    # ---------- 11. 基准比较 ----------
    D["bench"] = clean(rdf("v2_benchmark.csv").to_dict("records"))

    # ---------- 12. 成本敏感性 ----------
    D["cost"] = clean(rdf("v2_cost_sens.csv").to_dict("records"))

    # ---------- 13. 研究漏斗 ----------
    D["funnel"] = clean(rdf("v2_funnel.csv").to_dict("records"))

    # ---------- 14. 简单版本 + 邻域 ----------
    D["simple"] = clean(rdf("v2_simple_neighborhood.csv").to_dict("records"))

    # ---------- 15. Ablation ----------
    D["abl"] = clean(rdf("v2_ablation.csv").to_dict("records"))

    # ---------- 16. Entry 年度 ----------
    D["ey"] = clean(rdf("v2_entry_year.csv").to_dict("records"))

    D = clean(D)
    payload = json.dumps(D, ensure_ascii=False, separators=(",", ":"))
    print(f"payload {len(payload)/1024:.1f} KB")

    tpl = os.path.join(BASE, "scripts", "report_v2_template.html")
    html = open(tpl, encoding="utf-8").read()
    html = html.replace("/*__DATA__*/", payload)

    outp = os.path.join(OUT, "A股_MACD量能_V2因子研究报告.html")
    with open(outp, "w", encoding="utf-8") as f:
        f.write(html)
    print("written:", outp, f"{len(html)/1024:.1f} KB")


if __name__ == "__main__":
    main()
