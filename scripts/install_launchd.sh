#!/usr/bin/env bash
# ============================================================================
# install_launchd.sh —— 把「每日 15:30 更新 + 飞书播报」装成 macOS 开机自启任务
#
# 为什么需要它
# ------------
# WorkBuddy 的 automation（名称「A股每日飞书播报…」）是**应用内调度器**：
# 只有 WorkBuddy 应用在运行时才会触发。**关机重启、或没开 WorkBuddy，那次就跑不了。**
# 而 scripts/daily_alert.sh 本身是自包含的（自己探测 python、自己兜底起服），
# 完全不依赖 WorkBuddy —— 所以可以交给 macOS 自己的调度器 launchd 来跑。
#
# 装完后的效果：
#   · 开机自动加载，不需要手动启动任何东西
#   · 每周一~周五 15:30 自动跑 `daily_alert.sh`（更新 ~18min → 飞书播报）
#   · 到点时机器在睡眠/关机 → 等机器可用后**补跑一次**（launchd 默认行为）
#   · 日志：logs/launchd-daily-alert.log / .err.log
#
# ⚠️ 与 WorkBuddy automation 会**重复触发**！二选一：
#   装了 launchd 就建议把 WorkBuddy 里那条 automation 停掉（或反过来）。
#
# 用法：
#   bash scripts/install_launchd.sh            # 安装（幂等：重复执行会先卸载再装）
#   bash scripts/install_launchd.sh --uninstall
#   bash scripts/install_launchd.sh --status
#   bash scripts/install_launchd.sh --run-now  # 立刻触发一次（验证用，会真跑 18min 更新）
# ============================================================================
set -uo pipefail

LABEL="com.astock.daily-alert"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/scripts/$LABEL.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"

say() { printf '%s\n' "$*"; }

need_plist() {
  if [ ! -f "$SRC" ]; then
    say "!! 找不到 $SRC"
    exit 1
  fi
}

do_status() {
  say "== 状态：$LABEL =="
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    say "已加载 ✅"
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state|program|path" | head -6
  else
    say "未加载 ❌"
  fi
  say ""
  say "plist 安装位置：$DST"
  [ -f "$DST" ] && say "  （已就位）" || say "  （不存在）"
  say ""
  say "最近日志："
  for f in "$ROOT/logs/launchd-daily-alert.log" "$ROOT/logs/launchd-daily-alert.err.log"; do
    [ -f "$f" ] && { say "  --- $f (尾部) ---"; tail -8 "$f"; }
  done
}

do_uninstall() {
  say "== 卸载 $LABEL =="
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && say "已从 launchd 卸载" || say "（未加载或卸载失败，继续删文件）"
  if [ -f "$DST" ]; then rm -f "$DST" && say "已删除 $DST"; fi
  say "完成。"
}

do_install() {
  need_plist
  say "== 安装 $LABEL =="

  # 幂等：先卸载旧的（忽略失败）
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && say "  卸载旧实例" || true

  mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"
  cp "$SRC" "$DST" || { say "!! 复制 plist 失败"; exit 1; }
  say "  已复制 → $DST"

  # 现代写法（macOS 10.10+）。老的 `launchctl load` 已废弃但在部分版本仍可用。
  if launchctl bootstrap "$DOMAIN" "$DST" 2>/dev/null; then
    say "  bootstrap 成功 ✅"
  elif launchctl load "$DST" 2>/dev/null; then
    say "  load 成功 ✅（legacy 方式）"
  else
    say ""
    say "!! 自动加载失败。常见原因："
    say "   1. 在受限终端/沙箱里跑 —— 请改用**系统自带的「终端」App** 再试；"
    say "   2. 权限问题 —— 试试前面加 sudo（会装成系统级，需谨慎）；"
    say "   3. plist 语法问题 —— 用 plutil -lint \"$SRC\" 自查。"
    say ""
    say "   手动加载命令（在「终端」App 里执行）："
    say "     launchctl bootstrap $DOMAIN \"$DST\""
    exit 1
  fi

  say ""
  say "== 完成 =="
  say "  触发时间：周一~周五 15:30"
  say "  日志：$ROOT/logs/launchd-daily-alert.log"
  say "  手动触发一次：bash scripts/install_launchd.sh --run-now"
  say ""
  say "  ⚠️ 已装 launchd 后，建议把 WorkBuddy 里同名的 automation 停掉，避免重复跑。"
}

do_run_now() {
  say "== 立刻触发 $LABEL =="
  if launchctl kickstart -p "$DOMAIN/$LABEL" 2>/dev/null; then
    say "已触发 ✅ 日志：$ROOT/logs/launchd-daily-alert.log"
    say "（这会真跑一次完整更新，约 18 分钟）"
  else
    say "!! 触发失败 —— 任务未加载？先跑 bash scripts/install_launchd.sh"
    exit 1
  fi
}

case "${1:-install}" in
  --uninstall|-u) do_uninstall ;;
  --status|-s)    do_status ;;
  --run-now|-r)   do_run_now ;;
  --help|-h)      sed -n '2,30p' "${BASH_SOURCE[0]}" ;;
  *)              do_install ;;
esac
