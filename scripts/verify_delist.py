# -*- coding: utf-8 -*-
"""退市风险过滤 —— 端到端验证。

直接走 app/engine.py 的真实代码路径（Engine + build_mask），
对三条判据分别统计：被剔除的行数、涉及的股票数、以及最终候选池的收缩幅度。

只读，不改任何数据。
"""
import os
import sys
import time

os.environ.setdefault("PYTHONUTF8", "1")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import numpy as np  # noqa: E402
from engine import Engine, DEFAULT_PARAMS, PENNY_WIN, PENNY_DAYS_DEFAULT  # noqa: E402

t0 = time.time()
print("[..] 加载面板（约 90s）...", flush=True)
E = Engine()
NROW = int(E.C["n"])
NCOL = int(E.df.shape[1])
print(f"[OK] 面板已加载：{NROW:,} 行 / {NCOL} 列 / {E.build_ms} ms\n", flush=True)

code = E.code


def stat(mask_on, mask_off):
    """返回被剔除的行数 / 股票数 / 占比"""
    drop = np.asarray(mask_off, bool) & ~np.asarray(mask_on, bool)
    nd = int(drop.sum())
    ntot = int(np.asarray(mask_off, bool).sum())
    nstock = int(np.unique(code[drop]).size) if nd else 0
    pct = (nd / ntot * 100) if ntot else 0.0
    return nd, nstock, ntot, pct


# 基准：全部条件默认（退市过滤全关）
base_mask, base_conds = E.build_mask(DEFAULT_PARAMS, None)
base_rows = int(base_mask.sum())
base_stocks = int(np.unique(code[base_mask]).size)
print("=" * 74)
print(f"基准（退市过滤全关）：{base_rows:,} 行 / {base_stocks:,} 只股票")
print(f"  生效条件：{base_conds}")
print("=" * 74)

CASES = [
    ("① 财务类退市风险", {"financial": True, "loss2y": False, "penny": False}, {}),
    ("② 连续两年亏损", {"financial": False, "loss2y": True, "penny": False}, {}),
    ("③ 面值退市倒计时 (10/20)", {"financial": False, "loss2y": False, "penny": True}, {}),
    ("③' 面值退市 (20/20 规则原线)", {"financial": False, "loss2y": False, "penny": True},
     {"delist_penny_days": 20}),
    ("④ 财务 + 连续两年亏损", {"financial": True, "loss2y": True, "penny": False}, {}),
    ("⑤ 三条全开", {"financial": True, "loss2y": True, "penny": True}, {}),
    ("⑥ 三条全开 + 主板2024新规(3亿)", {"financial": True, "loss2y": True, "penny": True},
     {"delist_main_2024": True}),
]

print(f"\n{'判据':<34}{'剔除行数':>12}{'剔除股票':>10}{'占基准':>9}")
print("-" * 74)
results = []
for name, dl, extra in CASES:
    p = dict(DEFAULT_PARAMS)
    p["delist"] = dl
    p.update(extra)
    m, conds = E.build_mask(p, None)
    nd, nstock, _, pct = stat(m, base_mask)
    results.append((name, nd, nstock, pct, conds, int(m.sum()), int(np.unique(code[m]).size)))
    print(f"{name:<34}{nd:>12,}{nstock:>10,}{pct:>8.2f}%")

print("-" * 74)

# 明细：每条判据掩码自身的规模（不受其他条件干扰）
print("\n【各判据自身掩码的规模】（在整面板上，不经其他筛选）")
for k, v in [("非财务类退市风险", "financial"),
             ("非连续两年亏损", "loss2y"),
             ("非面值退市", "penny")]:
    p = dict(DEFAULT_PARAMS)
    p["delist"] = {kk: (kk == v) for kk in ("financial", "loss2y", "penny")}
    E.build_mask(p, None)
    part = E.delist_parts.get(k)
    if part is None:
        print(f"  {k:<20} — 掩码缺失！")
        continue
    part = np.asarray(part, bool)
    bad = int((~part).sum())
    bad_stock = int(np.unique(code[~part]).size)
    print(f"  {k:<20} 命中(将被剔除) {bad:>10,} 行 / {bad_stock:>6,} 只股票"
          f"  ({bad / part.size * 100:>6.2f}% of panel)")

# 面值判据的分布诊断
print("\n【面值退市判据 —— penny_ratio 分布】")
pr = np.nan_to_num(E.penny_ratio) * PENNY_WIN
pr = pr[np.isfinite(E.px_raw)]
if pr.size:
    qs = [0, 50, 90, 99, 99.9, 100]
    vals = np.percentile(pr, qs)
    for q, v in zip(qs, vals):
        print(f"  P{q:<6} = {v:>6.1f} 天 / {PENNY_WIN}")
    for d in (1, 5, 10, 15, 20):
        cnt = int((pr >= d).sum())
        print(f"  ≥{d:>2} 天低于 1 元：{cnt:>10,} 行  ({cnt / pr.size * 100:.4f}%)")
else:
    print("  px_raw 全为 NaN（面板缺少 close_price_raw 列？）")

# 未复权价 vs 前复权价：验证「必须用未复权」的结论
print("\n【价差验证：未复权 vs 前复权 判 <1 元的差异】")
try:
    px_adj = E.cl  # 引擎主价（前复权）
    raw_lt = np.isfinite(E.px_raw) & (E.px_raw < 1.0)
    adj_lt = np.isfinite(px_adj) & (px_adj < 1.0)
    print(f"  未复权 <1元：{int(raw_lt.sum()):>10,} 行 ({raw_lt.mean() * 100:.4f}%)")
    print(f"  前复权 <1元：{int(adj_lt.sum()):>10,} 行 ({adj_lt.mean() * 100:.4f}%)")
    print(f"  → 用前复权会多误判 {int((adj_lt & ~raw_lt).sum()):,} 行"
          f"（{adj_lt.sum() / max(raw_lt.sum(), 1):.1f}x）")
except Exception as e:
    print(f"  跳过：{e}")

print(f"\n[OK] 验证完成，用时 {time.time() - t0:.0f}s")
