#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMFY="$PROJECT_ROOT/runtime/ComfyUI"
PYTHON="$PROJECT_ROOT/runtime/.venv/bin/python"
PORT="${H3_COMFY_PORT:-8188}"
DEVICE="${H3_COMFY_DEVICE:-cpu}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

if [[ ! -x "$PYTHON" || ! -f "$COMFY/main.py" ]]; then
  printf '尚未安装，请先双击 Install.command。\n' >&2
  exit 1
fi

"$PYTHON" "$PROJECT_ROOT/scripts/migrate_config.py" \
  "$PROJECT_ROOT/config.json" \
  "$PROJECT_ROOT/config.example.json"

# A hard ComfyUI crash can leave its separately-sessioned H3 child running or
# SIGSTOP'ed. Only terminate jobs with complete birth fingerprints whose exact
# controller is provably gone; legacy/ambiguous records are left untouched.
"$PYTHON" "$PROJECT_ROOT/scripts/h3_control.py" cleanup-orphans

if [[ "${H3_NO_OPEN:-0}" != "1" ]]; then
  (sleep 2; open "http://127.0.0.1:$PORT") >/dev/null 2>&1 &
fi
printf 'Language / 界面语言: Comfy > Locale > Language > English / 中文\n'
printf 'H3 control / 任务控制: double-click "H3 Control.command" (auto is recommended on 48GB M5 Pro)\n'
cd "$COMFY"
case "$DEVICE" in
  cpu)
    # ComfyUI is the control plane; h3.c remains a separate Metal process.
    # Keeping torch on CPU avoids unnecessary unified-memory use and CUDA
    # fallback failures on hosts where PyTorch cannot detect MPS.
    exec "$PYTHON" main.py --cpu --listen 127.0.0.1 --port "$PORT" "$@"
    ;;
  auto)
    exec "$PYTHON" main.py --listen 127.0.0.1 --port "$PORT" "$@"
    ;;
  *)
    printf 'H3_COMFY_DEVICE must be cpu or auto (got: %s).\n' "$DEVICE" >&2
    exit 2
    ;;
esac
