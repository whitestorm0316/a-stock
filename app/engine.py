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

# ---- 退市风险过滤的常量（A 股交易规则）
# 面值退市：连续 20 个交易日收盘价均低于 1 元 → 终止上市。
# 必须用【未复权】价：前复权价被分红送转调低，用它判 <1 元会大量误判。
PENNY_PX = 1.0        # 面值线（元）
PENNY_WIN = 20        # 观察窗口（交易日）
# 默认要求窗口内「至少这么多天」低于面值线才剔除。
# 规则触发线是 20/20，但实测本面板内**从未出现**（0 行）——因为 v3b_lib.load_clean
# 已先行剔除了 ST 股与次新股，等到真跌破 1 元时往往已不在样本内。
# 故默认取 10/20 作为「已逼近面值退市」的预警口径，可调 1~20。
PENNY_DAYS_DEFAULT = 10
# 财务类退市风险（退市新规的组合指标）：最近一期财报
#   年化归母净利润 < 0  且  年化营业收入 < 阈值
# 阈值随规则变化：2020 退市新规 1 亿；2024-04 修订后主板 3 亿、双创 1 亿。
DELIST_REV_FLOOR = 1e8     # 默认营收阈值（元）
DELIST_REV_FLOOR_MAIN_2024 = 3e8   # 2024 新规主板阈值（元）


# ---- 行业两级归类（同花顺细分行业 → 证监会门类）
#  数据源只有同花顺行业指数(881xxx.TI)的 88 个细分行业，没有门类字段，
#  所以门类这一层是**人工归并**，配置在 app/industry_tree.json（该文件头部写明了局限）。
#  用 scripts/check_industry_tree.py 校验覆盖率，避免配置漂移导致行业静默消失。
TREE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "industry_tree.json")


def industry_tree(industries, ind_last=None):
    """把扁平的细分行业清单整理成「门类 → 细分行业」两级结构。

    参数
      industries  面板里实际存在的细分行业（扁平清单，来自 Engine.industries）
      ind_last    最新交易日每只股票的细分行业数组（用于统计各门类股票数，可省）

    返回 (groups, absent, unlisted, unlisted_n)
      groups   [{code, name, subs, n_sub, n_stock, notes}, ...]
               只保留 subs 非空的门类；subs 已按面板实际情况过滤
      absent   [{code, name, why}, ...] 本数据源无细分行业的门类（界面要如实说明）
      unlisted 面板里有、但配置未归类的细分行业 —— 兜底分组的依据，
               **绝不能静默丢弃**（配置漂移时用户会莫名其妙选不到某些行业）。
               实测面板里必然有「未知」（个股缺行业映射时的兜底值），属正常。
      unlisted_n  unlisted 在最新交易日的股票数合计（供界面显示量级）

    配置读不到时退化：全部行业进 unlisted，前端仍按单层列表渲染。
    """
    have = set(industries)
    try:
        with open(TREE_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"[engine] 行业归类配置读取失败（{e}），退化为单层行业列表")
        return [], [], sorted(have), 0

    # 各细分行业在最新交易日的股票数（用于门类计数）
    n_of = {}
    if ind_last is not None:
        names, counts = np.unique(np.asarray(ind_last, dtype=str),
                                  return_counts=True)
        n_of = dict(zip(names.tolist(), counts.tolist()))

    groups = []
    for g in cfg.get("groups") or []:
        # 只保留面板里真实存在的细分行业（配置可能领先/落后于数据）
        subs = [s for s in (g.get("subs") or []) if s in have]
        if not subs:
            continue
        n_stock = sum(n_of.get(s, 0) for s in subs) if ind_last is not None else None
        groups.append(dict(code=g.get("code"), name=g.get("name"), subs=subs,
                           n_sub=len(subs), n_stock=n_stock,
                           notes=g.get("notes") or []))

    claimed = {s for g in groups for s in g["subs"]}
    unlisted = sorted(have - claimed)
    unlisted_n = sum(n_of.get(s, 0) for s in unlisted)

    absent = [dict(code=a.get("code"), name=a.get("name"), why=a.get("why"))
              for a in (cfg.get("absent") or [])]
    return groups, absent, unlisted, unlisted_n


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
            # 未复权收盘价：面值退市（连续 20 日收盘 < 1 元）只能用原始价判断，
            # 前复权价被分红送转调低后会大幅误判（实测 0.016% vs 0.004%）
            "close_price_raw",
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
        # 行 → 股票序号（同一只票的所有行同值）。喂给 plan_positions 做
        # 「同一只票未平仓期间不得重复买入」的约束，见 v3b_lib.plan_positions。
        self.sid = np.repeat(np.arange(self.C["starts"].size, dtype=np.int64),
                             self.C["ends"] - self.C["starts"])

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
        # 未复权收盘价（面值退市判定专用；缺失时退化为 NaN，该项过滤自动失效）
        self.px_raw = (df["close_price_raw"].values.astype(np.float64)
                       if "close_price_raw" in df.columns
                       else np.full(C["n"], np.nan))
        # 面值退市观测：近 PENNY_WIN 日中，收盘价（未复权）低于面值线的天数占比
        # 1.0 = 窗口内天天低于面值（即规则定义的退市触发条件）
        if np.isfinite(self.px_raw).any():
            self.penny_ratio = L.roll_mean(
                (self.px_raw < PENNY_PX).astype(np.float64), C, PENNY_WIN)
        else:
            self.penny_ratio = np.zeros(C["n"])
        # ⚠️ 关于 ST / 风险警示：本面板【做不出】point-in-time 的判定。
        #   面板的 is_st_now 用【当前】股票名称判断（含未来信息），
        #   而 v3b_lib.load_clean 已经默认据此剔除了「截至今天仍是 ST」的股票
        #   （实测 204 只）。想按「当时是否 ST」过滤需要历史名称数据，这里没有。
        #   曾尝试用 limit_pct==5% 反推，实测不可行：40.4% 的行是 5%，
        #   因为 detect_limit_regime 在「120 日内从未触及涨跌停」时会兜底成 5%；
        #   改用「近60日 max|ret|<=5.2%」又会把 4942 只误判进来。两者都不可靠。
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
            self.fin_ann_rev = np.full(n, np.nan)
            self.fin_ann_np = np.full(n, np.nan)
            self.fin_np_ann = np.full(n, np.nan)
            self.fin_np_ann_prev = np.full(n, np.nan)
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

        # ---- 退市风险所需的口径
        # operating_income / parent_holder_net_profit 是【累计 YTD】，
        # 判「营收是否低于 1 亿」这类年度门槛必须先年化：× 4 / q
        F["ann_rev"] = F["operating_income"] * 4.0 / F["q"]
        F["ann_np"] = F["parent_holder_net_profit"] * 4.0 / F["q"]

        # 「上一个年报」的归母净利润 —— 用于判定连续两个年度亏损。
        # 只在年报(q==4)序列内部 shift，避免被季报打乱；非年报行随后 ffill。
        is_ann = (F["q"].values == 4)
        ann_pos = np.where(is_ann)[0]
        prev_ann_np = np.full(len(F), np.nan)
        if len(ann_pos):
            sub = pd.DataFrame({
                "thscode": F["thscode"].values[ann_pos],
                "np": F["parent_holder_net_profit"].values[ann_pos],
            })
            prev_ann_np[ann_pos] = sub.groupby("thscode", sort=False)["np"].shift(1).values
        F["prev_ann_np"] = prev_ann_np
        # 对非年报行，沿用「最近一个已披露年报」的当期/上期净利
        cur_ann = np.where(is_ann, F["parent_holder_net_profit"].values, np.nan)
        F["ann_np_latest"] = pd.Series(cur_ann).groupby(F["thscode"].values).ffill().values
        F["ann_np_prev"] = pd.Series(prev_ann_np).groupby(F["thscode"].values).ffill().values

        # 面板的股票顺序 → code
        code = self.code
        rev = np.full(n, np.nan); npr = np.full(n, np.nan)
        ryy = np.full(n, np.nan); nyy = np.full(n, np.nan)
        aq = np.zeros(n, np.int16); ad = np.full(n, -1, np.int32)
        # 退市风险专用：年化营收 / 年化归母净利 / 最近两个年度的年报净利
        arev = np.full(n, np.nan); anp = np.full(n, np.nan)
        anp_a = np.full(n, np.nan); anp_ap = np.full(n, np.nan)

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
            arev[rows[hit]] = F["ann_rev"].values[sel]
            anp[rows[hit]] = F["ann_np"].values[sel]
            anp_a[rows[hit]] = F["ann_np_latest"].values[sel]
            anp_ap[rows[hit]] = F["ann_np_prev"].values[sel]

        self.fin_rev = rev
        self.fin_np = npr
        self.fin_rev_yoy = ryy
        self.fin_np_yoy = nyy
        self.fin_asof_q = aq
        self.fin_asof_d = ad
        self.fin_ann_rev = arev
        self.fin_ann_np = anp
        self.fin_np_ann = anp_a          # 最近一个已披露年报的归母净利
        self.fin_np_ann_prev = anp_ap    # 再上一个年报的归母净利
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
          size_min/size_max : 市值分位区间（序号 0~9，0 = 最小10%，9 = 最大10%）
                              如 [0,1] = 最小10%~20%、[0,2] = 最小30%、[2,9] = 剔除最小20%
                              （size_min 缺省 0；只给 size_max 等价于旧的「市值上限」口径）
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

        # 市值区间：size_min / size_max 都是「分位序号」0~9（0 = 最小10%）
        #   · 只给 size_max      → 上界筛选（旧口径，向后兼容）
        #   · size_min > 0       → 同时卡下界，可选「10%~20%」这种区间
        #   ⚠️ 必须用 `is not None` 判断：0 是合法取值但为假值
        smin, smax = norm_size_band(p.get("size_min"), p.get("size_max"))
        if smin > 0 or smax < 9:
            parts[size_band_label(smin, smax)] = (
                np.isfinite(self.size_grp) &
                (self.size_grp >= smin) & (self.size_grp <= smax))

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

        # ---- 退市风险过滤
        # ⚠️ 纪律：四条判据**全部只用买入日 T 已经公开的信息**——
        #   财报取 publish_date <= T 的最新一期（沿用 _build_financials 的 as-of 对齐）；
        #   面值/ST 用当日及之前的行情与涨跌幅制度，不含任何未来数据。
        # 目的不是预测谁会退市，而是把「规则上已经踩到退市红线」的票剔除出候选池。
        dl = p.get("delist") or {}
        if isinstance(dl, dict) and any(bool(v) for v in dl.values()):
            dp = {}
            if dl.get("financial") and self.fin_avail:
                floor = float(p.get("delist_rev_floor") or DELIST_REV_FLOOR)
                thr = np.full(n, floor)
                if p.get("delist_main_2024"):
                    # 2024 退市新规：主板营收门槛 3 亿，创业板/科创板/北交所仍 1 亿
                    thr = np.where(self.board == "MAIN",
                                   DELIST_REV_FLOOR_MAIN_2024, floor)
                bad = (np.isfinite(self.fin_ann_np) & (self.fin_ann_np < 0) &
                       np.isfinite(self.fin_ann_rev) & (self.fin_ann_rev < thr))
                dp["非财务类退市风险"] = ~bad
            if dl.get("loss2y") and self.fin_avail:
                # 连续两个年度亏损（年报口径）
                bad = (np.isfinite(self.fin_np_ann) & (self.fin_np_ann < 0) &
                       np.isfinite(self.fin_np_ann_prev) & (self.fin_np_ann_prev < 0))
                dp["非连续两年亏损"] = ~bad
            if dl.get("penny"):
                days = int(p.get("delist_penny_days") or PENNY_DAYS_DEFAULT)
                # penny_ratio = 近 20 日中收盘价（未复权）低于面值线的天数占比
                dp["非面值退市"] = ~(np.nan_to_num(self.penny_ratio) * PENNY_WIN
                                     >= min(days, PENNY_WIN))
            self.delist_parts = dp
            for k, v in dp.items():
                m &= np.asarray(v, bool)
                parts[k] = v
        else:
            self.delist_parts = {}

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

    # ============================================================ 容量约束回测
    def capacity_plan(self, mask, hold=20, max_pos=10, max_new=3, pick="deep",
                      seed0=42, dedupe=True, ladder=None):
        """只算「实际建仓计划」，不算净值 —— 供 `trades()` 复用。

        为什么单独抽出来
        ----------------
        `capacity()` 会跑 200 次随机模拟（约 25s），但交易明细只需要**一次**
        确定性计划。所以这里做轻量版：一次 `plan_positions`，并补齐
        `pick_name` / `drop_pct` 等展示字段。

        `ladder`（每批买入只数）：非空时启用阶梯建仓，语义见
        `LADDER_PRESETS` / `v3b_lib.plan_positions(ladder=...)`。
        ⚠️ 此时**同时持仓上限 = sum(序列)**、每日买入由序列推出，传入的
        `max_pos` / `max_new` 都会让位。

        返回：`plan_positions()` 的结果 + max_pos/max_new/pick_name/drop_pct
        """
        from v3b_lib import plan_positions  # noqa: PLC0415
        lad = parse_ladder(ladder)
        mp_eff = int(sum(lad)) if lad else int(max_pos)
        mn_eff = int(max(lad)) if lad else int(max_new)   # 阶梯：每日买入上限 = 单批最大只数
        rule = PICK_RULES.get(str(pick), PICK_RULES["deep"])
        col = rule["col"]
        if col is None:
            pk = None
        elif col == "__random__":
            pk = "__random__"
        else:
            pk = np.asarray(self.df[col].values, np.float64)
        pl = plan_positions(mask, self.D, self.nd, int(hold),
                            max_pos=mp_eff, max_new=mn_eff,
                            pick=pk, pick_asc=rule["asc"], rng_seed=seed0,
                            stock_of_row=self.sid, dedupe=dedupe, ladder=lad)
        ns = max(pl["n_signal"], 1)
        pl.update(max_pos=int(mp_eff), max_new=int(mn_eff),
                  pick=str(pick), pick_name=rule["name"],
                  drop_pct=float(pl["n_drop"]) / ns)
        return pl

    def _daily_invested(self, pl, hold):
        """每日「投入权重之和」（占账户总资金比例）；非阶梯模式返回 None。

        为什么单独算：`nav_from_holds` 只回传每日**持仓只数**（cnt），
        但阶梯口径下用户真正关心的是「钱有多少在外面」——持仓 3 只可能只占
        3 成，也可能占 8 成。这里按与 `nav_from_holds` 完全同源的存活规则
        （`entry + j < exit_d`）把权重铺到日度上。
        """
        if not pl.get("ladder") or not pl["holds"]:
            return None
        w = np.asarray(pl["weights"], np.float64)
        h = np.asarray(pl["holds"], np.int64)
        ent, ext, rw = h[:, 0], h[:, 1], h[:, 2]
        out = np.zeros(self.nd)
        for j in range(int(hold)):
            alive = (ent + j) < ext
            if not alive.any():
                continue
            dd = self.D[rw[alive]] + j
            ww = w[alive]
            ok = dd < self.nd
            np.add.at(out, dd[ok], ww[ok])
        return out

    def capacity(self, mask, hold=20, max_pos=10, max_new=3, pick="deep",
                 n_sim=200, seed0=42, dedupe=True, ladder=None):
        """在「同时持仓上限 + 每日新开仓上限」下模拟，回答「实盘真能这么干吗」。

        `dedupe=True`（默认）：同一只票在**未平仓期间不得再次买入**，卖掉之后才
        恢复可交易。传 False 可退回原先允许重复持有同一标的的口径。

        为什么必须做
        ------------
        `stats()` 是**不限仓位**口径：某日 500 个信号就等权买 500 只。
        实测默认 K3 策略平均同时持仓 **943.9 只** —— 个人资金根本做不到。
        本方法按真实下单节奏模拟：

          每个交易日：先卖出到期仓位 → 再看当日新信号 → 排序取前 max_new 个
                      → 受 max_pos 上限截断 → 被丢弃的信号**不补买**（保守）

        ⚠️⚠️ 两个口径必须分清（这是容量回测最容易搞错的地方）
        ------------------------------------------------------------------
        · `on_invested`（已投资金口径）：日收益 = 持仓收益的等权平均
            → 与不限仓位基线**可直接对比**，但它假设「闲置仓位也在赚钱」。
        · `on_capital`（账户资金口径）：日收益 = 持仓收益之和 / max_pos
            → **这才是账户里真实看到的收益率**，被资金利用率摊薄。

        ⚠️ **阶梯建仓**（`ladder`，默认 None = 等权）
        ----------------------------------------------
        传只数数组（如 `[1,2,2,2,3]`）后改成「**每批买几只**」的口径：第 k 批买
        `ladder[k]` 只、每只等分资金，`sum(ladder)` 即满仓只数（= 同时持仓上限）。
        全部纪律见 `LADDER_PRESETS` / `v3b_lib.plan_positions(ladder=...)`。
        两个口径随之改为**加权**：
          · on_invested：分母 = 当日投入权重之和（回答「投出去的钱赚了多少」）
          · on_capital ：分母 = 1.0 = 全部资金（回答「整个账户赚了多少」）
        ⚠️ 启用阶梯后 `max_pos` 被 `sum(ladder)` 覆盖、`max_new` 由序列推出，
        `out["max_pos"]` 回传的是**实际生效值**。

        ⚠️ 排序只用信号日 T 当日可见字段（`PICK_RULES`），无前视偏差。
           实测「最超跌优先」显著优于随机（详见 PICK_RULES 注释）。

        参数
        ----
        mask     : bool[n]   原始信号掩码（不限仓位口径下会被全部买入的股票）
        max_pos  : int       同时持仓上限（阶梯模式下被 sum(ladder) 覆盖）
        max_new  : int       每日最多新建仓数（阶梯模式下由序列推出，不再使用）
        pick     : str       PICK_RULES 的键
        n_sim    : int       pick="rand" 时的随机模拟次数（用于给出分布的均值/分位）
        seed0    : int       随机种子起点
        ladder   : list|str|None  每批买入只数（如 [1,2,2,2,3] 或 "1,2,2,2,3"）；
                             None/非法 → 等权。见 parse_ladder()

        返回 dict：两种口径的指标 + 容量诊断 + 阶梯诊断 +（rand 时）随机分布对照
        """
        from v3b_lib import (plan_positions, nav_from_holds,  # noqa: PLC0415
                             capacity_stats, ann_stats)
        mask = np.asarray(mask, bool)
        rule = PICK_RULES.get(str(pick), PICK_RULES["deep"])
        col = rule["col"]
        n = self.C["n"]
        lad = parse_ladder(ladder)
        mp_eff = int(sum(lad)) if lad else int(max_pos)
        mn_eff = int(max(lad)) if lad else int(max_new)   # 阶梯：每日买入上限 = 单批最大只数

        def _run(pick_key, asc, seed):
            if pick_key is None:
                pk = None
            elif pick_key == "__random__":
                pk = "__random__"
            else:
                pk = np.asarray(self.df[pick_key].values, np.float64)
            pl = plan_positions(mask, self.D, self.nd, int(hold),
                                max_pos=mp_eff, max_new=mn_eff,
                                pick=pk, pick_asc=asc, rng_seed=seed,
                                stock_of_row=self.sid, dedupe=dedupe, ladder=lad)
            if lad is None:
                netA, cnt = nav_from_holds(pl["holds"], self.D, self.nd,
                                           self.oret_sig, self.C, hold=int(hold))
                netB, _ = nav_from_holds(pl["holds"], self.D, self.nd,
                                         self.oret_sig, self.C, hold=int(hold),
                                         capital_slots=int(mp_eff))
            else:
                # 阶梯口径：权重要传下去，账户口径的分母换成「全部资金」= 1.0
                ww = pl["weights"]
                netA, cnt = nav_from_holds(pl["holds"], self.D, self.nd,
                                           self.oret_sig, self.C, hold=int(hold),
                                           weights=ww)
                netB, _ = nav_from_holds(pl["holds"], self.D, self.nd,
                                         self.oret_sig, self.C, hold=int(hold),
                                         weights=ww, capital_slots=1.0)
            return pl, netA, netB, cnt

        pl, netA, netB, cnt = _run(col, rule["asc"], seed0)
        sA, sB = ann_stats(netA, self.nd), ann_stats(netB, self.nd)
        cs = capacity_stats(pl, self.nd, cnt, netA,
                            daily_w=self._daily_invested(pl, hold))

        out = dict(
            max_pos=int(mp_eff), max_new=int(mn_eff),
            ladder=pl.get("ladder"), ladder_total=pl.get("ladder_total"),
            pick=str(pick), pick_name=rule["name"], pick_desc=rule["desc"],
            # ---- 已投资金口径（与 stats() 基线可比）
            cagr=sA["cagr"], mdd=sA["mdd"], sharpe=sA["sharpe"],
            ann_arith=sA["ann_arith"], vol=sA["vol"], net=netA, cnt=cnt,
            # ---- 账户资金口径（实盘真实感受）
            cap_cagr=sB["cagr"], cap_mdd=sB["mdd"], cap_sharpe=sB["sharpe"],
            cap_vol=sB["vol"], net_cap=netB,
            # ---- 容量诊断
            n_hold=int(cs["n_hold"]), n_signal=int(cs["n_signal"]),
            n_drop=int(cs["n_drop"]), drop_pct=float(cs["drop_pct"]),
            n_drop_dup=int(cs["n_drop_dup"]),  # 因「已持有未平仓」被跳过
            fill_pct=float(cs["fill_pct"]),
            avg_pos=float(cs["avg_pos"]), max_pos_seen=int(cs["max_pos_seen"]),
            empty_pct=float(cs["empty_pct"]), active_pct=float(cs["active_pct"]),
            # ---- 阶梯建仓诊断（等权时全为 None）
            # avg_invested = 有持仓的日子里，平均有多少比例的资金在外面
            # full_pct     = 满仓（投入 100%）的交易日占比
            avg_invested=cs.get("avg_invested"), full_pct=cs.get("full_pct"),
            max_invested=cs.get("max_invested"),
            # ---- 分段表现
            yearly=self._seg_yearly(netB),
            segs=self._seg_periods(netB),
            nav=np.cumprod(1.0 + netB),
        )

        # ---- 随机基准分布（用于判断当前规则是否真的有效）
        if n_sim and str(pick) != "rand":
            cg = []
            for i in range(int(n_sim)):
                _, _, nb, _ = _run("__random__", True, seed0 + i)
                cg.append(ann_stats(nb, self.nd)["cagr"])
            cg = np.asarray(cg, np.float64)
            z = ((out["cap_cagr"] - cg.mean()) / cg.std(ddof=1)
                 if cg.std(ddof=1) > 0 else 0.0)
            out["rand_mean"] = float(cg.mean())
            out["rand_sd"] = float(cg.std(ddof=1))
            out["rand_p05"] = float(np.percentile(cg, 5))
            out["rand_p95"] = float(np.percentile(cg, 95))
            out["rand_min"] = float(cg.min())
            out["rand_max"] = float(cg.max())
            out["rand_pctile"] = float((cg < out["cap_cagr"]).mean())
            out["z"] = float(z)
            out["n_sim"] = int(n_sim)
        return out

    def _seg_yearly(self, net):
        yrs = self.years
        ytab = []
        for y in range(2015, 2027):
            s = yrs == y
            if s.sum() < 20:
                continue
            nav = np.cumprod(1.0 + net[s])
            ytab.append(dict(year=int(y), ret=float(nav[-1] - 1.0),
                             mdd=float((nav / np.maximum.accumulate(nav) - 1.0).min())))
        return ytab

    def _seg_periods(self, net):
        yrs = self.years
        SEGS = [("2015-2020", 2015, 2020), ("2021-2022", 2021, 2022),
                ("2023-2024", 2023, 2024), ("2025-2026", 2025, 2026)]
        segs = {}
        for sn, y0, y1 in SEGS:
            s = (yrs >= y0) & (yrs <= y1)
            segs[sn] = float(np.prod(1.0 + net[s]) - 1.0) if s.sum() >= 20 else None
        segs["IS(2015-2020)"] = float(np.prod(1.0 + net[yrs <= 2020]) - 1.0)
        segs["OOS(2021-2026)"] = float(np.prod(1.0 + net[yrs >= 2021]) - 1.0)
        return segs

    # ================================================================ 交易明细
    def trades(self, mask, hold=20, cost=RT_COST, include_fin=True, plan=None):
        """从掩码提取**逐笔交易明细**（与 stats() 完全同源，口径一致）。

        交易口径（固定持有期，可复现）：
          信号日 T  →  T+1 开盘买入  →  持有 H 个交易日  →  T+1+H 开盘卖出

        两种口径（由 `plan` 决定）
        -------------------------
        · `plan=None`（默认）→ **不限仓位**：列出全部信号对应的交易
              （默认 K3 为 93,553 笔，实盘做不完这么多）。
        · `plan=dict`（来自 `capacity_plan()`）→ **容量约束**：只保留
              真正被建仓的那些交易（默认 K3 为 787 笔建仓 / 781 笔可结算）。

        ⚠️⚠️ 为什么「建仓数」会 >「可结算数」（787 vs 781）
        -------------------------------------------------
        容量计划在建仓日只看「信号是否存在」，不看「持有期结束时数据是否还在」。
        数据末尾最后 H 个交易日内建的仓，**到期日超出了面板范围**，因此算不出收益。
        这批头寸是**未平仓**（实盘里就是还拿在手上），本方法用 `n_open` 单独报告，
        **不混进交易明细**，也**不计入胜率等统计**（否则会因缺少结局而偏乐观）。
        这一点必须显式呈现，不能悄悄丢 —— 否则「丢弃 99.2%」这类数字会对不上。

        实现要点
        --------
        1. 直接复用 `L.simulate_hold`，保证与 `stats()` 的 `n_trade`/胜率/PF 逐位一致；
        2. `simulate_hold` 返回的 `pos` 是**面板行号**（信号日所在行），
           因此：信号日 = day_idx[pos]，入场日 = 信号日 + 1，出场日 = 信号日 + 1 + H；
        3. 买卖价从 `shift_block(buy_open/sell_open, k)` 取，与 simulate_hold 同源；
        4. 基准（同口径市场收益）用 `CHAIN[H][入场日]`，即「T+1 开盘 → T+1+H 开盘」
           的市场链式收益，与个股 trade_ret 完全可比。
        5. `plan` 的过滤**放在最前面**（simulate_hold 之后立刻切），
           保证下游全部统计（汇总/逐年/分布/最佳最差）都自动只覆盖受限子集。

        参数
        ----
        plan : dict | None
            `capacity_plan()` 的返回；提供时只保留其 `holds` 里的交易。

        返回
        ----
        dict(
          rows     : list[dict]  逐笔明细（未排序，按出场日升序）
          summary  : dict        全局汇总（含分年、盈亏分布、最佳/最差）
          yearly   : list[dict]  逐年交易统计
          hist     : dict        收益分布直方图（分箱）
          n_trade  : int
          plan     : dict|None   容量约束信息（含 n_open 未平仓数）
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

        # ---- 容量约束：只保留真正建仓的那批（⚠️ 必须在任何统计之前切）
        plan_info = None
        wmap = {}          # 面板行号 → 该笔占账户资金的百分比（仅阶梯建仓时非空）
        if plan is not None:
            held_rows = np.asarray([h[2] for h in plan["holds"]], np.int64)
            keep = np.isin(pos, held_rows) if len(held_rows) else np.zeros(len(pos), bool)
            n_open = int(len(held_rows) - keep.sum())
            if plan.get("ladder") and len(held_rows):
                wmap = {int(hr): float(x) * 100.0
                        for hr, x in zip(held_rows.tolist(), plan.get("weights") or [])}
            r, ed, pos = r[keep], ed[keep], pos[keep]
            if len(r) == 0:
                return None
            plan_info = dict(
                max_pos=int(plan.get("max_pos") or 0),
                max_new=int(plan.get("max_new") or 0),
                pick=plan.get("pick"), pick_name=plan.get("pick_name"),
                ladder=plan.get("ladder"), ladder_total=plan.get("ladder_total"),
                n_signal=int(plan.get("n_signal") or 0),
                n_drop=int(plan.get("n_drop") or 0),
                drop_pct=float(plan.get("drop_pct") or 0.0),
                n_drop_dup=int(plan.get("n_drop_dup") or 0),  # 重复持仓被跳过
                n_plan=int(len(held_rows)),      # 计划建仓数
                n_open=n_open,                   # 期末仍未平仓（数据边界所致）
            )

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
                w=(round(wmap[int(p_i)], 3) if int(p_i) in wmap else None),
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
                    best=best20, worst=worst20, hold=int(hold), plan=plan_info)

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
        # 行业两级归类（门类 → 细分行业），按最新交易日统计各门类股票数
        _g, _absent, _unlisted, _unlisted_n = industry_tree(self.industries,
                                                            self.ind_name[idx])
        return dict(
            n_rows=int(self.C["n"]), n_stocks=int(len(self.C["starts"])),
            n_days=int(self.nd),
            date_start=str(self.day_str[0]), date_end=str(self.day_str[-1]),
            last_date=str(self.day_str[last]),
            build_ms=self.build_ms,
            industries=self.industries,
            industry_tree=_g,
            industry_absent=_absent,
            industry_unlisted=_unlisted,
            industry_unlisted_n=_unlisted_n,
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


# ================================================================ 阶梯建仓
# 「每批买入只数」序列：序列里的每个数 = **那一批买入的股票只数**。
# 语义与全部纪律见 v3b_lib.plan_positions 的 `ladder` 参数，摘要：
#   · `sum(ladder)` = 满仓只数 = 同时持仓上限（用户填的 max_pos 会被覆盖）
#   · 累计目标 `cumsum(ladder)` 决定补仓档位：每次建仓补到「下一个累计目标」
#   · 每只等分资金：w = 1/sum(ladder)，满仓时 Σw = 1.0 → **绝不超配**
#   · 满仓即停：持仓只数 ≥ sum(ladder) 后不再开新仓
# 因为同时持仓与每日买入都由序列推出，两者在前端会被**接管并置灰**，
# 用户只需给出这一个序列。
LADDER_PRESETS = [
    dict(id="l12223", name="1 / 2 / 2 / 2 / 3", ladder=[1, 2, 2, 2, 3],
         tag="满仓 10 只", desc="空仓先买 1 只试探，之后每批 2 只，最后一批提速到 3 只，"
                               "5 批正好买满 10 只 —— 开局最轻。"),
    dict(id="l11235", name="1 / 1 / 2 / 3 / 3", ladder=[1, 1, 2, 3, 3],
         tag="满仓 10 只", desc="前两批各 1 只（更保守的开局），后段提速到 3 只，5 批满仓。"),
    dict(id="l1234", name="1 / 2 / 3 / 4", ladder=[1, 2, 3, 4],
         tag="满仓 10 只", desc="4 批买满 10 只，建仓更快、批次更少（每批都在加码）。"),
    dict(id="l12345", name="1 / 2 / 3 / 4 / 5", ladder=[1, 2, 3, 4, 5],
         tag="满仓 15 只", desc="严格等差递增，5 批共 15 只，每只占 6.7% 资金 —— "
                               "持仓更分散、单票风险更低。"),
    dict(id="l22222", name="2 / 2 / 2 / 2 / 2", ladder=[2, 2, 2, 2, 2],
         tag="满仓 10 只 · 对照", desc="每批固定 2 只，5 批买满 10 只 —— "
                                      "数值上等价于「等权 10 只 / 每日 2 只」，用来做对照。"),
]


def parse_ladder(x):
    """把前端传来的阶梯序列规范成 `list[int]`；未启用/非法 → None（= 等权）。

    每个元素 = **该批买入的只数**（正整数）。宽容处理字符串
    （`"1,2,2,2,3"` / `"1/2/2"` / 全角逗号 / 空格），因为这是人手输入的框。
    非整数会四舍五入（如 `1.6,2.4` → `2,2`）；取整后 < 1 视为非法。
    非法输入**静默退回等权**而不是报错 —— 用户在输入框里打字时中间态必然非法
    （比如刚删到只剩 `"1,"`），弹错误会打断输入。
    """
    if x is None or x is False:
        return None
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return None
        for ch in ("，", "、", "/", "|", ";", "；", " ", "\t"):
            s = s.replace(ch, ",")
        parts = [t for t in s.split(",") if t.strip()]
        try:
            vals = [float(t) for t in parts]
        except ValueError:
            return None
    elif isinstance(x, (list, tuple, np.ndarray)):
        if len(x) == 0:
            return None
        try:
            vals = [float(v) for v in x]
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not vals or len(vals) > 50:
        return None
    cnt = [int(round(v)) for v in vals]
    if any(c < 1 for c in cnt):
        return None
    return cnt


# ================================================================ 市值区间
SIZE_N = 10          # 市值分位共 10 档（每档 10%），序号 0..9，0 = 最小
SIZE_PCT = ["10%", "20%", "30%", "40%", "50%",
            "60%", "70%", "80%", "90%", "100%"]


def norm_size_band(smin, smax):
    """把 (下限, 上限) 规范化成两个合法序号，并保证 `smin <= smax`。

    · 缺省：下限 0（最小10%），上限 9（全部）—— 只给上限时等价于旧口径
    · 越界值夹到 [0, 9]；非数字 → 用缺省
    · 传反了（下限 > 上限）自动交换，而不是返回空集
      （用户在双滑块上把两个把手拖过头是常见操作，静默纠正比报错友好）
    """
    def _one(v, default):
        if v is None or v == "":
            return default
        try:
            n = int(round(float(v)))
        except (TypeError, ValueError):
            return default
        return max(0, min(SIZE_N - 1, n))

    a = _one(smin, 0)
    b = _one(smax, SIZE_N - 1)
    return (b, a) if a > b else (a, b)


def size_band_label(smin, smax):
    """市值区间的中文标签（条件名 / 寻优结果行都用它，保证口径一致）。"""
    smin, smax = norm_size_band(smin, smax)
    if smin <= 0 and smax >= SIZE_N - 1:
        return "市值全部"
    lo = f"D{smin + 1}"
    hi = f"D{smax + 1}"
    band = f"{SIZE_PCT[smin - 1]}~{SIZE_PCT[smax]}"     # D_a~D_b 覆盖的百分比跨度
    if smin <= 0:
        return f"市值≤{hi}（最小{SIZE_PCT[smax]}）"
    if smin == smax:                                     # 单个分位，别写成 D2~D2
        return f"市值{lo}（{band}）"
    if smax >= SIZE_N - 1:
        return f"市值≥{lo}（剔除最小{SIZE_PCT[smin - 1]}）"
    return f"市值{lo}~{hi}（{band}）"


# ================================================================ 默认参数（K3 最优）
DEFAULT_PARAMS = {
    "px_ma60_min": 1, "px_ma60_max": 1,   # 距MA60 D1
    "size_min": 0,                        # 市值区间下限（0 = 最小10%）
    "size_max": 2,                        # 市值区间上限（2 = 最小30%）
    "mkt_state": "bear",                  # 市场净值 < MA60
    "mkt_hv": "any",
    "deep_any": False,
    "confirm": [],
    "hold": 20,
    # ---- 容量约束（实盘可执行性）
    #   max_pos = 0 表示不限（研究报告的原始口径：符合条件的全买）
    #   max_pos > 0 时启用「同时持仓上限 + 每日新开仓上限」
    "max_pos": 0,
    "max_new": 3,
    "pick": "deep",      # 信号超额时的选股规则，见 PICK_RULES
    # ---- 阶梯建仓（建仓节奏）
    #   None = 等权（每笔一样大，研究报告原始口径）
    #   给出只数数组（如 [1,2,2,2,3]）时，改为「**每批买几只**」：
    #   第 1 批买 1 只 → 第 2 批买 2 只 → …，每只等分资金，sum = 满仓只数。
    #   详见 LADDER_PRESETS / parse_ladder 与 v3b_lib.plan_positions(ladder=...)。
    #   ⚠️ 启用后 max_pos 被覆盖为 sum(cap_ladder)，max_new 由序列推出不再使用。
    "cap_ladder": None,
    # ---- 退市风险过滤（默认全关 = 研究报告原始口径）
    #   三条判据只用买入日 T 已公开的信息，见 build_mask 的「退市风险过滤」段。
    "delist": {"financial": False, "loss2y": False, "penny": False},
    "delist_rev_floor": None,      # None → 用 DELIST_REV_FLOOR(1亿)
    "delist_main_2024": False,     # 主板营收门槛按 2024 新规提到 3 亿
    "delist_penny_days": None,     # None → 用 PENNY_DAYS_DEFAULT(10/20)
}

# 信号数超过每日/持仓上限时的**选股排序规则**。
# ⚠️ 所有排序键都必须是信号日 T **当日收盘可见**的字段，否则就是前视偏差。
#    实测结论（默认 K3 策略，10只/日3只）：
#      · "deep"（最超跌优先）显著优于随机 —— 当日横截面内再分5档，
#        Q1(最超跌) 单笔净收益 5.27% vs Q5(最不超跌) 3.69%，差 1.58pp
#        (t=9.01, p=2e-19，19,202 vs 18,216 笔)；12 年中 11 年为正，
#        且 OOS(2021-26) 的差(+2.01pp) 比 IS(2015-20) 的(+1.06pp) 更大。
#      · "rand" 作为基准对照（多种子），用于判断某规则是否真有效。
PICK_RULES = {
    "deep":   dict(col="px_ma60_pct", asc=True,
                   name="最超跌优先", desc="距MA60 越低越优先（推荐，经检验有显著正超额）"),
    "amount": dict(col="turnover", asc=False,
                   name="成交额优先", desc="流动性最好，冲击成本最低"),
    "small":  dict(col="size_grp", asc=True,
                   name="小市值优先", desc="市值越小越优先"),
    "big":    dict(col="size_grp", asc=False,
                   name="大市值优先", desc="市值越大越优先"),
    "drop60": dict(col="ret60", asc=True,
                   name="60日跌幅优先", desc="ret60 越低越优先"),
    "rand":   dict(col="__random__", asc=True,
                   name="随机（基准）", desc="随机选，用作对照基准"),
    "none":   dict(col=None, asc=True,
                   name="原始顺序", desc="按面板原始顺序（等同最早上市优先）"),
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
