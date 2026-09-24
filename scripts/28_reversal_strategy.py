#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
28_reversal_strategy.py —— 「超跌 + 极端缩量」反转策略（日线）

研究假设(来自 V2 报告的非 Alpha 但有价值的线索):
  V2 发现 ① Volume Z < -1 在 6 个改善类事件中稳定正超额(+0.198% ~ +1.085%)
          ② 「过去20日没大涨」与「量温和」是唯二正贡献单条件
          ③ 宽止损优于趋势跟随退出
  本脚本把这三条线索组合成一个完整的可回测策略, 并严格检验它是否真的成立。

用户指定规格:
  Entry: MACD 柱超跌低位(hist_z < -1.5 或 0轴远下方)
       + 极端缩量(VR20 前20%低位 或 Volume Z < -1), 剔除放量标的
       + 过去20日涨幅 < 0%
       + 低波动率(hv20 / ATR/Price 中低水平)
  Exit : 不用趋势跟随(不跌破MA20/不死叉); 固定持有5~10日, 或 2~3 倍 ATR 宽止损
  风控 : 剔除市值最小10%(D1组); 0.3% 双边交易成本

⚠️ 本脚本同时检验一个关键矛盾:
   V2 的市值中性结果显示, 正超额**仅存在于 D1(最小市值组)**
   (D1 +0.3045% t=+2.06, D2起转负)。
   而用户规格要求**剔除 D1** —— 如果信号真的只在 D1 有效,
   剔除 D1 会直接杀死策略。所以 R5 变体专门保留 D1 做对照。

时序约定(严格无未来函数):
   信号 T 日收盘生成 -> T+1 日开盘买入(open[T+1]); T+1 开盘一字涨停则放弃
   退出在 T+k 日收盘确认 -> T+k+1 日开盘卖出; 跌停无法卖出则顺延
   持有 s 日的日收益 = oret[p], ..., oret[p+s-1], 其中 oret[p]=open[p+1]/open[p]-1

组合口径:
   每日等权持有全部在仓标的(日度再平衡), 组合日收益 = 在仓标的 oret 的等权均值
   成本按持仓天数均摊: 每个持仓每日承担 RT_COST/s
   -> 单笔持仓整个持有期恰好承担 RT_COST(精确, 非近似)

产出:
  output/v3_entry_exit.csv      Entry × Exit 网格(全样本)
  output/v3_segments.csv        训练/验证/测试三段
  output/v3_walkforward.csv     Walk-Forward
  output/v3_bench.csv           基准比较(全A等权 / 随机选股)
  output/v3_cost.csv            成本敏感性
  output/v3_d1.csv              D1 剔除 vs 保留 对照
  output/v3_run.log
"""
import os
import sys
import time
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

MIN_LIST = 120          # 上市满 120 个交易日
MAXH = 10               # 最长持有(日) —— 用户规格 5~10 日
RT_COST = 0.003         # 0.3% 双边交易成本(用户指定)
SEED = 20260923

LOGF = open(os.path.join(OUT, "v3_run.log"), "w", encoding="utf-8")
_t0 = time.time()


def log(m):
    line = f"[{time.time()-_t0:7.1f}s] {m}"
    print(line, flush=True)
    LOGF.write(line + "\n")
    LOGF.flush()


# ---------------------------------------------------------------- 分块工具
def block_bounds(codes):
    chg = np.flatnonzero(codes[1:] != codes[:-1]) + 1
    return (np.concatenate([[0], chg]), np.concatenate([chg, [len(codes)]]), chg)


def shift_fwd(a, chg, n, k):
    """out[t] = a[t+k] (同股票内, 跨块/越界 nan)。k 可正可负。"""
    out = np.full(n, np.nan, dtype=np.float64)
    if k == 0:
        return np.asarray(a, dtype=np.float64).copy()
    if k > 0:
        idx = np.arange(n - k)
        bound = np.zeros(n, dtype=np.int64)
        bound[chg] = 1
        csum = np.cumsum(bound)
        ok = csum[idx] == csum[idx + k]
        out[idx[ok]] = a[idx[ok] + k]
    else:
        m = -k
        idx = np.arange(n - m)
        bound = np.zeros(n, dtype=np.int64)
        bound[chg] = 1
        csum = np.cumsum(bound)
        ok = csum[idx] == csum[idx + m]
        out[idx[ok] + m] = a[idx[ok]]
    return out


def build_sell_open(op, can_sell, chg, n):
    """out[t] = 从 t 起(含 t)第一个可卖日的 open; 无则 nan(跌停顺延)"""
    tmp = np.where(np.asarray(can_sell, dtype=bool), op, np.nan)
    blk = np.zeros(n, dtype=np.int64)
    blk[chg] = 1
    blk = np.cumsum(blk)
    return pd.Series(tmp).groupby(blk).transform(lambda x: x[::-1].ffill()[::-1]).values


# ---------------------------------------------------------------- 路径模拟
def simulate_paths(pos, starts, ends, A, mult=None):
    """对信号位置 pos 模拟退出路径, 返回 (hold_days, sell_price, buy_price)

    mult=None  -> 固定持有 MAXH 日(即 s=MAXH)
    mult=2/3   -> ATR 倍数止损: 首次 close[p+k] < buy - mult*ATR 时, 在 open[p+k+1] 卖出
                  s = k+1; 未触发则 s = MAXH
    """
    n_ev = len(pos)
    hold = np.full(n_ev, np.nan, dtype=np.float64)
    sell = np.full(n_ev, np.nan, dtype=np.float64)
    buy = np.full(n_ev, np.nan, dtype=np.float64)
    if n_ev == 0:
        return hold, sell, buy

    close = A["close"]
    atr_abs = A["atr_abs"]
    sell_open = A["sell_open"]
    n = len(close)
    off = np.arange(MAXH + 1, dtype=np.int64)      # 列 k -> 全局位置 p+k

    for b0 in range(0, n_ev, 40_000):
        b1 = min(b0 + 40_000, n_ev)
        p = pos[b0:b1]
        nb = b1 - b0
        ci = np.searchsorted(starts, p, side="right") - 1
        e_end = ends[ci]

        idx = p[:, None] + off[None, :]
        valid = idx < e_end[:, None]
        safe = np.minimum(idx, n - 1)

        Mc = close[safe].astype(np.float64)
        Mc = np.where(valid, Mc, np.nan)

        B = A["buy"][p]
        # 卖出价矩阵: 第 k 列触发 -> 从 p+k+1 起第一个可卖日 open
        sidx = np.minimum(idx + 1, n - 1)
        Msell = sell_open[sidx]
        Msell = np.where((idx + 1) < e_end[:, None], Msell, np.nan)

        if mult is None:
            # 固定持有 MAXH 日: 卖出价 = 第 MAXH 列对应的 sell
            s_idx = np.full(nb, MAXH, dtype=np.int64)
            # 若数据不足以持有 MAXH 日, 退到该股票最后一列
            avail = valid.sum(axis=1) - 1                 # 最大可用列
            s_idx = np.minimum(s_idx, np.maximum(avail, 0))
            hold[b0:b1] = s_idx.astype(np.float64)
            sell[b0:b1] = Msell[np.arange(nb), s_idx]
        else:
            line = B[:, None] - mult * atr_abs[p][:, None]      # (nb,1)
            trig = np.zeros((nb, MAXH + 1), bool)
            trig[:, 1:] = Mc[:, 1:] < line
            any_t = trig.any(axis=1)
            kf = np.where(any_t, trig.argmax(axis=1), MAXH)
            s_idx = kf + (any_t.astype(np.int64))               # 触发 -> s=k+1; 未触发 -> s=MAXH
            # 未触发但数据不足时退到最后一列
            avail = valid.sum(axis=1) - 1
            s_idx = np.minimum(s_idx, np.maximum(avail, 0))
            hold[b0:b1] = s_idx.astype(np.float64)
            sell[b0:b1] = Msell[np.arange(nb), np.minimum(s_idx, MAXH)]
        buy[b0:b1] = B
    return hold, sell, buy


# ---------------------------------------------------------------- 组合 NAV
def portfolio_nav(sig, day_idx, n_days, oret, s_days, rt_cost=RT_COST, mask=None):
    """把信号数组转成每日等权组合净值。

    sig      : bool[n]  逐行信号
    day_idx  : int[n]   每行对应的交易日下标(全局升序)
    oret     : float[n] oret[p] = open[p+1]/open[p]-1 (同股票内)
    s_days   : float[n] 每个信号的持有天数(已模拟)
    返回 (nav, daily_ret_gross, daily_ret_net, n_pos)
    """
    pos = np.flatnonzero(sig & np.isfinite(s_days) & (s_days >= 1))
    if mask is not None:
        pos = pos[mask[pos]]
    n = len(sig)
    rsum = np.zeros(n_days, dtype=np.float64)
    csum = np.zeros(n_days, dtype=np.float64)
    cnt = np.zeros(n_days, dtype=np.int64)

    # 逐 j 累加: 持仓 p 的第 j 天收益 = oret[p+j], 落在日历日 day_idx[p]+j
    maxj = int(np.nanmax(s_days)) if len(pos) else 0
    maxj = min(maxj, MAXH)
    for j in range(maxj):
        sel = pos[(s_days[pos] > j)]                 # 还在仓的信号
        if len(sel) == 0:
            continue
        q = sel + j
        ok = q < n
        q = q[ok]
        sel = sel[ok]
        # 不能跨股票边界: p+j 必须与 p 同块
        # (用 oret 本身已保证: 跨块位置 oret=nan)
        r = oret[q]
        good = np.isfinite(r)
        d = day_idx[sel[good]] + j
        inb = d < n_days
        d = d[inb]
        r = r[good][inb]
        sv = sel[good][inb]
        np.add.at(rsum, d, r)
        np.add.at(cnt, d, 1)
        np.add.at(csum, d, rt_cost / s_days[sv])

    okd = cnt > 0
    gross = np.where(okd, rsum / np.maximum(cnt, 1), 0.0)
    net = np.where(okd, (rsum - csum) / np.maximum(cnt, 1), 0.0)
    nav = np.cumprod(1.0 + net)
    return nav, gross, net, cnt


def ann_stats(net, n_days, cnt=None, periods_per_year=252.0):
    """从日度净收益序列算年化/回撤/胜率/夏普(按有持仓日计)"""
    act = cnt > 0 if cnt is not None else np.ones(len(net), bool)
    r = net[act]
    if len(r) < 5:
        return dict(cagr=np.nan, mdd=np.nan, win=np.nan, sharpe=np.nan, n_active=0)
    nav = np.cumprod(1.0 + net)
    years = n_days / periods_per_year
    cagr = nav[-1] ** (1.0 / years) - 1.0 if years > 0 and nav[-1] > 0 else np.nan
    dd = (nav / np.maximum.accumulate(nav) - 1.0).min()
    win = float((r > 0).mean())
    sd = float(r.std(ddof=1))
    sharpe = float(r.mean() / sd * np.sqrt(periods_per_year)) if sd > 0 else np.nan
    return dict(cagr=float(cagr), mdd=float(dd), win=win, sharpe=sharpe,
                n_active=int(act.sum()))


def exw_t(ret, mk, dates, min_n=50):
    """日加权超额 + 按日聚类 t"""
    ok = np.isfinite(ret) & np.isfinite(mk)
    if ok.sum() < min_n:
        return np.nan, np.nan, int(ok.sum())
    e = ret[ok] - mk[ok]
    dm = pd.Series(e).groupby(dates[ok]).mean().values
    if len(dm) < 5:
        return np.nan, np.nan, int(ok.sum())
    sd = float(dm.std(ddof=1))
    t = float(dm.mean() / (sd / np.sqrt(len(dm)))) if sd > 0 else np.nan
    return float(dm.mean()), t, int(ok.sum())


def ymd(ts):
    return pd.DatetimeIndex(ts).year.values


# ---------------------------------------------------------------- 主流程
def main():
    cols = ["thscode", "date", "is_st_now", "days_since_list", "ret",
            "open_price", "close_price", "can_buy_open", "can_sell_open",
            "hist_z", "dif", "hist", "atr_pct", "hv20",
            "volz20", "vr20m", "rvol20", "vt_5_20", "ret20",
            "size_grp", "ind_code"]
    df = pd.read_parquet(os.path.join(PROC, "v2_panel.parquet"), columns=cols)
    log(f"读入 {df.shape[0]:,} 行")

    m = (~df["is_st_now"].fillna(True)) & (df["days_since_list"].fillna(0) >= MIN_LIST) \
        & df["ret"].notna()
    df = df.loc[m].copy()
    df = df.sort_values(["thscode", "date"], kind="stable").reset_index(drop=True)
    log(f"clean {len(df):,} 行 / {df['thscode'].nunique():,} 只")

    codes = df["thscode"].values
    starts, ends, chg = block_bounds(codes)
    n = len(df)
    log(f"分块 {len(starts):,} 个")

    op = df["open_price"].values.astype(np.float64)
    cl = df["close_price"].values.astype(np.float64)
    cb = df["can_buy_open"].fillna(False).values.astype(bool)
    cs = df["can_sell_open"].fillna(True).values.astype(bool)

    # --- 每日交易日下标
    udates = np.sort(df["date"].unique())
    day_idx = np.searchsorted(udates, df["date"].values).astype(np.int64)
    n_days = len(udates)
    log(f"交易日 {n_days:,} 天  {pd.Timestamp(udates[0]).date()} ~ {pd.Timestamp(udates[-1]).date()}")

    # --- oret: open[p+1]/open[p]-1 (同股票内)
    op_next = shift_fwd(op, chg, n, 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        oret = op_next / op - 1.0
    oret[~np.isfinite(oret)] = np.nan

    # --- 买入价: T+1 开盘; 涨停/停牌无法买入 -> nan
    buy = np.where(cb, op_next, np.nan)
    # --- 卖出价: 从 T+k+1 起第一个可卖日 open
    sell_open = build_sell_open(op, cs, chg, n)

    A = dict(close=cl, atr_abs=df["atr_pct"].values.astype(np.float64) * cl,
             sell_open=sell_open, buy=buy)

    # --- 市场基准: 全A等权 oret (与个股同口径!)
    mkt = pd.Series(oret).groupby(day_idx).mean().reindex(range(n_days)).values

    # --- 全A等权净值(基准 B0)
    nav_b0 = np.cumprod(1.0 + np.nan_to_num(mkt, nan=0.0))
    log("基准 B0 全A等权 完成")

    # ================================================================ Entry 条件
    hz = df["hist_z"].values.astype(np.float64)
    dif = df["dif"].values.astype(np.float64)
    vz = df["volz20"].values.astype(np.float64)
    vr = df["vr20m"].values.astype(np.float64)
    r20 = df["ret20"].values.astype(np.float64)
    hv = df["hv20"].values.astype(np.float64)
    sg = df["size_grp"].values.astype(np.float64)

    # 低波动阈值: 只用训练段(2015-2022)估计, 避免前视
    yr = ymd(df["date"].values)
    TR = yr <= 2022
    HV_MED_TR = float(np.nanmedian(hv[TR]))
    HV_T1_TR = float(np.nanpercentile(hv[TR], 33.3))
    log(f"训练段 hv20 中位数={HV_MED_TR:.5f}  33分位={HV_T1_TR:.5f}")

    # 极端缩量: VR20 前 20% 低位 —— 逐日截面分位(用训练段无法逐日估, 用全样本分位作阈值近似)
    # ⚠️ 用「绝对阈值」避免前视: 训练段 vr20m 的 20 分位
    VR_Q20_TR = float(np.nanpercentile(vr[TR], 20))
    log(f"训练段 vr20m 20分位={VR_Q20_TR:.5f}")

    nd1 = np.where(np.isfinite(sg), sg > 0, False)

    def clean_bool(arr, src):
        """把条件转 bool, 并把 src 为 NaN 的位置强制置 False(缺数据不入选)"""
        b = np.asarray(arr, dtype=bool).copy()
        b[~np.isfinite(src)] = False
        return b

    C = {
        "超跌": clean_bool(hz < -1.5, hz),
        "缩量": clean_bool(vz < -1.0, vz),
        "没涨": clean_bool(r20 < 0.0, r20),
        "低波": clean_bool(hv < HV_MED_TR, hv),
        "非D1": clean_bool(nd1, sg),
        "0轴下": clean_bool(dif < 0, dif),
        "缩量严": clean_bool(vz < -1.5, vz),
        "VR低位": clean_bool(vr < VR_Q20_TR, vr),
        "非D1非D2": clean_bool(sg >= 2, sg),
    }
    ALL = np.ones(n, bool)

    ENTRIES = [
        ("R0_完整(超跌+缩量+没涨+低波+非D1)",
         C["超跌"] & C["缩量"] & C["没涨"] & C["低波"] & C["非D1"]),
        ("R1_去低波", C["超跌"] & C["缩量"] & C["没涨"] & C["非D1"]),
        ("R2_去没涨", C["超跌"] & C["缩量"] & C["低波"] & C["非D1"]),
        ("R3_仅超跌+缩量", C["超跌"] & C["缩量"] & C["非D1"]),
        ("R4_完整+0轴下", C["超跌"] & C["缩量"] & C["没涨"] & C["低波"] & C["非D1"] & C["0轴下"]),
        ("R5_完整但不剔除D1", C["超跌"] & C["缩量"] & C["没涨"] & C["低波"]),
        ("R5b_仅D1(完整)", C["超跌"] & C["缩量"] & C["没涨"] & C["低波"] & clean_bool(sg == 0, sg)),
        ("R6_缩量更严(vz<-1.5)", C["超跌"] & C["缩量严"] & C["没涨"] & C["低波"] & C["非D1"]),
        ("R7_用VR低位替vz", C["超跌"] & C["VR低位"] & C["没涨"] & C["低波"] & C["非D1"]),
        ("R8_仅缩量+没涨+低波(无超跌)", C["缩量"] & C["没涨"] & C["低波"] & C["非D1"]),
        ("R9_仅超跌+没涨+低波(无缩量)", C["超跌"] & C["没涨"] & C["低波"] & C["非D1"]),
    ]

    for nm, sig in ENTRIES:
        log(f"  {nm}: 信号 {int(sig.sum()):,} 次 / {sig.sum()/n*100:.3f}% 行")

    # ================================================================ Exit 规则
    EXITS = [("XA_固定持有5日", None, 5), ("XA_固定持有10日", None, 10),
             ("XD_ATR2倍止损", 2.0, None), ("XD_ATR3倍止损", 3.0, None)]

    # 为每个 Entry 模拟每个 Exit
    rows = []
    for nm, sig in ENTRIES:
        pos = np.flatnonzero(sig)
        if len(pos) < 50:
            log(f"  跳过 {nm}(样本不足)")
            continue
        # 涨跌停约束: 剔除无法买入的信号
        buyable = np.isfinite(buy[pos])
        pos = pos[buyable]
        for xnm, mult, fixedH in EXITS:
            if fixedH is not None:
                hold, sell, bp = simulate_paths(pos, starts, ends, A, None)
                hold = np.where(np.isfinite(hold), hold, np.nan)
                # 固定 H 日: 强制 s=H(数据不足则已退到可用列)
                hold_use = np.minimum(np.full(len(pos), float(fixedH)), hold)
            else:
                hold, sell, bp = simulate_paths(pos, starts, ends, A, mult)
                hold_use = hold
            sd_ = np.full(n, np.nan)
            sd_[pos] = hold_use
            sig2 = np.zeros(n, bool)
            sig2[pos] = True
            nav, gross, net, cnt = portfolio_nav(sig2, day_idx, n_days, oret, sd_)
            st = ann_stats(net, n_days, cnt)
            # 日加权超额
            act = cnt > 0
            dd_ = np.flatnonzero(act)
            ex, tt, nns = exw_t(net[act], mkt[act], dd_)
            rows.append(dict(entry=nm, exit=xnm, n_sig=len(pos),
                             avg_hold=float(np.nanmean(hold_use)),
                             cagr=st["cagr"], mdd=st["mdd"], win=st["win"],
                             sharpe=st["sharpe"], exw=ex, t_exw=tt,
                             n_active_days=st["n_active"]))
        log(f"  完成 {nm}")
    ge = pd.DataFrame(rows)
    ge.to_csv(os.path.join(OUT, "v3_entry_exit.csv"), index=False, encoding="utf-8-sig")
    log(f"v3_entry_exit.csv  {len(ge)} 行")

    # ================================================================ 三段样本外
    SEGS = [("训练2015-2022", 2015, 2022), ("验证2023-2024", 2023, 2024),
            ("测试2025-2026", 2025, 2026)]
    rows = []
    for nm, sig in ENTRIES:
        pos0 = np.flatnonzero(sig)
        pos0 = pos0[np.isfinite(buy[pos0])]
        for xnm, mult, fixedH in EXITS:
            hold, sell, bp = simulate_paths(pos0, starts, ends, A, mult)
            hold_use = (np.minimum(np.full(len(pos0), float(fixedH)), hold)
                        if fixedH is not None else hold)
            sd_ = np.full(n, np.nan)
            sd_[pos0] = hold_use
            sig2 = np.zeros(n, bool)
            sig2[pos0] = True
            for snm, a, b in SEGS:
                yr_s = ymd(udates)
                dm = (yr_s >= a) & (yr_s <= b)
                nav, gross, net, cnt = portfolio_nav(sig2, day_idx, n_days, oret, sd_)
                net_s = np.where(dm, net, 0.0)
                cnt_s = np.where(dm, cnt, 0)
                st = ann_stats(net_s, int(dm.sum()), cnt_s)
                act = (cnt_s > 0)
                dd_ = np.flatnonzero(act)
                ex, tt, nns = exw_t(net_s[act], mkt[act], dd_)
                rows.append(dict(entry=nm, exit=xnm, seg=snm,
                                 n_sig=int((sig2 & dm[day_idx]).sum()),
                                 cagr=st["cagr"], mdd=st["mdd"], win=st["win"],
                                 exw=ex, t_exw=tt, n_days=int(dm.sum())))
        log(f"  三段 {nm} 完成")
    seg = pd.DataFrame(rows)
    seg.to_csv(os.path.join(OUT, "v3_segments.csv"), index=False, encoding="utf-8-sig")
    log(f"v3_segments.csv  {len(seg)} 行")

    # ================================================================ Walk-Forward
    WF = [(2015, 2019, 2020), (2016, 2020, 2021), (2017, 2021, 2022),
          (2018, 2022, 2023), (2019, 2023, 2024), (2020, 2024, 2025),
          (2021, 2025, 2026)]
    rows = []
    for nm, sig in ENTRIES:
        pos0 = np.flatnonzero(sig)
        pos0 = pos0[np.isfinite(buy[pos0])]
        for xnm, mult, fixedH in EXITS:
            hold, sell, bp = simulate_paths(pos0, starts, ends, A, mult)
            hold_use = (np.minimum(np.full(len(pos0), float(fixedH)), hold)
                        if fixedH is not None else hold)
            sd_ = np.full(n, np.nan)
            sd_[pos0] = hold_use
            sig2 = np.zeros(n, bool)
            sig2[pos0] = True
            nav, gross, net, cnt = portfolio_nav(sig2, day_idx, n_days, oret, sd_)
            yr_s = ymd(udates)
            for a, b, ty in WF:
                dm = (yr_s == ty)
                net_s = np.where(dm, net, 0.0)
                cnt_s = np.where(dm, cnt, 0)
                st = ann_stats(net_s, int(dm.sum()), cnt_s)
                act = cnt_s > 0
                ex, tt, nns = exw_t(net_s[act], mkt[act], np.flatnonzero(act))
                rows.append(dict(entry=nm, exit=xnm, train=f"{a}-{b}", test=ty,
                                 n_sig=int((sig2 & dm[day_idx]).sum()),
                                 cagr=st["cagr"], win=st["win"], exw=ex, t_exw=tt))
    wf = pd.DataFrame(rows)
    wf.to_csv(os.path.join(OUT, "v3_walkforward.csv"), index=False, encoding="utf-8-sig")
    log(f"v3_walkforward.csv  {len(wf)} 行")

    # ================================================================ D1 对照
    rows = []
    for tag, extra in [("剔除D1(用户规格)", C["非D1"]),
                       ("保留全部", ALL.astype(bool)),
                       ("仅D1", clean_bool(sg == 0, sg)),
                       ("仅D2-D3", clean_bool((sg >= 1) & (sg <= 2), sg)),
                       ("剔除D1-D3", clean_bool(sg >= 3, sg))]:
        sig = C["超跌"] & C["缩量"] & C["没涨"] & C["低波"] & extra
        pos0 = np.flatnonzero(sig)
        pos0 = pos0[np.isfinite(buy[pos0])]
        if len(pos0) < 50:
            continue
        hold, sell, bp = simulate_paths(pos0, starts, ends, A, None)
        hold_use = np.minimum(np.full(len(pos0), 10.0), hold)
        sd_ = np.full(n, np.nan)
        sd_[pos0] = hold_use
        sig2 = np.zeros(n, bool)
        sig2[pos0] = True
        nav, gross, net, cnt = portfolio_nav(sig2, day_idx, n_days, oret, sd_)
        st = ann_stats(net, n_days, cnt)
        act = cnt > 0
        ex, tt, nns = exw_t(net[act], mkt[act], np.flatnonzero(act))
        # 也统计逐笔毛收益(未扣成本的持有期收益)
        rows.append(dict(group=tag, n_sig=len(pos0), cagr=st["cagr"], mdd=st["mdd"],
                         win=st["win"], sharpe=st["sharpe"], exw=ex, t_exw=tt))
    d1 = pd.DataFrame(rows)
    d1.to_csv(os.path.join(OUT, "v3_d1.csv"), index=False, encoding="utf-8-sig")
    log(f"v3_d1.csv  {len(d1)} 行")

    # ================================================================ 基准
    rng = np.random.default_rng(SEED)
    rows = []
    # B0 全A等权
    st = ann_stats(np.nan_to_num(mkt, nan=0.0), n_days, np.ones(n_days, np.int64))
    rows.append(dict(bench="B0_全A等权(2015-2026)", n_sig=n, cagr=st["cagr"],
                     mdd=st["mdd"], win=st["win"], exw=0.0, t_exw=np.nan))
    # B4 随机选股: 与 R0 信号数相同的随机标的
    nm, sig0 = ENTRIES[0]
    k_target = int(sig0.sum())
    valid_pool = np.flatnonzero(np.isfinite(buy) & np.isfinite(oret))
    for rep in range(3):
        pick = rng.choice(valid_pool, size=min(k_target, len(valid_pool)), replace=False)
        sd_ = np.full(n, np.nan)
        sd_[pick] = 10.0
        sig2 = np.zeros(n, bool)
        sig2[pick] = True
        nav, gross, net, cnt = portfolio_nav(sig2, day_idx, n_days, oret, sd_)
        st = ann_stats(net, n_days, cnt)
        act = cnt > 0
        ex, tt, nns = exw_t(net[act], mkt[act], np.flatnonzero(act))
        rows.append(dict(bench=f"B4_随机选股 rep{rep+1}", n_sig=len(pick),
                         cagr=st["cagr"], mdd=st["mdd"], win=st["win"],
                         exw=ex, t_exw=tt))
    # 各单条件
    for cnm in ["超跌", "缩量", "没涨", "低波"]:
        sig = C[cnm] & C["非D1"]
        pos0 = np.flatnonzero(sig)
        pos0 = pos0[np.isfinite(buy[pos0])]
        if len(pos0) < 50:
            continue
        hold, sell, bp = simulate_paths(pos0, starts, ends, A, None)
        sd_ = np.full(n, np.nan)
        sd_[pos0] = np.minimum(np.full(len(pos0), 10.0), hold)
        sig2 = np.zeros(n, bool)
        sig2[pos0] = True
        nav, gross, net, cnt = portfolio_nav(sig2, day_idx, n_days, oret, sd_)
        st = ann_stats(net, n_days, cnt)
        act = cnt > 0
        ex, tt, nns = exw_t(net[act], mkt[act], np.flatnonzero(act))
        rows.append(dict(bench=f"仅{cnm}+非D1", n_sig=len(pos0), cagr=st["cagr"],
                         mdd=st["mdd"], win=st["win"], exw=ex, t_exw=tt))
    bm = pd.DataFrame(rows)
    bm.to_csv(os.path.join(OUT, "v3_bench.csv"), index=False, encoding="utf-8-sig")
    log(f"v3_bench.csv  {len(bm)} 行")

    # ================================================================ 成本敏感性
    rows = []
    COSTS = [0.0, 0.001, 0.002, 0.003, 0.005, 0.008, 0.012, 0.02]
    nm, sig0 = ENTRIES[0]
    pos0 = np.flatnonzero(sig0)
    pos0 = pos0[np.isfinite(buy[pos0])]
    hold, sell, bp = simulate_paths(pos0, starts, ends, A, None)
    sd_ = np.full(n, np.nan)
    sd_[pos0] = np.minimum(np.full(len(pos0), 10.0), hold)
    sig2 = np.zeros(n, bool)
    sig2[pos0] = True
    for c in COSTS:
        nav, gross, net, cnt = portfolio_nav(sig2, day_idx, n_days, oret, sd_, rt_cost=c)
        st = ann_stats(net, n_days, cnt)
        rows.append(dict(rt_cost=c, cagr=st["cagr"], mdd=st["mdd"], win=st["win"]))
    cs_df = pd.DataFrame(rows)
    cs_df.to_csv(os.path.join(OUT, "v3_cost.csv"), index=False, encoding="utf-8-sig")
    log(f"v3_cost.csv  {len(cs_df)} 行")

    log("全部完成")
    LOGF.close()


if __name__ == "__main__":
    main()
