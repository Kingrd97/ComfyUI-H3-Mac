#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$ROOT/runtime/.venv/bin/python"
if [[ "${H3_FOREGROUND:-0}" == "1" ]]; then
  exec "$ROOT/scripts/start.sh" "$@"
fi
"$PYTHON" "$ROOT/scripts/launchd.py" install
if [[ "${H3_NO_OPEN:-0}" != "1" ]]; then
  PORT="${H3_COMFY_PORT:-8188}"
  # launchd.py install has already waited for both our custom H3 node and the
  # durable vpipe worker.  Do not replace that exact check with a generic port
  # probe: another ComfyUI instance may be listening on 8188 without our node.
  "$PYTHON" "$ROOT/scripts/launchd.py" status >/dev/null
  open "http://127.0.0.1:$PORT"
fi
printf 'ComfyUI 与 vpipe worker 已由 launchd 保活。\n'
