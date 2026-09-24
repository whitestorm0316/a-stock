#!/usr/bin/env python3
"""diag6.py — 检验选股规则偏差: 代码序 vs 流动性序 vs 随机序"""
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

print("=== 无偏估计: 信号组合法 (等权持有全部信号, 重叠分片) ===")
print("每日信号按等权建仓, 持有 N 日后等权卖出, 多个分片叠加\n")

d = panel[(panel.days_since_list >= 120) & (~panel.is_st_now)
          & (panel.turnover >= 2e7) & np.isfinite(panel.close_price)].copy()
d = d.sort_values(["thscode", "date"])
gg = d.groupby("thscode", sort=False)

# T+1 开盘买入 → T+1+H 开盘卖出
for H in (5, 10, 20, 60):
    d[f"o2o{H}"] = gg["open_price"].transform(
        lambda x, H=H: x.shift(-(H + 1)) / x.shift(-1) - 1)

sig = (d["hist_cross_up"].values & (d["vol_ratio"].values > 1.1))
print(f"信号数: {sig.sum():,}")

for H in (5, 10, 20, 60):
    v = d.loc[sig, f"o2o{H}"].values
    v = v[np.isfinite(v)]
    b = d[f"o2o{H}"].values
    b = b[np.isfinite(b)]
    # 年化: 每笔持有H日, 每年可做 250/H 轮
    ann_sig = (1 + np.mean(v)) ** (250 / H) - 1
    ann_mkt = (1 + np.mean(b)) ** (250 / H) - 1
    print(f"  H={H:>2}d: 信号 {np.mean(v)*100:>+6.3f}% → 年化{ann_sig*100:>+6.1f}%  |  "
          f"市场 {np.mean(b)*100:>+6.3f}% → 年化{ann_mkt*100:>+6.1f}%  |  "
          f"超额 {(np.mean(v)-np.mean(b))*100:>+6.3f}%")

print("\n=== 对比: 各选股规则下的组合表现 (G信号, 20持仓, 死叉退出) ===")
ix = ENG.PanelIndex.get(panel)
A = ix.A
n = len(panel)
sig_full = A["hist_cross_up"] & (A["vol_ratio"] > 1.1)
ex_full = A["death_cross"]

# 规则1: 代码序 (引擎默认)
cfg = ENG.BacktestConfig(name="codeorder", max_positions=20, position_pct=0.05)
r1 = ENG.VectorizedBacktester(ix, cfg).run(sig_full, ex_full)
s1 = ENG.perf_stats(r1)
print(f"  代码序:        cagr={s1['cagr']*100:>6.1f}% trades={s1['n_trades']:>5} "
      f"wr={s1['win_rate']*100:>4.1f}% avg={s1['expectancy']*100:>+6.3f}%")

# 规则2: 随机序 (打散信号)
rng = np.random.default_rng(42)
for trial in range(3):
    perm = rng.permutation(n)
    # 保持每日信号数量不变, 但随机分配给同日的其他股票
    order = np.argsort(A["date"], kind="stable")
    s2 = np.zeros(n, dtype=bool)
    df_tmp = pd.DataFrame({"date": A["date"], "sig": sig_full})
    cnt = df_tmp.groupby("date")["sig"].transform("sum")
    rand = rng.random(n)
    df_tmp["r"] = rand
    df_tmp["rank"] = df_tmp.groupby("date")["r"].rank(method="first")
    s2 = ((df_tmp["rank"] <= cnt) & (cnt > 0)).values
    cfg2 = ENG.BacktestConfig(name=f"rand{trial}", max_positions=20, position_pct=0.05)
    r2 = ENG.VectorizedBacktester(ix, cfg2).run(s2, ex_full)
    st2 = ENG.perf_stats(r2)
    print(f"  随机序{trial}:      cagr={st2['cagr']*100:>6.1f}% trades={st2['n_trades']:>5} "
          f"wr={st2['win_rate']*100:>4.1f}% avg={st2['expectancy']*100:>+6.3f}%")

# 规则3: 流动性优先 (成交额最大优先, 现实可行)
liq = A["turnover"]
df3 = pd.DataFrame({"date": A["date"], "sig": sig_full, "liq": liq})
df3["rank"] = df3.groupby("date")["liq"].rank(ascending=False, method="first")
df3["nsig"] = df3.groupby("date")["sig"].transform("sum")
s3 = ((df3["rank"] <= df3["nsig"]) & (df3["nsig"] > 0)).values
cfg3 = ENG.BacktestConfig(name="liq", max_positions=20, position_pct=0.05)
r3 = ENG.VectorizedBacktester(ix, cfg3).run(s3, ex_full)
st3 = ENG.perf_stats(r3)
print(f"  流动性优先:    cagr={st3['cagr']*100:>6.1f}% trades={st3['n_trades']:>5} "
      f"wr={st3['win_rate']*100:>4.1f}% avg={st3['expectancy']*100:>+6.3f}%")

print("\n=== 引擎持仓股票代码分布 (代码序规则) ===")
tr = r1["trades"]
seg = tr["code"].str[:3].value_counts().head(12)
print(seg.to_string())
print(f"\n  全部交易涉及 {tr['code'].nunique()} 只不同股票 / 全市场 {panel['thscode'].nunique()} 只")
