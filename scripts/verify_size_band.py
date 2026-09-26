#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市值区间（size_min / size_max）与「我的方案」的端到端校验。

⚠️ 为什么要有这个脚本
-------------------
市值从「单边上限」改成「闭区间」是个**容易悄悄写错**的改动：
   · 上一版 size_max=2 表示「最小 30%」（size_grp ≤ 2，0 基）
   · 新版 [size_min, size_max] 里 0 是合法值但**是假值**，
     一句 `if p.get("size_min")` 就会把「最小 10%~20%」整段吞掉
   · 上下限写反、越界、只给一半 —— 前端双滑块都能产出这些形态
所以这里对着**正在运行的服务**打真实接口，用「信号数的集合关系」来证明
区间语义正确（不是靠肉眼看条件名）。

集合关系（A⊂B 时 n(A) < n(B)，且不重不漏的划分满足加法）：
    [0,0] ⊂ [0,1] ⊂ [0,2] ⊂ [0,9]
    n([0,1]) == n([0,0]) + n([1,1])         ← 相邻两档严格划分
    n([0,9]) - n([2,9]) == n([0,1])         ← 剔除的正好是最小 20%

用法：
    python app/server.py 8770 &
    python scripts/verify_size_band.py              # 默认端口 8770
    python scripts/verify_size_band.py 8772 --fast  # 少跑几个区间用例
"""
import json
import os
import sys
import time
import urllib.request

# ⚠️ 本机若设了 HTTP_PROXY，urllib 会把 127.0.0.1 的请求也送去代理，
#    代理连不上本地服务就回 502 —— 看起来像「服务没起」。这里显式直连。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PORT = 8770
FAST = False
CRUD_ONLY = False        # 只跑「我的方案」段（改后端预设接口时省 5 分钟）
for a in sys.argv[1:]:
    if a == "--fast":
        FAST = True
    elif a == "--crud-only":
        CRUD_ONLY = True
    elif a.isdigit():
        PORT = int(a)
BASE = f"http://127.0.0.1:{PORT}"

# 每个用例都跑一次完整回测，缓存住避免重复请求。
_CACHE = {}
_N_CALL = 0


def get(path, timeout=120):
    with _OPENER.open(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post(path, body, timeout=900):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def run(params, tag=None):
    """跑一次回测，返回 (n_signal, conditions)。同参数只请求一次。"""
    global _N_CALL
    key = json.dumps(params, sort_keys=True, ensure_ascii=False)
    if key in _CACHE:
        return _CACHE[key]
    _N_CALL += 1
    r = post("/api/backtest", {"params": params, "n_sim": 0})
    if r.get("error"):
        raise SystemExit(f"  回测失败（{tag or key[:60]}）：{r['error']}")
    out = (int(r["n_signal"]), list(r.get("conditions") or []))
    _CACHE[key] = out
    return out


def N(params):
    return run(params)[0]


def CONDS(params):
    return run(params)[1]


CHECKS = []


def chk(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))


def has_size_cond(conds):
    return [c for c in conds if c.startswith("市值")]


def band_checks(P):
    """区间语义的集合关系证明（17 次回测里占 14 次，所以单独成函数便于跳过）。"""
    print()
    print("【2】区间语义：闭区间 + 单调 + 不重不漏")
    A0 = N(P(size_min=0, size_max=0))     # 最小 10%
    A1 = N(P(size_min=1, size_max=1))     # 10%~20%
    A2 = N(P(size_min=2, size_max=2))     # 20%~30%
    C1 = N(P(size_min=0, size_max=1))     # 最小 20%
    C2 = N(P(size_min=0, size_max=2))     # 最小 30%（= 旧默认 size_max=2）
    ALL = N(P(size_min=0, size_max=9))    # 全部
    NOT2 = N(P(size_min=2, size_max=9))   # 剔除最小 20%
    print(f"  n([0,0])={A0:,}  n([1,1])={A1:,}  n([2,2])={A2:,}")
    print(f"  n([0,1])={C1:,}  n([0,2])={C2:,}  n([0,9])={ALL:,}  n([2,9])={NOT2:,}")
    chk("n(最小10%) > 0（面板里确实有最小分位）", A0 > 0, str(A0))
    chk("n([0,0]) < n([0,1]) < n([0,2]) < n([0,9])（严格单调）",
        A0 < C1 < C2 < ALL, f"{A0} < {C1} < {C2} < {ALL}")
    chk("★ n([0,1]) == n([0,0]) + n([1,1])（相邻两档严格划分，无重叠/无遗漏）",
        C1 == A0 + A1, f"{C1} vs {A0}+{A1}={A0 + A1}")
    chk("★ n([0,2]) == n([0,1]) + n([2,2])", C2 == C1 + A2,
        f"{C2} vs {C1}+{A2}={C1 + A2}")
    chk("★ n([0,9]) - n([2,9]) == n([0,1])（剔除的正好是最小 20%）",
        ALL - NOT2 == C1, f"{ALL - NOT2} vs {C1}")
    chk("区间真的收窄了（不是被当成「全部」）", ALL > C2 > C1 > A0,
        f"{ALL} > {C2} > {C1} > {A0}")

    print()
    print("【3】容错：写反 / 越界 / 只给一半（双滑块会产出这些形态）")
    chk("★ 上下限写反自动交换：[1,0] → n 等于 [0,1]",
        N(P(size_min=1, size_max=0)) == C1, str(N(P(size_min=1, size_max=0))))
    chk("越界夹取：size_min=99 → 等于 [9,9]",
        N(P(size_min=99, size_max=9)) == N(P(size_min=9, size_max=9)),
        str(N(P(size_min=99, size_max=9))))
    chk("越界夹取：size_max=-3 → 等于 [0,0]",
        N(P(size_min=0, size_max=-3)) == A0, str(N(P(size_min=0, size_max=-3))))
    chk("非数字当缺省：size_min='abc' → 等于 [0,2]",
        N(P(size_min="abc", size_max=2)) == C2, str(N(P(size_min="abc", size_max=2))))
    chk("★ 旧口径向后兼容：只给 size_max=1（无 size_min）→ 等于 [0,1]",
        N(P(size_min=None, size_max=1)) == C1, str(N(P(size_min=None, size_max=1))))
    chk("只给 size_max=2 → 等于新默认 [0,2]（K3 口径不变）",
        N(P(size_min=None, size_max=2)) == C2, str(N(P(size_min=None, size_max=2))))
    chk("size_min=0 不会被当成「未设置」（0 是合法值）",
        N(P(size_min=0, size_max=0)) == A0, str(N(P(size_min=0, size_max=0))))

    print()
    print("【4】条件名随区间变化（前端「已启用条件」直接读它）")
    n0 = has_size_cond(CONDS(P(size_min=0, size_max=0)))
    n1 = has_size_cond(CONDS(P(size_min=1, size_max=1)))
    n2 = has_size_cond(CONDS(P(size_min=1, size_max=2)))
    n3 = has_size_cond(CONDS(P(size_min=2, size_max=9)))
    n4 = has_size_cond(CONDS(P(size_min=0, size_max=9)))
    print(f"  [0,0] → {n0}")
    print(f"  [1,1] → {n1}")
    print(f"  [1,2] → {n2}")
    print(f"  [2,9] → {n3}")
    print(f"  [0,9] → {n4 or '（无市值条件）'}")
    chk("[0,0] 条件名是「市值≤D1（最小10%）」",
        n0 and "最小10%" in n0[0], str(n0))
    chk("[1,1] 条件名带百分比跨度「10%~20%」",
        n1 and "10%~20%" in n1[0], str(n1))
    chk("[1,2] 条件名是区间「D2~D3」", n2 and "D2~D3" in n2[0], str(n2))
    chk("[2,9] 条件名是「市值≥D3」", n3 and "D3" in n3[0] and "≥" in n3[0], str(n3))
    chk("★ [0,9] 不产生市值条件（全部 = 不筛选）", n4 == [], str(n4))

    if not FAST:
        print()
        print("【5】额外档位抽查")
        mid = N(P(size_min=4, size_max=4))
        chk("中间单档 [4,4] 介于 0 与全部之间", 0 < mid < ALL, f"{mid} ∈ (0, {ALL})")
        chk("[4,4] 条件名 = 市值D5（40%~50%）",
            "D5" in (has_size_cond(CONDS(P(size_min=4, size_max=4))) or [""])[0],
            str(has_size_cond(CONDS(P(size_min=4, size_max=4)))))
        chk("大市值档 [8,9] 与最小10% 互斥（并集 < 全部）",
            N(P(size_min=8, size_max=9)) + A0 < ALL,
            f"{N(P(size_min=8, size_max=9))} + {A0} < {ALL}")


def main():
    t0 = time.time()
    print(f"── 市值区间 + 我的方案 · 活服务 HTTP 校验（{BASE}）──")
    print()

    d = get("/api/defaults")
    defaults = d.get("defaults") or {}
    presets = d.get("presets") or []
    if not presets:
        raise SystemExit("  /api/defaults 没有 presets，无法取基准参数")
    base = {k: v for k, v in presets[0]["params"].items()}
    print(f"  基准参数 = 预设「{presets[0]['name']}」")

    def P(**over):
        q = dict(base)
        q.update(over)
        return q

    # ── 1. 接口契约
    print()
    print("【1】/api/defaults 新字段")
    chk("defaults 里有 size_min（新旧口径共存）", "size_min" in defaults,
        str(defaults.get("size_min")))
    sb = d.get("size_bands") or {}
    chk("size_bands.n == 10（十分位）", sb.get("n") == 10, str(sb.get("n")))
    chk("size_bands.pct 是 10 档百分比", isinstance(sb.get("pct"), list)
        and len(sb["pct"]) == 10 and sb["pct"][0] == "10%" and sb["pct"][-1] == "100%",
        str(sb.get("pct")))
    chk("user_presets 字段存在（可为空列表）", isinstance(d.get("user_presets"), list),
        str(type(d.get("user_presets")).__name__))

    # ── 2~5. 区间语义（用信号数的集合关系证明）
    if CRUD_ONLY:
        print()
        print("（--crud-only：跳过【2】~【5】区间用例，省 5 分钟）")
    else:
        band_checks(P)

    # ── 6. 我的方案
    print()
    print("【6】我的方案：保存 / 覆盖 / 校验 / 回测可用 / 删除")
    NAME = "__verify_size_band__"
    saved_id = None
    try:
        # 先清掉上次失败留下的同名方案
        for x in get("/api/defaults").get("user_presets") or []:
            if x["name"] == NAME:
                post("/api/presets/delete", {"id": x["id"]})

        r = post("/api/presets/save", {"name": NAME, "params": P(size_min=1, size_max=1)})
        chk("保存返回 ok", r.get("ok") is True, json.dumps(r, ensure_ascii=False)[:120])
        saved_id = r.get("id")
        chk("返回了稳定 id（U 编号）", isinstance(saved_id, str) and saved_id.startswith("U"),
            str(saved_id))
        ups = get("/api/defaults").get("user_presets") or []
        mine = [x for x in ups if x["name"] == NAME]
        chk("保存后出现在 /api/defaults 的 user_presets 里", len(mine) == 1,
            f"{len(ups)} 个自定义方案")
        chk("★ 存的参数带 size_min=1 / size_max=1",
            mine and mine[0]["params"].get("size_min") == 1
            and mine[0]["params"].get("size_max") == 1,
            json.dumps(mine[0]["params"], ensure_ascii=False)[:110] if mine else "—")
        chk("方案带 saved_at 时间戳", bool(mine and mine[0].get("saved_at")),
            mine[0].get("saved_at") if mine else "—")

        # ---- 股票池（pool）：板块 / 行业 / 自定义代码 / 交易所
        # 回归：早期 /api/presets/save 只收 params，股票池走独立的 pool 字段，
        #       于是整套股票池筛选都存不下来（用户反馈「板块筛选没保存」）。
        POOL_IN = {"mode": "custom", "codes_text": "000001.SZ, 600519",
                   "industries": ["半导体", "中药", "半导体"],   # 含重复项 → 应去重
                   "boards": ["main", "bj"],                     # 小写 → 应归一为大写
                   "exchanges": ["SH"], "__junk__": 1}           # 未知键 → 应丢弃
        post("/api/presets/save", {"name": NAME, "params": P(size_min=1, size_max=1),
                                   "pool": POOL_IN})
        mp = [x for x in (get("/api/defaults").get("user_presets") or [])
              if x["name"] == NAME]
        pool = (mp[0].get("pool") if mp else None) or {}
        chk("★ 股票池随方案一起保存（板块筛选不再丢）",
            pool.get("boards") == ["MAIN", "BJ"] and pool.get("mode") == "custom",
            json.dumps(pool, ensure_ascii=False)[:130])
        chk("★ 行业去重、boards 归一为大写、未知键被丢弃",
            pool.get("industries") == ["半导体", "中药"]
            and "__junk__" not in pool,
            json.dumps(pool.get("industries"), ensure_ascii=False)
            + " / " + str(pool.get("boards")))
        chk("自定义代码清单原样存下", pool.get("codes_text") == POOL_IN["codes_text"],
            str(pool.get("codes_text")))
        chk("交易所一并保存", pool.get("exchanges") == ["SH"], str(pool.get("exchanges")))

        # 不带 pool 的保存（脚本直连 API 的形态）→ 保留原池子，不误清
        post("/api/presets/save", {"name": NAME, "params": P(size_min=1, size_max=1)})
        mp2 = [x for x in (get("/api/defaults").get("user_presets") or [])
               if x["name"] == NAME]
        chk("★ 不带 pool 保存 → 保留原有股票池（不会误清）",
            (mp2[0].get("pool") or {}).get("exchanges") == ["SH"],
            json.dumps((mp2[0].get("pool") or {}), ensure_ascii=False)[:110])
        # 非法 mode → 回落 'all'（放最后：这一步会覆盖掉上面的池子）
        post("/api/presets/save", {"name": NAME, "params": P(), "pool": {"mode": "weird"}})
        mw = [x for x in (get("/api/defaults").get("user_presets") or [])
              if x["name"] == NAME]
        chk("★ 非法的 mode 回落为 'all'",
            (mw[0].get("pool") or {}).get("mode") == "all",
            json.dumps((mw[0].get("pool") or {}) if mw else None, ensure_ascii=False)[:90])
        # 新建且从未提交过 pool → None（前端据此「保持页面现有池子不动」）
        post("/api/presets/save", {"name": "__verify_nopool__", "params": P()})
        mz = [x for x in (get("/api/defaults").get("user_presets") or [])
              if x["name"] == "__verify_nopool__"]
        chk("★ 未提交 pool 的新方案 pool=None（前端据此不篡改页面池子）",
            bool(mz) and mz[0].get("pool") is None,
            str(mz[0].get("pool")) if mz else "—")

        # 白名单：未知键不进方案
        r2 = post("/api/presets/save", {"name": NAME,
                                        "params": P(size_min=1, size_max=1,
                                                    __junk__=1, tab="candidates")})
        ups2 = get("/api/defaults").get("user_presets") or []
        m2 = [x for x in ups2 if x["name"] == NAME]
        chk("同名再保存 = 覆盖（id 不变、不重复）",
            r2.get("overwrote") is True and r2.get("id") == saved_id
            and len(m2) == 1,
            f"overwrote={r2.get('overwrote')} id={r2.get('id')} 条数={len(m2)}")
        chk("★ 未知参数键被白名单丢弃（不会把 UI 状态存进方案）",
            m2 and "__junk__" not in m2[0]["params"] and "tab" not in m2[0]["params"],
            str(sorted(m2[0]["params"].keys()))[:110] if m2 else "—")

        r3 = post("/api/presets/save", {"name": "   ", "params": P()})
        chk("空名称 → ok:false 且带 msg", r3.get("ok") is False and r3.get("msg"),
            json.dumps(r3, ensure_ascii=False)[:90])
        chk("空名称失败时**不**用 error 键（否则前端会抛异常）",
            "error" not in r3, str(list(r3.keys())))
        r4 = post("/api/presets/save", {"name": "x" * 60, "params": P()})
        chk("超长名称 → ok:false", r4.get("ok") is False, json.dumps(r4, ensure_ascii=False)[:90])
        r5 = post("/api/presets/save", {"name": "无参数方案", "params": {}})
        chk("空参数 → ok:false", r5.get("ok") is False, json.dumps(r5, ensure_ascii=False)[:90])

        # 保存下来的参数能直接拿去回测（这就是「保存并复用」的意义）
        sp = dict(m2[0]["params"])
        sp.pop("size_min", None)
        sp.pop("size_max", None)          # 故意不全：验证缺键也能跑
        r6 = post("/api/backtest", {"params": sp, "n_sim": 0})
        chk("★ 用保存的参数直接回测可跑通",
            not r6.get("error") and r6.get("n_signal", 0) > 0,
            (r6.get("error") or f"n_signal={r6.get('n_signal'):,}"))
        # 缺 size_* 的键 → 后端用 DEFAULT_PARAMS 补齐（= [0,2]），
        # 与「前端永远两个都发」不冲突，但直连 API 的人要知道这个兜底值。
        C2 = N(P(size_min=0, size_max=2))
        chk("缺 size_* 键时走 DEFAULT_PARAMS（= [0,2]，即 K3 口径）",
            r6.get("n_signal") == C2, f"{r6.get('n_signal')} vs {C2}")

        r7 = post("/api/presets/delete", {"id": "U99999"})
        chk("删除不存在的方案 → ok:false", r7.get("ok") is False,
            json.dumps(r7, ensure_ascii=False)[:90])

        r8 = post("/api/presets/delete", {"id": saved_id})
        chk("删除返回 ok", r8.get("ok") is True, json.dumps(r8, ensure_ascii=False)[:90])
        left = [x for x in (get("/api/defaults").get("user_presets") or [])
                if x["name"] == NAME]
        chk("删除后不再出现在 user_presets", left == [], f"剩 {len(left)} 条")
    finally:
        # 不管前面哪条挂了，都别在用户机器上留垃圾方案
        for x in get("/api/defaults").get("user_presets") or []:
            if x["name"] in (NAME, "无参数方案", "__verify_nopool__"):
                post("/api/presets/delete", {"id": x["id"]})

    # ── 汇总
    print()
    print("── 汇总 ──")
    bad = [c for c in CHECKS if not c[1]]
    for name, good, detail in CHECKS:
        print(f"  {'✅' if good else '❌'} {name}" + (f"  → {detail}" if detail else ""))
    print()
    print(f"  {len(CHECKS) - len(bad)}/{len(CHECKS)} PASS"
          f"　（{_N_CALL} 次回测，{time.time() - t0:.1f}s）")
    if bad:
        print(f"  ❌ 失败 {len(bad)} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
