#!/usr/bin/env python3
"""
diag7.py — 隔离"选股规则"与"退出规则"对策略G表现的贡献

背景:
  事件研究(全部信号): 20日绝对收益 +1.23%
  引擎(按vol_ratio选前20只/日, 死叉退出): 单笔均值 -1.64%, 年化 -28%

候选解释:
  H1 选股规则: 按 vol_ratio 降序只取每日前 20 只, 而"量比越高未来收益越差"
               -> 引擎恰好选中当日最差的 20 只
  H2 退出规则: 死叉退出把持仓期压缩到 11.8 天, 且退出时点系统性地锁定亏损

2x2x2 对照设计:
  选股: vol_ratio 降序 / 随机
  退出: 死叉退出 / 固定持有20日
并单独统计每日前20 / 随机20 / 后20 的信号 r20 分布。
"""
import os
import time
import importlib.util
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(p, n):
    s = importlib.util.spec_from_file_location(n, p)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


ENG = load(os.path.join(ROOT, "scripts", "03_backtest.py"), "eng")
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")
H = 20


def bounds(codes):
    chg = np.flatnonzero(codes[1:] != codes[:-1]) + 1
    return np.concatenate([[0], chg]), np.concatenate([chg, [len(codes)]])


def main():
    t0 = time.time()
    cols = ["thscode", "date", "exchange", "open_price", "close_price", "turnover",
            "can_buy_open", "can_sell_open", "days_since_list", "is_st_now",
            "hist_cross_up", "death_cross", "vol_ratio"]
    p = pd.read_parquet(os.path.join(PROC, "panel.parquet"), columns=cols)
    g = p.groupby("thscode", sort=False)["turnover"]
    p["amt20"] = g.transform(lambda x: x.rolling(20, min_periods=10).mean())
    ix = ENG.PanelIndex.get(p)
    A = ix.A
    codes = A["thscode"]
    n_days = ix.n_days
    day_of_row = ix.day_of_row
    op = A["open_price"].astype(np.float64)
    # 注意: ix 的面板按 (date, thscode) 排序, 不是按股票分块!
    # 必须用 ix.code_rows / ix.code_days 做组内定位。
    print(f"  ready in {time.time()-t0:.0f}s")

    def shift_blk(x, k):
        """分块 shift(k): 组内位置由 ix.code_rows/code_days 决定"""
        out = np.full_like(x, np.nan)
        for c, rows in ix.code_rows.items():
            n = len(rows)
            if k >= 0:
                if n > k:
                    out[rows[k:]] = x[rows[:n - k]]
            else:
                m = -k
                if n > m:
                    out[rows[:n - m]] = x[rows[m:]]
        return out

    # 事件研究口径的 20 日收益 (T+1开盘 -> T+21开盘)
    r20 = shift_blk(op, -(H + 1)) / shift_blk(op, -1) - 1.0
    tradable = ((A["days_since_list"] >= 120) & (~A["is_st_now"])
                & (np.nan_to_num(A["amt20"], nan=0) >= 20e6))
    sig = A["hist_cross_up"] & (A["vol_ratio"] > 1.1) & tradable
    ex = A["death_cross"]
    vr = np.nan_to_num(A["vol_ratio"].astype(np.float64), nan=-np.inf)

    # ================= 1. 信号事件的 r20 分布 =================
    print("\n" + "=" * 100)
    print("1. 信号事件的 20 日收益分布 (按当日 vol_ratio 排名分组)")
    print("=" * 100)
    sr = np.flatnonzero(sig)
    sd = day_of_row[sr]
    sp = vr[sr]
    order = np.lexsort((-sp, sd))
    sr, sd = sr[order], sd[order]
    # 每日名次
    rank = np.zeros(len(sr), dtype=np.int32)
    prev_day, k = -1, 0
    for i, dd in enumerate(sd):
        if dd != prev_day:
            prev_day, k = dd, 0
        rank[i] = k
        k += 1
    nsig_per_day = np.bincount(sd, minlength=n_days)
    ev_r = r20[sr]
    ok = np.isfinite(ev_r)
    print(f"  全部信号事件: n={ok.sum():,}  平均20日收益 {np.nanmean(ev_r)*100:+.3f}%")
    for lo, hi, tag in [(0, 20, "每日前20 (引擎实际选取)"),
                        (20, 100, "第21-100"),
                        (100, 10**9, "第101及以后")]:
        m = ok & (rank >= lo) & (rank < hi)
        if m.sum():
            print(f"  {tag:<24} n={m.sum():>8,}  平均20日收益 {np.nanmean(ev_r[m])*100:+.3f}%")
    # 随机20只
    rng = np.random.default_rng(7)
    acc = []
    for _ in range(20):
        pick = rng.random(len(sr)) < (20.0 / np.maximum(nsig_per_day[sd], 1))
        acc.append(np.nanmean(ev_r[pick & ok]))
    print(f"  {'随机20只 (20次平均)':<24} 平均20日收益 {np.mean(acc)*100:+.3f}%")
    # 后20只
    m = ok & (rank >= np.maximum(nsig_per_day[sd] - 20, 0))
    print(f"  {'每日后20 (最低量比)':<24} n={m.sum():>8,}  平均20日收益 {np.nanmean(ev_r[m])*100:+.3f}%")

    # ================= 2. 2x2x2 对照回测 =================
    print("\n" + "=" * 100)
    print("2. 2x2x2 对照回测 (全A, 2015-2026, 含成本)")
    print("=" * 100)
    no_exit = np.zeros(len(codes), dtype=bool)
    combos = [
        ("VR降序 + 死叉退出", vr, ex, None),
        ("随机   + 死叉退出", None, ex, None),
        ("VR降序 + 固定持有20日", vr, no_exit, 20),
        ("随机   + 固定持有20日", None, no_exit, 20),
    ]
    rows = []
    for tag, prio, exx, mhd in combos:
        cfg = ENG.BacktestConfig(name=tag, max_positions=20, position_pct=0.05,
                                 min_days_listed=120, exclude_st=True,
                                 min_amount=20_000_000.0, max_hold_days=mhd)
        r = ENG.VectorizedBacktester(ix, cfg).run(sig, exx, priority=prio)
        st = ENG.perf_stats(r)
        tr = r["trades"]
        rows.append(dict(tag=tag, cagr=st["cagr"], mdd=st["max_drawdown"],
                         n=st["n_trades"], hold=st["avg_hold_days"],
                         wr=st["win_rate"], avg_ret=tr["ret"].mean(),
                         pf=st["profit_factor"]))
        print(f"  {tag:<22} cagr={st['cagr']*100:+7.2f}%  mdd={st['max_drawdown']*100:6.1f}%  "
              f"n={st['n_trades']:>5}  均持{st['avg_hold_days']:>5.1f}天  "
              f"胜率{st['win_rate']*100:>4.1f}%  单笔均值{tr['ret'].mean()*100:>+6.2f}%  PF={st['profit_factor']:.3f}")
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "diag_selection_exit.csv"), index=False)

    # ================= 3. 零成本等权组合 =================
    print("\n" + "=" * 100)
    print("3. 零成本等权组合 (固定持有20日, 等权) — 直接检验信号本身")
    print("=" * 100)

    def build_port(rows_sel, tag):
        """rows_sel: 选中的信号行号数组"""
        daily_sum = np.zeros(n_days)
        daily_cnt = np.zeros(n_days)
        for row in rows_sel:
            c = codes[row]
            rows = ix.code_rows[c]
            days = ix.code_days[c]
            pos = np.searchsorted(rows, row)
            if pos >= len(rows) or rows[pos] != row:
                continue
            d0 = days[pos]
            for kk in range(1, H + 1):
                t = d0 + kk
                if t >= n_days:
                    break
                q = pos + kk
                if q >= len(rows) or days[q] != t:
                    continue
                pv = op[rows[q - 1]]
                if not np.isfinite(pv) or pv <= 0 or not np.isfinite(op[rows[q]]):
                    continue
                daily_sum[t] += op[rows[q]] / pv - 1.0
                daily_cnt[t] += 1
        pr = np.where(daily_cnt > 0, daily_sum / np.maximum(daily_cnt, 1), 0.0)
        eq = np.cumprod(1.0 + pr)
        yrs = (pd.Timestamp(ix.dates[-1]) - pd.Timestamp(ix.dates[0])).days / 365.25
        peak = np.maximum.accumulate(eq)
        print(f"  {tag:<32} 累计 {eq[-1]-1:+7.1%}  年化 {(eq[-1]**(1/yrs)-1)*100:+7.2f}%  "
              f"最大回撤 {((eq/peak-1).min())*100:6.1f}%  平均持仓数 {daily_cnt.mean():.1f}")
        return eq

    build_port(sr[rank < 20], "每日前20 / 持有20日 / 零成本")
    build_port(sr, "全部信号 / 持有20日 / 零成本")
    # 随机20只
    rng2 = np.random.default_rng(11)
    pick = rng2.random(len(sr)) < (20.0 / np.maximum(np.bincount(sd, minlength=n_days)[sd], 1))
    build_port(sr[pick], "随机20只 / 持有20日 / 零成本")
    # 后20只
    nsig_day = np.bincount(sd, minlength=n_days)[sd]
    build_port(sr[rank >= np.maximum(nsig_day - 20, 0)], "每日后20 / 持有20日 / 零成本")

    print(f"\ndone in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
