#!/bin/bash
# 启动「A股超跌反转 · 交互式选股器」
# 用法：bash app/start.sh [端口]     默认 8770
PORT="${1:-8770}"
PY="/Users/chenqifeng/.workbuddy-ai/binaries/python/envs/default/bin/python"
cd "$(dirname "$0")/.." || exit 1
echo "正在启动（首次加载面板约 60~90 秒）..."
exec "$PY" app/server.py "$PORT"
