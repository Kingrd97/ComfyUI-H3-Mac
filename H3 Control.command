#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$ROOT/runtime/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

if [[ $# -gt 0 ]]; then
  exec "$PYTHON" "$ROOT/scripts/h3_control.py" "$@"
fi

printf '%s\n' \
  'H3 任务控制 / H3 Job Control' \
  '1) 查看状态 / Status' \
  '2) 暂停全部 / Pause all' \
  '3) 继续全部 / Resume all' \
  '4) 自动模式并继续 / Auto + resume' \
  '5) 低功耗模式并继续 / Low + resume' \
  '6) 满功耗模式并继续 / Max + resume'
read -r -p '请选择 / Choose [1-6]: ' CHOICE

case "$CHOICE" in
  1) ACTION=status ;;
  2) ACTION=pause ;;
  3) ACTION=resume ;;
  4) ACTION=auto ;;
  5) ACTION=low ;;
  6) ACTION=max ;;
  *) printf '无效选择 / Invalid choice.\n' >&2; exit 2 ;;
esac

"$PYTHON" "$ROOT/scripts/h3_control.py" "$ACTION" || true
printf '\n'
read -r -p '按回车关闭 / Press Enter to close: ' _
