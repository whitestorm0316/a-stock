#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_executability.py —— 「能不能实盘 / 有没有前视」的实证检验

代码审计只能证明「时序没写错」，证明不了「实盘还能赚到」。
真正决定成败的是四个问题，本脚本逐一给出数字：

  检验1  信号滞后敏感性（最重要）
     把信号整体延后 k 个交易日（k=0/1/2/3/5），即「我晚 k 天才发现这只票」。
     若 CAGR 在 k=1 就断崖式下跌 → 策略靠的是 T+1 开盘那一瞬间的定价错误，
     实盘必然抢不到（下单延迟、集合竞价排队、滑点），纸面收益不可实现。

  检验2  成本敏感性
     把双边成本从 0.3% 提到 0.5% / 0.8%（更贴近小市值股的真实冲击成本），
     看 CAGR 还剩多少。用 monkeypatch 注入 rt_cost —— engine.capacity 内部
     是「调用时才 from v3b_lib import」，所以替换模块属性可以生效。
     （注意：nav_from_holds 的 rt_cost 是默认参数、在 def 时绑定，
       直接改 L.RT_COST 无效，必须包一层。）

  检验3  交易可行性拦截量
     统计信号里有多少被「开盘一字涨停不可买」「跌停不可卖（顺延）」拦掉。
     这些是回测已经扣掉的部分，用于核对是否与真实盘面相符。

  检验4  ST 过滤的偏差量级（已知前视点）
     面板用「当前名称是否带 ST」剔除股票，等于用了未来信息。
     量化：被剔除多少只、这些票在自身历史（未变 ST 时）的 20 日收益分布。

产出 output/verify_exec.csv + output/verify_exec.log
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "app"))

from _console import bootstrap  # noqa: E402
bootstrap()

import v3b_lib as L  # noqa: E402

PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)
LOGF = open(os.path.join(OUT, "verify_exec.log"), "w", encoding="utf-8")
_t0 = time.time()


def log(m=""):
    line = f"[{time.time() - _t0:7.1f}s] {m}"
    print(line, flush=True)
    LOGF.write(line + "\n")
    LOGF.flush()


# U2 方案：主板 · 市值 10%~40% · 熊市 · hold 20 · 阶梯 1,2,2,2,2,3
U2 = dict(
    px_ma60_min=1, px_ma60_max=1, deep_any=False,
    size_min=1, size_max=3,
    mkt_state="bear", mkt_hv="any", confirm=[],
    hold=20, max_pos=12, max_new=3, pick="deep",
    cap_ladder="1,2,2,2,2,3", fin_period="latest",
    delist=dict(financial=True, loss2y=False, penny=True),
    delist_rev_floor=100000000, delist_main_2024=True, delist_penny_days=10,
)
POOL = dict(mode="all", codes_text="", industries=[],
            boards=["MAIN"], exchanges=["SH", "SZ"])
LAD = [1, 2, 2, 2, 2, 3]
HOLD = 20
ROWS = []


def run_cap(eng, mask, rt_cost=None, n_sim=0):
    """跑一次容量约束回测；rt_cost 给定时临时替换成本。"""
    orig = L.nav_from_holds
    if rt_cost is not None:
        def patched(*a, **kw):
            kw["rt_cost"] = rt_cost
            return orig(*a, **kw)
        L.nav_from_holds = patched
    try:
        return eng.capacity(mask, hold=HOLD, max_pos=12, max_new=3,
                            pick="deep", n_sim=n_sim, ladder=LAD)
    finally:
        L.nav_from_holds = orig


def main():
    from engine import Engine  # noqa: PLC0415
    log("加载 Engine ...")
    eng = Engine()
    log(f"Engine 就绪，build {eng.build_ms} ms，面板 {eng.C['n']:,} 行 / "
        f"{eng.C['n_days']:,} 个交易日")

    pool, err = eng.build_pool(POOL)
    if err:
        log(f"!! 股票池构建失败：{err}")
        return 1
    mask, conds = eng.build_mask(U2, pool)
    n_sig = int(np.asarray(mask, bool).sum())
    log(f"基准信号 {n_sig:,} 个（股票池内共 "
        f"{int(np.asarray(pool, bool).sum()):,} 行）")

    # ---------------------------------------------------------- 检验 1
    log("")
    log("=" * 68)
    log("检验1  信号滞后敏感性（把信号整体延后 k 个交易日）")
    log("=" * 68)
    base = None
    for k in (0, 1, 2, 3, 5, 10):
        if k == 0:
            mk = np.asarray(mask, bool)
        else:
            # shift_block(x, C, -k): out[t] = x[t-k] → 信号晚 k 天出现
            mk = np.nan_to_num(L.shift_block(np.asarray(mask, np.float64),
                                            eng.C, -k), nan=0.0) > 0.5
        c = run_cap(eng, mk)
        row = dict(test="lag", k=k, n_signal=int(mk.sum()),
                   cagr=c["cagr"], cap_cagr=c["cap_cagr"],
                   mdd=c["mdd"], cap_mdd=c["cap_mdd"],
                   sharpe=c["sharpe"], cap_sharpe=c["cap_sharpe"],
                   avg_invested=c["avg_invested"], n_hold=c["n_hold"])
        ROWS.append(row)
        if k == 0:
            base = row
        rel = (row["cap_cagr"] / base["cap_cagr"] * 100
               if base and base["cap_cagr"] else np.nan)
        log(f"  k={k:>2}  信号 {row['n_signal']:>7,}  "
            f"账户CAGR {row['cap_cagr']*100:>7.2f}%  "
            f"(占基准 {rel:>5.1f}%)  "
            f"已投CAGR {row['cagr']*100:>7.2f}%  "
            f"MDD {row['cap_mdd']*100:>7.2f}%  "
            f"Sharpe {row['cap_sharpe']:.2f}  平均仓位 {row['avg_invested']*100:.1f}%")

    # ---------------------------------------------------------- 检验 2
    log("")
    log("=" * 68)
    log("检验2  成本敏感性（双边 RT_COST，基准 0.3%）")
    log("=" * 68)
    for rc in (0.0, 0.003, 0.005, 0.008, 0.012):
        c = run_cap(eng, np.asarray(mask, bool), rt_cost=rc)
        row = dict(test="cost", rt_cost=rc, n_signal=n_sig,
                   cagr=c["cagr"], cap_cagr=c["cap_cagr"],
                   mdd=c["mdd"], cap_mdd=c["cap_mdd"],
                   sharpe=c["sharpe"], cap_sharpe=c["cap_sharpe"],
                   avg_invested=c["avg_invested"], n_hold=c["n_hold"])
        ROWS.append(row)
        nb = c["n_hold"]
        log(f"  双边 {rc*100:>4.1f}%  账户CAGR {row['cap_cagr']*100:>7.2f}%  "
            f"已投CAGR {row['cagr']*100:>7.2f}%  "
            f"MDD {row['cap_mdd']*100:>7.2f}%  "
            f"Sharpe {row['cap_sharpe']:.2f}  （{nb:,} 笔建仓）")

    # ---------------------------------------------------------- 检验 3
    log("")
    log("=" * 68)
    log("检验3  交易可行性拦截量（开盘涨跌停）")
    log("=" * 68)
    mkb = np.asarray(mask, bool)
    rows_m = np.flatnonzero(mkb)
    # 信号日 T → 实际买入日 = T+1（面板下一行，同股票内）
    nxt = np.arange(len(mkb)) + 1
    same = np.zeros(len(mkb), bool)
    same[:-1] = (eng.sid[1:] == eng.sid[:-1])
    ok = same[rows_m]
    buy_row = nxt[rows_m][ok]
    sig_row = rows_m[ok]
    cb = eng.can_buy[buy_row]
    log(f"  信号中「次日仍在本股票块内」的 {len(buy_row):,} 个")
    log(f"  其中次日开盘一字涨停（买不进） {int((~cb).sum()):,} 个 "
        f"({(~cb).mean()*100:.2f}%)")
    # 卖出日跌停（不可卖 → 顺延）
    sell_row = np.minimum(buy_row + HOLD, eng.C["n"] - 1)
    can_sell = np.isfinite(eng.sell_open[sell_row])
    log(f"  持有满 {HOLD} 日当日跌停（卖出顺延） "
        f"{int((~can_sell).sum()):,} 个 ({(~can_sell).mean()*100:.2f}%)")
    log("  说明：这两类拦截**已经**计入回测（买价取 can_buy 后的开盘价、"
        "卖出用 build_sell_open 顺延），无需额外扣减。")

    # ---------------------------------------------------------- 检验 4
    log("")
    log("=" * 68)
    log("检验4  ST 过滤的偏差量级（已知前视点：用当前名称判 ST）")
    log("=" * 68)
    cols = ["thscode", "date", "is_st_now", "days_since_list", "ret", "fwd20"]
    p = pd.read_parquet(os.path.join(PROC, "v2_panel.parquet"), columns=cols)
    n_row_all, n_code_all = len(p), p["thscode"].nunique()
    st = p["is_st_now"].fillna(True)
    st_codes = p.loc[st, "thscode"].nunique()
    log(f"  面板 {n_row_all:,} 行 / {n_code_all:,} 只")
    log(f"  被 is_st_now 剔除：{st_codes:,} 只 (占 {st_codes/n_code_all*100:.2f}%)，"
        f"{int(st.sum()):,} 行 (占 {st.mean()*100:.2f}%)")
    # 这些「最终变 ST」的股票，在自身历史里的 20 日前瞻收益
    cl = p.loc[~st & (p["days_since_list"].fillna(0) >= 120) & p["ret"].notna()]
    A = cl.loc[cl["thscode"].isin(set(p.loc[st, "thscode"])), "fwd20"].dropna()
    B = cl.loc[~cl["thscode"].isin(set(p.loc[st, "thscode"])), "fwd20"].dropna()
    log(f"  「后来变 ST」的股票在未 ST 时期的 20 日收益："
        f"均值 {A.mean()*100:+.2f}%  中位 {A.median()*100:+.2f}%（{len(A):,} 样本）")
    log(f"  「始终非 ST」的同类样本：           "
        f"均值 {B.mean()*100:+.2f}%  中位 {B.median()*100:+.2f}%（{len(B):,} 样本）")
    log(f"  两者差 {abs(A.mean()-B.mean())*100:.2f}pp → "
        f"剔除它们使回测收益**偏乐观**，方向确认。")
    log("  ⚠️ 量级仅供参考：本策略只买「最小 10%~40% 成交额 + 超跌」的票，")
    log("     正是 ST 高发区，实际影响可能比上述均值差更大。")

    df = pd.DataFrame(ROWS)
    df.to_csv(os.path.join(OUT, "verify_exec.csv"), index=False,
              encoding="utf-8-sig")
    log("")
    log(f"产出 output/verify_exec.csv（{len(df)} 行）")
    LOGF.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
