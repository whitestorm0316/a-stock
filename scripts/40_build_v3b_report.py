#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
40_build_v3b_report.py —— 生成 V3B 最终研究报告 HTML

模板 + 数据注入（占位符 /*__DATA__*/），避免手写大 JSON 造成括号失配。
"""
import os
import json
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "output")


def r2(x, nd=4):
    try:
        v = float(x)
        return None if not np.isfinite(v) else round(v, nd)
    except Exception:
        return None


def load(fn):
    p = os.path.join(OUT, fn)
    return pd.read_csv(p) if os.path.exists(p) else pd.DataFrame()


def main():
    D = {}

    # ---------- 第1节：8 维度十分位
    pd_df = load("v3b_price_decile.csv")
    DIMS = [("ret5", "过去5日"), ("ret10", "过去10日"), ("ret20", "过去20日"),
            ("ret40", "过去40日"), ("ret60", "过去60日"),
            ("px_ma20_pct", "距MA20"), ("px_ma60_pct", "距MA60"),
            ("px_ma120_pct", "距MA120")]
    s20 = pd_df[pd_df.H == 20]
    D["decile"] = {
        "dims": [cn for _, cn in DIMS],
        "cols": [d for d, _ in DIMS],
        "exw": [[r2(s20[(s20.dim == d) & (s20.decile == k)].exw.iloc[0] * 100, 3)
                 if len(s20[(s20.dim == d) & (s20.decile == k)]) else None
                 for k in range(1, 11)] for d, _ in DIMS],
        "t": [[r2(s20[(s20.dim == d) & (s20.decile == k)].t.iloc[0], 2)
               if len(s20[(s20.dim == d) & (s20.decile == k)]) else None
               for k in range(1, 11)] for d, _ in DIMS],
        "win": [[r2(s20[(s20.dim == d) & (s20.decile == k)].win.iloc[0] * 100, 1)
                 if len(s20[(s20.dim == d) & (s20.decile == k)]) else None
                 for k in range(1, 11)] for d, _ in DIMS],
        "pf": [[r2(s20[(s20.dim == d) & (s20.decile == k)].pf.iloc[0], 2)
                if len(s20[(s20.dim == d) & (s20.decile == k)]) else None
                for k in range(1, 11)] for d, _ in DIMS],
    }
    # H=1 / H=5 / H=10 的 D1
    D["d1_by_h"] = {}
    for H in [1, 5, 10, 20]:
        s = pd_df[(pd_df.H == H) & (pd_df.decile == 1)]
        D["d1_by_h"][H] = {d: r2(s[s.dim == d].exw.iloc[0] * 100, 3)
                           if len(s[s.dim == d]) else None for d, _ in DIMS}
        D["d1_by_h"][f"t{H}"] = {d: r2(s[s.dim == d].t.iloc[0], 2)
                                 if len(s[s.dim == d]) else None for d, _ in DIMS}

    # ---------- 第2节：组合
    cdf = load("v3b_d2d6_combo.csv")
    D["combos"] = [dict(name=r["combo"], n=int(r["n"]),
                        cagr=r2(r["nav_cagr"] * 100, 1), mdd=r2(r["nav_mdd"] * 100, 1),
                        sharpe=r2(r["nav_sharpe"], 2),
                        arith=r2(r["nav_ann_arith"] * 100, 1),
                        exw20=r2(r["exw20"] * 100, 3), t20=r2(r["t20"], 2),
                        exw5=r2(r["exw5"] * 100, 3), t5=r2(r["t5"], 2),
                        win=r2(r["win20"] * 100, 1), pf=r2(r["pf20"], 2))
                   for r in cdf.to_dict("records")]

    # ---------- 第3节：确认信号
    kdf = load("v3b_confirm.csv")
    D["confirm"] = [dict(tag=r["tag"], n=int(r["n"]),
                         exw20=r2(r["exw20"] * 100, 3), t20=r2(r["t20"], 2),
                         exw5=r2(r["exw5"] * 100, 3), t5=r2(r["t5"], 2),
                         exw60=r2(r["exw60"] * 100, 3), t60=r2(r["t60"], 2),
                         win=r2(r["win20"] * 100, 1), pf=r2(r["pf20"], 2))
                    for r in kdf.to_dict("records")]

    # ---------- 第4节：量能
    vdf = load("v3b_vol_grid.csv")
    D["vol"] = [dict(tag=r["tag"], n=int(r["n"]),
                     exw5=r2(r["exw5"] * 100, 3), t5=r2(r["t5"], 2),
                     exw10=r2(r["exw10"] * 100, 3), t10=r2(r["t10"], 2),
                     exw20=r2(r["exw20"] * 100, 3), t20=r2(r["t20"], 2),
                     exw60=r2(r["exw60"] * 100, 3), t60=r2(r["t60"], 2))
                for r in vdf.to_dict("records")]

    # ---------- 第5节：三阶段
    tdf = load("v3b_stage3.csv")
    D["stage"] = [dict(tag=r["tag"], n=int(r["n"]),
                       exw5=r2(r["exw5"] * 100, 3), t5=r2(r["t5"], 2),
                       exw20=r2(r["exw20"] * 100, 3), t20=r2(r["t20"], 2),
                       exw60=r2(r["exw60"] * 100, 3), t60=r2(r["t60"], 2),
                       win=r2(r["win20"] * 100, 1), pf=r2(r["pf20"], 2))
                  for r in tdf.to_dict("records")]

    # ---------- 第6节：市场
    mdf = load("v3b_market.csv")
    D["market"] = [dict(tag=r["tag"], n=int(r["n"]),
                        exw5=r2(r["exw5"] * 100, 3), t5=r2(r["t5"], 2),
                        exw20=r2(r["exw20"] * 100, 3), t20=r2(r["t20"], 2),
                        cagr=r2(r["nav_cagr"] * 100, 1), mdd=r2(r["nav_mdd"] * 100, 1))
                   for r in mdf.to_dict("records")]

    # ---------- 第7节：相对强弱
    rdf = load("v3b_rs.csv")
    D["rs"] = [dict(tag=r["tag"], n=int(r["n"]),
                    exw5=r2(r["exw5"] * 100, 3), t5=r2(r["t5"], 2),
                    exw20=r2(r["exw20"] * 100, 3), t20=r2(r["t20"], 2),
                    exw60=r2(r["exw60"] * 100, 3), t60=r2(r["t60"], 2))
               for r in rdf.to_dict("records")]

    # ---------- 第8节：市值
    zdf = load("v3b_size.csv")
    D["size"] = [dict(tag=r["tag"], n=int(r["n"]),
                      exw5=r2(r["exw5"] * 100, 3), t5=r2(r["t5"], 2),
                      exw20=r2(r["exw20"] * 100, 3), t20=r2(r["t20"], 2),
                      cagr=r2(r["nav_cagr"] * 100, 1), mdd=r2(r["nav_mdd"] * 100, 1),
                      sharpe=r2(r["nav_sharpe"], 2))
                 for r in zdf.to_dict("records")]

    # ---------- 第9/10节：IC
    idf = load("v3b_ic.csv")
    # ⚠️ 只保留「有横截面区分度」的因子。市场级时间序列（Breadth / 市场距MA60 /
    #    市场MA20/MA60-1 / 市场HV20）在横截面上逐日恒定，逐日截面 RankIC 无定义，
    #    其 ic20 为 nan 或由浮点噪声产生的伪值 → 必须排除，否则会把
    #    「无截面信息」的因子误排到榜首。
    idf = idf[idf["ic20"].notna() & (idf["n_day20"].fillna(0) >= 200)]
    D["ic"] = [dict(cn=r["factor_cn"], f=r["factor"], g=r["group"],
                    ic=r2(r["ic20"], 4), icir=r2(r["icir20"], 3),
                    t=r2(r["t20"], 2), pos=r2(r["ic_pos20"] * 100, 1),
                    nd=int(r["n_day20"]))
               for r in idf.sort_values("ic20", key=lambda s: s.abs(),
                                        ascending=False).to_dict("records")]
    # 分位形状
    ddf = load("v3b_ic_decile.csv")
    D["ic_shape"] = []
    for f, cn in [("px_ma60_pct", "距MA60"), ("ret40", "过去40日"), ("rs_sz20", "相对同规模20日"),
                  ("rvol20", "VOL/VOL20"), ("hist_z", "MACD柱Z"), ("hv20", "20日历史波动"),
                  ("sl5", "MA5斜率"), ("hist_upstreak", "MACD柱连续改善天数")]:
        s = ddf[ddf.factor == f].sort_values("decile")
        if len(s) == 10:
            D["ic_shape"].append(dict(cn=cn, exw=[r2(v * 100, 3) for v in s.exw.values]))
    fdf = load("v3b_fmb.csv")
    D["fmb"] = [dict(cn=r["factor_cn"], coef=r2(r["coef"], 5), t=r2(r["t"], 2))
                for r in fdf[fdf.H == 20].sort_values("coef", key=lambda s: s.abs(),
                                                      ascending=False).to_dict("records")]

    # ---------- 第11节：8 策略
    s8 = load("v3b_strategy8.csv")
    D["strat8"] = [dict(name=r["strategy"], n=int(r["n"]),
                        cagr=r2(r["impl_cagr"] * 100, 1), mdd=r2(r["impl_mdd"] * 100, 1),
                        sharpe=r2(r["impl_sharpe"], 2), win=r2(r["win"] * 100, 1),
                        pf=r2(r["pf"], 2), payoff=r2(r["payoff"], 2),
                        exw20=r2(r["exw20"] * 100, 3), t20=r2(r["t20"], 2),
                        n_trade=int(r["n_trade"]),
                        is_=r2(r["impl_is"] * 100, 1), oos=r2(r["impl_oos"] * 100, 1),
                        seg=[r2(r["impl_seg_2015-2020"] * 100, 1),
                             r2(r["impl_seg_2021-2022"] * 100, 1),
                             r2(r["impl_seg_2023-2024"] * 100, 1),
                             r2(r["impl_seg_2025-2026"] * 100, 1)])
                   for r in s8.to_dict("records")]

    # ---------- 第12节：简单策略
    ks = load("v3b_strategy_simple.csv")
    D["simple"] = [dict(name=r["strategy"], ncond=int(r["ncond"]), n=int(r["n"]),
                        cagr=r2(r["impl_cagr"] * 100, 1), mdd=r2(r["impl_mdd"] * 100, 1),
                        sharpe=r2(r["impl_sharpe"], 2), win=r2(r["win"] * 100, 1),
                        pf=r2(r["pf"], 2), exw20=r2(r["exw20"] * 100, 3), t20=r2(r["t20"], 2),
                        is_=r2(r["impl_is"] * 100, 1), oos=r2(r["impl_oos"] * 100, 1),
                        seg=[r2(r["impl_seg_2015-2020"] * 100, 1),
                             r2(r["impl_seg_2021-2022"] * 100, 1),
                             r2(r["impl_seg_2023-2024"] * 100, 1),
                             r2(r["impl_seg_2025-2026"] * 100, 1)])
                   for r in ks.to_dict("records")]

    # ---------- 第12节：稳健性
    rb = load("v3b_robust.csv")
    D["robust"] = [dict(th=int(r["MA60th"]), size=r["size"], mkt=r["market"],
                        n=int(r["n"]), cagr=r2(r["cagr"] * 100, 1),
                        mdd=r2(r["mdd"] * 100, 1), sharpe=r2(r["sharpe"], 2),
                        win=r2(r["win"] * 100, 1), pf=r2(r["pf"], 2),
                        is_=r2(r["is_ret"] * 100, 1), oos=r2(r["oos_ret"] * 100, 1),
                        neg=int(r["neg_seg"]))
                   for r in rb.to_dict("records")]
    ry = load("v3b_robust_year.csv")
    YRS = list(range(2015, 2027))
    D["robust_year"] = []
    for r in ry.to_dict("records"):
        D["robust_year"].append(dict(name=r["strategy"], n=int(r["n"]),
                                     cagr=r2(r["cagr"] * 100, 1), mdd=r2(r["mdd"] * 100, 1),
                                     sharpe=r2(r["sharpe"], 2), win=r2(r["win"] * 100, 1),
                                     pf=r2(r["pf"], 2), pos=r["pos_year"],
                                     is_=r2(r["is_ret"] * 100, 1),
                                     oos=r2(r["oos_ret"] * 100, 1),
                                     y=[r2(r[f"y{y}"] * 100, 0) for y in YRS],
                                     seg=[r2(r["seg_2015-2020"] * 100, 1),
                                          r2(r["seg_2021-2022"] * 100, 1),
                                          r2(r["seg_2023-2024"] * 100, 1),
                                          r2(r["seg_2025-2026"] * 100, 1)]))
    D["years"] = YRS

    # ---------- 第13节：归因
    at = load("v3b_attribution.csv")
    D["attr"] = []
    for r in at.to_dict("records"):
        D["attr"].append(dict(name=r["variant"], n=int(r["n"]),
                              cagr=r2(r["cagr"] * 100, 1), mdd=r2(r["mdd"] * 100, 1),
                              sharpe=r2(r["sharpe"], 2), win=r2(r["win"] * 100, 1),
                              pf=r2(r["pf"], 2), mean=r2(r["mean"] * 100, 2),
                              n_trade=int(r["n_trade"]),
                              y=[r2(r[f"y{y}"] * 100, 0) for y in YRS]))

    payload = json.dumps(D, ensure_ascii=False)

    tpl_path = os.path.join(ROOT, "scripts", "v3b_report_template.html")
    with open(tpl_path, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/*__DATA__*/", payload)
    out = os.path.join(OUT, "A股_价格超跌反转研究_V3B研究报告.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"-> {out}  ({len(html):,} bytes)")
    print(f"   payload {len(payload):,} bytes")


if __name__ == "__main__":
    main()
