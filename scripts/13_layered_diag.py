#!/usr/bin/env python3
"""
13_layered_diag.py — 解释 "沪深300池正收益 vs 全A池大幅负收益" 的 40pp 缺口

四个诊断:

A. 每日真随机对照 (隔离"选股规则"的贡献)
   之前 2x2x2 用的 priority=None 是"按股票固定随机数", 会造成跨期固定偏好,
   不是每日独立随机。这里用 (行号, 交易日) 混合散列实现真正的每日随机。

B. 基准年化: 几何 vs 算术
   全A等权 20 日轮动的"算术年化"(mean×12.6) 会显著高于"几何复利",
   而组合回测是几何复利的。必须用同一口径对比。

C. 按流动性分层的信号 20 日收益
   全A 池含大量小市值/低流动性股票, 检验信号在不同层级是否表现不同。

D. 相同引擎口径下的"全市场等权"与"沪深300等权"基准。
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
RAW = os.path.join(ROOT, "data", "raw")
import json


def main():
    t0 = time.time()
    cols = ["thscode", "date", "exchange", "open_price", "close_price", "turnover",
            "can_buy_open", "can_sell_open", "days_since_list", "is_st_now",
            "hist_cross_up", "death_cross", "vol_ratio"]
    p = pd.read_parquet(os.path.join(PROC, "panel.parquet"), columns=cols)
    g = p.groupby("thscode", sort=False)["turnover"]
    p["amt20"] = g.transform(lambda x: x.rolling(20, min_periods=10).mean())
    p = p.sort_values(["date", "thscode"], kind="stable").reset_index(drop=True)
    ix = ENG.PanelIndex.get(p)
    A = ix.A
    n = len(p)
    n_days = ix.n_days
    day_of_row = ix.day_of_row.astype(np.int64)
    op = A["open_price"].astype(np.float64)
    cl = A["close_price"].astype(np.float64)
    amt20 = np.nan_to_num(A["amt20"], nan=0.0)
    print(f"  ready in {time.time()-t0:.0f}s")

    sig = A["hist_cross_up"] & (A["vol_ratio"] > 1.1)
    ex = A["death_cross"]
    vr = np.nan_to_num(A["vol_ratio"].astype(np.float64), nan=-np.inf)
    tradable = ((A["days_since_list"] >= 120) & (~A["is_st_now"])
                & np.isfinite(cl) & (amt20 >= 20e6))

    # ================================================================
    # A. 每日真随机对照
    # ================================================================
    print("\n" + "=" * 100)
    print("A. 每日真随机 vs 量比优先 (策略G信号, 全A, 2015-2026, 含成本)")
    print("=" * 100)
    row_idx = np.arange(n, dtype=np.int64)
    # 每日真随机优先级: 用 (行号, 交易日) 的整数散列, 保证同日独立、跨日无固定偏好
    C1 = np.int64(2654435761)      # Knuth 乘数 (mod 2^32 内)
    C2 = np.int64(40503)
    x = (row_idx * C1) ^ (day_of_row * C2)
    x = x * C1
    x ^= (x >> np.int64(15))
    prio_daily = (x & np.int64(0x7FFFFFFF)).astype(np.float64)

    no_exit = np.zeros(n, dtype=bool)
    tests = [
        ("量比降序 + 死叉退出", vr, ex, None),
        ("每日真随机 + 死叉退出", prio_daily, ex, None),
        ("量比降序 + 固定持有20日", vr, no_exit, 20),
        ("每日真随机 + 固定持有20日", prio_daily, no_exit, 20),
        ("量比升序 + 固定持有20日", -vr, no_exit, 20),
    ]
    rowsA = []
    for tag, prio, exx, mhd in tests:
        cfg = ENG.BacktestConfig(name=tag, max_positions=20, position_pct=0.05,
                                 min_days_listed=120, exclude_st=True,
                                 min_amount=20_000_000.0, max_hold_days=mhd)
        r = ENG.VectorizedBacktester(ix, cfg).run(sig, exx, priority=prio)
        st = ENG.perf_stats(r)
        tr = r["trades"]
        rowsA.append(dict(tag=tag, cagr=st["cagr"], mdd=st["max_drawdown"],
                          n=st["n_trades"], hold=st["avg_hold_days"],
                          wr=st["win_rate"], avg_ret=tr["ret"].mean(),
                          pf=st["profit_factor"], fee=st["total_fee"]))
        print(f"  {tag:<26} cagr={st['cagr']*100:+7.2f}%  mdd={st['max_drawdown']*100:6.1f}%  "
              f"n={st['n_trades']:>5}  均持{st['avg_hold_days']:>5.1f}天  "
              f"胜率{st['win_rate']*100:>4.1f}%  单笔{tr['ret'].mean()*100:>+6.2f}%  "
              f"费用{st['total_fee']/1e4:>5.1f}万")
    pd.DataFrame(rowsA).to_csv(os.path.join(OUT, "diag_daily_random.csv"), index=False)

    # ================================================================
    # B. 几何 vs 算术 基准年化
    # ================================================================
    print("\n" + "=" * 100)
    print("B. 基准年化: 几何复利 vs 算术平均 (全A可交易等权, o2o)")
    print("=" * 100)
    geo_rows = []
    for freq in (1, 5, 10, 20):
        per = 252.0 / freq
        rets = []
        for c, rws in ix.code_rows.items():
            for i in range(0, len(rws) - freq, freq):
                r0, r1 = rws[i], rws[i + freq]
                if not tradable[r0] or not tradable[r1]:
                    continue
                if op[r0] > 0 and np.isfinite(op[r0]) and np.isfinite(op[r1]):
                    rets.append(op[r1] / op[r0] - 1)
        rets = np.array(rets)
        mean_r = rets.mean()
        std_r = rets.std()
        arith = mean_r * per
        net_arith = (1 + mean_r - 0.0054) ** per - 1
        geo = np.expm1(np.log1p(np.clip(rets, -0.999, None)).mean() * per)
        geo_rows.append(dict(freq=freq, arith=float(arith), geo=float(geo),
                             net=float(net_arith), mean=float(mean_r),
                             std=float(std_r),
                             drag=float((std_r ** 2 / 2) * per)))
        print(f"  {freq:>2}日轮动: 算术年化 {arith*100:+7.2f}%  几何年化 {geo*100:+7.2f}%  "
              f"扣成本(算术) {net_arith*100:+7.2f}%  单期 mean={mean_r*100:+.3f}% "
              f"std={std_r*100:.1f}%  波动拖累≈{(std_r**2/2)*per*100:.2f}pp/年")

    # ================================================================
    # C. 按流动性分层的信号 20 日收益
    # ================================================================
    print("\n" + "=" * 100)
    print("C. 按 20 日成交额分层的信号 20 日收益 (T+1开盘→T+21开盘)")
    print("=" * 100)

    def shift_by_code(x, k):
        out = np.full_like(x, np.nan)
        for c, rws in ix.code_rows.items():
            m = len(rws)
            if k >= 0:
                if m > k:
                    out[rws[k:]] = x[rws[:m - k]]
            else:
                q = -k
                if m > q:
                    out[rws[:m - q]] = x[rws[q:]]
        return out

    r20 = shift_by_code(op, -21) / shift_by_code(op, -1) - 1.0
    # 每日按 amt20 分层
    adf = pd.DataFrame({"date": A["date"], "amt": amt20})
    adf["rk"] = adf.groupby("date")["amt"].rank(ascending=False, method="first")
    adf["cnt"] = adf.groupby("date")["amt"].transform("size")
    adf["q"] = adf["rk"] / adf["cnt"]
    q = adf["q"].values

    smask = sig & tradable
    base_ok = tradable & np.isfinite(r20)
    print(f"  {'流动性分位':<16} {'信号n':>9} {'信号20日':>10} {'基准20日':>10} {'超额':>9}")
    for lo, hi, tag in [(0, .1, "前10% (最大)"), (.1, .3, "10-30%"),
                        (.3, .5, "30-50%"), (.5, .7, "50-70%"),
                        (.7, .9, "70-90%"), (.9, 1.0, "后10% (最小)")]:
        mq = (q >= lo) & (q < hi)
        ms = smask & mq & np.isfinite(r20)
        mb = base_ok & mq
        if ms.sum() and mb.sum():
            a_, b_ = np.nanmean(r20[ms]), np.nanmean(r20[mb])
            print(f"  {tag:<16} {ms.sum():>9,} {a_*100:>9.3f}% {b_*100:>9.3f}% "
                  f"{(a_-b_)*100:>+8.3f}%")

    # 沪深300 池
    codes_hs = set(json.load(open(os.path.join(RAW, "idx_hs300.json"))))
    hs = p["thscode"].isin(codes_hs).values
    ms = smask & hs & np.isfinite(r20)
    mb = base_ok & hs
    print(f"\n  沪深300池(当前成分, 有幸存者偏差): 信号20日 {np.nanmean(r20[ms])*100:+.3f}% "
          f"(n={ms.sum():,})  池内基准 {np.nanmean(r20[mb])*100:+.3f}%  "
          f"超额 {(np.nanmean(r20[ms])-np.nanmean(r20[mb]))*100:+.3f}%")

    # ================================================================
    # D. 相同引擎口径的基准
    # ================================================================
    print("\n" + "=" * 100)
    print("D. 相同引擎口径基准: 每日随机选20只, 固定持有20日 (真随机)")
    print("=" * 100)
    cfg = ENG.BacktestConfig(name="rand_bench", max_positions=20, position_pct=0.05,
                             min_days_listed=120, exclude_st=True,
                             min_amount=20_000_000.0, max_hold_days=20)
    r = ENG.VectorizedBacktester(ix, cfg).run(tradable, no_exit, priority=prio_daily)
    st = ENG.perf_stats(r)
    print(f"  每日真随机20只(全A可交易) + 固定持有20日: cagr={st['cagr']*100:+.2f}%  "
          f"mdd={st['max_drawdown']*100:.1f}%  n={st['n_trades']}  "
          f"均持{st['avg_hold_days']:.1f}天  胜率{st['win_rate']*100:.1f}%  "
          f"费用{st['total_fee']/1e4:.1f}万")
    print("  -> 这才是与策略G同口径的'无技巧基准'")

    # ---------------- 落盘 ----------------
    layer_rows = []
    for lo, hi, tag in [(0, .1, "前10% (最大)"), (.1, .3, "10-30%"),
                        (.3, .5, "30-50%"), (.5, .7, "50-70%"),
                        (.7, .9, "70-90%"), (.9, 1.0, "后10% (最小)")]:
        mq = (q >= lo) & (q < hi)
        ms = smask & mq & np.isfinite(r20)
        mb = base_ok & mq
        if ms.sum() and mb.sum():
            layer_rows.append(dict(tag=tag, n=int(ms.sum()),
                                   sig=float(np.nanmean(r20[ms])),
                                   base=float(np.nanmean(r20[mb])),
                                   ex=float(np.nanmean(r20[ms]) - np.nanmean(r20[mb]))))
    extra = {
        "layer_table": layer_rows,
        "hs300_signal": {
            "sig": float(np.nanmean(r20[ms_hs])) if (ms_hs := (smask & hs & np.isfinite(r20))).sum() else None,
            "base": float(np.nanmean(r20[base_ok & hs])),
            "n": int(ms_hs.sum()),
            "note": "沪深300当前成分股, 含幸存者偏差",
        },
        "random_bench": {"cagr": float(st["cagr"]), "mdd": float(st["max_drawdown"]),
                         "n": int(st["n_trades"]), "hold": float(st["avg_hold_days"]),
                         "wr": float(st["win_rate"]), "fee": float(st["total_fee"]),
                         "note": "每日真随机20只(全A可交易)+固定持有20日, 含成本"},
        "geo_arith": geo_rows,
    }
    with open(os.path.join(OUT, "chart_extra.json"), "w") as f:
        json.dump(extra, f, ensure_ascii=False, indent=1)
    print(f"  saved chart_extra.json")

    print(f"\ndone in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
