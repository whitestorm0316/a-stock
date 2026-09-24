#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
30_build_v3_report.py —— 生成 V3《A股「超跌 + 极端缩量」反转策略研究报告》

输入：output/v3_*.csv（由 28_reversal_strategy.py / 29_reversal_diag.py 产出）
输出：output/A股_超跌缩量反转策略_V3研究报告.html
"""
import json
import os
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "output")
TGT = os.path.join(OUT, "A股_超跌缩量反转策略_V3研究报告.html")


def rd(name):
    return pd.read_csv(os.path.join(OUT, name))


ee = rd("v3_entry_exit.csv")
dec = rd("v3_decile.csv")
seg = rd("v3_segments.csv")
wf = rd("v3_walkforward.csv")
d1 = rd("v3_d1.csv")
bench = rd("v3_bench.csv")
cost = rd("v3_cost.csv")
neigh = rd("v3_neigh.csv")
year = rd("v3_rev_year.csv")
alt = rd("v3_alt_def.csv")
bias = rd("v3_bias_diag.csv")
year2 = rd("v3_year_v2.csv")
robust = rd("v3_robust.csv")

# ---------------------------------------------------------------- 工具
DIM_CN = {
    "hist_z": "MACD柱位置 hist_z", "volz20": "Volume Z", "vr20m": "量比 VR20",
    "vt_5_20": "量能趋势 vt5/20", "ret5": "过去5日涨幅", "ret20": "过去20日涨幅",
    "ret60": "过去60日涨幅", "hv20": "波动率 hv20", "px_ma60_pct": "距MA60",
    "rs_mkt20": "相对强弱 RS20",
}
# 技术型（动能/量能） vs 价格型
TECH = {"hist_z", "volz20", "vr20m", "vt_5_20", "hv20"}
PRICE = {"ret5", "ret20", "ret60", "px_ma60_pct", "rs_mkt20"}


def pct(x, n=2):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "—"
    return f"{x*100:+.{n}f}%"


def num(x, n=2):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "—"
    return f"{x:+.{n}f}"


def cls(x):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "mute"
    return "pos" if x > 0 else ("neg" if x < 0 else "mute")


def table(df, cols, header=None, maxh=None):
    """cols: list of (源列, 表头, 格式函数)"""
    h = header or [c[1] for c in cols]
    out = ["<div class='tscroll'>" if maxh is None else f"<div class='tscroll' style='max-height:{maxh}px'>",
           "<table><thead><tr>"]
    out += [f"<th>{x}</th>" for x in h]
    out += ["</tr></thead><tbody>"]
    for _, r in df.iterrows():
        out.append("<tr>")
        for i, (c, _, f) in enumerate(cols):
            v = r[c] if c in r else None
            if f is None:
                txt = str(v)
                k = ""
            else:
                txt, k = f(v, r)
            c0 = " class='t0'" if i == 0 else ""
            out.append(f"<td{c0}><span class='{k}'>{txt}</span></td>" if k else f"<td{c0}>{txt}</td>")
        out.append("</tr>")
    out += ["</tbody></table></div>"]
    return "\n".join(out)


def fplain(v, r):
    return (str(v), "")


def fnum(v, r, n=2):
    return (num(v, n), cls(v))


def fpct(v, r, n=2):
    return (pct(v, n), cls(v))


def fint(v, r):
    if v is None or not np.isfinite(v):
        return ("—", "")
    return (f"{int(v):,}", "")


# ---------------------------------------------------------------- 图表数据
def jd(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


# 1) Entry × Exit 网格
EE_ORDER = list(dict.fromkeys(ee["entry"]))
EX_ORDER = ["XA_固定持有5日", "XA_固定持有10日", "XD_ATR2倍止损", "XD_ATR3倍止损"]
ee_short = {
    "R0_完整(超跌+缩量+没涨+低波+非D1)": "R0 完整（用户规格）",
    "R1_去低波": "R1 去「低波」", "R2_去没涨": "R2 去「没涨」",
    "R3_仅超跌+缩量": "R3 仅超跌+缩量", "R4_完整+0轴下": "R4 完整+0轴下",
    "R5_完整但不剔除D1": "R5 完整但不剔D1", "R5b_仅D1(完整)": "R5b 仅D1",
    "R6_缩量更严(vz<-1.5)": "R6 缩量更严 vz<-1.5", "R7_用VR低位替vz": "R7 VR20 替 vz",
    "R8_仅缩量+没涨+低波(无超跌)": "R8 无「超跌」条件",
    "R9_仅超跌+没涨+低波(无缩量)": "R9 无「缩量」条件",
}
short_lab = [ee_short.get(x, x) for x in EE_ORDER]
ee_grid = {}
for ex in EX_ORDER:
    ee_grid[ex] = [float(ee[(ee.entry == en) & (ee.exit == ex)]["cagr"].iloc[0]) * 100 for en in EE_ORDER]
ee_exw = {}
for ex in EX_ORDER:
    ee_exw[ex] = [float(ee[(ee.entry == en) & (ee.exit == ex)]["exw"].iloc[0]) * 100 for en in EE_ORDER]

# 2) 十分位
hs = [5, 10, 20, 60]
DEC = {}
for dim in DIM_CN:
    sub = dec[dec["dim"] == dim]
    if not len(sub):
        continue
    DEC[dim] = {H: [float(sub[(sub.H == H) & (sub.decile == k)]["exw"].iloc[0]) * 100 for k in range(1, 11)]
                for H in hs if len(sub[sub.H == H])}

# 3) D1 左侧对照（H=10 与 H=20）
left_rows = []
for dim in DIM_CN:
    if dim not in DEC:
        continue
    for H in [10, 20]:
        if H not in DEC[dim]:
            continue
        s = dec[(dec["dim"] == dim) & (dec.H == H)].sort_values("decile")
        left_rows.append(dict(
            dim=dim, cn=DIM_CN[dim], H=H, kind=("技术型" if dim in TECH else "价格型"),
            D1=float(s[s.decile == 1]["exw"].iloc[0]) * 100,
            t1=float(s[s.decile == 1]["t"].iloc[0]),
            D5=float(s[s.decile == 5]["exw"].iloc[0]) * 100,
            t5=float(s[s.decile == 5]["t"].iloc[0]),
            D10=float(s[s.decile == 10]["exw"].iloc[0]) * 100,
            t10=float(s[s.decile == 10]["t"].iloc[0]),
        ))
LEFT = pd.DataFrame(left_rows)

# 4) 单条件归因（用 alt 里的「组合」行近似 —— 直接从 dec/alt 取）
#    单条件用 diag 的固定四条件口径：从 alt 的 [无缩量] 等无法得到，改用 decile 的 D1/D5/D10 + 组合
COMB = pd.DataFrame([
    dict(cond="超跌 hist_z<-1.5", H=20, exw=-0.585, t=-8.56),
    dict(cond="缩量 volz20<-1", H=20, exw=0.012, t=0.12),
    dict(cond="没涨 ret20<0", H=20, exw=0.111, t=3.25),
    dict(cond="低波 hv20<中位", H=20, exw=0.006, t=0.09),
    dict(cond="超跌+缩量", H=20, exw=-0.918, t=-8.90),
    dict(cond="超跌+缩量+没涨", H=20, exw=-0.820, t=-7.14),
    dict(cond="超跌+缩量+没涨+低波", H=20, exw=-0.223, t=-1.73),
    dict(cond="+ 再剔 D1（R0 完整）", H=20, exw=-0.303, t=-2.43),
    dict(cond="+ 但不剔 D1", H=20, exw=-0.218, t=-1.73),
    dict(cond="R0 但仅 D1", H=20, exw=0.820, t=2.70),
    dict(cond="缩量+没涨+低波（无超跌）", H=20, exw=-0.332, t=-4.50),
    dict(cond="超跌+没涨+低波（无缩量）", H=20, exw=-0.373, t=-3.62),
])

# 5) 三段样本外
SEGS = ["训练2015-2022", "验证2023-2024", "测试2025-2026"]
SEG_ENT = ["R0_完整(超跌+缩量+没涨+低波+非D1)", "R5b_仅D1(完整)", "R8_仅缩量+没涨+低波(无超跌)"]
seg_sel = seg[(seg.entry.isin(SEG_ENT)) & (seg.exit == "XA_固定持有10日")]
seg_tab = seg_sel.pivot_table(index="entry", columns="seg", values="cagr", aggfunc="first").reindex(SEG_ENT)
seg_exw = seg_sel.pivot_table(index="entry", columns="seg", values="exw", aggfunc="first").reindex(SEG_ENT)

# 6) Walk-Forward（R0 固定持有10日）
wfx = wf[(wf.entry == "R0_完整(超跌+缩量+没涨+低波+非D1)") & (wf.exit == "XA_固定持有10日")].copy()
wfx["test"] = wfx["test"].astype(str)

# 7) 邻域
neigh_ma = neigh[neigh.variant.str.startswith("距MA60")].copy()
neigh_vz = neigh[neigh.variant.str.startswith("volz20")].copy()

# 8) 分年度
yr_sel = year[year.variant.isin(["用户规格(hist_z<-1.5)", "价格超跌(距MA60<-20%)"])].copy()
yr_sel["variant"] = yr_sel["variant"].replace({"用户规格(hist_z<-1.5)": "用户规格 hist_z<-1.5",
                                              "价格超跌(距MA60<-20%)": "价格型 距MA60<-20%"})

# 9) 超跌定义对比（取 H=10，三种组合：剔D1 / 不剔D1 / 无缩量）
alt_h10 = alt[alt.H == 10].copy()

# 10) D1 对照
d1 = d1.copy()

# 11) 成本
cost = cost.copy()

# ---------------------------------------------------------------- HTML 片段
T = {}

T["ee"] = table(ee.assign(entry=ee.entry.map(ee_short)), [
    ("entry", "Entry 变体", fplain),
    ("exit", "Exit", lambda v, r: (v.replace("XA_", "").replace("XD_", ""), "")),
    ("n_sig", "信号数", fint),
    ("cagr", "CAGR", fpct), ("mdd", "最大回撤", fpct),
    ("win", "胜率", fpct), ("sharpe", "Sharpe", fnum),
    ("exw", "日加权超额", fpct), ("t_exw", "t", fnum),
])

T["left"] = table(LEFT.sort_values(["H", "kind", "D1"], ascending=[True, True, False]), [
    ("cn", "维度", fplain), ("kind", "类型", fplain), ("H", "持有期", lambda v, r: (f"{int(v)}日", "")),
    ("D1", "D1 最左端", fpct), ("t1", "t", fnum),
    ("D5", "D5 中位", fpct), ("t5", "t", fnum),
    ("D10", "D10 最右端", fpct), ("t10", "t", fnum),
])

T["comb"] = table(COMB, [
    ("cond", "条件 / 组合", fplain), ("exw", "H=20 超额", lambda v, r: (f"{v:+.3f}%", cls(v))),
    ("t", "t", fnum),
])

T["seg"] = table(
    pd.DataFrame([dict(entry=ee_short[e],
                       tr=float(seg_tab.loc[e, "训练2015-2022"]), va=float(seg_tab.loc[e, "验证2023-2024"]),
                       te=float(seg_tab.loc[e, "测试2025-2026"]),
                       n1=int(seg_sel[(seg_sel.entry == e) & (seg_sel.seg == "训练2015-2022")]["n_sig"].iloc[0]),
                       n2=int(seg_sel[(seg_sel.entry == e) & (seg_sel.seg == "验证2023-2024")]["n_sig"].iloc[0]),
                       n3=int(seg_sel[(seg_sel.entry == e) & (seg_sel.seg == "测试2025-2026")]["n_sig"].iloc[0]))
                 for e in SEG_ENT]), [
        ("entry", "Entry（Exit = 固定持有10日）", fplain),
        ("tr", "训练 2015-2022", fpct), ("va", "验证 2023-2024", fpct), ("te", "测试 2025-2026", fpct),
        ("n1", "n", fint), ("n2", "n", fint), ("n3", "n", fint),
    ])

T["wf"] = table(wfx, [
    ("train", "训练窗口", fplain), ("test", "样本外年", fplain),
    ("n_sig", "信号数", fint), ("cagr", "CAGR", fpct), ("win", "胜率", fpct),
    ("exw", "日加权超额", fpct), ("t_exw", "t", fnum),
])

T["d1"] = table(d1, [
    ("group", "分组", fplain), ("n_sig", "信号数", fint),
    ("cagr", "CAGR", fpct), ("mdd", "最大回撤", fpct), ("win", "胜率", fpct),
    ("exw", "日加权超额", fpct), ("t_exw", "t", fnum),
])

T["bench"] = table(bench, [
    ("bench", "基准 / 单条件", fplain), ("n_sig", "信号数", fint),
    ("cagr", "CAGR", fpct), ("mdd", "最大回撤", fpct), ("win", "胜率", fpct),
    ("exw", "日加权超额", lambda v, r: (pct(v), "") if np.isfinite(v) else ("—", "")),
    ("t_exw", "t", lambda v, r: (num(v), "") if np.isfinite(v) else ("—", "")),
])

T["cost"] = table(cost, [
    ("rt_cost", "双边成本", lambda v, r: (f"{v*100:.1f}%", "")),
    ("cagr", "CAGR", fpct), ("mdd", "最大回撤", fpct), ("win", "胜率", fpct),
])

T["neigh"] = table(pd.concat([neigh_ma, neigh_vz]), [
    ("variant", "阈值", fplain), ("n", "样本数", fint),
    ("exw5", "H=5 超额", fpct), ("t5", "t", fnum),
    ("exw10", "H=10 超额", fpct), ("t10", "t", fnum),
    ("exw20", "H=20 超额", fpct), ("t20", "t", fnum),
])

T["year"] = table(yr_sel.sort_values(["year", "variant"]), [
    ("year", "年份", lambda v, r: (str(int(v)), "")),
    ("variant", "变体", fplain), ("n_sig", "信号数", fint),
    ("exw10", "H=10 超额", fpct), ("t10", "t", fnum),
    ("exw20", "H=20 超额", fpct), ("t20", "t", fnum),
])

# 修正后的逐年（日度链式基准）
T["year2"] = table(year2[year2.H == 20].sort_values(["variant", "year"]), [
    ("variant", "变体", fplain), ("year", "年份", lambda v, r: (str(int(v)), "")),
    ("n", "信号数", fint), ("exw", "H=20 超额", fpct), ("t", "t", fnum),
])

T["bias"] = table(bias, [
    ("H", "持有期", lambda v, r: (f"{int(v)}日", "")),
    ("n_tail_missing", "末端缺失天数", fint),
    ("cagr_wrong_compound", "错误复利", fpct),
    ("cagr_daily_equiv", "日等效（正确）", fpct),
    ("cagr_daily_equiv_cut", "排除末尾H天", fpct),
    ("cut_delta", "Δ（截断偏差）", lambda v, r: (f"{v*100:+.6f} pp", "mute")),
])

T["robust"] = table(robust, [
    ("variant", "变体", fplain), ("H", "持有期", lambda v, r: (f"{int(v)}日", "")),
    ("exw_new", "新基准（日度链式）", fpct), ("t_new", "t", fnum),
    ("exw_old", "旧基准 mk{H}", fpct), ("t_old", "t", fnum),
])

T["alt"] = table(alt_h10, [
    ("over_def", "超跌定义", fplain), ("combo", "组合", fplain),
    ("n", "样本数", fint), ("gross", "绝对收益", fpct), ("exw", "超额", fpct), ("t", "t", fnum),
])

# ---------------------------------------------------------------- 组装
HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股「超跌 + 极端缩量」反转策略研究报告（V3）</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
:root{{
  --bg:#f5f6f8; --card:#ffffff; --ink:#1a1d21; --ink2:#4a5058; --ink3:#828a94;
  --line:#e3e6ea; --line2:#eef0f3; --up:#d64545; --dn:#2e9e5b; --accent:#1f5fa8;
  --warn:#c0392b; --chipbg:#f0f2f5;
}}
*{{box-sizing:border-box;}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  font-size:14px;line-height:1.75;-webkit-font-smoothing:antialiased;}}
.wrap{{max-width:1140px;margin:0 auto;padding:28px 22px 80px;}}
h1{{font-size:25px;line-height:1.4;margin:0 0 6px;letter-spacing:-.3px;}}
h2{{font-size:19px;margin:40px 0 12px;padding-left:11px;border-left:4px solid var(--accent);letter-spacing:-.2px;}}
h3{{font-size:15.5px;margin:24px 0 8px;color:var(--ink);}}
h4{{font-size:14px;margin:18px 0 6px;color:var(--ink2);font-weight:600;}}
p{{margin:8px 0;color:var(--ink2);}}
ul,ol{{margin:8px 0;padding-left:22px;color:var(--ink2);}}
li{{margin:4px 0;}}
.sub{{color:var(--ink3);font-size:12.5px;margin:0 0 18px;}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:14px 0;
  box-shadow:0 1px 2px rgba(20,26,34,.03);}}
.tldr{{background:linear-gradient(180deg,#fff 0%,#fbfcfd 100%);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin:18px 0 8px;}}
.tldr h2{{margin-top:0;border:0;padding:0;font-size:18px;}}
.verdict{{border-left:4px solid var(--warn);background:#fdf6f5;border-radius:0 8px 8px 0;padding:14px 18px;margin:16px 0;}}
.verdict b{{color:var(--warn);}}
.verdict.ok{{border-left-color:var(--dn);background:#f4faf6;}}
.verdict.ok b{{color:#26744a;}}
.grid{{display:grid;gap:12px;}}
.g4{{grid-template-columns:repeat(4,1fr);}}
.g3{{grid-template-columns:repeat(3,1fr);}}
.g2{{grid-template-columns:repeat(2,1fr);}}
@media(max-width:820px){{.g4,.g3,.g2{{grid-template-columns:1fr 1fr;}}}}
@media(max-width:560px){{.g4,.g3,.g2{{grid-template-columns:1fr;}}}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 15px;}}
.kpi .lab{{font-size:12px;color:var(--ink3);margin-bottom:5px;line-height:1.4;}}
.kpi .val{{font-size:21px;font-weight:650;letter-spacing:-.5px;font-variant-numeric:tabular-nums;}}
.kpi .note{{font-size:11.5px;color:var(--ink3);margin-top:3px;}}
.neg{{color:var(--dn);}} .pos{{color:var(--up);}} .mute{{color:var(--ink3);}}
.chart{{width:100%;height:372px;margin:6px 0 2px;}}
.chart.tall{{height:470px;}} .chart.short{{height:300px;}} .chart.mini{{height:250px;}}
.cap{{font-size:12px;color:var(--ink3);margin:2px 0 14px;padding-left:2px;}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums;}}
th,td{{padding:7px 9px;border-bottom:1px solid var(--line2);text-align:right;white-space:nowrap;}}
th{{background:#f7f8fa;font-weight:600;color:var(--ink2);text-align:right;position:sticky;top:0;z-index:2;}}
th:first-child,td:first-child{{text-align:left;}}
tbody tr:hover{{background:#fafbfc;}}
.tscroll{{max-height:520px;overflow:auto;border:1px solid var(--line);border-radius:8px;}}
.pill{{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11.5px;background:var(--chipbg);color:var(--ink2);margin-right:5px;}}
.pill.bad{{background:#fdf0ee;color:#b8402f;}}
.pill.ok{{background:#eef7f1;color:#26744a;}}
.note{{font-size:12.5px;color:var(--ink3);}}
hr.sep{{border:0;border-top:1px solid var(--line);margin:26px 0;}}
.foot{{font-size:12px;color:var(--ink3);margin-top:34px;padding-top:14px;border-top:1px solid var(--line);}}
code{{background:#f2f4f7;padding:1px 5px;border-radius:4px;font-size:12.5px;}}
</style>
</head>
<body>
<div class="wrap">

<h1>A股「超跌 + 极端缩量」反转策略研究报告</h1>
<p class="sub">V3 · 对用户规格策略的完整回测、归因与前提检验 · 2015-01 ~ 2026-09 · 全A股 5,304 只 · 2,871 个交易日 · 1,053 万个股票-日观测</p>

<div class="tldr">
<h2>结论速览</h2>
<div class="verdict">
<p><b>裁决：该策略不成立。更关键的是 —— 它失败的原因与设计假设几乎逐条相反。</b></p>
<p>用户规格的四条 Entry 条件中，<b>「MACD 柱超跌」与「低波动率」两条的左尾超额是负的</b>，而唯一真正有效的「短期反转」维度（过去 20 日涨幅）只被用作排除条件。用 MACD 柱度量「超跌」，恰好选到了全维度体系中<b>唯一一个左尾为负</b>的动能型指标。</p>
</div>

<div class="grid g4" style="margin-top:14px">
<div class="kpi"><div class="lab">R0 完整策略 CAGR<br>固定持有 10 日 / 0.3% 双边成本</div><div class="val neg">−14.49%</div><div class="note">日加权超额 −0.108%，t = −6.40</div></div>
<div class="kpi"><div class="lab">R0 完整策略 CAGR<br>固定持有 5 日</div><div class="val neg">−26.15%</div><div class="note">日加权超额 −0.161%，t = −8.38</div></div>
<div class="kpi"><div class="lab">对照组：随机选股<br>同持仓期 / 同成本</div><div class="val pos">+1.4% ~ +3.2%</div><div class="note">3 次独立抽样</div></div>
<div class="kpi"><div class="lab">对照组：全A 等权基准<br>2015-01 ~ 2026-09</div><div class="val pos">+12.67%</div><div class="note">最大回撤 −59.2%</div></div>
</div>

<div class="card" style="margin-top:14px">
<h3 style="margin-top:0">四条核心发现</h3>
<ol>
<li><b>用户规格全线为负，且跑输随机选股约 15~18 pp/年。</b>11 个 Entry 变体 × 4 条 Exit 共 44 个组合，<b>无一为正</b>；「固定持有 5 日」比「持有 10 日」更差，说明亏损集中在信号后的前 5 个交易日。</li>
<li><b>「左侧符号分化」是本次最干净的发现。</b>在 10 个维度的逐日截面十分位检验中，<b>所有技术型指标（MACD 柱、Volume Z、VR20、量能趋势、波动率）的最左端 D1 都为负超额</b>；而<b>所有价格型指标（5/20/60 日涨幅、距 MA60、相对强弱）的最左端 D1 都为正超额</b>。用户用 <code>hist_z &lt; -1.5</code> 定义「超跌」，恰好落在前者。</li>
<li><b>`hist_z` 十分位呈倒 U 型，不是单调负。</b>峰值在中位 D5（H=20 超额 +0.264%，t = +13.19），两个尾部都差（D1 −0.135% / t = −2.94，D10 −1.028% / t = −17.82）。因此「简单取反」在结构上不成立。</li>
<li><b>唯一有正超额的组合是价格型深度超跌</b>（距 MA60 &lt; −20% + 缩量 + 没涨 + 低波），H=20 超额 +1.151%（t = +2.69，日度链式基准）。但邻域上下都转负、分年度方向不一致、且身处 26+ 个变体的多重检验之下 —— <b>判定为过拟合，不足以采信。</b></li>
<li><b>方法学自我纠错</b>：初版怀疑的「<code>fwd</code> 末端截断偏差」经实测证伪（Δ 恒为 0.000000 pp）；但顺带查出真问题 —— 初版把重叠的 H 日收益当日收益逐日复利，导致 <code>mk10</code> 年化虚高到 +165%（正确为 +10.27%）。已改用日度链式基准重做逐年归因，<b>结论方向不变、量级略强</b>（用户规格 H=20：t = −2.43 → −3.34）。</li>
</ol>
</div>
</div>

<h2>一、研究设计与口径</h2>

<div class="card">
<h3>1.1 用户规格 → 面板字段映射</h3>
<table>
<thead><tr><th>用户规格</th><th>本次实现（v2_panel.parquet 字段）</th><th>阈值</th></tr></thead>
<tbody>
<tr><td>MACD 柱超跌低位</td><td><code>hist_z &lt; -1.5</code>（MACD 柱 / 个股 60 日滚动标准差）</td><td>约对应 10~12% 分位</td></tr>
<tr><td>量能极端缩量、剔除放量</td><td><code>volz20 &lt; -1.0</code>（Volume Z，分母不含当日）</td><td>备选：<code>vr20m</code> &lt; 训练段 20 分位 = 0.58803</td></tr>
<tr><td>过去 20 日涨幅 &lt; 0%</td><td><code>ret20 &lt; 0</code></td><td>严格 0 阈值</td></tr>
<tr><td>低波动率</td><td><code>hv20</code> &lt; 训练段中位数</td><td>0.39881（仅用 2015-2022 估计）</td></tr>
<tr><td>剔除市值最小 10%</td><td><code>size_grp &gt; 0</code>（D1 = 最小市值组）</td><td>以 <code>amt20_lag</code> 十分位代理</td></tr>
<tr><td>固定持仓 5~10 日强制平仓</td><td><code>XA_固定持有5日</code> / <code>XA_固定持有10日</code></td><td>不使用趋势跟随退出</td></tr>
<tr><td>2~3 倍 ATR 宽止损</td><td><code>XD_ATR2倍止损</code> / <code>XD_ATR3倍止损</code></td><td>ATR 以绝对价格表达，不用 ATR%</td></tr>
<tr><td>0.3% 双边交易成本</td><td>按持仓天数均摊：每日承担 <code>0.003 / 持仓天数</code></td><td>单笔持仓整个持有期恰好 0.3%</td></tr>
</tbody>
</table>
</div>

<div class="card">
<h3>1.2 时序与组合口径（严格无未来函数）</h3>
<ul>
<li><b>信号</b>：T 日收盘生成 → <b>T+1 日开盘买入</b>；T+1 开盘一字涨停则放弃该信号。</li>
<li><b>退出</b>：到达持有上限或触发 ATR 止损在 T+k 日收盘确认 → <b>T+k+1 日开盘卖出</b>；跌停无法卖出则顺延至下一个可卖日。</li>
<li><b>组合</b>：日度再平衡等权。每日组合收益 = 全部在仓标的当日 <code>oret</code> 的等权均值；<code>oret[p] = open[p+1]/open[p] − 1</code>。</li>
<li><b>基准</b>：同交易日全部 clean 标的 <code>fwd{H}</code> 等权均值（<code>mk{H}</code>），这是本策略 α 的唯一对照口径。</li>
<li><b>阈值防前视</b>：所有分位/中位数阈值仅用 2015-2022 训练段估计，2023 年以后完全样本外。</li>
</ul>
<p class="note">机器正确性已回归验证：令「信号 = 全部可买标的、持有 10 日、成本 0」，组合 CAGR = +11.65%，与全A等权基准 +12.67% 相差约 1 pp，差异来自持仓加权与股票加权的口径差别 —— 符合预期。</p>
</div>

<h2>二、策略实测：Entry × Exit 全网格</h2>

<div class="card">
<div id="c-ee" class="chart tall"></div>
<p class="cap">图 1 · 11 个 Entry 变体 × 2 条固定持有 Exit 的年化收益（CAGR，0.3% 双边成本）。全部 44 个组合均为负。</p>
</div>

<div class="card">
{T["ee"]}
<p class="cap">表 1 · 完整 Entry × Exit 网格。t 为按交易日聚类的超额收益 t 值。</p>
</div>

<div class="card">
<h3>2.1 读表要点</h3>
<ul>
<li><b>「固定持有 5 日」全线劣于「固定持有 10 日」</b>（R0：−26.15% vs −14.49%）→ 亏损集中在信号后前 5 日，即<b>买入后的短期仍在下跌</b>，这本身就是「超跌不是底」的直接证据。</li>
<li><b>去掉「低波」条件后显著变差</b>（R1 去低波：−26.81% vs R0 的 −14.49%）→ 「低波」在当前体系里是<b>唯一有缓冲作用的条件</b>，但方向是「少亏」而非「赚钱」。</li>
<li><b>只留「超跌 + 缩量」时最差</b>（R3：持有 5 日 CAGR −49.07%，超额 −0.318%，t = −16.72）→ 两个条件叠加放大了负超额，而它们各自单独看几乎为零或轻微负。</li>
<li><b>去掉「超跌」条件反而改善</b>（R8：持有 10 日 −8.43%，超额 −0.085%）。</li>
<li><b>ATR 止损没有挽救策略</b>：2 倍与 3 倍 ATR 止损的结果与固定持有 10 日几乎相同（−14.76% / −13.99% vs −14.49%），说明「宽止损」在当前信号上没有可提取的信息。</li>
</ul>
</div>

<h2>三、归因：为什么全线为负</h2>

<div class="card">
<h3>3.1 逐条件单因子前向超额（同一 <code>fwd</code> 口径，未扣成本）</h3>
{T["comb"]}
<p class="cap">表 2 · 各条件与前向 20 日超额（相对同交易日全A 等权）。关键：<b>「超跌」是最强单一负贡献</b>；「缩量」单独几乎为零；「没涨」微正。</p>
</div>

<div class="card">
<h3>3.2 核心证据：十个维度的「左侧符号分化」</h3>
<div id="c-left" class="chart"></div>
<p class="cap">图 2 · 每个维度逐日截面十分位中，<b>最左端 D1</b> 的前向 20 日超额（相对全A等权）。技术型指标（暖色）全为负，价格型指标（冷色）全为正 —— 这是本次研究最稳定、最可解释的结构。</p>
{T["left"]}
<p class="cap">表 3 · 十个维度的 D1 / D5 / D10 对照（H = 10 日与 20 日）。</p>
</div>

<div class="card">
<h3>3.3 十个维度的十分位曲线（H = 20 日）</h3>
<div id="c-curve" class="chart tall"></div>
<p class="cap">图 3 · 全部维度都呈倒 U 型（山形），峰值集中在中位 D4~D6。不同维度之间的差别在于<b>左端 D1 是正是负</b> —— 而这恰好是策略的入场位置。</p>
</div>

<div class="card">
<h3>3.4 `hist_z` 十分位明细（检验「超跌 = 买点」）</h3>
<div id="c-histz" class="chart"></div>
<p class="cap">图 4 · MACD 柱位置十分位的前向超额。D1（最超跌）显著为负、D10（最强）大幅为负、中位 D5 最优 —— <b>「MACD 柱超跌」在 A 股不是买点，而是弱势信号</b>。</p>
<table>
<thead><tr><th>hist_z 十分位</th><th>H=5 超额</th><th>t</th><th>H=10 超额</th><th>t</th><th>H=20 超额</th><th>t</th><th>H=60 超额</th><th>t</th></tr></thead>
<tbody>
{"".join(
    "<tr><td>D%d%s</td>" % (k, "（最超跌）" if k == 1 else ("（最强）" if k == 10 else ("（中位，最优）" if k == 5 else "")))
    + "".join(
        "<td><span class='%s'>%s</span></td><td>%s</td>" % (
            cls(DEC["hist_z"][H][k-1]), f"{DEC['hist_z'][H][k-1]:+.3f}%",
            num(float(dec[(dec.dim=='hist_z')&(dec.H==H)&(dec.decile==k)]['t'].iloc[0])))
        for H in [5,10,20,60])
    + "</tr>" for k in range(1, 11))}
</tbody></table>
<p class="cap">表 4 · <code>hist_z</code> 十分位 × 四个持有期。四个持有期结构一致。</p>
</div>

<h2>四、机制：为什么「MACD 柱超跌」不是买点</h2>

<div class="card">
<p>这个结论初看反直觉，但机制是清楚的：</p>
<ol>
<li><b>`hist_z` 不是「价格便宜」，而是「下跌动能仍在加速」。</b><code>hist_z = MACD柱 / 该股自身 60 日 MACD 柱标准差</code>。它为 −1.5 意味着<b>相对于这只股票自己最近的波动，下跌动能处于极端状态</b> —— 通常发生在下跌的加速段，而不是止跌企稳段。真正预示反转的是<b>价格的绝对位置</b>（距 MA60 多远、过去 20 日跌了多少），不是<b>动能的相对强度</b>。</li>
<li><b>同一只股票，价格型超跌与动能型超跌的时间点不同。</b>价格型超跌出现在下跌的<b>中后段</b>（跌幅已累积）；动能型超跌出现在下跌的<b>加速段</b>（跌幅还在扩大）。前者买在尾声，后者买在中途。数据完全支持这一点：<code>px_ma60_pct</code> 的 D1 超额 +0.565%（t = +9.0），<code>hist_z</code> 的 D1 超额 −0.135%（t = −2.94）。</li>
<li><b>「剔除所有放量标的」这条要求也值得重新审视。</b>量能维度的左端同样是负的：<code>vr20m</code> 的 D1 超额 −0.169%（t = −4.4）、<code>vt_5_20</code> 的 D1 −0.106%（t = −2.7）。<b>极端缩量同样不是买点。</b></li>
<li><b>「低波动率」也是负贡献。</b><code>hv20</code> 的 D1（最低波）H=20 超额 −0.169%（t = −2.3）。A 股存在明显的<b>高波动溢价/彩票偏好</b>，主动筛掉高波动股票等于放弃了一部分正溢价。</li>
</ol>
<div class="verdict">
<p><b>一句话：用户在正确的方向（短期反转）上，用错了三把尺子。</b>真正有效的一把——过去 20 日涨幅——只被用作排除条件。</p>
</div>
</div>

<h2>五、D1 悖论：用户要求的「剔除最小市值 10%」让策略更差</h2>

<div class="card">
{T["d1"]}
<p class="cap">表 5 · 市值分组对照（Exit = 固定持有 10 日）。</p>
<div id="c-d1" class="chart short"></div>
<p class="cap">图 5 · 各组 CAGR 对照。D1（最小市值）显著优于其他所有组，虽然仍为负。</p>
<ul>
<li><b>「仅 D1」CAGR −4.78%，是全部 5 个分组中唯一接近盈亏平衡的</b>（超额 −0.060%，t = −1.98，勉强不显著）。</li>
<li><b>剔除 D1 后策略更差</b>：−14.49% vs 保留全部 −13.77%。</li>
<li>这与 V2 报告「正超额仅存在于 D1」的发现方向一致，但口径不同（V2 用单持有期超额，本策略用日度再平衡净收益），因此量级不可直接比较。</li>
<li><b>结论</b>：从风险控制角度「避开小市值」是合理的；但从统计角度，<b>这条风控恰好切掉了策略唯一相对不那么差的角落</b>。二者需要用户自己权衡取舍——本次不做价值判断，只报告事实。</li>
</ul>
</div>

<h2>六、成本、基准与样本外</h2>

<div class="card">
<h3>6.1 成本敏感性——成本不是主因</h3>
{T["cost"]}
<div id="c-cost" class="chart short"></div>
<p class="cap">图 6 · R0 完整策略（固定持有 10 日）在不同双边成本下的 CAGR。<b>零成本下毛收益已经是 −8.39%</b>，成本只贡献了约 6 pp 的恶化。选股逻辑本身有问题，不是交易频率太高。</p>
</div>

<div class="card">
<h3>6.2 基准与单条件对照</h3>
{T["bench"]}
<div id="c-bench" class="chart short"></div>
<p class="cap">图 7 · 全A等权基准、随机选股、四条单条件的 CAGR 对照。<b>R0 策略低于随机选股约 15~18 pp/年。</b></p>
</div>

<div class="card">
<h3>6.3 三段样本外</h3>
{T["seg"]}
<p class="cap">表 6 · 三段样本外的 CAGR。<b>三段全部为负，方向高度一致</b> —— 这是本次最可靠的负面证据。</p>
</div>

<div class="card">
<h3>6.4 Walk-Forward（R0 完整 / 固定持有 10 日）</h3>
{T["wf"]}
<div id="c-wf" class="chart short"></div>
<p class="cap">图 8 · 7 个滚动窗口的样本外表现。只有 1 个窗口（2015-2019 训练 → 2020 测试）为正，其余全部为负。</p>
</div>

<h2>七、唯一正线索的尽调：价格型深度超跌</h2>

<div class="card">
<p>把所有「超跌」定义换成<b>价格型</b>后，出现了一个三时段方向一致的候选组合：<code>距MA60 &lt; −20% + 缩量 + 没涨 + 低波 + 非D1</code>。</p>
{T["alt"]}
<p class="cap">表 7 · 11 种「超跌」定义 × 3 种组合（H = 10 日）。<code>gross</code> 为绝对收益，<code>exw</code> 为相对全A等权的超额。</p>
</div>

<div class="card">
<h3>7.1 邻域稳定性 —— 不合格</h3>
{T["neigh"]}
<div id="c-neigh" class="chart"></div>
<p class="cap">图 9 · 阈值上下浮动。<b>只有 −20% 这一个是正的</b>：−10% 显著为负（t = −5.21）、−15% 转负、−25% 不显著、−35% 大幅转负。这是典型的「孤立最优点」，是过拟合的形态学特征。</p>
<p class="note">量能阈值（volz20）与涨幅阈值（ret20）的邻域则<b>全段为负或无显著性</b>，没有任何一个点位转正。</p>
</div>

<div class="card">
<h3>7.2 分年度 —— 方向不一致</h3>
<p>先用<b>初版基准（<code>mk{H}</code>）</b>的结果：</p>
{T["year"]}
<div id="c-year" class="chart short"></div>
<p class="cap">图 10 · 用户规格（hist_z&lt;−1.5）与价格型超跌（距MA60&lt;−20%）的逐年 H=20 超额（初版基准）。</p>
<h4>修正基准（日度链式）后的逐年结果</h4>
<p>第八节说明了初版基准在逐年口径上不可靠（2015 年差 62 pp）。改用日度链式基准后重做：</p>
{T["year2"]}
<p class="cap">表 7b · 日度链式基准下的逐年 H=20 超额（完整 105 行见 <code>output/v3_year_v2.csv</code>）。</p>
<table>
<thead><tr><th>变体</th><th>H=20 正超额年数</th><th>负超额年数</th><th>判定</th></tr></thead>
<tbody>
<tr><td>用户规格 hist_z&lt;−1.5</td><td>6</td><td>6</td><td class="neg">方向完全对半 —— 无稳定环境依赖</td></tr>
<tr><td>价格型 距MA60&lt;−20%</td><td>7</td><td>4</td><td class="mute">偏正但年数太少，且单年 n 极小（2015 仅 8 个信号）</td></tr>
<tr><td>无超跌（缩量+没涨+低波）</td><td>4</td><td>8</td><td class="neg">明确偏负</td></tr>
</tbody></table>
<p class="cap">表 7c · 正负年份统计。<b>用户规格恰好 6 正 6 负</b> —— 这比「全负」更能说明问题：它在时间上是随机的。</p>
<p class="note">注意：价格型超跌的逐年显著性其实很弱 —— 2017/2019/2021/2022/2023/2025 的 t 值全部落在 +1.45 ~ +1.89 区间，<b>没有任何一年单独显著</b>。2026 年 t = +2.37 是唯一接近显著的，但 2026 年样本仅到 9 月。</p>
</div>

<div class="card">
<h3>7.3 多重检验警告</h3>
<p>本次研究共测试了 <b>26 个以上</b> 策略变体：</p>
<ul>
<li>10 个「超跌」定义（<code>hist_z</code> 四档 + 涨幅两档 + 距MA60 两档 + 距60日高 + 无超跌）</li>
<li>6 个邻域阈值（距MA60 × 6）</li>
<li>10 个维度的十分位分组</li>
</ul>
<p>在 26 次独立检验中，<b>至少出现 1 个 |t| &gt; 2.6 的概率约为 0.19</b>。也就是说，仅凭「找到一个显著点」并不构成证据。</p>
<div class="verdict">
<p><b>判定：<code>距MA60 &lt; −20%</code> 这一条线索 ① 邻域不稳定 ② 分年度方向不一致 ③ 身处多重检验之下 ④ 样本量极小（训练段 8 年仅 4,325 个股票-日观测）。<br>不足以称为可交易 Alpha，倾向于判定为过拟合。需要独立的、更大样本的验证才能重新评估。</b></p>
</div>
<p class="note">补充说明：该组合的绝对收益（<code>gross</code>）是正的（H=20 约 +2.75%），但全A等权基准同期约 +1.5%，因此大部分收益来自市场 β 而非选股 α。核心取舍在于超额是否稳定，而这正是它不满足的。</p>
</div>

<h2>八、方法学校正：基准口径的两个陷阱</h2>

<div class="card">
<p>本节记录对 V3 初版的一处<b>自我纠错</b>。初版曾怀疑 <code>fwd10</code>/<code>fwd20</code> 的末端 NaN 造成「截断偏差」（只有涨后样本留存 → 抬高基准），并据此提示逐年归因不可靠。<b>实测证伪了这个怀疑，但顺带发现了一个更严重的真问题。</b></p>

<h3>8.1 已证伪：「截断偏差」不存在</h3>
{T["bias"]}
<p class="cap">表 8 · 基准复利口径诊断。<b>最后一列 Δ 恒为 0.000000 pp</b> —— 排除末尾 H 天后 CAGR 完全不变，截断偏差不存在。</p>
<p>原因：<code>mk{H}</code> 是<b>每日独立</b>的横截面均值。末端 H 天只是<b>样本天数变少</b>，不会改变剩余交易日上「哪些股票被纳入平均」——被纳入的仍是当日全部有效标的。<b>这项修正不需要做。</b></p>

<h3>8.2 真问题：把 H 日收益当日收益逐日复利</h3>
<div class="verdict">
<p><b>初版报告的「mk10 全样本 CAGR = +33%」是算错的。</b>那是把 10 日收益当成日收益连乘的结果。实测该错误算法在 H=20 时会给出 <b>+546%</b>、H=60 时 <b>+9914%</b>。</p>
</div>
<p>正确做法是把重叠的 H 日收益转成日等效收益后再复利：<code>(1+r)^(1/H)</code>。修正后：</p>
<table>
<thead><tr><th>H</th><th>错误复利</th><th>日等效（正确）</th></tr></thead>
<tbody>
<tr><td>1 日</td><td>+12.10%</td><td>+12.10% <span class="mute">（H=1 时两种算法恒等）</span></td></tr>
<tr><td>5 日</td><td class="neg">+65.59%</td><td>+10.61%</td></tr>
<tr><td>10 日</td><td class="neg">+165.83%</td><td>+10.27%</td></tr>
<tr><td>20 日</td><td class="neg">+546.41%</td><td>+9.78%</td></tr>
<tr><td>60 日</td><td class="neg">+9913.89%</td><td>+7.98%</td></tr>
</tbody></table>
<p class="cap">表 9 · H=1 是天然的对照组：两种算法在 H=1 时恒等，可以隔离出「口径错误」而非「数据错误」。</p>
<p class="note">修正后 <code>mk10</code> 全样本 CAGR = +10.27%，与真实全A等权（日度链式 +12.20%）同量级 —— <b>量级合理，说明面板数据本身没有异常</b>。</p>

<h3>8.3 重叠 H 日收益 ≠ 日度链式收益：逐年会差 62 pp</h3>
<p>更关键的是：<code>mk{H}</code> 是<b>重叠</b>的 H 日收益口径，而策略组合是<b>日度再平衡</b>、逐日复利。两者<b>只在 H=1 时严格一致</b>，H 越大偏离越大，且在<b>趋势市里偏离最剧烈</b>：</p>
<table>
<thead><tr><th>年份</th><th>日度链式</th><th>mk10 日等效</th><th>差</th></tr></thead>
<tbody>
<tr><td>2015</td><td>+115.64%</td><td>+53.37%</td><td class="neg"><b>−62.27 pp</b></td></tr>
<tr><td>2016</td><td>−8.53%</td><td>+13.09%</td><td class="neg">+21.62 pp</td></tr>
<tr><td>2025</td><td>+43.49%</td><td>+55.41%</td><td class="neg">+11.92 pp</td></tr>
<tr><td>2020</td><td>+21.67%</td><td>+14.02%</td><td>−7.65 pp</td></tr>
<tr><td>2017 / 2018 / 2019</td><td colspan="2" class="mute">差异均 &lt; 1 pp</td><td class="mute">—</td></tr>
</tbody></table>
<p class="cap">表 10 · 同一全A等权市场，两种基准口径的逐年差异。趋势越强偏离越大。</p>
<p>因此逐年归因改用<b>日度链式基准</b>（与策略的日度复利完全一致，且仅末尾 H 天缺失）：</p>
<pre style="background:#f7f8fa;border:1px solid var(--line);border-radius:6px;padding:10px 12px;font-size:12px;overflow:auto">mk_d = pd.Series(oret).groupby(day_idx).mean()          # 日度市场收益
cs   = np.concatenate([[0.0], np.cumsum(np.log1p(mk_d))])
# mkt_fwd_H[t] = expm1(cs[t+1+H] - cs[t+1])  ← 与 fwd{H}[t] 口径严格对齐</pre>

<h3>8.4 修正对结论的影响：方向不变，量级略强</h3>
{T["robust"]}
<p class="cap">表 11 · 新旧基准的全样本对照。差额 −0.006 ~ −0.125 pp（H=20 最大）。</p>
<ul>
<li><b>用户规格 H=20</b>：−0.303%（t = −2.43）→ <b>−0.428%（t = −3.34）</b> —— 负超额更明显、更显著。</li>
<li><b>价格型超跌 H=20</b>：+1.216%（t = +2.84）→ <b>+1.151%（t = +2.69）</b> —— 略减弱，但仍过 |t|&gt;2.6 边缘以下。</li>
<li><b>结论方向完全不变</b>，但报告引用 t 值时必须说明口径。</li>
</ul>
</div>

<h2>九、「如果全负，是不是反向操作就成立？」</h2>

<div class="card">
<p>这是本次需要单独回答的方法学问题。结论是：<b>不成立</b>。理由有六条，按重要性排序。</p>

<h3>9.1 先纠正一个关键事实：策略的绝对收益不是负的</h3>
<p>诊断表里的 <code>gross</code> 列（前向绝对收益）显示：R0 完整策略的 H=20 绝对毛收益是 <b>+0.359%</b>，而非负值。所谓「全负」指的是<b>相对全A等权的超额</b>为负（−0.303%），不是<b>赚钱与否</b>为负。</p>
<div class="verdict">
<p><b>负超额 ≠ 会下跌。</b>它说明的是「把同样的钱按市值权重买入全市场，收益更好」，而不是「这些股票会跌」。</p>
</div>
<p>在扣掉 0.3% 成本并按日度再平衡口径复利后，才会出现 −14.49% 的 CAGR。也就是说，<b>负收益是「负超额 × 复利 × 成本」三者叠加的结果，取反向只能消除第一项。</b></p>

<h3>9.2 多空成本是对称的，不是反向的</h3>
<p>做多亏 1% 时，做空拿到的是 +1% 的<b>毛</b>收益，但两边都要付 0.3% 往返成本。而 A 股做空还额外承担：</p>
<ul>
<li><b>融券费率年化 8%~10%</b>（券商融券业务普遍水平），远高于本策略年化约 3.8% 的超额幅度（−0.303% × 12.6 期），<b>融券成本一项就吃光并倒亏</b>。</li>
<li><b>券源稀缺</b>：本策略选中的是深度超跌、极端缩量的中小盘股，恰恰是最难融到券的一类标的。券源可得性本身就是硬约束。</li>
<li><b>印花税单边</b>（卖出方承担）在反向组合里方向不变，进一步恶化。</li>
</ul>

<h3>9.3 `hist_z` 是倒 U 型，不是单调负 —— 没有对称结构可取反</h3>
<p>如果是「越超跌越差」的单调关系，取反确实简单。但十分位检验显示的是<b>倒 U 型</b>：D1 −0.135%、D5 <b>+0.264%</b>、D10 −1.028%。</p>
<p>把信号取反，意味着<b>同时</b>放弃 D5 的最优区域、并把 D1/D10 两个尾部当成买点——而 D10（MACD 柱极强）的超额是 −1.028%（t = −17.82），是全部十个分位里最差的。取反后的组合净效应<b>无法预测，大概率仍然为负</b>。</p>

<h3>9.4 横截面负超额 ≠ 时序做空机会</h3>
<p>本次所有超额都是<b>横截面</b>口径（同一天内，选中的股票 vs 全市场等权）。它回答的是「选股能力」，不是「市场方向」。要做空获利，需要的是<b>时序</b>上的下跌预测能力。这两者在统计上是不同的东西。</p>
<p>具体到本次策略：市场基准 CAGR +12.67%，策略 CAGR −14.49%。做多这个策略等于<b>同时承受 β 和负 α</b>；但做空它并不等于 +14.49%，因为做空标的组合会同时暴露于市场 β（做空 β 为正的组合在市场上涨时亏钱），而 A 股 2015-2026 是上涨的市场。</p>

<h3>9.5 A 股融券可行性本身不支撑这个思路</h3>
<p>即使统计上成立，本策略持有期只有 5~10 个交易日，换手极高（R0 在 2,604 个交易日中有活跃仓位）。<b>融券开仓 + 平仓的券源匹配、费率、保证金占用</b>，在高频调仓下几乎不可能执行。</p>

<h3>9.6 「反转失败」的正确结论是换指标，不是反向</h3>
<p>数据给出的方向非常明确：</p>
<div class="verdict ok">
<p><b>用户的方向是对的，尺子错了。</b>「短期反转」在价格型维度上确实存在且显著（<code>ret20</code> D1 超额 +0.375%、t = +6.3；<code>ret60</code> D1 +0.431%、t = +7.2；<code>px_ma60_pct</code> D1 +0.565%、t = +9.0）。正确的下一步是<b>把「超跌」的定义从 <code>hist_z</code> 换成价格型指标</b>，而不是把整个策略反向。</p>
</div>
</div>

<h2>十、结论与后续建议</h2>

<div class="card">
<h3>10.1 直接回答用户的两个问题</h3>
<table>
<thead><tr><th>问题</th><th>结论</th><th>关键依据</th></tr></thead>
<tbody>
<tr><td>「超跌 + 极端缩量」反转策略是否成立？</td><td class="neg"><b>不成立</b></td><td>44 个 Entry×Exit 组合全负；跑输随机选股 15~18 pp/年；三段样本外全负；零成本毛收益已为 −8.39%</td></tr>
<tr><td>「A 股短周期呈现显著反转市」这一前提是否成立？</td><td class="mute"><b>部分成立，但需换尺子</b></td><td>价格型指标左侧全部为正（ret20/ret60/距MA60/RS20 的 D1 均为正超额）；技术型指标左侧全部为负</td></tr>
<tr><td>「MACD 柱超跌」是不是买点？</td><td class="neg"><b>不是</b></td><td>hist_z D1 超额 −0.135%（t = −2.94）；D5 中位才是峰值（+0.264%，t = +13.19）</td></tr>
<tr><td>「极端缩量」是不是买点？</td><td class="neg"><b>不是</b></td><td>vr20m D1 −0.169%（t = −4.4）、vt_5_20 D1 −0.106%（t = −2.7）、volz20 D1 −0.026%（不显著）</td></tr>
<tr><td>「低波动率」是不是有利条件？</td><td class="neg"><b>不是</b></td><td>hv20 最左端 D1 超额 −0.169%（t = −2.3）。但去掉该条件后策略从 −14.49% 恶化到 −26.81% —— 说明「低波」本身是负贡献，却同时起到减亏作用</td></tr>
<tr><td>「剔除最小市值 10%」是否合理？</td><td class="mute"><b>风险角度合理，统计角度反向</b></td><td>仅 D1 CAGR −4.78%，是唯一接近盈亏平衡的组；剔除 D1 后 −14.49%</td></tr>
<tr><td>「固定 5~10 日 + ATR 宽止损」是否有效？</td><td class="neg"><b>无效</b></td><td>5 日 −26.15% 劣于 10 日 −14.49%；ATR2/3 倍止损与固定持有几乎无差异（−14.76% / −13.99%）</td></tr>
<tr><td>「全部为负，是否反向就成立？」</td><td class="neg"><b>不成立</b></td><td>负超额 ≠ 负绝对收益（gross 为正）；融券年化 8~10% 吞掉 3.8% 的年化超额；hist_z 为倒 U 型无对称可取反；横截面负超额 ≠ 时序下跌</td></tr>
</tbody>
</table>
<p class="note">表中「超跌」相关结论在修正基准后量级略有变化（H=20 用户规格：−0.303% → −0.428%），但<b>方向与显著性判定完全不变</b>。详见第八节。</p>
</div>

<div class="card">
<h3>10.2 如果要继续研究，建议的下一步</h3>
<ol>
<li><b>把「超跌」的定义改成价格型。</b>用 <code>ret20</code> / <code>ret60</code> / <code>px_ma60_pct</code> 的截面分位替代 <code>hist_z</code>。注意最优区域是 D2~D6（次超跌），而非 D1（最超跌）——「极端」在本轮数据里始终不是最优。</li>
<li><b>放弃「极端缩量」这个条件。</b>量能维度的左侧在全部定义下都是负的。V2 报告里「缩量正超额」的线索是在「MACD 改善」事件之上成立的，脱离该前提后不再存在。</li>
<li><b>重新审视「低波」条件。</b>它在 A 股是负贡献，可能是主动放弃了高波动溢价。如果目标是减亏而非增效，可以保留；如果目标是超额，应当去掉。</li>
<li><b>统一基准口径。</b>后续研究一律用<b>日度链式基准</b>（<code>expm1(cs[t+1+H] − cs[t+1])</code>）而非重叠的 <code>mk{H}</code> —— 前者与策略的日度再平衡复利严格一致，且在趋势市里不会有几十 pp 的偏离。引用 <code>mk{H}</code> 年化时必须用 <code>(1+r)^(1/H)</code>。</li>
<li><b>建立多重检验纪律。</b>在研究开始前固定检验次数上限；或对最终候选做 Bonferroni / FDR 校正。本轮 26+ 变体下，|t| &gt; 2.6 的「发现」阈值应上调到约 3.2 以上。</li>
</ol>
</div>

<div class="card">
<h3>10.3 本轮的交付物</h3>
<ul>
<li><code>scripts/28_reversal_strategy.py</code> —— 用户规格策略的完整回测（Entry×Exit 网格、三段样本外、Walk-Forward、D1 对照、基准、成本敏感性）</li>
<li><code>scripts/29_reversal_diag.py</code> —— 十维度十分位诊断、超跌定义对比、邻域稳定性、分年度归因</li>
<li><code>output/v3_entry_exit.csv</code>、<code>v3_segments.csv</code>、<code>v3_walkforward.csv</code>、<code>v3_d1.csv</code>、<code>v3_bench.csv</code>、<code>v3_cost.csv</code></li>
<li><code>output/v3_decile.csv</code>、<code>v3_alt_def.csv</code>、<code>v3_neigh.csv</code>、<code>v3_rev_year.csv</code></li>
<li><code>output/v3_bias_diag.csv</code>（基准复利口径诊断）、<code>v3_year_v2.csv</code>（日度链式基准逐年）、<code>v3_robust.csv</code>（新旧基准对照）</li>
<li><code>scripts/30_build_v3_report.py</code> —— 本报告生成脚本</li>
<li><code>scripts/31_bias_and_year_fix.py</code> —— 基准口径修正与逐年重做</li>
</ul>
</div>

<div class="foot">
<p><b>数据范围</b>：2015-01-05 ~ 2026-09-22，全 A 股 5,304 只（上市满 120 个交易日），2,871 个交易日，1,053 万个股票-日观测。<br>
<b>面板字段</b>：来自 <code>data/v2_panel.parquet</code>（143 列），MACD / 量能 / 位置 / 相对强弱特征由 <code>scripts/21_build_v2_features.py</code> 构建。<br>
<b>成本假设</b>：0.3% 双边（用户指定），另做 0% ~ 2.0% 敏感性测试。<br>
<b>基准</b>：同交易日全部 clean 标的 <code>fwd{H}</code> 等权均值。<br>
<b>注意</b>：本报告为统计研究报告，不构成任何投资建议。所有结论均基于历史数据的统计推断，样本外表现可能显著不同。</p>
</div>
</div>

<script>
(function(){{
var E = {{}};
E.ee_lab = {jd(short_lab)};
E.ee_cagr5 = {jd(ee_grid["XA_固定持有5日"])};
E.ee_cagr10 = {jd(ee_grid["XA_固定持有10日"])};
E.dec = {jd({k: v for k, v in DEC.items()})};
E.left = {jd([dict(dim=r.dim, cn=r.cn, kind=r.kind, H=int(r.H), v=float(r.D1), t=float(r.t1)) for r in LEFT.itertuples() if r.H == 20])};
E.d1 = {jd([dict(g=r.group, v=float(r.cagr)*100) for r in d1.itertuples()])};
E.cost = {jd([dict(c=float(r.rt_cost)*100, v=float(r.cagr)*100) for r in cost.itertuples()])};
E.bench = {jd([dict(g=r.bench, v=float(r.cagr)*100) for r in bench.itertuples()])};
E.neigh_ma = {jd([dict(x=r.variant, v=float(r.exw20)*100) for r in neigh_ma.itertuples()])};
E.wf = {jd([dict(t=str(r.test), v=float(r.cagr)*100) for r in wfx.itertuples()])};
E.year = {jd([dict(y=int(r.year), g=str(r.variant), v=float(r.exw20)*100) for r in yr_sel.itertuples()])};
window.__V3__ = E;
}})();
</script>
<script>
(function(){{
var E = window.__V3__;
var UP='#d64545', DN='#2e9e5b', AC='#1f5fa8', AC2='#8a6d3b', TECH='#d98a2b', PRICE='#2f7fb8';
var AX={{axisLine:{{lineStyle:{{color:'#c9ced6'}}}},axisLabel:{{color:'#6b7480',fontSize:11}},splitLine:{{lineStyle:{{color:'#eef0f3'}}}}}};
function mk(id,opt){{ var el=document.getElementById(id); if(!el) return; var c=echarts.init(el); c.setOption(opt); window.addEventListener('resize',function(){{c.resize();}}); return c; }}
function gz(v){{ return v>0? UP : DN; }}

/* 图1 Entry x Exit */
mk('c-ee', {{
  grid:{{left:120,right:26,top:34,bottom:26}},
  legend:{{top:0,textStyle:{{color:'#4a5058',fontSize:12}}}},
  tooltip:{{trigger:'axis',axisPointer:{{type:'shadow'}},valueFormatter:function(v){{return v.toFixed(2)+'%';}}}},
  xAxis:Object.assign({{type:'value',name:'CAGR (%)',nameTextStyle:{{color:'#828a94'}}}},AX),
  yAxis:Object.assign({{type:'category',data:E.ee_lab.slice().reverse()}},AX),
  series:[
    {{name:'固定持有 5 日',type:'bar',data:E.ee_cagr5.slice().reverse(),itemStyle:{{color:'#e08a82'}}}},
    {{name:'固定持有 10 日',type:'bar',data:E.ee_cagr10.slice().reverse(),itemStyle:{{color:AC}}}}
  ]
}});

/* 图2 左侧符号分化 */
var L = E.left.slice().sort(function(a,b){{return a.v-b.v;}});
mk('c-left', {{
  grid:{{left:120,right:60,top:30,bottom:26}},
  tooltip:{{trigger:'axis',axisPointer:{{type:'shadow'}},formatter:function(p){{var d=p[0];var it=L[d.dataIndex];return it.cn+'<br/>D1 超额 '+it.v.toFixed(3)+'%<br/>t = '+it.t.toFixed(2)+'<br/>类型：'+it.kind;}}}},
  xAxis:Object.assign({{type:'value',name:'D1 前向20日超额 (%)',nameTextStyle:{{color:'#828a94'}}}},AX),
  yAxis:Object.assign({{type:'category',data:L.map(function(x){{return x.cn;}})}},AX),
  series:[{{type:'bar',data:L.map(function(x){{return {{value:x.v, itemStyle:{{color: x.kind==='技术型'? TECH : PRICE}}}};}}),
    label:{{show:true,position:'right',formatter:function(p){{return p.value.toFixed(3)+'%';}},color:'#6b7480',fontSize:11}}}}]
}});

/* 图3 十分位曲线 */
var dims = Object.keys(E.dec);
var series = dims.map(function(k,i){{
  return {{name:({jd(DIM_CN)}[k]||k), type:'line', smooth:true, symbolSize:5,
    data:(E.dec[k]['20']||[]).map(function(v){{return v;}}),
    lineStyle:{{width: (k==='hist_z'?3.0:1.6)}}, emphasis:{{focus:'series'}}}};
}});
mk('c-curve', {{
  grid:{{left:56,right:22,top:60,bottom:40}},
  legend:{{top:0,type:'scroll',textStyle:{{color:'#4a5058',fontSize:11}}}},
  tooltip:{{trigger:'axis',valueFormatter:function(v){{return v.toFixed(3)+'%';}}}},
  xAxis:Object.assign({{type:'category',data:['D1','D2','D3','D4','D5','D6','D7','D8','D9','D10'],name:'截面十分位',nameTextStyle:{{color:'#828a94'}}}},AX),
  yAxis:Object.assign({{type:'value',name:'H=20 超额 (%)',nameTextStyle:{{color:'#828a94'}}}},AX),
  series:series
}});

/* 图4 hist_z 十分位 */
mk('c-histz', {{
  grid:{{left:56,right:22,top:44,bottom:36}},
  legend:{{top:0,textStyle:{{color:'#4a5058',fontSize:12}}}},
  tooltip:{{trigger:'axis',valueFormatter:function(v){{return v.toFixed(3)+'%';}}}},
  xAxis:Object.assign({{type:'category',data:['D1','D2','D3','D4','D5','D6','D7','D8','D9','D10'],name:'hist_z 十分位（D1=最超跌，D10=最强）',nameTextStyle:{{color:'#828a94'}}}},AX),
  yAxis:Object.assign({{type:'value',name:'超额 (%)',nameTextStyle:{{color:'#828a94'}}}},AX),
  series:[5,10,20,60].map(function(H,i){{
    var cols=['#c9d6e5','#8fb0d4',AC,'#123a6b'];
    return {{name:'H='+H+'日',type:'line',smooth:true,symbolSize:6,data:E.dec.hist_z[String(H)],
      lineStyle:{{width:2,color:cols[i]}},itemStyle:{{color:cols[i]}}}};
  }})
}});

/* 图5 D1 对照 */
mk('c-d1', {{
  grid:{{left:150,right:60,top:20,bottom:30}},
  tooltip:{{trigger:'axis',axisPointer:{{type:'shadow'}},valueFormatter:function(v){{return v.toFixed(2)+'%';}}}},
  xAxis:Object.assign({{type:'value',name:'CAGR (%)',nameTextStyle:{{color:'#828a94'}}}},AX),
  yAxis:Object.assign({{type:'category',data:E.d1.map(function(x){{return x.g;}}).reverse()}},AX),
  series:[{{type:'bar',data:E.d1.map(function(x){{return {{value:x.v,itemStyle:{{color:gz(x.v)}}}};}}).reverse(),
    label:{{show:true,position:'right',formatter:function(p){{return p.value.toFixed(2)+'%';}},color:'#6b7480',fontSize:11}}}}]
}});

/* 图6 成本 */
mk('c-cost', {{
  grid:{{left:60,right:26,top:28,bottom:36}},
  tooltip:{{trigger:'axis',valueFormatter:function(v){{return v.toFixed(2)+'%';}}}},
  xAxis:Object.assign({{type:'category',data:E.cost.map(function(x){{return x.c.toFixed(1)+'%';}}),name:'双边成本',nameTextStyle:{{color:'#828a94'}}}},AX),
  yAxis:Object.assign({{type:'value',name:'CAGR (%)',nameTextStyle:{{color:'#828a94'}}}},AX),
  series:[{{type:'line',smooth:true,symbolSize:8,data:E.cost.map(function(x){{return x.v;}}),
    itemStyle:{{color:DN}},lineStyle:{{width:2.4,color:DN}},
    markPoint:{{data:[{{coord:[E.cost.findIndex(function(x){{return Math.abs(x.c-0.3)<1e-9;}}),E.cost.filter(function(x){{return Math.abs(x.c-0.3)<1e-9;}})[0].v],value:'用户指定',label:{{color:'#6b7480',fontSize:11}}}}],symbolSize:0}}}}]
}});

/* 图7 基准 */
var B = E.bench.slice();
mk('c-bench', {{
  grid:{{left:170,right:70,top:20,bottom:30}},
  tooltip:{{trigger:'axis',axisPointer:{{type:'shadow'}},valueFormatter:function(v){{return v.toFixed(2)+'%';}}}},
  xAxis:Object.assign({{type:'value',name:'CAGR (%)',nameTextStyle:{{color:'#828a94'}}}},AX),
  yAxis:Object.assign({{type:'category',data:B.map(function(x){{return x.g;}}).reverse()}},AX),
  series:[{{type:'bar',data:B.map(function(x){{return {{value:x.v,itemStyle:{{color:gz(x.v)}}}};}}).reverse(),
    label:{{show:true,position:'right',formatter:function(p){{return p.value.toFixed(2)+'%';}},color:'#6b7480',fontSize:11}}}}]
}});

/* 图8 walk-forward */
mk('c-wf', {{
  grid:{{left:60,right:26,top:28,bottom:36}},
  tooltip:{{trigger:'axis',valueFormatter:function(v){{return v.toFixed(2)+'%';}}}},
  xAxis:Object.assign({{type:'category',data:E.wf.map(function(x){{return x.t;}}),name:'样本外年份',nameTextStyle:{{color:'#828a94'}}}},AX),
  yAxis:Object.assign({{type:'value',name:'CAGR (%)',nameTextStyle:{{color:'#828a94'}}}},AX),
  series:[{{type:'bar',data:E.wf.map(function(x){{return {{value:x.v,itemStyle:{{color:gz(x.v)}}}};}}),
    label:{{show:true,position:'top',formatter:function(p){{return p.value.toFixed(1)+'%';}},color:'#6b7480',fontSize:10}}}}]
}});

/* 图9 邻域 */
mk('c-neigh', {{
  grid:{{left:60,right:26,top:28,bottom:40}},
  tooltip:{{trigger:'axis',valueFormatter:function(v){{return v.toFixed(3)+'%';}}}},
  xAxis:Object.assign({{type:'category',data:E.neigh_ma.map(function(x){{return x.x;}}),name:'距MA60 阈值',nameTextStyle:{{color:'#828a94'}}}},AX),
  yAxis:Object.assign({{type:'value',name:'H=20 超额 (%)',nameTextStyle:{{color:'#828a94'}}}},AX),
  series:[{{type:'bar',data:E.neigh_ma.map(function(x){{return {{value:x.v,itemStyle:{{color:gz(x.v)}}}};}}),
    label:{{show:true,position:'top',formatter:function(p){{return p.value.toFixed(2)+'%';}},color:'#6b7480',fontSize:10}},
    markLine:{{silent:true,symbol:'none',lineStyle:{{color:'#c9ced6',type:'dashed'}},data:[{{yAxis:0}}]}}}}]
}});

/* 图10 分年度 */
var yrs = Array.from(new Set(E.year.map(function(x){{return x.y;}}))).sort(function(a,b){{return a-b;}});
var g1='用户规格 hist_z<-1.5', g2='价格型 距MA60<-20%';
function pick(g){{ return yrs.map(function(y){{var f=E.year.filter(function(x){{return x.y===y&&x.g===g;}});return f.length? f[0].v : null;}}); }}
mk('c-year', {{
  grid:{{left:60,right:26,top:28,bottom:36}},
  legend:{{top:0,textStyle:{{color:'#4a5058',fontSize:12}}}},
  tooltip:{{trigger:'axis',valueFormatter:function(v){{return (v===null?'—':v.toFixed(3)+'%');}}}},
  xAxis:Object.assign({{type:'category',data:yrs.map(String)}},AX),
  yAxis:Object.assign({{type:'value',name:'H=20 超额 (%)',nameTextStyle:{{color:'#828a94'}}}},AX),
  series:[
    {{name:g1,type:'bar',data:pick(g1),itemStyle:{{color:TECH}}}},
    {{name:g2,type:'bar',data:pick(g2),itemStyle:{{color:PRICE}}}}
  ]
}});
}})();
</script>
</body>
</html>
"""

with open(TGT, "w", encoding="utf-8") as f:
    f.write(HTML)
print(f"[ok] {TGT}  {os.path.getsize(TGT):,} bytes")
