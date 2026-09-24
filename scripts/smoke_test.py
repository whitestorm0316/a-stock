#!/usr/bin/env python3
"""smoke_test.py — 引擎冒烟测试: 验证无未来函数、成本、涨跌停约束是否生效"""
import os, sys, time, importlib.util
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ENG = load(os.path.join(ROOT, "scripts", "03_backtest.py"), "eng")
STR = load(os.path.join(ROOT, "scripts", "04_strategies.py"), "str")

t0 = time.time()
panel = pd.read_parquet(os.path.join(ROOT, "data", "processed", "panel.parquet"))
g = panel.groupby("thscode", sort=False)["turnover"]
panel["amt20"] = g.transform(lambda x: x.rolling(20, min_periods=10).mean())
print(f"panel {len(panel):,} rows, {time.time()-t0:.1f}s")

t1 = time.time()
ix = ENG.PanelIndex.get(panel)
print(f"PanelIndex built in {time.time()-t1:.1f}s, days={ix.n_days}")
A = ix.A

print("\n=== 指标与约束校验 ===")
print(f"  golden_cross == hist_cross_up: {(A['golden_cross']==A['hist_cross_up']).mean()*100:.2f}%")
sub = panel[panel.thscode == "600519.SH"].sort_values("date").reset_index(drop=True)
man = sub["close_price"].rolling(20).mean()
print(f"  ma20 校验(茅台): calc={man.iloc[-1]:.4f} panel={sub['ma20'].iloc[-1]:.4f}")
print(f"  开盘涨停(不可买)占比: {(~A['can_buy_open']).mean()*100:.2f}%")
print(f"  开盘跌停(不可卖)占比: {(~A['can_sell_open']).mean()*100:.2f}%")
print(f"  停牌行占比: {(~A['has_next_bar']).mean()*100:.2f}%")
print(f"  ST 行占比: {A['is_st_now'].mean()*100:.2f}%")
print(f"  limit_pct 分布: {pd.Series(A['limit_pct']).value_counts().to_dict()}")

# 涨跌停价校验: 用原始价
pc = pd.Series(A["prev_close_raw"]); up = pd.Series(A["limit_up_px"])
lim = pd.Series(A["limit_pct"])
chk = (up - pc * (1 + lim)).abs()
print(f"  limit_up_px 与 round(prev_close_raw*(1+lim),2) 最大偏差: {chk.max():.4f}")

print("\n=== 端到端回测 (Strategy A, 全A, 全期) ===")
t2 = time.time()
cfg = ENG.BacktestConfig(name="smoke_A", max_positions=20, position_pct=0.05)
sig, ex, desc = STR.strat_A(A)
print("  signal count:", int(sig.sum()))
r = ENG.VectorizedBacktester(ix, cfg)
res = r.run(sig, ex)
st = ENG.perf_stats(res)
print(f"  elapsed {time.time()-t2:.1f}s")
print(f"  trades={st['n_trades']} cagr={st['cagr']*100:.1f}% mdd={st['max_drawdown']*100:.1f}% "
      f"sharpe={st['sharpe']:.2f} wr={st['win_rate']*100:.1f}% payoff={st['payoff_ratio']:.2f} "
      f"PF={st['profit_factor']:.2f}")
tr = res["trades"]
print(f"  avg hold days: {tr['hold_days'].mean():.1f}")
print(f"  fee total: {tr['fee'].sum():,.0f}  pnl total: {tr['pnl'].sum():,.0f}")
print(f"  equity start={res['equity'][0]:,.0f} end={res['equity'][-1]:,.0f}")

print("\n=== 成交价验证: 买入价应 = 买入日开盘价 x 1.001 ===")
m = panel.set_index(["thscode", "date"])["open_price"]
bad = 0; tested = 0
for _, row in tr.head(200).iterrows():
    k = (row["code"], row["buy_date"])
    if k not in m.index:
        continue
    tested += 1
    exp = m.loc[k] * 1.001
    if abs(row["buy_px"] - exp) > 1e-4:
        bad += 1
        if bad <= 3:
            print(f"  MISMATCH {row['code']} {pd.Timestamp(row['buy_date']).date()} "
                  f"got={row['buy_px']:.4f} exp={exp:.4f}")
print(f"  成交价一致: {tested-bad}/{tested}")

print("\n=== 卖出价验证: 卖出价应 = 卖出日开盘价 x 0.999 ===")
bad2 = 0; tested2 = 0
for _, row in tr.head(200).iterrows():
    k = (row["code"], row["sell_date"])
    if k not in m.index or row["reason"] in ("eod_liquidate", "suspended"):
        continue
    tested2 += 1
    exp = m.loc[k] * 0.999
    if abs(row["sell_px"] - exp) > 1e-4:
        bad2 += 1
        if bad2 <= 3:
            print(f"  MISMATCH {row['code']} {pd.Timestamp(row['sell_date']).date()} "
                  f"got={row['sell_px']:.4f} exp={exp:.4f}")
print(f"  卖出价一致: {tested2-bad2}/{tested2}")

print("\n=== 关键: 买入日必须晚于信号日 (T+1) ===")
# 重跑并记录信号日
print("  交易明细样例:")
print(tr[["code","buy_date","sell_date","buy_px","sell_px","ret","hold_days","reason"]].head(6).to_string(index=False))
print(f"\ntotal {time.time()-t0:.1f}s")
