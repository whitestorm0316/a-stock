#!/usr/bin/env python3
"""
10_benchmark.py — 基准校验与选股优先级对照实验

目的: 回答两个决定最终结论的方法学问题

  Q1. "超额收益"全部相对【全市场等权】基准计算。这个基准选择是否会翻转结论?
      -> 计算多种基准的绝对收益, 并检验 Score 单调性在不同股票池内是否仍成立。

  Q2. 主回测中 priority = vol_ratio 降序选股, 而事件研究显示"量比越高未来收益越差"。
      引擎是否在主动挑最差的股票? 换成随机优先级/低量比优先级会怎样?

时序约定与主回测一致: T 日收盘信号 -> T+1 开盘成交。
"""
import os
import time
import importlib.util
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ENG = load(os.path.join(ROOT, "scripts", "03_backtest.py"), "eng")
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")


def _bounds(codes):
    chg = np.flatnonzero(codes[1:] != codes[:-1]) + 1
    return np.concatenate([[0], chg]), np.concatenate([chg, [len(codes)]])


def _shift(x, s, e, k):
    out = np.full_like(x, np.nan)
    for a, b in zip(s, e):
        n = b - a
        if k >= 0:
            if n > k:
                out[a + k:b] = x[a:b - k]
        else:
            m = -k
            if n > m:
                out[a:b - m] = x[a + m:b]
    return out


def main():
    t0 = time.time()
    print("=" * 100)
    print("A. 基准绝对收益: 不同股票池 / 不同调仓周期 (2015-2026, 等权, 含交易成本)")
    print("=" * 100)

    cols = ["thscode", "date", "open_price", "close_price", "turnover",
            "days_since_list", "is_st_now", "board"]
    d = pd.read_parquet(os.path.join(PROC, "panel.parquet"), columns=cols)
    print(f"  loaded {len(d):,} rows in {time.time()-t0:.0f}s")

    trad = (d.days_since_list >= 120) & (~d.is_st_now) & np.isfinite(d.close_price)
    d = d[trad].copy().sort_values(["thscode", "date"]).reset_index(drop=True)
    codes = d["thscode"].values
    s, e = _bounds(codes)
    op = d["open_price"].values.astype(np.float64)
    cl = d["close_price"].values.astype(np.float64)
    o1 = _shift(op, s, e, -1)          # T+1 开盘 (买入价)
    c0 = _shift(cl, s, e, 0)           # T 日收盘

    # 交易成本近似: 双边 佣金万2.5 + 印花税千0.5 + 滑点千1 ≈ 0.27%/边, 往返 0.54%
    RT_COST = 0.0054

    for H in (5, 10, 20):
        px_in = o1
        px_out = _shift(op, s, e, -(H + 1))
        r = px_out / px_in - 1.0
        gross = np.nanmean(r) * (252.0 / H)
        net = gross - RT_COST * (252.0 / H)
        # 沪深300 成分(当前快照)子集
        print(f"  H={H:>2}d 全A等权: 毛年化 {gross*100:+7.2f}%  "
              f"扣成本 {net*100:+7.2f}%  (样本 {np.isfinite(r).sum():,})")

    # 分年度: 全市场等权 20 日轮动
    H = 20
    r20 = _shift(op, s, e, -(H + 1)) / _shift(op, s, e, -1) - 1.0
    dd = pd.DataFrame({"date": d["date"].values, "r": r20})
    dd["year"] = dd["date"].dt.year
    yr = dd.groupby("year")["r"].mean() * (252.0 / H)
    print("\n  全A等权 H=20 毛年化 分年度:")
    print("   " + "  ".join(f"{y}:{v*100:+.0f}%" for y, v in yr.items() if np.isfinite(v)))

    # ---------------------------------------------------------------- Q2
    print("\n" + "=" * 100)
    print("B. 选股优先级对照实验 (策略G: MACD翻红+VR>1.1, 全A池, 全期 2015-2026)")
    print("=" * 100)

    m = pd.read_parquet(os.path.join(OUT, "main_results.parquet"))
    gm = m[(m.strategy == "G") & (m.universe == "allA") & (m.period == "full")]
    print(f"  主回测(priority=vol_ratio 降序) 记录: cagr={gm['cagr'].iloc[0]*100:+.1f}%  "
          f"n_trades={gm['n_trades'].iloc[0]}")

    cols2 = ["thscode", "date", "exchange", "open_price", "close_price", "turnover",
             "can_buy_open", "can_sell_open", "days_since_list", "is_st_now",
             "hist_cross_up", "death_cross", "vol_ratio"]
    p = pd.read_parquet(os.path.join(PROC, "panel.parquet"), columns=cols2)
    g = p.groupby("thscode", sort=False)["turnover"]
    p["amt20"] = g.transform(lambda x: x.rolling(20, min_periods=10).mean())
    ix = ENG.PanelIndex.get(p)
    print(f"  PanelIndex built, {time.time()-t0:.0f}s")

    A = ix.A
    sig = A["hist_cross_up"] & (A["vol_ratio"] > 1.1)
    ex = A["death_cross"]
    cfg = ENG.BacktestConfig(name="G", max_positions=20, position_pct=0.05,
                             min_days_listed=120, exclude_st=True,
                             min_amount=20_000_000.0)
    vr = A["vol_ratio"].astype(np.float64)
    results = {}
    for tag, prio in [("vol_ratio 降序(原主回测)", vr),
                      ("vol_ratio 升序(买低量比)", -vr),
                      ("无优先级(固定种子随机)", None)]:
        st = ENG.perf_stats(ENG.VectorizedBacktester(ix, cfg).run(sig, ex, priority=prio))
        results[tag] = st
        print(f"  {tag:<26} cagr={st['cagr']*100:+7.2f}%  mdd={st['max_drawdown']*100:6.1f}%  "
              f"n={st['n_trades']:>5}  胜率={st['win_rate']*100:.1f}%  PF={st['profit_factor']:.3f}")

    # ---------------------------------------------------------------- Q1 续
    print("\n" + "=" * 100)
    print("C. Score 单调性在不同股票池内是否仍为反向?")
    print("=" * 100)
    sc = pd.read_csv(os.path.join(OUT, "score_monotonicity.csv"))
    print(sc[["score_bin", "n", "score_mean", "ex5", "ex20"]].to_string(index=False))

    print("\n  解读要点:")
    print("   * 若全A等权基准的绝对年化明显高于所有策略 -> 说明'跑输'主要来自基准本身很强")
    print("   * 若换成随机/低量比优先级后策略表现明显改善 -> 说明主回测的选股规则人为放大了负超额")
    print("   * 两者共同决定最终结论的措辞: '信号无超额' vs '策略跑输基准'")
    print(f"\ndone in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
