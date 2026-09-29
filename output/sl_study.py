#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sl_study.py —— 「加 10% 止损会不会更好」的离线实证检验

统一模型（关键：三种出场共用同一套日度归集）
------------------------------------------------
每笔交易在「建仓日 entry = 信号日+1」以开盘价买入。定义 m = 完整持有的天数：
  · 无止损      m = 20
  · 收盘触发止损 m ∈ [1,20]（第 m 日收盘 ≤ 止损价 → 第 m+1 日开盘卖）
  · 日内触发止损 第 j 日盘中触及止损价 → 当日按止损价卖（m = j，另加当日部分收益）

日度归集对三者完全一致：该笔参与日槽 j 当且仅当 `m > j`（j = 0..19），
收益用 oret_sig（T+1 开盘 → T+2 开盘），与引擎 portfolio_nav 同口径。
收盘触发在交易级上用 sell_open[entry+m]（含跌停顺延），与 v3b_lib.simulate_hold 一致。

纪律：
  1. 先逐位复现服务端基线，不通过就不继续；
  2. 止损与基线共用**同一批可结算信号**，保证可比；
  3. 同时给交易级与日度再平衡组合两套口径，并注明不可混用；
  4. 日内触发是**乐观上界**（假设按止损价成交、跌停也能卖），收盘触发是**保守下界**。

内存：面板 3.8GB、本机空闲仅 ~90MB 且服务常驻 → 按 rowgroup 分块读、块内过滤后再拼。
"""
import os
import sys
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import v3b_lib as L  # noqa: E402

PANEL = os.path.join(ROOT, "data", "processed", "v2_panel.parquet")
COLS = ["thscode", "date", "open_price", "high_price", "low_price", "close_price",
        "can_buy_open", "can_sell_open", "px_ma60_pct", "size_grp", "ret",
        "days_since_list", "is_st_now"]

HOLD = 20
RT_COST = L.RT_COST
SL_GRID = [0.05, 0.08, 0.10, 0.15, 0.20, 0.25]


def load_min():
    """分块读 → 块内过滤 → 拼接。峰值 ≈ 清洗后(~1.6GB) + 单块"""
    parts = []
    f = pq.ParquetFile(PANEL)
    for i in range(f.metadata.num_row_groups):
        t = f.read_row_group(i, columns=COLS)
        d = t.to_pandas()
        del t
        m = (~d["is_st_now"].fillna(True)) & (d["days_since_list"].fillna(0) >= 120) \
            & d["ret"].notna()
        parts.append(d.loc[m])
        del d
    df = pd.concat(parts, ignore_index=True)
    del parts
    return df.drop(columns=["is_st_now", "days_since_list"]) \
             .sort_values(["thscode", "date"], kind="stable").reset_index(drop=True)


def main():
    print("=" * 78, flush=True)
    print("STEP 0  读面板（分块过滤）", flush=True)
    df = load_min()
    print(f"  清洗后 {len(df):,} 行 / {df['thscode'].nunique():,} 只", flush=True)

    C = L.ctx(df)
    n, nd = C["n"], C["n_days"]
    D, ud = C["day_idx"], C["ud"]

    print("STEP 1  预计算", flush=True)
    op = df["open_price"].values.astype(np.float64)
    cl = df["close_price"].values.astype(np.float64)
    lo = df["low_price"].values.astype(np.float64)
    oret, oret_sig = L.oret_from(df, C)
    sell_open = L.build_sell_open(op, df["can_sell_open"].values.astype(bool), C)
    buy_open = np.where(df["can_buy_open"].values.astype(bool), op, np.nan)
    mk_d = L.market_oret(oret, C)
    nav_m = np.cumprod(1.0 + np.nan_to_num(mk_d))
    ma60 = pd.Series(nav_m).rolling(60, min_periods=60).mean().values
    bull = np.where(np.isfinite(ma60), nav_m > ma60, False)
    dec_px = L.decile(df["px_ma60_pct"].values.astype(np.float64), C, 10)
    size_grp = df["size_grp"].values.astype(np.float64)
    print(f"  牛市日占比 {bull.mean():.4f} / {nd} 个交易日", flush=True)

    print("STEP 2  K3 默认 mask（距MA60 D1 + 市值最小30% + 熊市）", flush=True)
    mask = ((dec_px == 1) & np.isfinite(size_grp) & (size_grp >= 0) & (size_grp <= 2)
            & (~bull[D]))
    print(f"  信号数 {int(mask.sum()):,}", flush=True)

    # ---------------- 基线复现 ----------------
    print("\nSTEP 3  逐位复现服务端基线", flush=True)
    r, ed, p = L.simulate_hold(mask, buy_open, sell_open, C, HOLD)
    ts0 = L.trade_stats(r, cost=RT_COST)
    net0, cnt0 = L.portfolio_nav(mask, D, nd, oret_sig, np.full(n, float(HOLD)), C)
    st0 = L.ann_stats(net0, nd)
    act0 = cnt0 > 0
    checks = [("n_trade", ts0["n"], 105152), ("win", ts0["win"], 0.5851),
              ("mean", ts0["mean"], 0.045132), ("median", ts0["median"], 0.025915),
              ("pf", ts0["pf"], 2.1880), ("CAGR", st0["cagr"], 0.2954),
              ("MDD", st0["mdd"], -0.4134), ("Sharpe", st0["sharpe"], 0.9717),
              ("avg_hold", cnt0[act0].mean(), 1058.0)]
    tol = {"n_trade": 0, "win": 3e-4, "mean": 2e-5, "median": 2e-5, "pf": 3e-3,
           "CAGR": 5e-4, "MDD": 5e-4, "Sharpe": 3e-3, "avg_hold": 2.0}
    bad = []
    for k, mine, ref in checks:
        d_ = abs(mine - ref)
        good = d_ <= tol[k]
        if not good:
            bad.append(k)
        print(f"  {k:<10} mine={mine:>12.6f}  ref={ref:>12.6f}  差={d_:.2e}  "
              f"{'✅' if good else '❌'}", flush=True)
    if bad:
        print(f"\n  >>> 基线复现失败（{bad}）—— 停止，后续结论不可信", flush=True)
        return
    print("\n  >>> 基线复现通过 ✅（口径与服务端一致，可继续）", flush=True)

    # ---------------- 交易级：预取持有期切片 ----------------
    # ⚠️⚠️ 两套索引必须分开，这是本脚本最容易搞错的地方（踩过，基线一度差 6pp）：
    #   · 收益/日期归集 → 用**信号日行号 p**：引擎 portfolio_nav 的口径是
    #       「第 j 日槽 = oret_sig[p+j]，记在交易日 D[p]+j 上」（j=0..H-1）
    #       j=0..19 恰好覆盖 买入open(entry) → 卖出open(entry+20)，无缝无重。
    #   · 止损触发价 → 用**建仓日行号 entry = p+1**：要检查的是实际持有的
    #       entry, entry+1, ..., entry+19 这些交易日的 开盘/最低/收盘。
    #   两者相差正好 1 个位置；混用会让组合收益整体挪一天（并多吃一天持有期外的收益）。
    ntr = len(p)
    entry = p + 1
    entry_px = op[entry]
    ar = np.arange(HOLD)[None, :]
    RET_IDX = p[:, None] + ar                      # 收益/日槽（信号日口径）
    ENT_IDX = entry[:, None] + ar                  # 价格触发（建仓日口径）
    D_j = D[p][:, None] + ar                       # 日槽对应的交易日序号
    oret_j = oret_sig[RET_IDX]
    cl_j = cl[ENT_IDX]
    op_j = op[ENT_IDX]
    lo_j = lo[ENT_IDX]
    print(f"\nSTEP 4  止损对照（{ntr:,} 笔可结算交易，持有 {HOLD} 日）", flush=True)

    def classify(sl, intraday):
        """返回 (m, partial, pidx)：m=完整持有天数；partial=日内部分收益；pidx=部分收益的日序号"""
        m = np.full(ntr, HOLD, np.int32)
        partial = np.full(ntr, np.nan)
        pidx = np.full(ntr, -1, np.int32)
        liq = entry_px * (1.0 - sl)
        done = np.zeros(ntr, bool)
        if not intraday:
            for j in range(HOLD):
                hit = (~done) & np.isfinite(cl_j[:, j]) & (cl_j[:, j] <= liq)
                m[hit] = j + 1
                done |= hit
        else:
            for j in range(HOLD):
                gap = (~done) & np.isfinite(op_j[:, j]) & (op_j[:, j] <= liq)
                touch = (~done) & np.isfinite(lo_j[:, j]) & (lo_j[:, j] <= liq)
                hit = gap | touch
                if hit.any():
                    fill = np.where(gap, op_j[:, j], liq)
                    m[hit] = j
                    partial[hit] = fill[hit] / op_j[hit, j] - 1.0
                    pidx[hit] = D_j[hit, j]
                    done |= hit
        return m, partial, pidx

    def evaluate(m, partial, pidx):
        """交易级 + 日度再平衡组合（两者口径不同，分别报告）"""
        row_ex = entry + m
        gross = sell_open[row_ex] / entry_px - 1.0
        has_p = np.isfinite(partial)
        if has_p.any():
            # 日内触发：前 m 个日槽链式 × (1+partial)。须逐槽累乘（m 逐笔不同）
            cum = np.ones(ntr)
            for k in range(HOLD):
                use = has_p & (m > k)
                cum[use] *= (1.0 + oret_j[use, k])
            gross = np.where(has_p, cum * (1.0 + np.nan_to_num(partial)) - 1.0, gross)
        tr = L.trade_stats(gross, cost=RT_COST)
        rsum = np.zeros(nd); csum = np.zeros(nd); cntd = np.zeros(nd, np.int64)
        for j in range(HOLD):
            live = m > j
            if not live.any():
                continue
            d = D_j[live, j]
            rj = oret_j[live, j]
            okj = np.isfinite(rj) & (d < nd)
            np.add.at(rsum, d[okj], rj[okj])
            np.add.at(csum, d[okj], RT_COST / HOLD)
            np.add.at(cntd, d[okj], 1)
        if has_p.any():
            d = pidx[has_p]; pv = partial[has_p]
            okp = np.isfinite(pv) & (d >= 0) & (d < nd)
            np.add.at(rsum, d[okp], pv[okp])
            np.add.at(csum, d[okp], RT_COST / HOLD)
            np.add.at(cntd, d[okp], 1)
        act = cntd > 0
        net = np.where(act, (rsum - csum) / np.maximum(cntd, 1), 0.0)
        return tr, L.ann_stats(net, nd), net, cntd, act, gross

    hdr = (f"  {'方案':<24}{'止损笔':>7}{'止损率':>7}{'胜率':>8}{'单笔均值':>10}"
           f"{'PF':>7}{'CAGR':>9}{'MDD':>9}{'Sharpe':>8}{'均持仓':>8}")
    print(hdr, flush=True)
    print("  " + "-" * (len(hdr) - 2), flush=True)

    results = {}
    m_full = np.full(ntr, HOLD, np.int32)
    tr_, st_, net_, cnt_, act_, gr_ = evaluate(m_full, np.full(ntr, np.nan),
                                              np.full(ntr, -1, np.int32))
    results["base"] = dict(m=m_full, tr=tr_, st=st_, net=net_, gross=gr_, nstop=0)
    print(f"  {'无止损（基线）':<24}{0:>7}{'—':>7}{tr_['win']:>8.4f}{tr_['mean']:>10.4f}"
          f"{tr_['pf']:>7.3f}{st_['cagr']*100:>8.1f}%{st_['mdd']*100:>8.1f}%"
          f"{st_['sharpe']:>8.3f}{cnt_[act_].mean():>8.0f}", flush=True)

    print("\n  --- 收盘触发（保守下界：第 m 日收盘跌破 → 第 m+1 日开盘卖）---", flush=True)
    for sl in SL_GRID:
        m_, pt_, pid_ = classify(sl, intraday=False)
        tr_, st_, net_, cnt_, act_, gr_ = evaluate(m_, pt_, pid_)
        ns = int((m_ < HOLD).sum())
        results[("close", sl)] = dict(m=m_, tr=tr_, st=st_, net=net_, gross=gr_, nstop=ns)
        print(f"  {f'止损{sl*100:.0f}%':<24}{ns:>7}{ns/ntr*100:>6.1f}%{tr_['win']:>8.4f}"
              f"{tr_['mean']:>10.4f}{tr_['pf']:>7.3f}{st_['cagr']*100:>8.1f}%"
              f"{st_['mdd']*100:>8.1f}%{st_['sharpe']:>8.3f}"
              f"{cnt_[act_].mean():>8.0f}", flush=True)

    print("\n  --- 日内触发（乐观上界：假设一定按止损价成交、跌停也能卖）---", flush=True)
    for sl in SL_GRID:
        m_, pt_, pid_ = classify(sl, intraday=True)
        tr_, st_, net_, cnt_, act_, gr_ = evaluate(m_, pt_, pid_)
        ns = int((np.isfinite(pt_)).sum())
        results[("intra", sl)] = dict(m=m_, tr=tr_, st=st_, net=net_, gross=gr_, nstop=ns)
        print(f"  {f'止损{sl*100:.0f}%':<24}{ns:>7}{ns/ntr*100:>6.1f}%{tr_['win']:>8.4f}"
              f"{tr_['mean']:>10.4f}{tr_['pf']:>7.3f}{st_['cagr']*100:>8.1f}%"
              f"{st_['mdd']*100:>8.1f}%{st_['sharpe']:>8.3f}"
              f"{cnt_[act_].mean():>8.0f}", flush=True)

    # ---------------- 诊断 ----------------
    print("\nSTEP 5  诊断：10% 止损卖掉的票，若不卖会怎样", flush=True)
    m10 = results[("close", 0.10)]["m"]
    stopped = m10 < HOLD
    r_stop = r[stopped]
    real_s = sell_open[entry[stopped] + m10[stopped]] / entry_px[stopped] - 1.0 - RT_COST
    print(f"  被止损 {int(stopped.sum()):,} 笔（{stopped.mean()*100:.1f}%）", flush=True)
    print(f"  若不止损、持有满 {HOLD} 日：均值 {r_stop.mean()*100:+6.2f}%  "
          f"中位 {np.median(r_stop)*100:+6.2f}%  为正占比 {(r_stop>0).mean()*100:.1f}%",
          flush=True)
    print(f"  实际止损出场：            均值 {real_s.mean()*100:+6.2f}%  "
          f"中位 {np.median(real_s)*100:+6.2f}%  为正占比 {(real_s>0).mean()*100:.1f}%",
          flush=True)
    print(f"  → 「不止损 − 止损」均值 {(r_stop.mean()-real_s.mean())*100:+.2f}pp"
          f"（正 = 止损卖早了/卖亏了）", flush=True)
    print(f"  止损后若反弹超过 0 的比例：{(r_stop>0).mean()*100:.1f}%"
          f"，其中涨幅 >10% 的 {(r_stop>0.10).mean()*100:.1f}%", flush=True)
    ns_mask = ~stopped
    print(f"  未止损 {int(ns_mask.sum()):,} 笔：均值 {r[ns_mask].mean()*100:+.2f}%  "
          f"中位 {np.median(r[ns_mask])*100:+.2f}%  胜率 {(r[ns_mask]>0).mean()*100:.1f}%",
          flush=True)

    # ---------------- 逐年 ----------------
    print("\nSTEP 6  逐年对照", flush=True)
    yrs = pd.DatetimeIndex(ud).year.values
    ysig = yrs[D[p]]
    print(f"  {'年':<6}{'笔数':>8}{'止损率':>8}{'基线单笔':>10}{'止损后单笔':>11}"
          f"{'差pp':>9}   {'基线组合':>9}{'止损组合':>9}{'差pp':>9}", flush=True)
    net_b = results["base"]["net"]
    net_s = results[("close", 0.10)]["net"]
    for y in range(2015, 2027):
        s = ysig == y
        if s.sum() < 50:
            continue
        bs = L.trade_stats(r[s], cost=RT_COST)
        ss = stopped[s]
        sl_ret = np.where(ss, sell_open[entry[s] + m10[s]] / entry_px[s] - 1.0 - RT_COST,
                          r[s] - RT_COST)
        ds = yrs == y
        nb = float(np.prod(1.0 + net_b[ds]) - 1.0)
        nsl = float(np.prod(1.0 + net_s[ds]) - 1.0)
        print(f"  {y:<6}{int(s.sum()):>8,}{ss.mean()*100:>7.1f}%{bs['mean']*100:>9.2f}%"
              f"{sl_ret.mean()*100:>10.2f}%{(sl_ret.mean()-bs['mean'])*100:>+8.2f}"
              f"   {nb*100:>8.1f}%{nsl*100:>8.1f}%{(nsl-nb)*100:>+8.1f}", flush=True)

    # ---------------- 分位稳健性：止损后是否「卖在最低点」 ----------------
    print("\nSTEP 7  止损幅度的邻域稳健性（收盘触发）", flush=True)
    for sl in SL_GRID:
        d_ = results[("close", sl)]
        print(f"  {sl*100:>4.0f}%  止损率 {d_['nstop']/ntr*100:>5.1f}%  "
              f"CAGR {d_['st']['cagr']*100:>6.1f}%  vs 基线 {st0['cagr']*100:.1f}%  "
              f"→ 差 {(d_['st']['cagr']-st0['cagr'])*100:>+6.1f}pp   "
              f"Sharpe 差 {d_['st']['sharpe']-st0['sharpe']:>+6.3f}", flush=True)

    # ---------------- STEP 8：决定性诊断 ----------------
    # 把每笔交易按「持仓期内盘中最低点」分桶，看它最终的 20 日收益。
    # 这直接回答：-10% 这个回撤，到底是「危险信号」还是「买点」。
    print("\nSTEP 8  「回撤深度 → 最终结果」分桶（决定性诊断）", flush=True)
    low_ret = np.nanmin(lo_j / entry_px[:, None] - 1.0, axis=1)
    buckets = [(-1.0, -0.35, "盘中跌破 -35%"), (-0.35, -0.25, "盘中 -35%~-25%"),
               (-0.25, -0.20, "盘中 -25%~-20%"), (-0.20, -0.15, "盘中 -20%~-15%"),
               (-0.15, -0.12, "盘中 -15%~-12%"), (-0.12, -0.10, "盘中 -12%~-10%"),
               (-0.10, -0.05, "盘中 -10%~-5%"), (-0.05, 1.0, "盘中浅于 -5%")]
    print(f"  {'回撤桶':<18}{'笔数':>8}{'占比':>8}{'最终20日均值':>13}"
          f"{'中位':>9}{'胜率':>8}{'最终>0占比':>11}", flush=True)
    for a, b, nm in buckets:
        s = (low_ret > a) & (low_ret <= b)
        if s.sum() < 20:
            continue
        rr = r[s]
        print(f"  {nm:<18}{int(s.sum()):>8,}{s.mean()*100:>7.1f}%"
              f"{rr.mean()*100:>12.2f}%{np.median(rr)*100:>8.2f}%"
              f"{(rr>0).mean()*100:>7.1f}%{(rr>0).mean()*100:>10.1f}%", flush=True)
    deep = low_ret <= -0.10
    print(f"\n  盘中曾跌破 -10% 的 {int(deep.sum()):,} 笔（{deep.mean()*100:.1f}%）："
          f"最终 20 日均值 {r[deep].mean()*100:+.2f}%，"
          f"中位 {np.median(r[deep])*100:+.2f}%，"
          f"为正 {(r[deep]>0).mean()*100:.1f}%，"
          f"涨超 10% 的 {(r[deep]>0.10).mean()*100:.1f}%", flush=True)
    shal = low_ret > -0.10
    print(f"  从未跌破 -10% 的 {int(shal.sum()):,} 笔（{shal.mean()*100:.1f}%）："
          f"最终均值 {r[shal].mean()*100:+.2f}%，"
          f"为正 {(r[shal]>0).mean()*100:.1f}%", flush=True)
    print(f"\n  仅看「盘中曾跌破 -10%」这批内部，若按 -10% 止损（收盘触发）后实际拿到：", flush=True)
    sub = deep
    if sub.sum() > 0:
        sub_idx = np.flatnonzero(sub)
        stop_here = stopped[sub_idx]
        held = r[sub_idx]
        cut = np.where(stop_here, sell_open[entry[sub_idx] + m10[sub_idx]]
                       / entry_px[sub_idx] - 1.0 - RT_COST, held - RT_COST)
        print(f"    持有到底 { (held-RT_COST).mean()*100:+.2f}%  vs  止损 {cut.mean()*100:+.2f}%"
              f"  → {(cut.mean()-(held-RT_COST).mean())*100:+.2f}pp", flush=True)

    print(f"\n{'='*78}\n完成。", flush=True)
    print("提示：本脚本只做研究，未改动 app/ 下任何生产代码。", flush=True)


if __name__ == "__main__":
    main()
