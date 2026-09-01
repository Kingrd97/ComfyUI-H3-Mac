#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
mode="${1:-low}"
if [[ "$mode" == "check" ]]; then
  exec "$ROOT/scripts/prepare_vpipe_q8.sh" check
fi
export H3_MODEL_PREP_MODE="$mode"
exec "$ROOT/scripts/download_model.sh" VPipeQ8
