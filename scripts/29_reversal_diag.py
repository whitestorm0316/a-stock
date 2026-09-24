#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
29_reversal_diag.py —— 「超跌+极端缩量」反转策略的诊断与归因

28 脚本已证明用户规格的策略全线为负。本脚本回答三个问题:
  Q1 用户的「反转市」前提是否成立? —— 逐日截面十分位检验各维度的单调性
  Q2 为什么「MACD 柱超跌」不是买点? —— hist_z 十分位 + 邻域
  Q3 唯一正线索(价格型深度超跌)是真的吗? —— 邻域稳定性 + 分段 + 多重检验

产出:
  output/v3_decile.csv        各维度逐日截面十分位 → 前向超额
  output/v3_alt_def.csv       「超跌」不同定义的组合表现
  output/v3_neigh.csv         阈值邻域稳定性
  output/v3_rev_year.csv      分年度
  output/v3_diag_run.log
"""
import os
import time
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")
os.makedirs(OUT, exist_ok=True)
LOGF = open(os.path.join(OUT, "v3_diag_run.log"), "w", encoding="utf-8")
_t0 = time.time()


def log(m):
    line = f"[{time.time()-_t0:7.1f}s] {m}"
    print(line, flush=True)
    LOGF.write(line + "\n")
    LOGF.flush()


HZ = [5, 10, 20, 60]


def main():
    cols = ["thscode", "date", "is_st_now", "days_since_list", "ret",
            "hist_z", "dif", "volz20", "vr20m", "rvol20", "vt_5_20",
            "ret20", "ret5", "ret60", "hv20", "atr_pct", "size_grp",
            "px_ma20_pct", "px_ma60_pct", "px_h60", "rs_mkt20"] + \
           [f"fwd{H}" for H in HZ]
    df = pd.read_parquet(os.path.join(PROC, "v2_panel.parquet"), columns=cols)
    m = (~df["is_st_now"].fillna(True)) & (df["days_since_list"].fillna(0) >= 120) \
        & df["ret"].notna()
    df = df.loc[m].sort_values(["thscode", "date"], kind="stable").reset_index(drop=True)
    n = len(df)
    log(f"clean {n:,} 行 / {df['thscode'].nunique():,} 只")

    udates = np.sort(df["date"].unique())
    day_idx = np.searchsorted(udates, df["date"].values).astype(np.int64)
    n_days = len(udates)
    yr = pd.DatetimeIndex(df["date"].values).year.values
    TR = yr <= 2022
    log(f"交易日 {n_days:,} 天")

    # 同口径市场前向基准
    MK = {H: pd.Series(df[f"fwd{H}"].values).groupby(day_idx).mean()
          .reindex(range(n_days)).values for H in HZ}

    def exw(mask, H, ymask=None):
        """日加权超额 + 按日聚类 t"""
        p = np.flatnonzero(mask)
        if ymask is not None:
            p = p[ymask[p]]
        if len(p) < 40:
            return None
        r = df[f"fwd{H}"].values[p]
        mm = MK[H][day_idx[p]]
        ok = np.isfinite(r) & np.isfinite(mm)
        if ok.sum() < 40:
            return None
        e = r[ok] - mm[ok]
        dm = pd.Series(e).groupby(day_idx[p][ok]).mean().values
        if len(dm) < 5:
            return None
        sd = dm.std(ddof=1)
        t = dm.mean() / (sd / np.sqrt(len(dm))) if sd > 0 else np.nan
        return int(ok.sum()), float(r[ok].mean()), float(dm.mean()), float(t)

    def qbin(vals, nb=10):
        s = pd.Series(np.asarray(vals, dtype=np.float64))
        r = s.groupby(day_idx).rank(pct=True, method="average").values
        b = np.minimum(np.floor(r * nb), nb - 1.0)
        b[~np.isfinite(r)] = np.nan
        return b

    def cb(a, src):
        x = np.asarray(a, bool).copy()
        x[~np.isfinite(src)] = False
        return x

    # ============================================ Q1: 各维度十分位单调性
    log("Q1 各维度逐日截面十分位 → 前向超额")
    DIMS = [("hist_z", "MACD柱位置"), ("volz20", "Volume Z"), ("vr20m", "量比VR20"),
            ("vt_5_20", "量能趋势"), ("ret5", "过去5日涨幅"),
            ("ret20", "过去20日涨幅"), ("ret60", "过去60日涨幅"),
            ("hv20", "波动率"), ("px_ma60_pct", "距MA60"), ("rs_mkt20", "相对强弱RS20")]
    rows = []
    for col, cn in DIMS:
        v = df[col].values.astype(np.float64)
        b = qbin(v, 10)
        for g in range(10):
            sig = (b == g)
            for H in HZ:
                o = exw(sig, H)
                if o is None:
                    continue
                rows.append(dict(dim=col, dim_cn=cn, decile=g + 1, H=H,
                                 n=o[0], gross=o[1], exw=o[2], t=o[3]))
        log(f"  {cn} 完成")
    dec = pd.DataFrame(rows)
    dec.to_csv(os.path.join(OUT, "v3_decile.csv"), index=False, encoding="utf-8-sig")
    log(f"v3_decile.csv  {len(dec)} 行")

    # ============================================ Q2/Q3: 超跌定义 + 组合
    log("Q2/Q3 「超跌」不同定义 + 组合")
    hv = df["hv20"].values.astype(np.float64)
    HV_MED = float(np.nanmedian(hv[TR]))
    sg = df["size_grp"].values.astype(np.float64)
    r20 = df["ret20"].values.astype(np.float64)
    pm60 = df["px_ma60_pct"].values.astype(np.float64)
    ph60 = df["px_h60"].values.astype(np.float64)
    hzv = df["hist_z"].values.astype(np.float64)

    SLIM = cb(df["volz20"].values < -1.0, df["volz20"].values)
    NR = cb(r20 < 0.0, r20)
    LB = cb(hv < HV_MED, hv)
    ND1 = cb(sg > 0, sg)
    log(f"  训练段 hv20 中位数={HV_MED:.5f}")

    DEFS = [
        ("hist_z<-1.5 (用户规格)", cb(hzv < -1.5, hzv)),
        ("hist_z<-2.0", cb(hzv < -2.0, hzv)),
        ("hist_z<-1.0", cb(hzv < -1.0, hzv)),
        ("hist_z 0~-1 (温和)", cb((hzv < 0) & (hzv > -1.0), hzv)),
        ("hist_z>-1 (非超跌)", cb(hzv > -1.0, hzv)),
        ("ret20<-15%", cb(r20 < -0.15, r20)),
        ("ret20<-25%", cb(r20 < -0.25, r20)),
        ("距MA60<-20%", cb(pm60 < -0.20, pm60)),
        ("距MA60<-30%", cb(pm60 < -0.30, pm60)),
        ("距60日高<-30%", cb(ph60 < -0.30, ph60)),
        ("无超跌条件", np.ones(n, bool)),
    ]
    rows = []
    for nm, os_ in DEFS:
        for combo_nm, sig in [
            (f"{nm}", os_ & SLIM & NR & LB & ND1),
            (f"{nm} [不剔除D1]", os_ & SLIM & NR & LB),
            (f"{nm} [无缩量]", os_ & NR & LB & ND1),
        ]:
            for H in (5, 10, 20):
                o = exw(sig, H)
                if o is None:
                    continue
                rows.append(dict(over_def=nm, combo=combo_nm, H=H,
                                 n=o[0], gross=o[1], exw=o[2], t=o[3]))
        log(f"  {nm} 完成")
    alt = pd.DataFrame(rows)
    alt.to_csv(os.path.join(OUT, "v3_alt_def.csv"), index=False, encoding="utf-8-sig")
    log(f"v3_alt_def.csv  {len(alt)} 行")

    # ============================================ Q3: 邻域稳定性
    log("Q3 邻域稳定性")
    rows = []
    NEIGH = []
    for th in (-0.10, -0.15, -0.20, -0.25, -0.30, -0.35):
        NEIGH.append((f"距MA60<{th:.0%}", cb(pm60 < th, pm60)))
    for th in (-1.0, -1.2, -1.5, -1.8, -2.2):
        NEIGH.append((f"volz20<{th}", cb(df["volz20"].values < th, df["volz20"].values)))
    for th in (0.0, -0.05, -0.10, -0.15, -0.20):
        NEIGH.append((f"ret20<{th:.0%}", cb(r20 < th, r20)))
    for nm, os_ in NEIGH:
        sig = os_ & SLIM & NR & LB & ND1
        rec = dict(variant=nm, n=int(sig.sum()))
        for H in (5, 10, 20):
            o = exw(sig, H)
            rec[f"exw{H}"] = o[2] if o else np.nan
            rec[f"t{H}"] = o[3] if o else np.nan
        rows.append(rec)
    nei = pd.DataFrame(rows)
    nei.to_csv(os.path.join(OUT, "v3_neigh.csv"), index=False, encoding="utf-8-sig")
    log(f"v3_neigh.csv  {len(nei)} 行")

    # ============================================ Q3: 分年度
    log("Q3 分年度")
    sig_main = cb(pm60 < -0.20, pm60) & SLIM & NR & LB & ND1
    sig_user = cb(hzv < -1.5, hzv) & SLIM & NR & LB & ND1
    rows = []
    for y in range(2015, 2027):
        ym = (yr == y)
        for tag, sig in [("价格超跌(距MA60<-20%)", sig_main),
                         ("用户规格(hist_z<-1.5)", sig_user)]:
            rec = dict(year=y, variant=tag, n_sig=int((sig & ym).sum()))
            for H in (5, 10, 20):
                o = exw(sig, H, ym)
                rec[f"exw{H}"] = o[2] if o else np.nan
                rec[f"t{H}"] = o[3] if o else np.nan
            rows.append(rec)
    yy = pd.DataFrame(rows)
    yy.to_csv(os.path.join(OUT, "v3_rev_year.csv"), index=False, encoding="utf-8-sig")
    log(f"v3_rev_year.csv  {len(yy)} 行")

    # ============================================ 控制台摘要
    log("=" * 78)
    log("摘要 1 —— 用户规格策略 (hist_z<-1.5 + 缩量 + 没涨 + 低波 + 剔D1):")
    for H in (5, 10, 20):
        o = exw(sig_user, H)
        log(f"   H={H:>2}: n={o[0]:>7,}  超额={o[2]:+.4%}  t={o[3]:+.2f}")
    log("摘要 2 —— hist_z 十分位 H=20 超额(检验「超跌=买点」):")
    d20 = dec[(dec["dim"] == "hist_z") & (dec["H"] == 20)]
    for _, r_ in d20.iterrows():
        log(f"   D{int(r_['decile']):>2}: 超额={r_['exw']:+.4%}  t={r_['t']:+.2f}")
    log("摘要 3 —— 价格型深度超跌(距MA60<-20%) 邻域是否稳定:")
    for _, r_ in nei.iterrows():
        if r_["variant"].startswith("距MA60"):
            log(f"   {r_['variant']:<16} n={int(r_['n']):>7,}  "
                f"H10超额={r_['exw10']:+.4%} t={r_['t10']:+.2f}")
    log("完成")
    LOGF.close()


if __name__ == "__main__":
    main()
