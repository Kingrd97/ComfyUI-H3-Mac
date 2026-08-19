#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$PROJECT_ROOT/runtime"
COMFY="$RUNTIME/ComfyUI"
H3_SRC="$RUNTIME/h3.c"
VENV="$RUNTIME/.venv"

info() { printf '\n\033[1;34m[H3 Mac]\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31m错误：\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "本项目只支持 macOS。"
[[ "$(uname -m)" == "arm64" ]] || die "h3.c 需要 Apple Silicon（M 系列芯片）。"
command -v xcode-select >/dev/null || die "找不到 xcode-select。"
xcode-select -p >/dev/null 2>&1 || die "请先执行 xcode-select --install，安装完成后再运行本脚本。"
command -v brew >/dev/null || die "请先从 https://brew.sh 安装 Homebrew。"

info "安装系统依赖（FFmpeg、Python、Git）"
brew install ffmpeg python@3.12 git

mkdir -p "$RUNTIME/models"

if [[ -d "$COMFY/.git" ]]; then
  info "更新 ComfyUI"
  git -C "$COMFY" pull --ff-only
else
  info "下载 ComfyUI"
  git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git "$COMFY"
fi

if [[ -d "$H3_SRC/.git" ]]; then
  info "更新 h3.c"
  git -C "$H3_SRC" pull --ff-only
else
  info "下载 h3.c"
  git clone --recursive https://github.com/antirez/h3.c.git "$H3_SRC"
fi
git -C "$H3_SRC" submodule update --init --recursive

info "编译 h3.c（Apple Metal）"
make -C "$H3_SRC" -j"$(sysctl -n hw.logicalcpu)"

info "建立隔离的 Python 环境"
"$(brew --prefix python@3.12)/bin/python3.12" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel
"$VENV/bin/python" -m pip install -r "$COMFY/requirements.txt"
"$VENV/bin/python" -m pip install -r "$PROJECT_ROOT/requirements.txt"

info "接入 ComfyUI 自定义节点"
mkdir -p "$COMFY/custom_nodes"
NODE_LINK="$COMFY/custom_nodes/ComfyUI-H3-Mac"
if [[ -L "$NODE_LINK" ]]; then
  rm "$NODE_LINK"
elif [[ -e "$NODE_LINK" ]]; then
  die "$NODE_LINK 已存在且不是本项目的链接，请先手动处理。"
fi
ln -s "$PROJECT_ROOT" "$NODE_LINK"

if [[ ! -f "$PROJECT_ROOT/config.json" ]]; then
  cp "$PROJECT_ROOT/config.example.json" "$PROJECT_ROOT/config.json"
fi

chmod +x "$PROJECT_ROOT"/*.command "$PROJECT_ROOT"/scripts/*.sh

info "安装完成"
printf '%s\n' \
  "1. 双击 Download Model.command 下载模型" \
  "2. 双击 Start.command 启动 ComfyUI" \
  "3. 浏览器打开 http://127.0.0.1:8188"
