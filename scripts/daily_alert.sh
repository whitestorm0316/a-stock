#!/usr/bin/env bash
# ============================================================================
# daily_alert.sh —— 每日数据更新 + 「熊市且有信号」飞书提醒（一条命令走完）
#
# 流程：
#   1. scripts/ctl.py update --no-browser   # 停服 → 增量取数 → 重建面板 → 起服（~18min）
#   2. 确认服务真的在跑（更新失败时 ctl.py **不会**重新起服 → 补一次 start）
#   3. scripts/42_market_alert.py           # 熊市 + 有信号 → 发飞书；否则静默
#
# 为什么两件事要绑在一起：
#   `ctl.py update` 会**先停服再起服**（重建面板要独占内存，服务常驻 ~7GB）。
#   其中「起服」只在更新**成功**时才做 —— 取数或重建一旦失败，服务就是停着的，
#   第 3 步会连不上直接报错。所以这里在第 2 步补一个「服务不在就拉起来」的兜底。
#
# 用法：
#   bash scripts/daily_alert.sh                 # 正常（无人值守用这个）
#   bash scripts/daily_alert.sh --dry-run       # 不真发飞书，只打印消息
#   bash scripts/daily_alert.sh --skip-update   # 跳过更新，只用现有面板检查（秒级）
#   bash scripts/daily_alert.sh --skip-update --dry-run
#
# 可选环境变量：PORT（默认 8770）、PY、NODE_BIN、A_STOCK_ALERT_TO
#
# 定时（由 WorkBuddy automation 托管，名称「A股熊市有信号提醒」，周一至周五 19:30）。
# 等价的 crontab 写法：
#   30 19 * * 1-5 cd /Users/chenqifeng/code/a-stock && bash scripts/daily_alert.sh >> logs/daily_alert.log 2>&1
# ============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

PY="${PY:-/Users/chenqifeng/.workbuddy-ai/binaries/python/envs/default/bin/python}"
NODE_BIN="${NODE_BIN:-/Users/chenqifeng/.workbuddy-ai/binaries/node/versions/22.22.2-3/bin}"
PORT="${PORT:-8770}"

# lark-cli 由飞书连接器装在托管 node 的全局 prefix 下，不在默认 PATH 里
export PATH="$NODE_BIN:$PATH"

DRY=""
SKIP_UPDATE=0
LARK_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --dry-run)     DRY="--dry-run" ;;
    --skip-update) SKIP_UPDATE=1 ;;
    *)             LARK_ARGS+=("$arg") ;;   # 透传给 42_market_alert.py（--force / --max-stale 等）
  esac
done

log() { printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# 服务是否已就绪（就绪 = /api/meta 能返回 JSON；加载中是 502 → 视为未就绪）
svc_up() {
  "$PY" -c "
import sys, urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:$PORT/api/meta', timeout=4).read()
except Exception:
    raise SystemExit(1)
" 2>/dev/null
}

# ---------------------------------------------------------------- 1/3 更新
UPD_RC=0
if [[ $SKIP_UPDATE -eq 1 ]]; then
  log "== 1/3 跳过数据更新（--skip-update：直接用现有面板检查）=="
else
  log "== 1/3 数据更新（停服→取数→重建→起服，约 18 分钟）=="
  "$PY" scripts/ctl.py update --no-browser
  UPD_RC=$?
  if [[ $UPD_RC -ne 0 ]]; then
    log "!! 数据更新失败（exit=${UPD_RC}）—— 服务此刻很可能是停着的，下一步会兜底拉起。"
    log "   若数据因此陈旧，42_market_alert.py 的新鲜度护栏（--max-stale）会拦下提醒。"
  fi
fi

# ---------------------------------------------------------------- 2/3 确保服务在跑
# `ctl.py update` 只在更新成功后才起服；失败时服务停着 → 第 3 步必然连不上。
# 这里无条件确认一次，服务不在就拉起来（start 会自带就绪轮询 + 日志尾部诊断）。
if svc_up; then
  # ⚠️ 变量必须用 ${} 界定：bash 3.2 会把紧跟其后的全角括号字节当成变量名的一部分
  log "== 2/3 服务已在运行（端口 ${PORT}）=="
else
  log "== 2/3 服务未就绪，尝试启动（python scripts/ctl.py start --no-browser）=="
  "$PY" scripts/ctl.py start --port "$PORT" --no-browser
  START_RC=$?
  if [[ $START_RC -ne 0 ]]; then
    log "!! 服务启动失败（exit=${START_RC}）。详情见 logs/server-$PORT.log。"
    [[ $UPD_RC -eq 0 ]] && UPD_RC=$START_RC
  fi
fi

# ---------------------------------------------------------------- 3/3 检查 + 提醒
log "== 3/3 检查「熊市 + 有信号」=="
# ⚠️ macOS 自带 bash 3.2：`set -u` 下展开空数组 `"${arr[@]}"` 会报 unbound variable，
#    而 `${arr[@]+...}` 的嵌套引号又会被 3.2 解析错。老老实实分两支最稳。
if [[ ${#LARK_ARGS[@]} -gt 0 ]]; then
  "$PY" scripts/42_market_alert.py --port "$PORT" $DRY "${LARK_ARGS[@]}"
else
  "$PY" scripts/42_market_alert.py --port "$PORT" $DRY
fi
ALERT_RC=$?
if [[ $ALERT_RC -ne 0 ]]; then
  log "!! 提醒检查失败（exit=${ALERT_RC}）"
fi

# 任一环节失败都以非 0 收场（无人值守时便于自动化告警）
RC=$ALERT_RC
[[ $UPD_RC -ne 0 && $RC -eq 0 ]] && RC=$UPD_RC
log "== 完成（update=${UPD_RC} alert=${ALERT_RC} → exit=${RC}）=="
exit $RC
