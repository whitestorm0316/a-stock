#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engine.py —— 「A股超跌反转」交互式选股/回测引擎

设计目标
--------
1. **启动时一次性预计算**：把面板里所有「策略条件」与「收益口径」都算成 numpy 数组，
   前端改参数时只做布尔掩码的与运算 + 一次组合净值，**毫秒级**返回。
2. **口径与研究报告完全一致**：直接复用 scripts/v3b_lib.py，
   特别是 `oret_sig`（T+1开盘 → T+2开盘）这个致命口径。
3. **支持自定义股票群**：白名单 / 黑名单 / 行业 / 市值范围 / 交易所。

⚠️ 纪律（来自 V3B 两轮踩坑）
- 组合回测必须用 `oret_sig`，用 `oret` 会把信号日之前的暴跌日算进来（实测差 1.37pp/日）。
- `_rank_within_np` 必须双键排序 + int64，否则秩完全错误。
- 市场基准必须自建（指数数据仅 2023 起）。
"""
import os
import sys
import json
import time
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import v3b_lib as L  # noqa: E402

PPY = L.PPY
RT_COST = L.RT_COST


# ================================================================ 面板加载
class Engine:
    def __init__(self):
        t0 = time.time()
        self._load()
        self._build()
        self.build_ms = int((time.time() - t0) * 1000)

    # ------------------------------------------------------------ 原始面板
    def _load(self):
        cols = [
            "thscode", "date", "name", "is_st_now", "days_since_list", "ret",
            "open_price", "close_price", "high_price", "low_price",
            "can_buy_open", "can_sell_open", "size_grp", "limit_pct",
            "ind_code", "ind_name",
            # 价格型
            "ret5", "ret10", "ret20", "ret40", "ret60",
            "px_ma20_pct", "px_ma60_pct", "px_ma120_pct",
            # 量能
            "rvol20", "vr20m", "volz20", "turnover", "volume",
            # MACD
            "dif", "dea", "hist", "hist_z", "hist_upstreak",
            # 波动
            "hv20", "hv60", "atr_pct",
            # 相对强弱
            "rs_mkt20", "rs_sz20", "mkt_r20",
            # 未来收益
            "fwd1", "fwd5", "fwd10", "fwd20", "fwd40", "fwd60",
        ]
        df = L.load_clean(cols)
        self.df = df
        self.C = L.ctx(df)
        # ⚠️ 面板的 thscode/name/ind_name 是 pyarrow 字符串，需显式转 numpy str 数组，
        #    否则 np.char.* 与 np.isin 会因 ObjectDType 报 UFuncNoLoopError
        self.code = np.asarray(self.C["code"], dtype=str)
        self.C["code"] = self.code
        self.D = self.C["day_idx"]
        self.ud = self.C["ud"]
        self.nd = self.C["n_days"]

    # ------------------------------------------------------------ 预计算
    def _build(self):
        df, C = self.df, self.C
        n = C["n"]

        # ---- 收益口径
        self.oret, self.oret_sig = L.oret_from(df, C)
        self.mk_d = L.market_oret(self.oret, C)
        self.CHAIN = {H: L.chain_fwd(self.mk_d, C, H) for H in [1, 5, 10, 20, 40, 60]}
        self.FWD = {H: df[f"fwd{H}"].values.astype(np.float64)
                    for H in [1, 5, 10, 20, 40, 60]}

        # ---- 均线
        cl = df["close_price"].values.astype(np.float64)
        op = df["open_price"].values.astype(np.float64)
        self.cl = cl
        self.ma5 = L.roll_mean(cl, C, 5)
        self.ma10 = L.roll_mean(cl, C, 10)
        self.ma20 = L.roll_mean(cl, C, 20)
        self.ma5_prev = L.roll_shift_mean(cl, C, 5, 1)

        # ---- 可交易开盘价
        can_buy = df["can_buy_open"].values.astype(bool)
        self.can_buy = can_buy
        self.buy_open = np.where(can_buy, op, np.nan)
        self.sell_open = L.build_sell_open(op, df["can_sell_open"].values.astype(bool), C)

        # ---- 市场环境（自建，指数数据仅 2023 起）
        nav = np.cumprod(1.0 + np.nan_to_num(self.mk_d))
        mma20 = pd.Series(nav).rolling(20, min_periods=20).mean().values
        mma60 = pd.Series(nav).rolling(60, min_periods=60).mean().values
        self.mkt_nav = nav
        self.mkt_bull = nav > mma60                       # 牛市：市场净值在 MA60 上
        self.mkt_ma20v60 = mma20 > mma60
        self.mkt_dist60 = np.where(np.isfinite(mma60) & (mma60 > 0), nav / mma60 - 1.0, np.nan)
        self.mhv20 = pd.Series(np.nan_to_num(self.mk_d)).rolling(
            20, min_periods=20).std().values * np.sqrt(252)
        self.mhv_med = float(np.nanmedian(self.mhv20))
        # Breadth：当日上涨家数占比
        dd = self.D
        up = self.oret > 0
        fin = np.isfinite(self.oret)
        br_s = np.bincount(dd[up & fin], minlength=self.nd)
        br_c = np.bincount(dd[fin], minlength=self.nd)
        self.breadth = np.where(br_c > 0, br_s / np.maximum(br_c, 1), np.nan)

        # ---- 分位箱号（1..10，D1=最弱）
        g = lambda c: df[c].values.astype(np.float64)
        self.B = {
            "ret5":   L.decile(g("ret5"), C, 10),
            "ret10":  L.decile(g("ret10"), C, 10),
            "ret20":  L.decile(g("ret20"), C, 10),
            "ret40":  L.decile(g("ret40"), C, 10),
            "ret60":  L.decile(g("ret60"), C, 10),
            "px_ma20":  L.decile(g("px_ma20_pct"), C, 10),
            "px_ma60":  L.decile(g("px_ma60_pct"), C, 10),
            "px_ma120": L.decile(g("px_ma120_pct"), C, 10),
            "rs_sz20":  L.decile(g("rs_sz20"), C, 10),
            "rs_mkt20": L.decile(g("rs_mkt20"), C, 10),
            "hv20":     L.decile(g("hv20"), C, 10),
            "rvol20":   L.decile(g("rvol20"), C, 10),
            "turnover": L.decile(g("turnover"), C, 10),
        }
        # 市值分位（size_grp 已 0~9，0=最小）
        self.size_grp = df["size_grp"].values.astype(np.float64)
        self.size_rank = np.where(np.isfinite(self.size_grp), self.size_grp + 1.0, np.nan)

        # ---- 静态属性（用于股票池筛选）
        self.ind_name = np.asarray(df["ind_name"].fillna("未知").values, dtype=str)
        self.stk_name = np.asarray(df["name"].fillna("").values, dtype=str)
        # 交易所：SH / SZ / BJ（北交所）
        ex = np.where(np.char.endswith(self.code, ".SH"), "SH",
                      np.where(np.char.endswith(self.code, ".BJ"), "BJ", "SZ"))
        self.exchange = ex
        # 股票池的行业清单
        self.industries = sorted(set(self.ind_name.tolist()))

        # ---- 逐日市场状态（供扫描时快速取用）
        self.day_mkt_bull = self.mkt_bull
        self.day_mkt_hv = self.mhv20
        self.day_breadth = self.breadth

        # ---- 交易日字符串
        self.day_str = np.array([str(x)[:10] for x in self.ud])
        self.years = pd.DatetimeIndex(self.ud).year.values

    # ================================================================ 条件
    def build_mask(self, p, pool=None):
        """把前端参数 p 转成布尔掩码。

        p 支持的键：
          px_ma60_min/max   : 距MA60 分位区间（1..10），如 [1,2] = D1~D2
          px_ma120_min/max  : 距MA120 分位区间
          ret20_min/max     : ret20 分位区间
          ret60_min/max     : ret60 分位区间
          size_max          : 市值分组上界（0~9），2 = 最小30%
          mkt_state         : "any" | "bear" | "bull"   市场净值 vs MA60
          mkt_hv            : "any" | "high" | "low"   市场HV20 vs 中位
          breadth_max/min   : Breadth 区间
          rs_sz_min/max     : 相对同规模强弱 分位区间
          hv_min/max        : 个股 HV20 分位区间
          rvol_min/max      : 量比分位区间
          confirm           : list[str]  反转确认信号（默认空 = 不等确认）
          deep_any          : bool  深度超跌 = ret40/ret60/距MA60 任一 D1
          pool              : 股票池（由前端选中）
        """
        B, D, n = self.B, self.D, self.C["n"]
        m = np.ones(n, bool)

        def rng(key, box, lo=None, hi=None):
            if lo is None and hi is None:
                return None
            b = box
            if lo is None:
                lo = 1
            if hi is None:
                hi = 10
            return np.isfinite(b) & (b >= lo) & (b <= hi)

        parts = {}
        if p.get("px_ma60_min") or p.get("px_ma60_max"):
            parts["距MA60"] = rng("px_ma60", B["px_ma60"],
                                  p.get("px_ma60_min"), p.get("px_ma60_max"))
        if p.get("px_ma120_min") or p.get("px_ma120_max"):
            parts["距MA120"] = rng("px_ma120", B["px_ma120"],
                                   p.get("px_ma120_min"), p.get("px_ma120_max"))
        if p.get("ret20_min") or p.get("ret20_max"):
            parts["ret20"] = rng("ret20", B["ret20"],
                                 p.get("ret20_min"), p.get("ret20_max"))
        if p.get("ret60_min") or p.get("ret60_max"):
            parts["ret60"] = rng("ret60", B["ret60"],
                                 p.get("ret60_min"), p.get("ret60_max"))
        if p.get("deep_any"):
            parts["深度超跌"] = (
                np.isfinite(B["ret40"]) & np.isfinite(B["ret60"]) & np.isfinite(B["px_ma60"]) &
                ((B["ret40"] == 1) | (B["ret60"] == 1) | (B["px_ma60"] == 1)))

        # 市值
        smax = p.get("size_max")
        if smax is not None and smax < 9:
            parts[f"市值≤D{smax+1}"] = np.isfinite(self.size_grp) & (self.size_grp <= smax)

        # 市场状态
        ms = p.get("mkt_state", "any")
        if ms == "bear":
            parts["市场<MA60"] = ~self.mkt_bull[D]
        elif ms == "bull":
            parts["市场>MA60"] = self.mkt_bull[D]

        mh = p.get("mkt_hv", "any")
        if mh == "high":
            parts["市场高波动"] = self.mhv20[D] > self.mhv_med
        elif mh == "low":
            parts["市场低波动"] = self.mhv20[D] <= self.mhv_med

        if p.get("breadth_min") is not None:
            parts["Breadth下限"] = self.breadth[D] >= p["breadth_min"]
        if p.get("breadth_max") is not None:
            parts["Breadth上限"] = self.breadth[D] <= p["breadth_max"]

        if p.get("rs_sz_min") or p.get("rs_sz_max"):
            parts["相对同规模"] = rng("rs_sz20", B["rs_sz20"],
                                     p.get("rs_sz_min"), p.get("rs_sz_max"))
        if p.get("hv_min") or p.get("hv_max"):
            parts["个股波动"] = rng("hv20", B["hv20"],
                                   p.get("hv_min"), p.get("hv_max"))
        if p.get("rvol_min") or p.get("rvol_max"):
            parts["量比"] = rng("rvol20", B["rvol20"],
                               p.get("rvol_min"), p.get("rvol_max"))

        # 反转确认信号
        sigs = set(p.get("confirm") or [])
        if sigs:
            if "ma5" in sigs:
                parts["Close>MA5"] = self.cl > self.ma5
            if "ma5up" in sigs:
                parts["MA5向上"] = self.ma5 > self.ma5_prev
            if "up2" in sigs:
                parts["连涨2日"] = np.nan_to_num(self.oret) > 0
            if "ma10" in sigs:
                parts["Close>MA10"] = self.cl > self.ma10
            if "ma20" in sigs:
                parts["Close>MA20"] = self.cl > self.ma20
            if "histpos" in sigs:
                parts["MACD柱>0"] = self.df["hist"].values > 0
            if "difdea" in sigs:
                parts["DIF>DEA"] = self.df["dif"].values > self.df["dea"].values
            if "histup3" in sigs:
                parts["MACD柱连改善3日"] = self.df["hist_upstreak"].values >= 3
            if "volup" in sigs:
                parts["放量(量比>1.2)"] = self.df["rvol20"].values > 1.2

        for k, v in parts.items():
            m &= np.asarray(v, bool)

        # 股票池
        if pool is not None:
            m &= pool

        return m, list(parts.keys())

    # ================================================================ 股票池
    def build_pool(self, spec):
        """spec = {mode, codes:[], codes_text:'', industries:[], exchanges:[],
                   exclude_st:bool}"""
        n = self.C["n"]
        mask = np.ones(n, bool)
        mode = (spec or {}).get("mode", "all")

        if mode == "custom":
            codes = set(spec.get("codes") or [])
            txt = (spec.get("codes_text") or "").replace(",", " ").replace("，", " ")
            for t in txt.split():
                t = t.strip().upper()
                if t:
                    codes.add(t)
                    # 允许用户只写 6 位数字
                    if t.isdigit() and len(t) == 6:
                        codes.add(t + ".SH")
                        codes.add(t + ".SZ")
            if not codes:
                return None, "自定义股票池为空"
            cm = np.isin(self.code.astype(str), list(codes))
            mask &= cm
            if cm.sum() == 0:
                return None, f"自定义股票池未匹配到任何股票（{len(codes)} 个代码）"

        inds = set(spec.get("industries") or [])
        if inds:
            mask &= np.isin(self.ind_name, list(inds))
        exs = set(spec.get("exchanges") or [])
        if exs:
            mask &= np.isin(self.exchange, list(exs))
        if spec.get("exclude_st"):
            # load_clean 已剔除当前 ST，此处保留接口
            pass
        return mask, None

    # ================================================================ 统计
    def stats(self, mask, hold=20):
        """完整统计：组合净值（含成本）/ 交易级 / 超额 / 分期 / 逐年 / 逐日序列"""
        mask = np.asarray(mask, bool)
        n = self.C["n"]
        if mask.sum() < 50:
            return None

        # --- 交易级（T+1 开盘买，T+1+H 开盘卖）
        r, ed, pos = L.simulate_hold(mask, self.buy_open, self.sell_open, self.C, hold)
        ts = L.trade_stats(r, cost=RT_COST) if len(r) else None

        # --- 日度再平衡组合（含成本摊薄）
        net, cnt = L.portfolio_nav(mask, self.D, self.nd, self.oret_sig,
                                   np.full(n, float(hold)), self.C)
        st = L.ann_stats(net, self.nd)
        act = cnt > 0
        st["avg_holdings"] = float(cnt[act].mean()) if act.any() else 0.0
        st["n_active_day"] = int(act.sum())
        st["active_pct"] = float(act.mean())

        # --- 毛收益状态组合（无成本，与报告第 1~10 节可比）
        pst, gross, _ = L.state_nav(mask, self.oret_sig, self.C)

        # --- 日加权超额（多持有期）
        exw = {}
        for H in [1, 5, 10, 20, 40, 60]:
            res = L.exw_t(self.FWD[H][mask], self.CHAIN[H][self.D][mask], self.D[mask])
            exw[H] = res

        # --- 逐年
        yrs = self.years
        ytab = []
        for y in range(2015, 2027):
            s = yrs == y
            if s.sum() < 20:
                continue
            nav = np.cumprod(1.0 + net[s])
            ytab.append(dict(year=int(y), ret=float(nav[-1] - 1.0),
                             mdd=float((nav / np.maximum.accumulate(nav) - 1.0).min())))

        # --- 四段分期
        SEGS = [("2015-2020", 2015, 2020), ("2021-2022", 2021, 2022),
                ("2023-2024", 2023, 2024), ("2025-2026", 2025, 2026)]
        segs = {}
        for sn, y0, y1 in SEGS:
            s = (yrs >= y0) & (yrs <= y1)
            segs[sn] = float(np.prod(1.0 + net[s]) - 1.0) if s.sum() >= 20 else None
        segs["IS(2015-2020)"] = float(np.prod(1.0 + net[yrs <= 2020]) - 1.0)
        segs["OOS(2021-2026)"] = float(np.prod(1.0 + net[yrs >= 2021]) - 1.0)

        # --- 净值曲线（按日）
        nav_impl = np.cumprod(1.0 + net)
        nav_gross = np.cumprod(1.0 + gross)

        return dict(
            n_signal=int(mask.sum()), n_trade=int(ts["n"]) if ts else 0,
            cagr=st["cagr"], mdd=st["mdd"], sharpe=st["sharpe"],
            ann_arith=st["ann_arith"], vol=st["vol"],
            gross_cagr=pst["cagr"], gross_mdd=pst["mdd"], gross_sharpe=pst["sharpe"],
            win=ts["win"] if ts else None, payoff=ts["payoff"] if ts else None,
            pf=ts["pf"] if ts else None, mean=ts["mean"] if ts else None,
            median=ts["median"] if ts else None,
            p10=ts["p10"] if ts else None, p90=ts["p90"] if ts else None,
            avg_holdings=st["avg_holdings"], active_pct=st["active_pct"],
            exw={H: (exw[H]["exw"] if exw[H] else None) for H in exw},
            exw_t={H: (exw[H]["t"] if exw[H] else None) for H in exw},
            yearly=ytab, segs=segs,
            nav=nav_impl, nav_gross=nav_gross, net=net, cnt=cnt,
        )

    # ================================================================ 扫描
    def scan(self, pool_mask=None, limit=200, p=None, lookback=180):
        """扫描**最近一个有信号的交易日**，输出符合条件的个股明细 + 打分。

        ⚠️ 设计要点：熊市条件（市场净值 < MA60）会让「最新交易日」经常 0 结果
           （A股大部分时间处于净值 MA60 上方）。此时**自动回溯**最近一个有信号的
           交易日，并在返回里标明 `asof` 与 `fallback_days`，让用户知道看的是哪一天。
        """
        D = self.D
        n = self.C["n"]
        # 条件掩码
        cond = None
        if p:
            cond, _ = self.build_mask(p, None)
        if pool_mask is not None:
            cond = pool_mask if cond is None else (cond & pool_mask)
        if cond is None:
            cond = np.ones(n, bool)

        # 找最后一个「有行情」的交易日
        last = self.nd - 1
        while last > 0:
            if (D == last).sum() > 100:
                break
            last -= 1

        # 从 last 起向前回溯，找第一个有信号的交易日
        asof, fb = last, 0
        for back in range(0, min(lookback, last) + 1):
            d = last - back
            if (cond & (D == d)).sum() > 0:
                asof, fb = d, back
                break

        m = cond & (D == asof)
        idx = np.flatnonzero(m)
        if len(idx) == 0:
            return dict(date=str(self.day_str[last]), n=0, shown=0, items=[],
                        asof=str(self.day_str[last]), fallback_days=0,
                        latest_date=str(self.day_str[last]),
                        latest_n=0,
                        mkt_bull=bool(self.mkt_bull[last]),
                        mkt_dist=self._f(self.mkt_dist60[last] * 100, 2),
                        breadth=self._f(self.breadth[last] * 100, 1),
                        hv20=self._f(self.mhv20[last] * 100, 1))

        # ---- 打分：距MA60 越深 + 超跌越深 + 市值越小 → 分越高
        # ⚠️ 分位是整数（1~10），单纯加权会产生大量并列（实测 D1/D1/D0 全部 130 分）。
        #    因此加入**连续的原始值**作为决胜项，让排序真正有区分度。
        bm = self.B["px_ma60"][idx]
        b40 = self.B["ret40"][idx]
        sg = self.size_grp[idx]
        raw60 = self.df["px_ma60_pct"].values[idx]      # 连续，负得越多越好
        raw40 = self.df["ret40"].values[idx]           # 连续
        score = np.zeros(len(idx))
        score += np.where(np.isfinite(bm), (11.0 - bm) * 6.0, 0.0)
        score += np.where(np.isfinite(b40), (11.0 - b40) * 3.0, 0.0)
        score += np.where(np.isfinite(sg), (10.0 - sg) * 4.0, 0.0)
        # 决胜：连续原始值（缩放到 0~10 量级，不改变主排序方向）
        score += np.where(np.isfinite(raw60), -np.clip(raw60, -1, 1) * 10.0, 0.0)
        score += np.where(np.isfinite(raw40), -np.clip(raw40, -1, 1) * 5.0, 0.0)
        order = np.argsort(-score)
        idx = idx[order]
        score = score[order]

        items = []
        dist60v = self.df["px_ma60_pct"].values
        dist20v = self.df["px_ma20_pct"].values
        ret20v = self.df["ret20"].values
        ret60v = self.df["ret60"].values
        hv20v = self.df["hv20"].values
        rvolv = self.df["rvol20"].values
        for k, i in enumerate(idx[:limit]):
            items.append(dict(
                code=str(self.code[i]), name=str(self.stk_name[i]),
                ind=str(self.ind_name[i]), ex=str(self.exchange[i]),
                close=self._f(self.cl[i], 2),
                dist60=self._f(dist60v[i] * 100, 2) if np.isfinite(dist60v[i]) else None,
                dist20=self._f(dist20v[i] * 100, 2) if np.isfinite(dist20v[i]) else None,
                ret20=self._f(ret20v[i] * 100, 2) if np.isfinite(ret20v[i]) else None,
                ret60=self._f(ret60v[i] * 100, 2) if np.isfinite(ret60v[i]) else None,
                d_px60=self._i(self.B["px_ma60"][i]),
                d_ret40=self._i(self.B["ret40"][i]),
                d_ret60=self._i(self.B["ret60"][i]),
                d_ma120=self._i(self.B["px_ma120"][i]),
                size_grp=self._i(self.size_grp[i]),
                hv20=self._f(hv20v[i] * 100, 1) if np.isfinite(hv20v[i]) else None,
                rvol=self._f(rvolv[i], 2) if np.isfinite(rvolv[i]) else None,
                score=round(float(score[k]), 1),
                can_buy=bool(self.can_buy[i]),
            ))

        mkt_ok = bool(self.mkt_bull[last]) if last < self.nd else False
        latest_n = int((cond & (D == last)).sum())
        return dict(
            date=str(self.day_str[asof]),
            asof=str(self.day_str[asof]),
            latest_date=str(self.day_str[last]),
            fallback_days=int(fb),
            latest_n=latest_n,
            n=int(m.sum()),
            shown=len(items),
            mkt_bull=bool(self.mkt_bull[asof]),
            mkt_dist=self._f(self.mkt_dist60[asof] * 100, 2),
            breadth=self._f(self.breadth[asof] * 100, 1),
            hv20=self._f(self.mhv20[asof] * 100, 1),
            mkt_bull_now=mkt_ok,
            items=items,
        )

    # ================================================================ 单股透视
    def stock_detail(self, code):
        code = code.strip().upper()
        if code.isdigit() and len(code) == 6:
            cand = [code + ".SH", code + ".SZ"]
            hit = [c for c in cand if (self.code == c).any()]
            code = hit[0] if hit else code
        s = np.flatnonzero(self.code == code)
        if len(s) == 0:
            return None
        # 取最近 250 个交易日
        s = s[-250:]
        out = dict(
            code=code, name=str(self.stk_name[s[-1]]), ind=str(self.ind_name[s[-1]]),
            rows=[dict(
                date=str(self.day_str[self.D[i]]),
                close=round(float(self.cl[i]), 2),
                ma20=round(float(self.ma20[i]), 2) if np.isfinite(self.ma20[i]) else None,
                ma60=round(float(self.cl[i] / (1 + self.df["px_ma60_pct"].values[i])), 2)
                if np.isfinite(self.df["px_ma60_pct"].values[i])
                and (1 + self.df["px_ma60_pct"].values[i]) > 0 else None,
                dist60=round(float(self.df["px_ma60_pct"].values[i]) * 100, 2)
                if np.isfinite(self.df["px_ma60_pct"].values[i]) else None,
                ret20=round(float(self.df["ret20"].values[i]) * 100, 2)
                if np.isfinite(self.df["ret20"].values[i]) else None,
                rvol=round(float(self.df["rvol20"].values[i]), 2)
                if np.isfinite(self.df["rvol20"].values[i]) else None,
                hist=round(float(self.df["hist"].values[i]), 4)
                if np.isfinite(self.df["hist"].values[i]) else None,
                size_grp=self._i(self.size_grp[i]),
            ) for i in s],
        )
        # 该股历史上触发 K3 默认条件的次数与后续收益
        try:
            c, _ = self.build_mask(DEFAULT_PARAMS, None)
            hit = np.flatnonzero(c & (self.code == code))
            fut = []
            for i in hit:
                d = {H: (round(float(self.FWD[H][i]) * 100, 2)
                         if np.isfinite(self.FWD[H][i]) else None) for H in [5, 20, 60]}
                fut.append(dict(date=str(self.day_str[self.D[i]]),
                                dist60=round(float(self.df["px_ma60_pct"].values[i]) * 100, 2),
                                fwd5=d[5], fwd20=d[20], fwd60=d[60]))
            out["signal_dates"] = fut
        except Exception:
            out["signal_dates"] = []
        return out

    @staticmethod
    def _f(x, nd=2):
        try:
            v = float(x)
            return round(v, nd) if np.isfinite(v) else None
        except Exception:
            return None

    @staticmethod
    def _i(x):
        try:
            v = float(x)
            return int(v) if np.isfinite(v) else None
        except Exception:
            return None

    # ================================================================ 元信息
    def meta(self):
        # 股票清单（最新交易日）
        last = self.nd - 1
        m = self.D == last
        idx = np.flatnonzero(m)
        stocks = [dict(code=str(self.code[i]), name=str(self.stk_name[i]),
                       ind=str(self.ind_name[i]), ex=str(self.exchange[i]))
                  for i in idx]
        stocks.sort(key=lambda x: x["code"])
        return dict(
            n_rows=int(self.C["n"]), n_stocks=int(len(self.C["starts"])),
            n_days=int(self.nd),
            date_start=str(self.day_str[0]), date_end=str(self.day_str[-1]),
            last_date=str(self.day_str[last]),
            build_ms=self.build_ms,
            industries=self.industries,
            n_industry_stocks=len(stocks),
            stocks=stocks,
        )


# ================================================================ 默认参数（K3 最优）
DEFAULT_PARAMS = {
    "px_ma60_min": 1, "px_ma60_max": 1,   # 距MA60 D1
    "size_max": 2,                        # 最小 30%
    "mkt_state": "bear",                  # 市场净值 < MA60
    "mkt_hv": "any",
    "deep_any": False,
    "confirm": [],
    "hold": 20,
}


if __name__ == "__main__":
    t = time.time()
    e = Engine()
    print(f"build {e.build_ms} ms   total {int((time.time()-t)*1000)} ms")
    m = e.meta()
    print({k: v for k, v in m.items() if k != "stocks"})
    mk, conds = e.build_mask(DEFAULT_PARAMS)
    print("K3 条件:", conds, " 信号数:", int(mk.sum()))
    st = e.stats(mk, 20)
    print(f"CAGR {st['cagr']*100:.1f}%  MDD {st['mdd']*100:.1f}%  "
          f"Sharpe {st['sharpe']:.2f}  胜率 {st['win']*100:.1f}%  PF {st['pf']:.2f}")
