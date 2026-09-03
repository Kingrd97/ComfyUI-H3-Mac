#!/bin/bash
set -euo pipefail

if [[ "${VPIPE_REF_PREP_CAFFEINATED:-0}" != "1" && -x /usr/bin/caffeinate ]]; then
  exec /usr/bin/caffeinate -s /usr/bin/env VPIPE_REF_PREP_CAFFEINATED=1 \
    /bin/bash "$0" "$@"
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
PYTHON="$PROJECT_ROOT/runtime/.venv/bin/python"
source "$PROJECT_ROOT/versions.env"

mode="${1:-low}"
case "$mode" in
  low|max|check) ;;
  *) printf '用法：Prepare vpipe Ref2VA Q8.command [check|low|max]\n' >&2; exit 2 ;;
esac

if [[ "$mode" != "check" && "${H3_MODEL_LICENSE_ACCEPTED:-0}" != "1" ]]; then
  printf '请通过 Prepare vpipe Ref2VA Q8.command 确认 MiniMax H3 Community License。\n' >&2
  exit 1
fi

[[ -x "$PYTHON" ]] || { printf '请先运行 Install.command。\n' >&2; exit 1; }
vpipe_bin="$(
  "$PYTHON" -c \
    'import sys; from pathlib import Path; from h3_bridge.vpipe import load_vpipe_config; print(load_vpipe_config(Path(sys.argv[1])).binary)' \
    "$PROJECT_ROOT"
)"
work_dir="$(
  "$PYTHON" -c \
    'import sys; from pathlib import Path; from h3_bridge.vpipe import load_vpipe_config; print(load_vpipe_config(Path(sys.argv[1])).work_dir)' \
    "$PROJECT_ROOT"
)"
[[ -x "$vpipe_bin" ]] || { printf '找不到已验证的 vpipe：%s\n' "$vpipe_bin" >&2; exit 1; }
version_output="$("$vpipe_bin" --version 2>&1 || true)"
[[ "$version_output" == *"${VPIPE_REF:0:7}"* ]] || {
  printf 'vpipe 版本不匹配（需要 %s，当前：%s）。请重跑 Install.command。\n' \
    "${VPIPE_REF:0:7}" "${version_output:-无输出}" >&2
  exit 1
}
command -v aria2c >/dev/null 2>&1 || {
  printf '缺少 aria2c，请重跑 Install.command。\n' >&2
  exit 1
}
taskpolicy_bin="$(command -v taskpolicy 2>/dev/null || true)"

connections="${VPIPE_DOWNLOAD_CONNECTIONS:-8}"
case "$connections" in
  ''|*[!0-9]*) printf 'VPIPE_DOWNLOAD_CONNECTIONS 必须是 1–16 的整数。\n' >&2; exit 2 ;;
esac
(( connections >= 1 && connections <= 16 )) || {
  printf 'VPIPE_DOWNLOAD_CONNECTIONS 必须是 1–16 的整数。\n' >&2
  exit 2
}

stage_root="$work_dir/models/.staging/minimax-h3-ref2va-q8"
dit_source_root="$stage_root/dit-source"
dit_quant_root="$stage_root/dit-quantized"
base_root="$work_dir/models/local/MiniMax-H3-FL2VA-8bit"
final_root="$work_dir/models/local/MiniMax-H3-Ref2VA-8bit"
model_key="local/MiniMax-H3-Ref2VA-8bit"
lock_dir="$work_dir/.comfyui-h3-ref2va-q8.lock"
dit_name="minimax_h3_ref2va_bf16.safetensors"
dit_path="$dit_source_root/diffusion_models/$dit_name"
dit_url="https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/$VPIPE_H3_REPACK_REF/diffusion_models/$dit_name?download=true"
dit_sha="e32c54c1a7b4f5f397f195cea267ccb18806303bb665678c4bee60953bdf3026"

case "$stage_root" in
  "$work_dir"/models/.staging/minimax-h3-ref2va-q8) ;;
  *) printf '拒绝使用意外的 staging 路径：%s\n' "$stage_root" >&2; exit 1 ;;
esac

verify_component() {
  component_root="$1"
  component_kind="$2"
  "$PYTHON" "$PROJECT_ROOT/scripts/verify_vpipe_assets.py" \
    --project-root "$PROJECT_ROOT" --component-root "$component_root" \
    --component-kind "$component_kind" --quiet >/dev/null 2>&1
}

verify_partition() {
  "$PYTHON" - "$1" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads((Path(sys.argv[1]) / "config.json").read_text(encoding="utf-8"))
if config.get("_minimax_h3_partition") != "ref2va":
    raise SystemExit(1)
quant = config.get("quantization")
if not isinstance(quant, dict) or quant.get("bits") != 8 or quant.get("group_size") != 64:
    raise SystemExit(1)
PY
}

shared_assets_ready() {
  verify_component "$base_root/text_encoders" encoder && \
    [[ -s "$base_root/vae/minimax_h3_video_vae_fp16.safetensors" ]] && \
    [[ -s "$base_root/vae/minimax_h3_audio_vae_fp32.safetensors" ]] && \
    [[ -s "$base_root/tokenizer/tokenizer.json" ]] && \
    [[ -s "$base_root/tokenizer/tokenizer_config.json" ]]
}

final_ready() {
  verify_component "$final_root/diffusion_models" dit && \
    verify_partition "$final_root/diffusion_models" && \
    verify_component "$final_root/text_encoders" encoder && \
    [[ -s "$final_root/vae/minimax_h3_video_vae_fp16.safetensors" ]] && \
    [[ -s "$final_root/vae/minimax_h3_audio_vae_fp32.safetensors" ]] && \
    [[ -s "$final_root/tokenizer/tokenizer.json" ]]
}

run_vpipe() {
  if [[ "$mode" == "low" ]]; then
    low_command=("$vpipe_bin" --memory-cap-mb 8192 --wired-pool-mb 4096 "$@")
    if [[ -n "$taskpolicy_bin" ]]; then
      low_command=("$taskpolicy_bin" -b "${low_command[@]}")
    fi
    /usr/bin/caffeinate -s "${low_command[@]}"
  else
    /usr/bin/caffeinate -s "$vpipe_bin" "$@"
  fi
}

register_model() {
  cd "$work_dir"
  run_vpipe --launch-stage model-register \
    --stage-cfg "model_dir=$final_root" --stage-cfg "key=$model_key" \
    --stage-cfg model_type=minimax-h3-ref2va \
    --stage-cfg overwrite_existing=true
}

available_kib="$(df -k "$work_dir" | awk 'NR == 2 { print $4 }')"
available_gib=$(( available_kib / 1024 / 1024 ))
printf 'vpipe 工作目录：%s\n可用磁盘：约 %s GiB\n' "$work_dir" "$available_gib"

if final_ready; then
  printf 'Ref2VA Q8 已完整，无需再次下载或量化：%s\n' "$final_root"
  if [[ "$mode" == "check" ]]; then exit 0; fi
  register_model
  printf '已刷新 vpipe 模型登记。\n'
  exit 0
elif [[ "$mode" == "check" ]]; then
  shared_assets_ready || {
    printf '预检失败：现有 FL2VA Q8 编码器/VAE/tokenizer 不完整。\n' >&2
    exit 1
  }
  resume_credit_kib=0
  [[ -d "$dit_source_root" ]] && resume_credit_kib="$(du -sk "$dit_source_root" | awk '{print $1}')"
  (( available_kib + resume_credit_kib >= 110000000 )) || {
    printf '预检失败：首次准备需要约 105 GiB 总余量（可用空间 + 已续传 Ref2VA DiT）。\n' >&2
    exit 1
  }
  printf '只读预检通过。下载可续传；量化阶段中断后需要重新量化。\n'
  exit 0
fi

shared_assets_ready || {
  printf '缺少完整的 FL2VA Q8 共享编码器/VAE/tokenizer；请先完成 Prepare vpipe Q8.command。\n' >&2
  exit 1
}

/bin/mkdir -p "$work_dir/models/local"
if ! mkdir "$lock_dir" 2>/dev/null; then
  old_pid="$(sed -n '1p' "$lock_dir/pid" 2>/dev/null || true)"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    printf '已有 Ref2VA Q8 准备任务在运行（PID %s）。\n' "$old_pid" >&2
    exit 1
  fi
  if [[ ! "$old_pid" =~ ^[0-9]+$ ]]; then
    lock_mtime="$(stat -f %m "$lock_dir" 2>/dev/null || printf '0')"
    lock_age=$(( $(date +%s) - lock_mtime ))
    if (( lock_age < 30 )); then
      printf '另一个 Ref2VA Q8 准备任务正在初始化锁；请稍后重试。\n' >&2
      exit 1
    fi
  fi
  /bin/rm -rf -- "$lock_dir"
  if ! mkdir "$lock_dir" 2>/dev/null; then
    printf '另一个 Ref2VA Q8 准备任务已抢先取得锁；本次退出。\n' >&2
    exit 1
  fi
fi
printf '%s\n' "$$" > "$lock_dir/pid"
cleanup_lock() { /bin/rm -rf -- "$lock_dir"; }
trap cleanup_lock EXIT INT TERM

remove_guarded_tree() {
  target_path="$1"
  case "$target_path" in
    "$stage_root"/*|"$final_root") ;;
    *) printf '拒绝清理意外路径：%s\n' "$target_path" >&2; exit 1 ;;
  esac
  [[ ! -e "$target_path" ]] || /bin/rm -rf -- "$target_path"
}

if [[ -d "$dit_quant_root" ]] && \
   ! verify_component "$dit_quant_root/diffusion_models" dit; then
  printf '清理未完成的 Ref2VA Q8 量化输出。\n'
  remove_guarded_tree "$dit_quant_root"
fi
if [[ -d "$final_root" ]] && ! final_ready; then
  printf '清理未完成的 Ref2VA 最终目录。\n'
  remove_guarded_tree "$final_root"
fi

resume_credit_kib=0
[[ -d "$dit_source_root" ]] && resume_credit_kib="$(du -sk "$dit_source_root" | awk '{print $1}')"
available_kib="$(df -k "$work_dir" | awk 'NR == 2 { print $4 }')"
if (( available_kib + resume_credit_kib < 110000000 )); then
  printf '空间不足：需要约 105 GiB 总余量（可用空间 + 已续传 Ref2VA DiT）。\n' >&2
  exit 1
fi

download_checked() {
  /bin/mkdir -p "$(dirname "$dit_path")"
  printf '下载/续传 Ref2VA BF16 DiT（66.28 GB，%s 连接）……\n' "$connections"
  aria_args=(
    --continue=true --auto-file-renaming=false --allow-overwrite=true
    --file-allocation=none --max-connection-per-server="$connections"
    --split="$connections" --min-split-size=8M --connect-timeout=30
    --timeout=30 --retry-wait=3 --max-tries=0 --summary-interval=10
    --console-log-level=notice --dir="$(dirname "$dit_path")"
    --out="$dit_name" --checksum="sha-256=$dit_sha"
  )
  if [[ -n "${HF_TOKEN:-}" ]]; then
    aria_args+=(--header="Authorization: Bearer ${HF_TOKEN}")
  fi
  aria2c "${aria_args[@]}" "$dit_url"
}

if ! verify_component "$dit_quant_root/diffusion_models" dit; then
  [[ -f "$dit_path" ]] || download_checked
  actual_sha="$(/usr/bin/shasum -a 256 "$dit_path" | awk '{print $1}')"
  [[ "$actual_sha" == "$dit_sha" ]] || {
    printf 'Ref2VA BF16 DiT SHA-256 不匹配；保留文件，不开始量化。\n' >&2
    exit 1
  }
  printf '将 Ref2VA DiT 转换为 Q8（group 64；低功耗模式只改变调度，不改变画质）……\n'
  cd "$work_dir"
  run_vpipe --launch-stage model-quantize \
    --stage-cfg "src_model=$dit_source_root" \
    --stage-cfg "output_name=$dit_quant_root" \
    --stage-cfg target=dit --stage-cfg bits=8 --stage-cfg group_size=64 \
    --stage-cfg quant_modulation=true --stage-cfg skip_existing=false
fi

verify_component "$dit_quant_root/diffusion_models" dit && \
  verify_partition "$dit_quant_root/diffusion_models" || {
    printf 'Ref2VA Q8 输出不完整或分区标记错误；保留 BF16 源文件以便重试。\n' >&2
    exit 1
  }

/bin/mkdir -p "$final_root"
/bin/cp -al "$base_root/." "$final_root/"
/bin/rm -rf -- "$final_root/diffusion_models"
/bin/mv "$dit_quant_root/diffusion_models" "$final_root/diffusion_models"
rmdir "$dit_quant_root" 2>/dev/null || true

final_ready || {
  printf '最终 Ref2VA Q8 结构校验失败；保留 BF16 源文件。\n' >&2
  exit 1
}

register_model

printf '最终 Ref2VA Q8 已校验并登记；删除一次性 BF16 源文件。\n'
/bin/rm -f -- "$dit_path" "$dit_path.aria2"
rmdir "$dit_source_root/diffusion_models" "$dit_source_root" "$stage_root" \
  2>/dev/null || true

printf '\nRef2VA Q8 准备完成：%s\n' "$final_root"
printf '共享编码器和 VAE 使用硬链接，新增物理占用主要是约 33 GiB Ref2VA DiT。\n'
df -h "$work_dir"
