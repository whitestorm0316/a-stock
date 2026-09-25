#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
21_build_v2_features.py — V2 因子研究特征工程

在 panel.parquet 基础上构建 V2 特征集。严格防未来函数:
  所有滚动窗口只使用 <= t 的数据; 所有"前 N 日"统计使用 [t-N, t-1] (不含当日);
  所有未来收益使用 T+1 开盘买入 -> T+1+H 开盘卖出。

分两个阶段(内存与行序都关键):
  阶段1 在 panel 的【按 thscode 分块】布局上算所有"按股票分组"的特征
        (滚动统计/EMA/shift 都需要块内连续)
  阶段2 重排为【(date, thscode)】布局, 才能算跨股票的日度聚合
        (市场收益/行业收益/规模分组/相对强弱)

产出:
  data/processed/v2_panel.parquet
"""
import json
import os
import sys
import time
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
RAW = os.path.join(ROOT, "data", "raw")

HORIZONS = [1, 5, 10, 20, 40, 60]
MIN_LIST = 120          # 上市满 120 交易日才纳入
t0 = time.time()


def log(msg):
    print(f"[{time.time()-t0:7.1f}s] {msg}", flush=True)


# ---------------------------------------------------------------- 分块工具
def block_bounds(codes):
    """codes 为分块连续数组, 返回每块的 [start, end) 边界"""
    chg = np.flatnonzero(codes[1:] != codes[:-1]) + 1
    return (np.concatenate([[0], chg]), np.concatenate([chg, [len(codes)]]))


def _agg(w, how):
    if how == "mean":
        return w.mean(axis=1)
    if how == "median":
        return np.median(w, axis=1)
    if how == "std":
        return w.std(axis=1, ddof=1)
    if how == "max":
        return w.max(axis=1)
    if how == "min":
        return w.min(axis=1)
    if how == "sum":
        return w.sum(axis=1)
    raise ValueError(how)


def roll_cur(x, starts, ends, W, how):
    """滚动窗口 [t-W+1, t] (含当日)"""
    out = np.full(len(x), np.nan, dtype=np.float64)
    for s, e in zip(starts, ends):
        xb = np.asarray(x[s:e], dtype=np.float64)
        n = len(xb)
        if n < W:
            continue
        v = sliding_window_view(xb, W)          # v[i] = xb[i:i+W]
        out[s + W - 1:e] = _agg(v, how)
    return out


def roll_prev(x, starts, ends, W, how):
    """滚动窗口 [t-W, t-1] (不含当日), 用于 RVOL / Volume Z / 前N日最高"""
    out = np.full(len(x), np.nan, dtype=np.float64)
    for s, e in zip(starts, ends):
        xb = np.asarray(x[s:e], dtype=np.float64)
        n = len(xb)
        if n < W + 1:
            continue
        v = sliding_window_view(xb, W)          # len n-W+1, v[i]=xb[i:i+W]
        st = _agg(v, how)                       # st[i] 对应窗口 xb[i:i+W]
        # 窗口 [t-W, t-1] 对应 i = t-W, 故 t 从 W 到 n-1
        out[s + W:e] = st[:n - W]
    return out


def shift_blk(x, starts, ends, k):
    """分块 shift: k>0 取历史, k<0 取未来; 越界 nan"""
    out = np.full(len(x), np.nan, dtype=np.float64)
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


def roll_count(x, starts, ends, W, how="sum"):
    """滚动计数(含当日), 用于 Volume Structure 状态计数"""
    return roll_cur(np.asarray(x, dtype=np.float64), starts, ends, W, how)


# ---------------------------------------------------------------- 阶段 0: 读入
def load():
    cols = [
        "thscode", "date", "name", "board", "list_date", "days_since_list",
        "is_st_now", "can_buy_open", "can_sell_open", "limit_pct",
        "open_price", "high_price", "low_price", "close_price",
        # 未复权收盘价：面值退市（连续20日收盘<1元）必须用它判断。
        # 前复权价因分红送转会被整体调低，用它判 <1 元会大幅误判
        # （实测前复权 <1 占比 0.016% vs 未复权 0.004%，差 4 倍）。
        "close_price_raw",
        "volume", "turnover", "ret",
        "ma5", "ma20", "ma30", "ma60", "ma120",
        "vol5", "vol10", "vol20", "vol60",
        "vol_ratio", "vol5_vol20", "vol10_vol20", "vol5_vol60",
        "dif", "dea", "hist", "hist_slope", "hist_cross_up", "hist_cross_dn",
        "golden_cross", "death_cross",
        "px_ma20_pct", "px_ma60_pct",
        "high20_prev", "high60_prev", "high120", "high20", "high60",
        "dv_bull", "dv_bull_recent",
    ]
    have = pd.read_parquet(os.path.join(PROC, "panel.parquet"), columns=None).columns
    cols = [c for c in cols if c in have]
    log(f"panel 列命中 {len(cols)}")
    df = pd.read_parquet(os.path.join(PROC, "panel.parquet"), columns=cols)
    log(f"读入 {df.shape[0]:,} 行 × {df.shape[1]} 列")
    return df


# ---------------------------------------------------------------- 阶段 1: 按股票分块
def build_block_features(df):
    codes = df["thscode"].values
    s_, e_ = block_bounds(codes)
    log(f"股票块数 {len(s_)}")

    V = lambda c: df[c].values.astype(np.float64)
    vol = V("volume")
    close = V("close_price")
    high = V("high_price")
    low = V("low_price")
    ret = V("ret")
    hist = V("hist")
    dif = V("dif")
    dea = V("dea")
    amt = V("turnover")

    out = {}

    # ---------- 2.1 RVOL (Median, 不含当日) + 2.2 VR (Mean, 不含当日)
    for W in (10, 20, 60):
        med = roll_prev(vol, s_, e_, W, "median")
        out[f"rvol{W}"] = vol / med
        out[f"vr{W}m"] = vol / roll_prev(vol, s_, e_, W, "mean")
    log("RVOL / VR20 完成")

    # ---------- 2.3 Volume Trend
    v5 = roll_cur(vol, s_, e_, 5, "mean")
    v10 = roll_cur(vol, s_, e_, 10, "mean")
    v20 = roll_cur(vol, s_, e_, 20, "mean")
    v60 = roll_cur(vol, s_, e_, 60, "mean")
    out["vt_5_20"] = v5 / v20
    out["vt_10_20"] = v10 / v20
    out["vt_20_60"] = v20 / v60
    # 量能持续改善: vt_5_20 连续 N 日上行
    vt = out["vt_5_20"].copy()
    vt_prev1 = shift_blk(vt, s_, e_, 1)
    vt_prev2 = shift_blk(vt, s_, e_, 2)
    up1 = (vt > vt_prev1).astype(np.float64)
    up1[np.isnan(vt) | np.isnan(vt_prev1)] = np.nan
    up2 = (vt_prev1 > vt_prev2).astype(np.float64)
    up2[np.isnan(vt_prev1) | np.isnan(vt_prev2)] = np.nan
    out["vt_up1"] = up1
    out["vt_up3"] = ((up1 == 1) & (up2 == 1)).astype(np.float64)
    out["dvt5"] = vt - shift_blk(vt, s_, e_, 5)
    out["dvt10"] = vt - shift_blk(vt, s_, e_, 10)
    log("Volume Trend 完成")

    # ---------- 2.4 Volume Acceleration
    for k in (1, 3, 5):
        out[f"vchg{k}"] = vol / shift_blk(vol, s_, e_, k) - 1.0

    # ---------- 2.5 Volume Z-Score (不含当日)
    m20 = roll_prev(vol, s_, e_, 20, "mean")
    sd20 = roll_prev(vol, s_, e_, 20, "std")
    vz = (vol - m20) / sd20
    vz[~np.isfinite(vz)] = np.nan
    out["volz20"] = vz
    out["volz_bin"] = np.digitize(vz, [-1.0, 0.0, 1.0, 2.0]).astype(np.float64)
    out["volz_bin"][np.isnan(vz)] = np.nan
    log("Volume Acceleration / Z-Score 完成")

    # ---------- 2.6 Turnover 代理: 成交额滚动均值(不含当日, 用于分层防未来)
    out["amt20_lag"] = roll_prev(amt, s_, e_, 20, "mean")
    out["amt60_lag"] = roll_prev(amt, s_, e_, 60, "mean")
    # 均价(元/股)
    out["vwap"] = np.where(vol > 0, amt / vol, np.nan)
    log("成交额代理 完成")

    # ---------- 三、Volume 方向: 上涨日/下跌日成交量
    is_up = (ret > 0).astype(np.float64)
    is_dn = (ret < 0).astype(np.float64)
    is_up[np.isnan(ret)] = np.nan
    is_dn[np.isnan(ret)] = np.nan
    up_vol = np.where(is_up == 1, vol, 0.0)
    dn_vol = np.where(is_dn == 1, vol, 0.0)
    up_vol[np.isnan(is_up)] = np.nan
    dn_vol[np.isnan(is_dn)] = np.nan
    for W in (5, 10, 20):
        su = roll_cur(up_vol, s_, e_, W, "sum")
        sd = roll_cur(dn_vol, s_, e_, W, "sum")
        sv = roll_cur(vol, s_, e_, W, "sum")
        cu = roll_cur(is_up, s_, e_, W, "sum")
        cd = roll_cur(is_dn, s_, e_, W, "sum")
        out[f"upvol_r{W}"] = su / sv
        out[f"dnvol_r{W}"] = sd / sv
        uavg = su / np.where(cu > 0, cu, np.nan)
        davg = sd / np.where(cd > 0, cd, np.nan)
        out[f"ud_vol{W}"] = uavg / davg
    log("Volume 方向 完成")

    # ---------- 四、Volume Structure 四状态计数 (含当日)
    vr = out["vr20m"]
    big = (vr > 1.0).astype(np.float64)
    small = (vr <= 1.0).astype(np.float64)
    big[np.isnan(vr)] = np.nan
    small[np.isnan(vr)] = np.nan
    A = ((ret > 0) & (vr <= 1.0)).astype(np.float64)   # 缩量上涨
    B = ((ret > 0) & (vr > 1.0)).astype(np.float64)    # 放量上涨
    C = ((ret < 0) & (vr <= 1.0)).astype(np.float64)   # 缩量下跌
    D = ((ret < 0) & (vr > 1.0)).astype(np.float64)    # 放量下跌
    for arr in (A, B, C, D):
        arr[np.isnan(ret) | np.isnan(vr) | (ret == 0)] = np.nan
    for W in (5, 10, 20):
        out[f"stA{W}"] = roll_cur(A, s_, e_, W, "sum")
        out[f"stB{W}"] = roll_cur(B, s_, e_, W, "sum")
        out[f"stC{W}"] = roll_cur(C, s_, e_, W, "sum")
        out[f"stD{W}"] = roll_cur(D, s_, e_, W, "sum")
    out["stA1"], out["stB1"], out["stC1"], out["stD1"] = A, B, C, D
    out["vr_gt1"] = big
    log("Volume Structure 完成")

    # ---------- 五/六、MACD 特征
    out["dif_pos"] = (dif > 0).astype(np.float64)
    out["dea_pos"] = (dea > 0).astype(np.float64)
    out["hist_pos"] = (hist > 0).astype(np.float64)
    out["dif_neg"] = (dif < 0).astype(np.float64)
    gc = V("golden_cross")
    out["gc_above0"] = gc * (dif > 0)
    out["gc_below0"] = gc * (dif < 0)
    out["gc_dea_above0"] = gc * (dea > 0)
    out["gc_dea_below0"] = gc * (dea < 0)

    # hist 变化速度与加速度
    d1 = hist - shift_blk(hist, s_, e_, 1)
    out["dhist1"] = d1
    out["dhist3"] = hist - shift_blk(hist, s_, e_, 3)
    out["dhist5"] = hist - shift_blk(hist, s_, e_, 5)
    out["hist_acc"] = d1 - shift_blk(d1, s_, e_, 1)

    # hist 归一化(按股票滚动标准差), 使跨股票可比
    hsd = roll_cur(hist, s_, e_, 60, "std")
    hsd = np.where(hsd > 0, hsd, np.nan)
    out["hist_z"] = hist / hsd
    out["dhist1_z"] = d1 / hsd
    out["dhist3_z"] = out["dhist3"] / hsd

    # 三态分类
    d3 = out["dhist3"]
    d1z = out["dhist1_z"]
    hz = out["hist_z"]
    # 状态1: 持续改善但未翻红
    st1 = ((hist < 0) & (d1 > 0) & (d3 > 0)).astype(np.float64)
    # 状态2: 快速翻红 (当日翻正 且 斜率处于自身高位)
    fast = np.where(np.isnan(d1z), np.nan, (d1z > 1.0).astype(np.float64))
    st2 = V("hist_cross_up") * fast
    # 状态3: 高位衰减
    hmax20 = roll_cur(hist, s_, e_, 20, "max")
    st3 = ((hist > 0) & (d1 < 0) & (hist > 0.5 * hmax20)).astype(np.float64)
    for arr in (st1, st2, st3):
        arr[np.isnan(hist)] = np.nan
    out["st_slowimp"] = st1
    out["st_fastflip"] = st2
    out["st_decay"] = st3
    # 连续改善天数(hist 上行 streak)
    up_hist = (d1 > 0).astype(np.float64)
    up_hist[np.isnan(d1)] = np.nan
    out["hist_upstreak"] = roll_cur(up_hist, s_, e_, 5, "sum")
    out["hist_up3"] = (out["hist_upstreak"] >= 3).astype(np.float64)
    out["hist_up5"] = (out["hist_upstreak"] >= 5).astype(np.float64)
    log("MACD 特征 完成")

    # ---------- 八、Price Position
    out["px_ma120_pct"] = close / V("ma120") - 1.0
    h20p, h60p = V("high20_prev"), V("high60_prev")
    out["px_h20"] = close / h20p - 1.0
    out["px_h60"] = close / h60p - 1.0
    # 120 日最高: 用 [t-120, t-1] 不含当日
    h120p = roll_prev(high, s_, e_, 120, "max")
    out["px_h120"] = close / h120p - 1.0
    log("Price Position 完成")

    # ---------- 九、Momentum
    for k in (3, 5, 10, 20, 40, 60):
        out[f"ret{k}"] = close / shift_blk(close, s_, e_, k) - 1.0
    log("Momentum 完成")

    # ---------- 十、ATR / Volatility
    pc = shift_blk(close, s_, e_, 1)
    tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
    out["tr"] = tr
    out["atr14"] = roll_cur(tr, s_, e_, 14, "mean")
    out["atr20"] = roll_cur(tr, s_, e_, 20, "mean")
    out["atr_pct"] = out["atr14"] / close
    out["hv20"] = roll_cur(ret, s_, e_, 20, "std") * np.sqrt(252.0)
    out["hv60"] = roll_cur(ret, s_, e_, 60, "std") * np.sqrt(252.0)
    log("ATR / Volatility 完成")

    # ---------- 未来收益 (T+1 开盘 -> T+1+H 开盘)
    op = V("open_price")
    o1 = shift_blk(op, s_, e_, -1)
    for H in HORIZONS:
        out[f"fwd{H}"] = shift_blk(op, s_, e_, -(H + 1)) / o1 - 1.0
    log("未来收益 完成")

    return out, (s_, e_)


# ---------------------------------------------------------------- 阶段 2: 跨股票
def build_cross_features(df, out, bounds):
    s_, e_ = bounds
    # 先把所有新列挂上
    for k, v in out.items():
        df[k] = v.astype(np.float32)
    log(f"挂载 {len(out)} 个新列, shape={df.shape}")

    # 行业映射
    imap = json.load(open(os.path.join(RAW, "industry_map.json")))
    df["ind_code"] = df["thscode"].map(lambda x: (imap.get(x) or {}).get("code"))
    df["ind_name"] = df["thscode"].map(lambda x: (imap.get(x) or {}).get("name"))
    log(f"行业映射命中 {df['ind_code'].notna().mean()*100:.1f}%")

    # 干净样本(用于构造基准): 非ST, 上市>=120日, 有 ret
    clean = (~df["is_st_now"].fillna(True)) & (df["days_since_list"].fillna(0) >= MIN_LIST) \
        & df["ret"].notna()
    log(f"clean 样本 {clean.sum():,} / {len(df):,} "
        f"({clean.mean()*100:.1f}%)")

    # 规模分组: 每个交易日按 amt20_lag (前20日成交额, 不含当日) 十分位
    # 只在 clean 样本内排名, 避免 ST/新股污染
    sub = df.loc[clean, ["date", "amt20_lag", "ret", "ind_code"]].copy()
    sub["size_pct"] = sub.groupby("date")["amt20_lag"].rank(pct=True)
    # 注意: rank 在 amt20_lag 全空的日子会产出 NaN, 故不能 astype("int8")
    sg = np.minimum(np.floor(sub["size_pct"].values * 10.0), 9.0)
    sg[~np.isfinite(sg) | (sub["size_pct"].values <= 0)] = np.nan
    sub["size_grp"] = sg
    df["size_grp"] = np.nan
    df.loc[clean, "size_grp"] = sub["size_grp"]
    log("规模十分位 完成")

    # 日度基准收益 (等权)
    mkt = sub.groupby("date")["ret"].mean()
    ind = sub.groupby(["date", "ind_code"])["ret"].mean()
    szg = sub.groupby(["date", "size_grp"])["ret"].mean()

    mkt = mkt.sort_index()
    # 净值化, 便于算 k 日收益
    def kret(nav, k):
        """由日度净值序列算 k 日收益 nav_t/nav_{t-k}-1"""
        return nav / nav.shift(k) - 1.0

    mkt_nav = (1 + mkt).cumprod()
    df = df.merge(mkt.rename("mkt_ret1"), left_on="date", right_index=True, how="left")
    for k in (5, 10, 20, 60):
        kv = kret(mkt_nav, k).rename(f"mkt_r{k}")
        df = df.merge(kv, left_on="date", right_index=True, how="left")

    ind_df = ind.reset_index()
    ind_df.columns = ["date", "ind_code", "ind_ret1"]
    ind_nav = ind_df.copy()
    ind_nav["nav"] = ind_nav.groupby("ind_code")["ind_ret1"].transform(
        lambda x: (1 + x).cumprod())
    for k in (5, 10, 20, 60):
        ind_nav[f"ind_r{k}"] = ind_nav.groupby("ind_code")["nav"].transform(
            lambda x, kk=k: x / x.shift(kk) - 1.0)
    df = df.merge(ind_nav[["date", "ind_code", "ind_ret1"] + [f"ind_r{k}" for k in (5, 10, 20, 60)]],
                  on=["date", "ind_code"], how="left")

    sz_df = szg.reset_index()
    sz_df.columns = ["date", "size_grp", "sz_ret1"]
    sz_df["sz_grp"] = sz_df.groupby("size_grp")["sz_ret1"].transform(
        lambda x: (1 + x).cumprod())
    for k in (5, 10, 20, 60):
        sz_df[f"sz_r{k}"] = sz_df.groupby("size_grp")["sz_grp"].transform(
            lambda x, kk=k: x / x.shift(kk) - 1.0)
    df = df.merge(sz_df[["date", "size_grp", "sz_ret1"] + [f"sz_r{k}" for k in (5, 10, 20, 60)]],
                  on=["date", "size_grp"], how="left")
    log("市场/行业/规模基准 完成")

    # Relative Strength
    for k in (5, 10, 20, 60):
        df[f"rs_mkt{k}"] = df[f"ret{k}"] - df[f"mkt_r{k}"]
        df[f"rs_ind{k}"] = df[f"ret{k}"] - df[f"ind_r{k}"]
        df[f"rs_sz{k}"] = df[f"ret{k}"] - df[f"sz_r{k}"]
    log("Relative Strength 完成")

    return df


# ---------------------------------------------------------------- main
def main():
    os.makedirs(PROC, exist_ok=True)
    df = load()
    out, bounds = build_block_features(df)
    log("阶段1 完成")
    df = build_cross_features(df, out, bounds)
    log("阶段2 完成")

    # 只保留必要列, 且压缩为 float32 以控制体积
    keep_meta = ["thscode", "date", "name", "board", "days_since_list",
                 "is_st_now", "can_buy_open", "can_sell_open", "limit_pct",
                 "open_price", "high_price", "low_price", "close_price",
                 "close_price_raw",
                 "volume", "turnover", "ret", "ind_code", "ind_name", "size_grp"]
    keep_feat = [
        # volume 结构
        "rvol10", "rvol20", "rvol60", "vr10m", "vr20m", "vr60m",
        "vt_5_20", "vt_10_20", "vt_20_60", "vt_up1", "vt_up3", "dvt5", "dvt10",
        "vchg1", "vchg3", "vchg5", "volz20", "volz_bin",
        "amt20_lag", "amt60_lag", "vwap",
        "upvol_r5", "upvol_r10", "upvol_r20",
        "dnvol_r5", "dnvol_r10", "dnvol_r20",
        "ud_vol5", "ud_vol10", "ud_vol20",
        "stA1", "stB1", "stC1", "stD1",
        "stA5", "stB5", "stC5", "stD5",
        "stA10", "stB10", "stC10", "stD10",
        "stA20", "stB20", "stC20", "stD20",
        # MACD
        "dif", "dea", "hist", "dif_pos", "dea_pos", "hist_pos", "dif_neg",
        "golden_cross", "death_cross", "hist_cross_up", "hist_cross_dn",
        "gc_above0", "gc_below0", "gc_dea_above0", "gc_dea_below0",
        "dhist1", "dhist3", "dhist5", "hist_acc",
        "hist_z", "dhist1_z", "dhist3_z",
        "st_slowimp", "st_fastflip", "st_decay",
        "hist_upstreak", "hist_up3", "hist_up5",
        # price position
        "px_ma20_pct", "px_ma60_pct", "px_ma120_pct",
        "px_h20", "px_h60", "px_h120",
        # momentum
        "ret3", "ret5", "ret10", "ret20", "ret40", "ret60",
        # volatility
        "atr14", "atr20", "atr_pct", "hv20", "hv60",
        # benchmark / RS
        "mkt_ret1", "ind_ret1", "sz_ret1",
        "mkt_r5", "mkt_r10", "mkt_r20", "mkt_r60",
        "ind_r5", "ind_r10", "ind_r20", "ind_r60",
        "sz_r5", "sz_r10", "sz_r20", "sz_r60",
        "rs_mkt5", "rs_mkt10", "rs_mkt20", "rs_mkt60",
        "rs_ind5", "rs_ind10", "rs_ind20", "rs_ind60",
        "rs_sz5", "rs_sz10", "rs_sz20", "rs_sz60",
        # 未来收益
        "fwd1", "fwd5", "fwd10", "fwd20", "fwd40", "fwd60",
    ]
    cols = [c for c in keep_meta + keep_feat if c in df.columns]
    df = df[cols]
    log(f"最终 {df.shape[0]:,} 行 × {df.shape[1]} 列")

    # 对齐最终行序: (date, thscode) 便于后续分析
    df = df.sort_values(["date", "thscode"], kind="stable").reset_index(drop=True)
    log("已按 (date, thscode) 排序")

    outp = os.path.join(PROC, "v2_panel.parquet")
    df.to_parquet(outp, compression="zstd", index=False)
    sz = os.path.getsize(outp) / 1024 / 1024
    log(f"保存 {outp}  {sz:.1f} MB")

    # 摘要
    print("\n=== 关键特征缺失率 ===")
    chk = ["rvol20", "vr20m", "volz20", "vt_5_20", "upvol_r20", "stD20",
           "hist_z", "gc_above0", "gc_below0", "px_h60", "ret20",
           "atr14", "hv20", "rs_ind20", "rs_sz20", "fwd20", "size_grp"]
    for c in chk:
        if c in df.columns:
            print(f"  {c:12s} 缺失 {df[c].isna().mean()*100:6.2f}%")
    print(f"\n行业数: {df['ind_code'].nunique()}  规模组: {df['size_grp'].nunique()}")


if __name__ == "__main__":
    main()
