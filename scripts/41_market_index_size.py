#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
41_market_index_size.py —— 牛熊判定的「市场指数」换成小市值口径会不会更好？

用户提问：「如果以小市值构筑指数 会不会效果更好呢 比如市值小于50%的等权指数」

⚠️ 必须把问题拆成两层，否则会得出错误结论：

  A. 描述层：换口径后，自建指数的长期收益 / 波动 / 与真实指数的分歧如何变化。
     —— 这一层只说明「指数长什么样」，**不能**回答「是否更好」。

  B. 决策层：把它接到默认 K3 策略上**当牛熊过滤器**用，看 CAGR / MDD / Sharpe。
     —— 「指数涨得多」≠「择时更准」。小市值指数波动更大、更容易上下穿 MA60，
        可能反而制造更多假信号。**这一层才是「更好」的定义。**

默认 K3（与 app/engine.py 的 DEFAULT_PARAMS 一致）：
    距MA60 D1（最超跌 10%） + 市值最小 30%（size_grp 0~2） + 市场 < MA60（熊市才买）
    持有 20 日，T+1 开盘买 / T+1+H 开盘卖，0.3% 双边成本

产出：
    output/v3b_market_size_index.csv    各口径的描述层 + 决策层指标
    reports/41_market_index_size.md     结论报告
"""
import os
import sys
import time
import json
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v3b_lib as L

HOLD = 20
MA_WIN = 60
_t0 = time.time()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log(m):
    print(f"[{time.time() - _t0:6.1f}s] {m}", flush=True)


# ================================================================ 市场指数构造
def mkt_oret_sub(oret, C, sub):
    """指定行子集上的「等权日收益」+ 每日成分数。

    ⚠️ 走 `v3b_lib.market_oret`（与引擎**同一个实现**），不要在这里另写一份
       bincount —— 两边各写一份会让口径悄悄漂移，而「引擎的数字和研究报告对不上」
       是最难查的一类问题。
    """
    d = C["day_idx"]
    ok = np.isfinite(oret) if sub is None else (np.isfinite(oret) & np.asarray(sub, bool))
    return L.market_oret(oret, C, sub), np.bincount(d[ok], minlength=C["n_days"])


def build_index(mk_d, C, ma_win=MA_WIN):
    """mk_d → (nav, ma, dist, bull)。与 engine._build 完全同口径"""
    nav = np.cumprod(1.0 + np.nan_to_num(mk_d))
    ma = pd.Series(nav).rolling(ma_win, min_periods=ma_win).mean().values
    dist = np.where(np.isfinite(ma) & (ma > 0), nav / ma - 1.0, np.nan)
    # ⚠️ 与 engine 一致：ma 未成形时 `nav > nan` → False → 被当成「熊市」。
    #    早期 59 天因此全算熊市。这是既有行为，这里不引入额外差异，但要在报告里说明。
    bull = np.where(np.isfinite(ma), nav > ma, False)
    return nav, ma, dist, bull


# 市值口径。size_grp 0..9，0 = 最小 10%（面板既有分位序号，非「档位」）
# ⚠️ `all` 返回 None = **不加市值过滤**（真正的全A），不能用 `size_grp 0~9` 代替 ——
#    那样会把 size_grp 缺失的行也排除掉，与引擎/README 的 29.81% 基线对不上。
VARIANTS = [
    ("all",     "全A等权（当前基线）",    lambda sg: None),
    ("small50", "小50% 等权",            lambda sg: sg <= 4),
    ("small30", "小30% 等权",            lambda sg: sg <= 2),
    ("small20", "小20% 等权",            lambda sg: sg <= 1),
    ("small10", "小10% 等权",            lambda sg: sg == 0),
    ("big50",   "大50% 等权（方向对照）", lambda sg: sg >= 5),
]

def sub_of(sg, f):
    """口径函数 → 行掩码。`None` 表示**不加过滤**（全A），此时也不要叠 `isfinite`。"""
    s = f(sg)
    return None if s is None else (s & np.isfinite(sg))


INDEX_FILES = [
    ("上证指数", "index_sh000001.json"),
    ("沪深300", "index_hs300.json"),
    ("中证500", "index_zz500.json"),
    ("中证1000", "index_zz1000.json"),
]


def load_real_indices():
    """真实指数 → {name: Series(按日 close)}，用于「口径分歧」对照（仅 2023 起）

    ⚠️ `data/raw/index_*.json` 的实际结构是 **list[dict]**，键为
       `date_ms`（毫秒时间戳）/ `open_price` / `close_price` / `high_price` /
       `low_price` / `volume` / `turnover` —— **不是** `date`/`close`。
       早先按 `date`/`close` 取会静默拿到 None，全部指数被跳过（异常被吞掉），
       表现成「对照表里一片 —」，很难查。这里兼容两种写法。
    """
    out = {}
    for name, fn in INDEX_FILES:
        p = os.path.join(ROOT, "data", "raw", fn)
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
            rows = raw if isinstance(raw, list) else (raw.get("data") or raw.get("klines") or [])
            if not rows:
                continue
            if isinstance(rows[0], dict):
                def pick(r, *keys):
                    for k in keys:
                        if k in r and r[k] is not None:
                            return r[k]
                    return None
                ds = [pick(r, "date_ms", "date", "day", "ts") for r in rows]
                cs = [pick(r, "close_price", "close", "c") for r in rows]
            else:  # [ts, o, h, l, c, ...]
                ds = [r[0] for r in rows]
                cs = [r[4] for r in rows]
            # date_ms 是毫秒时间戳；若是字符串日期则原样解析
            if isinstance(ds[0], (int, float)):
                idx = pd.to_datetime(np.asarray(ds, np.int64), unit="ms").normalize()
            else:
                idx = pd.to_datetime(ds).normalize()
            s = pd.Series(np.asarray(cs, float), index=idx)
            s = s[~s.index.duplicated(keep="last")].sort_index()
            out[name] = s
        except Exception as e:      # 可选数据，坏了不该拖垮研究
            log(f"  ! 读 {fn} 失败：{type(e).__name__}: {e}")
    return out


def main():
    log("加载面板（只取需要的列，降低内存占用）…")
    df = L.load_clean(["px_ma60_pct", "fwd20"])
    C = L.ctx(df)
    n, nd = C["n"], C["n_days"]
    log(f"面板 {n:,} 行 / {nd} 日 / {C['starts'].size} 只")

    oret, oret_sig = L.oret_from(df, C)
    sg = df["size_grp"].values.astype(np.float64)
    D = C["day_idx"]

    # K3 基础信号（市场过滤之前）：距MA60 D1 + 市值最小 30%
    px60d = L.decile(df["px_ma60_pct"].values.astype(np.float64), C, 10)
    base = (px60d == 1) & np.isfinite(sg) & (sg >= 0) & (sg <= 2)
    log(f"K3 基础信号（未加市场过滤）：{int(base.sum()):,} 行")

    op = df["open_price"].values.astype(np.float64)
    buy_open = np.where(df["can_buy_open"].values.astype(bool), op, np.nan)
    sell_open = L.build_sell_open(op, df["can_sell_open"].values.astype(bool), C)

    real = load_real_indices()
    log(f"真实指数：{list(real)}")
    ud = pd.DatetimeIndex(C["ud"])
    yrs = ud.year.values

    rows = []
    series_store = {}
    for key, label, subf in VARIANTS:
        sub = sub_of(sg, subf)
        mk_d, cnt_v = mkt_oret_sub(oret, C, sub)
        nav, ma, dist, bull = build_index(mk_d, C)
        series_store[key] = dict(nav=nav, ma=ma, dist=dist, bull=bull)

        # ---------- A. 描述层 ----------
        okd = np.isfinite(ma)
        ret_all = float(nav[-1] - 1.0)
        cagr_idx = float(nav[-1] ** (252.0 / max(int(okd.sum()), 1)) - 1.0) if nav[-1] > 0 else np.nan
        dd = float((nav / np.maximum.accumulate(nav) - 1.0).min())
        vol = float(np.nanstd(np.nan_to_num(mk_d)) * np.sqrt(252))
        n_sw = int(np.sum(bull[1:] != bull[:-1]))
        bear_pct = float(1.0 - bull[okd].mean()) if okd.any() else np.nan
        avg_n = float(cnt_v[cnt_v > 0].mean())

        rec = dict(
            key=key, label=label,
            n_avg=round(avg_n, 1),
            idx_total=round(ret_all, 3),
            idx_cagr=round(cagr_idx, 4) if np.isfinite(cagr_idx) else None,
            idx_mdd=round(dd, 4),
            idx_vol=round(vol, 4),
            dist_now=round(float(dist[-1]) * 100, 2) if np.isfinite(dist[-1]) else None,
            bull_now=bool(bull[-1]),
            n_switch=n_sw,
            bear_pct=round(bear_pct * 100, 1),
        )

        # ---------- 与真实指数的分歧（仅重叠区间） ----------
        for nm, s in real.items():
            # ⚠️ 必须先把「指数日期」映射到「面板日历位置」，再两边按同一批日期取数。
            #    早先写成 `s.values[pos[m]]` —— `pos` 是 **ud（面板日历）** 的位置，
            #    却拿去索引 **s（指数序列）**，两套下标混用，结果毫无意义。
            rpos = ud.searchsorted(s.index.values, side="left")
            inb = rpos < len(ud)
            hit = np.zeros(len(rpos), bool)
            hit[inb] = (ud[rpos[inb]] == s.index.values[inb])
            if hit.sum() < 120:
                continue
            pi = rpos[hit]                      # 面板日历位置
            ci = np.flatnonzero(hit)            # 指数序列位置
            # 真实指数自身的 MA60（在**全历史**上算，再取重叠段）
            ma60r = pd.Series(s.values).rolling(60, min_periods=60).mean().values
            rb = (bool(s.values[ci[-1]] > ma60r[ci[-1]])
                  if np.isfinite(ma60r[ci[-1]]) else None)
            # 重叠区间：自建净值 vs 真实指数的日收益相关 + 累计收益差
            r_self = np.diff(np.log(nav[pi]))
            r_real = np.diff(np.log(s.values[ci]))
            k = min(len(r_self), len(r_real))
            corr = float(np.corrcoef(r_self[-k:], r_real[-k:])[0, 1]) if k > 30 else np.nan
            cum_self = float(nav[pi[-1]] / nav[pi[0]] - 1.0)
            cum_real = float(s.values[ci[-1]] / s.values[ci[0]] - 1.0)
            nd_over = max(int(len(ci)), 1)
            gap_ann = float((np.log1p(cum_self) - np.log1p(cum_real)) / nd_over * 252.0)
            rec[f"corr_{nm}"] = round(corr, 3) if np.isfinite(corr) else None
            rec[f"realbull_{nm}"] = rb
            rec[f"gap_{nm}"] = round(gap_ann, 4)
        rows.append(rec)

    # ---------- B. 决策层：把每个口径当过滤器 ----------
    log("决策层：逐个口径做市场过滤，跑 K3 策略…")
    for rec in rows:
        st = series_store[rec["key"]]
        bull = st["bull"]
        sig = base & (~bull[D])
        n_sig = int(sig.sum())
        r, ed, pos = L.simulate_hold(sig, buy_open, sell_open, C, HOLD)
        ts = L.trade_stats(r, cost=L.RT_COST) if len(r) else None
        net, cnt = L.portfolio_nav(sig, D, nd, oret_sig, np.full(n, float(HOLD)), C)
        series_store[rec["key"]]["net"] = net
        ast = L.ann_stats(net, nd)
        act = cnt > 0
        # 逐年（看稳定性，不只看总 CAGR）
        yr_ret = {}
        for y in range(2015, 2027):
            s = yrs == y
            if s.sum() < 20:
                continue
            yr_ret[y] = float(np.prod(1.0 + net[s]) - 1.0)
        rec.update(
            n_signal=n_sig, n_trade=int(ts["n"]) if ts else 0,
            cagr=round(ast["cagr"], 4), mdd=round(ast["mdd"], 4),
            sharpe=round(ast["sharpe"], 3), vol_p=round(ast["vol"], 4),
            win=round(ts["win"], 4) if ts else None,
            pf=round(ts["pf"], 3) if ts else None,
            mean=round(ts["mean"], 4) if ts else None,
            avg_hold=round(float(cnt[act].mean()), 1) if act.any() else 0.0,
            active_pct=round(float(act.mean()), 4),
            n_year_pos=sum(1 for v in yr_ret.values() if v > 0),
            n_year=len(yr_ret),
        )
        rec["_yr"] = yr_ret

    # ---------- 参照：不加市场过滤 ----------
    log("参照：K3 基础信号 + 不做市场过滤…")
    r0, _, _ = L.simulate_hold(base, buy_open, sell_open, C, HOLD)
    ts0 = L.trade_stats(r0, cost=L.RT_COST) if len(r0) else None
    net0, cnt0 = L.portfolio_nav(base, D, nd, oret_sig, np.full(n, float(HOLD)), C)
    a0 = L.ann_stats(net0, nd)
    ref_nofilter = dict(
        key="nofilter", label="【参照】不加市场过滤",
        n_signal=int(base.sum()), n_trade=int(ts0["n"]) if ts0 else 0,
        cagr=round(a0["cagr"], 4), mdd=round(a0["mdd"], 4),
        sharpe=round(a0["sharpe"], 3), vol_p=round(a0["vol"], 4),
        win=round(ts0["win"], 4) if ts0 else None,
        pf=round(ts0["pf"], 3) if ts0 else None,
        mean=round(ts0["mean"], 4) if ts0 else None,
    )
    log(f"  不加过滤：CAGR {a0['cagr']*100:.2f}%  MDD {a0['mdd']*100:.2f}%  Sharpe {a0['sharpe']:.3f}")

    # ============================================================ E. IS / OOS 稳健性
    # ⚠️ 小市值溢价在 2021 年后（微盘股时代）显著增强。若「改善」只出现在 OOS，
    #    那更像是**押中了最近这一轮风格**，而不是过滤器本身更准。必须拆开看。
    log("E. IS/OOS 拆分…")
    is_m, oos_m = (yrs <= 2020), (yrs >= 2021)
    is_yrs = max(int(is_m.sum()) / 252.0, 1e-9)
    oos_yrs = max(int(oos_m.sum()) / 252.0, 1e-9)
    for r in rows:
        net = series_store[r["key"]]["net"]
        for tag, m, nyr in (("is", is_m, is_yrs), ("oos", oos_m, oos_yrs)):
            nav_s = np.cumprod(1.0 + net[m])
            r[f"{tag}_cagr"] = round(float(nav_s[-1] ** (1.0 / nyr) - 1.0), 4)
            r[f"{tag}_mdd"] = round(float((nav_s / np.maximum.accumulate(nav_s) - 1.0).min()), 4)

    # ============================================================ F. MA 窗口敏感性
    log("F. MA 窗口敏感性（20/60/120）…")
    subf = {k: f for k, _, f in VARIANTS}
    lblf = {k: l for k, l, _ in VARIANTS}
    ma_rows = []
    for key in ("all", "small50", "small30"):
        sub = sub_of(sg, subf[key])
        mk_d_v, _ = mkt_oret_sub(oret, C, sub)
        for w in (20, 60, 120):
            _, _, _, bull_w = build_index(mk_d_v, C, w)
            sig = base & (~bull_w[D])
            if sig.sum() < 50:
                continue
            net_w, _ = L.portfolio_nav(sig, D, nd, oret_sig, np.full(n, float(HOLD)), C)
            a_w = L.ann_stats(net_w, nd)
            ma_rows.append(dict(variant=key, label=lblf[key], ma=w,
                                n_signal=int(sig.sum()),
                                cagr=round(a_w["cagr"], 4),
                                mdd=round(a_w["mdd"], 4),
                                sharpe=round(a_w["sharpe"], 3)))

    # ============================================================ G. 池错配检验
    # 若「小市值过滤器更好」只是因为**过滤器与被交易池同源**（策略本来就在买小市值），
    # 那么把它接到**大市值**策略上应该失效。这一步用来区分
    # 「更准的择时信号」与「恰好匹配了信号池」。
    log("G. 池错配检验（大市值策略 × 小市值过滤器）…")
    base_big = (px60d == 1) & np.isfinite(sg) & (sg >= 7)
    mix_rows = []
    if int(base_big.sum()) >= 500:
        for key in ("all", "small50", "small30", "big50"):
            bull_v = series_store[key]["bull"]
            sig = base_big & (~bull_v[D])
            if sig.sum() < 200:
                continue
            r_b, _, _ = L.simulate_hold(sig, buy_open, sell_open, C, HOLD)
            ts_b = L.trade_stats(r_b, cost=L.RT_COST) if len(r_b) else None
            net_b, _ = L.portfolio_nav(sig, D, nd, oret_sig, np.full(n, float(HOLD)), C)
            a_b = L.ann_stats(net_b, nd)
            mix_rows.append(dict(filter=key, label=lblf[key], n_signal=int(sig.sum()),
                                 cagr=round(a_b["cagr"], 4), mdd=round(a_b["mdd"], 4),
                                 sharpe=round(a_b["sharpe"], 3),
                                 win=round(ts_b["win"], 4) if ts_b else None))
        # 大市值策略不加过滤
        net_b0, _ = L.portfolio_nav(base_big, D, nd, oret_sig, np.full(n, float(HOLD)), C)
        a_b0 = L.ann_stats(net_b0, nd)
        mix_nofilter = dict(n_signal=int(base_big.sum()), cagr=round(a_b0["cagr"], 4),
                            mdd=round(a_b0["mdd"], 4), sharpe=round(a_b0["sharpe"], 3))

    # ============================================================ H. 择时准确度
    # 直接问：被该口径判为「熊市」的日子，K3 基础信号后续 20 日是不是真的更好？
    # 这是过滤器**本身**的质量，与它恰好覆盖多少信号无关。
    log("H. 择时准确度（熊市日 vs 牛市日的 K3 基础信号前 20 日收益）…")
    fwd20 = df["fwd20"].values.astype(np.float64)

    def daily_mean(x, d, ndays):
        ok = np.isfinite(x)
        s = np.bincount(d[ok], weights=x[ok], minlength=ndays)
        c = np.bincount(d[ok], minlength=ndays)
        return np.where(c > 0, s / np.maximum(c, 1), np.nan), c

    tim_rows = []
    for key, label, _ in VARIANTS:
        bd = series_store[key]["bull"][D]
        m_bear, _ = daily_mean(fwd20[base & (~bd)], D[base & (~bd)], nd)
        m_bull, _ = daily_mean(fwd20[base & bd], D[base & bd], nd)
        # ⚠️ 不能用**配对**检验：`bull` 是逐日状态，同一天要么全熊要么全牛，
        #    两组的日子集合**天然不相交**，配对样本恒为空（踩过这个坑）。
        a = m_bear[np.isfinite(m_bear)]
        b = m_bull[np.isfinite(m_bull)]
        n_a, n_b = len(a), len(b)
        if n_a < 30 or n_b < 30:
            continue
        se = np.sqrt(a.var(ddof=1) / n_a + b.var(ddof=1) / n_b)
        t_raw = float((a.mean() - b.mean()) / se) if se > 0 else np.nan
        # ⚠️ fwd20 是重叠窗口，逐日序列有 ~20 日自相关 → 朴素 t 严重高估。
        #    除以 sqrt(20) 做保守折算（近似「不重叠样本」的 t）。
        t_adj = t_raw / np.sqrt(HOLD) if np.isfinite(t_raw) else np.nan
        tim_rows.append(dict(label=label, n_bear_day=n_a, n_bull_day=n_b,
                             bear=round(float(a.mean()) * 100, 3),
                             bull=round(float(b.mean()) * 100, 3),
                             diff=round(float(a.mean() - b.mean()) * 100, 3),
                             t_raw=round(t_raw, 2) if np.isfinite(t_raw) else None,
                             t_adj=round(t_adj, 2) if np.isfinite(t_adj) else None))

    # ---------- 输出 ----------
    dfout = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    outp = os.path.join(L.OUT, "v3b_market_size_index.csv")
    dfout.to_csv(outp, index=False, encoding="utf-8-sig")
    log(f"写出 {outp}")

    # 控制台表格
    print("\n" + "=" * 108)
    print("A. 描述层 —— 不同市值口径的自建指数")
    print("=" * 108)
    hdr = f"{'口径':<22}{'成分数':>8}{'总收益':>10}{'年化':>9}{'MDD':>9}{'波动':>8}{'距MA60':>9}{'熊市日%':>9}{'切换':>6}"
    print(hdr)
    for r in rows:
        print(f"{r['label']:<22}{r['n_avg']:>8.0f}{r['idx_total']*100:>9.1f}%"
              f"{r['idx_cagr']*100:>8.2f}%{r['idx_mdd']*100:>8.1f}%{r['idx_vol']*100:>7.1f}%"
              f"{r['dist_now']:>8.2f}%{r['bear_pct']:>8.1f}%{r['n_switch']:>6d}")

    print("\n" + "=" * 108)
    print("B. 决策层 —— 用它当牛熊过滤器跑 K3（距MA60 D1 + 最小30% + 熊市，持有20日）")
    print("=" * 108)
    hdr = f"{'过滤器口径':<22}{'信号数':>9}{'成交数':>9}{'CAGR':>9}{'MDD':>9}{'Sharpe':>9}{'胜率':>8}{'PF':>7}{'正收益年':>9}"
    print(hdr)
    print(f"{ref_nofilter['label']:<22}{ref_nofilter['n_signal']:>9,}{ref_nofilter['n_trade']:>9,}"
          f"{ref_nofilter['cagr']*100:>8.2f}%{ref_nofilter['mdd']*100:>8.1f}%"
          f"{ref_nofilter['sharpe']:>9.3f}{ref_nofilter['win']*100:>7.1f}%"
          f"{ref_nofilter['pf']:>7.2f}{'—':>9}")
    for r in rows:
        star = " ★" if r["key"] == "all" else ""
        print(f"{r['label']:<22}{r['n_signal']:>9,}{r['n_trade']:>9,}"
              f"{r['cagr']*100:>8.2f}%{r['mdd']*100:>8.1f}%{r['sharpe']:>9.3f}"
              f"{r['win']*100:>7.1f}%{r['pf']:>7.2f}{r['n_year_pos']:>6d}/{r['n_year']:<2d}{star}")

    print("\n" + "=" * 108)
    print("C. 逐年收益（决策层，%）")
    print("=" * 108)
    ylist = sorted(rows[0]["_yr"].keys())
    print(f"{'口径':<22}" + "".join(f"{y:>8}" for y in ylist))
    for r in rows:
        print(f"{r['label']:<22}" + "".join(
            (f"{r['_yr'][y]*100:>7.1f}%" if y in r["_yr"] else f"{'—':>8}") for y in ylist))

    print("\n" + "=" * 108)
    print("D. 与真实指数的对照（重叠区间 2023 起）：相关 / 自建相对指数的年化超额(pp) / 指数末值牛熊")
    print("=" * 108)
    print(f"{'口径':<22}" + "".join(f"{nm:>21}" for nm, _ in INDEX_FILES))
    for r in rows:
        cells = []
        for nm, _ in INDEX_FILES:
            c = r.get(f"corr_{nm}")
            g = r.get(f"gap_{nm}")
            b = r.get(f"realbull_{nm}")
            if c is None:
                cells.append("—")
            else:
                cells.append(f"{c:.2f}/{g*100:+.1f}/{'牛' if b else '熊'}")
        print(f"{r['label']:<22}" + "".join(f"{c:>21}" for c in cells))

    print("\n" + "=" * 108)
    print("E. IS / OOS 拆分（决策层 CAGR）—— 改善是否只集中在最近这一轮小市值风格？")
    print("=" * 108)
    print(f"{'过滤器口径':<22}{'IS 2015-20':>14}{'OOS 2021-26':>14}{'IS MDD':>11}{'OOS MDD':>11}")
    for r in rows:
        print(f"{r['label']:<22}{r['is_cagr']*100:>13.2f}%{r['oos_cagr']*100:>13.2f}%"
              f"{r['is_mdd']*100:>10.1f}%{r['oos_mdd']*100:>10.1f}%")

    print("\n" + "=" * 108)
    print("F. MA 窗口敏感性（换成 MA20 / MA120 是否仍成立）")
    print("=" * 108)
    print(f"{'口径':<22}{'MA':>5}{'信号数':>10}{'CAGR':>9}{'MDD':>9}{'Sharpe':>9}")
    for m in ma_rows:
        print(f"{m['label']:<22}{m['ma']:>5}{m['n_signal']:>10,}"
              f"{m['cagr']*100:>8.2f}%{m['mdd']*100:>8.1f}%{m['sharpe']:>9.3f}")

    if mix_rows:
        print("\n" + "=" * 108)
        print("G. 池错配检验 —— 同一批过滤器接到【大市值超跌】策略（距MA60 D1 + 市值最大30%）")
        print("=" * 108)
        print(f"{'过滤器口径':<22}{'信号数':>10}{'CAGR':>9}{'MDD':>9}{'Sharpe':>9}{'胜率':>8}")
        print(f"{'【参照】不加过滤':<22}{mix_nofilter['n_signal']:>10,}"
              f"{mix_nofilter['cagr']*100:>8.2f}%{mix_nofilter['mdd']*100:>8.1f}%"
              f"{mix_nofilter['sharpe']:>9.3f}{'—':>8}")
        for m in mix_rows:
            print(f"{m['label']:<22}{m['n_signal']:>10,}{m['cagr']*100:>8.2f}%"
                  f"{m['mdd']*100:>8.1f}%{m['sharpe']:>9.3f}"
                  f"{(m['win']*100 if m['win'] else 0):>7.1f}%")

    print("\n" + "=" * 108)
    print("H. 择时准确度 —— K3 基础信号在前 20 日的平均收益（按该口径判定的牛/熊日分组）")
    print("=" * 108)
    print(f"{'口径':<22}{'熊日数':>8}{'牛日数':>8}{'熊市日均':>11}{'牛市日均':>11}{'差(pp)':>10}{'t(朴素)':>10}{'t(保守)':>10}")
    for m in tim_rows:
        print(f"{m['label']:<22}{m['n_bear_day']:>8,}{m['n_bull_day']:>8,}"
              f"{m['bear']:>10.2f}%{m['bull']:>10.2f}%"
              f"{m['diff']:>+9.2f}%{m['t_raw'] if m['t_raw'] is not None else 0:>10.2f}"
              f"{m['t_adj'] if m['t_adj'] is not None else 0:>10.2f}")

    with open(os.path.join(L.OUT, "v3b_market_size_index_ref.json"), "w", encoding="utf-8") as f:
        json.dump({"nofilter": ref_nofilter, "variants": rows,
                   "ma_window": ma_rows,
                   "universe_mismatch": mix_rows,
                   "universe_mismatch_nofilter": mix_nofilter if mix_rows else None,
                   "timing_accuracy": tim_rows,
                   "base_n_signal": int(base.sum())}, f, ensure_ascii=False, indent=1)
    log("完成")
    return rows, ref_nofilter


if __name__ == "__main__":
    main()
