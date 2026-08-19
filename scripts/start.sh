#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMFY="$PROJECT_ROOT/runtime/ComfyUI"
PYTHON="$PROJECT_ROOT/runtime/.venv/bin/python"
PORT="${H3_COMFY_PORT:-8188}"

if [[ ! -x "$PYTHON" || ! -f "$COMFY/main.py" ]]; then
  printf '尚未安装，请先双击 Install.command。\n' >&2
  exit 1
fi

(sleep 2; open "http://127.0.0.1:$PORT") >/dev/null 2>&1 &
cd "$COMFY"
exec "$PYTHON" main.py --listen 127.0.0.1 --port "$PORT" "$@"
