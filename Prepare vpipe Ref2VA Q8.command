#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
mode="${1:-low}"
case "$mode" in
  low|max|check) ;;
  *) printf '用法：./Prepare\\ vpipe\\ Ref2VA\\ Q8.command [check|low|max]\n' >&2; exit 2 ;;
esac

if [[ "$mode" == "check" ]]; then
  exec "$ROOT/scripts/prepare_vpipe_ref2va_q8.sh" check
fi

printf '%s\n' \
  '' \
  '这会下载 66.28GB Ref2VA BF16 DiT，转换为约 33GiB Vpipe Q8，成功后自动删除 BF16。' \
  '现有 FL2VA Q8 的编码器、VAE 与 tokenizer 会通过硬链接复用，不会重复量化。' \
  '下载可 Ctrl-C 后续传；量化阶段中断后需要重新量化。' \
  '模型受 MiniMax H3 Community License 约束：https://huggingface.co/MiniMaxAI/MiniMax-H3'
read -r -p '确认你已阅读并同意该模型许可证？输入 AGREE 继续: ' consent
[[ "$consent" == "AGREE" ]] || { printf '已取消。\n'; exit 0; }

export H3_MODEL_LICENSE_ACCEPTED=1
exec "$ROOT/scripts/prepare_vpipe_ref2va_q8.sh" "$mode"
