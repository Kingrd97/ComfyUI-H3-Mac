#!/bin/bash
set -u

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

printf 'ComfyUI-H3-Mac 环境检查\n\n'
[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] && printf '✅ Apple Silicon macOS\n' || { printf '❌ 需要 Apple Silicon macOS\n'; failed=1; }
check "FFmpeg" command -v ffmpeg
check "FFprobe" command -v ffprobe
check "h3.c 可执行文件" test -x "$PROJECT_ROOT/runtime/h3.c/h3"
check "ComfyUI" test -f "$PROJECT_ROOT/runtime/ComfyUI/main.py"
check "Python 虚拟环境" test -x "$PROJECT_ROOT/runtime/.venv/bin/python"

if [[ -d "$PROJECT_ROOT/runtime/models/MiniMax-H3/FL2VA" ]]; then
  printf '✅ FL2VA 基础模型目录\n'
  if [[ -d "$PROJECT_ROOT/runtime/models/MiniMax-H3/Ref2VA" ]]; then
    printf '✅ Ref2VA 扩展模型目录\n'
  else
    printf 'ℹ️  未安装 Ref2VA；首尾帧和文生视频仍可用\n'
  fi
else
  printf '⚠️  尚未找到 FL2VA 基础模型，请运行 Download Model.command\n'
fi

memsize="$(sysctl -n hw.memsize 2>/dev/null || true)"
if [[ "$memsize" =~ ^[0-9]+$ ]]; then
  printf '\n物理内存：%s GiB\n' "$(( memsize / 1024 / 1024 / 1024 ))"
else
  printf '\n物理内存：无法读取\n'
fi
printf '可用磁盘：%s\n' "$(df -h "$PROJECT_ROOT" | awk 'NR==2 {print $4}')"
exit "$failed"
