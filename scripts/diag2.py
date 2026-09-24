#!/usr/bin/env python3
"""diag2.py — 隔离验证引擎: 用无信号偏差的策略复现市场收益"""
import os, importlib.util
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(p, n):
    s = importlib.util.spec_from_file_location(n, p)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


ENG = load(os.path.join(ROOT, "scripts", "03_backtest.py"), "eng")

panel = pd.read_parquet(os.path.join(ROOT, "data", "processed", "panel.parquet"))
g = panel.groupby("thscode", sort=False)["turnover"]
panel["amt20"] = g.transform(lambda x: x.rolling(20, min_periods=10).mean())
ix = ENG.PanelIndex.get(panel)
A = ix.A
n = len(panel)

print("=== 测试1: 引擎能否复现市场收益? 信号=所有可交易股票 ===")
# 用 hash 做伪随机选择, 保证无信号偏差
h = (pd.factorize(panel["thscode"])[0] * 2654435761 % 1000).astype(float) / 1000.0
np.random.seed(42)
rand = np.random.rand(n)
sig_all = rand < 0.02                     # 每天约2%的股票被选中
print(f"  signal density: {sig_all.mean():.3%}")

for mp, pp in [(20, 0.05), (100, 0.01), (500, 0.002)]:
    cfg = ENG.BacktestConfig(name=f"rand{mp}", max_positions=mp, position_pct=pp,
                             max_hold_days=10)
    r = ENG.VectorizedBacktester(ix, cfg).run(sig_all, np.zeros(n, dtype=bool))
    st = ENG.perf_stats(r)
    print(f"  随机买入 持仓{mp:>3}: trades={st['n_trades']:>6} cagr={st['cagr']*100:>6.1f}% "
          f"mdd={st['max_drawdown']*100:>6.1f}% avgpos={st['avg_positions']:>5.1f} "
          f"wr={st['win_rate']*100:>4.1f}% PF={st['profit_factor']:>4.2f} "
          f"avg_ret={st['expectancy']*100:>+6.3f}%")

print("\n=== 测试2: 单票买入持有 (检验盯市与清算) ===")
# 只买 600519.SH 一次, 持有到底
one = np.zeros(n, dtype=bool)
m = (panel["thscode"].values == "600519.SH")
first = np.nonzero(m)[0][60] if m.sum() > 60 else None
if first is not None:
    one[first] = True
cfg = ENG.BacktestConfig(name="single", max_positions=1, position_pct=1.0)
r = ENG.VectorizedBacktester(ix, cfg).run(one, np.zeros(n, dtype=bool))
st = ENG.perf_stats(r)
tr = r["trades"]
print(f"  trades={len(tr)}")
if len(tr):
    print(f"  {tr.iloc[0]['code']} buy={pd.Timestamp(tr.iloc[0]['buy_date']).date()} "
          f"px={tr.iloc[0]['buy_px']:.2f} sell={pd.Timestamp(tr.iloc[0]['sell_date']).date()} "
          f"px={tr.iloc[0]['sell_px']:.2f} ret={tr.iloc[0]['ret']*100:.2f}%")
    # 对比实际价格
    s = panel[panel.thscode == "600519.SH"].sort_values("date").reset_index(drop=True)
    print(f"  实际: 首次开盘={s['open_price'].iloc[60]:.2f} 末日收盘={s['close_price'].iloc[-1]:.2f} "
          f"涨幅={s['close_price'].iloc[-1]/s['open_price'].iloc[60]-1:+.2%}")

print("\n=== 测试3: 逐年收益 (随机策略 vs 市场) ===")
cfg = ENG.BacktestConfig(name="rand", max_positions=100, position_pct=0.01, max_hold_days=10)
r = ENG.VectorizedBacktester(ix, cfg).run(sig_all, np.zeros(n, dtype=bool))
yr = ENG.yearly_returns(r)
mk = panel.groupby("date")["ret"].mean().sort_index()
mky = (1 + mk).groupby(mk.index.year).prod() - 1
cmp = pd.DataFrame({"engine": yr, "market_eqw": mky})
cmp["diff"] = cmp["engine"] - cmp["market_eqw"]
print((cmp * 100).round(1).to_string())

print("\n=== 测试4: 抽查单笔交易的实际价格路径 ===")
tr = r["trades"]
print(f"  总交易数 {len(tr)}")
smp = tr.head(3)
for _, row in smp.iterrows():
    s = panel[panel.thscode == row["code"]].sort_values("date").reset_index(drop=True)
    bi = s.index[s["date"] == row["buy_date"]]
    si = s.index[s["date"] == row["sell_date"]]
    print(f"  {row['code']}: buy_date={pd.Timestamp(row['buy_date']).date()} "
          f"open={s['open_price'].iloc[bi[0]] if len(bi) else 'NA':.3f} "
          f"| engine_buy_px={row['buy_px']:.3f}")
    print(f"      sell_date={pd.Timestamp(row['sell_date']).date()} "
          f"open={s['open_price'].iloc[si[0]] if len(si) else 'NA':.3f} "
          f"| engine_sell_px={row['sell_px']:.3f} | ret={row['ret']*100:+.2f}%")
    if len(bi) and len(si):
        actual = (s['open_price'].iloc[si[0]] * 0.999) / (s['open_price'].iloc[bi[0]] * 1.001) - 1
        print(f"      expected ret (含滑点, 不含费) = {actual*100:+.2f}%")

print("\n=== 测试5: 全市场等权 buy&hold 基准对照 ===")
d = panel[(panel.days_since_list >= 120) & (~panel.is_st_now) & (panel.turnover >= 2e7)]
mm = d.groupby("date")["ret"].mean().sort_index()
nav = (1 + mm).cumprod()
yrs = (nav.index[-1] - nav.index[0]).days / 365.25
print(f"  可交易样本等权 buy&hold: 累计{nav.iloc[-1]-1:+.1%} 年化{nav.iloc[-1]**(1/yrs)-1:+.1%} "
      f"回撤{(nav/nav.cummax()-1).min():.1%}")
