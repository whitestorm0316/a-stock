#!/usr/bin/env python3
"""
02_build_dataset.py — 构建复权行情面板 + 全量技术指标

关键设计（防未来函数）:
  * 复权: 用复权因子事件流重建"前复权"价格, 复权因子仅使用除权除息日 <= 当日的信息
  * 指标: 所有滚动/EMA 指标只用 <= t 的数据
  * 涨跌停: 用"尾随窗口内触及的涨跌停价位"反推该股当时的涨跌幅限制制度(5/10/20/30%),
            不使用未来信息
  * 新股: 依据 list_date 计算上市天数, 用于剔除上市初期样本

产出:
  data/processed/panel.parquet   全字段面板
  data/processed/universe.json   股票池定义(固定池 + 动态池)
  data/processed/calendar.json   交易日历
"""
import os
import json
import time
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
PROC = os.path.join(ROOT, "data", "processed")
os.makedirs(PROC, exist_ok=True)


def to_date(ms):
    return (pd.to_datetime(ms, unit="ms", utc=True)
            .dt.tz_convert("Asia/Shanghai")
            .dt.normalize().dt.tz_localize(None))


def board_default_limit(thscode):
    code = thscode.split(".")[0]
    if thscode.endswith(".BJ"):
        return 0.30
    if code[:3] in ("688", "689", "300", "301"):
        return 0.20
    return 0.10


def g_roll(df, col, w, how="mean"):
    """按股票分组滚动"""
    s = df.groupby("thscode", sort=False)[col].rolling(w, min_periods=max(2, w // 2))
    s = getattr(s, how)()
    if isinstance(s.index, pd.MultiIndex):
        s = s.reset_index(level=0, drop=True)
    return s.reindex(df.index)


def g_ewm(df, col, span):
    s = df.groupby("thscode", sort=False)[col].ewm(span=span, adjust=False).mean()
    if isinstance(s.index, pd.MultiIndex):
        s = s.reset_index(level=0, drop=True)
    return s.reindex(df.index)


# ----------------------------------------------------------------------------
# 1. 复权
# ----------------------------------------------------------------------------
def build_adjusted_prices(df, adj):
    """用复权因子重建前复权价格 (锚定最新交易日)"""
    print("  computing adjustment factors ...")
    df = df.sort_values(["thscode", "date"]).reset_index(drop=True)

    # 每只股票的 (日期数组, 收盘价数组) 用于定位除权日前收盘
    grp = df.groupby("thscode", sort=False)
    bounds = {}
    for code, idx in grp.indices.items():
        bounds[code] = idx

    # 复权比例: ratio = (P_prev - 现金分红 + 配股比例*配股价) / (P_prev * (1 + 送股 + 配股))
    adj = adj.copy()
    adj["ex_date"] = to_date(adj["ex_date_ms"])
    adj = adj[adj["ex_date"].notna()]

    ratios = {}   # thscode -> list of (ex_date, ratio)
    n_missing = 0
    for code, sub in adj.groupby("thscode", sort=False):
        idx = bounds.get(code)
        if idx is None:
            n_missing += 1
            continue
        dates = df["date"].values[idx]
        closes = df["close_price"].values[idx]
        lst = []
        for _, r in sub.iterrows():
            exd = np.datetime64(r["ex_date"])
            pos = np.searchsorted(dates, exd, side="left")
            if pos == 0 or pos >= len(dates):
                continue
            p_prev = closes[pos - 1]
            if not np.isfinite(p_prev) or p_prev <= 0:
                continue
            div = float(r["dividend_per_share"] or 0.0)
            bonus = float(r["per_share_bonus"] or 0.0)
            allot = float(r["allotment_ratio"] or 0.0)
            allot_px = float(r["allotment_price"] or 0.0)
            denom = p_prev * (1.0 + bonus + allot)
            if denom <= 0:
                continue
            ratio = (p_prev - div + allot * allot_px) / denom
            if 0 < ratio < 5:
                lst.append((exd, ratio))
        if lst:
            lst.sort(key=lambda x: x[0])
            ratios[code] = lst

    print(f"    stocks with adj events: {len(ratios)} (unmatched codes: {n_missing})")

    # 逐股票计算累积因子
    cum = np.ones(len(df), dtype=np.float64)
    dates_all = df["date"].values
    for code, lst in ratios.items():
        idx = bounds[code]
        exd = np.array([x[0] for x in lst])
        rat = np.array([x[1] for x in lst])
        rev_cum = np.cumprod(rat[::-1])[::-1]        # R[i] = prod_{j>=i} rat[j]
        pos = np.searchsorted(exd, dates_all[idx], side="right")  # 首个 ex_date > 当日
        c = np.where(pos < len(rat), rev_cum[np.clip(pos, 0, len(rat) - 1)], 1.0)
        cum[idx] = c

    df["adj_factor"] = cum
    for c in ["open_price", "high_price", "low_price", "close_price"]:
        df[c + "_raw"] = df[c]
        df[c] = df[c] * cum
    df["volume_raw"] = df["volume"]
    df["volume"] = df["volume"] / cum          # 保持成交额一致
    df["turnover_raw"] = df["turnover"]
    df["turnover"] = df["turnover"]
    return df


# ----------------------------------------------------------------------------
# 2. 指标
# ----------------------------------------------------------------------------
def compute_indicators(df, macd_params=(12, 26, 9)):
    f, s, sig = macd_params
    print(f"  computing indicators (MACD {f}/{s}/{sig}) ...")
    df = df.sort_values(["thscode", "date"]).reset_index(drop=True)
    c = df["close_price"]

    df["ema_fast"] = g_ewm(df, "close_price", f)
    df["ema_slow"] = g_ewm(df, "close_price", s)
    df["dif"] = df["ema_fast"] - df["ema_slow"]
    df["dea"] = g_ewm(df, "dif", sig)
    df["hist"] = 2.0 * (df["dif"] - df["dea"])

    # 均线
    for w in (5, 10, 20, 30, 60, 120):
        df[f"ma{w}"] = g_roll(df, "close_price", w)

    # 量能
    for w in (5, 10, 20, 60):
        df[f"vol{w}"] = g_roll(df, "volume", w)
    df["vol5_vol20"] = df["vol5"] / df["vol20"]
    df["vol10_vol20"] = df["vol10"] / df["vol20"]
    df["vol5_vol60"] = df["vol5"] / df["vol60"]
    df["vol_ratio"] = df["volume"] / df["vol20"]

    # MACD 形态特征
    g = df.groupby("thscode", sort=False)
    df["hist_prev"] = g["hist"].shift(1)
    df["hist_prev2"] = g["hist"].shift(2)
    df["hist_slope"] = df["hist"] - df["hist_prev"]
    df["hist_slope_prev"] = df["hist_prev"] - df["hist_prev2"]
    df["dif_dea_dist"] = df["dif"] - df["dea"]
    df["dif_dea_dist_pct"] = df["dif_dea_dist"] / c.replace(0, np.nan)
    df["hist_cross_up"] = (df["hist"] > 0) & (df["hist_prev"] <= 0)
    df["hist_cross_dn"] = (df["hist"] < 0) & (df["hist_prev"] >= 0)
    df["golden_cross"] = (df["dif"] > df["dea"]) & (g["dif"].shift(1) <= g["dea"].shift(1))
    df["death_cross"] = (df["dif"] < df["dea"]) & (g["dif"].shift(1) >= g["dea"].shift(1))

    # 连续同向天数
    up = (df["hist_slope"] > 0).astype(np.int8)
    df["_up"] = up
    # 连续上升计数
    def _streak(x):
        out = np.zeros(len(x), dtype=np.int16)
        k = 0
        for i, v in enumerate(x):
            k = k + 1 if v else 0
            out[i] = k
        return out
    df["hist_up_streak"] = df.groupby("thscode", sort=False)["_up"].transform(_streak)
    df.drop(columns=["_up"], inplace=True)

    # 价格位置
    df["px_ma20_pct"] = (c - df["ma20"]) / df["ma20"]
    df["px_ma60_pct"] = (c - df["ma60"]) / df["ma60"]
    df["ma20_ma60"] = df["ma20"] / df["ma60"]
    df["ma60_ma120"] = df["ma60"] / df["ma120"]

    # 距离 N 日最高/最低 (含当日, 仅回看)
    for w in (20, 60):
        hi = g_roll(df, "high_price", w, "max")
        lo = g_roll(df, "low_price", w, "min")
        df[f"high{w}"] = hi
        df[f"low{w}"] = lo
        df[f"dist_high{w}"] = (c - hi) / hi          # <= 0
        df[f"dist_low{w}"] = (c - lo) / lo           # >= 0

    # 前 N 日最高(不含当日) —— 用于突破判断, 严格防未来
    for w in (20, 60):
        df[f"high{w}_prev"] = g["high_price"].transform(
            lambda x, w=w: x.rolling(w, min_periods=w).max().shift(1))

    # 日收益 / 前收盘
    df["prev_close"] = g["close_price"].shift(1)
    df["ret"] = df["close_price"] / df["prev_close"] - 1.0
    df["next_open"] = g["open_price"].shift(-1)
    df["next_close"] = g["close_price"].shift(-1)
    df["next_ret_oc"] = df["next_close"] / df["next_open"] - 1.0

    # 未来收益(仅用于事件研究, 绝不进入交易决策)
    for h in (1, 5, 10, 20, 60):
        df[f"fwd_ret{h}"] = g["close_price"].transform(
            lambda x, h=h: x.shift(-h) / x - 1.0)

    return df


# ----------------------------------------------------------------------------
# 2b. 背离检测 (底背离: 价格创新低但 MACD 未创新低)
# ----------------------------------------------------------------------------
def compute_divergence(df, lookback=60, max_gap=90):
    print(f"  detecting divergence (lookback={lookback}, max_gap={max_gap}) ...")
    df = df.sort_values(["thscode", "date"]).reset_index(drop=True)
    n = len(df)
    dv_bull = np.zeros(n, dtype=bool)      # 底背离发生日
    dv_bear = np.zeros(n, dtype=bool)

    g = df.groupby("thscode", sort=False)
    roll_low = g["low_price"].transform(
        lambda x: x.rolling(lookback, min_periods=lookback).min())
    roll_high = g["high_price"].transform(
        lambda x: x.rolling(lookback, min_periods=lookback).max())

    low = df["low_price"].values
    high = df["high_price"].values
    dif = df["dif"].values
    hist = df["hist"].values
    rl = roll_low.values
    rh = roll_high.values

    is_ll = np.isfinite(rl) & (low <= rl * 1.0001)
    is_hh = np.isfinite(rh) & (high >= rh * 0.9999)

    for code, idx in g.indices.items():
        ev = idx[is_ll[idx]]
        if len(ev) > 1:
            p, d = low[ev], dif[ev]
            for k in range(1, len(ev)):
                if (ev[k] - ev[k - 1]) > max_gap:
                    continue
                # 价格更低, DIF 更高 -> 底背离
                if p[k] < p[k - 1] * 0.999 and d[k] > d[k - 1]:
                    dv_bull[ev[k]] = True
        ev2 = idx[is_hh[idx]]
        if len(ev2) > 1:
            p2, d2 = high[ev2], dif[ev2]
            for k in range(1, len(ev2)):
                if (ev2[k] - ev2[k - 1]) > max_gap:
                    continue
                # 价格更高, DIF 更低 -> 顶背离
                if p2[k] > p2[k - 1] * 1.001 and d2[k] < d2[k - 1]:
                    dv_bear[ev2[k]] = True

    df["dv_bull"] = dv_bull
    df["dv_bear"] = dv_bear
    # 底背离后的翻红确认窗口(近10日内出现过底背离)
    df["_dvb"] = dv_bull
    df["dv_bull_recent"] = (df.groupby("thscode", sort=False)["_dvb"]
                            .transform(lambda x: x.rolling(10, min_periods=1).max())
                            .astype(bool))
    df.drop(columns=["_dvb"], inplace=True)
    print(f"    bullish divergence events: {dv_bull.sum():,}  "
          f"bearish: {dv_bear.sum():,}")
    return df


# ----------------------------------------------------------------------------
# 3. 涨跌停制度识别 (point-in-time, 全部基于未复权原始价)
# ----------------------------------------------------------------------------
def detect_limit_regime(df):
    """
    用尾随 120 日窗口内是否触及 5%/10%/20%/30% 涨跌停价, 反推当时适用的涨跌幅限制。
    全部使用未复权价, 因为交易所涨跌停价按原始价 round 到 0.01。

    产出:
      limit_pct        当日适用的涨跌幅限制
      limit_up_px      当日涨停价   = round(昨收 * (1+limit), 2)
      limit_dn_px      当日跌停价   = round(昨收 * (1-limit), 2)
      can_buy_open     当日开盘价 < 涨停价  → 开盘可买入 (一字板则不可)
      can_sell_open    当日开盘价 > 跌停价  → 开盘可卖出
    """
    print("  detecting price-limit regime (raw prices) ...")
    df = df.sort_values(["thscode", "date"]).reset_index(drop=True)
    g = df.groupby("thscode", sort=False)
    df["prev_close_raw"] = g["close_price_raw"].shift(1)

    pc = df["prev_close_raw"].values
    hi = df["high_price_raw"].values
    lo = df["low_price_raw"].values
    codes = df["thscode"].values

    best = np.full(len(df), -1.0)
    best_L = np.full(len(df), np.nan)
    for L in (0.05, 0.10, 0.20, 0.30):
        up = np.round(pc * (1 + L), 2)
        dn = np.round(pc * (1 - L), 2)
        hit = (np.isclose(hi, up, atol=0.005) | np.isclose(lo, dn, atol=0.005)).astype(np.float64)
        hit[~np.isfinite(pc)] = np.nan
        df["_hit"] = hit
        cnt = g_roll(df, "_hit", 120, "sum").values
        m = np.nan_to_num(cnt, nan=-1) > best
        best = np.where(m, np.nan_to_num(cnt, nan=-1), best)
        best_L = np.where(m, L, best_L)

    df["limit_pct"] = best_L
    nohit = ~np.isfinite(best_L)
    if nohit.any():
        bl = np.array([board_default_limit(c) for c in codes])
        df.loc[nohit, "limit_pct"] = bl[nohit]
    df.drop(columns=["_hit"], inplace=True)

    # 当日涨跌停价与开盘可交易性 (使用原始价)
    lim = df["limit_pct"].values
    df["limit_up_px"] = np.round(pc * (1 + lim), 2)
    df["limit_dn_px"] = np.round(pc * (1 - lim), 2)
    op = df["open_price_raw"].values
    valid = np.isfinite(pc) & np.isfinite(op)
    df["can_buy_open"] = valid & (op < df["limit_up_px"].values - 1e-6)
    df["can_sell_open"] = valid & (op > df["limit_dn_px"].values + 1e-6)

    # 次日是否停牌 (T+1 无 K 线) —— 仅供执行层使用, 不参与信号过滤
    nxt_exists = np.isfinite(df["prev_close_raw"].shift(-1).values)
    df["has_next_bar"] = nxt_exists
    return df


# ----------------------------------------------------------------------------
def main():
    t0 = time.time()
    print("[1/6] load raw daily k")
    df = pd.read_parquet(os.path.join(RAW, "daily_k_10y.parquet"))
    print(f"  rows={len(df):,} cols={list(df.columns)}")

    # 合并补齐的 2015-2016 早期数据
    gap_path = os.path.join(RAW, "gap_2015.parquet")
    if os.path.exists(gap_path):
        gap = pd.read_parquet(gap_path)
        for c in ("currency", "interval", "adjusted"):
            gap[c] = "none"
        gap = gap.reindex(columns=df.columns)
        df = pd.concat([gap, df], ignore_index=True)
        df = df.drop_duplicates(subset=["thscode", "date_ms"], keep="last")
        print(f"  merged gap data -> rows={len(df):,}")

    df["date"] = to_date(df["date_ms"])
    print(f"  date range: {df['date'].min().date()} ~ {df['date'].max().date()}")
    print(f"  n stocks: {df['thscode'].nunique()}")

    print("[2/6] load adjustment factors")
    adj_path = os.path.join(RAW, "adj_factors.parquet")
    if os.path.exists(adj_path):
        adj = pd.read_parquet(adj_path)
        print(f"  adj events: {len(adj):,}")
        df = build_adjusted_prices(df, adj)
    else:
        print("  !! adjustment factors missing, using raw prices")
        df["adj_factor"] = 1.0

    print("[3/6] merge ticker meta")
    tickers = json.load(open(os.path.join(RAW, "tickers_ashare.json")))
    if isinstance(tickers, dict):
        tickers = tickers["data"]["item"]
    tk = pd.DataFrame(tickers)[["thscode", "name", "list_date", "exchange"]]
    tk["list_date"] = pd.to_datetime(tk["list_date"], errors="coerce")
    df = df.merge(tk, on="thscode", how="left")
    df["is_st_now"] = df["name"].fillna("").str.contains("ST")
    df["board"] = np.where(df["thscode"].str.endswith(".BJ"), "BJ",
                  np.where(df["thscode"].str[:3].isin(["688", "689"]), "STAR",
                  np.where(df["thscode"].str[:3].isin(["300", "301"]), "CHINEXT", "MAIN")))

    print("[4/6] compute indicators")
    df = compute_indicators(df)

    print("[4b] compute divergence")
    df = compute_divergence(df)

    print("[5/6] detect limit regime")
    df = detect_limit_regime(df)

    # 上市天数
    df["days_since_list"] = (df["date"] - df["list_date"]).dt.days

    keep = ["thscode", "ticker",
            "date", "name", "exchange", "board", "list_date", "days_since_list",
            "is_st_now", "limit_pct", "limit_up_px", "limit_dn_px",
            "can_buy_open", "can_sell_open", "has_next_bar", "adj_factor",
            "open_price", "high_price", "low_price", "close_price",
            "open_price_raw", "high_price_raw", "low_price_raw", "close_price_raw",
            "volume", "turnover", "prev_close", "prev_close_raw", "ret",
            "ma5", "ma10", "ma20", "ma30", "ma60", "ma120",
            "vol5", "vol10", "vol20", "vol60",
            "vol5_vol20", "vol10_vol20", "vol5_vol60", "vol_ratio",
            "dif", "dea", "hist", "hist_prev", "hist_slope", "hist_slope_prev",
            "dif_dea_dist", "dif_dea_dist_pct", "hist_cross_up", "hist_cross_dn",
            "golden_cross", "death_cross", "hist_up_streak",
            "px_ma20_pct", "px_ma60_pct", "ma20_ma60", "ma60_ma120",
            "high20", "low20", "high60", "low60",
            "dist_high20", "dist_low20", "dist_high60", "dist_low60",
            "high20_prev", "high60_prev",
            "dv_bull", "dv_bear", "dv_bull_recent",
            "fwd_ret1", "fwd_ret5", "fwd_ret10", "fwd_ret20", "fwd_ret60"]
    keep = [c for c in keep if c in df.columns]
    out = df[keep].copy()

    # 降精度省内存
    f32 = [c for c in out.columns if out[c].dtype == np.float64]
    if f32:
        out[f32] = out[f32].astype(np.float32)

    print("[6/6] save panel")
    p = os.path.join(PROC, "panel.parquet")
    out.to_parquet(p, index=False, compression="zstd")
    print(f"  saved {p}  {os.path.getsize(p)/1e6:.1f} MB  rows={len(out):,}")

    # 交易日历
    cal = sorted(out["date"].unique())
    json.dump([str(pd.Timestamp(d).date()) for d in cal],
              open(os.path.join(PROC, "calendar.json"), "w"))
    print(f"  calendar: {len(cal)} trading days, {cal[0]} ~ {cal[-1]}")
    print(f"done in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
