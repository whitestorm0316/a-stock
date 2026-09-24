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

PY="${PYTHON:-$(command -v python3 || command -v python)}"

# 兼容老用法：bash app/start.sh 9000  →  --port 9000
if [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]]; then
    exec "$PY" scripts/ctl.py start --port "$1" "${@:2}"
fi

exec "$PY" scripts/ctl.py start "$@"
