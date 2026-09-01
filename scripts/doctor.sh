#!/bin/bash
set -u

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
H3_ROOT="$PROJECT_ROOT/runtime/h3.c"
H3_BINARY="$H3_ROOT/h3"
MODEL_ROOT="$PROJECT_ROOT/runtime/models/MiniMax-H3"
MODEL_MANIFEST="$PROJECT_ROOT/runtime/models/MiniMax-H3.manifest.json"
MODEL_VENV="$PROJECT_ROOT/runtime/model-tools-venv"
source "$PROJECT_ROOT/versions.env"
export PATH="$PROJECT_ROOT/runtime/bin:$HOME/.local/bin:/opt/homebrew/bin:$PATH"
failed=0

check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf '✅ %s\n' "$label"
  else
    printf '❌ %s\n' "$label"
    failed=1
  fi
}

optional_check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf '✅ %s\n' "$label"
  else
    printf '⚠️  %s（可选；auto 会使用系统指标回退）\n' "$label"
  fi
}

detailed_check() {
  local label="$1"
  shift
  local output
  if output="$("$@" 2>&1)"; then
    printf '✅ %s\n' "$label"
  else
    printf '❌ %s\n' "$label"
    printf '   %s\n' "$output" >&2
    failed=1
    return 1
  fi
  return 0
}

h3_model_info() {
  (cd "$H3_ROOT" && "$H3_BINARY" -d "$MODEL_ROOT" --info)
}

printf 'ComfyUI-H3-Mac 环境检查\n\n'
[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] && printf '✅ Apple Silicon macOS\n' || { printf '❌ 需要 Apple Silicon macOS\n'; failed=1; }
macos_major="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"
[[ "$macos_major" =~ ^[0-9]+$ && "$macos_major" -ge 15 ]] && printf '✅ macOS 15 或更高版本\n' || { printf '❌ h3.c 需要 macOS 15 或更高版本\n'; failed=1; }
sdk_version="$(xcrun --sdk macosx --show-sdk-version 2>/dev/null || true)"
sdk_major="${sdk_version%%.*}"
[[ "$sdk_major" =~ ^[0-9]+$ && "$sdk_major" -ge 26 ]] && printf '✅ macOS SDK %s\n' "$sdk_version" || { printf '❌ h3.c 编译需要 macOS SDK 26 或更高版本（当前：%s）\n' "${sdk_version:-无法读取}"; failed=1; }
check "FFmpeg" command -v ffmpeg
check "FFprobe" command -v ffprobe
if command -v vpipe >/dev/null 2>&1; then
  vpipe_version="$(vpipe --version 2>/dev/null || true)"
  vpipe_short_ref="${VPIPE_REF:0:7}"
  if [[ "$vpipe_version" == *"$vpipe_short_ref"* ]]; then
    printf '✅ vpipe v0.1.37（%s）\n' "$vpipe_short_ref"
  else
    printf '❌ vpipe 不是已验证的 v0.1.37（当前：%s）\n' \
      "${vpipe_version:-无法读取版本}"
    failed=1
  fi
else
  if [[ "$macos_major" =~ ^[0-9]+$ && "$macos_major" -lt 26 ]]; then
    printf '⚠️  vpipe v0.1.37 需要 macOS 26；当前仅可使用 h3.c 后端\n'
  else
    printf '❌ vpipe Q8 后端未安装；请重新运行 Install.command\n'
    failed=1
  fi
fi
check "h3.c 可执行文件" test -x "$H3_BINARY"
optional_check "前台卡顿保护器" test -x "$PROJECT_ROOT/runtime/bin/h3-guardian"
check "ComfyUI" test -f "$PROJECT_ROOT/runtime/ComfyUI/main.py"
check "Python 虚拟环境" test -x "$PROJECT_ROOT/runtime/.venv/bin/python"
detailed_check "ComfyUI / vpipe launchd 控制面实际就绪" \
  "$PROJECT_ROOT/runtime/.venv/bin/python" \
  "$PROJECT_ROOT/scripts/launchd.py" status
check "模型工具虚拟环境" test -x "$MODEL_VENV/bin/python"
if [[ -x "$MODEL_VENV/bin/python" ]]; then
  installed_hub_version="$("$MODEL_VENV/bin/python" -c 'import huggingface_hub; print(huggingface_hub.__version__)' 2>/dev/null || true)"
  if [[ "$installed_hub_version" == "$HUGGINGFACE_HUB_VERSION" ]]; then
    printf '✅ 模型下载工具版本 %s\n' "$installed_hub_version"
  else
    printf '❌ 模型下载工具版本应为 %s，当前为 %s\n' \
      "$HUGGINGFACE_HUB_VERSION" "${installed_hub_version:-未安装}"
    failed=1
  fi
fi

q8_ok=0
q8_error=""
if [[ -x "$PROJECT_ROOT/runtime/.venv/bin/python" ]]; then
  printf '⏳ 正在全量校验 vpipe Q8/LoRA SHA-256（需顺序读取约 67.5 GiB）……\n'
  if q8_error="$(
    "$PROJECT_ROOT/runtime/.venv/bin/python" \
      "$PROJECT_ROOT/scripts/verify_vpipe_assets.py" \
      --project-root "$PROJECT_ROOT" --deep 2>&1
  )"; then
    printf '✅ %s\n' "$q8_error"
    q8_ok=1
  fi
fi

if [[ -d "$MODEL_ROOT/FL2VA" ]]; then
  printf '✅ FL2VA 基础模型目录\n'
  manifest_ok=0
  if [[ ! -f "$MODEL_MANIFEST" ]]; then
    printf '❌ 缺少锁定模型清单；请重新运行 Download Model.command\n'
    failed=1
  elif [[ -x "$MODEL_VENV/bin/python" ]]; then
    required_task="FL2VA"
    [[ -d "$MODEL_ROOT/Ref2VA" ]] && required_task="Ref2VA"
    if detailed_check "锁定模型清单、任务与文件尺寸" \
      "$MODEL_VENV/bin/python" "$PROJECT_ROOT/scripts/model_snapshot.py" verify \
      --revision "$H3_MODEL_REF" \
      --model-root "$MODEL_ROOT" \
      --manifest "$MODEL_MANIFEST" \
      --require-task "$required_task"; then
      manifest_ok=1
    fi
  fi
  detailed_check "h3.c 模型结构检查（--info）" h3_model_info
  if [[ -d "$MODEL_ROOT/Ref2VA" ]]; then
    if [[ "$manifest_ok" -eq 1 ]]; then
      printf '✅ Ref2VA 扩展模型目录\n'
    else
      printf '❌ Ref2VA 目录存在但锁定清单未确认完整下载\n'
      failed=1
    fi
  else
    printf 'ℹ️  未安装 Ref2VA；首尾帧和文生视频仍可用\n'
  fi
else
  if [[ "$q8_ok" -eq 1 ]]; then
    printf 'ℹ️  未安装 h3.c BF16 模型；vpipe Q8 工作流仍可正常使用\n'
  else
    printf '❌ 尚未找到完整的 h3.c BF16 或 vpipe Q8 模型\n'
    [[ -n "$q8_error" ]] && printf '   %s\n' "$q8_error" >&2
    failed=1
  fi
fi

memsize="$(sysctl -n hw.memsize 2>/dev/null || true)"
if [[ "$memsize" =~ ^[0-9]+$ ]]; then
  printf '\n物理内存：%s GiB\n' "$(( memsize / 1024 / 1024 / 1024 ))"
else
  printf '\n物理内存：无法读取\n'
fi
printf '可用磁盘：%s\n' "$(df -h "$PROJECT_ROOT" | awk 'NR==2 {print $4}')"
exit "$failed"
