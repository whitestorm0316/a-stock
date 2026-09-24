#!/usr/bin/env python3
"""diag3.py — 决定性对照: 引擎持有全部股票 vs 市场等权基准"""
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

# 可交易样本
tradable = ((panel.days_since_list >= 120) & (~panel.is_st_now)
            & (panel.turnover >= 2e7) & np.isfinite(panel.close_price)).values

print("=== 基准: 可交易样本等权 buy&hold (每日再平衡, 无成本) ===")
d = panel[tradable]
mm = d.groupby("date")["ret"].mean().sort_index()
nav = (1 + mm).cumprod()
yrs = (nav.index[-1] - nav.index[0]).days / 365.25
print(f"  每日再平衡等权: 年化{nav.iloc[-1]**(1/yrs)-1:+.1%} 回撤{(nav/nav.cummax()-1).min():.1%}")
# 每月再平衡
mn = (1 + mm).groupby(mm.index.to_period("M")).prod() - 1
navm = (1 + mn).cumprod()
print(f"  每月再平衡等权: 年化{navm.iloc[-1]**(1/yrs)-1:+.1%}")
# 买入持有(不换手)
first_last = d.groupby("thscode")["close_price"].agg(["first", "last"])
bh = (first_last["last"] / first_last["first"]).mean()
print(f"  纯买入持有(等权, 全期不换手): 总{bh-1:+.1%} 年化{bh**(1/yrs)-1:+.1%}")

print("\n=== 引擎: 持有全部可交易股票, 不设退出, 无成本 ===")
ENG.SLIPPAGE = 0.0
ENG.COMMISSION_RATE = 0.0
ENG.COMMISSION_MIN = 0.0
ENG.STAMP_TAX_BEFORE = 0.0
ENG.STAMP_TAX_AFTER = 0.0
ENG.TRANSFER_FEE = 0.0

sig = tradable.copy()
cfg = ENG.BacktestConfig(name="holdall", initial_cash=10_000_000.0,
                         max_positions=100000, position_pct=0.0005,
                         min_days_listed=120, exclude_st=True,
                         min_amount=2e7, max_amount_pct=1.0)
r = ENG.VectorizedBacktester(ix, cfg).run(sig, np.zeros(n, dtype=bool))
st = ENG.perf_stats(r)
print(f"  trades={st['n_trades']} cagr={st['cagr']*100:+.1f}% "
      f"mdd={st['max_drawdown']*100:.1f}% avgpos={st['avg_positions']:.0f} "
      f"total_ret={st['total_return']*100:+.1f}%")
print(f"  (对比: 每日再平衡等权 {nav.iloc[-1]**(1/yrs)-1:+.1%})")

print("\n=== 引擎: 持有全部 + 正常成本 ===")
ENG.SLIPPAGE = 0.001
ENG.COMMISSION_RATE = 0.00025
ENG.COMMISSION_MIN = 5.0
ENG.STAMP_TAX_BEFORE = 0.001
ENG.STAMP_TAX_AFTER = 0.0005
ENG.TRANSFER_FEE = 0.00001
cfg2 = ENG.BacktestConfig(name="holdall_cost", initial_cash=10_000_000.0,
                          max_positions=100000, position_pct=0.0005,
                          min_days_listed=120, exclude_st=True,
                          min_amount=2e7, max_amount_pct=1.0)
r2 = ENG.VectorizedBacktester(ix, cfg2).run(sig, np.zeros(n, dtype=bool))
st2 = ENG.perf_stats(r2)
print(f"  cagr={st2['cagr']*100:+.1f}% total_ret={st2['total_return']*100:+.1f}% "
      f"fee={st2['total_fee']:,.0f}")

print("\n=== 引擎: 随机20只 + 正常成本 + 持有10日 (多次试验) ===")
rng = np.random.default_rng(0)
cagrs = []
for trial in range(5):
    rand = rng.random(n)
    s = (rand < 0.02) & tradable
    cfg3 = ENG.BacktestConfig(name=f"r{trial}", max_positions=20, position_pct=0.05,
                              max_hold_days=10)
    rr = ENG.VectorizedBacktester(ix, cfg3).run(s, np.zeros(n, dtype=bool))
    ss = ENG.perf_stats(rr)
    cagrs.append(ss["cagr"])
    print(f"  trial{trial}: cagr={ss['cagr']*100:+6.1f}% mdd={ss['max_drawdown']*100:6.1f}% "
          f"trades={ss['n_trades']:>5} avg_ret={ss['expectancy']*100:+.3f}%")
print(f"  平均 cagr = {np.mean(cagrs)*100:+.1f}%")
print(f"  年化成本拖累估计: 25次换仓/年 x 0.2%滑点 x 2(双边) ~= {25*0.002*2*100:.1f}%/年")
