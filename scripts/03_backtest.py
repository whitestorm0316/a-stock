#!/usr/bin/env python3
"""
03_backtest.py — 无未来函数的组合回测引擎 (向量化信号 + 逐日撮合)

时序约定（严格防未来函数）
================================================================
  T 日 15:00 收盘  →  用截至 T 日收盘的数据计算指标与信号
  T+1 日 09:30 开盘 →  按 T+1 开盘价成交（含滑点）

  * 决策信息集与成交时点完全不重叠：T 日收盘信号绝不在 T 日成交
  * 不使用未来最高价/最低价/成交量/财务数据
  * 涨跌停：T+1 开盘价触及涨停 → 无法买入；触及跌停 → 无法卖出
  * 停牌：T+1 无 K 线 → 挂单顺延至下一个可交易日（成交价仍用届时开盘价）
  * 复权：以最新交易日为锚的前复权，复权因子仅用除权除息日 ≤ 当日的部分

A 股成本模型
================================================================
  佣金      双边万2.5，单笔最低 5 元
  印花税    卖出千1（2023-08-28 起减半为千0.5）
  过户费    万0.1；2022-04-29 前仅沪市收取，之后沪深双边收取
  滑点      双边千1（按不利方向调整成交价）
"""
import os
import numpy as np
import pandas as pd
from dataclasses import dataclass, replace
from typing import Dict, Optional

# ---------------------------------------------------------------- 成本参数
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_TAX_BEFORE = 0.001
STAMP_TAX_AFTER = 0.0005
STAMP_CUT_DATE = np.datetime64("2023-08-28")
TRANSFER_FEE = 0.00001
TRANSFER_FEE_START = np.datetime64("2022-04-29")
SLIPPAGE = 0.001


@dataclass
class BacktestConfig:
    name: str = "strategy"
    initial_cash: float = 1_000_000.0
    max_positions: int = 20
    position_pct: float = 0.05
    min_days_listed: int = 120
    exclude_st: bool = True
    min_amount: float = 20_000_000.0
    max_amount_pct: float = 0.02
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    max_hold_days: Optional[int] = None
    cooldown: int = 0
    max_pending: Optional[int] = None   # 挂单队列上限, 默认 3x max_positions
    min_order_amount: float = 0.0       # 单笔最小下单金额(元), 默认不设限
                                        # 注: 不可设成 5000 之类的固定门槛, 否则账户衰减后
                                        #     预算低于门槛会永久停止交易(死亡螺旋伪影)


# ================================================================ 面板索引
class PanelIndex:
    """一次性构建的行情面板索引, 可跨多次回测复用"""
    _cache = {}

    @classmethod
    def get(cls, panel: pd.DataFrame) -> "PanelIndex":
        # 缓存键需包含指标指纹, 否则不同 MACD 参数的面板会命中同一缓存
        fp = 0.0
        for c in ("hist", "dif", "vol_ratio", "close_price"):
            if c in panel.columns:
                v = panel[c].values
                fp += float(np.nansum(v[::997])) * (1.0 + abs(hash(c)) % 7)
        key = (len(panel), str(panel["date"].iloc[0]),
               str(panel["date"].iloc[-1]), panel["thscode"].iloc[0],
               round(fp, 4), tuple(sorted(panel.columns)))
        if key not in cls._cache:
            cls._cache.clear()
            cls._cache[key] = cls(panel)
        return cls._cache[key]

    def __init__(self, panel: pd.DataFrame):
        p = panel.sort_values(["date", "thscode"], kind="stable").reset_index(drop=True)
        self.panel = p
        self.dates = np.array(sorted(p["date"].unique()))
        self.n_days = len(self.dates)
        dvals = p["date"].values
        self.starts = np.searchsorted(dvals, self.dates, side="left")
        self.ends = np.searchsorted(dvals, self.dates, side="right")
        self.A = {c: p[c].values for c in p.columns}
        # 行 -> 交易日序号 (向量化)
        self.day_of_row = np.searchsorted(self.dates, dvals, side="left").astype(np.int32)
        # code -> 行号数组(按日期升序) + 对应的交易日序号
        idx = p.groupby("thscode", sort=False).indices
        self.code_rows = {c: np.sort(v) for c, v in idx.items()}
        self.code_days = {c: self.day_of_row[r] for c, r in self.code_rows.items()}

    def lookup(self, code, t):
        """返回 (code, 第 t 个交易日) 的面板行号, 不存在则 None"""
        days = self.code_days.get(code)
        if days is None:
            return None
        pos = np.searchsorted(days, t)
        if pos < len(days) and days[pos] == t:
            return int(self.code_rows[code][pos])
        return None


# ================================================================ 回测引擎
class VectorizedBacktester:
    def __init__(self, index: PanelIndex, config: BacktestConfig, seed: int = 12345):
        self.ix = index
        self.cfg = config
        self.A = index.A
        self.dates = index.dates
        self.n_days = index.n_days
        self.starts = index.starts
        self.ends = index.ends
        self.codes = index.A["thscode"]
        # 每日随机优先级 (固定种子, 保证可复现)
        rng = np.random.default_rng(seed)
        self._rand = rng.random(len(self.codes))

    def _rng(self, sl):
        return self._rand[sl]

    def run(self, sig_all: np.ndarray, exit_all: np.ndarray,
            priority: Optional[np.ndarray] = None,
            seed: int = 12345,
            day_range: Optional[tuple] = None) -> Dict:
        """
        priority: 与面板等长的信号优先级数组, 数值越大越优先建仓。
                  当某日信号数超过可用持仓槽位时, 按 priority 降序选取。
                  为 None 时用固定种子的随机序, 保证结果可复现且无系统性选股偏差。
        day_range: (lo, hi) 交易日序号闭区间。只在该窗口内交易与统计,
                   净值序列也被裁剪到该窗口, 使年化收益率计算正确。
                   若为 None 则用全部交易日。
        """
        cfg = self.cfg
        A = self.A
        dates = self.dates
        n_days = self.n_days
        starts, ends = self.starts, self.ends
        codes = self.codes
        close = A["close_price"]
        open_px = A["open_price"]
        can_buy_open = A["can_buy_open"]
        can_sell_open = A["can_sell_open"]
        days_listed = A["days_since_list"]
        is_st = A["is_st_now"]
        amt = A["turnover"]
        amt20 = A["amt20"]
        exch = A["exchange"]

        if day_range is not None:
            t_lo, t_hi = int(day_range[0]), int(day_range[1])
            t_lo = max(0, t_lo)
            t_hi = min(n_days - 1, t_hi)
        else:
            t_lo, t_hi = 0, n_days - 1
        n_win = t_hi - t_lo + 1

        cash = cfg.initial_cash
        holdings: Dict[str, dict] = {}
        pending_buys: Dict[str, int] = {}
        pending_sig: Dict[str, object] = {}
        pending_sells: Dict[str, int] = {}
        last_exit_i: Dict[str, int] = {}
        equity = np.zeros(n_win)
        npos = np.zeros(n_win)
        trades = []
        sl_map = self.ix.lookup

        for t in range(t_lo, t_hi + 1):
            s, e = starts[t], ends[t]
            d = dates[t]
            ti = t - t_lo

            # ---- 1. 执行挂单：T 日开盘成交，信号来自 T-1 收盘 ----
            # 成交价 = T 日开盘价(含滑点)；涨跌停约束用 T 日开盘价与 T 日涨跌停价比较
            if pending_sells:
                for code in list(pending_sells.keys()):
                    h = holdings.get(code)
                    if h is None:
                        pending_sells.pop(code, None)
                        continue
                    j = sl_map(code, t)
                    if j is None:
                        continue                    # 停牌, 顺延
                    if not can_sell_open[j]:
                        continue                    # 开盘即跌停, 无法卖出, 顺延
                    px = open_px[j]
                    if not np.isfinite(px) or px <= 0:
                        continue
                    sell_px = px * (1 - SLIPPAGE)
                    gross = h["shares"] * sell_px
                    fee = self._sell_fee(gross, d, exch[j])
                    cash += gross - fee
                    pnl = gross - fee - h["cost_total"]
                    trades.append((code, h["buy_date"], d, h["cost_px"], sell_px,
                                   h["shares"], t - h["buy_i"], pnl,
                                   pnl / h["cost_total"], h["buy_fee"] + fee,
                                   h["reason"], h.get("signal_date")))
                    holdings.pop(code, None)
                    last_exit_i[code] = t
                    pending_sells.pop(code, None)

            if pending_buys:
                for code in list(pending_buys.keys()):
                    if len(holdings) >= cfg.max_positions:
                        pending_buys.pop(code, None)
                        continue
                    j = sl_map(code, t)
                    if j is None:
                        pending_buys.pop(code, None)
                        continue
                    if not can_buy_open[j]:
                        pending_buys.pop(code, None)   # 开盘即涨停, 放弃该笔
                        continue
                    px = open_px[j]
                    if not np.isfinite(px) or px <= 0:
                        pending_buys.pop(code, None)
                        continue
                    buy_px = px * (1 + SLIPPAGE)
                    total_eq = cash + self._mtm(holdings, close, t)
                    budget = min(total_eq * cfg.position_pct, cash)
                    a = amt[j] if np.isfinite(amt[j]) else 0.0
                    budget = min(budget, a * cfg.max_amount_pct)
                    if budget < cfg.min_order_amount:
                        pending_buys.pop(code, None)
                        continue
                    shares = int(budget / buy_px / 100) * 100
                    if shares < 100:
                        pending_buys.pop(code, None)
                        continue
                    gross = shares * buy_px
                    fee = self._buy_fee(gross, d, exch[j])
                    while gross + fee > cash and shares > 100:
                        shares -= 100
                        gross = shares * buy_px
                        fee = self._buy_fee(gross, d, exch[j])
                    if gross + fee > cash:
                        pending_buys.pop(code, None)
                        continue
                    cash -= gross + fee
                    holdings[code] = dict(shares=shares, cost_px=buy_px,
                                          cost_total=gross + fee, buy_fee=fee,
                                          buy_date=d, buy_i=t, reason="signal",
                                          signal_date=pending_sig.pop(code, None))
                    pending_buys.pop(code, None)

            # ---- 2. T 日收盘信号 → 挂 T+1 单 ----
            # 仅做"信息可得性"过滤, 不做可成交性预判(成交性在 T+1 执行时判定)
            sl = slice(s, e)
            sig = sig_all[sl]
            if sig.any():
                ok = ((days_listed[sl] >= cfg.min_days_listed)
                      & np.isfinite(close[sl])
                      & (np.nan_to_num(amt20[sl], nan=0) >= cfg.min_amount))
                if cfg.exclude_st:
                    ok &= ~is_st[sl]
                cand = np.nonzero(sig & ok)[0]
                if len(cand):
                    # 按优先级排序 (信号强度), 保证超配时选取无系统性偏差
                    if priority is not None:
                        pri = priority[sl]
                        cand = cand[np.argsort(-np.nan_to_num(pri[cand], nan=-np.inf),
                                               kind="stable")]
                    else:
                        rng_sl = self._rand[sl]
                        cand = cand[np.argsort(rng_sl[cand], kind="stable")]
                    cday = codes[sl]
                    cap = cfg.max_pending or (cfg.max_positions * 3)
                    for k in cand:
                        if len(pending_buys) >= cap:
                            break
                        c = cday[k]
                        if c in holdings or c in pending_buys:
                            continue
                        if cfg.cooldown and c in last_exit_i:
                            if t - last_exit_i[c] < cfg.cooldown:
                                continue
                        pending_buys[c] = t + 1
                        pending_sig[c] = d

            # ---- 3. T 日收盘卖出判定 ----
            # 3a. 信号退出 (仅当 exit 信号触发)
            if holdings:
                exs = np.nonzero(exit_all[sl])[0] if exit_all[sl].any() else ()
                cday = codes[sl]
                for k in exs:
                    c = cday[k]
                    h = holdings.get(c)
                    if h is None or c in pending_sells:
                        continue
                    h["reason"] = "signal"
                    pending_sells[c] = t + 1

                # 3b. 风险退出 (独立于信号, 每日检查)
                if (cfg.max_hold_days or cfg.stop_loss is not None
                        or cfg.take_profit is not None):
                    for c, h in holdings.items():
                        if c in pending_sells:
                            continue
                        if cfg.max_hold_days and (t - h["buy_i"]) >= cfg.max_hold_days:
                            h["reason"] = "max_hold"
                            pending_sells[c] = t + 1
                            continue
                        j = sl_map(c, t)
                        if j is None:
                            continue
                        px_now = close[j]
                        if not np.isfinite(px_now):
                            continue
                        r_now = px_now / h["cost_px"] - 1
                        if cfg.stop_loss is not None and r_now <= cfg.stop_loss:
                            h["reason"] = "stop_loss"
                            pending_sells[c] = t + 1
                            continue
                        if cfg.take_profit is not None and r_now >= cfg.take_profit:
                            h["reason"] = "take_profit"
                            pending_sells[c] = t + 1
                            continue

            # ---- 4. 盯市 ----
            equity[ti] = cash + self._mtm(holdings, close, t)
            npos[ti] = len(holdings)

        # 期末清算: 按窗口最后交易日收盘价卖出剩余持仓
        if holdings:
            d = dates[t_hi]
            for code, h in list(holdings.items()):
                j = sl_map(code, t_hi)
                if j is None:
                    # 末日无行情(长期停牌), 按成本价了结, 不计盈亏
                    trades.append((code, h["buy_date"], d, h["cost_px"],
                                   h["cost_px"], h["shares"],
                                   t_hi - h["buy_i"], 0.0, 0.0,
                                   h["buy_fee"], "suspended", h.get("signal_date")))
                    continue
                px = close[j]
                if not np.isfinite(px):
                    px = h["cost_px"]
                sell_px = px * (1 - SLIPPAGE)
                gross = h["shares"] * sell_px
                fe = self._sell_fee(gross, d, exch[j])
                cash += gross - fe
                pnl = gross - fe - h["cost_total"]
                trades.append((code, h["buy_date"], d, h["cost_px"], sell_px,
                               h["shares"], t_hi - h["buy_i"], pnl,
                               pnl / h["cost_total"], h["buy_fee"] + fe,
                               "eod_liquidate", h.get("signal_date")))
            holdings.clear()
            equity[-1] = cash

        tr = pd.DataFrame(trades, columns=["code", "buy_date", "sell_date",
                                           "buy_px", "sell_px", "shares",
                                           "hold_days", "pnl", "ret", "fee",
                                           "reason", "signal_date"])
        return dict(config=cfg, name=cfg.name, dates=dates[t_lo:t_hi + 1],
                    equity=equity, positions=npos, trades=tr,
                    day_range=(t_lo, t_hi))

    def _mtm(self, holdings, close, t):
        if not holdings:
            return 0.0
        tot = 0.0
        for code, h in holdings.items():
            j = self.ix.lookup(code, t)
            px = close[j] if j is not None else h["cost_px"]
            if not np.isfinite(px):
                px = h["cost_px"]
            tot += h["shares"] * px
        return tot

    def _buy_fee(self, gross, date, exchange):
        comm = max(gross * COMMISSION_RATE, COMMISSION_MIN)
        tf = TRANSFER_FEE if (date >= TRANSFER_FEE_START or exchange == "SH") else 0.0
        return comm + gross * tf

    def _sell_fee(self, gross, date, exchange):
        comm = max(gross * COMMISSION_RATE, COMMISSION_MIN)
        st = STAMP_TAX_AFTER if date >= STAMP_CUT_DATE else STAMP_TAX_BEFORE
        tf = TRANSFER_FEE if (date >= TRANSFER_FEE_START or exchange == "SH") else 0.0
        return comm + gross * st + gross * tf


# ================================================================ 绩效指标
def _max_streak(mask):
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        if cur > best:
            best = cur
    return best


def perf_stats(res: Dict, rf: float = 0.02) -> Dict:
    eq = np.asarray(res["equity"], dtype=np.float64)
    dates = pd.DatetimeIndex(res["dates"])
    n = len(eq)
    ret = np.zeros(n)
    ret[1:] = eq[1:] / np.where(eq[:-1] == 0, np.nan, eq[:-1]) - 1
    ret = np.nan_to_num(ret)
    years = (dates[-1] - dates[0]).days / 365.25
    total_ret = eq[-1] / eq[0] - 1
    cagr = (eq[-1] / eq[0]) ** (1 / years) - 1 if years > 0 and eq[0] > 0 else np.nan
    ann_vol = np.std(ret, ddof=1) * np.sqrt(252)
    sharpe = (np.mean(ret) * 252 - rf) / ann_vol if ann_vol > 0 else np.nan
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    mdd = float(dd.min())
    calmar = cagr / abs(mdd) if mdd < 0 else np.nan
    out = dict(
        name=res["name"], n_days=n, years=round(years, 2),
        total_return=float(total_ret), cagr=float(cagr),
        ann_vol=float(ann_vol), sharpe=float(sharpe),
        max_drawdown=mdd,
        calmar=float(calmar) if np.isfinite(calmar) else np.nan,
        final_equity=float(eq[-1]), avg_positions=float(np.mean(res["positions"])),
        n_trades=0, win_rate=np.nan, payoff_ratio=np.nan,
        profit_factor=np.nan, wr_x_payoff=np.nan,
    )
    tr = res["trades"]
    if len(tr) > 0:
        wins = tr[tr["pnl"] > 0]
        losses = tr[tr["pnl"] <= 0]
        gw = float(wins["pnl"].sum())
        gl = float(-losses["pnl"].sum())
        wr = len(wins) / len(tr)
        aw = float(wins["ret"].mean()) if len(wins) else 0.0
        al = float(losses["ret"].mean()) if len(losses) else 0.0
        payoff = aw / abs(al) if al != 0 else np.nan
        out.update(
            n_trades=int(len(tr)),
            trades_per_year=float(len(tr) / years) if years > 0 else np.nan,
            win_rate=float(wr), avg_win_pct=aw, avg_loss_pct=al,
            payoff_ratio=float(payoff) if np.isfinite(payoff) else np.nan,
            wr_x_payoff=float(wr * payoff) if np.isfinite(payoff) else np.nan,
            profit_factor=float(gw / gl) if gl > 0 else np.nan,
            avg_hold_days=float(tr["hold_days"].mean()),
            avg_pnl=float(tr["pnl"].mean()),
            expectancy=float(tr["ret"].mean()),
            max_consec_loss=_max_streak(tr["pnl"].values <= 0),
            max_consec_win=_max_streak(tr["pnl"].values > 0),
            total_fee=float(tr["fee"].sum()),
            fee_to_grosswin=float(tr["fee"].sum() / gw) if gw > 0 else np.nan,
            fee_to_pnl=float(tr["fee"].sum() / tr["pnl"].sum())
            if tr["pnl"].sum() > 0 else np.nan,
        )
    out["_res"] = res
    return out


def yearly_returns(res) -> pd.Series:
    eq = pd.Series(res["equity"], index=pd.DatetimeIndex(res["dates"]))
    ye = eq.resample("YE").last()
    out = {}
    prev = eq.iloc[0]
    for d, v in ye.items():
        out[d.year] = v / prev - 1
        prev = v
    return pd.Series(out).sort_index()


def monthly_returns(res) -> pd.Series:
    eq = pd.Series(res["equity"], index=pd.DatetimeIndex(res["dates"]))
    me = eq.resample("ME").last()
    prev = pd.Series([eq.iloc[0]], index=[eq.index[0]])
    return pd.concat([prev, me]).pct_change().dropna()


if __name__ == "__main__":
    print("engine ok")
