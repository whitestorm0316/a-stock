#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v3b_lib.py —— V3B「A股价格超跌反转研究」共享计算库

设计要点（均为前几轮踩坑后固化的纪律）：
  1. 所有滚动/位移都**显式按股票分块**，绝不依赖面板既有行序
  2. 逐日截面十分位：D1 = 最弱 10%，D10 = 最强 10%（rank ascend + floor）
  3. 超额收益一律用**日加权**（先按日求均值，再对日度均值序列做 t 检验）
  4. 交易级统计同时输出中位数/胜率/盈亏比/Profit Factor（均值会被右偏分布误导）
  5. 市场基准一律用**日度链式**口径（expm1(Σlog(1+r))），不用重叠 H 日收益复利
"""
import os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
RAW = os.path.join(ROOT, "data", "raw")
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)

MIN_LIST = 120
PPY = 252.0          # 交易日年化约定（A股实际约 242~243，见 skill 陷阱说明）
RT_COST = 0.003      # 0.3% 双边（用户指定）


# ================================================================ 加载
def load_clean(extra_cols, min_list=MIN_LIST):
    """读面板并做统一清洗：非 ST、上市满 min_list 日、ret 非空、按(股票,日期)排序"""
    base = ["thscode", "date", "is_st_now", "days_since_list", "ret",
            "open_price", "close_price", "high_price", "low_price",
            "can_buy_open", "can_sell_open", "size_grp", "limit_pct"]
    cols = list(dict.fromkeys(base + list(extra_cols)))
    df = pd.read_parquet(os.path.join(PROC, "v2_panel.parquet"), columns=cols)
    m = (~df["is_st_now"].fillna(True)) & (df["days_since_list"].fillna(0) >= min_list) \
        & df["ret"].notna()
    df = df.loc[m].sort_values(["thscode", "date"], kind="stable").reset_index(drop=True)
    return df


def ctx(df):
    """构建分块索引与日历索引"""
    ud = np.sort(df["date"].unique())
    day_idx = np.searchsorted(ud, df["date"].values).astype(np.int64)
    code = df["thscode"].values
    chg = np.flatnonzero(code[1:] != code[:-1]) + 1
    n = len(df)
    return dict(
        n=n, code=code, ud=ud, day_idx=day_idx, n_days=len(ud),
        yr=pd.DatetimeIndex(df["date"].values).year.values,
        chg=chg,
        starts=np.concatenate([[0], chg]),
        ends=np.concatenate([chg, [n]]),
    )


# ================================================================ 分块原语
def roll_mean(x, C, W):
    """块内 W 日均值（含当日）。每块用前缀和，整体 O(n)"""
    x = np.asarray(x, np.float64)
    out = np.full(len(x), np.nan)
    for s, e in zip(C["starts"], C["ends"]):
        if e - s < W:
            continue
        cs = np.concatenate([[0.0], np.cumsum(x[s:e])])
        out[s + W - 1:e] = (cs[W:] - cs[:-W]) / W
    return out


def shift_block(x, C, k):
    """out[t] = x[t+k]，同股票内，跨块/越界为 nan。k 可正可负"""
    x = np.asarray(x, np.float64)
    n = len(x)
    out = np.full(n, np.nan)
    if k == 0:
        return x.copy()
    bound = np.zeros(n, np.int64)
    bound[C["chg"]] = 1
    cs = np.cumsum(bound)
    if k > 0:
        idx = np.arange(n - k)
        ok = cs[idx] == cs[idx + k]
        out[idx[ok]] = x[idx[ok] + k]
    else:
        m = -k
        idx = np.arange(n - m)
        ok = cs[idx] == cs[idx + m]
        out[idx[ok] + m] = x[idx[ok]]
    return out


def roll_shift_mean(x, C, W, k):
    """MA_W 的 k 日位移（用于斜率：k=1 → 前一日 MA）"""
    return shift_block(roll_mean(x, C, W), C, k)


# ================================================================ 截面
def _rank_within_np(v, d, nd):
    """块内平均秩（1 起）。双键排序 + int64 精确累加。

    ⚠️⚠️ 必须双键排序（主键 d，次键 v）。单键 argsort 会使同块元素分散，
        `arange(n) − 块起点` 得到负数，秩完全错误（实测 n=6000 时 maxdiff 5803）。
        详见 35_factor_ic.py 同名函数的说明。
    ⚠️ 性能：np.lexsort 在 1e7 行上单次约 10s；两次 stable argsort 约 3.6s。
    """
    n = len(v)
    o1 = np.argsort(v, kind="stable")
    order = o1[np.argsort(d[o1], kind="stable")]
    ds = d[order]
    vs = v[order]
    cntg = np.bincount(ds, minlength=nd)
    nb = len(cntg)
    starts_b = np.zeros(nb + 1, np.int64)
    starts_b[1:] = np.cumsum(cntg)
    pos = np.arange(n, dtype=np.int64) - starts_b[ds]
    newrun = np.empty(n, bool)
    newrun[0] = True
    if n > 1:
        newrun[1:] = (ds[1:] != ds[:-1]) | (vs[1:] != vs[:-1])
    run_id = np.cumsum(newrun) - 1
    idx_first = np.flatnonzero(newrun)
    run_sum = np.add.reduceat(pos, idx_first)
    run_cnt = np.diff(np.append(idx_first, n)).astype(np.int64)
    avg = run_sum.astype(np.float64) / run_cnt.astype(np.float64) + 1.0
    out = np.empty(n, np.float64)
    out[order] = avg[run_id]
    return out


def rank_pct_within(vals, C):
    """逐日截面百分位 0~1（平均秩 / 当日有效数）。向量化，替代 pandas groupby.rank。"""
    v = np.asarray(vals, np.float64)
    ok = np.isfinite(v)
    out = np.full(len(v), np.nan, np.float64)
    if ok.sum() == 0:
        return out
    d = C["day_idx"][ok]
    r = _rank_within_np(v[ok], d, C["n_days"])
    cnt = np.bincount(d, minlength=C["n_days"]).astype(np.float64)
    out[ok] = r / cnt[d]
    return out


def decile(vals, C, nb=10):
    """逐日截面分位箱号 1..nb。D1 = 最弱（最小），Dn = 最强"""
    r = rank_pct_within(vals, C)
    b = np.minimum(np.floor(r * nb), nb - 1.0) + 1.0
    b[~np.isfinite(r)] = np.nan
    return b


def cs_rank(vals, C):
    """逐日截面百分位 0~1"""
    s = pd.Series(np.asarray(vals, np.float64))
    return s.groupby(C["day_idx"]).rank(pct=True, method="average").values


# ================================================================ 市场基准
def market_oret(oret, C):
    """全A等权日度收益（含 NaN→0）"""
    d = C["day_idx"]
    ok = np.isfinite(oret)
    rsum = np.bincount(d[ok], weights=oret[ok], minlength=C["n_days"])
    cnt = np.bincount(d[ok], minlength=C["n_days"])
    return np.where(cnt > 0, rsum / np.maximum(cnt, 1), np.nan)


def oret_from(df, C):
    """【标准口径】可交易开盘→开盘收益

    oret[t]     = open[t+1]/open[t] - 1      （T 日开盘买入，T+1 开盘卖出）
    oret_sig[t] = oret[t+1]                  （T 日收盘信号 → T+1 开盘买入 → T+2 开盘卖出）

    ⚠️ 任何「T 日收盘生成信号」的策略回测都必须用 oret_sig。
       用 oret 会引入信号日之前的那一天（超跌股在该日通常继续下跌），
       实测在 ret5 D1 上使日度组合收益从 +0.0068% 变为 -1.30%（差 1.37pp）。
    """
    op = df["open_price"].values.astype(np.float64)
    oret = shift_block(op, C, 1) / op - 1.0
    return oret, shift_block(oret, C, 1)


def chain_fwd(mk_daily, C, H):
    """mkt_fwd_H[t] = expm1(cs[t+1+H]-cs[t+1])，与 fwd{H}[t] 口径对齐"""
    lg = np.log1p(np.nan_to_num(mk_daily, nan=0.0))
    cs = np.concatenate([[0.0], np.cumsum(lg)])
    nd = C["n_days"]
    out = np.full(nd, np.nan)
    hi = nd - 1 - H
    if hi > 0:
        t = np.arange(0, hi)
        out[t] = np.expm1(cs[t + 1 + H] - cs[t + 1])
    return out


# ================================================================ 统计
def exw_t(ret, mk, day_idx, min_n=40, n_days=None):
    """日加权超额（先按日求均值，再对日度均值做 t 检验）+ 观测加权均值

    ⚠️ 性能：原用 `pd.Series(e).groupby(day_idx).mean()` —— 在 1e7 行、
        320 次调用的循环里会累积到 40 分钟以上。
        改用 `np.bincount` 向量化后单次约 0.1s（快 30 倍以上），结果完全一致。
    """
    ret = np.asarray(ret, np.float64)
    mk = np.asarray(mk, np.float64)
    ok = np.isfinite(ret) & np.isfinite(mk)
    if ok.sum() < min_n:
        return None
    e = ret[ok] - mk[ok]
    d = day_idx[ok]
    if n_days is None:
        n_days = int(d.max()) + 1
    cnt = np.bincount(d, minlength=n_days).astype(np.float64)
    s = np.bincount(d, weights=e, minlength=n_days)
    has = cnt > 0
    dm = s[has] / cnt[has]                      # 每日超额均值
    if len(dm) < 5:
        return None
    sd = dm.std(ddof=1)
    t = dm.mean() / (sd / np.sqrt(len(dm))) if sd > 0 else np.nan
    return dict(n=int(ok.sum()), n_day=int(len(dm)),
                ex=float(ret[ok].mean()), exw=float(dm.mean()), t=float(t))


def trade_stats(r, cost=0.0):
    """交易级统计：均值/中位数/胜率/盈亏比/Profit Factor"""
    r = np.asarray(r, np.float64)
    r = r[np.isfinite(r)]
    if len(r) < 20:
        return None
    rn = r - cost
    pos = rn[rn > 0]
    neg = rn[rn < 0]
    tot_neg = -neg.sum()
    pf = float(pos.sum() / tot_neg) if tot_neg > 0 else np.nan
    payoff = float(pos.mean() / (-neg.mean())) if len(pos) and len(neg) and neg.mean() < 0 else np.nan
    return dict(n=int(len(rn)), mean=float(rn.mean()), median=float(np.median(rn)),
                win=float((rn > 0).mean()), payoff=payoff, pf=pf,
                p10=float(np.percentile(rn, 10)), p90=float(np.percentile(rn, 90)))


def ann_stats(net, n_days, periods_per_year=PPY):
    """由日度净收益序列算 CAGR / MDD / Sharpe

    ⚠️ 同时给出 ann_arith = mean*PPY（算术年化）。二者差异 = 波动拖累：
       日度再平衡等权组合在高波动下，几何 CAGR 会被系统性压低。
       当 ann_arith 与 cagr 符号相反时，说明该组合不可实现（纯波动拖累）。
    """
    net = np.asarray(net, np.float64)
    nav = np.cumprod(1.0 + net)
    years = n_days / periods_per_year
    cagr = nav[-1] ** (1.0 / years) - 1.0 if nav[-1] > 0 else -1.0
    dd = float((nav / np.maximum.accumulate(nav) - 1.0).min())
    sd = net.std(ddof=1)
    sharpe = float(net.mean() / sd * np.sqrt(periods_per_year)) if sd > 0 else np.nan
    return dict(cagr=float(cagr), mdd=dd, sharpe=sharpe,
                ann_arith=float(net.mean() * periods_per_year),
                vol=float(sd * np.sqrt(periods_per_year)))


def state_nav(mask, oret_sig, C):
    """状态组合：每日等权持有 mask 为真的股票，日度再平衡。

    ⚠️ 必须传入 **oret_sig**（= shift_block(oret, C, 1)），即「T+1 开盘 → T+2 开盘」。
       若误传 oret（T 开盘 → T+1 开盘），则衡量的是信号日之前已错过的那一天，
       在「超跌」类因子上会给出严重偏负的结果（实测差 1.37pp/日）。

    返回日度收益序列（未计成本的毛收益）与统计。"""
    ok = np.asarray(mask, bool) & np.isfinite(oret_sig)
    d = C["day_idx"]
    rsum = np.bincount(d[ok], weights=oret_sig[ok], minlength=C["n_days"])
    cnt = np.bincount(d[ok], minlength=C["n_days"])
    ret = np.where(cnt > 0, rsum / np.maximum(cnt, 1), 0.0)
    active = cnt > 0
    st = ann_stats(ret, C["n_days"])
    st["n_active_day"] = int(active.sum())
    st["avg_holdings"] = float(cnt[active].mean()) if active.any() else 0.0
    return st, ret, cnt


def yearly_table(ret_daily, C, label):
    """按年输出状态组合的毛收益"""
    rows = []
    yrs = pd.DatetimeIndex(C["ud"]).year.values
    for y in range(2015, 2027):
        s = yrs == y
        if s.sum() == 0:
            continue
        r = ret_daily[s]
        nav = np.cumprod(1.0 + r)
        rows.append(dict(variant=label, year=y, n_day=int(s.sum()),
                         ret=float(nav[-1] - 1.0),
                         mdd=float((nav / np.maximum.accumulate(nav) - 1.0).min())))
    return rows


# ================================================================ 交易模拟
def block_bounds_from_chg(chg, n):
    return np.concatenate([[0], chg]), np.concatenate([chg, [n]])


def build_sell_open(op, can_sell, C):
    """out[t] = 从 t 起(含 t)第一个可卖日的 open；无则 nan（跌停顺延）"""
    tmp = np.where(np.asarray(can_sell, bool), op, np.nan)
    blk = np.zeros(len(op), np.int64)
    blk[C["chg"]] = 1
    blk = np.cumsum(blk)
    return pd.Series(tmp).groupby(blk).transform(
        lambda x: x[::-1].ffill()[::-1]).values


def simulate_hold(sig, buy_open, sell_open, C, H):
    """信号 T 日生成 → T+1 开盘买入 → 持有 H 日 → T+1+H 开盘卖出。
    返回 (trade_ret, entry_day_idx)"""
    n = C["n"]
    bi = shift_block(buy_open, C, 1)        # T+1 开盘买价
    si = shift_block(sell_open, C, 1 + H)   # T+1+H 开盘卖价
    r = si / bi - 1.0
    p = np.flatnonzero(np.asarray(sig, bool) & np.isfinite(r))
    return r[p], C["day_idx"][p], p


def portfolio_nav(sig, day_idx, n_days, oret_sig, s_days, C, rt_cost=RT_COST):
    """日度再平衡等权组合；成本按持仓天数摊薄（单笔整个持有期恰好承担 rt_cost）

    ⚠️ 必须传入 **oret_sig**（T+1 开盘 → T+2 开盘），理由同 state_nav。
       持仓第 j 日（j=0 起）的收益 = oret_sig[sel + j]。
    """
    n = C["n"]
    pos = np.flatnonzero(np.asarray(sig, bool) & np.isfinite(s_days) & (s_days >= 1))
    rsum = np.zeros(n_days)
    csum = np.zeros(n_days)
    cnt = np.zeros(n_days, np.int64)
    maxj = int(min(np.nanmax(s_days) if len(pos) else 0, 60))
    for j in range(maxj):
        sel = pos[s_days[pos] > j]
        if len(sel) == 0:
            continue
        q = sel + j
        good = q < n
        q, sel = q[good], sel[good]
        r = oret_sig[q]
        ok = np.isfinite(r)
        q, sel, r = q[ok], sel[ok], r[ok]
        d = day_idx[sel] + j
        inb = d < n_days
        d, r, sel = d[inb], r[inb], sel[inb]
        np.add.at(rsum, d, r)
        np.add.at(cnt, d, 1)
        np.add.at(csum, d, rt_cost / s_days[sel])
    act = cnt > 0
    net = np.where(act, (rsum - csum) / np.maximum(cnt, 1), 0.0)
    return net, cnt


# ================================================================ 容量约束组合
def plan_positions(mask, day_idx, n_days, hold, max_pos=10, max_new=3,
                   pick=None, pick_asc=True, rng_seed=42,
                   stock_of_row=None, dedupe=True):
    """在**同时持仓上限 + 每日新开仓上限**下，逐日顺序决定「实际持有」哪些仓位。

    为什么需要这个
    --------------
    `portfolio_nav` 是「不限仓位」口径：某日有 500 个信号就等权持有 500 只。
    实盘做不到（资金不够、也没人管得过来）。本函数按真实下单节奏模拟：

      每个交易日 d：
        1. 先处理**到期卖出**（按实际建仓日 + hold 计算），释放仓位；
        2. 再看当日新信号，按 `pick` 排序取前 `max_new` 个；
        3. 若「已持仓 + 新建仓」会超过 `max_pos`，则**截断**（信号被丢弃）；
        4. 被丢弃的信号**不再补买**（保守假设：机会过了就是过了）。

    ⚠️ 关键纪律：**排序必须在信号日 T 的信息集内完成**（用 T 日收盘可见的字段），
       绝不能用到未来收益，否则又是前视偏差。

    ⚠️ **同一只票不得重复持仓**（`dedupe=True`，默认开启）
    --------------------------------------------------------
    已持有某只票、且这笔仓位**还没到期卖出**时，即使它明天又出信号，也**不再买入**；
    等这笔仓位卖出之后，该票恢复可交易。传 `stock_of_row` 才生效。

    为什么必要：不约束的话，一只持续满足条件的票会在 20 个交易日里天天被买入，
    同时挂着十几个同一标的的仓位 —— 这既不是真实的下单逻辑（等于对单一标的加杠杆、
    集中风险），也会让「同时持仓 10 只」的约束失去意义（实际只有 3~4 只不同的票）。

    注意实现上的取舍：**先剔除重复、再按 max_new / 仓位余量截断**。
    也就是撞上已持仓的候选时往下顺延往下一位候选 —— 模拟真人「这只已经拿了，买下一只」，
    而不是白白浪费当天的买入额度。被判为重复的信号计入 `n_drop_dup`。

    参数
    ----
    mask      : bool[n]   信号掩码（面板行级）
    day_idx   : int[n]    每行的交易日序号
    n_days    : int       总交易日数
    hold      : int       固定持有期（交易日）
    max_pos   : int       同时持仓上限（0/None = 不限）
    max_new   : int       每日最多新建仓数（0/None = 不限）
    pick      : float[n]  同日候选的排序键；None 表示按面板原始顺序
    pick_asc  : bool      True = 键小者优先（如「最超跌优先」）；
                          False = 键大者优先（如「成交额大者优先」）
    stock_of_row : int[n] 每行所属股票的序号（同一只票同值）。用于「不得重复持仓」，
                          传 None 则该约束失效
    dedupe    : bool      True = 同一只票未平仓期间不得再买（默认）

    返回
    ----
    dict(
      holds      : list[(entry_day, exit_day, row_idx)]  实际建仓记录
      n_drop     : int      因容量限制/重复持仓被丢弃的信号数
      n_drop_dup : int      其中「已持有未平仓而被跳过」的部分
      n_signal   : int      总信号数
    )

    说明：这里只做**仓位筛选**，不在这里算收益 —— 收益统一由
    `nav_from_holds()` 按「等权、逐日市值」口径计算，避免两处算法漂移。
    """
    mask = np.asarray(mask, bool)
    hold = int(hold)
    unlimited_pos = (max_pos is None) or (int(max_pos) <= 0)
    unlimited_new = (max_new is None) or (int(max_new) <= 0)
    max_pos = int(max_pos) if not unlimited_pos else 10 ** 9
    max_new = int(max_new) if not unlimited_new else 10 ** 9

    # ---- 按日归集信号行号（保持面板原始顺序，确保可复现）
    sig_rows = np.flatnonzero(mask & np.isfinite(day_idx))
    if len(sig_rows) == 0:
        return dict(holds=[], n_drop=0, n_drop_dup=0, n_signal=0)

    # 按 (日, 原始顺序) 分组
    order = np.lexsort((sig_rows, day_idx[sig_rows]))
    sig_rows = sig_rows[order]
    sig_days = day_idx[sig_rows]
    # 每日信号的起止位置
    day_start = np.searchsorted(sig_days, np.arange(n_days), side="left")
    day_end = np.searchsorted(sig_days, np.arange(n_days), side="right")

    rng = np.random.default_rng(rng_seed)
    # 预生成随机数（随机排序规则用），与行号一一对应放在 dict 里查
    rand_key = None
    if pick is not None and str(pick) == "__random__":
        rand_key = rng.random(len(mask))

    # 「不得重复持仓」只在拿到行→股票映射时才生效
    dedupe = bool(dedupe) and stock_of_row is not None
    if dedupe:
        stock_of_row = np.asarray(stock_of_row)

    holds = []              # (entry_day, exit_day, row_idx)
    open_exits = []         # 已建仓的到期日列表（用于快速释放）
    open_sids = []          # 与 open_exits 一一对应：该仓位持有的股票序号
    n_drop = 0
    n_drop_dup = 0          # 其中「已持有未平仓」而被跳过者
    occupied = 0            # 当前持仓数（= 尚未到期的 holds 数）

    for d in range(n_days):
        # ---- 1. 先释放到期仓位（exit_day <= d 视为今日开盘前已卖出）
        #        卖出后该股票立刻恢复可交易 —— 「同仓不得二次交易」到此为止
        if open_exits:
            if dedupe:
                # ⚠️ 必须同步过滤 open_sids：不开启去重时 open_sids 恒为空，
                #    若统一走 zip 会得到空序列 → 持仓被误判全部到期 → 上限失效
                still_e, still_s = [], []
                for e, s in zip(open_exits, open_sids):
                    if e > d:
                        still_e.append(e)
                        still_s.append(s)
                open_exits, open_sids = still_e, still_s
                occupied = len(still_e)
            else:
                open_exits = [e for e in open_exits if e > d]
                occupied = len(open_exits)
        # ---- 2. 今日新信号
        a, b = day_start[d], day_end[d]
        if b <= a:
            continue
        rows = sig_rows[a:b]
        # ---- 3. 排序（只在 T 日信息集内）
        if pick is not None:
            if rand_key is not None:
                k = rand_key[rows]
            else:
                k = np.asarray(pick)[rows]
            # nan 排到最后（无法比较的候选不优先）
            finite = np.isfinite(k)
            ord_idx = (np.lexsort((rows, -k if not pick_asc else k))
                       if finite.all() else
                       np.concatenate([np.flatnonzero(finite)[
                           np.lexsort((rows[finite], -k[finite] if not pick_asc else k[finite]))],
                           np.flatnonzero(~finite)]))
            rows = rows[ord_idx]
        # ---- 4. 剔除「已持有且未平仓」的股票；卖出后才回到候选池
        if dedupe and open_sids:
            held = set(open_sids)
            kept = [int(r) for r in rows if stock_of_row[r] not in held]
            n_drop_dup += len(rows) - len(kept)
            n_drop += len(rows) - len(kept)
            rows = kept
            if not rows:
                continue
        # ---- 5. 容量截断
        room = max_pos - occupied
        if room <= 0:
            n_drop += len(rows)
            continue
        take = min(len(rows), max_new, room)
        n_drop += len(rows) - take
        for r in rows[:take]:
            # 实际建仓日 = 信号日 + 1（T+1 开盘买入）
            entry = int(d) + 1
            exit_d = entry + hold
            holds.append((entry, exit_d, int(r)))
            open_exits.append(exit_d)
            if dedupe:
                open_sids.append(int(stock_of_row[r]))
            occupied += 1
    return dict(holds=holds, n_drop=int(n_drop), n_signal=int(len(sig_rows)),
                n_drop_dup=int(n_drop_dup),
                pick=str(pick) if pick is not None else None)


def nav_from_holds(holds, day_idx, n_days, oret_sig, C,
                   rt_cost=RT_COST, hold=None, capital_slots=None):
    """把「实际建仓记录」转成日度净值序列（等权、逐日按市值加权）。

    ⚠️⚠️ 两种口径，**必须弄清在用哪个**（这是容量约束回测最容易搞错的地方）：

    · `capital_slots=None` → **已投资金口径**
        日收益 = 开仓头寸收益的等权平均 = `Σr / cnt`
        与 `portfolio_nav` 完全同构，**可与「不限仓位」基线直接对比**。
        但它假设「闲置的仓位也在赚同样的钱」，会**高估**真实账户收益。

    · `capital_slots=10` → **账户总资金口径**
        日收益 = `Σr / capital_slots`
        即把资金等分成 10 份，每份对应一个仓位；未开仓的份额赚 0（现金）。
        **这才是用户账户里真实看到的收益率**，但它被资金利用率摊薄。

    两个口径的比值就是资金利用率 `cnt / capital_slots`。

    其余口径（与 portfolio_nav 对齐，保证可比）
    ----------------------------------------
    · 每笔仓位**等权**入场；成本按持仓天数摊薄，单笔整个持有期恰好承担 `rt_cost`；
    · 空仓日收益 = 0（现金），且计入总交易日。

    持仓第 j 日（j=0 起，即 entry+j）的收益用 `oret_sig[row + j]`
    （row 是信号日行号；entry = 信号日+1，故 entry+j 开盘 → 下一日开盘）。

    ⚠️ 为什么用 oret_sig：同 portfolio_nav / state_nav ——
       持仓期内每天的收益必须是「该日开盘→次日开盘」，错用 oret 会偏一天。
    """
    n = C["n"]
    rsum = np.zeros(n_days)
    csum = np.zeros(n_days)
    cnt = np.zeros(n_days, np.int64)
    if not holds:
        return np.zeros(n_days), cnt

    holds = np.asarray(holds, np.int64)
    entry = holds[:, 0]
    exit_d = holds[:, 1]
    row = holds[:, 2]
    if hold is None:
        H = int((exit_d - entry).max())
    else:
        H = int(hold)

    for j in range(H):
        # 仅计入「第 j 日仍在持有」的仓位：entry+j < exit_d
        alive = (entry + j) < exit_d
        if not alive.any():
            continue
        rr = row[alive]
        q = rr + j                       # 面板行号
        good = q < n
        rr, q = rr[good], q[good]
        r = oret_sig[q]
        ok = np.isfinite(r)
        r = r[ok]
        d = day_idx[rr[ok]] + j
        inb = d < n_days
        d, r = d[inb], r[inb]
        np.add.at(rsum, d, r)
        np.add.at(cnt, d, 1)
        np.add.at(csum, d, rt_cost / max(H, 1))
    # 分母：已投资金口径用 cnt；账户口径用固定的 capital_slots。
    # 分子在两种口径下**完全相同**：当日所有持仓的收益之和 − 当日所有持仓的成本之和
    # （csum 每天累积 cnt × rt_cost/H，即 H 日恰好摊完 rt_cost）。只换分母。
    denom = np.maximum(cnt, 1) if capital_slots is None \
        else np.full(n_days, float(capital_slots))
    act = cnt > 0
    net = np.where(act, (rsum - csum) / denom, 0.0)
    return net, cnt


def capacity_stats(plan, n_days, cnt, net, nd_active=None):
    """容量相关指标：信号丢弃率、满仓天数、平均持仓、资金利用率"""
    holds = plan["holds"]
    n_sig = max(plan["n_signal"], 1)
    return dict(
        n_hold=len(holds),
        n_signal=plan["n_signal"],
        n_drop=plan["n_drop"],
        drop_pct=float(plan["n_drop"]) / n_sig,
        fill_pct=float(len(holds)) / n_sig,
        avg_pos=float(cnt[cnt > 0].mean()) if (cnt > 0).any() else 0.0,
        max_pos_seen=int(cnt.max()) if len(cnt) else 0,
        empty_days=int((cnt == 0).sum()),
        empty_pct=float((cnt == 0).mean()),
        active_pct=float((cnt > 0).mean()),
        # 「已持有未平仓」而被跳过的信号数（同一只票不得重复持仓）
        n_drop_dup=int(plan.get("n_drop_dup", 0)),
    )
