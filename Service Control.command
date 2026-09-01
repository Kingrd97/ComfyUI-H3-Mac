#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$ROOT/runtime/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

if [[ $# -gt 0 ]]; then
  exec "$PYTHON" "$ROOT/scripts/launchd.py" "$@"
fi

printf '%s\n' \
  'ComfyUI / vpipe 后台服务' \
  '1) 查看状态' \
  '2) 安装并启动（不会重启已运行任务）' \
  '3) 重启全部服务（会中断当前 UI 请求）' \
  '4) 只重启 vpipe worker' \
  '5) 停止全部服务' \
  '6) 卸载 launchd 服务'
read -r -p '请选择 [1-6]: ' CHOICE
case "$CHOICE" in
  1) ACTION=(status) ;;
  2) ACTION=(install) ;;
  3) ACTION=(restart) ;;
  4) ACTION=(restart --worker-only) ;;
  5) ACTION=(stop) ;;
  6) ACTION=(uninstall) ;;
  *) printf '无效选择。\n' >&2; exit 2 ;;
esac
"$PYTHON" "$ROOT/scripts/launchd.py" "${ACTION[@]}" || true
printf '\n'
read -r -p '按回车关闭: ' _
