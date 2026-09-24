#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""刷新前端测试的 fixture —— 从运行中的本地服务抓取真实响应。

⚠️ 为什么要有这个脚本
-------------------
前端测试用的是 **fixture 快照**（`tests/fixtures/*.json`），不是现场请求，
这样才能离线跑、结果可复现。但快照会随代码演进而过期
（例如给响应加字段后，旧快照没有该字段，断言就会挂）。

所以：**每次改了后端响应结构，都要重新跑本脚本并提交 fixtures。**

用法：
    python app/server.py 8772 &          # 先起服务
    python tests/make_fixtures.py        # 再抓快照（默认端口 8772）
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "fixtures")
URL = "http://127.0.0.1:" + (sys.argv[1] if len(sys.argv) > 1 else "8772")

POOL = {"mode": "all", "boards": ["MAIN", "CHINEXT", "STAR"], "exchanges": ["SH", "SZ"]}


def get(path):
    with urllib.request.urlopen(URL + path, timeout=120) as r:
        return json.loads(r.read().decode())


def post(path, body, timeout=600):
    req = urllib.request.Request(
        URL + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def save(name, obj):
    p = os.path.join(OUT, f"fixture_{name}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    print(f"  ✅ {name:16s} {os.path.getsize(p)/1024:8.1f} KB")


def main():
    os.makedirs(OUT, exist_ok=True)
    print(f"从 {URL} 抓取 fixture → {OUT}")
    save("meta", get("/api/meta"))
    save("defaults", get("/api/defaults"))
    save("scan", post("/api/scan", {"params": {}, "pool": POOL, "limit": 120}))
    save("backtest", post("/api/backtest", {"params": {}, "pool": POOL}))
    save("trades", post("/api/trades", {"params": {}, "pool": POOL,
                                        "page": 1, "page_size": 50}))
    # 交易明细 · 容量约束口径（前端 ⑧ 开启后走这条分支，含 plan 诊断块）
    save("trades_cap", post("/api/trades",
                            {"params": {"max_pos": 10, "max_new": 3, "pick": "deep"},
                             "pool": POOL, "page": 1, "page_size": 50}))
    # 容量约束：n_sim 设小一点让抓取快一些，前端断言只关心字段与关键数值
    cap = post("/api/backtest",
               {"params": {"max_pos": 10, "max_new": 3, "pick": "deep"},
                "pool": POOL, "n_sim": 200})
    # nav_inv 是逐日收益的采样净值，前端不用，剔掉瘦身
    cap.get("capacity", {}).pop("nav_inv", None)
    save("backtest_cap", cap)
    print("完成。⚠️ 记得把 fixtures 一起提交，否则 CI/他人无法复现。")


if __name__ == "__main__":
    main()
