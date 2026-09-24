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

    # ------------------------------------------------------------ 板块兜底
    def _board_from_code(self):
        """按证券代码号段推断板块（面板无 board 列时的兜底）。

        号段规则（经全样本实证 + limit_pct 交叉验证）：
          沪市主板 600/601/603/605      深市主板 000/001/002/003/302
          创业板   300/301              科创板   688/689
          北交所   920 / 8xxxxx / 4xxxxx（其余兜底）
        ⚠️ 302 属深市主板而非创业板 —— 已用 limit_pct 验证（无 20% 涨停记录），
           例如 302132.SZ 中航成飞。
        """
        c3 = np.char.slice(np.asarray(self.code, dtype=str), 0, 3)
        return np.select(
            [np.isin(c3, ["600", "601", "603", "605"]),
             np.isin(c3, ["000", "001", "002", "003", "302"]),
             np.isin(c3, ["300", "301"]),
             np.isin(c3, ["688", "689"])],
            ["MAIN", "MAIN", "CHINEXT", "STAR"], default="BJ")

    # ------------------------------------------------------------ 原始面板
    def _load(self):
        cols = [
            "thscode", "date", "name", "is_st_now", "days_since_list", "ret",
            "open_price", "close_price", "high_price", "low_price",
            "can_buy_open", "can_sell_open", "size_grp", "limit_pct",
            "ind_code", "ind_name",
            # ⚠️ board = 板块（MAIN/CHINEXT/STAR/BJ），由数据源直接给出
            "board",
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
        # ⚠️ 优先用面板自带的 exchange 列（数据源权威），缺失时按代码后缀兜底
        if "exchange" in df.columns:
            self.exchange = np.asarray(df["exchange"].fillna("").values, dtype=str)
            self.exchange[self.exchange == ""] = np.where(
                np.char.endswith(self.code, ".SH"), "SH",
                np.where(np.char.endswith(self.code, ".BJ"), "BJ", "SZ"))[self.exchange == ""]
        else:
            self.exchange = np.where(np.char.endswith(self.code, ".SH"), "SH",
                                     np.where(np.char.endswith(self.code, ".BJ"), "BJ", "SZ"))

        # ---- 板块（MAIN/CHINEXT/STAR/BJ）
        # ⚠️ 优先用面板自带的 board 列 —— 这是数据源直接给出的官方分类，
        #    比按代码号段猜更可靠。已用 limit_pct（涨跌停幅度）交叉验证：
        #      MAIN    → 10%（沪深主板）
        #      CHINEXT → 20%（创业板）/ STAR → 20%（科创板）
        #      BJ      → 30%（北交所）
        #    自检手法：若某板块出现 20% 涨停却不是创业板/科创板，说明分类错了。
        #    兜底（面板无 board 列时）按号段推断：
        #      600/601/603/605 → 沪市主板；000/001/002/003/302 → 深市主板
        #      300/301 → 创业板；688/689 → 科创板；其余 → 北交所
        if "board" in df.columns:
            bd = np.char.upper(np.asarray(df["board"].fillna("").values, dtype=str))
            # 容忍不同数据源的命名风格，统一到 MAIN/CHINEXT/STAR/BJ
            bd = np.select(
                [np.isin(bd, ["MAIN", "MAIN_SH", "MAIN_SZ", "主板", "沪市主板", "深市主板"]),
                 np.isin(bd, ["CHINEXT", "GEM", "创业板"]),
                 np.isin(bd, ["STAR", "STAR_MARKET", "科创板"]),
                 np.isin(bd, ["BJ", "BSE", "北交所"])],
                ["MAIN", "CHINEXT", "STAR", "BJ"], default="")
            fb = self._board_from_code()
            self.board = np.where(bd == "", fb, bd)
        else:
            self.board = self._board_from_code()
        self.boards = ["MAIN", "CHINEXT", "STAR", "BJ"]

        # 股票池的行业清单
        self.industries = sorted(set(self.ind_name.tolist()))

        # ---- 逐日市场状态（供扫描时快速取用）
        self.day_mkt_bull = self.mkt_bull
        self.day_mkt_hv = self.mhv20
        self.day_breadth = self.breadth

        # ---- 交易日字符串
        self.day_str = np.array([str(x)[:10] for x in self.ud])
        self.years = pd.DatetimeIndex(self.ud).year.values

        # ---- 财务数据（as-of 动态对齐，无前视偏差）
        self._build_financials()

    # ------------------------------------------------------------ 财务 as-of
    def _build_financials(self):
        """把财务面板对齐到每个「股票×交易日」单元格。

        ⚠️ 核心纪律：交易日 T 只能使用 `publish_date <= T` 的最新一期财报，
           否则就是前视偏差（用未来才知道的财报选过去的股票）。

        做法：
          1. 对每只股票，按 publish_date 升序排列财报期；
          2. 对每个交易日 T，用 searchsorted 找「披露日 <= T」的最后一期；
          3. 把该期的营收/归母净利润/同比增长率写到该单元格。

        产出（与面板行一一对应的 numpy 数组）：
          self.fin_rev     营业总收入（累计 YTD，元，NaN=当时无数据）
          self.fin_np      归母净利润（累计 YTD，元）
          self.fin_rev_yoy 营收同比增长率（小数，如 0.25 = +25%）
          self.fin_np_yoy  归母净利润同比增长率
          self.fin_pe      营收口径的估值代理（总市值/营收TTM，越小越便宜）
          self.fin_asof_q  该单元格生效的报告期编号（0=无）
          self.fin_asof_d  该单元格生效的报告期末的「日序号」（面板 D 口径）
        """
        n = self.C["n"]
        fp = os.path.join(ROOT, "data", "processed", "fin_panel.parquet")
        Z = lambda: (np.full(n, np.nan, np.float64), np.zeros(n, np.int16),
                     np.full(n, -1, np.int32))
        if not os.path.exists(fp):
            self.fin_rev, self.fin_np = np.full(n, np.nan), np.full(n, np.nan)
            self.fin_rev_yoy, self.fin_np_yoy = np.full(n, np.nan), np.full(n, np.nan)
            self.fin_asof_q = np.zeros(n, np.int16)
            self.fin_asof_d = np.full(n, -1, np.int32)
            self.fin_avail = False
            self.fin_n_stocks = 0
            self.fin_n_periods = 0
            self.fin_date_start = self.fin_date_end = None
            self.fin_pub_start = self.fin_pub_end = None
            self.fin_actual_pct = 0.0
            print("[engine] ⚠️ 未找到 fin_panel.parquet，财务筛选不可用", flush=True)
            return

        F = pd.read_parquet(fp)
        F["report_end"] = pd.to_datetime(F["report_end"])
        F["publish_date"] = pd.to_datetime(F["publish_date"])
        F = F.dropna(subset=["publish_date", "report_end"])

        # 交易日（面板 ud 为 datetime64）→ 用于 searchsorted 的 int64 日
        day_int = pd.DatetimeIndex(self.ud).values.astype("datetime64[D]").astype(np.int64)

        # 同比：同一财年、同一 q 的上一年值
        F = F.sort_values(["thscode", "report_end"]).reset_index(drop=True)
        F["prev_rev"] = F.groupby(["thscode", "q"])["operating_income"].shift(1)
        F["prev_np"] = F.groupby(["thscode", "q"])["parent_holder_net_profit"].shift(1)
        # 只在「上一年」时计算同比（避免跨年错配）
        F["prev_y"] = F.groupby(["thscode", "q"])["report_end"].shift(1)
        ok = F["prev_y"].notna() & (
            (F["report_end"].dt.year - F["prev_y"].dt.year) == 1)
        F["rev_yoy"] = np.where(
            ok & (F["prev_rev"].abs() > 1.0),
            (F["operating_income"] - F["prev_rev"]) / F["prev_rev"].abs(), np.nan)
        F["np_yoy"] = np.where(
            ok & (F["prev_np"].abs() > 1.0),
            (F["parent_holder_net_profit"] - F["prev_np"]) / F["prev_np"].abs(), np.nan)

        # 面板的股票顺序 → code
        code = self.code
        rev = np.full(n, np.nan); npr = np.full(n, np.nan)
        ryy = np.full(n, np.nan); nyy = np.full(n, np.nan)
        aq = np.zeros(n, np.int16); ad = np.full(n, -1, np.int32)

        pub_d = F["publish_date"].values.astype("datetime64[D]").astype(np.int64)
        re_d = F["report_end"].values.astype("datetime64[D]").astype(np.int64)
        Fg = F.groupby("thscode", sort=False).indices
        for s_i, (st, en) in enumerate(zip(self.C["starts"], self.C["ends"])):
            c = code[st]                            # 该股票 thscode
            gidx = Fg.get(c)
            if gidx is None:
                continue
            en = int(en)
            rows = np.arange(st, en)
            pdv = pub_d[gidx]                       # 该股各期披露日（升序）
            tday = day_int[self.D[rows]]            # 各交易日的日序号
            # searchsorted: 找 publish_date <= T 的最后一期
            pos = np.searchsorted(pdv, tday, side="right") - 1
            hit = pos >= 0
            if not hit.any():
                continue
            sel = gidx[pos[hit]]                    # 命中的财报行号
            rev[rows[hit]] = F["operating_income"].values[sel]
            npr[rows[hit]] = F["parent_holder_net_profit"].values[sel]
            ryy[rows[hit]] = F["rev_yoy"].values[sel]
            nyy[rows[hit]] = F["np_yoy"].values[sel]
            aq[rows[hit]] = F["q"].values[sel].astype(np.int16)
            # 报告期末 → 日序号（用于展示「财报期」）
            ad[rows[hit]] = np.searchsorted(day_int, re_d[sel], side="right") - 1

        self.fin_rev = rev
        self.fin_np = npr
        self.fin_rev_yoy = ryy
        self.fin_np_yoy = nyy
        self.fin_asof_q = aq
        self.fin_asof_d = ad
        self.fin_avail = True
        # 财务面板的真实规模（供 /api/meta 展示）
        self.fin_n_stocks = int(F["thscode"].nunique())
        self.fin_n_periods = int(len(F))
        self.fin_date_start = str(F["report_end"].min().date())
        self.fin_date_end = str(F["report_end"].max().date())
        self.fin_pub_start = str(F["publish_date"].min().date())
        self.fin_pub_end = str(F["publish_date"].max().date())
        self.fin_actual_pct = float((F["src"] == "actual").mean())

        cov = float(np.isfinite(rev).mean())
        print(f"[engine] 财务对齐完成：覆盖率 {cov:.1%}，"
              f"有效期数 {int(np.isfinite(rev).sum()):,}", flush=True)

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

        # ---- 财务条件（动态 as-of，无前视偏差）
        # rev_min/rev_max        : 营业总收入（元）下限/上限
        # rev_yoy_min/rev_yoy_max: 营收同比增长率（小数，0.2 = +20%）
        # np_min/np_max          : 归母净利润（元）
        # np_yoy_min/np_yoy_max  : 归母净利润同比增长率
        # fin_period             : "latest" | "q1" | "q2" | "q3" | "q4"
        if self.fin_avail:
            def gv(k):
                v = p.get(k)
                return None if v in (None, "") else float(v)

            fp_sel = p.get("fin_period", "latest")
            if fp_sel and fp_sel != "latest":
                want = {"q1": 1, "q2": 2, "q3": 3, "q4": 4}.get(str(fp_sel).lower())
                if want:
                    parts[f"财报期Q{want}"] = self.fin_asof_q == want

            rmin, rmax = gv("rev_min"), gv("rev_max")
            if rmin is not None or rmax is not None:
                ok = np.isfinite(self.fin_rev)
                if rmin is not None:
                    ok = ok & (self.fin_rev >= rmin)
                if rmax is not None:
                    ok = ok & (self.fin_rev <= rmax)
                parts["营收区间"] = ok

            nmin, nmax = gv("np_min"), gv("np_max")
            if nmin is not None or nmax is not None:
                ok = np.isfinite(self.fin_np)
                if nmin is not None:
                    ok = ok & (self.fin_np >= nmin)
                if nmax is not None:
                    ok = ok & (self.fin_np <= nmax)
                parts["归母净利区间"] = ok

            rymin, rymax = gv("rev_yoy_min"), gv("rev_yoy_max")
            if rymin is not None or rymax is not None:
                ok = np.isfinite(self.fin_rev_yoy)
                if rymin is not None:
                    ok = ok & (self.fin_rev_yoy >= rymin)
                if rymax is not None:
                    ok = ok & (self.fin_rev_yoy <= rymax)
                parts["营收同比"] = ok

            nymin, nymax = gv("np_yoy_min"), gv("np_yoy_max")
            if nymin is not None or nymax is not None:
                ok = np.isfinite(self.fin_np_yoy)
                if nymin is not None:
                    ok = ok & (self.fin_np_yoy >= nymin)
                if nymax is not None:
                    ok = ok & (self.fin_np_yoy <= nymax)
                parts["归母同比"] = ok

            # 财务条件在这里才与主掩码做与运算（必须在 parts 更新之后）
            for k, v in parts.items():
                m &= np.asarray(v, bool)

        # 股票池
        if pool is not None:
            m &= pool

        return m, list(parts.keys())

    # ================================================================ 股票池
    def build_pool(self, spec):
        """spec = {mode, codes:[], codes_text:'', industries:[], exchanges:[],
                   boards:[], exclude_st:bool}

        boards 取值（可多选）：
          'MAIN'    主板（沪深主板，数据源 original 分类）
          'CHINEXT' 创业板 / 'STAR' 科创板 / 'BJ' 北交所
        大小写不敏感（会自动 upper），未知取值直接忽略。
        """
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
        # ---- 板块筛选（大小写不敏感；空列表 = 不限）
        bds = [str(x).strip().upper() for x in (spec.get("boards") or []) if str(x).strip()]
        if bds:
            valid = [b for b in bds if b in self.boards]
            if not valid:
                return None, f"板块筛选无有效取值（{bds}），可选：{self.boards}"
            mask &= np.isin(self.board, valid)
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

    # ================================================================ 交易明细
    def trades(self, mask, hold=20, cost=RT_COST, include_fin=True):
        """从掩码提取**逐笔交易明细**（与 stats() 完全同源，口径一致）。

        交易口径（固定持有期，可复现）：
          信号日 T  →  T+1 开盘买入  →  持有 H 个交易日  →  T+1+H 开盘卖出

        实现要点
        --------
        1. 直接复用 `L.simulate_hold`，保证与 `stats()` 的 `n_trade`/胜率/PF 逐位一致；
        2. `simulate_hold` 返回的 `pos` 是**面板行号**（信号日所在行），
           因此：信号日 = day_idx[pos]，入场日 = 信号日 + 1，出场日 = 信号日 + 1 + H；
        3. 买卖价从 `shift_block(buy_open/sell_open, k)` 取，与 simulate_hold 同源；
        4. 基准（同口径市场收益）用 `CHAIN[H][入场日]`，即「T+1 开盘 → T+1+H 开盘」
           的市场链式收益，与个股 trade_ret 完全可比。

        返回
        ----
        dict(
          rows     : list[dict]  逐笔明细（未排序，按出场日升序）
          summary  : dict        全局汇总（含分年、盈亏分布、最佳/最差）
          yearly   : list[dict]  逐年交易统计
          hist     : dict        收益分布直方图（分箱）
          n_trade  : int
        )

        ⚠️⚠️ 单位约定 —— 本接口**故意混用两套**，改动前务必看清：
          · rows[*] / best[*] / worst[*] ：已 **×100**，值是百分数
              （`net=8.53` 读作 **+8.53%**）。前端用 `PCTN()` 直接加 % 号。
          · summary / yearly / hist.edges ：也是百分数（`mean=0.0452` 是**比率**，
              `best=5.3076` 是 **530.76%**）。
              → 即 summary 里 best/worst 是**比率**，而 best[] 里 net 是**百分数**，
                即使名字相同也不同单位。前端 summary/yearly 用 `PCT()`（×100），
                best[]/worst[]/rows[] 用 `PCTN()`（不乘）。
          · 历史教训：曾把 `rows[*].net`（8.53）用 `PCT()` 渲染成 **853.00%**。
            核实方法：`rows[0].buy/sell` 手算 `sell/buy-1`，与 `net/100` 对照。
        """
        mask = np.asarray(mask, bool)
        if mask.sum() == 0:
            return None
        r, ed, pos = L.simulate_hold(mask, self.buy_open, self.sell_open, self.C, hold)
        if len(r) == 0:
            return None

        # ---- 买卖价（与 simulate_hold 内部完全同源）
        bi_all = L.shift_block(self.buy_open, self.C, 1)
        si_all = L.shift_block(self.sell_open, self.C, 1 + hold)
        bi = bi_all[pos]
        si = si_all[pos]

        sig_d = self.D[pos]                       # 信号日序号
        ent_d = sig_d + 1                         # 入场日序号（T+1）
        ext_d = sig_d + 1 + hold                  # 出场日序号（T+1+H）

        # ---- 基准：同口径市场链式收益（T+1 开盘 → T+1+H 开盘）
        exc = np.full(len(r), np.nan)
        ch = self.CHAIN.get(hold)
        if ch is not None:
            valid = ent_d < self.nd
            mk = np.full(len(r), np.nan)
            mk[valid] = ch[ent_d[valid]]
            ok = np.isfinite(mk)
            exc[ok] = r[ok] - mk[ok]
        mkt = np.full(len(r), np.nan)
        if ch is not None:
            valid = ent_d < self.nd
            mkt[valid] = ch[ent_d[valid]]

        net = r - cost                            # 扣完成本的单笔收益
        pnl = net > 0
        years = self.years[np.clip(ent_d, 0, self.nd - 1)]

        # ---- 逐笔明细
        rows = []
        fin_ok = bool(getattr(self, "fin_avail", False)) and include_fin
        for i in range(len(r)):
            p_i = pos[i]
            s_i = sig_d[i]
            # 信号日的财务 as-of 快照（回测时点「当时已知」的财报）
            fr = fy = nr = ny = None
            fq = 0
            fend = None
            if fin_ok:
                fq = int(self.fin_asof_q[p_i])
                if np.isfinite(self.fin_rev[p_i]):
                    fr = int(round(float(self.fin_rev[p_i])))
                if np.isfinite(self.fin_rev_yoy[p_i]):
                    fy = round(float(self.fin_rev_yoy[p_i]) * 100, 2)
                if np.isfinite(self.fin_np[p_i]):
                    nr = int(round(float(self.fin_np[p_i])))
                if np.isfinite(self.fin_np_yoy[p_i]):
                    ny = round(float(self.fin_np_yoy[p_i]) * 100, 2)
                if self.fin_asof_d[p_i] >= 0:
                    fend = str(self.day_str[self.fin_asof_d[p_i]])
            rows.append(dict(
                seq=i + 1,
                code=str(self.code[p_i]), name=str(self.stk_name[p_i]),
                ind=str(self.ind_name[p_i]), ex=str(self.exchange[p_i]),
                board=str(self.board[p_i]),
                signal_date=str(self.day_str[s_i]),
                entry_date=str(self.day_str[ent_d[i]]) if ent_d[i] < self.nd else None,
                exit_date=str(self.day_str[ext_d[i]]) if ext_d[i] < self.nd else None,
                buy=round(float(bi[i]), 3) if np.isfinite(bi[i]) else None,
                sell=round(float(si[i]), 3) if np.isfinite(si[i]) else None,
                hold=hold,
                ret=round(float(r[i]) * 100, 2),
                net=round(float(net[i]) * 100, 2),
                bench=(round(float(mkt[i]) * 100, 2) if np.isfinite(mkt[i]) else None),
                excess=(round(float(exc[i]) * 100, 2) if np.isfinite(exc[i]) else None),
                win=bool(pnl[i]),
                size_grp=self._i(self.size_grp[p_i]),
                d_px60=self._i(self.B["px_ma60"][p_i]),
                d_ret60=self._i(self.B["ret60"][p_i]),
                fin_rev=fr, fin_rev_yoy=fy, fin_np=nr, fin_np_yoy=ny,
                fin_q=fq, fin_end=fend,
            ))

        # ---- 全局汇总
        srt = np.argsort(ext_d, kind="stable")
        rows_sorted = [rows[i] for i in srt]

        wins = net[pnl]
        loss = net[~pnl]
        tot_win = float(wins.sum()) if len(wins) else 0.0
        tot_loss = float(-loss.sum()) if len(loss) else 0.0
        pf = (tot_win / tot_loss) if tot_loss > 0 else None
        payoff = (float(wins.mean() / (-loss.mean()))
                  if len(wins) and len(loss) and loss.mean() < 0 else None)
        # 盈亏分布（等宽分箱，覆盖 −60% ~ +120%）
        edges = [-1.0, -0.5, -0.3, -0.2, -0.1, -0.05, 0.0,
                 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0]
        cnt, _ = np.histogram(net, bins=edges)
        hist = dict(edges=[round(float(e) * 100, 1) for e in edges],
                    counts=[int(c) for c in cnt])
        # 单笔持有天数内的年化（供参考；H=20 时换算为年化）
        summary = dict(
            n_trade=int(len(net)),
            hold=int(hold),
            cost=float(cost),
            win=float(pnl.mean()),
            mean=float(net.mean()),
            median=float(np.median(net)),
            std=float(net.std(ddof=1)) if len(net) > 1 else None,
            best=float(net.max()), worst=float(net.min()),
            p10=float(np.percentile(net, 10)),
            p25=float(np.percentile(net, 25)),
            p75=float(np.percentile(net, 75)),
            p90=float(np.percentile(net, 90)),
            pf=pf, payoff=payoff,
            avg_excess=(float(np.nanmean(exc)) if np.isfinite(exc).any() else None),
            excess_win=(float((exc > 0).mean()) if np.isfinite(exc).any() else None),
            avg_ret=float(r.mean()),
            date_start=str(self.day_str[int(ent_d.min())]) if len(ent_d) else None,
            date_end=str(self.day_str[int(min(ext_d.max(), self.nd - 1))]),
            n_stock=int(len(set(self.code[pos].tolist()))),
        )

        # ---- 逐年交易统计
        yearly = []
        for y in range(2015, 2027):
            s = years == y
            if s.sum() < 5:
                continue
            rr = net[s]
            w = rr > 0
            tl = float(-rr[~w].sum()) if (~w).any() else 0.0
            yearly.append(dict(
                year=int(y), n=int(s.sum()), n_win=int(w.sum()),
                win=float(w.mean()), mean=float(rr.mean()),
                median=float(np.median(rr)),
                sum=float(rr.sum()),
                pf=(float(rr[w].sum() / tl) if tl > 0 else None),
                best=float(rr.max()), worst=float(rr.min()),
            ))

        # ---- 最佳 / 最差 各 20 笔
        order = np.argsort(-net)
        def pick(idxs):
            out = []
            for i in idxs:
                out.append(dict(code=str(self.code[pos[i]]),
                                name=str(self.stk_name[pos[i]]),
                                signal_date=str(self.day_str[sig_d[i]]),
                                exit_date=(str(self.day_str[ext_d[i]])
                                           if ext_d[i] < self.nd else None),
                                net=round(float(net[i]) * 100, 2),
                                ret=round(float(r[i]) * 100, 2),
                                excess=(round(float(exc[i]) * 100, 2)
                                        if np.isfinite(exc[i]) else None)))
            return out
        best20 = pick(order[:20])
        worst20 = pick(order[-20:][::-1])

        return dict(n_trade=int(len(net)), rows=rows_sorted, summary=summary,
                    yearly=yearly, hist=hist,
                    best=best20, worst=worst20, hold=int(hold))

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
            # 财务字段（as-of 生效期）
            fq = int(self.fin_asof_q[i]) if self.fin_avail else 0
            fend = None
            if self.fin_avail and self.fin_asof_d[i] >= 0:
                fend = str(self.day_str[self.fin_asof_d[i]])
            items.append(dict(
                code=str(self.code[i]), name=str(self.stk_name[i]),
                ind=str(self.ind_name[i]), ex=str(self.exchange[i]),
                board=str(self.board[i]),
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
                # ---- 财务（动态）
                fin_rev=self._money(self.fin_rev[i]) if self.fin_avail else None,
                fin_np=self._money(self.fin_np[i]) if self.fin_avail else None,
                fin_rev_yoy=(round(float(self.fin_rev_yoy[i]) * 100, 2)
                             if self.fin_avail and np.isfinite(self.fin_rev_yoy[i]) else None),
                fin_np_yoy=(round(float(self.fin_np_yoy[i]) * 100, 2)
                            if self.fin_avail and np.isfinite(self.fin_np_yoy[i]) else None),
                fin_q=fq,
                fin_end=fend,
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
            board=str(self.board[s[-1]]), ex=str(self.exchange[s[-1]]),
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

    @staticmethod
    def _money(x):
        """金额 → 元，保留为可读整数（前端按 亿/万 格式化）。"""
        try:
            v = float(x)
            return int(round(v)) if np.isfinite(v) else None
        except Exception:
            return None

    # ================================================================ 元信息
    def meta(self):
        # 股票清单（最新交易日）
        last = self.nd - 1
        m = self.D == last
        idx = np.flatnonzero(m)
        stocks = [dict(code=str(self.code[i]), name=str(self.stk_name[i]),
                       ind=str(self.ind_name[i]), ex=str(self.exchange[i]),
                       board=str(self.board[i]))
                  for i in idx]
        stocks.sort(key=lambda x: x["code"])
        # 各板块股票数（供前端提示）
        _bc = {b: int((self.board == b).sum()) for b in self.boards}
        return dict(
            n_rows=int(self.C["n"]), n_stocks=int(len(self.C["starts"])),
            n_days=int(self.nd),
            date_start=str(self.day_str[0]), date_end=str(self.day_str[-1]),
            last_date=str(self.day_str[last]),
            build_ms=self.build_ms,
            industries=self.industries,
            boards=self.boards,
            board_names={"MAIN": "主板", "CHINEXT": "创业板", "STAR": "科创板",
                         "BJ": "北交所"},
            board_counts=_bc,
            n_industry_stocks=len(stocks),
            stocks=stocks,
            fin_avail=bool(getattr(self, "fin_avail", False)),
            fin_n_stocks=int(getattr(self, "fin_n_stocks", 0)),
            fin_n_periods=int(getattr(self, "fin_n_periods", 0)),
            fin_date_start=getattr(self, "fin_date_start", None),
            fin_date_end=getattr(self, "fin_date_end", None),
            fin_pub_start=getattr(self, "fin_pub_start", None),
            fin_pub_end=getattr(self, "fin_pub_end", None),
            fin_actual_pct=round(float(getattr(self, "fin_actual_pct", 0.0)), 4),
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
