#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「阶梯建仓 vs 等权」的**两个口径**数字完整摊开，并列出实测的完整条件。

为什么要单独有这个脚本
----------------------
`verify_ladder_http.py` 是**守门测试**（断言 PASS/FAIL），表里只印一列 CAGR。
那一列是 `c["cagr"]` = **已投资金口径**（分母 = 当日投入权重之和），
**不是**账户口径（`cap_cagr`，分母 = 1.0 = 全部资金）。
两者在「资金利用率不同」时不可直接横比收益 —— 混用就会得出错误结论。

本脚本把两列并排印出，并附带数据面板区间、策略参数、分年明细，
用于回答「这个数字是在什么条件下测出来的」。

⚠️ 需要服务已在运行（默认 127.0.0.1:8770）：  python app/server.py 8770
⚠️ 本机设了 HTTP_PROXY，127.0.0.1 的请求会被送去代理回 502 →
   这里显式挂空 ProxyHandler 绕过。

用法：
  python scripts/diag_ladder_caliber.py [--base http://127.0.0.1:8770] [--sim 0]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _console import bootstrap  # noqa: E402

bootstrap()

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PPY = 252.0


def _get(base, path, timeout=60):
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
                    help="随机基准模拟次数（0 = 跳过，最快）")
    a = ap.parse_args()
    base = a.base.rstrip("/")

    d = _get(base, "/api/defaults")
    p0 = d["presets"][0]
    pbase = dict(p0["params"])
    meta = _get(base, "/api/meta")

    print("=== ① 数据面板 / 区间 ===")
    for k in ("date_start", "date_end", "last_date", "n_days", "n_stocks", "n_rows"):
        if k in meta:
            print("  %-14s %s" % (k, meta[k]))
    print()

    print("=== ② 策略参数（预设：%s）===" % p0.get("name"))
    for k in ("px_ma60_min", "px_ma60_max", "size_max", "mkt_state", "mkt_hv",
              "hold", "pick", "max_pos", "max_new", "cap_ladder"):
        print("  %-14s %r" % (k, pbase.get(k)))
    print("  %-14s %s" % ("交易成本", "0.3% 双边（RT_COST=0.003）"))
    print("  %-14s %s" % ("年化约定", "252 交易日（PPY）"))
    print("  %-14s %s" % ("去重", "同一只票未平仓期间不得重复买入"))
    print()

    def run(tag, **over):
        res = _post(base, "/api/backtest",
                    {"params": {**pbase, **over}, "n_sim": a.sim})
        return tag, (res.get("capacity") or {})

    cases = [
        run("A 等权 10只 / 日2只", max_pos=10, max_new=2),
        run("B 等权 10只 / 日3只", max_pos=10, max_new=3),
        run("C 阶梯 1,2,2,2,3", max_pos=0, cap_ladder="1,2,2,2,3"),
    ]

    hdr = ("口径", "已投CAGR", "已投MDD", "账户CAGR", "账户MDD",
           "平均仓位", "满仓日", "平均持仓", "建仓/信号", "丢弃率")
    print("=== ③ 两口径对照（n_sim=%d）===" % a.sim)
    print("  %-18s %9s %9s %9s %9s %8s %7s %8s %11s %7s" % hdr)
    for tag, c in cases:
        ai, fp = c.get("avg_invested"), c.get("full_pct")
        ai_s = "   —   " if ai is None else "%6.1f%%" % (ai * 100)
        fp_s = "   —   " if fp is None else "%6.1f%%" % (fp * 100)
        print("  %-18s %9.2f%% %9.2f%% %9.2f%% %9.2f%% %8s %7s %7.2f %5d/%-5d %6.1f%%" % (
            tag,
            (c.get("cagr") or 0) * 100, (c.get("mdd") or 0) * 100,
            (c.get("cap_cagr") or 0) * 100, (c.get("cap_mdd") or 0) * 100,
            ai_s, fp_s, c.get("avg_pos") or 0,
            int(c.get("n_hold") or 0), int(c.get("n_signal") or 0),
            (c.get("drop_pct") or 0) * 100))
    print("  ⚠️ 等权行的「平均仓位」为 — 是因为该诊断只对阶梯口径计算；")
    print("     等权的等价指标 = 平均持仓 / max_pos（A 为 %.1f%%）。"
          % ((cases[0][1].get("avg_pos") or 0) / max(cases[0][1].get("max_pos") or 10, 1) * 100))
    print()

    nd = int(meta.get("n_days") or 0)
    print("=== ④ 全期累计倍数（由账户 CAGR 反推，%d 个交易日）===" % nd)
    for tag, c in cases:
        g = c.get("cap_cagr") or 0
        mult = (1.0 + g) ** (nd / PPY) if nd else float("nan")
        print("  %-18s 账户口径 ≈ %.2f 倍" % (tag, mult))
    print()

    print("=== ⑤ 分年（账户口径：年度收益 / 年内 MDD）===")
    ym = {}
    years = []
    for tag, c in cases:
        ym[tag] = {int(e["year"]): e for e in (c.get("yearly") or [])}
        for y in ym[tag]:
            if y not in years:
                years.append(y)
    years.sort()
    print("  %-18s %s" % ("口径", " ".join("%16s" % y for y in years)))
    for tag, _c in cases:
        cells = []
        for y in years:
            e = ym[tag].get(y)
            cells.append("%16s" % ("—" if not e else "%+7.2f%% /%7.2f%%" % (
                e["ret"] * 100, e["mdd"] * 100)))
        print("  %-18s %s" % (tag, " ".join(cells)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
