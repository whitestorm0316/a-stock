#!/usr/bin/env python3
"""
07_sensitivity.py — 参数敏感性分析 + 样本外验证

原则: 只用训练集(2015-2022)选参数, 在验证集(2023-2024)与样本外(2025-2026)检验。
若最优参数附近表现急剧下降 -> 判定过拟合。

参数维度:
  MACD 参数: 8/21/5, 12/26/9, 15/30/9
  量能均线: VOL5/VOL20, VOL10/VOL20, VOL5/VOL60
  放量阈值: 1.0, 1.1, 1.2, 1.5, 2.0
  趋势均线: MA20, MA30, MA60
  回撤容忍: ±3%, ±5%, ±8%
"""
import os
import json
import time
import importlib.util
import numpy as np
import pandas as pd
from scipy.signal import lfilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ENG = load(os.path.join(ROOT, "scripts", "03_backtest.py"), "eng")
STR = load(os.path.join(ROOT, "scripts", "04_strategies.py"), "str")

PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")

PERIODS = {
    "train": ("2015-01-01", "2022-12-31"),
    "valid": ("2023-01-01", "2024-12-31"),
    "test":  ("2025-01-01", "2026-12-31"),
}


def _block_bounds(codes):
    """面板按 thscode 分块连续, 返回每块 [start, end) 边界"""
    chg = np.flatnonzero(codes[1:] != codes[:-1]) + 1
    starts = np.concatenate([[0], chg])
    ends = np.concatenate([chg, [len(codes)]])
    return starts, ends


def _ema_blocked(x, starts, ends, span):
    """分块 EMA, 初始条件对齐 pandas ewm(adjust=False): out[0] = x[0]"""
    a = 2.0 / (span + 1.0)
    b = 1.0 - a
    out = np.empty_like(x)
    for s, e in zip(starts, ends):
        xb = x[s:e]
        zi = np.array([b * xb[0]])
        out[s:e] = lfilter([a], [1.0, -b], xb, zi=zi)[0]
    return out


def _shift_blocked(x, starts, ends, k):
    """分块 shift(k): k>0 取历史, k<0 取未来; 越界为 nan"""
    out = np.full_like(x, np.nan)
    for s, e in zip(starts, ends):
        n = e - s
        if k >= 0:
            if n > k:
                out[s + k:e] = x[s:e - k]
        else:
            m = -k
            if n > m:
                out[s:e - m] = x[s + m:e]
    return out


def _sorted_index(panel):
    """构造与 PanelIndex 完全一致的行序 (date, thscode) 的索引与位置映射。

    关键: PanelIndex.__init__ 内部会 sort_values(["date","thscode"])。
    若调用方用未排序的 panel 取值, 信号与行情会完全错位。
    本函数返回 (index, pos), pos 把 panel 的行号映射到 index 的行号。
    """
    p = panel.sort_values(["date", "thscode"], kind="stable")
    return p.index.to_numpy(), p


def compute_macd(df, fast, slow, sig):
    """按指定参数重算 MACD 列 (numpy 分块, 数值与 pandas ewm 一致到 1e-12)

    注意: df 必须已按 (date, thscode) 排序 (由 _sorted_index 保证),
          否则分块边界会与 PanelIndex 不一致。
    """
    codes = df["thscode"].values
    starts, ends = _block_bounds(codes)
    c = df["close_price"].values.astype(np.float64)
    ef = _ema_blocked(c, starts, ends, fast)
    es = _ema_blocked(c, starts, ends, slow)
    dif = ef - es
    dea = _ema_blocked(dif, starts, ends, sig)
    hist = 2.0 * (dif - dea)
    hist_prev = _shift_blocked(hist, starts, ends, 1)
    dif_prev = _shift_blocked(dif, starts, ends, 1)
    dea_prev = _shift_blocked(dea, starts, ends, 1)

    out = df.copy()
    out["dif"] = dif
    out["dea"] = dea
    out["hist"] = hist
    out["hist_prev"] = hist_prev
    out["hist_slope"] = hist - hist_prev
    out["hist_cross_up"] = (hist > 0) & (hist_prev <= 0)
    out["hist_cross_dn"] = (hist < 0) & (hist_prev >= 0)
    out["golden_cross"] = (dif > dea) & (dif_prev <= dea_prev)
    out["death_cross"] = (dif < dea) & (dif_prev >= dea_prev)
    return out


def evaluate(ix, A, sig, ex, prio, lo, hi, cfg_kw=None):
    """在 [lo, hi] 期间内回测; 净值序列被裁剪到该期间, 年化收益按期间天数计算"""
    a = int(np.searchsorted(ix.dates, np.datetime64(lo), side="left"))
    b = int(np.searchsorted(ix.dates, np.datetime64(hi), side="right")) - 1
    kw = dict(max_positions=20, position_pct=0.05, min_days_listed=120,
              exclude_st=True, min_amount=20_000_000.0)
    if cfg_kw:
        kw.update(cfg_kw)
    cfg = ENG.BacktestConfig(name="s", **kw)
    r = ENG.VectorizedBacktester(ix, cfg).run(sig, ex, priority=prio,
                                              day_range=(a, b))
    return ENG.perf_stats(r)


def main():
    t0 = time.time()
    print("loading panel ...")
    base_cols = ["thscode", "date", "exchange", "open_price", "close_price",
                 "volume", "turnover", "can_buy_open", "can_sell_open",
                 "ma5", "ma20", "ma30", "ma60", "ma120",
                 "vol5", "vol10", "vol20", "vol60", "vol_ratio",
                 "vol5_vol20", "vol10_vol20", "vol5_vol60",
                 "hist", "dif", "dea", "death_cross", "high20_prev",
                 "days_since_list", "is_st_now"]
    panel = pd.read_parquet(os.path.join(PROC, "panel.parquet"), columns=base_cols)
    # 数据布局说明:
    #   panel.parquet 按 thscode 分块连续 (便于分块 EMA)
    #   PanelIndex 内部按 (date, thscode) 排序
    #   因此: 先在分块布局算 MACD, 再用一次预算好的排序置换 perm 重排,
    #         使 A 字典与 ix 的行序严格一致 (否则信号与行情完全错位)
    g = panel.groupby("thscode", sort=False)["turnover"]
    panel["amt20"] = g.transform(lambda x: x.rolling(20, min_periods=10).mean())
    panel = panel[base_cols + ["amt20"]]
    perm = panel.sort_values(["date", "thscode"], kind="stable").index.to_numpy()
    print(f"  {len(panel):,} rows, sort permutation ready, {time.time()-t0:.0f}s")

    def to_engine(df_blocked):
        """分块布局 -> (date, thscode) 布局, 返回 (df_sorted, A_dict, PanelIndex)"""
        p = df_blocked.iloc[perm].reset_index(drop=True)
        A = {c: p[c].values for c in p.columns}
        return p, A, ENG.PanelIndex.get(p)

    base_cols = ["thscode", "date", "exchange", "open_price", "close_price",
                 "volume", "turnover", "can_buy_open", "can_sell_open",
                 "ma5", "ma20", "ma30", "ma60", "ma120",
                 "vol5", "vol10", "vol20", "vol60", "vol_ratio",
                 "vol5_vol20", "vol10_vol20", "vol5_vol60",
                 "hist", "dif", "dea", "death_cross", "high20_prev",
                 "days_since_list", "is_st_now", "amt20"]
    panel = panel[base_cols].copy()

    # ---------------- 1. MACD 参数敏感性 ----------------
    print("\n" + "=" * 78)
    print("1. MACD 参数敏感性 (策略G: MACD翻红+VR>1.1)")
    print("=" * 78)
    rows = []
    for fast, slow, sigp in [(8, 21, 5), (12, 26, 9), (15, 30, 9), (5, 35, 5), (10, 22, 7)]:
        p2 = compute_macd(panel, fast, slow, sigp)
        _, A2, ix2 = to_engine(p2)
        sig = A2["hist_cross_up"] & (A2["vol_ratio"] > 1.1)
        ex = A2["death_cross"]
        prio = A2["vol_ratio"]
        rec = {"macd": f"{fast}/{slow}/{sigp}"}
        for pn, (lo, hi) in PERIODS.items():
            st = evaluate(ix2, A2, sig, ex, prio, lo, hi)
            rec[f"cagr_{pn}"] = st["cagr"]
            rec[f"mdd_{pn}"] = st["max_drawdown"]
            rec[f"sharpe_{pn}"] = st["sharpe"]
            rec[f"n_{pn}"] = st["n_trades"]
        rows.append(rec)
        print(f"  {fast}/{slow}/{sigp}: train {rec['cagr_train']*100:+.1f}% "
              f"valid {rec['cagr_valid']*100:+.1f}% test {rec['cagr_test']*100:+.1f}% "
              f"({time.time()-t0:.0f}s)", flush=True)
    mdf = pd.DataFrame(rows)
    mdf.to_csv(os.path.join(OUT, "sens_macd.csv"), index=False)

    # ---------------- 2. 放量阈值敏感性 ----------------
    print("\n" + "=" * 78)
    print("2. 放量阈值敏感性 (策略G, MACD 12/26/9)")
    print("=" * 78)
    p3 = compute_macd(panel, 12, 26, 9)
    _, A3, ix3 = to_engine(p3)
    rows = []
    for th in [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 2.0, 2.5]:
        sig = A3["hist_cross_up"] & (A3["vol_ratio"] > th)
        ex = A3["death_cross"]
        rec = {"vr_th": th}
        for pn, (lo, hi) in PERIODS.items():
            st = evaluate(ix3, A3, sig, ex, A3["vol_ratio"], lo, hi)
            rec[f"cagr_{pn}"] = st["cagr"]
            rec[f"mdd_{pn}"] = st["max_drawdown"]
            rec[f"sharpe_{pn}"] = st["sharpe"]
            rec[f"n_{pn}"] = st["n_trades"]
            rec[f"wr_{pn}"] = st["win_rate"]
        rows.append(rec)
        print(f"  VR>{th}: train {rec['cagr_train']*100:+.1f}% valid {rec['cagr_valid']*100:+.1f}% "
              f"test {rec['cagr_test']*100:+.1f}%", flush=True)
    vdf = pd.DataFrame(rows)
    vdf.to_csv(os.path.join(OUT, "sens_vr.csv"), index=False)

    # ---------------- 3. 量能均线组合 ----------------
    print("\n" + "=" * 78)
    print("3. 量能均线组合敏感性")
    print("=" * 78)
    rows = []
    for vcol in ["vol5_vol20", "vol10_vol20", "vol5_vol60"]:
        sig = A3["hist_cross_up"] & (A3[vcol] > 1.0) & (A3["vol_ratio"] > 1.1)
        ex = A3["death_cross"]
        rec = {"vol_ma": vcol}
        for pn, (lo, hi) in PERIODS.items():
            st = evaluate(ix3, A3, sig, ex, A3["vol_ratio"], lo, hi)
            rec[f"cagr_{pn}"] = st["cagr"]
            rec[f"sharpe_{pn}"] = st["sharpe"]
            rec[f"n_{pn}"] = st["n_trades"]
        rows.append(rec)
        print(f"  {vcol}: train {rec['cagr_train']*100:+.1f}% valid {rec['cagr_valid']*100:+.1f}% "
              f"test {rec['cagr_test']*100:+.1f}%")
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "sens_volma.csv"), index=False)

    # ---------------- 4. 趋势均线敏感性 ----------------
    print("\n" + "=" * 78)
    print("4. 趋势均线敏感性 (MACD翻红+VR>1.1+Close>MAx+MAx>MAy)")
    print("=" * 78)
    rows = []
    for mx, my in [("ma20", "ma60"), ("ma20", "ma120"), ("ma30", "ma60"),
                   ("ma30", "ma120"), ("ma60", "ma120")]:
        c = A3["close_price"]
        sig = (A3["hist_cross_up"] & (A3["vol_ratio"] > 1.1)
               & (c > A3[mx]) & (A3[mx] > A3[my]))
        ex = A3["death_cross"] | (c < A3[mx])
        rec = {"trend": f"{mx}>{my}"}
        for pn, (lo, hi) in PERIODS.items():
            st = evaluate(ix3, A3, sig, ex, A3["vol_ratio"], lo, hi)
            rec[f"cagr_{pn}"] = st["cagr"]
            rec[f"mdd_{pn}"] = st["max_drawdown"]
            rec[f"sharpe_{pn}"] = st["sharpe"]
            rec[f"n_{pn}"] = st["n_trades"]
        rows.append(rec)
        print(f"  {mx}>{my}: train {rec['cagr_train']*100:+.1f}% valid {rec['cagr_valid']*100:+.1f}% "
              f"test {rec['cagr_test']*100:+.1f}%", flush=True)
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "sens_trend.csv"), index=False)

    # ---------------- 5. 回撤容忍度 (策略D) ----------------
    print("\n" + "=" * 78)
    print("5. 回撤容忍度敏感性 (策略D 缩量回踩)")
    print("=" * 78)
    rows = []
    for tol in [0.02, 0.03, 0.05, 0.08, 0.10, 0.15]:
        c = A3["close_price"]
        mm, ml = A3["ma20"], A3["ma60"]
        sig = ((mm > ml) & (c > ml) & (A3["dif"] > 0)
               & (np.abs(A3["close_price"] / mm - 1) <= tol)
               & (A3["hist"] > A3["hist_prev"])
               & (c > A3["ma5"])
               & (A3["vol_ratio"] > 1.1)
               & (A3["vol5_vol20"] < 1.0))
        ex = A3["death_cross"] | (c < mm)
        rec = {"pullback_tol": tol}
        for pn, (lo, hi) in PERIODS.items():
            st = evaluate(ix3, A3, sig, ex, A3["vol_ratio"], lo, hi)
            rec[f"cagr_{pn}"] = st["cagr"]
            rec[f"sharpe_{pn}"] = st["sharpe"]
            rec[f"n_{pn}"] = st["n_trades"]
            rec[f"wr_{pn}"] = st["win_rate"]
            rec[f"payoff_{pn}"] = st["payoff_ratio"]
        rows.append(rec)
        print(f"  ±{tol:.0%}: train {rec['cagr_train']*100:+.1f}% valid {rec['cagr_valid']*100:+.1f}% "
              f"test {rec['cagr_test']*100:+.1f}%")
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "sens_pullback.csv"), index=False)

    print(f"\nall sensitivity analyses done in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
