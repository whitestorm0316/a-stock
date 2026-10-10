#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_alert.py —— scripts/42_market_alert.py 的单元测试（纯函数，不需要服务）

覆盖点：
  1. day_change()    当日大盘涨跌幅（净值日变化），含边界（序列不足 / nav 缺失 / 为 0）
  2. build_message() 三种形态的文案正确性：
       · 熊市 + 有信号  → 标题含「熊市」「有信号」，列出候选
       · 牛市 + 无信号  → 标题含「牛市」「无信号」，**不列候选**（那是回溯日的，会误导）
       · 熊市 + 无信号  → 说明为什么没票
       · 均线未成形     → 不判牛熊
  3. 口径一致性      「低于 MA20」的数值必须是负数（曾出现 +0.51% 自相矛盾：
                     dist 取的是 MA60 口径而 ma 显示 MA20）
  4. 候选只在该列时列  无信号时即使传入 items 也不能出现「候选」
  5. stale_days()    数据陈旧判定
  6. --force 提示    强制发送时正文要标明，避免被误当成真实信号

为什么单独一个 .py 而不是 jsdom：
  这些全是 42_market_alert.py 的纯函数，跟浏览器无关；
  用 jsdom 测要先把 Python 逻辑翻译成 JS，纯属自找漂移。

跑法：
  python tests/test_alert.py          # 直接跑
  bash tests/run.sh alert             # 走统一入口
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOD_PATH = ROOT / "scripts" / "42_market_alert.py"

# ⚠️ 模块名以数字开头 → 不能 `import 42_market_alert`，用 spec 从路径加载。
#    且模块顶部有 `if __name__ == "__main__"` 守卫，加载不会触发主流程。
spec = importlib.util.spec_from_file_location("market_alert", MOD_PATH)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

n = 0
fails: list[str] = []
out: list[str] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    global n
    n += 1
    out.append(f"{'✅' if cond else '❌'} {n:02d}. {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(label)


def mk_series(*pairs, ma_dists=None):
    """造一个 /api/market 形态的迷你响应。

    pairs: (date, nav) 序列；ma_dists: {ma: dist} 用于 windows
    """
    series = [dict(date=d, nav=v) for d, v in pairs]
    win = []
    for ma, dist in (ma_dists or {}).items():
        win.append(dict(ma=ma, dist=dist, bull=(dist < 0)))
    return dict(series=series, windows=win,
                dist_now=(ma_dists or {}).get(60))


def scan_of(**kw):
    d = dict(latest_date="2026-10-09", asof="2026-10-09", fallback_days=0,
             latest_n=0, mkt_bull_now=True, mkt_ma=60, mkt_dist=0.51,
             mkt_index_name="全A等权", items=[])
    d.update(kw)
    return d


# ---------------------------------------------------------------- 1. day_change
pct, span = M.day_change(mk_series(("2026-10-08", 3.0), ("2026-10-09", 3.12)))
ok("day_change 基本计算", pct is not None and abs(pct - 4.0) < 1e-6, f"{pct}")
ok("day_change 日期跨度", span == "10-08→10-09", str(span))

pct, span = M.day_change(mk_series(("2026-10-08", 2.0), ("2026-10-09", 1.9)))
ok("day_change 下跌为负", pct is not None and pct < 0, f"{pct}")

ok("day_change 序列不足 → (None,None)", M.day_change(mk_series(("2026-10-09", 3.0))) == (None, None))
ok("day_change 无 series → (None,None)", M.day_change({}) == (None, None))
ok("day_change mk=None → (None,None)", M.day_change(None) == (None, None))
ok("day_change nav 为 0 → 不崩且返回 None",
   M.day_change(mk_series(("a", 0.0), ("b", 1.0)))[0] is None)
ok("day_change nav 缺失 → 不崩且返回 None",
   M.day_change({"series": [{"date": "a"}, {"date": "b", "nav": 1.0}]})[0] is None)

ok("_fmt_pct 正数带 +", M._fmt_pct(4.0) == "+4.00%")
ok("_fmt_pct 负数", M._fmt_pct(-2.5) == "-2.50%")
ok("_fmt_pct None → 破折号", M._fmt_pct(None) == "—")

# ---------------------------------------------------------------- 2. stale_days
from datetime import datetime, timezone, timedelta  # noqa: E402
today = datetime.now(M.CST).date()
ok("stale_days 今天 → 0", M.stale_days(str(today)) == 0)
ok("stale_days 3 天前", M.stale_days(str(today - timedelta(days=3))) == 3)
ok("stale_days 空 → None", M.stale_days(None) is None)
ok("stale_days 非法 → None", M.stale_days("not-a-date") is None)
ok("stale_days 带时间戳也能解析", M.stale_days(str(today) + " 15:00:00") == 0)

# ---------------------------------------------------------------- 3. 熊市 + 有信号
mk = mk_series(("2026-10-08", 3.0), ("2026-10-09", 3.06),
               ma_dists={20: -1.92, 60: -2.10, 120: -3.32})
sc = scan_of(mkt_bull_now=False, latest_n=39, items=[
    dict(code="920701.SZ", name="豪声电子", ind="消费电子", dist60=-14.9),
])
md = M.build_message(sc, mk, sc["items"])
ok("熊市有信号：标题含「熊市」", "熊市" in md.splitlines()[0], md.splitlines()[0])
ok("熊市有信号：标题含「有信号」", "有信号" in md.splitlines()[0])
ok("熊市有信号：列出候选", "候选" in md and "豪声电子" in md)
ok("熊市有信号：报信号数", "39 只" in md)
ok("熊市有信号：含今日大盘", "今日大盘" in md, [l for l in md.splitlines() if "今日大盘" in l][:1])
ok("熊市有信号：涨跌幅 +2.00%", "+2.00%" in md)
ok("熊市有信号：净值低于 MA60", "低于" in md and "MA60" in md)

# ---------------------------------------------------------------- 4. 牛市 + 无信号
mk2 = mk_series(("2026-10-08", 3.0), ("2026-10-09", 2.97),
                ma_dists={20: -1.92, 60: 0.51, 120: -3.32})
sc2 = scan_of(mkt_bull_now=True, latest_n=0, items=[
    dict(code="603370.SH", name="华新精科", ind="电机", dist60=-18.7),
])
md2 = M.build_message(sc2, mk2, sc2["items"])
ok("牛市无信号：标题含「牛市」", "牛市" in md2.splitlines()[0], md2.splitlines()[0])
ok("牛市无信号：标题含「无信号」", "无信号" in md2.splitlines()[0])
ok("牛市无信号：**不列候选**（items 非空也不列）", "候选" not in md2 and "华新精科" not in md2)
ok("牛市无信号：报 0 只", "0 只" in md2)
ok("牛市无信号：说明距均线还差多少", "再跌" in md2 and "跌破" in md2)
ok("牛市无信号：不写「低于」", "低于" not in md2)
ok("牛市无信号：涨跌幅为负 -1.00%", "-1.00%" in md2, [l for l in md2.splitlines() if "今日大盘" in l][:1])

# ---------------------------------------------------------------- 5. 熊市 + 无信号
sc3 = scan_of(mkt_bull_now=False, latest_n=0, mkt_dist=-2.1, items=[
    dict(code="x", name="不该出现", ind="", dist60=-9.9),
])
md3 = M.build_message(sc3, mk, sc3["items"])
ok("熊市无信号：标题「熊市」「无信号」",
   "熊市" in md3.splitlines()[0] and "无信号" in md3.splitlines()[0])
ok("熊市无信号：不列候选", "候选" not in md3 and "不该出现" not in md3)
ok("熊市无信号：说明为何无票（距MA60/市值）", "距MA60" in md3 or "市值" in md3)

# ---------------------------------------------------------------- 6. 均线未成形
sc4 = scan_of(mkt_bull_now=None, latest_n=0, mkt_dist=None)
md4 = M.build_message(sc4, {"series": [], "windows": []}, [])
ok("未成形：标题提示无法判定", "无法判定" in md4.splitlines()[0], md4.splitlines()[0])
ok("未成形：不误报熊市/牛市", "熊市" not in md4.splitlines()[0] and "牛市" not in md4.splitlines()[0])
ok("未成形：今日大盘显示破折号", "—" in md4 and "今日大盘" in md4)

# ---------------------------------------------------------------- 7. 口径一致性（回归）
# 曾出的 bug：ma 显示 MA20 而 dist 取的是 MA60 的值 → 「低于 MA20（+0.51%）」自相矛盾
for ma, dist in ((20, -1.92), (60, 0.51), (120, -3.32)):
    m = mk_series(("2026-10-08", 3.0), ("2026-10-09", 3.0),
                  ma_dists={20: -1.92, 60: 0.51, 120: -3.32})
    bull = dist < 0
    s = scan_of(mkt_bull_now=bull, latest_n=0, mkt_ma=ma,
                mkt_dist=0.51)   # ⚠️ 故意给 MA60 的值，看会不会串味
    txt = M.build_message(s, m, [])
    line = [l for l in txt.splitlines() if "**市场**" in l][0]
    ok(f"口径 MA{ma}：显示的数值与窗口匹配",
       f"{dist:+.2f}%" in line, line)

# ---------------------------------------------------------------- 8. --force 标注
md5 = M.build_message(scan_of(mkt_bull_now=True, latest_n=0), mk2, [], force=True)
ok("--force 时正文标明强制发送", "强制发送" in md5 or "--force" in md5)
md6 = M.build_message(scan_of(mkt_bull_now=True, latest_n=0), mk2, [], force=False)
ok("非 --force 时无强制标注", "强制发送" not in md6)

# ---------------------------------------------------------------- 9. 换窗口 / 真实指数
mk3 = dict(mk2)
mk3["indices"] = [dict(name="上证指数", dist=-2.21, bull=False)]
md7 = M.build_message(scan_of(mkt_bull_now=True, latest_n=0), mk3, [])
ok("含「同口径换窗口」", "同口径换窗口" in md7)
ok("含「真实指数对照」", "真实指数对照" in md7 and "上证指数" in md7)

# ---- 指数对照的排序：同花顺全A（唯一市值加权）必须优先，不能被 [:4] 截掉
mk4 = dict(mk2)
mk4["indices"] = [
    dict(name="上证指数", dist=-2.21, bull=False),
    dict(name="沪深300", dist=-6.03, bull=False),
    dict(name="中证500", dist=-5.56, bull=False),
    dict(name="中证1000", dist=-3.81, bull=False),
    dict(name="同花顺全A", dist=-1.10, bull=False),
]
md8 = M.build_message(scan_of(mkt_bull_now=True, latest_n=0), mk4, [])
line8 = [l for l in md8.splitlines() if "真实指数对照" in l][0]
ok("指数对照优先带「同花顺全A」（不被截断丢掉）", "同花顺全A" in line8, line8)
ok("指数对照总数不超过 4 条", line8.count("｜") <= 3, line8)

# 只有 4 条（无全A）时也不该出错
mk5 = dict(mk2)
mk5["indices"] = [dict(name=f"指数{i}", dist=-1.0, bull=False) for i in range(4)]
md9 = M.build_message(scan_of(mkt_bull_now=True, latest_n=0), mk5, [])
ok("无全A 时指数对照仍正常", "真实指数对照" in md9)

# ---------------------------------------------------------------- 输出
print("\n".join(out))
print("-" * 62)
if fails:
    print(f"❌ 失败 {len(fails)} 项：")
    for f in fails:
        print(f"   · {f}")
    print(f"\n{len(out) - len(fails)}/{len(out)} 通过")
    sys.exit(1)
print(f"✅ 全部通过：{len(out)}/{len(out)}")
