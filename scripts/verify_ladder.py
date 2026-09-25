#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶梯建仓（建仓节奏）端到端验证。

阶梯语义（见 LADDER_PRESETS / v3b_lib.plan_positions 的 ladder 参数）：
  序列里的每个数 = **该批买入的股票只数**；
  满仓只数 = sum(ladder)（= 同时持仓上限，用户填的 max_pos 会被覆盖）；
  每只等分资金 = 1/sum(ladder)；
  每次建仓把持仓补到「大于当前持仓的最小累计目标 cumsum(ladder)」，
  买满 sum(ladder) 只即停 → **满仓即停**；本批超不过剩余资金 → **绝不超配**。

三部分：
  A. 构造数据手算对照 —— 每批只数 / 累计目标 / 满仓即停 / 到期补仓 / 资金护栏
  B. 等权零回归 —— weights 路径在「全 1 权重」下必须与等权路径逐位一致
  C. 真实面板 —— 等权 vs 阶梯的建仓笔数 / 收益 / 资金占用对比

用法：
  python scripts/verify_ladder.py           # 全跑（含面板，约 2.5 分钟：面板加载就要 2 分钟）
  python scripts/verify_ladder.py --fast    # 只跑 A/B（不加载面板，秒级）
  python scripts/verify_ladder.py --only-c  # 只跑 C（面板已确认没问题时省掉 A/B 输出）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))

from _console import bootstrap, mark  # noqa: E402

bootstrap()

import numpy as np  # noqa: E402

OK = mark("\u2705", "[OK]")
BAD = mark("\u274c", "[!!]")
N_FAIL = [0]


def chk(name, cond, detail=""):
    if cond:
        print(f"  {OK} {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    else:
        N_FAIL[0] += 1
        print(f"  {BAD} {name}  —— {detail}", flush=True)


def _daily_w(holds, weights, day_of_row, hold, nd):
    """每日投入权重之和（占账户总资金比例）。与 Engine._daily_invested 同源。"""
    w = np.asarray(weights, np.float64)
    h = np.asarray(holds, np.int64)
    ent, ext, rw = h[:, 0], h[:, 1], h[:, 2]
    out = np.zeros(nd)
    for j in range(int(hold)):
        alive = (ent + j) < ext
        if not alive.any():
            continue
        dd = day_of_row[rw[alive]] + j
        ww = w[alive]
        ok = dd < nd
        np.add.at(out, dd[ok], ww[ok])
    return out


def _max_concurrent(ent, ext, nd):
    return max(int(((ent <= d) & (ext > d)).sum()) for d in range(nd))


# ================================================================ A
def part_a():
    from v3b_lib import plan_positions
    print("\n=== A. 构造数据手算对照（30 天 × 每天 6 个信号，持有 20 日）", flush=True)
    ND, H, NS = 30, 20, 6
    rows = [(d, d * 100 + s) for d in range(ND) for s in range(NS)]
    mask = np.ones(len(rows), bool)
    D = np.array([r[0] for r in rows])
    sid = np.array([r[1] for r in rows])
    lad = [1, 2, 2, 2, 3]                 # 每批只数；满仓 = 10 只
    CAP = sum(lad)

    # ⚠️ 故意传一个「错的」max_pos/max_new，验证它们会被序列推导值覆盖
    pl = plan_positions(mask, D, ND, H, max_pos=3, max_new=1, stock_of_row=sid,
                        dedupe=True, ladder=lad)
    ent = np.array([h[0] for h in pl["holds"]])
    ext = np.array([h[1] for h in pl["holds"]])
    w = np.array(pl["weights"], np.float64)

    chk("ladder 原样回传", pl["ladder"] == lad, str(pl["ladder"]))
    chk("满仓只数 = sum(ladder) = 10", abs(pl["ladder_total"] - CAP) < 1e-9,
        str(pl["ladder_total"]))
    chk("每日买入上限 = max(ladder) = 3（随序列自动，覆盖传入的 1）",
        pl.get("max_new_eff") == 3, str(pl.get("max_new_eff")))
    chk("每只等分资金 = 1/10 = 10%",
        np.allclose(w, 1.0 / CAP, atol=1e-15), f"取值集合 {sorted(set(w.round(6)))}")

    def cnt_of(e):
        return int((ent == e).sum())

    chk("第 1 批买 1 只", cnt_of(1) == 1, f"{cnt_of(1)} 只")
    chk("第 2 批买 2 只（累计目标 3）", cnt_of(2) == 2, f"{cnt_of(2)} 只")
    chk("第 3 批买 2 只（累计目标 5）", cnt_of(3) == 2, f"{cnt_of(3)} 只")
    chk("第 4 批买 2 只（累计目标 7）", cnt_of(4) == 2, f"{cnt_of(4)} 只")
    chk("第 5 批买 3 只（累计目标 10 = 满仓）", cnt_of(5) == 3, f"{cnt_of(5)} 只")

    cum = np.cumsum([cnt_of(e) for e in (1, 2, 3, 4, 5)])
    chk("累计持有 1 → 3 → 5 → 7 → 10",
        np.array_equal(cum, np.array([1, 3, 5, 7, 10])), str(cum.tolist()))
    chk("满仓即停（entry 6..21 无新建仓）",
        not any(6 <= e <= 21 for e in ent.tolist()),
        f"建仓日={sorted(set(ent.tolist()))}")

    dw = _daily_w(pl["holds"], w, D, H, ND)
    chk("任意时点投入 ≤ 100%（绝不超配）", float(dw.max()) <= 1.0 + 1e-9,
        f"max={dw.max()*100:.2f}%")
    chk("出现过满仓 100%", abs(dw.max() - 1.0) < 1e-9, f"max={dw.max()*100:.2f}%")
    chk("并发持仓从不超过 sum(ladder)=10",
        _max_concurrent(ent, ext, ND) <= CAP,
        f"峰值 {_max_concurrent(ent, ext, ND)} 只")

    # ---- ★ 持有期必须**逐笔**从各自建仓日起算（不是都锚定第一批的 T+hold）
    #      否则各批会在同一天被一起卖掉，档位节奏彻底失真。
    hp = sorted(set(int(x - e) for e, x, _ in pl["holds"]))
    chk("★ 每笔持有期恒 = hold（各批独立起算）", hp == [H], str(hp))
    ex_of_first = set(int(x) for e, x, _ in pl["holds"] if e == 1)
    ex_of_last = set(int(x) for e, x, _ in pl["holds"] if e == 5)
    chk("★ 第 1 批与第 5 批的平仓日相差 4 天（各自 +hold，非同日）",
        ex_of_first == {1 + H} and ex_of_last == {5 + H},
        f"第1批平仓 {sorted(ex_of_first)}，第5批平仓 {sorted(ex_of_last)}")

    # ---- 到期释放后按「累计目标」补仓：补仓量 = 那一批到期释放的只数
    #      （批次是连着的，所以 refill 序列自然复现 1/2/2/2/3）
    # ⚠️ 过渡期持仓会短暂低于满仓：T+1 买入有 1 天空档（d 日卖出 → d 日的信号
    #    d+1 开盘才建仓），而到期是成批的（一次卖 2~3 只），所以补仓慢一天。
    chk("首轮到期后重新建仓", cnt_of(22) > 0, str(sorted(set(ent.tolist()))))
    tail = [cnt_of(e) for e in range(22, 27)]
    chk("满仓后补仓量 = 到期释放的那批只数（复现 1/2/2/2/3）",
        tail == [1, 2, 2, 2, 3], str(tail))
    occ = [int(((ent <= d) & (ext > d)).sum()) for d in range(22, 30)]
    chk("补仓期间持仓从不超过满仓 10 只", max(occ) <= CAP, str(occ))
    chk("补仓到位后回到满仓 10 只（T+1 空档只造成短暂回落）",
        occ[-1] == CAP, str(occ))

    # ---- 信号不足 → 下一批补齐（供不应求时不该漏掉档位）
    rows2 = [(0, 0)] + [(d, 900 + d * 10 + s) for d in range(1, 6) for s in range(5)]
    pl2 = plan_positions(np.ones(len(rows2), bool),
                         np.array([r[0] for r in rows2]), 10, 100,
                         stock_of_row=np.array([r[1] for r in rows2]),
                         dedupe=True, ladder=lad)
    e2 = np.array([h[0] for h in pl2["holds"]])
    chk("信号不足也逐日补到累计目标（1→3→5→7→10）",
        np.array_equal(np.cumsum(np.bincount(e2, minlength=7)[1:6]), [1, 3, 5, 7, 10]),
        str(np.cumsum(np.bincount(e2, minlength=7)[1:6]).tolist()))

    # ---- 短持有期快速轮转（卖出最频繁，资金护栏压力最大）
    pl3 = plan_positions(mask, D, ND, 3, stock_of_row=sid, dedupe=True, ladder=lad)
    dw3 = _daily_w(pl3["holds"], pl3["weights"], D, 3, ND)
    chk("持有 3 日快速轮转下仍不超配", float(dw3.max()) <= 1.0 + 1e-9,
        f"max={dw3.max()*100:.2f}%，建仓 {len(pl3['holds'])} 笔")

    # ---- 第二个序列 1,1,2,2,3,3：验证「批 ≠ 天」但只数序列严格复现
    #      理想情况（每天信号都够）= 1天1只、1天1只、1天2只、1天2只、1天3只、1天3只
    lad2 = [1, 1, 2, 2, 3, 3]
    pl4 = plan_positions(mask, D, ND, H, stock_of_row=sid, dedupe=True, ladder=lad2)
    e4 = np.array([h[0] for h in pl4["holds"]])
    w4 = np.array(pl4["weights"], np.float64)
    seq4 = [int((e4 == d).sum()) for d in range(1, 7)]
    chk("1,1,2,2,3,3 → 前 6 天各买 1/1/2/2/3/3 只（信号充足时即 1 天 1 批）",
        seq4 == lad2, str(seq4))
    chk("1,1,2,2,3,3 → 满仓 12 只 = sum", abs(pl4["ladder_total"] - 12) < 1e-9,
        str(pl4["ladder_total"]))
    chk("1,1,2,2,3,3 → 每只 1/12 ≈ 8.333%",
        np.allclose(w4, 1.0 / 12, atol=1e-15),
        str(sorted(set(w4.round(6)))))

    # 信号不足时「批」会跨天顺延，但累计目标仍在推进（不会漏档）
    rows5 = [(0, 0)] + [(d, 500 + d) for d in (1, 2)] \
            + [(d, 600 + d * 10 + s) for d in range(3, 8) for s in range(6)]
    D5 = np.array([r[0] for r in rows5])
    sid5 = np.array([r[1] for r in rows5])
    pl5 = plan_positions(np.ones(len(rows5), bool), D5, 12, 100,
                         stock_of_row=sid5, dedupe=True, ladder=lad2)
    e5 = np.array([h[0] for h in pl5["holds"]])
    e5 = e5[e5 <= 8]
    cum5 = np.cumsum(np.bincount(e5, minlength=10)[1:9]).tolist()
    chk("信号不足时批次顺延、累计目标仍为 1→2→4→6→9→12",
        cum5[-1] == 12 and set(cum5) >= {1, 2, 4, 6, 9, 12},
        str(cum5))

    # ---- 对照：等权
    pe = plan_positions(mask, D, ND, H, max_new=3, stock_of_row=sid,
                        dedupe=True, max_pos=10)
    chk("等权模式 weights 恒为 1", float(np.max(pe["weights"])) == 1.0
        and float(np.min(pe["weights"])) == 1.0)
    chk("等权模式 ladder 为空", pe["ladder"] is None)

    # ---- 非法输入静默退回等权
    for bad in ([], [0, 1], [1, -1], [0, 0], "abc"):
        pb = plan_positions(mask, D, ND, H, max_new=3, stock_of_row=sid,
                            dedupe=True, max_pos=10, ladder=bad)
        if pb["ladder"] is not None:
            N_FAIL[0] += 1
            print(f"  {BAD} 非法 ladder {bad!r} 未退回等权", flush=True)
    chk("非法 ladder 全部静默退回等权", True)


# ================================================================ B
def part_b():
    from v3b_lib import plan_positions, nav_from_holds
    print("\n=== B. 等权零回归（weights 路径在全 1 权重下必须与等权逐位一致）", flush=True)
    ND, H, NS = 40, 20, 6
    rows = [(d, d * 100 + s) for d in range(ND) for s in range(NS)]
    mask = np.ones(len(rows), bool)
    D = np.array([r[0] for r in rows])
    sid = np.array([r[1] for r in rows])
    n = len(rows)
    rng = np.random.default_rng(7)
    C = dict(n=n, day_idx=np.repeat(np.arange(ND), NS), n_days=ND)
    oret = rng.normal(0.001, 0.03, n)

    pl = plan_positions(mask, D, ND, H, max_new=3, stock_of_row=sid,
                        dedupe=True, max_pos=10)
    holds = pl["holds"]
    ones = np.ones(len(holds), np.float64)

    a1, c1 = nav_from_holds(holds, D, ND, oret, C, hold=H)
    a2, c2 = nav_from_holds(holds, D, ND, oret, C, hold=H, weights=ones)
    chk("已投资金口径：weights=None ≡ weights=全1", np.allclose(a1, a2, atol=1e-15),
        f"maxdiff={np.abs(a1-a2).max():.2e}")
    b1, _ = nav_from_holds(holds, D, ND, oret, C, hold=H, capital_slots=10)
    b2, _ = nav_from_holds(holds, D, ND, oret, C, hold=H, weights=ones,
                           capital_slots=10)
    chk("账户口径：weights=None ≡ weights=全1", np.allclose(b1, b2, atol=1e-15),
        f"maxdiff={np.abs(b1-b2).max():.2e}")
    chk("cnt 序列一致", np.array_equal(c1, c2))

    # ---- 加权口径的分母必须正确（手算 2 笔 +1% 的组合）
    C2 = dict(n=2, day_idx=np.array([0, 0]), n_days=1)
    o2 = np.array([0.01, 0.01])
    h2 = [(0, 1, 0), (0, 1, 1)]
    w2 = np.array([0.1, 0.3])
    acc, _ = nav_from_holds(h2, np.array([0, 0]), 1, o2, C2, hold=1,
                            weights=w2, capital_slots=1.0, rt_cost=0.0)
    inv, _ = nav_from_holds(h2, np.array([0, 0]), 1, o2, C2, hold=1,
                            weights=w2, rt_cost=0.0)
    chk("账户口径：0.1×1% + 0.3×1% = 0.4%", abs(acc[0] - 0.004) < 1e-12,
        f"{acc[0]*100:.4f}%")
    chk("已投资金口径：加权平均后 = 1%", abs(inv[0] - 0.01) < 1e-12,
        f"{inv[0]*100:.4f}%")

    # ---- 阶梯的「每只等分」应与等权「N 份等分」在数值上同源
    pl4 = plan_positions(mask, D, ND, H, stock_of_row=sid, dedupe=True,
                         ladder=[2, 2, 2, 2, 2])          # 满仓 10 只
    plc = plan_positions(mask, D, ND, H, max_pos=10, max_new=2,
                         stock_of_row=sid, dedupe=True)
    chk("阶梯 [2,2,2,2,2] 与「等权 10 只 / 日 2 只」建仓计划逐笔一致",
        pl4["holds"] == plc["holds"],
        f"{len(pl4['holds'])} vs {len(plc['holds'])} 笔")
    ww = np.array(pl4["weights"], np.float64)
    chk("等价性：weights=0.1 与 capital_slots=10 数值同源",
        np.allclose(ww, 1.0 / 10, atol=1e-15), f"{sorted(set(ww.round(6)))}")


# ================================================================ C
def part_c():
    from engine import Engine, DEFAULT_PARAMS
    print("\n=== C. 真实面板：等权 vs 阶梯（默认 K3，n_sim=0 跳过随机对照）", flush=True)
    E = Engine()
    print(f"  面板 {E.C['n']:,} 行 / 标的 {E.C['starts'].size:,} 只，"
          f"加载 {E.build_ms} ms", flush=True)
    mask, conds = E.build_mask(DEFAULT_PARAMS, None)
    print(f"  K3 信号 {int(mask.sum()):,} 个", flush=True)
    hold = 20

    def run(name, **kw):
        kw.setdefault("max_pos", 10)
        kw.setdefault("max_new", 3)
        r = E.capacity(mask, hold=hold, pick="deep", n_sim=0, **kw)
        lad = r.get("ladder")
        print(f"  {name:<22} 建仓 {r['n_hold']:>5} 笔 | "
              f"已投 CAGR {r['cagr']*100:>6.2f}% | 账户 CAGR {r['cap_cagr']*100:>6.2f}% | "
              f"MDD {r['cap_mdd']*100:>6.2f}% | 均持仓 {r['avg_pos']:>5.1f} 只"
              + (f" | 均投入 {r['avg_invested']*100:>5.1f}% | 满仓 {r['full_pct']*100:>4.1f}%"
                 if r.get("avg_invested") is not None else ""), flush=True)
        if lad and r.get("max_invested") is not None:
            chk(f"「{name}」任意时点投入 ≤100%",
                r["max_invested"] <= 1.0 + 1e-9, f"max={r['max_invested']*100:.2f}%")
        return r

    eq3 = run("等权 10只/日3只", max_pos=10, max_new=3)
    eq2 = run("等权 10只/日2只", max_pos=10, max_new=2)
    ld = run("阶梯 1/2/2/2/3", ladder=[1, 2, 2, 2, 3])
    l22 = run("阶梯 2/2/2/2/2", ladder=[2, 2, 2, 2, 2])
    l15 = run("阶梯 1/2/3/4/5", ladder=[1, 2, 3, 4, 5])
    print("  ⚠️ 阶梯的满仓只数 = 序列之和，所以 1/2/2/2/3 与 2/2/2/2/2 都是 10 只、"
          "每只 10%；1/2/3/4/5 是 15 只、每只 6.67%。", flush=True)

    chk("阶梯 max_pos 被序列之和覆盖（1/2/2/2/3 → 10）", ld["max_pos"] == 10,
        str(ld["max_pos"]))
    chk("阶梯 1/2/3/4/5 → max_pos 15", l15["max_pos"] == 15, str(l15["max_pos"]))
    # ⚠️ 最强恒等式：全 2 阶梯（每批 2 只、满仓 10 只）≡ 等权「10 只 / 日 2 只」
    chk("阶梯 2/2/2/2/2 ≡ 等权 10只/日2只（计划与收益逐位一致）",
        abs(l22["cap_cagr"] - eq2["cap_cagr"]) < 1e-12 and l22["n_hold"] == eq2["n_hold"],
        f"{l22['cap_cagr']*100:.6f}% vs {eq2['cap_cagr']*100:.6f}%；"
        f"建仓 {l22['n_hold']} vs {eq2['n_hold']}")
    # 同上限（10 只）下比「节奏」本身的贡献
    chk("同 10 只上限下，1/2/2/2/3 与等权「日3只」结果不同（节奏生效）",
        abs(ld["cap_cagr"] - eq3["cap_cagr"]) > 1e-6,
        f"阶梯 {ld['cap_cagr']*100:.2f}% vs 等权日3只 {eq3['cap_cagr']*100:.2f}%"
        f"（建仓 {ld['n_hold']} / {eq3['n_hold']} 笔）")
    chk("阶梯模式下「均投入」可用（>0）",
        ld.get("avg_invested") is not None and ld["avg_invested"] > 0,
        f"{ld.get('avg_invested')}")
    chk("等权模式下不产出阶梯指标（不误导）",
        eq3.get("avg_invested") is None, str(eq3.get("avg_invested")))

    # 交易明细：每笔应带仓位（= 1/满仓只数）
    plan = E.capacity_plan(mask, hold=hold, max_pos=0, max_new=3,
                           pick="deep", ladder=[1, 2, 2, 2, 3])
    tr = E.trades(mask, hold, include_fin=False, plan=plan)
    ws = [x["w"] for x in (tr["rows"] if tr else []) if x.get("w") is not None]
    chk("交易明细每笔带仓位 w", len(ws) > 0 and len(ws) == len(tr["rows"]),
        f"{len(ws)}/{len(tr['rows'])} 笔；取值集合 {sorted(set(round(x,1) for x in ws))}")
    chk("明细仓位恒为 10%（每只等分，满仓 10 只）",
        all(abs(x - 10.0) < 1e-6 for x in ws), f"max={max(ws):.2f}%")
    chk("plan 回传 ladder", (tr["plan"] or {}).get("ladder") == [1, 2, 2, 2, 3],
        str((tr["plan"] or {}).get("ladder")))
    chk("plan 回传满仓只数（ladder_total）",
        (tr["plan"] or {}).get("ladder_total") == 10,
        str((tr["plan"] or {}).get("ladder_total")))


if __name__ == "__main__":
    fast = "--fast" in sys.argv
    only_c = "--only-c" in sys.argv
    if not only_c:
        part_a()
        part_b()
    if not fast:
        part_c()
    print()
    if N_FAIL[0]:
        print(f"{BAD} 共 {N_FAIL[0]} 项未通过", flush=True)
        sys.exit(1)
    print(f"{OK} 全部通过", flush=True)
