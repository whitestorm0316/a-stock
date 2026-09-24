#!/usr/bin/env python3
"""diag4.py — 快速验证选股偏差假设 (纯 pandas, 不走引擎)"""
import os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
panel = pd.read_parquet(os.path.join(ROOT, "data", "processed", "panel.parquet"))
panel = panel.sort_values(["thscode", "date"]).reset_index(drop=True)

tradable = ((panel.days_since_list >= 120) & (~panel.is_st_now)
            & (panel.turnover >= 2e7) & np.isfinite(panel.close_price))
panel["tradable"] = tradable

print("=== 假设验证: 按代码顺序选股是否本身就很差? ===")
print("(模拟引擎行为: 持有代码最小的 N 只可交易股票, 每月再平衡)\n")

for N in (20, 100):
    # 每个月初选出代码最小的 N 只可交易股票
    panel["ym"] = panel["date"].dt.to_period("M")
    res = []
    for ym, sub in panel[panel.tradable].groupby("ym"):
        picks = sub["thscode"].drop_duplicates().sort_values().head(N)
        s = sub[sub.thscode.isin(picks)]
        # 当月等权收益
        r = s.groupby("date")["ret"].mean().sort_index()
        res.append((1 + r).prod() - 1)
    res = pd.Series(res, index=sorted(panel["ym"].unique()))
    nav = (1 + res).cumprod()
    yrs = len(res) / 12
    print(f"  代码最小的{N:>3}只(月度再平衡): 累计{nav.iloc[-1]-1:+.1%} "
          f"年化{nav.iloc[-1]**(1/yrs)-1:+.1%} 月胜率{(res>0).mean():.1%}")

# 随机 N 只
rng = np.random.default_rng(7)
for N in (20, 100):
    allres = []
    for trial in range(20):
        res = []
        for ym, sub in panel[panel.tradable].groupby("ym"):
            codes = sub["thscode"].drop_duplicates().values
            picks = rng.choice(codes, size=min(N, len(codes)), replace=False)
            s = sub[sub.thscode.isin(picks)]
            r = s.groupby("date")["ret"].mean().sort_index()
            res.append((1 + r).prod() - 1)
        nav = (1 + pd.Series(res)).cumprod()
        allres.append(nav.iloc[-1])
    yrs = len(res) / 12
    m = np.mean(allres)
    print(f"  随机{N:>3}只(20次试验均值): 累计{m-1:+.1%} 年化{m**(1/yrs)-1:+.1%} "
          f"[范围 {np.min(allres)-1:+.0%} ~ {np.max(allres)-1:+.0%}]")

# 全市场
r = panel[panel.tradable].groupby("date")["ret"].mean().sort_index()
nav = (1 + r).cumprod()
yrs = (nav.index[-1] - nav.index[0]).days / 365.25
print(f"\n  全市场可交易等权: 累计{nav.iloc[-1]-1:+.1%} 年化{nav.iloc[-1]**(1/yrs)-1:+.1%}")

print("\n=== 按代码段分组的买入持有收益 ===")
fl = panel[panel.tradable].groupby("thscode")["close_price"].agg(["first", "last"])
fl["seg"] = fl.index.str[:3]
seg = fl.groupby("seg").apply(lambda x: (x["last"] / x["first"]).mean() - 1,
                              include_groups=False)
print((seg.sort_values().head(8) * 100).round(1).to_string())
print("  ...")
print((seg.sort_values().tail(8) * 100).round(1).to_string())

print("\n=== 结论 ===")
print("若'代码最小的N只'表现远差于'全市场', 则引擎的代码序选股是主要偏差来源")
