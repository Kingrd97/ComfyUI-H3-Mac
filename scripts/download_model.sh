#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_ROOT/runtime/.venv"
MODEL_ROOT="$PROJECT_ROOT/runtime/models/MiniMax-H3"
LICENSE_URL="https://huggingface.co/MiniMaxAI/MiniMax-H3"

if [[ ! -x "$VENV/bin/python" ]]; then
  printf '尚未安装，请先双击 Install.command。\n' >&2
  exit 1
fi

TASK="${1:-}"
if [[ -z "$TASK" ]]; then
  printf '%s\n' \
    "选择模型任务：" \
    "  1) Ref2VA（推荐：图片/视频/音频参考生成视频）" \
    "  2) FL2VA（首尾帧生成视频）"
  read -r -p "输入 1 或 2: " choice
  case "$choice" in
    1) TASK="Ref2VA" ;;
    2) TASK="FL2VA" ;;
    *) printf '无效选择。\n' >&2; exit 1 ;;
  esac
fi
[[ "$TASK" == "Ref2VA" || "$TASK" == "FL2VA" ]] || { printf '任务只能是 Ref2VA 或 FL2VA。\n' >&2; exit 1; }

if [[ "$TASK" == "Ref2VA" ]]; then
  printf '\nRef2VA 套件会同时下载必需的 FL2VA 基础文件，总量约 144 GB；建议至少预留 170 GB。\n'
  includes=("FL2VA/*" "Ref2VA/*")
else
  printf '\nFL2VA 基础模型体积很大，请先确认磁盘有充足空间。\n'
  includes=("FL2VA/*")
fi
printf '模型受 MiniMax H3 Community License 约束：%s\n' "$LICENSE_URL"
read -r -p "确认你已阅读并同意该模型许可证？输入 AGREE 继续: " consent
[[ "$consent" == "AGREE" ]] || { printf '已取消。\n'; exit 0; }

"$VENV/bin/python" -m pip install --upgrade "huggingface_hub[cli]"
mkdir -p "$MODEL_ROOT"

printf '\n开始下载 %s。中断后重新运行会自动断点续传。\n' "$TASK"
"$VENV/bin/hf" download MiniMaxAI/MiniMax-H3 \
  --include "model_index.json" "modular_model_index.json" "${includes[@]}" \
  --local-dir "$MODEL_ROOT"

printf '\n模型下载完成：%s（%s）\n' "$MODEL_ROOT" "$TASK"
