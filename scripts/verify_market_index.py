#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_market_index.py —— 校验「市场指数口径 / 均线窗口」选项接对了没有。

它回答的是：**界面上切换这两个下拉，能不能复现研究报告的数字？**
（报告：reports/41_market_index_size.md，脚本：scripts/41_market_index_size.py）

判定标准：
  1. 默认口径（all / MA60）必须**逐字复现** README 记录的引擎基线
     （CAGR 29.81% / MDD −41.3% / Sharpe 0.979 / 信号 105,809）；
  2. 换口径 / 换窗口的结果必须落在研究报告的对应格子上（容差 0.15pp）；
  3. `meta()` 必须把两个清单暴露出去（前端下拉靠它渲染）。

用法：
    python scripts/ctl.py stop                       # 服务常驻 ~7GB，先停
    /Users/chenqifeng/.workbuddy-ai/binaries/python/envs/default/bin/python \
        scripts/verify_market_index.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import engine as E  # noqa: E402

# (口径, 窗口, 期望 CAGR%, 期望 Sharpe, 说明) —— 全部取自 reports/41_market_index_size.md
CASES = [
    ("all",     60, 29.81, 0.979, "★ 默认口径 = README 基线（必须逐字吻合）"),
    ("small50", 60, 35.19, 1.117, "研究报告：小50% / MA60"),
    ("small30", 60, 35.35, 1.125, "研究报告：小30% / MA60"),
    ("small10", 60, 38.07, 1.207, "研究报告：小10% / MA60"),
    ("big50",   60, 25.59, 0.865, "研究报告：大50% / MA60（方向对照）"),
    ("all",    120, 34.88, 1.109, "研究报告：全A / MA120（≈ 小50%/MA60）"),
    ("small50", 20, 16.81, 0.640, "研究报告：小50% / MA20（反而变差）"),
    ("small50", 120, 43.64, 1.407, "研究报告：小50% / MA120"),
]

TOL = 0.15          # pp 容差（引擎与研究脚本走同一实现，理应几乎完全一致）

fails = []


def check(name, ok, detail=""):
    print(("  ✅ " if ok else "  ❌ ") + name + (f"  → {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def main():
    t0 = time.time()
    print("加载引擎（面板 ~3.7GB，约 60~90 秒）…")
    e = E.Engine()
    print(f"就绪：{int((time.time() - t0) * 1000)} ms   "
          f"{e.C['n']:,} 行 / {e.nd} 日 / {e.C['starts'].size} 只\n")

    # ---------------- 1. meta 暴露清单 ----------------
    print("1. meta() 是否暴露两个清单（前端下拉靠它渲染）")
    m = e.meta()
    keys = [x["key"] for x in m.get("mkt_indexes") or []]
    mas = [x["ma"] for x in m.get("mkt_ma_windows") or []]
    check("meta.mkt_indexes 有 6 个口径", len(keys) == 6, str(keys))
    check("meta.mkt_ma_windows 有 20/60/120", mas == [20, 60, 120], str(mas))
    check("默认口径是 all", m.get("mkt_index_default") == "all")
    check("默认窗口是 60", int(m.get("mkt_ma_default") or 0) == 60)
    check("每个口径都带 name/desc",
          all(x.get("name") and x.get("desc") for x in m["mkt_indexes"]))

    # ---------------- 2. 各组合复现研究报告 ----------------
    print("\n2. 各（口径 × 窗口）组合 vs 研究报告")
    print(f"   {'口径':<9}{'窗口':>5}{'信号数':>10}{'CAGR':>9}{'期望':>9}{'Sharpe':>9}{'期望':>8}")
    for idx, ma, exp_c, exp_s, note in CASES:
        p = dict(E.DEFAULT_PARAMS)
        p["mkt_index"], p["mkt_ma"] = idx, ma
        mask, conds = e.build_mask(p, None)
        st = e.stats(mask, 20)
        got_c, got_s = st["cagr"] * 100, st["sharpe"]
        print(f"   {idx:<9}{ma:>5}{int(mask.sum()):>10,}"
              f"{got_c:>8.2f}%{exp_c:>8.2f}%{got_s:>9.3f}{exp_s:>8.3f}   {note}")
        check(f"{idx}/MA{ma} CAGR 复现研究报告",
              abs(got_c - exp_c) <= TOL, f"{got_c:.2f}% vs {exp_c:.2f}%")
        check(f"{idx}/MA{ma} Sharpe 复现研究报告",
              abs(got_s - exp_s) <= 0.02, f"{got_s:.3f} vs {exp_s:.3f}")
        # 条件标签：默认口径必须保持旧文案
        lbl = [c for c in conds if "市场<" in c]
        if idx == "all" and ma == 60:
            check("默认口径的条件标签仍是「市场<MA60」（前端/fixtures 依赖）",
                  lbl == ["市场<MA60"], str(lbl))
        else:
            check(f"{idx}/MA{ma} 条件标签带口径名", bool(lbl) and lbl[0] != "市场<MA60",
                  str(lbl))

    # ---------------- 3. 默认口径向后兼容 ----------------
    print("\n3. 默认口径是否与改动前完全一致（向后兼容）")
    p0 = dict(E.DEFAULT_PARAMS)
    m0, _ = e.build_mask(p0, None)
    check("DEFAULT_PARAMS 默认 mkt_index=all", p0.get("mkt_index") == "all")
    check("DEFAULT_PARAMS 默认 mkt_ma=60", int(p0.get("mkt_ma") or 0) == 60)
    check("默认信号数 = 105,809（README 基线）", int(m0.sum()) == 105809,
          f"{int(m0.sum()):,}")
    st0 = e.stats(m0, 20)
    check("默认 CAGR = 29.81%（README 基线）", abs(st0["cagr"] * 100 - 29.81) <= TOL,
          f"{st0['cagr']*100:.2f}%")
    check("默认 MDD = −41.3%（README 基线）", abs(st0["mdd"] * 100 + 41.32) <= 0.3,
          f"{st0['mdd']*100:.2f}%")
    # 旧属性名仍指向默认口径
    d0 = e.MKT[(E.MKT_INDEX_DEFAULT, E.MKT_MA_DEFAULT)]
    check("self.mkt_bull 指向默认口径", e.mkt_bull is d0["bull"])
    check("self.mkt_dist60 指向默认口径", e.mkt_dist60 is d0["dist"])

    # ---------------- 4. market_series / scan 跟随口径 ----------------
    print("\n4. /api/market 与 /api/scan 是否跟随口径")
    ms_a = e.market_series(180, "all", 60)
    ms_s = e.market_series(180, "small50", 60)
    check("market_series 回传 index_key/ma_win",
          ms_a["index_key"] == "all" and ms_a["ma_win"] == 60,
          f"{ms_a['index_key']}/{ms_a['ma_win']}")
    check("换口径后 dist_now 变化",
          ms_a["dist_now"] != ms_s["dist_now"],
          f"all {ms_a['dist_now']}% vs small50 {ms_s['dist_now']}%")
    check("series 项用通用键 `ma`（不再是写死的 ma60）",
          "ma" in ms_a["series"][-1] and "ma60" not in ms_a["series"][-1],
          str(sorted(ms_a["series"][-1].keys())))
    check("windows 回传同一口径下 3 个窗口",
          [x["ma"] for x in ms_a["windows"]] == [20, 60, 120],
          str([(x["ma"], x["dist"]) for x in ms_a["windows"]]))
    check("非法口径被规范化回默认", e.market_series(30, "nope", 999)["index_key"] == "all")

    sc = e.scan(None, limit=5, p={**E.DEFAULT_PARAMS, "mkt_index": "small50"})
    check("scan 回传所选口径", sc.get("mkt_index") == "small50", str(sc.get("mkt_index")))
    check("scan 回传口径中文名", sc.get("mkt_index_name") == "小50% 等权",
          str(sc.get("mkt_index_name")))
    check("scan 的市场字段与 market_series 一致",
          sc.get("mkt_bull_now") == ms_s["bull_now"],
          f"scan {sc.get('mkt_bull_now')} vs market {ms_s['bull_now']}")

    print("\n" + "=" * 72)
    if fails:
        print(f"❌ {len(fails)} 项失败：")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
