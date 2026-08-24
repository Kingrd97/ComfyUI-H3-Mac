#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$ROOT/runtime/.venv/bin/python"
if [[ "${H3_FOREGROUND:-0}" == "1" ]]; then
  exec "$ROOT/scripts/start.sh" "$@"
fi
"$PYTHON" "$ROOT/scripts/launchd.py" install
if [[ "${H3_NO_OPEN:-0}" != "1" ]]; then
  open "http://127.0.0.1:${H3_COMFY_PORT:-8188}"
fi
printf 'ComfyUI 与 vpipe worker 已由 launchd 保活。\n'
