#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$PROJECT_ROOT/runtime"
COMFY="$RUNTIME/ComfyUI"
H3_SRC="$RUNTIME/h3.c"
VENV="$RUNTIME/.venv"
MODEL_VENV="$RUNTIME/model-tools-venv"
BIN="$RUNTIME/bin"
source "$PROJECT_ROOT/versions.env"

info() { printf '\n\033[1;34m[H3 Mac]\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m[H3 Mac 警告]\033[0m %s\n' "$*" >&2; }
die() { printf '\n\033[1;31m错误：\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "本项目只支持 macOS。"
[[ "$(uname -m)" == "arm64" ]] || die "h3.c 需要 Apple Silicon（M 系列芯片）。"
macos_major="$(sw_vers -productVersion | cut -d. -f1)"
[[ "$macos_major" =~ ^[0-9]+$ && "$macos_major" -ge 15 ]] || \
  die "锁定版 h3.c 使用 macOS 15 引入的 Metal API，需要 macOS 15 或更高版本。"
command -v xcode-select >/dev/null || die "找不到 xcode-select。"
xcode-select -p >/dev/null 2>&1 || die "请先执行 xcode-select --install，安装完成后再运行本脚本。"
sdk_version="$(xcrun --sdk macosx --show-sdk-version 2>/dev/null || true)"
sdk_major="${sdk_version%%.*}"
[[ "$sdk_major" =~ ^[0-9]+$ && "$sdk_major" -ge 26 ]] || \
  die "锁定版 h3.c 编译需要 macOS SDK 26 或更高版本（当前：${sdk_version:-无法读取}）。请升级 Xcode 或 Xcode Command Line Tools。"
command -v brew >/dev/null || die "请先从 https://brew.sh 安装 Homebrew。"

info "安装系统依赖（FFmpeg、Python、Git、断点续传下载器）"
brew install ffmpeg python@3.12 git aria2

mkdir -p "$RUNTIME/models" "$BIN"

if [[ "$macos_major" -ge 26 ]]; then
  /bin/bash "$PROJECT_ROOT/scripts/install_vpipe.sh"
else
  warn "vpipe v${VPIPE_VERSION} 官方二进制需要 macOS 26；本机将只安装 h3.c 后端。"
fi

if [[ -d "$COMFY/.git" ]]; then
  info "同步已验证的 ComfyUI 版本"
  git -C "$COMFY" fetch --depth 1 origin "$COMFYUI_REF"
else
  info "下载 ComfyUI"
  git clone --filter=blob:none --no-checkout https://github.com/Comfy-Org/ComfyUI.git "$COMFY"
fi
git -C "$COMFY" checkout --detach "$COMFYUI_REF"

if [[ -d "$H3_SRC/.git" ]]; then
  info "同步已验证的 h3.c 版本"
  git -C "$H3_SRC" fetch --depth 1 origin "$H3_REF"
else
  info "下载 h3.c"
  git clone --filter=blob:none --no-checkout https://github.com/antirez/h3.c.git "$H3_SRC"
fi
git -C "$H3_SRC" checkout --detach "$H3_REF"
git -C "$H3_SRC" submodule update --init --recursive

info "编译 h3.c（Apple Metal）"
make -C "$H3_SRC" clean
MACOSX_DEPLOYMENT_TARGET=15.0 make -C "$H3_SRC" -j"$(sysctl -n hw.logicalcpu)"

info "编译前台卡顿保护器（无界面、无需额外权限）"
SWIFT_MODULE_CACHE="$RUNTIME/swift-module-cache"
mkdir -p "$SWIFT_MODULE_CACHE"
if ! xcrun swiftc \
    -O \
    -target arm64-apple-macos12.0 \
    -module-cache-path "$SWIFT_MODULE_CACHE" \
    -framework AppKit \
    -framework CoreGraphics \
    -framework CoreVideo \
    -framework QuartzCore \
    "$PROJECT_ROOT/native/H3Guardian.swift" \
    -o "$BIN/h3-guardian"; then
  rm -f "$BIN/h3-guardian"
  warn "原生保护器编译失败；安装会继续，auto 将自动使用 CPU/WindowServer/GPU 回退指标。建议升级 Xcode Command Line Tools 后重跑安装。"
fi

info "建立隔离的 Python 环境"
"$(brew --prefix python@3.12)/bin/python3.12" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel
"$VENV/bin/python" -m pip install -r "$COMFY/requirements.txt"
"$VENV/bin/python" -m pip install -r "$PROJECT_ROOT/requirements.txt"

info "建立隔离的模型下载工具环境"
"$(brew --prefix python@3.12)/bin/python3.12" -m venv "$MODEL_VENV"
"$MODEL_VENV/bin/python" -m pip install "huggingface_hub==$HUGGINGFACE_HUB_VERSION"

info "接入 ComfyUI 自定义节点"
mkdir -p "$COMFY/custom_nodes"
NODE_LINK="$COMFY/custom_nodes/ComfyUI-H3-Mac"
if [[ -L "$NODE_LINK" ]]; then
  rm "$NODE_LINK"
elif [[ -e "$NODE_LINK" ]]; then
  die "$NODE_LINK 已存在且不是本项目的链接，请先手动处理。"
fi
ln -s "$PROJECT_ROOT" "$NODE_LINK"

info "创建或安全迁移本地配置"
"$VENV/bin/python" "$PROJECT_ROOT/scripts/migrate_config.py" \
  "$PROJECT_ROOT/config.json" \
  "$PROJECT_ROOT/config.example.json"

chmod +x "$PROJECT_ROOT"/*.command "$PROJECT_ROOT"/scripts/*.sh "$PROJECT_ROOT"/scripts/*.py

info "安装并启动 ComfyUI / vpipe launchd 后台服务"
warn "现在会短暂重启 ComfyUI/worker 以加载新代码；存活的 vpipe 引擎会按进程指纹恢复接管。"
"$VENV/bin/python" "$PROJECT_ROOT/scripts/launchd.py" restart

info "安装完成"
printf '%s\n' \
  "1. 双击 Download Model.command；推荐选择 vpipe Q8（约 65 GiB，首次需约 120 GiB 空闲）" \
  "2. ComfyUI 与 vpipe worker 已由 launchd 自动保活；Start.command 可检查并打开界面" \
  "3. 浏览器打开 http://127.0.0.1:8188" \
  "4. 运行中可双击 H3 Control.command 暂停、继续或切换资源策略"
