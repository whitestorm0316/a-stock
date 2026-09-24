#!/usr/bin/env python3
"""
04_strategies.py — Strategy A~F 与参数化变体 (全量向量化)

重要事实: 因 MACD柱 = 2 x (DIF - DEA),
  "MACD柱由负转正"  ⟺  "DIF 上穿 DEA (金叉)"
两者是同一事件的两种表述, 因此原需求中的买点1与买点2在 MACD 维度完全等价,
差异只来自附加的量能条件。本模块据此统一实现。

所有信号均在 T 日收盘可得, 由引擎在 T+1 开盘执行。
"""
import numpy as np
from typing import Dict

EPS = 1e-12


def get(A, col):
    return A[col].astype(np.float64)


# --------------------------------------------------------------------------
# Strategy A: MACD 基础 (基准)
# --------------------------------------------------------------------------
def strat_A(A, p=None):
    """买入: DIF 上穿 DEA; 卖出: DIF 下穿 DEA"""
    sig = A["golden_cross"]
    ex = A["death_cross"]
    return sig, ex, "DIF上穿DEA买入 / DIF下穿DEA卖出"


# --------------------------------------------------------------------------
# Strategy B: MACD + 量能
# --------------------------------------------------------------------------
def strat_B(A, p=None):
    """买入: 金叉 + VOL5>VOL20 + 当日量>VOL20*th; 卖出: 死叉"""
    p = p or {}
    th = p.get("vr_th", 1.0)
    sig = (A["golden_cross"] & (get(A, "vol5_vol20") > 1.0)
           & (get(A, "vol_ratio") > th))
    ex = A["death_cross"]
    return sig, ex, f"MACD金叉 + VOL5>VOL20 + VolumeRatio>{th}"


# --------------------------------------------------------------------------
# Strategy C: MACD + 量能 + 趋势
# --------------------------------------------------------------------------
def strat_C(A, p=None):
    """买入: MACD翻红 + Close>MA + MA多头 + 量能; 卖出: 死叉 或 破MA"""
    p = p or {}
    th = p.get("vr_th", 1.1)
    ma_mid = p.get("ma_mid", "ma20")
    ma_long = p.get("ma_long", "ma60")
    exit_mode = p.get("exit_mode", "both")
    c = get(A, "close_price")
    sig = (A["hist_cross_up"]
           & (get(A, "dif") > get(A, "dea"))
           & (c > get(A, ma_mid))
           & (get(A, ma_mid) > get(A, ma_long))
           & (get(A, "vol5_vol20") > 1.0)
           & (get(A, "vol_ratio") > th))
    if exit_mode == "death":
        ex = A["death_cross"]
    elif exit_mode == "ma":
        ex = c < get(A, ma_mid)
    else:
        ex = A["death_cross"] | (c < get(A, ma_mid))
    return sig, ex, (f"MACD柱翻红+Close>{ma_mid}+{ma_mid}>{ma_long}"
                     f"+VOL5>VOL20+VolumeRatio>{th}; 退出={exit_mode}")


# --------------------------------------------------------------------------
# Strategy D: 上涨趋势中的缩量回调买入
# --------------------------------------------------------------------------
def strat_D(A, p=None):
    p = p or {}
    tol = p.get("pullback_tol", 0.05)
    vr_th = p.get("vr_th", 1.1)
    ma_mid = p.get("ma_mid", "ma20")
    ma_long = p.get("ma_long", "ma60")
    require_dry = p.get("require_dry", True)

    c = get(A, "close_price")
    mm = get(A, ma_mid)
    ml = get(A, ma_long)
    ma5 = get(A, "ma5")
    hist = get(A, "hist")
    hist_prev = get(A, "hist_prev")
    px_ma = np.abs(get(A, "px_ma20_pct"))
    vr = get(A, "vol_ratio")
    v5v20 = get(A, "vol5_vol20")

    uptrend = (mm > ml) & (c > ml) & (get(A, "dif") > 0)
    near_ma = px_ma <= tol
    macd_turn_up = (hist > hist_prev) & (hist > -0.5)
    reclaim5 = c > ma5
    vol_up = vr > vr_th
    sig = uptrend & near_ma & macd_turn_up & reclaim5 & vol_up
    if require_dry:
        # 回调期缩量: 5日均量低于20日均量
        sig &= (v5v20 < 1.0)
    ex = A["death_cross"] | (c < mm)
    return sig, ex, (f"{ma_mid}>{ma_long} + 缩量回踩{ma_mid}(±{tol:.0%}) + "
                     f"MACD柱转强 + 站上MA5 + 放量>{vr_th}")


# --------------------------------------------------------------------------
# Strategy E: 放量突破
# --------------------------------------------------------------------------
def strat_E(A, p=None):
    p = p or {}
    th = p.get("vr_th", 1.5)
    lb = p.get("lookback", 20)
    c = get(A, "close_price")
    sig = ((c > get(A, f"high{lb}_prev"))
           & (get(A, "hist") > 0)
           & (get(A, "dif") > get(A, "dea"))
           & (get(A, "vol_ratio") > th))
    ex = (c < get(A, "ma20")) | A["death_cross"]
    return sig, ex, f"Close>前{lb}日最高 + MACD多头 + VolumeRatio>{th}; 破MA20或死叉"


# --------------------------------------------------------------------------
# Strategy F: 底背离 (左侧 vs 右侧)
# --------------------------------------------------------------------------
def strat_F(A, p=None):
    p = p or {}
    mode = p.get("mode", "right")
    require_dry = p.get("require_dry", False)
    vr_th = p.get("vr_th", 0.0)
    if mode == "right":
        sig = A["dv_bull_recent"] & A["hist_cross_up"]
    else:
        sig = A["dv_bull"]
    if require_dry:
        sig = sig & (get(A, "vol5_vol20") < 1.0)
    if vr_th and vr_th > 0:
        sig = sig & (get(A, "vol_ratio") > vr_th)
    ex = A["death_cross"] | (get(A, "close_price") < get(A, "ma20"))
    return sig, ex, (f"底背离 mode={mode}"
                     + ("+缩量" if require_dry else "")
                     + (f"+VolumeRatio>{vr_th}" if vr_th else ""))


# --------------------------------------------------------------------------
# Strategy G: MACD翻红 + 放量 (用户第十二节核心组合)
# --------------------------------------------------------------------------
def strat_G(A, p=None):
    p = p or {}
    th = p.get("vr_th", 1.1)
    sig = A["hist_cross_up"] & (get(A, "vol_ratio") > th)
    ex = A["death_cross"]
    return sig, ex, f"MACD柱由负转正 + VolumeRatio>{th}"


# --------------------------------------------------------------------------
# Strategy H: 仅收盘价突破(无MACD) — 用于归因对照
# --------------------------------------------------------------------------
def strat_H(A, p=None):
    p = p or {}
    th = p.get("vr_th", 1.5)
    c = get(A, "close_price")
    sig = (c > get(A, "high20_prev")) & (get(A, "vol_ratio") > th)
    ex = c < get(A, "ma20")
    return sig, ex, f"纯突破: Close>前20日最高 + VolumeRatio>{th}"


# --------------------------------------------------------------------------
# Strategy I: 仅趋势(无MACD无量能) — 用于归因对照
# --------------------------------------------------------------------------
def strat_I(A, p=None):
    c = get(A, "close_price")
    ma20 = get(A, "ma20")
    ma60 = get(A, "ma60")
    ma5 = get(A, "ma5")
    sig = (ma20 > ma60) & (c > ma20) & (ma5 > ma20)
    ex = c < ma20
    return sig, ex, "纯趋势: MA20>MA60 + Close>MA20 + MA5>MA20"


REGISTRY: Dict[str, callable] = {
    "A": strat_A, "B": strat_B, "C": strat_C, "D": strat_D,
    "E": strat_E, "F": strat_F, "G": strat_G, "H": strat_H, "I": strat_I,
}


def build(strategy: str, params=None):
    fn = REGISTRY[strategy]
    return fn, params
