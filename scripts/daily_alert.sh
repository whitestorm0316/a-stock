#!/usr/bin/env bash
# ============================================================================
# daily_alert.sh —— 每日数据更新 + 「熊市且有信号」飞书提醒
#
# ⚠️ 本脚本现在只是 **薄封装**，真正的编排逻辑在 scripts/daily_alert.py。
#
#    为什么改成这样：
#      1. Windows 没有 bash —— 「每天自动更新 + 有信号发提醒」这件事同样需要能在
#         别人的 Windows 机器上跑，所以把逻辑收进纯标准库的 Python 脚本；
#      2. 两套实现最容易各自漂移（改了 .py 忘了 .sh，或反过来），
#         留一个入口独占逻辑，另一个只负责转发。
#
#    macOS 上的 automation（「A股熊市有信号提醒」）继续调用本 .sh，
#    行为与直接跑 .py 逐条一致。
#
# 原流程（现由 daily_alert.py 实现）：
#   1. scripts/ctl.py update --no-browser   # 停服 → 增量取数 → 重建面板 → 起服（~18min）
#   2. 确认服务真的在跑（更新失败时 ctl.py **不会**重新起服 → 补一次 start）
#   3. scripts/42_market_alert.py           # 熊市 + 有信号 → 发飞书；否则静默
#
# 为什么两件事要绑在一起：
#   `ctl.py update` 会**先停服再起服**（重建面板要独占内存，服务常驻数 GB）。
#   其中「起服」只在更新**成功**时才做 —— 取数或重建一旦失败，服务就是停着的，
#   第 3 步会连不上直接报错。所以这里在第 2 步补一个「服务不在就拉起来」的兜底。
#
# 用法（与 .py 完全相同）：
#   bash scripts/daily_alert.sh                 # 正常（无人值守用这个）
#   bash scripts/daily_alert.sh --dry-run       # 不真发飞书，只打印消息
#   bash scripts/daily_alert.sh --skip-update   # 跳过更新，只用现有面板检查（秒级）
#   bash scripts/daily_alert.sh --check         # 自检发送通道与服务连通性
#   bash scripts/daily_alert.sh --skip-update --dry-run
#
# 可选环境变量：PORT（默认 8770）、PY / ASTOCK_PY、A_STOCK_ALERT_WEBHOOK
#
# 定时（由 WorkBuddy automation 托管，名称「A股熊市有信号提醒」，周一至周五 19:30）。
# 等价的 crontab 写法：
#   30 19 * * 1-5 cd /path/to/a-stock && bash scripts/daily_alert.sh >> logs/daily_alert.log 2>&1
#
# Windows 用户：双击仓库根目录的 daily_alert.cmd（等价入口）。
# ============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

# 解释器优先级与 daily_alert.py 的 find_python() 保持一致：
#   env PY / ASTOCK_PY → 托管环境 → 仓库 .venv → 本机 python3
PY="${PY:-${ASTOCK_PY:-}}"
if [[ -z "$PY" ]]; then
  for cand in \
    "$HOME/.workbuddy-ai/binaries/python/envs/default/bin/python" \
    "$HOME/.workbuddy/binaries/python/envs/default/bin/python" \
    "$ROOT/.venv/bin/python"
  do
    [[ -x "$cand" ]] && { PY="$cand"; break; }
  done
fi
PY="${PY:-python3}"

exec "$PY" scripts/daily_alert.py "$@"
