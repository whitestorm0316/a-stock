#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
24_v2_entry_exit.py — V2 Entry / Exit 完全拆解 + Walk-Forward + 严格样本外

用户第十二节: Entry 与 Exit 必须完全拆开, 不预设 MACD金叉买/死叉卖。
  本脚本对每个 Entry 事件, 独立跑 18 条 Exit 规则, 让数据决定谁更好。

用户第十九节: 严格样本外 —— 训练 2015-2022 / 验证 2023-2024 / 测试 2025-2026
用户第二十节: Walk-Forward 7 个滚动窗口
用户第二十三节: T日收盘生成信号 -> T+1开盘买入; 涨/跌停无法成交需处理

时序约定(严格无未来函数):
  信号 T 日收盘后生成 -> T+1 日开盘买入 (买入价 = open[T+1], 若 T+1 开盘一字涨停则放弃)
  退出条件在 T+k 日收盘确认 -> T+k+1 日开盘卖出
    若 T+k+1 开盘一字跌停则顺延到下一个可卖日
  未触发则 MAXH 日后强制平仓

性能设计(关键): 路径模拟只做一次 —— 先取所有 Entry 的位置并集, 一次性模拟,
  再用 位置->下标 映射给每个 Entry 取子集。避免 9 个 Entry × 3 段重复模拟。

产出:
  output/v2_entry_exit.csv    Entry × Exit 网格(全样本)
  output/v2_walkforward.csv   Walk-Forward 7 窗口
  output/v2_oos.csv           训练/验证/测试三段样本外
  output/v2_entry_year.csv    Entry × 年度
"""
import os
import time
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

MIN_LIST = 120
MAXH = 60
BATCH = 40_000
HZ = [1, 5, 10, 20, 30, 40, 60]
t0 = time.time()


def log(m):
    print(f"[{time.time()-t0:7.1f}s] {m}", flush=True)


# ---------------------------------------------------------------- 分块工具
def block_bounds(codes):
    chg = np.flatnonzero(codes[1:] != codes[:-1]) + 1
    return (np.concatenate([[0], chg]), np.concatenate([chg, [len(codes)]]), chg)


def shift_fwd(a, chg, n, k):
    """out[t] = a[t+k] (同股票内, 越界 nan) —— 用全局索引向量化"""
    out = np.full(n, np.nan, dtype=np.float64)
    idx = np.arange(n - k)
    ok = np.ones(n - k, dtype=bool)
    # 同一股票: 位置 t 与 t+k 之间不能跨越股票边界
    if k > 0:
        bound = np.zeros(n, dtype=np.int64)
        bound[chg] = 1
        csum = np.cumsum(bound)
        ok = csum[idx] == csum[idx + k]
    out[idx[ok]] = a[idx[ok] + k]
    return out


def build_sell_open(op, can_sell, chg, n):
    """out[t] = 从 t 起(含 t)第一个可卖日的 open; 无则 nan
    分块内反向 ffill —— 处理跌停无法卖出(顺延)"""
    tmp = np.where(np.asarray(can_sell, dtype=bool), op, np.nan)
    s = pd.Series(tmp)
    # 分块 id
    blk = np.zeros(n, dtype=np.int64)
    blk[chg] = 1
    blk = np.cumsum(blk)
    out = s.groupby(blk).transform(lambda x: x[::-1].ffill()[::-1])
    return out.values


# ---------------------------------------------------------------- Exit 规则表
RULES = [
    ("XA_固定持有5日", "fixed", 5),
    ("XA_固定持有10日", "fixed", 10),
    ("XA_固定持有20日", "fixed", 20),
    ("XA_固定持有30日", "fixed", 30),
    ("XA_固定持有40日", "fixed", 40),
    ("XA_固定持有60日", "fixed", 60),
    ("XB_跌破MA20", "ma20", None),
    ("XC_跌破MA60", "ma60", None),
    ("XD_ATR2倍止损", "atr", 2.0),
    ("XD_ATR3倍止损", "atr", 3.0),
    ("XE_固定止损-5%", "stop", 0.05),
    ("XE_固定止损-8%", "stop", 0.08),
    ("XE_固定止损-10%", "stop", 0.10),
    ("XE_固定止损-15%", "stop", 0.15),
    ("XF_移动止盈8%", "trail", 0.08),
    ("XF_移动止盈12%", "trail", 0.12),
    ("XG_MACD死叉", "dead", None),
    ("XH_hist连3日降", "hist3", None),
]


def simulate(entry_pos, starts, ends, A):
    """对 entry_pos(全局位置) 模拟全部路径型 Exit, 返回 {rule: (ret, hold)}"""
    keys = [r[0] for r in RULES if r[1] != "fixed"]
    n_ev = len(entry_pos)
    res = {k: (np.full(n_ev, np.nan), np.full(n_ev, np.nan)) for k in keys}
    if n_ev == 0:
        return res

    close = A["close"]; pma20 = A["px_ma20"]; pma60 = A["px_ma60"]
    atrp = A["atr_pct"]; dcross = A["death_cross"]; hist = A["hist"]
    sell_open = A["sell_open"]
    n = len(close)
    off = np.arange(MAXH + 1, dtype=np.int64)   # 列 k -> 全局位置 p+k (k=0 为 T+1 日)

    for b0 in range(0, n_ev, BATCH):
        b1 = min(b0 + BATCH, n_ev)
        p = entry_pos[b0:b1]
        nb = b1 - b0
        ci = np.searchsorted(starts, p, side="right") - 1
        e_end = ends[ci]
        B = A["buy"][p]                          # 买入价 = open[T+1]
        atr_abs = atrp[p] * close[p]
        idx = p[:, None] + off[None, :]
        valid = idx < e_end[:, None]
        safe = np.minimum(idx, n - 1)

        def g(a):
            m = a[safe].astype(np.float64)
            m = np.where(valid, m, np.nan)
            m[~np.isfinite(m)] = np.nan
            return m

        Mc = g(close); Mp20 = g(pma20); Mp60 = g(pma60)
        Mdead = g(dcross); Mhist = g(hist)
        # 卖出价矩阵: 第 k 列触发 -> 从位置 p+k+1 起第一个可卖日的 open
        sidx = np.minimum(idx + 1, n - 1)
        Msell = sell_open[sidx]
        Msell = np.where((idx + 1) < e_end[:, None], Msell, np.nan)

        def do(key, trig):
            ret, hold = res[key]
            any_t = trig.any(axis=1)
            kf = np.where(any_t, trig.argmax(axis=1), MAXH)
            sell = Msell[np.arange(nb), kf]
            r = sell / B - 1.0
            r[~np.isfinite(sell) | ~np.isfinite(B) | (B <= 0)] = np.nan
            ret[b0:b1] = r
            hold[b0:b1] = kf.astype(np.float64)

        # B: 跌破 MA20 (k>=1)
        trig = np.zeros((nb, MAXH + 1), bool); trig[:, 1:] = Mp20[:, 1:] < 0
        do("XB_跌破MA20", trig)
        # C: 跌破 MA60
        trig = np.zeros((nb, MAXH + 1), bool); trig[:, 1:] = Mp60[:, 1:] < 0
        do("XC_跌破MA60", trig)
        # D: ATR 止损 (line 沿 k 不变, 直接广播到 (nb, MAXH))
        for mult in (2.0, 3.0):
            line = B[:, None] - mult * atr_abs[:, None]     # (nb,1)
            trig = np.zeros((nb, MAXH + 1), bool)
            trig[:, 1:] = Mc[:, 1:] < line
            do(f"XD_ATR{int(mult)}倍止损", trig)
        # E: 固定止损
        for s in (0.05, 0.08, 0.10, 0.15):
            line = B[:, None] * (1.0 - s)                    # (nb,1)
            trig = np.zeros((nb, MAXH + 1), bool)
            trig[:, 1:] = Mc[:, 1:] < line
            do(f"XE_固定止损-{int(s*100)}%", trig)
        # F: 移动止盈
        runmax = np.fmax.accumulate(Mc, axis=1)
        for x in (0.08, 0.12):
            trig = np.zeros((nb, MAXH + 1), bool)
            trig[:, 1:] = Mc[:, 1:] < runmax[:, 1:] * (1.0 - x)
            do(f"XF_移动止盈{int(x*100)}%", trig)
        # G: MACD 死叉
        trig = np.zeros((nb, MAXH + 1), bool); trig[:, 1:] = Mdead[:, 1:] == 1
        do("XG_MACD死叉", trig)
        # H: hist 连续 3 日下降
        d = np.diff(Mhist, axis=1)
        dn = d < 0
        trig = np.zeros((nb, MAXH + 1), bool)
        if MAXH >= 3:
            tri = dn[:, :-2] & dn[:, 1:-1] & dn[:, 2:]
            trig[:, 3:] = tri
        do("XH_hist连3日降", trig)
    return res


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


def main():
    cols = ["thscode", "date", "is_st_now", "days_since_list", "ret",
            "open_price", "close_price", "can_buy_open", "can_sell_open",
            "px_ma20_pct", "px_ma60_pct", "atr_pct",
            "death_cross", "golden_cross", "gc_below0", "gc_above0",
            "hist", "hist_up3", "hist_up5", "st_slowimp",
            "vr20m", "vt_5_20", "rs_mkt20", "ret20", "px_h60", "stD10"]
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
    cb = df["can_buy_open"].fillna(False).values.astype(bool)
    cs = df["can_sell_open"].fillna(True).values.astype(bool)

    # 买入价 = open[T+1]; 且 T+1 开盘必须可买(非一字涨停)
    buy = shift_fwd(op, chg, n, 1)
    buy_ok = shift_fwd(cb.astype(np.float64), chg, n, 1) == 1.0
    # 卖出: 从某位置起第一个可卖日开盘价
    sell_open = build_sell_open(op, cs, chg, n)
    log("买卖价(含涨跌停约束) 完成")

    # 自算未来收益(固定持有期用, 与买入价同口径)
    for H in HZ:
        num = shift_fwd(op, chg, n, H + 1)
        df[f"fwd{H}"] = num / buy - 1.0
    df["_buy_ok"] = buy_ok
    log("未来收益(自算) 完成")

    A = {
        "close": df["close_price"].values.astype(np.float64),
        "px_ma20": df["px_ma20_pct"].values.astype(np.float64),
        "px_ma60": df["px_ma60_pct"].values.astype(np.float64),
        "atr_pct": df["atr_pct"].values.astype(np.float64),
        "death_cross": df["death_cross"].values.astype(np.float64),
        "hist": df["hist"].values.astype(np.float64),
        "sell_open": sell_open,
        "buy": buy,
    }

    d = df["date"].values
    yr = pd.to_datetime(d).year.values
    df["_yr"] = yr

    # ---------------- Entry 事件
    gc = df["golden_cross"].values == 1
    gcb = df["gc_below0"].values == 1
    gca = df["gc_above0"].values == 1
    h3 = df["hist_up3"].values == 1
    h5 = df["hist_up5"].values == 1
    r20 = df["ret20"].values
    pma20 = df["px_ma20_pct"].values
    vt = df["vt_5_20"].values
    rsm = df["rs_mkt20"].values
    pxh60 = df["px_h60"].values
    stD10 = np.nan_to_num(df["stD10"].values, nan=0.0)

    ENT = {
        "EN1_MACD金叉": gc,
        "EN2_0轴下金叉": gcb,
        "EN3_0轴上金叉": gca,
        "EN4_柱连3日改善": h3,
        "EN5_柱连5日改善": h5,
        "EN6_金叉+不过热+量温和+RS正": gc & (r20 < 0.10) & (np.abs(pma20) < 0.05)
                                      & (vt < 1.2) & (rsm > 0),
        "EN7_0轴下金叉+距60日高点<-15%": gcb & (pxh60 < -0.15),
        "EN8_金叉+放量跌为主": gc & (stD10 >= 5),
        "EN9_慢改善未翻红": df["st_slowimp"].values == 1,
    }
    for k, v in ENT.items():
        log(f"  {k}: {int(np.nansum(v & buy_ok)):,} (可买入)")

    # 市场基准(同口径)
    mk = {}
    for H in HZ:
        mk[H] = pd.Series(df[f"fwd{H}"].values).groupby(d).transform("mean").values

    # ---------------- 一次性模拟全部路径型 Exit
    all_em = np.zeros(n, bool)
    for em in ENT.values():
        all_em |= em
    allpos = np.flatnonzero(all_em & buy_ok)
    log(f"Entry 并集 {len(allpos):,} 个可买入信号 -> 开始路径模拟")
    sim = simulate(allpos, starts, ends, A)
    log("路径模拟完成")

    pos2i = np.full(n, -1, dtype=np.int64)
    pos2i[allpos] = np.arange(len(allpos))

    def get_ret(rule, kind, par, ep):
        """返回该 Entry 位置上的 Exit 收益"""
        if kind == "fixed":
            return df[f"fwd{par}"].values[ep], np.full(len(ep), float(par))
        ii = pos2i[ep]
        ok = ii >= 0
        r = np.full(len(ep), np.nan)
        h = np.full(len(ep), np.nan)
        r[ok] = sim[rule][0][ii[ok]]
        h[ok] = sim[rule][1][ii[ok]]
        return r, h

    def get_mk(rule, kind, par, ep):
        return (mk[par] if kind == "fixed" else mk[20])[ep]

    # ---------------- 全样本 Entry × Exit
    log("全样本 Entry × Exit 网格 ...")
    rows = []
    for en, em in ENT.items():
        ep = np.flatnonzero(em & buy_ok)
        for rule, kind, par in RULES:
            r, h = get_ret(rule, kind, par, ep)
            ok = np.isfinite(r)
            if ok.sum() < 200:
                continue
            rr = r[ok]
            e = rr - get_mk(rule, kind, par, ep)[ok]
            ew, tt, nn = exw_t(r, get_mk(rule, kind, par, ep), d[ep])
            rows.append({
                "entry": en, "exit": rule, "n": int(ok.sum()),
                "mean": float(rr.mean()), "median": float(np.median(rr)),
                "win": float((rr > 0).mean()),
                "p10": float(np.percentile(rr, 10)), "p25": float(np.percentile(rr, 25)),
                "p50": float(np.percentile(rr, 50)), "p75": float(np.percentile(rr, 75)),
                "p90": float(np.percentile(rr, 90)),
                "ex": float(e[np.isfinite(e)].mean()) if np.isfinite(e).sum() else np.nan,
                "exw": ew, "t": tt, "hold_mean": float(h[ok].mean()),
            })
        log(f"  {en} 完成")
    edf = pd.DataFrame(rows)
    edf.to_csv(os.path.join(OUT, "v2_entry_exit.csv"), index=False)
    log(f"-> v2_entry_exit.csv ({len(edf)} 行)")

    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    for en in ENT:
        sub = edf[edf.entry == en]
        if sub.empty:
            continue
        print("\n" + "=" * 118)
        print(f"【{en}】不同 Exit 对比 (按日加权超额 exw 排序)")
        print("=" * 118)
        print(sub.sort_values("exw", ascending=False)[
            ["exit", "n", "mean", "median", "win", "exw", "t", "hold_mean"]
        ].to_string(index=False, float_format=lambda x: f"{x:.5f}"))

    # ---------------- Walk-Forward
    log("Walk-Forward ...")
    WF = [(2015, 2019, 2020), (2016, 2020, 2021), (2017, 2021, 2022),
          (2018, 2022, 2023), (2019, 2023, 2024), (2020, 2024, 2025),
          (2021, 2025, 2026)]
    wf_rows = []
    for en, em in ENT.items():
        ep = np.flatnonzero(em & buy_ok)
        cache = {}
        for rule, kind, par in RULES:
            cache[rule] = (get_ret(rule, kind, par, ep), get_mk(rule, kind, par, ep))
        for a, b, c in WF:
            tr = (yr[ep] >= a) & (yr[ep] <= b)
            te = yr[ep] == c
            if tr.sum() < 300 or te.sum() < 50:
                continue
            best, best_v = None, -9e9
            for rule, kind, par in RULES:
                (r, _), mm = cache[rule]
                ew, _, nn = exw_t(r[tr], mm[tr], d[ep][tr], min_n=200)
                if np.isfinite(ew) and ew > best_v:
                    best_v, best = ew, rule
            if best is None:
                continue
            (r, _), mm = cache[best]
            ew, tt, nn = exw_t(r[te], mm[te], d[ep][te], min_n=30)
            ok = np.isfinite(r[te]) & np.isfinite(mm[te])
            if ok.sum() < 30:
                continue
            wf_rows.append({
                "entry": en, "train": f"{a}-{b}", "test": c, "best_exit": best,
                "train_exw": best_v, "test_n": int(ok.sum()),
                "test_mean": float(r[te][ok].mean()),
                "test_win": float((r[te][ok] > 0).mean()),
                "test_exw": ew, "test_t": tt,
            })
    wdf = pd.DataFrame(wf_rows)
    wdf.to_csv(os.path.join(OUT, "v2_walkforward.csv"), index=False)
    log(f"-> v2_walkforward.csv ({len(wdf)} 行)")
    if not wdf.empty:
        print("\n" + "=" * 118)
        print("【Walk-Forward】训练段最优 Exit 在测试年表现 (test_exw>0 且 test_t>1 才算通过)")
        print("=" * 118)
        print(wdf.to_string(index=False, float_format=lambda x: f"{x:.5f}"))
        okwf = wdf[(wdf.test_exw > 0) & (wdf.test_t > 1.0)]
        print(f"\n  通过窗口数: {len(okwf)} / {len(wdf)}")
        if not okwf.empty:
            print(okwf.to_string(index=False, float_format=lambda x: f"{x:.5f}"))

    # ---------------- 严格样本外
    log("样本外三段 ...")
    SEG = [("训练2015-2022", 2015, 2022), ("验证2023-2024", 2023, 2024),
           ("测试2025-2026", 2025, 2026)]
    oos_rows = []
    for en, em in ENT.items():
        ep = np.flatnonzero(em & buy_ok)
        for rule, kind, par in RULES:
            r, _ = get_ret(rule, kind, par, ep)
            mm = get_mk(rule, kind, par, ep)
            for sname, a, b in SEG:
                sel = (yr[ep] >= a) & (yr[ep] <= b)
                ok = np.isfinite(r) & np.isfinite(mm) & sel
                if ok.sum() < 50:
                    continue
                ew, tt, nn = exw_t(r, mm, d[ep], min_n=0)
                # 重新按段算
                ew, tt, nn = exw_t(np.where(sel, r, np.nan), np.where(sel, mm, np.nan),
                                   d[ep], min_n=50)
                oos_rows.append({
                    "entry": en, "exit": rule, "seg": sname, "n": int(ok.sum()),
                    "mean": float(r[ok].mean()), "win": float((r[ok] > 0).mean()),
                    "exw": ew, "t": tt,
                })
        log(f"  {en} 样本外完成")
    odf = pd.DataFrame(oos_rows)
    odf.to_csv(os.path.join(OUT, "v2_oos.csv"), index=False)
    log(f"-> v2_oos.csv ({len(odf)} 行)")

    if not odf.empty:
        print("\n" + "=" * 118)
        print("【样本外】每 Entry 训练段(2015-2022)最优 Exit -> 验证段 / 测试段")
        print("=" * 118)
        pick = []
        for en in ENT:
            tr = odf[(odf.entry == en) & (odf.seg == "训练2015-2022")]
            if tr.empty:
                continue
            best = tr.sort_values("exw", ascending=False).iloc[0]
            pick.append({"entry": en, "best_exit": best["exit"], "train_exw": best["exw"]})
        pk = pd.DataFrame(pick)
        if not pk.empty:
            mg = pk.merge(odf, left_on=["entry", "best_exit"],
                          right_on=["entry", "exit"], how="left")
            print(mg[["entry", "best_exit", "seg", "n", "mean", "win", "exw", "t"]]
                  .to_string(index=False, float_format=lambda x: f"{x:.5f}"))

    # ---------------- Entry × 年度
    log("Entry × 年度 ...")
    yrows = []
    for en, em in ENT.items():
        ep = np.flatnonzero(em & buy_ok)
        r = df["fwd20"].values[ep]
        ex = r - mk[20][ep]
        for y in range(2015, 2027):
            sel = yr[ep] == y
            ok = np.isfinite(ex) & sel
            if ok.sum() < 30:
                continue
            dm = pd.Series(ex[ok]).groupby(d[ep][ok]).mean().values
            sd = float(dm.std(ddof=1)) if len(dm) > 5 else np.nan
            yrows.append({"entry": en, "year": y, "n": int(ok.sum()),
                          "mean20": float(r[ok].mean()),
                          "win": float((r[ok] > 0).mean()),
                          "exw20": float(dm.mean()) if len(dm) > 0 else np.nan,
                          "t": float(dm.mean() / (sd / np.sqrt(len(dm)))) if (sd and sd > 0) else np.nan})
    ydf = pd.DataFrame(yrows)
    ydf.to_csv(os.path.join(OUT, "v2_entry_year.csv"), index=False)
    log(f"-> v2_entry_year.csv ({len(ydf)} 行)")

    log("done")


if __name__ == "__main__":
    main()
