#!/usr/bin/env bash
# 启动「A股超跌反转 · 交互式选股器」
#
# 用法：
#   bash app/start.sh              # 默认端口 8770，就绪后自动开浏览器
#   bash app/start.sh 9000         # 自定义端口
#   bash app/start.sh --no-browser # 不自动开浏览器
#
# 具体逻辑在 scripts/ctl.py（Windows / macOS / Linux 通用）。
# 以前这里硬编码过作者机器的 macOS Python 绝对路径，换机器必然失败，已移除。
# Windows 用户请改用根目录的 start.cmd（双击即可）。
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.."

# ---- 解释器探测 ------------------------------------------------------------
# ⚠️ 不能简单地 `command -v python3` 了事。本脚本（ctl.py）只用标准库，
#    所以随便一个 python3 都能「跑起来」，但服务进程要 import numpy/pandas。
#    实测坑：本机 PATH 上的 python3 是没装 pandas 的托管解释器 ——
#    `bash app/start.sh` 会在子进程里炸出 `No module named 'numpy'`，
#    看起来像「代码坏了」，其实是解释器选错了。
#    所以这里逐个候选**试 import 一下**，选第一个真的能跑服务的。
#
# 排序原则：**不能只看「能 import」**。本机同时存在多个可用解释器：
#     /usr/bin/python3        3.9.6  + pandas 2.3.3   ← 系统自带，能跑
#     …/envs/default/bin/python 3.13 + pandas 3.0.6  ← 本项目正式环境
#     pandas 2.x 与 3.x 大版本不同，本项目按 3.13 + pandas 3.0 开发与测试过，
#     因此**项目环境优先于系统 python**，否则会静默跑在另一个 pandas 上。
#     顺序：$PYTHON → PATH 上的 python3/python → 项目环境 → 系统兜底。
_can_run() {
    "$1" -c 'import pandas, numpy, pyarrow' >/dev/null 2>&1
}

PY=""
_skipped=""
if [[ -n "${PYTHON:-}" ]]; then
    PY="$PYTHON"                      # 用户显式指定 → 无条件尊重（哪怕它坏）
    echo "使用 PYTHON 指定的解释器：$PY"
else
    for cand in \
        "$(command -v python3 2>/dev/null || true)" \
        "$(command -v python 2>/dev/null || true)" \
        "$HOME/.workbuddy-ai/binaries/python/envs/default/bin/python" \
        "/opt/homebrew/bin/python3" \
        "/usr/local/bin/python3" \
        "/usr/bin/python3"
    do
        [[ -n "$cand" && -x "$cand" ]] || continue
        if _can_run "$cand"; then PY="$cand"; break; fi
        # 记下第一个被跳过的，稍后提示一句 —— 避免用户以为「怎么换了 python」
        [[ -z "$_skipped" ]] && _skipped="$cand"
    done
fi

if [[ -z "$PY" ]]; then
    echo "!! 没找到装了 pandas/numpy/pyarrow 的 Python 解释器。" >&2
    echo "   当前 PATH 上的 python3：$(command -v python3 2>/dev/null || echo '(无)')" >&2
    echo "" >&2
    echo "   装上依赖：" >&2
    echo "     $(command -v python3 2>/dev/null || echo python3) -m pip install pandas numpy pyarrow" >&2
    echo "   或指定已有解释器：" >&2
    echo "     PYTHON=/path/to/python3 bash app/start.sh" >&2
    exit 2
fi

if [[ -n "$_skipped" ]]; then
    echo "提示：$_skipped 缺少 pandas/numpy，已改用 $PY"
fi

_can_run "$PY" || {
    # 只可能是显式 PYTHON= 指到了坏解释器，交给 ctl.py 给出完整提示
    exec "$PY" scripts/ctl.py start "$@"
}

# 兼容老用法：bash app/start.sh 9000  →  --port 9000
if [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]]; then
    exec "$PY" scripts/ctl.py start --port "$1" "${@:2}"
fi

exec "$PY" scripts/ctl.py start "$@"
