#!/bin/bash
set -euo pipefail

if [[ "${VPIPE_PREP_CAFFEINATED:-0}" != "1" && -x /usr/bin/caffeinate ]]; then
  exec /usr/bin/caffeinate -s /usr/bin/env VPIPE_PREP_CAFFEINATED=1 \
    /bin/bash "$0" "$@"
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
PYTHON="$PROJECT_ROOT/runtime/.venv/bin/python"
source "$PROJECT_ROOT/versions.env"

mode="${1:-low}"
case "$mode" in
  low|max|check) ;;
  *) printf '用法：Prepare vpipe Q8.command [check|low|max]\n' >&2; exit 2 ;;
esac

if [[ "$mode" != "check" && "${H3_MODEL_LICENSE_ACCEPTED:-0}" != "1" ]]; then
  printf '请通过 Download Model.command（选 1）或 Prepare vpipe Q8.command 先确认 MiniMax H3 Community License。\n' >&2
  exit 1
fi

[[ -x "$PYTHON" ]] || { printf '请先运行 Install.command。\n' >&2; exit 1; }
macos_major="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"
[[ "$macos_major" =~ ^[0-9]+$ && "$macos_major" -ge 26 ]] || {
  printf 'vpipe v%s 需要 macOS 26 或更高版本。\n' "$VPIPE_VERSION" >&2
  exit 1
}

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

stage_root="$work_dir/models/.staging/minimax-h3-fl2va-q8"
dit_source_root="$stage_root/dit-source"
dit_quant_root="$stage_root/dit-quantized"
full_source_root="$stage_root/full-source"
final_root="$work_dir/models/local/MiniMax-H3-FL2VA-8bit"
model_key="local/MiniMax-H3-FL2VA-8bit"
manifest="$work_dir/models/.comfyui-h3-q8-manifest.json"
lock_dir="$work_dir/.comfyui-h3-q8.lock"
comfy_ref="$VPIPE_H3_REPACK_REF"
minimax_ref="$H3_MODEL_REF"
dit_name="minimax_h3_fl2va_bf16.safetensors"
dit_path="$dit_source_root/diffusion_models/$dit_name"
dit_url="https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/$comfy_ref/diffusion_models/$dit_name?download=true"
dit_sha="907d4add438438ec1544f5240c3b38532ed934fe6be75677a6bbda2a6fdd6182"

case "$stage_root" in
  "$work_dir"/models/.staging/minimax-h3-fl2va-q8) ;;
  *) printf '拒绝使用意外的 staging 路径：%s\n' "$stage_root" >&2; exit 1 ;;
esac

# `check` is a strict read-only preflight. It must never create the worker
# lock, move a completed stage, or remove a partial/invalid quantizer output.
if [[ "$mode" == "check" ]]; then
  available_kib="$(df -k "$PROJECT_ROOT" | awk 'NR == 2 { print $4 }')"
  available_gib=$(( available_kib / 1024 / 1024 ))
  printf 'vpipe 工作目录：%s\n可用磁盘：约 %s GiB\n' "$work_dir" "$available_gib"
  if "$PYTHON" "$PROJECT_ROOT/scripts/verify_vpipe_assets.py" \
      --project-root "$PROJECT_ROOT" --quiet >/dev/null 2>&1; then
    printf '预检通过：vpipe Q8 与两套 LoRA 已完整，无需再次准备。\n'
    exit 0
  fi
  if "$PYTHON" "$PROJECT_ROOT/scripts/verify_vpipe_assets.py" \
      --project-root "$PROJECT_ROOT" --allow-schema-migration \
      --allow-engine-migration --allow-missing-hashes \
      --quiet >/dev/null 2>&1; then
    printf '预检通过：旧版 SHA 清单可安全原地升级；重跑 low/max 不会重新下载。\n'
    exit 0
  fi
  if "$PYTHON" "$PROJECT_ROOT/scripts/verify_vpipe_assets.py" \
      --project-root "$PROJECT_ROOT" --files-only --allow-schema-migration \
      --allow-engine-migration --allow-missing-hashes \
      --quiet >/dev/null 2>&1; then
    printf '预检通过：权重文件完整，但 vpipe 注册表或旧清单需要刷新；重跑 low/max 不会重新下载。\n'
    exit 0
  fi
  if "$PYTHON" "$PROJECT_ROOT/scripts/verify_vpipe_assets.py" \
      --project-root "$PROJECT_ROOT" \
      --component-root "$full_source_root/diffusion_models" \
      --component-kind dit --quiet >/dev/null 2>&1; then
    stage2_credit_kib=0
    for reusable_dir in text_encoders vae tokenizer; do
      if [[ -d "$full_source_root/$reusable_dir" ]]; then
        reusable_kib="$(du -sk "$full_source_root/$reusable_dir" | awk '{print $1}')"
        stage2_credit_kib=$(( stage2_credit_kib + reusable_kib ))
      fi
    done
    (( available_kib + stage2_credit_kib >= 88000000 )) || {
      printf '预检失败：提示编码器阶段需要约 84 GiB 总余量（可用空间 + 已续传的编码器/VAE/tokenizer）。\n' >&2
      exit 1
    }
  else
    dit_credit_kib=0
    if [[ -d "$dit_source_root" ]]; then
      dit_credit_kib="$(du -sk "$dit_source_root" | awk '{print $1}')"
    fi
    (( available_kib + dit_credit_kib >= 125000000 )) || {
      printf '预检失败：首次准备需要约 120 GiB 总余量（可用空间 + 已续传 DiT）。\n' >&2
      exit 1
    }
  fi
  printf '只读预检通过；未创建、移动或删除任何模型文件。\n'
  printf '后台友好准备：./Prepare\\ vpipe\\ Q8.command low\n'
  printf '最高性能准备：./Prepare\\ vpipe\\ Q8.command max\n'
  exit 0
fi

mkdir -p "$work_dir" "$work_dir/models/local"
if ! mkdir "$lock_dir" 2>/dev/null; then
  old_pid="$(sed -n '1p' "$lock_dir/pid" 2>/dev/null || true)"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    printf '已有 Q8 准备任务在运行（PID %s）。\n' "$old_pid" >&2
    exit 1
  fi
  if [[ ! "$old_pid" =~ ^[0-9]+$ ]]; then
    lock_mtime="$(stat -f %m "$lock_dir" 2>/dev/null || printf '0')"
    lock_age=$(( $(date +%s) - lock_mtime ))
    if (( lock_age < 30 )); then
      printf '另一个 Q8 准备任务正在初始化锁；请稍后重试。\n' >&2
      exit 1
    fi
  fi
  /bin/rm -rf -- "$lock_dir"
  if ! mkdir "$lock_dir" 2>/dev/null; then
    printf '另一个 Q8 准备任务已抢先取得锁；本次退出。\n' >&2
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
  if [[ -e "$target_path" ]]; then
    printf '清理受保护的临时目录：%s\n' "$target_path"
    /bin/rm -rf -- "$target_path"
  fi
}

verify_component() {
  component_root="$1"
  component_kind="$2"
  "$PYTHON" "$PROJECT_ROOT/scripts/verify_vpipe_assets.py" \
    --project-root "$PROJECT_ROOT" --component-root "$component_root" \
    --component-kind "$component_kind" --quiet >/dev/null 2>&1
}

core_ready=0
manifest_state="$("$PYTHON" -c \
  'import json,sys; from pathlib import Path
p=Path(sys.argv[1])
if not p.exists(): print("absent")
else:
  try: value=json.loads(p.read_text(encoding="utf-8"))
  except Exception: print("invalid")
  else: print("hashed" if isinstance(value,dict) and isinstance(value.get("sha256"),dict) else "unhashed")' \
  "$manifest")"
if [[ "$manifest_state" == "invalid" ]]; then
  printf 'Q8 清单无法解析；为保护现有权重和 staging，本次未删除任何模型文件：%s\n' \
    "$manifest" >&2
  exit 1
fi
if [[ "$manifest_state" == "hashed" ]]; then
  printf '校验现有 Q8 核心权重 SHA-256（约顺序读取 65 GiB）……\n'
  if "$PYTHON" "$PROJECT_ROOT/scripts/verify_vpipe_assets.py" \
      --project-root "$PROJECT_ROOT" --model-only --verify-model-hashes \
      --deep --allow-schema-migration --allow-engine-migration --quiet; then
    core_ready=1
  else
    printf '现有 Q8 核心权重未通过可信 SHA；为保护可恢复 staging，本次未删除任何模型文件。\n' >&2
    exit 1
  fi
elif "$PYTHON" "$PROJECT_ROOT/scripts/verify_vpipe_assets.py" \
    --project-root "$PROJECT_ROOT" --model-only --quiet >/dev/null 2>&1; then
  core_ready=1
fi

# Quantizer output is not resumable. Reuse only a component whose config,
# Q8/group metadata, index, every shard and minimum total size all verify.
# Cleaning invalid output before the disk gate prevents an interrupted output
# from consuming the very space needed to restart the same stage.
if (( core_ready == 1 )); then
  printf '现有 Q8 核心可复用；暂时保留 staging，待整套资产验证完成后再清理。\n'
else
  if [[ -d "$full_source_root" ]] && \
      ! verify_component "$full_source_root/diffusion_models" dit; then
    remove_guarded_tree "$full_source_root"
  fi
  if [[ -d "$dit_quant_root" ]]; then
    if verify_component "$dit_quant_root/diffusion_models" dit; then
      if [[ ! -d "$full_source_root" ]]; then
        mv "$dit_quant_root" "$full_source_root"
      else
        remove_guarded_tree "$dit_quant_root"
      fi
    else
      remove_guarded_tree "$dit_quant_root"
    fi
  fi
  if [[ -d "$final_root" ]]; then
    remove_guarded_tree "$final_root"
  fi
  if verify_component "$full_source_root/diffusion_models" dit && \
      [[ -f "$dit_path" ]]; then
    printf 'DiT Q8 已校验；删除已完成阶段的 66.28GB BF16 DiT。\n'
    /bin/rm -f -- "$dit_path" "$dit_path.aria2"
    rmdir "$dit_source_root/diffusion_models" "$dit_source_root" 2>/dev/null || true
  fi
fi

available_kib="$(df -k "$work_dir" | awk 'NR == 2 { print $4 }')"
available_gib=$(( available_kib / 1024 / 1024 ))
printf 'vpipe 工作目录：%s\n可用磁盘：约 %s GiB\n' "$work_dir" "$available_gib"
if (( core_ready == 0 )); then
  if [[ -f "$full_source_root/diffusion_models/config.json" ]]; then
    # Stage 1 is already durable. Credit every reusable Stage 2 download, but
    # not the durable Q8 DiT in this same tree. This keeps a Ctrl-C after the
    # encoder/VAE/tokenizer downloads from being rejected on resume merely
    # because those already-validated bytes now occupy the expected free space.
    stage2_credit_kib=0
    for reusable_dir in text_encoders vae tokenizer; do
      if [[ -d "$full_source_root/$reusable_dir" ]]; then
        reusable_kib="$(du -sk "$full_source_root/$reusable_dir" | awk '{print $1}')"
        stage2_credit_kib=$(( stage2_credit_kib + reusable_kib ))
      fi
    done
    if (( available_kib + stage2_credit_kib < 88000000 )); then
      printf 'DiT Q8 已完成；提示编码器阶段还需要约 84 GiB 总余量（可用空间 + 已续传的编码器/VAE/tokenizer）。\n' >&2
      exit 1
    fi
  else
    dit_credit_kib=0
    if [[ -d "$dit_source_root" ]]; then
      dit_credit_kib="$(du -sk "$dit_source_root" | awk '{print $1}')"
    fi
    if (( available_kib + dit_credit_kib < 125000000 )); then
      printf '紧凑 Q8 流程首次准备至少需要约 120 GiB 总余量（可用空间 + 已续传 DiT）；当前不足。\n' >&2
      exit 1
    fi
  fi
fi

run_vpipe() {
  if [[ "$mode" == "low" ]]; then
    low_command=(
      "$vpipe_bin" --memory-cap-mb 8192 --wired-pool-mb 4096 "$@"
    )
    if [[ -n "$taskpolicy_bin" ]]; then
      low_command=("$taskpolicy_bin" -b "${low_command[@]}")
    fi
    /usr/bin/caffeinate -s "${low_command[@]}"
  else
    /usr/bin/caffeinate -s "$vpipe_bin" "$@"
  fi
}

download_checked() {
  url="$1"
  target_dir="$2"
  target_name="$3"
  expected_sha="$4"
  mkdir -p "$target_dir"
  printf '下载/续传：%s\n' "$target_name"
  aria_args=(
    --continue=true --auto-file-renaming=false --allow-overwrite=true
    --file-allocation=none --max-connection-per-server="$connections"
    --split="$connections" --min-split-size=8M --connect-timeout=30
    --timeout=30 --retry-wait=3 --max-tries=0 --summary-interval=10
    --console-log-level=notice --dir="$target_dir" --out="$target_name"
    --checksum="sha-256=$expected_sha"
  )
  if [[ -n "${HF_TOKEN:-}" ]]; then
    aria_args+=(--header="Authorization: Bearer ${HF_TOKEN}")
  fi
  aria2c "${aria_args[@]}" "$url"
}

if ! verify_component "$full_source_root/diffusion_models" dit && \
   ! verify_component "$final_root/diffusion_models" dit; then
  download_checked "$dit_url" "$dit_source_root/diffusion_models" "$dit_name" "$dit_sha"
  if [[ -d "$dit_quant_root" ]] && \
      ! verify_component "$dit_quant_root/diffusion_models" dit; then
    remove_guarded_tree "$dit_quant_root"
  fi
  if ! verify_component "$dit_quant_root/diffusion_models" dit; then
    printf '阶段 1/2：将 FL2VA DiT 转换为 Q8（group 64）\n'
    cd "$work_dir"
    run_vpipe --launch-stage model-quantize \
      --stage-cfg "src_model=$dit_source_root" \
      --stage-cfg "output_name=$dit_quant_root" \
      --stage-cfg target=dit --stage-cfg bits=8 --stage-cfg group_size=64 \
      --stage-cfg quant_modulation=true
  fi
  verify_component "$dit_quant_root/diffusion_models" dit || {
    printf 'DiT Q8 输出不完整；保留 BF16 源文件以便重试。\n' >&2
    exit 1
  }
  mv "$dit_quant_root" "$full_source_root"
  printf 'DiT Q8 已完成；删除已校验的 66.28GB BF16 DiT。\n'
  /bin/rm -f -- "$dit_path" "$dit_path.aria2"
  rmdir "$dit_source_root/diffusion_models" "$dit_source_root" 2>/dev/null || true
fi

if verify_component "$full_source_root/diffusion_models" dit && [[ -f "$dit_path" ]]; then
  /bin/rm -f -- "$dit_path" "$dit_path.aria2"
fi

if [[ ! -f "$final_root/text_encoders/config.json" ]]; then
  [[ -f "$full_source_root/diffusion_models/config.json" ]] || {
    printf '缺少已量化的 DiT，无法继续提示编码器阶段。\n' >&2
    exit 1
  }
  enc_name="qwen3vl_32b_minimax_h3_bf16.safetensors"
  download_checked \
    "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/$comfy_ref/text_encoders/$enc_name?download=true" \
    "$full_source_root/text_encoders" "$enc_name" \
    "600d567f6a9629c8574e8e7041b199bdd9c59a986afa7906910a81919610607d"
  download_checked \
    "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/$comfy_ref/vae/minimax_h3_video_vae_fp16.safetensors?download=true" \
    "$full_source_root/vae" "minimax_h3_video_vae_fp16.safetensors" \
    "7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522"
  download_checked \
    "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/$comfy_ref/vae/minimax_h3_audio_vae_fp32.safetensors?download=true" \
    "$full_source_root/vae" "minimax_h3_audio_vae_fp32.safetensors" \
    "8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48"
  download_checked \
    "https://huggingface.co/MiniMaxAI/MiniMax-H3/resolve/$minimax_ref/FL2VA/tokenizer/tokenizer.json?download=true" \
    "$full_source_root/tokenizer" "tokenizer.json" \
    "a5d85b6dcc535e6b93115a9ef287e6132fdbf30270da6218194ba742261173c7"
  download_checked \
    "https://huggingface.co/MiniMaxAI/MiniMax-H3/resolve/$minimax_ref/FL2VA/tokenizer/tokenizer_config.json?download=true" \
    "$full_source_root/tokenizer" "tokenizer_config.json" \
    "a07e942ac874baa13758de8d1fbdb186683cc03416b5589e1b6671c6b3057c68"
  if [[ -d "$final_root" && ! -f "$final_root/text_encoders/config.json" ]]; then
    remove_guarded_tree "$final_root"
  fi
  printf '阶段 2/2：将 Qwen3-VL-32B 提示编码器转换为 Q8（group 64）\n'
  cd "$work_dir"
  run_vpipe --launch-stage model-quantize \
    --stage-cfg "src_model=$full_source_root" \
    --stage-cfg "output_name=$final_root" \
    --stage-cfg target=text_encoder --stage-cfg bits=8 --stage-cfg group_size=64
fi

"$PYTHON" "$PROJECT_ROOT/scripts/verify_vpipe_assets.py" \
  --project-root "$PROJECT_ROOT" --model-only
if [[ -d "$full_source_root" ]]; then
  printf '最终 Q8 模型完整；删除临时 BF16 编码器和 staging 硬链接。\n'
  remove_guarded_tree "$full_source_root"
fi

cd "$work_dir"
run_vpipe --launch-stage model-register \
  --stage-cfg "model_dir=$final_root" --stage-cfg "key=$model_key" \
  --stage-cfg model_type=minimax-h3-fl2va \
  --stage-cfg overwrite_existing=true

larry_dir="$work_dir/models/larryvrh/MiniMax-H3-Turbo-Lora"
larry_name="minimax_h3_turbo_v4_step600_ema.safetensors"
download_checked \
  "https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora/resolve/$VPIPE_LARRY_LORA_REF/$larry_name?download=true" \
  "$larry_dir" "$larry_name" \
  "5f3a626cd72c93a8b9318d6760c510bc5092d2ab13aaba1f932c5bab07a416d3"
run_vpipe --launch-stage model-register \
  --stage-cfg "model_dir=$larry_dir" \
  --stage-cfg key=larryvrh/MiniMax-H3-Turbo-Lora-v4-600-ema \
  --stage-cfg model_type=minimax-h3-lora \
  --stage-cfg overwrite_existing=true

lightx_dir="$work_dir/models/lightx2v/Minimax-h3-Turbo"
lightx_name="minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"
download_checked \
  "https://huggingface.co/lightx2v/Minimax-h3-Turbo/resolve/$VPIPE_LIGHTX_LORA_REF/$lightx_name?download=true" \
  "$lightx_dir" "$lightx_name" \
  "c396a9a06f58399e9df9754b18299818d84a2ddd371724ba48fe4a41221437dc"
run_vpipe --launch-stage model-register \
  --stage-cfg "model_dir=$lightx_dir" \
  --stage-cfg key=lightx2v/Minimax-h3-Turbo-4step-768p \
  --stage-cfg model_type=minimax-h3-lora \
  --stage-cfg overwrite_existing=true

"$PYTHON" "$PROJECT_ROOT/scripts/verify_vpipe_assets.py" \
  --project-root "$PROJECT_ROOT" --initialize-manifest
printf '生成本地 Q8 权重 SHA-256 清单（会顺序读取约 67.5 GiB）……\n'
"$PYTHON" "$PROJECT_ROOT/scripts/verify_vpipe_assets.py" \
  --project-root "$PROJECT_ROOT" --write-hashes --files-only
"$PYTHON" "$PROJECT_ROOT/scripts/verify_vpipe_assets.py" --project-root "$PROJECT_ROOT"

if (( core_ready == 1 )); then
  remove_guarded_tree "$dit_quant_root"
  remove_guarded_tree "$full_source_root"
  remove_guarded_tree "$dit_source_root"
fi

printf '\nQ8 H3 与两套 Turbo LoRA 已准备完成：%s\n' "$final_root"
