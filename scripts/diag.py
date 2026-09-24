#!/usr/bin/env python3
"""diag.py — 诊断: 收益从哪来、为何组合回测远低于事件研究"""
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

print("=== 1. 验证退出规则现在生效 ===")
fn, _ = STR.build("G", {})
sig, ex, desc = fn(A, {})
print("signal:", desc)
for label, mh, sl, tp in [("死叉退出", None, None, None),
                          ("持有5日", 5, None, None),
                          ("持有20日", 20, None, None),
                          ("持有60日", 60, None, None),
                          ("持有120日", 120, None, None)]:
    cfg = ENG.BacktestConfig(name=label, max_positions=20, position_pct=0.05,
                             max_hold_days=mh, stop_loss=sl, take_profit=tp)
    r = ENG.VectorizedBacktester(ix, cfg).run(sig, ex)
    st = ENG.perf_stats(r)
    print(f"  {label:<10} trades={st['n_trades']:>5} cagr={st['cagr']*100:>6.1f}% "
          f"mdd={st['max_drawdown']*100:>6.1f}% wr={st['win_rate']*100:>4.1f}% "
          f"payoff={st['payoff_ratio']:>4.2f} PF={st['profit_factor']:>4.2f} "
          f"hold={st['avg_hold_days']:>5.1f}d")

print("\n=== 2. 分解: 信号后 T+1 开盘买入 → 各期收益 (纯事件, 无组合约束) ===")
# 用 fwd 收益近似: 买入 T+1 开盘, 卖出 T+1+N 开盘
d = panel.copy()
d = d[(d.days_since_list >= 120) & (~d.is_st_now) & (d.turnover >= 2e7)]
d = d.sort_values(["thscode", "date"])
gg = d.groupby("thscode", sort=False)
for k in (1, 5, 10, 20, 60):
    d[f"o2o_{k}"] = gg["open_price"].transform(lambda x, k=k: x.shift(-(k + 1)) / x.shift(-1) - 1)
# 同样算 c2c (信号日收盘买入, 不可实现, 用于对比)
for k in (1, 5, 10, 20, 60):
    d[f"c2c_{k}"] = gg["close_price"].transform(lambda x, k=k: x.shift(-k) / x - 1)

m = d["hist_cross_up"].values.astype(bool) & (d["vol_ratio"].values > 1.1)
print(f"  signal count (tradable): {m.sum():,}")
for k in (1, 5, 10, 20, 60):
    a = d.loc[m, f"o2o_{k}"].values
    a = a[np.isfinite(a)]
    b = d.loc[m, f"c2c_{k}"].values
    b = b[np.isfinite(b)]
    print(f"  {k:>2}d: T+1开盘买入→T+1+{k}开盘卖出 {np.mean(a)*100:>+6.2f}%  |  "
          f"信号日收盘→{k}日后收盘 {np.mean(b)*100:>+6.2f}%  |  跳空损耗 {(np.mean(a)-np.mean(b))*100:>+6.2f}%")

print("\n=== 3. 全市场同期基准 (T+1开盘买入→N日后开盘, 随机基准) ===")
for k in (1, 5, 10, 20, 60):
    a = d[f"o2o_{k}"].values
    a = a[np.isfinite(a)]
    print(f"  {k:>2}d: {np.mean(a)*100:>+6.2f}%")

print("\n=== 4. 组合容量测试 (不同持仓数, 死叉退出) ===")
for mp, pp in [(5, 0.20), (10, 0.10), (20, 0.05), (50, 0.02), (100, 0.01)]:
    cfg = ENG.BacktestConfig(name=f"mp{mp}", max_positions=mp, position_pct=pp)
    r = ENG.VectorizedBacktester(ix, cfg).run(sig, ex)
    st = ENG.perf_stats(r)
    print(f"  持仓{mp:>3} 单票{pp:.0%}: trades={st['n_trades']:>5} cagr={st['cagr']*100:>6.1f}% "
          f"mdd={st['max_drawdown']*100:>6.1f}% avgpos={st['avg_positions']:>5.1f} "
          f"wr={st['win_rate']*100:>4.1f}% PF={st['profit_factor']:>4.2f}")

print("\n=== 5. 交易成本影响 ===")
import importlib
ENG2 = load(os.path.join(ROOT, "scripts", "03_backtest.py"), "eng2")
for slip, comm in [(0.0, 0.0), (0.0005, 0.0001), (0.001, 0.00025)]:
    ENG2.SLIPPAGE = slip
    ENG2.COMMISSION_RATE = comm
    cfg = ENG2.BacktestConfig(name="cost", max_positions=20, position_pct=0.05)
    r = ENG2.VectorizedBacktester(ix, cfg).run(sig, ex)
    st = ENG2.perf_stats(r)
    print(f"  滑点{slip:.4f} 佣金{comm:.5f}: cagr={st['cagr']*100:>6.1f}% "
          f"fee合计={st['total_fee']:>10,.0f} fee/毛利={st['fee_to_grosswin']:.2%}")
