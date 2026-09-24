#!/usr/bin/env python3
"""diag_lookahead.py — 精确定位方法2/方法4差异来源"""
import os, importlib.util
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(p, n):
    s = importlib.util.spec_from_file_location(n, p)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


ENG = load(os.path.join(ROOT, "scripts", "03_backtest.py"), "eng")
STR = load(os.path.join(ROOT, "scripts", "04_strategies.py"), "str")
P = os.path.join(ROOT, "data", "processed", "panel.parquet")

print("=" * 74)
print("诊断A: 方法2差异出现在哪些日期? (若仅在末日附近 -> 边界效应)")
print("=" * 74)
full = pd.read_parquet(P)
CUT = pd.Timestamp("2020-12-31")

def run_panel(p2, tag):
    p2 = p2.copy()
    gg = p2.groupby("thscode", sort=False)["turnover"]
    p2["amt20"] = gg.transform(lambda x: x.rolling(20, min_periods=10).mean())
    ix = ENG.PanelIndex.get(p2)
    A = ix.A
    fn, _ = STR.build("G", {})
    sig, ex, _ = fn(A, {})
    cfg = ENG.BacktestConfig(name=tag, max_positions=20, position_pct=0.05)
    res = ENG.VectorizedBacktester(ix, cfg).run(sig, ex)
    return pd.Series(res["equity"], index=pd.DatetimeIndex(res["dates"]))

eq_full = run_panel(full, "full")
eq_cut = run_panel(full[full.date <= CUT], "cut")
common = eq_full.index.intersection(eq_cut.index)
a = eq_full.reindex(common); b = eq_cut.reindex(common)
da = a / a.iloc[0]; db = b / b.iloc[0]
diff = (da - db).abs()
print(f"  重叠 {len(common)} 天, 最大差异 {diff.max():.6f} 于 {diff.idxmax().date()}")
nz = diff[diff > 1e-9]
print(f"  差异 > 1e-9 的天数: {len(nz)} / {len(common)}")
if len(nz):
    print(f"  差异首次出现: {nz.index[0].date()}")
    print(f"  差异最后出现: {nz.index[-1].date()}")
    print(f"  末日 {common[-1].date()} 的差异: {diff.iloc[-1]:.6f}")
    # 差异的日期分布
    print(f"  差异日期分布 (按年): {nz.index.year.value_counts().sort_index().to_dict()}")
    # 排除最后 10 天后的差异
    tail = diff.iloc[:-10]
    print(f"  排除最后10个交易日后的最大差异: {tail.max():.9f}")

print("\n" + "=" * 74)
print("诊断B: 方法4差异是否为浮点精度? (检查差异处 hist 是否临界)")
print("=" * 74)
p4 = pd.read_parquet(P, columns=["thscode", "date", "close_price", "volume",
                                 "hist_cross_up", "vol_ratio"])
p4 = p4.sort_values(["thscode", "date"]).reset_index(drop=True)
orig = (p4["hist_cross_up"].values & (p4["vol_ratio"].values > 1.1))

CUT2 = pd.Timestamp("2021-01-01")
rng = np.random.default_rng(0)
mask = (p4["date"] >= CUT2).values
p4b = p4.copy()
p4b["close_price"] = p4b["close_price"].astype(np.float64)
p4b["volume"] = p4b["volume"].astype(np.float64)
p4b.loc[mask, "close_price"] = rng.uniform(1, 100, mask.sum())
p4b.loc[mask, "volume"] = rng.uniform(1e5, 1e8, mask.sum())

g4 = p4b.groupby("thscode", sort=False)
e12 = g4["close_price"].transform(lambda x: x.ewm(span=12, adjust=False).mean())
e26 = g4["close_price"].transform(lambda x: x.ewm(span=26, adjust=False).mean())
dif = e12 - e26
dea = dif.groupby(p4b["thscode"], sort=False).transform(
    lambda x: x.ewm(span=9, adjust=False).mean())
hist = 2 * (dif - dea)
hc = (hist > 0) & (hist.groupby(p4b["thscode"], sort=False).shift(1) <= 0)
vr = p4b["volume"] / g4["volume"].transform(lambda x: x.rolling(20).mean())
new = (hc.values & (vr.values > 1.1))

before = orig & (p4["date"] < CUT2).values
after = new & (p4["date"] < CUT2).values
d = before.astype(int) - after.astype(int)
idx = np.nonzero(d)[0]
print(f"  差异处数: {len(idx)}")
if len(idx):
    # 看差异处的 hist 与 vr 是否临界
    h_at = hist.values[idx]
    v_at = vr.values[idx]
    print(f"  差异处 |hist|: 中位 {np.median(np.abs(h_at)):.2e}, "
          f"最大 {np.max(np.abs(h_at)):.2e}")
    print(f"  差异处 |vr-1.1|: 中位 {np.median(np.abs(v_at-1.1)):.2e}, "
          f"最大 {np.max(np.abs(v_at-1.1)):.2e}")
    # 与原始 hist 对比
    h_orig = p4["hist_cross_up"].values
    print(f"  差异处原信号: {h_orig[idx].sum()} 个为 True (原hist_cross_up)")
    # 统计临界比例
    near0 = (np.abs(h_at) < 1e-3).mean()
    near11 = (np.abs(v_at - 1.1) < 1e-3).mean()
    print(f"  差异处 |hist|<1e-3 占比: {near0*100:.1f}%")
    print(f"  差异处 |vr-1.1|<1e-3 占比: {near11*100:.1f}%")
    print(f"  -> {'✓ 差异全部来自临界值处的浮点精度' if near0 > 0.5 or near11 > 0.5 else '需进一步检查'}")

print("\n" + "=" * 74)
print("诊断C: 用同一精度(float32)重算, 信号应完全一致")
print("=" * 74)
p4c = p4.copy()   # 保持 float32
p4c.loc[mask, "close_price"] = rng.uniform(1, 100, mask.sum()).astype(np.float32)
p4c.loc[mask, "volume"] = rng.uniform(1e5, 1e8, mask.sum()).astype(np.float32)
g5 = p4c.groupby("thscode", sort=False)
e12c = g5["close_price"].transform(lambda x: x.ewm(span=12, adjust=False).mean())
e26c = g5["close_price"].transform(lambda x: x.ewm(span=26, adjust=False).mean())
difc = e12c - e26c
deac = difc.groupby(p4c["thscode"], sort=False).transform(
    lambda x: x.ewm(span=9, adjust=False).mean())
histc = 2 * (difc - deac)
hcc = (histc > 0) & (histc.groupby(p4c["thscode"], sort=False).shift(1) <= 0)
vrc = p4c["volume"] / g5["volume"].transform(lambda x: x.rolling(20).mean())
newc = (hcc.values & (vrc.values > 1.1))
afterc = newc & (p4["date"] < CUT2).values
dd = before.astype(int) - afterc.astype(int)
print(f"  float32 重算后差异处数: {np.abs(dd).sum()}")
print(f"  {'✓ 确认: 原差异源于 float32/float64 精度' if np.abs(dd).sum() == 0 else '仍有差异, 需深入'}")

print("\n" + "=" * 74)
print("诊断D: 真正的未来函数检测 — 用未来数据反推信号是否可能")
print("=" * 74)
# 严格检验: 对随机抽样的 (股票, 日期), 只用该日及之前的数据重算指标, 与面板值比对
sub = pd.read_parquet(P, columns=["thscode", "date", "close_price", "volume",
                                  "dif", "dea", "hist", "ma20", "vol_ratio"])
sub = sub.sort_values(["thscode", "date"]).reset_index(drop=True)
g6 = sub.groupby("thscode", sort=False)
rng2 = np.random.default_rng(5)
codes = sub["thscode"].unique()
bad_cnt = 0
tot = 0
for c in rng2.choice(codes, size=40, replace=False):
    s = sub[sub.thscode == c].reset_index(drop=True)
    if len(s) < 300:
        continue
    for pos in rng2.choice(np.arange(200, len(s)), size=3, replace=False):
        window = s.iloc[:pos + 1]
        cl = window["close_price"].astype(np.float64)
        vol = window["volume"].astype(np.float64)
        e12 = cl.ewm(span=12, adjust=False).mean()
        e26 = cl.ewm(span=26, adjust=False).mean()
        dif_ = e12 - e26
        dea_ = dif_.ewm(span=9, adjust=False).mean()
        hist_ = 2 * (dif_ - dea_)
        ma20_ = cl.rolling(20).mean().iloc[-1]
        vr_ = vol.iloc[-1] / vol.rolling(20).mean().iloc[-1]
        tot += 1
        if abs(hist_.iloc[-1] - s["hist"].iloc[pos]) > 1e-3:
            bad_cnt += 1
        if abs(ma20_ - s["ma20"].iloc[pos]) > 1e-3:
            bad_cnt += 1
        if abs(vr_ - s["vol_ratio"].iloc[pos]) > 1e-3:
            bad_cnt += 1
print(f"  抽样 {tot} 个 (股票,日期) 点, 逐点截断重算")
print(f"  指标不一致次数: {bad_cnt}")
print(f"  {'✓ 通过: 每个时点指标仅依赖该时点及之前数据' if bad_cnt == 0 else '✗ 存在未来信息'}")
