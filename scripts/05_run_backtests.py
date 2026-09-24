#!/usr/bin/env python3
"""
05_run_backtests.py — 主回测网格

维度:
  策略 A~I (含归因对照 H/I)
  股票池: 全A / 沪深300 / 中证500 / 中证1000 / 动态前300
  期间:   train(2015-2022) / valid(2023-2024) / test(2025-2026) / full(2015-2026)

股票池定义:
  固定池 = 指数"当前"成分股 (存在幸存者偏差, 用于与动态池对比)
  动态池 = 每日按成交额排名前 N (point-in-time, 无幸存者偏差)
"""
import os
import sys
import json
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
STR = load(os.path.join(ROOT, "scripts", "04_strategies.py"), "str")

PROC = os.path.join(ROOT, "data", "processed")
RAW = os.path.join(ROOT, "data", "raw")
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

PERIODS = {
    "train": ("2015-01-01", "2022-12-31"),
    "valid": ("2023-01-01", "2024-12-31"),
    "test":  ("2025-01-01", "2026-12-31"),
    "full":  ("2015-01-01", "2026-12-31"),
}

STRATEGIES = [
    ("A", {}, "MACD基础(金叉买/死叉卖)"),
    ("B", {}, "MACD金叉+量能"),
    ("C", {}, "MACD+量能+趋势"),
    ("D", {}, "上涨趋势缩量回调"),
    ("E", {}, "放量突破"),
    ("F", {"mode": "right"}, "底背离右侧确认"),
    ("F", {"mode": "left"}, "底背离左侧买入"),
    ("G", {}, "MACD翻红+放量"),
    ("H", {}, "对照:纯突破(无MACD)"),
    ("I", {}, "对照:纯趋势(无MACD无量能)"),
]


def build_universe_masks(panel):
    """预计算各股票池的布尔 mask

    重要: panel 必须与 PanelIndex 内部行序一致 (即按 (date, thscode) 排序),
          否则 mask 会与信号错位, 股票池过滤完全失效。
    """
    masks = {}
    n = len(panel)
    for name in ("hs300", "zz500", "zz1000"):
        p = os.path.join(RAW, f"idx_{name}.json")
        codes = set(json.load(open(p))) if os.path.exists(p) else set()
        masks[name] = panel["thscode"].isin(codes).values
    masks["allA"] = np.ones(n, dtype=bool)

    # 动态池: 每日成交额前 N (point-in-time)
    df = pd.DataFrame({"date": panel["date"].values,
                       "amt": panel["turnover"].values})
    for N in (300, 500):
        df["rk"] = df.groupby("date")["amt"].rank(ascending=False, method="first")
        masks[f"dyn{N}"] = (df["rk"] <= N).values
    return masks


def build_regimes(panel):
    """市场环境分类 (基于全A等权指数, 仅用历史信息)"""
    d = panel.groupby("date")["ret"].mean().sort_index()
    idx = (1 + d.fillna(0)).cumprod()
    ma250 = idx.rolling(250, min_periods=120).mean()
    vol20 = d.rolling(20, min_periods=15).std() * np.sqrt(252)
    vol_med = vol20.rolling(250, min_periods=120).median()
    trend = idx / ma250 - 1
    cat = pd.Series("震荡", index=idx.index)
    cat[trend > 0.10] = "牛市"
    cat[trend < -0.10] = "熊市"
    volcat = pd.Series("中波动", index=idx.index)
    volcat[vol20 > vol_med * 1.2] = "高波动"
    volcat[vol20 < vol_med * 0.8] = "低波动"
    reg = pd.DataFrame({"trend250": trend, "vol20": vol20,
                        "regime": cat, "vol_regime": volcat})
    return reg


def main():
    t0 = time.time()
    print("loading panel ...")
    panel = pd.read_parquet(os.path.join(PROC, "panel.parquet"))
    print(f"  {len(panel):,} rows, {time.time()-t0:.0f}s")
    g = panel.groupby("thscode", sort=False)["turnover"]
    panel["amt20"] = g.transform(lambda x: x.rolling(20, min_periods=10).mean())
    print(f"  amt20 done, {time.time()-t0:.0f}s")

    # ================================================================
    # 行序约定 (极易出错, 务必遵守):
    #   panel.parquet 按 thscode 分块连续 -> 分组滚动/EMA 需要这个布局
    #   PanelIndex 内部按 (date, thscode) 排序 -> 所有 A[...] 数组是这个布局
    #   因此: 先在分块布局算 amt20, 再整体重排成 (date, thscode),
    #         之后 masks / signals / regimes 全部基于重排后的 panel。
    #   任何"用未排序 panel 造 mask、再和 ix.A 一起用"的写法都会静默错位。
    # ================================================================
    panel = panel.sort_values(["date", "thscode"], kind="stable").reset_index(drop=True)
    print(f"  sorted to (date, thscode), {time.time()-t0:.0f}s")

    umasks = build_universe_masks(panel)
    reg = build_regimes(panel)
    reg.to_parquet(os.path.join(OUT, "market_regimes.parquet"))
    print(f"  universe masks + regimes, {time.time()-t0:.0f}s")
    print("  regime distribution:")
    print("   ", reg["regime"].value_counts().to_dict())
    print("   ", reg["vol_regime"].value_counts().to_dict())

    ix = ENG.PanelIndex.get(panel)
    A = ix.A
    n = len(panel)
    print(f"  PanelIndex built, {time.time()-t0:.0f}s")

    date_arr = A["date"]
    # 期间 -> 交易日序号窗口 (用于把净值序列裁剪到该期间, 保证年化计算正确)
    period_days = {}
    for pname, (lo, hi) in PERIODS.items():
        a = int(np.searchsorted(ix.dates, np.datetime64(lo), side="left"))
        b = int(np.searchsorted(ix.dates, np.datetime64(hi), side="right")) - 1
        period_days[pname] = (a, b)
        print(f"  period {pname}: days [{a}, {b}] "
              f"{ix.dates[a]} ~ {ix.dates[b]} ({b - a + 1} days)")

    results = []
    equity_store = {}
    total = len(STRATEGIES) * (len(umasks) + 1) * len(PERIODS)
    done = 0

    for sk, params, desc in STRATEGIES:
        fn, _ = STR.build(sk, params)
        sig_raw, ex_raw, sdesc = fn(A, params)
        # 优先级: 信号强度 (用成交量比作为排序依据, 越强越优先)
        prio = A["vol_ratio"] * 1.0

        for uname, umask in list(umasks.items()) + [("noFilter", None)]:
            sig_u = sig_raw if umask is None else (sig_raw & umask)
            for pname, (lo, hi) in PERIODS.items():
                m = (date_arr >= np.datetime64(lo)) & (date_arr <= np.datetime64(hi))
                sig = sig_u & m
                ex = ex_raw & m
                cfg = ENG.BacktestConfig(
                    name=f"{sk}_{uname}_{pname}", max_positions=20,
                    position_pct=0.05, min_days_listed=120,
                    exclude_st=True, min_amount=20_000_000.0)
                try:
                    r = ENG.VectorizedBacktester(ix, cfg).run(
                        sig, ex, priority=prio, day_range=period_days[pname])
                    st = ENG.perf_stats(r)
                    st["strategy"] = sk
                    st["strategy_desc"] = desc
                    st["universe"] = uname
                    st["period"] = pname
                    st["params"] = json.dumps(params)
                    st["sig_desc"] = sdesc
                    st["n_signals"] = int(sig.sum())
                    st.pop("_res", None)
                    results.append(st)
                    if pname == "full" and uname in ("allA", "hs300"):
                        equity_store[f"{sk}_{uname}"] = (
                            pd.Series(r["equity"],
                                      index=pd.DatetimeIndex(r["dates"])))
                except Exception as e:
                    print(f"  ERR {sk}/{uname}/{pname}: {e}")
                done += 1
            if done % 40 < len(PERIODS):
                print(f"  {done}/{total} runs, {time.time()-t0:.0f}s", flush=True)

    dfr = pd.DataFrame(results)
    dfr.to_parquet(os.path.join(OUT, "main_results.parquet"))
    print(f"\nsaved {len(dfr)} results in {time.time()-t0:.0f}s")

    # 保存净值曲线
    eqdf = pd.DataFrame(equity_store)
    eqdf.to_parquet(os.path.join(OUT, "equity_curves.parquet"))
    print(f"saved equity curves: {eqdf.shape}")

    # 打印全期结果
    pd.set_option("display.width", 250)
    sub = dfr[(dfr.period == "full") & (dfr.universe.isin(["allA", "hs300"]))]
    cols = ["strategy", "universe", "cagr", "max_drawdown", "sharpe", "calmar",
            "win_rate", "payoff_ratio", "profit_factor", "n_trades",
            "avg_hold_days"]
    s2 = sub[cols].copy()
    for c in ("cagr", "max_drawdown", "win_rate"):
        s2[c] = (s2[c] * 100).round(1)
    for c in ("sharpe", "calmar", "payoff_ratio", "profit_factor", "avg_hold_days"):
        s2[c] = s2[c].round(2)
    print("\n=== 全期结果 (全A / 沪深300) ===")
    print(s2.to_string(index=False))


if __name__ == "__main__":
    main()
