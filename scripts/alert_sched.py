#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""alert_sched.py —— 「每日定时播报」的**纯逻辑**（可独立测试，零副作用）

为什么单独一个模块
------------------
判断逻辑原本想直接写在 `app/server.py` 里，但那个文件**在模块级就构造 Engine()**
（吃 ~7GB / 60 秒）—— 于是任何 `import server` 的测试都要等一分钟并吃掉几 GB，
根本没法做单元测试。

所以把「什么时候该触发」这部分抽出来：**只依赖标准库、无副作用**，
`server.py` 与 `tests/test_alert.py` 都从这里导入，**两份实现不可能漂移**。

用户可见的契约
--------------
- 工作日（周一~周五）`HH:MM` 触发一次
- **当天只触发一次**（靠 `last_date` 去重，服务重启也不会重发）
- ⚠️ **不判法定节假日** —— 非交易日的更新会拿到空数据，但
  `42_market_alert.py` 有「数据新鲜度」护栏（`--max-stale`）会跳过发送，
  不会误报「今天有信号」。多一层日历依赖不值当。
"""
from __future__ import annotations

from datetime import date, datetime, time as _time, timedelta, timezone

# 东八区固定时区（与 42_market_alert.py 的 CST 一致；不用系统本地时区，
# 免得在别处跑时「15:30」变成奇怪的时间）
TZ = timezone(timedelta(hours=8))

DEFAULT_HH = 15
DEFAULT_MM = 30


def parse_time(raw: str | None) -> tuple[int, int]:
    """'HH:MM' → (h, m)。**写错就回落到默认** —— 配置问题不该让服务起不来。"""
    try:
        hh, mm = str(raw or "").split(":")
        return int(hh) % 24, int(mm) % 60
    except Exception:  # noqa: BLE001
        return DEFAULT_HH, DEFAULT_MM


def enabled_from_env(raw: str | None) -> bool:
    """'0'/'false'/'no'/'off' → 关闭；其余（含空）→ 开启。"""
    return str(raw or "1").strip().lower() not in ("0", "false", "no", "off")


def due(now: datetime, enabled: bool, hh: int, mm: int,
        last_date: str | None, *, boot_at: datetime | None = None,
        grace_min: int = 10) -> bool:
    """现在该不该触发？四条件全满足：

      1. 调度启用；
      2. 今天周一~周五；
      3. 已过触发时刻，且**今天还没触发过**（`last_date` = 上次触发的日期）；
      4. ⚠️ **已过服务启动宽限期** —— 这是踩过坑之后加的。

    为什么需要第 4 条（`boot_at` / `grace_min`）
    --------------------------------------------
    没有它的时候：服务在 18:06 启动，而当天 15:30 已过、`last_date` 又是空的
    → **启动即判定「该触发」** → 立刻拉起一次 18 分钟的数据更新 + 播报。
    后果：每次重启服务都可能触发一次重更新（重建面板要独占 7GB，
    还把服务停掉再起 —— 用户只是想重启一下界面而已）。

    加宽限期后：**只有「服务运行期间跨越了触发时刻」才会触发**，
    启动那一刻已经错过的不补跑（要补跑请手动 `bash scripts/daily_alert.sh`）。

    ⚠️ 注意这与「错过的更新要不要补」是**两个决策**：
       本设计选择**不补** —— 因为「补」会把「重启服务」变成「触发重更新」，
       副作用远大于收益。真正的缺失靠 `42_market_alert` 的
       「数据陈旧」护栏提示（stale_days > max_stale 时不发，如实汇报）。
    """
    if not enabled:
        return False
    if now.weekday() >= 5:                     # 5=周六 6=周日
        return False
    if (now.hour, now.minute) < (hh, mm):
        return False
    if now.strftime("%Y-%m-%d") == (last_date or ""):
        return False
    if boot_at is not None and (now - boot_at) < timedelta(minutes=grace_min):
        return False
    return True


def next_due(now: datetime, enabled: bool, hh: int, mm: int) -> str | None:
    """下一次触发时刻（人可读 'YYYY-MM-DD HH:MM'）；未启用 → None。

    只按「工作日 + 时刻」往后找（最多 8 天，必然覆盖到一个工作日）。
    ⚠️ 与 `due()` 同口径：**不排除法定节假日**。
    """
    if not enabled:
        return None
    d: date = now.date()
    for _ in range(8):
        cand = datetime.combine(d, _time(hh, mm), tzinfo=now.tzinfo or TZ)
        if cand > now and cand.weekday() < 5:
            return cand.strftime("%Y-%m-%d %H:%M")
        d = d + timedelta(days=1)
    return None


def now_cst() -> datetime:
    return datetime.now(TZ)
