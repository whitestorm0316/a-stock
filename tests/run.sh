#!/usr/bin/env bash
# 前端测试套件的统一入口（jsdom 无头 + 真实 Chrome CDP）
#
# 用法：
#   bash tests/run.sh              # 跑全部 jsdom 套件（离线，用 fixtures）
#   bash tests/run.sh cap          # 只跑仓位约束
#   bash tests/run.sh ind          # 只跑行业两级树
#   bash tests/run.sh delist       # 只跑退市风险过滤
#   bash tests/run.sh cdp          # 跑真实 Chrome 端到端（需先启动 app/server.py）
#
# ⚠️ 为什么逐条串行跑：5 套 jsdom 同时驻留内存会触发 OOM（SIGTERM 137）。
set -u
cd "$(dirname "$0")/.." || exit 1

NODE="${NODE:-}"
if [ -z "$NODE" ] || ! command -v "$NODE" >/dev/null 2>&1; then
  # Windows：优先用 WorkBuddy 托管的 node；macOS/Linux 退化到 PATH 里的 node
  for cand in \
    "/c/Users/50651/.workbuddy/binaries/node/versions/22.22.2-3/node.exe" \
    "C:/Users/50651/.workbuddy/binaries/node/versions/22.22.2-3/node.exe" \
    "$HOME/.workbuddy-ai/binaries/node/versions/22.22.2-2/bin/node"; do
    if [ -x "$cand" ]; then NODE="$cand"; break; fi
  done
fi
[ -n "$NODE" ] || NODE=node

case "${1:-all}" in
  cap)    SETS="test_cap" ;;
  board)  SETS="test_board" ;;
  ind)    SETS="test_indtree" ;;
  fin)    SETS="test_fin" ;;
  delist) SETS="test_delist" ;;
  trades) SETS="test_trades" ;;
  cdp)    SETS="__CDP__" ;;
  all)    SETS="test_cap test_board test_indtree test_fin test_delist test_trades" ;;
  *) echo "未知参数：$1（可选 cap|board|ind|fin|delist|trades|cdp|all）"; exit 2 ;;
esac

if [ "$SETS" = "__CDP__" ]; then
  echo "== 真实 Chrome 端到端（需要 app/server.py 在 8772 + CDP 在 9333）"
  "$NODE" tests/cdp_cap.js
  exit $?
fi

# ⚠️ 不要用 sed 的非贪婪 `.*?`：macOS 自带的 BSD sed 不支持，会报
#    'repetition-operator operand invalid'。改用 grep -o 提取 N/M。
parse() {
  # 从输出里取**最后**一个 形如 N/M 的数字对（各套件的文案格式不统一：
  #   有的是「通过 32/32」，有的是「50/50 通过」）
  echo "$1" | grep -oE '[0-9]+/[0-9]+' | tail -1
}

total_pass=0; total_fail=0; failed=""

for s in $SETS; do
  printf '%-14s ' "$s"
  out=$("$NODE" "tests/$s.js" 2>&1) || true
  pair=$(parse "$out")
  if [ -z "$pair" ]; then
    echo "❌ 未能解析结果"
    echo "$out" | tail -6
    failed="$failed $s"
    continue
  fi
  p=${pair%%/*}; t=${pair##*/}
  total_pass=$((total_pass + p)); total_fail=$((total_fail + t - p))
  if [ "$p" = "$t" ]; then echo "✅ 通过 $pair"; else echo "❌ 通过 $pair"; failed="$failed $s"; fi
done

echo "------------------------------------------------------------"
if [ -z "$failed" ]; then
  echo "✅ 全部通过：$total_pass 项"
  exit 0
else
  echo "❌ 失败套件：$failed（通过 $total_pass / 共 $((total_pass + total_fail))）"
  exit 1
fi
