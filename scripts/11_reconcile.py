#!/usr/bin/env python3
"""
11_reconcile.py — 组合回测 vs 事件研究的对账 (决定最终结论措辞)

核心矛盾:
  事件研究: "MACD翻红+VR>1.1" 的 20 日绝对收益 +1.23%, 与全市场基准 +1.23% 相同 (超额≈0)
  组合回测: 同一信号, 全A池, 12 年年化 -17.9%

两者不可能同时为真。本脚本逐项拆解缺口来源:
  A. 成本敏感性: 把成本乘数设为 0 / 0.25 / 0.5 / 1 / 2, 看成本贡献多少
  B. 换手与费用诊断: 年化换手、总费用/初始资金、毛利 vs 净利
  C. 单笔交易收益分布 vs 事件研究收益
  D. 绕过引擎, 用事件研究口径直接构造等权组合 (无成本), 看是否复现 -18%
  E. 基准绝对年化 (含/不含成本)
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


def bounds(codes):
    chg = np.flatnonzero(codes[1:] != codes[:-1]) + 1
    return np.concatenate([[0], chg]), np.concatenate([chg, [len(codes)]])


def main():
    t0 = time.time()
    cols = ["thscode", "date", "exchange", "open_price", "close_price", "turnover",
            "can_buy_open", "can_sell_open", "days_since_list", "is_st_now",
            "hist_cross_up", "death_cross", "vol_ratio", "ret"]
    p = pd.read_parquet(os.path.join(PROC, "panel.parquet"), columns=cols)
    g = p.groupby("thscode", sort=False)["turnover"]
    p["amt20"] = g.transform(lambda x: x.rolling(20, min_periods=10).mean())
    ix = ENG.PanelIndex.get(p)
    A = ix.A
    dates = ix.dates
    n_days = ix.n_days
    print(f"  loaded + index in {time.time()-t0:.0f}s")

    sig = A["hist_cross_up"] & (A["vol_ratio"] > 1.1)
    ex = A["death_cross"]
    vr = A["vol_ratio"].astype(np.float64)
    op = A["open_price"].astype(np.float64)

    # ================= A. 成本敏感性 =================
    print("\n" + "=" * 100)
    print("A. 成本敏感性 (策略G, 全A, 2015-2026) — 成本乘数作用于佣金/印花税/过户费/滑点")
    print("=" * 100)
    orig = (ENG.COMMISSION_RATE, ENG.COMMISSION_MIN, ENG.STAMP_TAX_BEFORE,
            ENG.STAMP_TAX_AFTER, ENG.TRANSFER_FEE, ENG.SLIPPAGE)
    rows = []
    for mult in (0.0, 0.25, 0.5, 1.0, 2.0):
        ENG.COMMISSION_RATE = orig[0] * mult
        ENG.COMMISSION_MIN = orig[1] * mult
        ENG.STAMP_TAX_BEFORE = orig[2] * mult
        ENG.STAMP_TAX_AFTER = orig[3] * mult
        ENG.TRANSFER_FEE = orig[4] * mult
        ENG.SLIPPAGE = orig[5] * mult
        cfg = ENG.BacktestConfig(name=f"c{mult}", max_positions=20, position_pct=0.05,
                                 min_days_listed=120, exclude_st=True,
                                 min_amount=20_000_000.0)
        st = ENG.perf_stats(ENG.VectorizedBacktester(ix, cfg).run(sig, ex, priority=vr))
        rows.append(dict(cost_mult=mult, cagr=st["cagr"], total_return=st["total_return"],
                         mdd=st["max_drawdown"], n_trades=st["n_trades"],
                         total_fee=st["total_fee"], final_eq=st["final_equity"],
                         avg_hold=st["avg_hold_days"]))
        print(f"  成本×{mult:<4} cagr={st['cagr']*100:+7.2f}%  终值={st['final_equity']/1e4:>9.1f}万  "
              f"总费用={st['total_fee']/1e4:>8.1f}万  交易={st['n_trades']:>5}  "
              f"均持{st['avg_hold_days']:.1f}天")
    (ENG.COMMISSION_RATE, ENG.COMMISSION_MIN, ENG.STAMP_TAX_BEFORE,
     ENG.STAMP_TAX_AFTER, ENG.TRANSFER_FEE, ENG.SLIPPAGE) = orig
    cost_df = pd.DataFrame(rows)
    cost_df.to_csv(os.path.join(OUT, "reconcile_cost.csv"), index=False)

    # ================= B. 换手与费用 =================
    print("\n" + "=" * 100)
    print("B. 换手与费用诊断 (成本×1.0)")
    print("=" * 100)
    cfg = ENG.BacktestConfig(name="G", max_positions=20, position_pct=0.05,
                             min_days_listed=120, exclude_st=True,
                             min_amount=20_000_000.0)
    res = ENG.VectorizedBacktester(ix, cfg).run(sig, ex, priority=vr)
    st = ENG.perf_stats(res)
    tr = res["trades"]
    years = st["years"]
    init = 1_000_000.0
    turnover_per_year = st["n_trades"] / years
    print(f"  年数={years}  交易数={st['n_trades']}  年均交易={turnover_per_year:.0f} 笔")
    print(f"  平均持仓 {st['avg_hold_days']:.1f} 天  平均持仓数 {st['avg_positions']:.1f}")
    print(f"  总费用 {st['total_fee']/1e4:.1f} 万 = 初始资金 {st['total_fee']/init*100:.0f}%  "
          f"= 年均 {st['total_fee']/years/init*100:.1f}% (占初始资金)")
    print(f"  单笔平均收益 {tr['ret'].mean()*100:+.3f}%  中位 {tr['ret'].median()*100:+.3f}%  "
          f"胜率 {(tr['ret']>0).mean()*100:.1f}%")
    print(f"  毛盈亏(不含费用) {tr['pnl'].sum()/init*100:+.0f}%  "
          f"净盈亏 {st['total_return']*100:+.0f}%")
    print(f"  → 毛利+{tr['pnl'].sum()/init*100:.0f}% 被 {st['total_fee']/init*100:.0f}% 的费用吞噬")

    # ================= C. 单笔收益分布 =================
    print("\n" + "=" * 100)
    print("C. 引擎单笔交易收益 vs 事件研究 20 日收益")
    print("=" * 100)
    r = pd.read_csv(os.path.join(OUT, "rigorous_event_study.csv"))
    ev = r[r.label == "MACD翻红 + VR>1.1"].iloc[0]
    print(f"  事件研究(全市场可交易样本): 1日{ev.abs1*100:+.3f}%  5日{ev.abs5*100:+.3f}%  "
          f"10日{ev.abs10*100:+.3f}%  20日{ev.abs20*100:+.3f}%")
    print(f"  引擎单笔实际: 均值 {tr['ret'].mean()*100:+.3f}%  "
          f"（含滑点与费用，持仓 {st['avg_hold_days']:.1f} 天）")
    for h in (1, 5, 10, 20, 60):
        m = tr[tr.hold_days <= h]
        if len(m):
            print(f"    持仓<={h:>2}天 子样本 ({len(m):>5}笔): 均值 {m['ret'].mean()*100:+.3f}%")

    # ================= D. 绕过引擎的事件研究口径组合 =================
    print("\n" + "=" * 100)
    print("D. 绕过引擎: 用事件研究口径直接构造等权组合 (无成本)")
    print("=" * 100)
    codes = A["thscode"]
    n_days_ = ix.n_days
    day_of_row = ix.day_of_row
    close = A["close_price"].astype(np.float64)
    tradable = ((A["days_since_list"] >= 120) & (~A["is_st_now"])
                & np.isfinite(close)
                & (np.nan_to_num(A["amt20"], nan=0) >= 20e6))

    sig_ok = sig & tradable
    sr = np.flatnonzero(sig_ok)
    sd = day_of_row[sr]
    sp = np.nan_to_num(vr[sr], nan=-np.inf)
    order = np.lexsort((-sp, sd))
    sr, sd = sr[order], sd[order]

    def build_port(rows_sel, tag, H_):
        """T+1 开盘买入 → T+1+H 开盘卖出, 等权, 零成本。
        组内定位必须用 ix.code_rows / ix.code_days (面板按 date,thscode 排序)。"""
        daily_sum = np.zeros(n_days_)
        daily_cnt = np.zeros(n_days_)
        for row in rows_sel:
            c = codes[row]
            rows = ix.code_rows[c]
            days = ix.code_days[c]
            pos = np.searchsorted(rows, row)
            if pos >= len(rows) or rows[pos] != row:
                continue
            d0 = days[pos]
            for kk in range(1, H_ + 1):
                t = d0 + kk
                if t >= n_days_:
                    break
                q = pos + kk
                if q >= len(rows) or days[q] != t:
                    continue
                p_in = op[rows[pos + 1]] if pos + 1 < len(rows) else np.nan
                p_out = op[rows[q]]
                if not np.isfinite(p_in) or p_in <= 0 or not np.isfinite(p_out):
                    continue
                daily_sum[t] += p_out / p_in - 1.0
                daily_cnt[t] += 1
        pr = np.where(daily_cnt > 0, daily_sum / np.maximum(daily_cnt, 1), 0.0)
        eq = np.cumprod(1.0 + pr)
        yrs_ = (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days / 365.25
        peak = np.maximum.accumulate(eq)
        print(f"  {tag:<34} 累计 {eq[-1]-1:+8.1%}  年化 {(eq[-1]**(1/yrs_)-1)*100:+7.2f}%  "
              f"最大回撤 {((eq/peak-1).min())*100:6.1f}%  平均持仓数 {daily_cnt.mean():.1f}")
        return eq

    # 每日按量比降序取前 20
    nsig_day = np.bincount(sd, minlength=n_days_)[sd]
    rank = np.zeros(len(sr), dtype=np.int32)
    prev_d, k = -1, 0
    for i, dd in enumerate(sd):
        if dd != prev_d:
            prev_d, k = dd, 0
        rank[i] = k
        k += 1
    print(f"  信号总数(可交易): {len(sr):,}")
    build_port(sr[rank < 20], "每日前20 / 持有20日 / 零成本", 20)
    build_port(sr, "全部信号 / 持有20日 / 零成本", 20)
    build_port(sr[rank >= np.maximum(nsig_day - 20, 0)],
               "每日后20 / 持有20日 / 零成本", 20)

    # ================= E. 基准绝对年化 =================
    print("\n" + "=" * 100)
    print("E. 基准绝对年化 (全A可交易等权, o2o)")
    print("=" * 100)
    cl_all = A["close_price"].astype(np.float64)
    for freq, nm in ((1, "日频"), (5, "5日"), (20, "20日")):
        rets = []
        for c, rows in ix.code_rows.items():
            for i in range(0, len(rows) - freq, freq):
                r0, r1 = rows[i], rows[i + freq]
                if not tradable[r0] or not tradable[r1]:
                    continue
                if op[r0] > 0 and np.isfinite(op[r0]) and np.isfinite(op[r1]):
                    rets.append(op[r1] / op[r0] - 1)
        if not rets:
            continue
        rets = np.array(rets)
        per = 252.0 / freq
        net = (1 + np.mean(rets) - 0.0054) ** per - 1
        print(f"  {nm}等权轮动: 毛年化 {np.mean(rets)*per*100:+7.2f}%  "
              f"扣往返成本 {net*100:+7.2f}%  (样本 {len(rets):,})")

    print("\n  策略G 引擎(含成本) 年化 "
          f"{st['cagr']*100:+.2f}%   引擎(零成本) "
          f"{cost_df[cost_df.cost_mult==0.0]['cagr'].iloc[0]*100:+.2f}%")

    print("\n" + "=" * 100)
    print("结论线索")
    print("=" * 100)
    print("  1) 若 零成本 与 含成本 年化差距 ≈ 费用/初始资金年均占比 -> 缺口主要来自交易成本")
    print("  2) 若 事件口径组合(零成本) 仍为负 -> 缺口来自持仓集中/选股规则")
    print("  3) 若 事件口径组合 明显低于 等权基准 -> 说明信号本身劣于市场平均")
    print(f"\ndone in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
