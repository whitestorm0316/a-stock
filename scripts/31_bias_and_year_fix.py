#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
31_bias_and_year_fix.py —— 修正 V3 的两处口径问题

背景：
  28/29 脚本的分年度归因（v3_rev_year.csv）用 mk{H} 作市场基准，而 mk{H}
  是「当日全部 clean 标的 fwd{H} 的等权均值」。本脚本回答两个问题：

  Q-A 「fwd{H} 末端 NaN 造成截断偏差」这一说法是否成立？
      —— 实测排除末尾 H 天后 mk10 的 CAGR 只变 0.04pp → 是误报。
         真正的偏差来自「把 H 日收益当日收益逐日复利」的错误算法。

  Q-B 逐年归因的口径是否可靠？
      —— 不可靠。mk10 是 10 日收益的重叠复利，与日度链式收益在
         趋势市里差异极大（2015 年相差 62pp）。改用**日度链式基准**重做：
         mkt_fwd_H[t] = expm1(cs[t+1+H] - cs[t+1])，与 fwd{H}[t] 口径完全对齐，
         且无末端截断（仅最后 H 天缺失）。

产出：
  output/v3_bias_diag.csv     基准复利口径三种算法对照
  output/v3_year_v2.csv       日度链式基准下的逐年归因
  output/v3_robust.csv        新基准下的全样本稳健性
  output/v3_bias_run.log
"""
import os
import time
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "output")
LOGF = open(os.path.join(OUT, "v3_bias_run.log"), "w", encoding="utf-8")
_t0 = time.time()

MIN_LIST = 120
HZ = [1, 5, 10, 20, 60]
PPY = 252.0


def log(m):
    line = f"[{time.time()-_t0:7.1f}s] {m}"
    print(line, flush=True)
    LOGF.write(line + "\n")
    LOGF.flush()


def main():
    cols = ["thscode", "date", "is_st_now", "days_since_list", "ret", "open_price",
            "hist_z", "volz20", "vr20m", "vt_5_20", "hv20", "atr_pct",
            "ret5", "ret20", "ret60", "px_ma60_pct", "size_grp", "rs_mkt20"] + \
           [f"fwd{H}" for H in HZ]
    df = pd.read_parquet(os.path.join(PROC, "v2_panel.parquet"), columns=cols)
    m = (~df["is_st_now"].fillna(True)) & (df["days_since_list"].fillna(0) >= MIN_LIST) \
        & df["ret"].notna()
    df = df.loc[m].sort_values(["thscode", "date"], kind="stable").reset_index(drop=True)
    n = len(df)
    log(f"clean {n:,} 行 / {df['thscode'].nunique():,} 只")

    ud = np.sort(df["date"].unique())
    day_idx = np.searchsorted(ud, df["date"].values).astype(np.int64)
    nd = len(ud)
    yr = pd.DatetimeIndex(df["date"].values).year.values
    TR = yr <= 2022
    log(f"交易日 {nd:,} 天  {str(ud[0])[:10]} ~ {str(ud[-1])[:10]}")

    op = df["open_price"].values.astype(np.float64)
    code = df["thscode"].values

    # ---------------------------------------------------------------- oret
    chg = np.flatnonzero(code[1:] != code[:-1]) + 1
    oret = np.full(n, np.nan)
    idx = np.arange(n - 1)
    bound = np.zeros(n, np.int64)
    bound[chg] = 1
    cs_b = np.cumsum(bound)
    ok = cs_b[idx] == cs_b[idx + 1]
    oret[idx[ok]] = op[idx[ok] + 1] / op[idx[ok]] - 1.0
    log(f"oret 有效 {np.isfinite(oret).sum():,} / {n:,}")

    # ---------------------------------------------------------------- 日度链式市场
    mk_d = pd.Series(oret).groupby(day_idx).mean().reindex(range(nd)).values
    n_day_valid = np.isfinite(mk_d).sum()
    mk_d = np.where(np.isfinite(mk_d), mk_d, 0.0)
    log(f"日度市场收益有效 {n_day_valid:,} / {nd:,} 天")

    lg = np.log1p(mk_d)
    cs = np.concatenate([[0.0], np.cumsum(lg)])          # 长度 nd+1

    def chain_fwd(H):
        """mkt_fwd_H[t] = expm1(cs[t+1+H]-cs[t+1])，与 fwd{H}[t] 口径对齐"""
        out = np.full(nd, np.nan)
        hi = nd - 1 - H
        if hi <= 0:
            return out
        t = np.arange(0, hi)
        out[t] = np.expm1(cs[t + 1 + H] - cs[t + 1])
        return out

    CHAIN = {H: chain_fwd(H) for H in HZ}

    # ---------------------------------------------------------------- 【A】基准口径诊断
    yrs_all = (ud[-1] - ud[0]).astype("timedelta64[D]").astype(float) / 365.25
    rows = []
    for H in HZ:
        mk = pd.Series(df[f"fwd{H}"].values).groupby(day_idx).mean().reindex(range(nd)).values
        v = mk[np.isfinite(mk)]
        nav_a = np.prod(1.0 + v)                       # 错：把 H 日收益当日收益复利
        nav_b = np.prod((1.0 + v) ** (1.0 / H))        # 对：日等效后复利
        vc = CHAIN[H][np.isfinite(CHAIN[H])]
        nav_c = np.prod(1.0 + vc)                      # 对：日度链式
        # 排除末尾 H 天（检验「截断偏差」）
        v2 = mk[:nd - H]
        v2 = v2[np.isfinite(v2)]
        nav_b2 = np.prod((1.0 + v2) ** (1.0 / H))
        rows.append(dict(
            H=H,
            n_valid_day=int(len(v)),
            n_tail_missing=int(nd - len(v)),
            cagr_wrong_compound=(nav_a ** (1 / yrs_all) - 1),
            cagr_daily_equiv=(nav_b ** (1 / yrs_all) - 1),
            cagr_chain=(nav_c ** (1 / yrs_all) - 1),
            cagr_daily_equiv_cut=(nav_b2 ** (1 / yrs_all) - 1),
            cut_delta=(nav_b2 ** (1 / yrs_all)) - (nav_b ** (1 / yrs_all)),
        ))
    bias = pd.DataFrame(rows)
    bias.to_csv(os.path.join(OUT, "v3_bias_diag.csv"), index=False)
    log("【A】基准复利口径诊断")
    for r in bias.itertuples():
        log(f"   H={r.H:>2}: 错算法={r.cagr_wrong_compound*100:+8.2f}%  "
            f"日等效={r.cagr_daily_equiv*100:+6.2f}%  日度链式={r.cagr_chain*100:+6.2f}%  "
            f"排除末尾{r.H}天={r.cagr_daily_equiv_cut*100:+6.2f}% (Δ={r.cut_delta*100:+.3f}pp)")

    # ---------------------------------------------------------------- 条件
    hz = df["hist_z"].values.astype(np.float64)
    vz = df["volz20"].values.astype(np.float64)
    r20 = df["ret20"].values.astype(np.float64)
    hv = df["hv20"].values.astype(np.float64)
    sg = df["size_grp"].values.astype(np.float64)
    pm60 = df["px_ma60_pct"].values.astype(np.float64)

    def cb(a, src):
        x = np.asarray(a, bool).copy()
        x[~np.isfinite(src)] = False
        return x

    HV_MED_TR = float(np.nanmedian(hv[TR]))
    log(f"   训练段 hv20 中位数 = {HV_MED_TR:.5f}")

    C = {
        "超跌": cb(hz < -1.5, hz), "缩量": cb(vz < -1.0, vz),
        "没涨": cb(r20 < 0.0, r20), "低波": cb(hv < HV_MED_TR, hv),
        "非D1": cb(sg > 0, sg), "价格超跌": cb(pm60 < -0.20, pm60),
    }
    VARIANTS = {
        "用户规格(hist_z<-1.5)": C["超跌"] & C["缩量"] & C["没涨"] & C["低波"] & C["非D1"],
        "价格超跌(距MA60<-20%)": C["价格超跌"] & C["缩量"] & C["没涨"] & C["低波"] & C["非D1"],
        "无超跌(缩量+没涨+低波)": C["缩量"] & C["没涨"] & C["低波"] & C["非D1"],
    }

    def exw_chain(mask, H, mk_chain, ymask=None):
        """用日度链式基准算日加权超额"""
        p = np.flatnonzero(mask)
        if ymask is not None:
            p = p[ymask[p]]
        if len(p) < 40:
            return None
        r = df[f"fwd{H}"].values[p].astype(np.float64)
        mm = mk_chain[day_idx[p]]
        ok = np.isfinite(r) & np.isfinite(mm)
        if ok.sum() < 40:
            return None
        e = r[ok] - mm[ok]
        dd = day_idx[p][ok]
        dm = pd.Series(e).groupby(dd).mean().values
        if len(dm) < 5:
            return None
        sd = dm.std(ddof=1)
        t = dm.mean() / (sd / np.sqrt(len(dm))) if sd > 0 else np.nan
        return int(ok.sum()), float(r[ok].mean()), float(dm.mean()), float(t)

    # ---------------------------------------------------------------- 【B】逐年（新基准）
    log("【B】日度链式基准下的逐年归因")
    yrows = []
    for name, mask in VARIANTS.items():
        for y in range(2015, 2027):
            ym = yr == y
            for H in [5, 10, 20]:
                res = exw_chain(mask, H, CHAIN[H], ym)
                if res is None:
                    continue
                nn, gross, exw, t = res
                yrows.append(dict(variant=name, year=y, H=H, n=nn,
                                  gross=gross, exw=exw, t=t))
    ydf = pd.DataFrame(yrows)
    ydf.to_csv(os.path.join(OUT, "v3_year_v2.csv"), index=False)
    log(f"   v3_year_v2.csv  {len(ydf)} 行")

    # 正负年份统计
    for name in VARIANTS:
        s = ydf[(ydf.variant == name) & (ydf.H == 20)]
        if len(s) == 0:
            continue
        pos = int((s.exw > 0).sum())
        neg = int((s.exw < 0).sum())
        log(f"   {name:<26} H=20 年度: 正 {pos} / 负 {neg} / 共 {len(s)}")

    # ---------------------------------------------------------------- 【C】全样本稳健性
    log("【C】新基准下的全样本稳健性（vs 旧 mk{H} 基准）")
    rrows = []
    for name, mask in VARIANTS.items():
        for H in [5, 10, 20]:
            # 新基准
            a = exw_chain(mask, H, CHAIN[H])
            # 旧基准
            MK = pd.Series(df[f"fwd{H}"].values).groupby(day_idx).mean().reindex(range(nd)).values
            p = np.flatnonzero(mask)
            r = df[f"fwd{H}"].values[p].astype(np.float64)
            mm = MK[day_idx[p]]
            ok = np.isfinite(r) & np.isfinite(mm)
            e = r[ok] - mm[ok]
            dm = pd.Series(e).groupby(day_idx[p][ok]).mean().values
            sd = dm.std(ddof=1)
            t_old = dm.mean() / (sd / np.sqrt(len(dm))) if sd > 0 else np.nan
            rrows.append(dict(
                variant=name, H=H,
                n_new=a[0] if a else 0,
                exw_new=a[2] if a else np.nan, t_new=a[3] if a else np.nan,
                exw_old=float(dm.mean()), t_old=float(t_old),
            ))
    rdf = pd.DataFrame(rrows)
    rdf.to_csv(os.path.join(OUT, "v3_robust.csv"), index=False)
    for r in rdf.itertuples():
        log(f"   {r.variant:<26} H={r.H:>2}: 新 {r.exw_new*100:+.3f}%(t{r.t_new:+.2f})  "
            f"旧 {r.exw_old*100:+.3f}%(t{r.t_old:+.2f})  差 {100*(r.exw_new-r.exw_old):+.3f}pp")

    log("完成")


if __name__ == "__main__":
    main()
