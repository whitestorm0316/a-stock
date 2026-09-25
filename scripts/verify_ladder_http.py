#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶梯建仓（建仓节奏）**活服务 HTTP 端到端**验证。

与 verify_ladder.py 的分工：
  verify_ladder.py       直连 Engine / 合成数据，验证**算法**（速度快、无依赖）
  本脚本                 打真实 HTTP 接口，验证**整条链路**（前端参数 → server → engine → 回包）

阶梯语义：序列里的数 = 该批买入只数；满仓只数 = sum(序列) = 同时持仓上限；
每只等分资金 = 1/sum；每次建仓补到下一个累计目标，买满即停，绝不超配。

⚠️ 需要服务已在运行（默认 127.0.0.1:8770）：
     python app/server.py 8770

⚠️ 本机设了 HTTP_PROXY（http://127.0.0.1:55613），urllib 会把 127.0.0.1 的请求
   送去代理，代理连不上本地服务就回 502 —— 很容易误判成「服务挂了」。
   所以这里**显式挂空 ProxyHandler** 绕过代理（curl 则用 --noproxy '*'）。

用法：
  python scripts/verify_ladder_http.py [--base http://127.0.0.1:8770] [--sim 0]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from _console import bootstrap, mark  # noqa: E402

bootstrap()

# 绕过本机 HTTP_PROXY —— 否则 127.0.0.1 会被送去代理并收到 502
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_OK = mark("✅", "[OK]")
_BAD = mark("❌", "[X]")


def _get(base, path, timeout=30):
    with _OPENER.open(base + path, timeout=timeout) as r:
        return json.load(r)


def _post(base, path, body, timeout=900):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with _OPENER.open(req, timeout=timeout) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8770")
    ap.add_argument("--sim", type=int, default=0,
                    help="回测随机基准模拟次数（0 = 跳过，最快；默认 0）")
    a = ap.parse_args()
    base = a.base.rstrip("/")

    try:
        defaults = _get(base, "/api/defaults")
    except Exception as e:  # noqa: BLE001
        print("%s 连不上服务 %s —— 请先 `python app/server.py 8770`（%s）"
              % (_BAD, base, e))
        return 2

    checks: list[tuple[str, bool]] = []

    # ── 1. /api/defaults 下发阶梯预设 ─────────────────────────────
    presets = defaults.get("ladder_presets") or []
    checks.append(("defaults.ladder_presets 有 5 个预设", len(presets) == 5))
    checks.append(("预设 l12223 == [1,2,2,2,3]",
                   any(p.get("ladder") == [1, 2, 2, 2, 3] for p in presets)))
    checks.append(("defaults.params.cap_ladder 默认 None",
                   defaults.get("defaults", {}).get("cap_ladder") is None))

    pbase = dict(defaults["presets"][0]["params"])  # ★K3（最优 3 条件）

    # ── 2. /api/backtest：阶梯 vs 等权 ────────────────────────────
    def cap_of(**over):
        res = _post(base, "/api/backtest",
                    {"params": {**pbase, **over}, "n_sim": a.sim})
        return (res.get("capacity") or {}), res

    cA, _ = cap_of(max_pos=10, max_new=2)                  # A 等权 10 只 / 日 2 只
    cB, _ = cap_of(max_pos=10, max_new=3)                  # B 等权 10 只 / 日 3 只
    cC, _ = cap_of(max_pos=0, cap_ladder="1,2,2,2,3")      # C 阶梯 1/2/2/2/3
    cD, _ = cap_of(max_pos=0, cap_ladder="2,2,2,2,2")      # D 阶梯 2/2/2/2/2
    cE, _ = cap_of(max_pos=0, cap_ladder="1，2 / 2 2 3")   # E 全角+斜杠+空格
    cF, resF = cap_of(max_pos=0, cap_ladder="abx")         # F 非法 → 等权（无约束）
    cG, _ = cap_of(max_pos=0, cap_ladder="1,2,3,4,5")      # G 满仓 15 只

    # 恒等式：全 2 阶梯（满仓 10 只 / 每批 2 只）≡ 等权「10 只 / 日 2 只」
    checks.append(("恒等式 D(2/2/2/2/2) == A(等权10只/日2只) —— CAGR",
                   abs((cD.get("cagr") or -1) - (cA.get("cagr") or -2)) < 1e-12))
    checks.append(("恒等式 D == A —— MDD",
                   abs((cD.get("mdd") or -1) - (cA.get("mdd") or -2)) < 1e-12))
    checks.append(("等权路径不含阶梯字段 (A.ladder is None)", cA.get("ladder") is None))
    checks.append(("阶梯路径回 ladder=[1,2,2,2,3]",
                   cC.get("ladder") == [1, 2, 2, 2, 3]))
    checks.append(("ladder_total == 满仓只数 10（= 序列之和）",
                   cC.get("ladder_total") == 10))
    checks.append(("阶梯启用后 max_pos 归一为 sum(序列) = 10", cC.get("max_pos") == 10))
    checks.append(("1/2/3/4/5 → max_pos = 15", cG.get("max_pos") == 15))
    checks.append(("风控护栏 max_invested <= 1.0",
                   all((c.get("max_invested") is None) or c["max_invested"] <= 1 + 1e-9
                       for c in (cC, cD, cE, cG))))
    checks.append(("阶梯模式产出平均仓位 avg_invested > 0",
                   (cC.get("avg_invested") or 0) > 0))
    checks.append(("非法输入静默退回无约束等权 (capacity is None)",
                   resF.get("capacity") is None))
    checks.append(("容错解析 '1，2 / 2 2 3' → [1,2,2,2,3]",
                   cE.get("ladder") == [1, 2, 2, 2, 3]))
    checks.append(("容错解析与标准写法结果一致（E == C）",
                   abs((cE.get("cagr") or -1) - (cC.get("cagr") or -2)) < 1e-12))
    checks.append(("同 10 只上限下节奏生效（C != B）",
                   abs((cC.get("cagr") or 0) - (cB.get("cagr") or 0)) > 1e-6))
    # ★ 头号结论：阶梯靠「更好的建仓节奏」把账户口径做上去、且回撤更浅
    #   （A/B/C 的资金利用率接近：C 平均仓位 81%、A 平均持仓 8.22/10 ≈ 82%，
    #    故账户口径可直接横比）
    checks.append(("★ C 账户口径收益 > B（等权10只/日3只）",
                   (cC.get("cap_cagr") or 0) > (cB.get("cap_cagr") or 0)))
    checks.append(("★ C 账户口径回撤浅于 B（MDD 更接近 0）",
                   (cC.get("cap_mdd") or -9) > (cB.get("cap_mdd") or -9)))
    checks.append(("★ C 账户口径收益 > A（等权10只/日2只）",
                   (cC.get("cap_cagr") or 0) > (cA.get("cap_cagr") or 0)))
    checks.append(("两口径都回传（cap_cagr 存在且 <= 已投 cagr）",
                   isinstance(cC.get("cap_cagr"), (int, float))
                   and (cC.get("cap_cagr") or 0) <= (cC.get("cagr") or 0) + 1e-12))

    # ── 3. /api/trades：明细口径与 w 列 ───────────────────────────
    trC = _post(base, "/api/trades",
                {"params": {**pbase, "max_pos": 0, "cap_ladder": "1,2,2,2,3"},
                 "sort": "exit_date", "order": "desc", "page": 1, "page_size": 6})
    trA = _post(base, "/api/trades",
                {"params": {**pbase, "max_pos": 10, "max_new": 2},
                 "sort": "exit_date", "order": "desc", "page": 1, "page_size": 6})
    rowsC, rowsA = trC.get("rows") or [], trA.get("rows") or []
    plC, plA = trC.get("plan") or {}, trA.get("plan") or {}
    wsC = [r.get("w") for r in rowsC]

    checks.append(("trades.plan.ladder == [1,2,2,2,3]",
                   plC.get("ladder") == [1, 2, 2, 2, 3]))
    checks.append(("trades.plan.ladder_total == 10（满仓只数）",
                   plC.get("ladder_total") == 10))
    checks.append(("trades.plan.max_pos == 10",
                   plC.get("max_pos") == 10))
    checks.append(("trades 阶梯模式每行仓位 w 恒为 10%（每只等分）",
                   bool(wsC) and all(isinstance(w, (int, float)) and abs(w - 10.0) < 1e-6
                                     for w in wsC)))
    checks.append(("trades 等权模式 plan.ladder is None", plA.get("ladder") is None))
    checks.append(("trades 等权模式每行 w is None",
                   bool(rowsA) and all(r.get("w") is None for r in rowsA)))
    checks.append(("trades.plan.max_new == 3（= 序列最大档，随序列自动）",
                   plC.get("max_new") == 3))
    checks.append(("trades 阶梯 n_trade 与等权10只/日2只 不同（节奏已生效）",
                   trC.get("n_trade") != trA.get("n_trade")))
    trW = _post(base, "/api/trades",
                {"params": {**pbase, "max_pos": 0, "cap_ladder": "1,2,2,2,3"},
                 "sort": "w", "order": "desc", "page": 1, "page_size": 6})
    checks.append(("trades 支持 sort=w", bool(trW.get("rows"))))

    # ── 汇总 ───────────────────────────────────────────────────
    print("── 阶梯建仓 · 活服务 HTTP 端到端 ──")
    print("base=%s  sim=%d  预设=%d" % (base, a.sim, len(presets)))
    print()
    # ⚠️ 两个口径必须并排打印，否则极易混用：
    #    已投 = 分母「当日投入权重之和」（假设闲置资金不下蛋，可与不限仓位基线比）
    #    账户 = 分母 1.0（全部资金，实盘真实感受；资金利用率不同时**不可**横比收益）
    print("  %-22s %-22s %-9s %-16s" %
          ("口径", "已投CAGR / 已投MDD", "max_pos", "ladder/满仓"))
    print("  %-22s %-22s %-9s %-16s" %
          ("", "账户CAGR / 账户MDD", "", ""))
    for tag, c in (("A 等权10只/日2只", cA), ("B 等权10只/日3只", cB),
                   ("C 阶梯1/2/2/2/3", cC), ("D 阶梯2/2/2/2/2", cD),
                   ("E 容错解析", cE), ("G 阶梯1/2/3/4/5", cG)):
        lad = c.get("ladder")
        lt = c.get("ladder_total")
        lab = f"{lad} → {lt}只" if lad else "—"
        print("  %-22s %+8.2f%% /%+8.2f%%   %-9s %-16s" % (
            tag, (c.get("cagr") or 0) * 100, (c.get("mdd") or 0) * 100,
            c.get("max_pos"), lab))
        print("  %-22s %+8.2f%% /%+8.2f%%" % (
            "", (c.get("cap_cagr") or 0) * 100, (c.get("cap_mdd") or 0) * 100))
    print("  %-22s %s" % ("F 非法→等权(无约束)", "capacity=None"))
    print()
    bad = 0
    for name, good in checks:
        print("  %s %s" % (_OK if good else _BAD, name))
        bad += (not good)
    print()
    print("%s HTTP E2E：%s（%d/%d）" % (
        mark("🎉", "[>>]"), "PASS" if bad == 0 else "FAIL",
        len(checks) - bad, len(checks)))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
