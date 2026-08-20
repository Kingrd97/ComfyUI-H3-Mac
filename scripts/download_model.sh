#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_ROOT/runtime/model-tools-venv"
MODEL_ROOT="$PROJECT_ROOT/runtime/models/MiniMax-H3"
MODEL_CACHE="$PROJECT_ROOT/runtime/models/.cache/huggingface"
MODEL_MANIFEST="$PROJECT_ROOT/runtime/models/MiniMax-H3.manifest.json"
LICENSE_URL="https://huggingface.co/MiniMaxAI/MiniMax-H3"
source "$PROJECT_ROOT/versions.env"

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
  printf '%s\n' \
    "" \
    "Ref2VA 需要 FL2VA 基础文件。两个目录逻辑总量约 268 GiB；" \
    "本下载器使用内容寻址缓存，相同权重只保存一次，首次约 196 GiB，建议至少预留 220 GiB。"
else
  printf '\nFL2VA 约 134 GiB，建议至少预留 150 GiB。下载前会精确检查当前锁定版本和可用空间。\n'
fi
printf '模型受 MiniMax H3 Community License 约束：%s\n' "$LICENSE_URL"
read -r -p "确认你已阅读并同意该模型许可证？输入 AGREE 继续: " consent
[[ "$consent" == "AGREE" ]] || { printf '已取消。\n'; exit 0; }

installed_hub_version="$("$VENV/bin/python" -c 'import huggingface_hub; print(huggingface_hub.__version__)' 2>/dev/null || true)"
if [[ "$installed_hub_version" != "$HUGGINGFACE_HUB_VERSION" ]]; then
  printf 'huggingface_hub 版本不匹配（需要 %s，当前 %s），请重新运行 Install.command。\n' \
    "$HUGGINGFACE_HUB_VERSION" "${installed_hub_version:-未安装}" >&2
  exit 1
fi
mkdir -p "$MODEL_CACHE"
export HF_XET_CACHE="$PROJECT_ROOT/runtime/models/.cache/xet"

printf '\n开始下载 %s。重跑会复用已完成的内容寻址 blob；未完成文件由下载客户端尽力续传。\n' "$TASK"
"$VENV/bin/python" "$PROJECT_ROOT/scripts/model_snapshot.py" download \
  --task "$TASK" \
  --revision "$H3_MODEL_REF" \
  --model-root "$MODEL_ROOT" \
  --cache-dir "$MODEL_CACHE" \
  --manifest "$MODEL_MANIFEST"

printf '\n模型下载并校验完成：%s（%s，版本 %s）\n' "$MODEL_ROOT" "$TASK" "$H3_MODEL_REF"
