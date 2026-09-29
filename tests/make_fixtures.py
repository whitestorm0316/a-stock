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
    python tests/make_fixtures.py 8770 --only defaults,tune
                                         # 只刷指定的几个（其余不动）

⚠️ 全量抓一遍要 2 分钟（backtest_cap 的 n_sim=200 随机对照就要 25 秒）。
   只改了一处响应结构时用 `--only` 省时间 —— 但**改了公共字段（如 defaults）
   还是要全量**，否则各快照之间会不一致。
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "fixtures")

# ⚠️ 取端口时**必须跳过 `--only` 后面那个值**。
#    早先写成 `[a for a in argv[1:] if not a.startswith("--")]` —— 它把
#    `--only market` 里的 `market` 也当成位置参数 → URL 变成
#    `http://127.0.0.1:market` → 报 `nonnumeric port: 'market'`，
#    而错误信息完全指不到「是 --only 的值被吃掉了」。
#    只在 `--only` 排在最后（如 `8770 --only x,y`）时碰巧正常，所以一直没暴露。
_raw = sys.argv[1:]
_skip = set()
for _i, _a in enumerate(_raw):
    if _a == "--only":
        _skip.add(_i + 1)          # 跳过它的值
    elif _a.startswith("--only="):
        _skip.add(_i)
_ARGS = [a for _i, a in enumerate(_raw) if _i not in _skip and not a.startswith("--")]
URL = "http://127.0.0.1:" + (_ARGS[0] if _ARGS else "8772")

# --only a,b,c → 只刷这几个；缺省 = 全刷
ONLY = set()
for i, a in enumerate(sys.argv[1:]):
    if a == "--only" and i + 2 <= len(sys.argv[1:]):
        ONLY = {x.strip() for x in sys.argv[i + 2].split(",") if x.strip()}
    elif a.startswith("--only="):
        ONLY = {x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()}


def want(name):
    """该 fixture 是否需要刷新（--only 未指定时全部刷新）。"""
    return (not ONLY) or (name in ONLY)

POOL = {"mode": "all", "boards": ["MAIN", "CHINEXT", "STAR"], "exchanges": ["SH", "SZ"]}

# ⚠️ 本机若设了 HTTP_PROXY（企业/工具代理很常见），urllib 会把 127.0.0.1 的请求
#    也送去代理，代理连不上本地服务就回 502 Bad Gateway —— 看起来像「服务没起」。
#    这里给本机地址显式开直连。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path):
    with _OPENER.open(URL + path, timeout=120) as r:
        return json.loads(r.read().decode())


def post(path, body, timeout=600):
    req = urllib.request.Request(
        URL + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def save(name, obj):
    p = os.path.join(OUT, f"fixture_{name}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    print(f"  ✅ {name:16s} {os.path.getsize(p)/1024:8.1f} KB")


def main():
    os.makedirs(OUT, exist_ok=True)
    print(f"从 {URL} 抓取 fixture → {OUT}"
          + (f"（只刷 {', '.join(sorted(ONLY))}）" if ONLY else "（全量）"))
    # ⚠️ 每个抓取都要 `want()` 守卫，**不能只让 save() 空转** ——
    #    否则 --only 省不了时间：backtest_cap 的 post 本身就要 25 秒。
    if want("meta"):
        save("meta", get("/api/meta"))
    if want("defaults"):
        save("defaults", get("/api/defaults"))
    # 市场环境序列（自建全A等权净值 vs MA60）+ 真实指数对照。
    #   180 天足够前端画对照表；指数侧多留 60 根才能算出窗口起点的 MA60。
    if want("market"):
        save("market", get("/api/market?days=180"))
    if want("scan"):
        save("scan", post("/api/scan", {"params": {}, "pool": POOL, "limit": 120}))
    if want("backtest"):
        save("backtest", post("/api/backtest", {"params": {}, "pool": POOL}))
    if want("trades"):
        save("trades", post("/api/trades", {"params": {}, "pool": POOL,
                                            "page": 1, "page_size": 50}))
    # 交易明细 · 容量约束口径（前端 ⑧ 开启后走这条分支，含 plan 诊断块）
    if want("trades_cap"):
        save("trades_cap", post("/api/trades",
                                {"params": {"max_pos": 10, "max_new": 3, "pick": "deep"},
                                 "pool": POOL, "page": 1, "page_size": 50}))
    if want("backtest_cap"):
        cap = post("/api/backtest",
                   {"params": {"max_pos": 10, "max_new": 3, "pick": "deep"},
                    "pool": POOL, "n_sim": 200})
        # nav_inv 是逐日收益的采样净值，前端不用，剔掉瘦身
        cap.get("capacity", {}).pop("nav_inv", None)
        save("backtest_cap", cap)
    # 参数寻优 · 信号口径（不限仓位，按 Sharpe 排序）
    if want("tune"):
        save("tune", post("/api/tune", {"params": {}, "pool": POOL,
                                        "axes": {"px_ma60": True, "size_band": True,
                                                 "mkt_state": True}}))
    # 参数寻优 · 建仓节奏口径（容量约束，按账户资金口径 Sharpe 排序）
    #   ⚠️ 必须**同时勾 px_ma60**：节奏轴单独跑时 10 档节奏给出 10 种不同结果，
    #      不会触发「多行同结果」；只有叠加嵌套区间（D1 ⊂ D1~D2 ⊂ …）才会撞车 ——
    #      而那个提示正是前端要断言的（容量口径下最容易让人以为程序坏了的现象）。
    if want("tune_rhythm"):
        save("tune_rhythm", post("/api/tune", {"params": {}, "pool": POOL,
                                               "axes": {"rhythm": True, "px_ma60": True}}))
    # 单股透视（POST 形态：携带左侧参数 + 股票池）
    #   ⚠️ 这里刻意用**与 scan 相同的 params/pool**，让两个 fixture 可以互相对照：
    #      「主面板说有信号、单股透视说没有」这个真实 bug 就靠这对快照锁住。
    if want("stock"):
        save("stock", post("/api/stock", {
            "code": "001256.SZ",
            "params": {"px_ma60_min": 1, "px_ma60_max": 1,
                       "size_min": 0, "size_max": 3, "mkt_state": "bear"},
            "pool": {"mode": "all", "boards": ["MAIN"], "exchanges": ["SH", "SZ"]}}))
    # 单股透视 · 全部历史图表（用户点「全部历史」后的懒加载形态）
    #   ⚠️ 必须与上面的 stock 用**同一套 params/pool**，否则前端在「窗口视图 ↔ 全历史视图」
    #      切换时看到的两组数字对不上（那正是这个页面要防止的问题）。
    #      hist_rows 有 800~2900 行，刻意不在 stock 里返回（省流量），只在这里给。
    if want("stock_full"):
        save("stock_full", post("/api/stock", {
            "code": "001256.SZ",
            "params": {"px_ma60_min": 1, "px_ma60_max": 1,
                       "size_min": 0, "size_max": 3, "mkt_state": "bear"},
            "pool": {"mode": "all", "boards": ["MAIN"], "exchanges": ["SH", "SZ"]},
            "full_history": True}))
    print("完成。⚠️ 记得把 fixtures 一起提交，否则 CI/他人无法复现。")


if __name__ == "__main__":
    main()
