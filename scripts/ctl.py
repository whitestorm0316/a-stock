#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ctl.py —— 选股器的统一启停 / 数据更新入口（Windows / macOS / Linux 通用）

为什么需要它
------------
原来启动要记住三条散落的命令，且各自都有坑：

  · app/start.sh 里硬编码了作者机器的 macOS 路径，换机器必然失败
  · Windows 没有 bash，得手写 taskkill + python app/server.py PORT
  · 更新数据前**必须先停服务**（服务常驻约 7GB，同时重建面板会 OOM）
  · 面板加载要 80~100 秒，期间 curl/浏览器访问一律 502，难判断是崩了还是在加载

本脚本把这些固化成一个入口，逐个解决：

  start    自动释放占用端口 → 后台拉起服务 → 轮询 /api/meta 直到就绪 → 自动开浏览器
  stop     按端口精确查杀监听进程（不是靠 Ctrl-C）
  restart  停 → 启
  update   停服务 → 跑 99_daily_update.py → 重建面板 → 启服务（一条命令走完每日流程）
  status   服务状态 + 数据文件清单 + 最近交易日

日志写到 logs/server-<port>.log，启动失败时会自动打印日志尾部。

用法
----
  python scripts/ctl.py status
  python scripts/ctl.py start            # 默认 8770，就绪后自动开浏览器
  python scripts/ctl.py start --port 9000 --no-browser
  python scripts/ctl.py stop
  python scripts/ctl.py restart
  python scripts/ctl.py update           # 原始数据 + 重建面板，最后自动起服务
  python scripts/ctl.py update --no-rebuild
  python scripts/ctl.py update --industry        # 顺带刷新行业归属（建议每周一次）
  python scripts/ctl.py update --no-start        # 只更新，不自动起服务

Windows 上不想敲命令的话，双击仓库根目录的 start.cmd / stop.cmd / update.cmd。

依赖：只用标准库，不需要装 psutil。查杀端口依赖系统命令
（Windows: PowerShell Get-NetTCPConnection；macOS/Linux: lsof）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
IS_WIN = os.name == "nt"
LOG_DIR = ROOT / "logs"
DEFAULT_PORT = 8770

# 启动必需 vs 可选（缺失只降级，不崩）
REQUIRED = ["data/processed/v2_panel.parquet"]
OPTIONAL = {
    "data/processed/panel.parquet": "基础面板（缺失会直接起不来）",
    "data/processed/fin_panel.parquet": "财务筛选页",
}


# ---------------------------------------------------------------- 基础工具

def filesize_mb(p: Path) -> str:
    if not p.exists():
        return "-"
    return f"{p.stat().st_size / 1024 / 1024:,.0f} MB"


def port_open(port: int, timeout: float = 0.6) -> bool:
    """端口是否有人在听（不关心对方是不是本服务）。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def fetch_meta(port: int, timeout: float = 3.0) -> Optional[dict]:
    """拿到 /api/meta 说明服务已就绪；否则 None（含加载中的 502）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None


def _run_out(cmd: list[str], timeout: int = 25) -> str:
    """执行外部命令取 stdout。

    ⚠ 绝对不要开 text=True：中文 Windows 的 netstat / tasklist 输出是 GBK，
    按 UTF-8 解码会抛 UnicodeDecodeError，异常发生在 subprocess 的读线程里，
    结果是**整个 stdout 被丢掉、命令看起来像什么都没输出**（踩过一次）。
    这里统一按字节读 + errors="replace"：中文变乱码无所谓，
    我们要解析的只有 PID 数字和 LISTENING 这类 ASCII 标记，不会被破坏。
    """
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    except (FileNotFoundError, OSError):
        return ""
    return p.stdout.decode("utf-8", errors="replace")


def _ps_pids(port: int) -> list[int]:
    out = _run_out([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        f"Get-NetTCPConnection -LocalPort {port} -State Listen "
        f"-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess",
    ])
    return [int(t) for t in out.split() if t.isdigit() and int(t) > 0]


def _netstat_pids(port: int) -> list[int]:
    """PowerShell 不可用时的兜底（某些沙箱/精简系统会禁 powershell.exe）。"""
    out = _run_out(["netstat", "-ano", "-p", "TCP"])
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        # 形如：TCP  127.0.0.1:8770  0.0.0.0:0  LISTENING  20412
        if len(parts) < 4 or "LISTENING" not in line:
            continue
        if not parts[1].endswith(f":{port}") and not parts[1].endswith(f".{port}"):
            continue
        if parts[-1].isdigit():
            v = int(parts[-1])
            if v not in pids:
                pids.append(v)
    return pids


def pids_on_port(port: int) -> list[int]:
    """查监听该端口的进程 PID。无第三方依赖，查不到返回空列表。"""
    if IS_WIN:
        pids = _ps_pids(port)
        return pids or _netstat_pids(port)

    if not shutil.which("lsof"):
        print("  !! 找不到 lsof，无法自动查杀端口，请手动处理")
        return []
    out = _run_out(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
    pids = []
    for tok in out.split():
        if tok.isdigit():
            v = int(tok)
            if v not in pids:
                pids.append(v)
    return pids


def kill_pid(pid: int) -> None:
    if IS_WIN:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        for sig in (15, 9):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
            except PermissionError:
                return
            time.sleep(0.6)
            try:
                os.kill(pid, 0)
            except OSError:
                return


def free_gb() -> Optional[float]:
    """可用物理内存（GB）。拿不到就返回 None，不做无谓猜测。"""
    try:
        if IS_WIN:
            out = _run_out([
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory",
            ]).strip()
            kb = float(out.splitlines()[0].split(".")[0]) if out else None
            return kb / 1024 / 1024 if kb else None
        if sys.platform == "linux":
            for line in (Path("/proc/meminfo").read_text().splitlines() or []):
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except Exception:  # noqa: BLE001
        return None
    return None


def check_data() -> list[str]:
    return [f for f in REQUIRED if not (ROOT / f).exists()]


def stop_port(port: int, wait: int = 25) -> bool:
    pids = pids_on_port(port)
    if not pids:
        return True
    print(f"  释放端口 {port}（PID {', '.join(map(str, pids))}）...")
    for pid in pids:
        kill_pid(pid)
    deadline = time.time() + wait
    while time.time() < deadline:
        if not port_open(port):
            print("  端口已释放")
            return True
        time.sleep(0.8)
    print(f"  !! 端口 {port} 仍被占用，请手动查杀")
    return False


# ---------------------------------------------------------------- 子命令

def cmd_status(args: argparse.Namespace) -> int:
    port = args.port
    print("=" * 56)
    print("  服务状态")
    print("=" * 56)
    meta = fetch_meta(port, timeout=4) if port_open(port) else None
    if meta:
        print(f"  运行中      http://127.0.0.1:{port}/")
        print(f"  最近交易日  {meta.get('last_date')}")
        print(f"  交易日数    {meta.get('n_days')}")
        print(f"  标的数量    {meta.get('n_stocks')}")
        print(f"  行业数量    {len(meta.get('industries') or [])}")
        print(f"  财务面板    {'可用' if meta.get('fin_avail') else '不可用（财务筛选将缺失）'}")
    elif port_open(port):
        print(f"  端口 {port} 被占用但服务未就绪")
        print("  可能是正在加载面板（约 80~100 秒），稍后重试本命令确认")
    else:
        print(f"  未运行      （python scripts/ctl.py start）")
        print("             注：刚启动的头 ~80 秒服务在加载面板，此时端口还没开始监听，")
        print("             也可能显示成未运行 —— 等一会儿再看，别急着重跑。")

    print()
    print("  数据文件")
    for f in REQUIRED:
        p = ROOT / f
        print(f"    [必需] {f:<38} {filesize_mb(p):>10}")
    for f, desc in OPTIONAL.items():
        p = ROOT / f
        ok = "有" if p.exists() else "无"
        print(f"    [可选] {f:<38} {filesize_mb(p):>10}   {ok} · {desc}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    port = args.port
    if not port_open(port):
        print(f"端口 {port} 上没有运行中的服务")
        return 0
    return 0 if stop_port(port) else 1


def cmd_start(args: argparse.Namespace) -> int:
    port = args.port

    missing = check_data()
    if missing:
        print("!! 缺少启动必需的数据文件：")
        for f in missing:
            print(f"     {f}")
        print()
        print("   数据不入库（体积超限 GitHub），需本地重建：")
        print("     python scripts/01_fetch_data.py")
        print("     python scripts/01b_fetch_gap.py")
        print("     python scripts/01c_fetch_index.py")
        print("     python scripts/20_fetch_industry.py")
        print("     python scripts/02_build_dataset.py")
        print("     python scripts/21_build_v2_features.py")
        return 2

    for f, desc in OPTIONAL.items():
        if not (ROOT / f).exists():
            print(f"  · 提示：{f} 不存在 → {desc}不可用")

    if port_open(port):
        print(f"端口 {port} 已被占用，先停止旧服务")
        if not stop_port(port):
            return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"server-{port}.log"

    print("=" * 56)
    print(f"  启动选股器  →  http://127.0.0.1:{port}/")
    print(f"  日志        {log_path}")
    print("=" * 56)

    kw = dict(cwd=str(ROOT), stdin=subprocess.DEVNULL)
    if IS_WIN:
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True

    try:
        lf = open(log_path, "a", encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"!! 无法写日志：{e}")
        return 1

    try:
        proc = subprocess.Popen(
            [sys.executable, "app/server.py", str(port)],
            stdout=lf, stderr=subprocess.STDOUT, **kw,
        )
    finally:
        lf.close()

    print(f"  进程 PID {proc.pid}，正在加载面板（通常 80~100 秒，期间访问会 502）...")

    t0 = time.time()
    deadline = t0 + args.wait
    last_msg = t0
    meta: Optional[dict] = None
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"\n!! 服务进程已退出（code={proc.returncode}），日志尾部：")
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
                for line in tail:
                    print("   " + line)
            except OSError:
                pass
            return 1
        meta = fetch_meta(port, timeout=3)
        if meta:
            break
        now = time.time()
        if now - last_msg >= 15:
            print(f"     ... 已等待 {now - t0:.0f}s")
            last_msg = now
        time.sleep(2)

    if not meta:
        print(f"\n!! 等待 {args.wait}s 仍未就绪，请查看日志 {log_path}")
        return 1

    print(f"\n  就绪（{time.time() - t0:.0f}s）")
    print(f"    最近交易日  {meta.get('last_date')}")
    print(f"    标的 {meta.get('n_stocks')} 只 / {meta.get('n_days')} 个交易日")
    print(f"    财务面板    {'可用' if meta.get('fin_avail') else '不可用'}")

    if not args.no_browser:
        try:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception:  # noqa: BLE001
            pass
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    stop_port(args.port)
    return cmd_start(args)


def cmd_update(args: argparse.Namespace) -> int:
    script = ROOT / "scripts" / "99_daily_update.py"
    if not script.exists():
        print(f"!! 找不到 {script}")
        return 2

    # 服务常驻约 7GB，与 02/21 重建争内存会 OOM，必须先停
    print("=" * 56)
    print("  每日更新")
    print("=" * 56)
    if port_open(args.port):
        print("  停止选股器服务（释放内存，否则重建面板会 OOM）")
        stop_port(args.port)
    else:
        print("  服务未运行，跳过停止")

    gb = free_gb()
    if args.rebuild and gb is not None:
        print(f"  可用内存 {gb:.1f} GB")
        if gb < 6:
            print("  !! 可用内存偏低，重建面板可能失败。建议关掉占内存的其它程序后重试。")
            if gb < 3:
                print("  !! 低于 3GB，建议改用 --no-rebuild（只更新原始数据）")

    cmd = [sys.executable, str(script)]
    if args.rebuild:
        cmd.append("--rebuild")
    if args.industry:
        cmd.append("--industry")
    if getattr(args, "since", None):
        cmd += ["--since", args.since]

    flags = []
    if args.rebuild:
        flags.append("--rebuild")
    if args.industry:
        flags.append("--industry")
    if getattr(args, "since", None):
        flags += ["--since", args.since]
    print("  $ python scripts/99_daily_update.py " + " ".join(flags))
    print(f"  预计耗时：{'约 18 分钟（含重建面板）' if args.rebuild else '约 10.5 分钟'}")
    print("  输出如下：\n" + "-" * 56)

    t0 = time.time()
    rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    print("-" * 56)
    if rc != 0:
        print(f"!! 更新失败（code={rc}），耗时 {time.time() - t0:.0f}s")
        print("   原始数据可采用追加式写盘，中途失败通常不会损坏已有数据，重跑即可。")
        return rc
    print(f"  更新完成，耗时 {time.time() - t0:.0f}s")

    if args.start:
        print()
        return cmd_start(args)
    print("  服务未启动（--no-start），需要时运行：python scripts/ctl.py start")
    return 0


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ctl.py",
        description="选股器统一控制入口（start / stop / restart / update / status）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Windows 双击：根目录 start.cmd / stop.cmd / update.cmd",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, help_txt in [
        ("start", "启动服务（就绪后自动打开浏览器）"),
        ("stop", "停止服务"),
        ("restart", "重启服务"),
        ("status", "查看服务与数据状态"),
        ("update", "每日增量更新 + 重建面板"),
    ]:
        sp = sub.add_parser(name, help=help_txt)
        sp.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口（默认 {DEFAULT_PORT}）")
        # start 与 restart 最终都走 cmd_start，会读这两个属性，必须一起提供
        if name in ("start", "restart"):
            sp.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
            sp.add_argument("--wait", type=int, default=600, help="就绪等待上限秒数（默认 600）")
        if name == "update":
            sp.add_argument("--no-rebuild", dest="rebuild", action="store_false",
                            help="只更新原始数据，不重建面板")
            sp.add_argument("--industry", action="store_true",
                            help="顺带刷新行业归属（新股用，建议每周一次）")
            sp.add_argument("--since", help="手动指定起始日期 YYYY-MM-DD，回补某段区间")
            sp.add_argument("--no-start", dest="start", action="store_false",
                            help="更新完不自动启动服务")
            sp.add_argument("--wait", type=int, default=600, help="就绪等待上限秒数")

    return p


def main() -> int:
    args = build_parser().parse_args()
    os.chdir(ROOT)
    fn = {
        "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
        "status": cmd_status, "update": cmd_update,
    }[args.cmd]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
