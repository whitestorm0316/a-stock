#!/usr/bin/env python3
"""diag5.py — 逐笔对账: 引擎成交价与真实价格路径是否一致"""
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

panel = pd.read_parquet(os.path.join(ROOT, "data", "processed", "panel.parquet"))
g = panel.groupby("thscode", sort=False)["turnover"]
panel["amt20"] = g.transform(lambda x: x.rolling(20, min_periods=10).mean())
ix = ENG.PanelIndex.get(panel)
A = ix.A
n = len(panel)

# 价格查找表 (未复权与复权)
px = panel.set_index(["thscode", "date"])[["open_price", "close_price",
                                           "open_price_raw", "close_price_raw"]]

fn, _ = STR.build("G", {})
sig, ex, desc = fn(A, {})
cfg = ENG.BacktestConfig(name="G", max_positions=20, position_pct=0.05)
r = ENG.VectorizedBacktester(ix, cfg).run(sig, ex)
tr = r["trades"].copy()
print(f"trades={len(tr)}")
print(f"engine: cagr={ENG.perf_stats(r)['cagr']*100:+.1f}%")

# 逐笔核对: 用面板价格重算每笔收益
recs = []
for _, row in tr.iterrows():
    c = row["code"]
    bd, sd = row["buy_date"], row["sell_date"]
    try:
        bo = px.loc[(c, bd), "open_price"]
        so = px.loc[(c, sd), "open_price"]
    except KeyError:
        recs.append((c, bd, sd, np.nan, np.nan, row["ret"], np.nan))
        continue
    exp_ret = (so * 0.999) / (bo * 1.001) - 1
    recs.append((c, bd, sd, bo * 1.001, so * 0.999, row["ret"], exp_ret))

rec = pd.DataFrame(recs, columns=["code", "buy_date", "sell_date", "exp_buy",
                                  "exp_sell", "eng_ret", "exp_ret"])
rec["diff"] = rec["eng_ret"] - rec["exp_ret"]
print(f"\n=== 成交价对账 ===")
print(f"  buy_px 与 开盘*1.001 一致: {(rec['exp_buy'] - tr['buy_px']).abs().lt(1e-4).mean()*100:.1f}%")
print(f"  笔收益 与 重算收益 一致: {rec['diff'].abs().lt(1e-4).mean()*100:.1f}%")
print(f"  引擎笔均收益: {tr['ret'].mean()*100:+.3f}%   重算笔均: {rec['exp_ret'].mean()*100:+.3f}%")

print(f"\n=== 收益分布对比 ===")
print(f"  引擎: 胜率{(tr['ret']>0).mean()*100:.1f}% 均值{tr['ret'].mean()*100:+.3f}% "
      f"中位{tr['ret'].median()*100:+.3f}% 标准差{tr['ret'].std()*100:.2f}%")
print(f"  重算: 胜率{(rec['exp_ret']>0).mean()*100:.1f}% 均值{rec['exp_ret'].mean()*100:+.3f}% "
      f"中位{rec['exp_ret'].median()*100:+.3f}% 标准差{rec['exp_ret'].std()*100:.2f}%")

print(f"\n=== 同期同股票池随机基准 (相同持有期) ===")
# 用相同的 (buy_date, hold_days) 但随机选股票
rng = np.random.default_rng(1)
panel2 = panel.copy()
pxc = panel2.set_index(["thscode", "date"])["open_price"]
codes_by_date = panel2.groupby("date")["thscode"].apply(list)
allret = []
for _, row in tr.iterrows():
    bd, hd = row["buy_date"], int(row["hold_days"])
    # 找 bd 当天可交易的随机股票
    pass
# 简化: 直接比较相同持有天数下 G 信号股 vs 全市场
d = panel[(panel.days_since_list >= 120) & (~panel.is_st_now) & (panel.turnover >= 2e7)]
d = d.sort_values(["thscode", "date"])
gg = d.groupby("thscode", sort=False)
for k in (5, 10, 20):
    d[f"o2o{k}"] = gg["open_price"].transform(lambda x, k=k: x.shift(-(k+1)) / x.shift(-1) - 1)
msig = (d["hist_cross_up"].values & (d["vol_ratio"].values > 1.1))
print(f"  {'持有':<6}{'G信号股 o2o':<16}{'全市场 o2o':<16}{'超额':<10}{'样本数'}")
for k in (5, 10, 20):
    a = d.loc[msig, f"o2o{k}"].values; a = a[np.isfinite(a)]
    b = d[f"o2o{k}"].values; b = b[np.isfinite(b)]
    print(f"  {k:<6}{np.mean(a)*100:>+8.3f}%      {np.mean(b)*100:>+8.3f}%      "
          f"{(np.mean(a)-np.mean(b))*100:>+7.3f}%   {len(a):,}")

print(f"\n=== 引擎持仓的等权收益 vs 市场 (逐日) ===")
# 从交易明细重建每日持仓, 计算持仓等权日收益
eq = pd.Series(r["equity"], index=pd.DatetimeIndex(r["dates"]))
mkt = panel.groupby("date")["ret"].mean().sort_index()
mkt = mkt.reindex(eq.index)
print(f"  引擎年化: {(eq.iloc[-1]/eq.iloc[0])**(252/len(eq))-1:+.1%}")
print(f"  市场年化: {(1+mkt.fillna(0)).prod()**(252/len(mkt))-1:+.1%}")
print(f"  引擎日收益均值: {eq.pct_change().mean()*100:+.4f}%  市场: {mkt.mean()*100:+.4f}%")
print(f"  引擎日收益标准差: {eq.pct_change().std()*100:.3f}%  市场: {mkt.std()*100:.3f}%")
