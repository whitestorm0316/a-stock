# -*- coding: utf-8 -*-
"""退市风险过滤 —— HTTP 端到端验证（打真实运行中的服务）。

对一个基准参数集，分别开/关三条退市判据，对比：
  · 信号数 / 交易笔数 / 年化 / 回撤 / 胜率
  · 生效条件列表里是否出现退市判据名
"""
import json
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://127.0.0.1:8770"

# ⚠️ 本机环境设了 HTTP_PROXY（如 WorkBuddy 的 127.0.0.1:55613），urllib 会把
#    127.0.0.1 的请求也走代理 → 代理连不上本地服务就回 502 Bad Gateway，
#    看起来像「服务挂了」。这里显式给本机地址开直连。
_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}))


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _opener.open(req, timeout=1800) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path):
    with _opener.open(BASE + path, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


# ---- 1) 默认参数里应已带上 delist（默认全关）
rep = get("/api/defaults")
d = rep.get("defaults") or {}
print("=" * 80)
print("【1】/api/defaults → defaults 里的退市相关字段")
for k in ("delist", "delist_rev_floor", "delist_main_2024", "delist_penny_days"):
    print(f"  {k:<22} = {d.get(k, '**缺失!**')}")

params = dict(d)
params.pop("delist", None)

CASES = [
    ("A 基准（退市过滤全关）", None),
    ("B +财务类退市风险", {"financial": True, "loss2y": False, "penny": False}),
    ("C +连续两年亏损", {"financial": False, "loss2y": True, "penny": False}),
    ("D +面值退市 10/20", {"financial": False, "loss2y": False, "penny": True}),
    ("E 三条全开", {"financial": True, "loss2y": True, "penny": True}),
    ("F 三条全开+主板2024 3亿", {"financial": True, "loss2y": True, "penny": True}),
]


def pc(v):
    return "—" if v is None else f"{v * 100:.2f}%"


print("\n" + "=" * 80)
print("【2】/api/backtest 端到端对比（不限仓位口径）")
print("-" * 80)
print(f"{'用例':<28}{'信号数':>10}{'笔数':>9}{'CAGR':>9}{'回撤':>9}{'胜率':>8}")
print("-" * 80)

rows = []
for name, dl in CASES:
    p = dict(params)
    if dl is not None:
        p["delist"] = dl
        if name.startswith("F"):
            p["delist_main_2024"] = True
    t0 = time.time()
    res = post("/api/backtest", {"params": p, "pool": None, "n_sim": 0})
    dt = time.time() - t0
    if res.get("error"):
        print(f"{name:<28} ERROR: {res['error']}")
        continue
    rows.append((name, res))
    print(f"{name:<26}{res.get('n_signal', 0):>10,}{res.get('n_trade', 0):>9,}"
          f"{pc(res.get('cagr')):>9}{pc(res.get('mdd')):>9}{pc(res.get('win')):>8}"
          f"  ({dt:.0f}s)")
print("-" * 80)

base = rows[0][1] if rows else {}
print("\n【3】相对基准的变化（信号数 / 笔数）")
for name, res in rows[1:]:
    dn = res.get("n_signal", 0) - base.get("n_signal", 0)
    dtr = res.get("n_trade", 0) - base.get("n_trade", 0)
    pn = dn / max(base.get("n_signal", 1), 1) * 100
    print(f"  {name:<28} 信号 {dn:>+7,} ({pn:>+6.2f}%)   笔数 {dtr:>+7,}")

print("\n【4】生效条件列表（应能看到退市判据名）")
for name, res in rows:
    dls = [c for c in (res.get("conditions") or []) if "退市" in c or "亏损" in c]
    print(f"  {name:<28} {dls if dls else '（无退市判据）'}")

print("\n[OK] HTTP 端到端验证完成")
