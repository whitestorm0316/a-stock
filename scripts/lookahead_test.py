#!/usr/bin/env python3
"""
lookahead_test.py — 未来函数决定性检测

方法1: 截断测试 (最有力)
  用 2015-2020 数据算信号, 与用 2015-2026 全量数据算的信号比较。
  若引擎或指标引入未来信息, 两者在重叠区间会不一致。
  同时: 只保留到 2020 的数据跑回测, 与全量回测在 2015-2020 的净值必须一致。

方法2: 打乱未来测试
  随机打乱 T+1 之后的行情, 信号不应改变。

方法3: 逐笔时间戳检查
  每笔交易的 buy_date 必须 > 触发该笔的信号日。
"""
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

print("=" * 76)
print("方法1: 截断测试 — 指标不含未来信息")
print("=" * 76)
full = pd.read_parquet(os.path.join(ROOT, "data", "processed", "panel.parquet"),
                       columns=["thscode", "date", "close_price", "volume",
                                "hist", "ma20", "vol_ratio", "hist_cross_up",
                                "golden_cross", "dif", "dea"])
CUT = pd.Timestamp("2020-12-31")
sub = full[full.date <= CUT].copy()

# 用截断数据重算指标, 与全量数据的重叠区间比较
def recompute(df):
    df = df.sort_values(["thscode", "date"]).reset_index(drop=True)
    g = df.groupby("thscode", sort=False)
    e12 = g["close_price"].transform(lambda x: x.ewm(span=12, adjust=False).mean())
    e26 = g["close_price"].transform(lambda x: x.ewm(span=26, adjust=False).mean())
    dif = e12 - e26
    df["_dif"] = dif
    dea = df.groupby("thscode", sort=False)["_dif"].transform(
        lambda x: x.ewm(span=9, adjust=False).mean())
    df["_dea"] = dea
    df["_hist"] = 2 * (dif - dea)
    df["_ma20"] = g["close_price"].transform(lambda x: x.rolling(20).mean())
    df["_vol20"] = g["volume"].transform(lambda x: x.rolling(20).mean())
    df["_vr"] = df["volume"] / df["_vol20"]
    return df

r = recompute(sub)
cmp = r[["thscode", "date", "hist", "_hist", "ma20", "_ma20",
         "vol_ratio", "_vr"]].dropna()
d_hist = (cmp["hist"] - cmp["_hist"]).abs().max()
d_ma = (cmp["ma20"] - cmp["_ma20"]).abs().max()
d_vr = (cmp["vol_ratio"] - cmp["_vr"]).abs().max()
print(f"  重叠区间样本: {len(cmp):,}")
print(f"  MACD柱 最大差异: {d_hist:.2e}  {'✓ 一致' if d_hist < 1e-4 else '✗ 不一致'}")
print(f"  MA20   最大差异: {d_ma:.2e}  {'✓ 一致' if d_ma < 1e-4 else '✗ 不一致'}")
print(f"  VolRatio 最大差异: {d_vr:.2e}  {'✓ 一致' if d_vr < 1e-4 else '✗ 不一致'}")

print("\n" + "=" * 76)
print("方法2: 截断回测 — 净值在重叠区间必须一致")
print("=" * 76)
for tag, dset in [("全量", full), ("截至2020", sub)]:
    p2 = pd.read_parquet(os.path.join(ROOT, "data", "processed", "panel.parquet"))
    p2 = p2[p2.date <= dset.date.max()] if tag != "全量" else p2
    gg = p2.groupby("thscode", sort=False)["turnover"]
    p2["amt20"] = gg.transform(lambda x: x.rolling(20, min_periods=10).mean())
    ix = ENG.PanelIndex.get(p2)
    A = ix.A
    fn, _ = STR.build("G", {})
    sig, ex, _ = fn(A, {})
    cfg = ENG.BacktestConfig(name=tag, max_positions=20, position_pct=0.05)
    res = ENG.VectorizedBacktester(ix, cfg).run(sig, ex)
    eq = pd.Series(res["equity"], index=pd.DatetimeIndex(res["dates"]))
    globals()[f"eq_{tag.replace('截至','')}"] = eq
    print(f"  {tag}: {len(eq)} 天, 期末 {eq.iloc[-1]:,.0f}, "
          f"区间 {eq.index[0].date()} ~ {eq.index[-1].date()}")

a = globals()["eq_全量"]
b = globals()["eq_2020"]
common = a.index.intersection(b.index)
da = (a.reindex(common) / a.reindex(common).iloc[0])
db = (b.reindex(common) / b.reindex(common).iloc[0])
diff = (da - db).abs().max()
print(f"  重叠区间 {len(common)} 天, 归一化净值最大差异: {diff:.6f}")
print(f"  {'✓ 通过: 无未来函数' if diff < 1e-6 else '✗ 存在未来函数泄漏'}")

print("\n" + "=" * 76)
print("方法3: 逐笔时间戳检查 — buy_date 必须晚于信号日")
print("=" * 76)
p3 = pd.read_parquet(os.path.join(ROOT, "data", "processed", "panel.parquet"))
gg = p3.groupby("thscode", sort=False)["turnover"]
p3["amt20"] = gg.transform(lambda x: x.rolling(20, min_periods=10).mean())
ix = ENG.PanelIndex.get(p3)
A = ix.A
fn, _ = STR.build("G", {})
sig, ex, _ = fn(A, {})
cfg = ENG.BacktestConfig(name="G", max_positions=20, position_pct=0.05)
res = ENG.VectorizedBacktester(ix, cfg).run(sig, ex)
tr = res["trades"]

# 引擎已记录 signal_date, 直接核对
tt = tr[tr["signal_date"].notna()].copy()
gaps = (tt["buy_date"] - tt["signal_date"]).dt.days.values
bad = int((gaps <= 0).sum())
print(f"  检查 {len(tt)} 笔, 违规(买入日<=信号日) {bad} 笔")
print(f"  {'✓ 通过: 全部 T+1 及之后执行' if bad == 0 else '✗ 存在当日或提前成交'}")
if bad:
    print(tt[gaps <= 0][["code","signal_date","buy_date"]].head(3).to_string(index=False))
print(f"  信号日→买入日 间隔: 中位 {np.median(gaps):.0f} 天, "
      f"最小 {gaps.min()} 天, 最大 {gaps.max()} 天")
print(f"  间隔=1天占比 {(gaps==1).mean()*100:.1f}% (T+1), "
      f"2-4天 {(np.isin(gaps,[2,3,4])).mean()*100:.1f}% (含周末), "
      f">4天 {(gaps>4).mean()*100:.1f}% (停牌/涨跌停顺延)")

print("\n" + "=" * 76)
print("方法4: 打乱未来行情, 信号必须不变")
print("=" * 76)
p4 = pd.read_parquet(os.path.join(ROOT, "data", "processed", "panel.parquet"),
                     columns=["thscode", "date", "close_price", "volume",
                              "hist_cross_up", "vol_ratio", "dif", "dea"])
p4 = p4.sort_values(["thscode", "date"]).reset_index(drop=True)
orig = (p4["hist_cross_up"].values & (p4["vol_ratio"].values > 1.1))

# 把 2021 年之后的 close/volume 全部替换成随机值
CUT2 = pd.Timestamp("2021-01-01")
rng = np.random.default_rng(0)
mask = (p4["date"] >= CUT2).values
p4b = p4.copy()
p4b["close_price"] = p4b["close_price"].astype(np.float64)
p4b.loc[mask, "close_price"] = rng.uniform(1, 100, mask.sum())
p4b["volume"] = p4b["volume"].astype(np.float64)
p4b.loc[mask, "volume"] = rng.uniform(1e5, 1e8, mask.sum())
# 重算指标
g4 = p4b.groupby("thscode", sort=False)
e12 = g4["close_price"].transform(lambda x: x.ewm(span=12, adjust=False).mean())
e26 = g4["close_price"].transform(lambda x: x.ewm(span=26, adjust=False).mean())
dif = e12 - e26
dea = dif.groupby(p4b["thscode"]).transform(lambda x: x.ewm(span=9, adjust=False).mean())
hist = 2 * (dif - dea)
hc = (hist > 0) & (hist.groupby(p4b["thscode"]).shift(1) <= 0)
vr = p4b["volume"] / g4["volume"].transform(lambda x: x.rolling(20, min_periods=10).mean())
new = (hc.values & (vr.values > 1.1))
before = orig & (p4["date"] < CUT2).values
after = new & (p4["date"] < CUT2).values
print(f"  打乱前信号数(2021前): {before.sum():,}")
print(f"  打乱后信号数(2021前): {after.sum():,}")
print(f"  信号差异: {np.abs(before.astype(int)-after.astype(int)).sum()} 处")
print(f"  {'✓ 通过: 未来行情不影响历史信号' if np.abs(before.astype(int)-after.astype(int)).sum()==0 else '✗ 信号受未来影响'}")
