#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_alert.py —— 每日数据更新 + 「熊市且有信号」飞书提醒（Windows / macOS / Linux 通用）

一条命令走完三件事（与 daily_alert.sh 完全同构，后者已改为委托本脚本）：
  1. scripts/ctl.py update --no-browser   # 停服 → 增量取数 → 重建面板 → 起服（约 18min）
  2. 确认服务真的在跑（更新失败时 ctl.py **不会**重新起服 → 补一次 start）
  3. scripts/42_market_alert.py           # 熊市 + 有信号 → 发飞书；否则静默

为什么要有这个 .py 版本
----------------------
`.sh` 在 Windows 上跑不了（没有 bash）。而「每天自动更新 + 有信号发提醒」这件事
显然需要能在别人的 Windows 机器上跑，所以把编排逻辑收进一个纯标准库的 Python 脚本，
两个平台共用同一份实现，避免「同一件事两套代码各自漂移」。

.`sh` 现在是薄封装（`exec "$PY" scripts/daily_alert.py "$@"`），macOS 上的 automation
继续用它，行为与本文档逐条一致。

为什么两件事要绑在一起
----------------------
`ctl.py update` 会**先停服再起服**（重建面板要独占内存，服务常驻数 GB）。
其中「起服」只在更新**成功**时才做 —— 取数或重建一旦失败，服务就是停着的，
第 3 步会连不上直接报错。所以这里在第 2 步补一个「服务不在就拉起来」的兜底。

用法
----
  python scripts/daily_alert.py                 # 正常（无人值守用这个）
  python scripts/daily_alert.py --dry-run       # 不真发飞书，只打印消息
  python scripts/daily_alert.py --skip-update   # 跳过更新，只用现有面板检查（秒级）
  python scripts/daily_alert.py --skip-update --dry-run

  Windows 不想敲命令：双击仓库根目录的 daily_alert.cmd。

可选环境变量：PORT（默认 8770）、PY / ASTOCK_PY、A_STOCK_ALERT_WEBHOOK

自动化（由 WorkBuddy automation 托管，名称「A股熊市有信号提醒」，周一至周五 19:30）。
等价 crontab：
  30 19 * * 1-5 cd /path/to/a-stock && python scripts/daily_alert.py >> logs/daily_alert.log 2>&1

退出码
------
  0  正常（含「条件不满足，未发送」—— 不是失败）
  非 0  任一环节失败（update / start / alert 三个 rc 取第一个非 0）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IS_WIN = os.name == "nt"
CST = timezone(timedelta(hours=8))

# 中文 Windows 控制台是 GBK：print emoji/中文可能崩，统一交给 _console。
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from _console import bootstrap  # noqa: E402

    bootstrap()
except Exception:  # noqa: BLE001  —— _console 缺失不致命
    pass


# ---------------------------------------------------------------- 解释器
# ⚠️ 顺序有讲究：`log()` 要在 `find_python()` **之前**定义，
#    因为探测失败时会调用 log 提示「已跳过 xxx」。原先 log 定义在
#    `PY = find_python()` 之后 —— 平时不需要提示所以一直没暴露，
#    只有在**真的遇到坏解释器**（正是最需要那句提示的时候）才 NameError 崩掉。
def log(msg: str) -> None:
    stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{stamp}] {msg}", flush=True)


def _usable(py: str) -> bool:
    """该解释器能不能跑本项目（需要 numpy/pandas/pyarrow）。

    ⚠️ 为什么必须真的试一次 import，而不是只看文件存在
    ------------------------------------------------
    本脚本自己只用标准库，但第 1 步会去跑 `ctl.py update`，而它拉起的
    服务进程要 import numpy/pandas。**光判断路径存在是不够的**：本机 PATH 上
    的 python3 就是个没装 pandas 的解释器（WorkBuddy 托管版），
    `-x` 和 `Path.exists()` 都通过，跑起来才炸 —— 而那时已经过了很久。
    """
    try:
        r = subprocess.run(
            [py, "-c", "import pandas, numpy, pyarrow"],
            capture_output=True, timeout=60,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_python() -> str:
    """挑一个能跑本项目的 python。

    优先级：env PY / ASTOCK_PY（显式指定，无条件尊重）
            → 常见虚拟环境（项目环境优先，pandas 大版本与开发时一致）
            → PATH 上的 python3/python（**必须先试 import 通过**）
            → 本进程解释器（兜底，交给下游报错）

    ⚠️ PATH 上的候选要 import 验证：本机 `python3` 是没装 pandas 的托管解释器，
       `Path.exists()` 会通过，但一跑就 ModuleNotFoundError。跳过它并提示一句，
       而不是把一个注定失败的解释器交给下游。
    """
    for k in ("A_STOCK_PY", "ASTOCK_PY", "PY"):
        v = os.environ.get(k)
        if v and Path(v).exists():
            return v          # 显式指定 → 尊重用户，哪怕它不可用

    # 常见虚拟环境（跨平台：Windows 是 Scripts/，POSIX 是 bin/）
    cands = [
        Path.home() / ".workbuddy-ai" / "binaries" / "python" / "envs" / "default"
        / ("Scripts/python.exe" if IS_WIN else "bin/python"),
        Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "default"
        / ("Scripts/python.exe" if IS_WIN else "bin/python"),
        ROOT / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python"),
    ]
    for c in cands:
        if c.exists():
            if _usable(str(c)):
                return str(c)
            log(f"提示：{c} 存在但缺少 pandas/numpy，已跳过")

    # PATH 上的解释器 —— 必须试过 import 才敢用
    import shutil as _shutil

    for name in ("python3", "python"):
        found = _shutil.which(name)
        if not found:
            continue
        if _usable(found):
            return found
        log(f"提示：PATH 上的 {name}（{found}）缺少 pandas/numpy，已跳过")

    log("!! 没找到装了 pandas/numpy/pyarrow 的解释器，回退到当前解释器")
    return sys.executable


PY = find_python()


def run(cmd: list[str]) -> int:
    """同步运行子进程，透传输出与 UTF-8 环境，返回退出码。"""
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8:replace")
    try:
        return subprocess.run(cmd, cwd=str(ROOT), env=env).returncode
    except FileNotFoundError:
        print(f"!! 找不到命令：{cmd[0]}")
        return 127
    except KeyboardInterrupt:
        print("\n!! 被中断")
        return 130


def svc_up(port: int, timeout: float = 4.0) -> bool:
    """服务是否已就绪（就绪 = /api/meta 能返回 JSON；加载中是 502 → 视为未就绪）。

    ⚠️ 必须用 urllib 而不是 socket 探端口：面板加载期间端口**已经在监听**，
       但请求会 502。只看端口会把「加载中」误判成「就绪」。
    """
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/meta", timeout=timeout) as r:
            r.read()
        return True
    except Exception:  # noqa: BLE001
        return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="daily_alert.py",
        description="每日数据更新 + 「熊市且有信号」飞书提醒（跨平台）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/daily_alert.py\n"
            "  python scripts/daily_alert.py --dry-run\n"
            "  python scripts/daily_alert.py --skip-update --dry-run\n"
            "  python scripts/daily_alert.py --force --dry-run\n\n"
            "Windows 双击入口：根目录 daily_alert.cmd\n"
        ),
    )
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8770))
    p.add_argument("--dry-run", action="store_true",
                   help="不真发飞书，只打印消息")
    p.add_argument("--skip-update", action="store_true",
                   help="跳过数据更新，直接用现有面板检查（秒级）")
    # ⚠️ 默认**不开**浏览器：本脚本的典型用法是无人值守（automation / 计划任务），
    #    弹浏览器既没用还会在无桌面会话里报错。这与原 daily_alert.sh 硬编码
    #    `--no-browser` 的行为一致，别改成默认开。
    p.add_argument("--browser", action="store_true",
                   help="兜底起服后打开浏览器（手动运行时才需要）")
    # 以下透传给 42_market_alert.py
    p.add_argument("--force", action="store_true", help="忽略触发条件，强制发送（自测用）")
    p.add_argument("--check", action="store_true",
                   help="只自检「发送通道配置 + 服务连通性」，不更新、不取信号、不发送")
    p.add_argument("--max-stale", type=int, default=None,
                   help="最新交易日最多可陈旧几个自然日（默认 4）")
    p.add_argument("--to", default=None, help="lark-cli 收件人 open_id（回退通道用）")
    p.add_argument("--params", default=None, help="覆盖选股参数（JSON 字符串）")
    return p


def main() -> int:
    a = build_parser().parse_args()
    port = a.port

    # --check 是纯自检：不更新数据、不取信号、不发送。
    # ⚠️ 必须在第 1 步之前就分流，否则用户以为「只是检查一下」却触发了 18 分钟重建。
    if a.check:
        log("== 自检模式（--check）：不更新、不取信号、不发送 ==")
        return run([PY, "scripts/42_market_alert.py", "--port", str(port), "--check"])

    # ---------------------------------------------------------- 1/3 更新
    upd_rc = 0
    if a.skip_update:
        log("== 1/3 跳过数据更新（--skip-update：直接用现有面板检查）==")
    else:
        log("== 1/3 数据更新（停服→取数→重建→起服，约 18 分钟）==")
        upd_rc = run([PY, "scripts/ctl.py", "update", "--no-browser"])
        if upd_rc != 0:
            log(f"!! 数据更新失败（exit={upd_rc}）—— 服务此刻很可能是停着的，"
                f"下一步会兜底拉起。")
            log("   若数据因此陈旧，42_market_alert.py 的新鲜度护栏（--max-stale）"
                "会拦下提醒。")

    # ---------------------------------------------------------- 2/3 确保服务在跑
    # `ctl.py update` 只在更新成功后才起服；失败时服务停着 → 第 3 步必然连不上。
    # 这里无条件确认一次，服务不在就拉起来（start 会自带就绪轮询 + 日志尾部诊断）。
    if svc_up(port):
        log(f"== 2/3 服务已在运行（端口 {port}）==")
    else:
        log(f"== 2/3 服务未就绪，尝试启动（python scripts/ctl.py start）==")
        start_cmd = [PY, "scripts/ctl.py", "start", "--port", str(port)]
        # 无人值守默认不开浏览器；只有用户显式 --browser 时才开
        if not a.browser:
            start_cmd.append("--no-browser")
        start_rc = run(start_cmd)
        if start_rc != 0:
            log(f"!! 服务启动失败（exit={start_rc}）。详情见 logs/server-{port}.log。")
            if upd_rc == 0:
                upd_rc = start_rc

    # ---------------------------------------------------------- 3/3 检查 + 提醒
    log("== 3/3 检查「熊市 + 有信号」==")
    cmd = [PY, "scripts/42_market_alert.py", "--port", str(port)]
    if a.dry_run:
        cmd.append("--dry-run")
    if a.force:
        cmd.append("--force")
    if a.max_stale is not None:
        cmd += ["--max-stale", str(a.max_stale)]
    if a.to:
        cmd += ["--to", a.to]
    if a.params:
        cmd += ["--params", a.params]
    alert_rc = run(cmd)
    if alert_rc != 0:
        log(f"!! 提醒检查失败（exit={alert_rc}）")

    # 任一环节失败都以非 0 收场（无人值守时便于自动化告警）
    rc = alert_rc
    if upd_rc != 0 and rc == 0:
        rc = upd_rc
    log(f"== 完成（update={upd_rc} alert={alert_rc} → exit={rc}）==")
    return rc


if __name__ == "__main__":
    sys.exit(main())
