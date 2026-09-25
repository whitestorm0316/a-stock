#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_industry_tree.py — 校验行业两级归类配置是否覆盖全部细分行业。

为什么需要它：
  app/industry_tree.json 是【人工归并】的配置（数据源只有同花顺 88 个细分行业，
  没有门类字段）。人工维护的清单一定会漂移：重跑 20_fetch_industry.py 后行业可能
  增删改名，此时配置若不更新，界面上就会悄悄少掉几个行业 —— 这种「静默丢数据」
  比报错更危险。

本脚本做三件事：
  1. 覆盖率：配置里的细分行业 vs 数据源里的细分行业，双向对账
     —— 配置多出来的（写错了名字） / 数据源多出来的（漏配了）都要报出来
  2. 重复：同一个细分行业被归到多个门类（会导致勾选语义混乱）
  3. 门类代码/名称合法性：对照证监会 2012 版 A~S 门类表

退出码：0 = 通过，1 = 有问题（可直接用于 CI / 提交前检查）。

用法：
    python scripts/check_industry_tree.py
    python scripts/check_industry_tree.py --strict   # 把 absent 门类也算问题
"""
import argparse
import collections
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TREE = os.path.join(ROOT, "app", "industry_tree.json")
MAP = os.path.join(ROOT, "data", "raw", "industry_map.json")

# 证监会 2012 版门类（《上市公司行业分类指引》）
CSRC = {
    "A": "农、林、牧、渔业",
    "B": "采矿业",
    "C": "制造业",
    "D": "电力、热力、燃气及水生产和供应业",
    "E": "建筑业",
    "F": "批发和零售业",
    "G": "交通运输、仓储和邮政业",
    "H": "住宿和餐饮业",
    "I": "信息传输、软件和信息技术服务业",
    "J": "金融业",
    "K": "房地产业",
    "L": "租赁和商务服务业",
    "M": "科学研究和技术服务业",
    "N": "水利、环境和公共设施管理业",
    "O": "居民服务、修理和其他服务业",
    "P": "教育",
    "Q": "卫生和社会工作",
    "R": "文化、体育和娱乐业",
    "S": "综合",
}


def load_data_industries():
    """数据源里的细分行业清单（来自 industry_map.json）。"""
    if not os.path.exists(MAP):
        return None
    with open(MAP, encoding="utf-8") as f:
        m = json.load(f)
    return collections.Counter(v["name"] for v in m.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="把「本数据源无细分行业的门类」也视为问题")
    args = ap.parse_args()

    with open(TREE, encoding="utf-8") as f:
        tree = json.load(f)
    groups = tree["groups"]
    absent = tree.get("absent") or []

    problems = []

    # ---- 1. 门类代码/名称合法性 + 重复
    seen_group = {}
    for g in groups:
        code, name = g.get("code"), g.get("name")
        if code not in CSRC:
            problems.append(f"门类代码非法：{code!r}（不在 A~S 中）")
        elif CSRC[code] != name:
            problems.append(f"门类名称与证监会不符：{code} 写的是 {name!r}，"
                            f"应为 {CSRC[code]!r}")
        if code in seen_group:
            problems.append(f"门类代码重复：{code}")
        seen_group[code] = name

    for a in absent:
        if a.get("code") not in CSRC:
            problems.append(f"absent 段门类代码非法：{a.get('code')!r}")

    covered = [s for g in groups for s in g.get("subs") or []]
    dup = [k for k, v in collections.Counter(covered).items() if v > 1]
    for d in dup:
        owners = [g["code"] for g in groups if d in (g.get("subs") or [])]
        problems.append(f"细分行业被归到多个门类：{d} → {owners}")

    # ---- 2. 与数据源双向对账
    data = load_data_industries()
    print("=" * 76)
    print("行业两级归类校验")
    print("=" * 76)
    print(f"  配置门类数     : {len(groups)}"
          f"（另有 {len(absent)} 个门类本数据源无细分行业）")
    print(f"  配置细分行业数 : {len(covered)}（去重后 {len(set(covered))}）")

    if data is None:
        print("  ⚠️ 找不到 data/raw/industry_map.json —— 跳过与数据源的对账")
        print("     （数据被 gitignore，需先跑 scripts/20_fetch_industry.py）")
    else:
        print(f"  数据源细分行业 : {len(data)}（覆盖 {sum(data.values())} 只股票）")
        cfg = set(covered)
        src = set(data)
        only_cfg = sorted(cfg - src)
        only_src = sorted(src - cfg)
        if only_cfg:
            problems.append(
                "配置里写了但数据源没有的行业（名字写错或已下线，共 %d 个）：%s"
                % (len(only_cfg), "、".join(only_cfg)))
        if only_src:
            problems.append(
                "数据源有但配置漏配的行业（界面上会消失，共 %d 个）：%s"
                % (len(only_src), "、".join(only_src)))
        miss_pct = len(only_src) / max(len(src), 1) * 100
        print(f"  覆盖率         : {100 - miss_pct:.1f}%"
              f"（{len(covered)} / {len(src)}）")

        # 每个门类下的股票数（让归类结果可人工复核）
        cnt = {g["code"]: sum(data.get(s, 0) for s in (g.get("subs") or []))
               for g in groups}
        print()
        print("  门类下股票数（按同花顺细分行业成员汇总，含重复成员）：")
        for g in groups:
            bar = "▇" * max(1, round(cnt[g["code"]] / 40))
            print(f"    {g['code']} {g['name'][:18]:<20}{cnt[g['code']]:>6} 只  {bar}")
        for a in absent:
            print(f"    {a['code']} {a['name'][:18]:<20}{'—':>6}     "
                  f"（本数据源无细分行业）")

    if args.strict:
        for a in absent:
            problems.append(f"[strict] 门类 {a['code']} {a['name']} 没有细分行业")

    # ---- 3. 结论
    print()
    if problems:
        print(f"[FAIL] 发现 {len(problems)} 个问题：")
        for p in problems:
            print("  · " + p)
        return 1
    print("[OK] 校验通过：全覆盖、无重复、门类合法")
    return 0


if __name__ == "__main__":
    sys.exit(main())
