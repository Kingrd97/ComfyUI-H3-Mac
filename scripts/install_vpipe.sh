#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/versions.env"
RUNTIME="$PROJECT_ROOT/runtime"
DOWNLOADS="$RUNTIME/downloads"
APP_ROOT="$RUNTIME/vpipe"
BIN="$RUNTIME/bin"
short_ref="${VPIPE_REF:0:7}"
APP="$APP_ROOT/Vpipe Manager-${VPIPE_VERSION}-${short_ref}.app"

info() { printf '\n\033[1;34m[vpipe]\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mvpipe 安装错误：\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] || \
  die "vpipe H3 后端需要 Apple Silicon macOS。"
macos_major="$(sw_vers -productVersion | cut -d. -f1)"
[[ "$macos_major" =~ ^[0-9]+$ && "$macos_major" -ge 26 ]] || \
  die "vpipe v${VPIPE_VERSION} 官方二进制需要 macOS 26 或更高版本。"

mkdir -p "$DOWNLOADS" "$APP_ROOT" "$BIN"
DMG="$DOWNLOADS/VpipeManager-${VPIPE_VERSION}-with-ffmpeg.dmg"
SOURCE_DMG="${VPIPE_DMG_SOURCE:-}"

if [[ -n "$SOURCE_DMG" ]]; then
  [[ -f "$SOURCE_DMG" ]] || die "指定的离线 DMG 不存在：$SOURCE_DMG"
  if [[ "$SOURCE_DMG" != "$DMG" ]]; then
    cp "$SOURCE_DMG" "$DMG.part"
    mv "$DMG.part" "$DMG"
  fi
elif [[ ! -f "$DMG" ]]; then
  info "下载官方签名 vpipe v${VPIPE_VERSION}（约 28 MB，可续传）"
  curl --fail --location --retry 5 --retry-delay 2 \
    --continue-at - --output "$DMG.part" "$VPIPE_RELEASE_URL"
  mv "$DMG.part" "$DMG"
fi

actual_sha="$(shasum -a 256 "$DMG" | awk '{print $1}')"
if [[ "$actual_sha" != "$VPIPE_RELEASE_SHA256" ]]; then
  /bin/rm -f -- "$DMG" "$DMG.part"
  die "官方 DMG SHA-256 不匹配；已删除缓存，请重试。"
fi

mount_dir="$(mktemp -d "${TMPDIR:-/tmp}/comfyui-h3-vpipe.XXXXXX")"
mounted=0
cleanup() {
  if [[ "$mounted" == "1" ]]; then
    hdiutil detach "$mount_dir" >/dev/null 2>&1 || true
  fi
  /bin/rm -rf -- "$mount_dir"
}
trap cleanup EXIT INT TERM

info "验证并安装官方签名应用包"
hdiutil attach -nobrowse -readonly -mountpoint "$mount_dir" "$DMG" >/dev/null
mounted=1
source_app="$mount_dir/Vpipe Manager.app"
[[ -d "$source_app" ]] || die "DMG 中缺少 Vpipe Manager.app。"
codesign --verify --deep --strict "$source_app" || die "vpipe 官方应用签名验证失败。"

staging_app="$APP_ROOT/.Vpipe Manager-${VPIPE_VERSION}-${short_ref}.app.new"
/bin/rm -rf -- "$staging_app"
ditto "$source_app" "$staging_app"
codesign --verify --deep --strict "$staging_app" || die "复制后的 vpipe 签名验证失败。"
version_output="$("$staging_app/Contents/Helpers/vpipe" --version 2>&1 || true)"
[[ "$version_output" == *"$short_ref"* ]] || \
  die "vpipe 版本不匹配（需要 $short_ref，得到：${version_output:-无输出}）。"

if [[ -d "$APP" ]]; then
  installed_version="$("$APP/Contents/Helpers/vpipe" --version 2>&1 || true)"
  if codesign --verify --deep --strict "$APP" >/dev/null 2>&1 && \
      [[ "$installed_version" == *"$short_ref"* ]]; then
    /bin/rm -rf -- "$staging_app"
  else
    backup_app="$APP_ROOT/.Vpipe Manager-${VPIPE_VERSION}-${short_ref}.app.previous"
    /bin/rm -rf -- "$backup_app"
    mv "$APP" "$backup_app"
    if ! mv "$staging_app" "$APP"; then
      mv "$backup_app" "$APP"
      die "替换损坏的 vpipe 版本失败；旧目录已恢复。"
    fi
    /bin/rm -rf -- "$backup_app"
  fi
else
  mv "$staging_app" "$APP"
fi

atomic_link() {
  link_target="$1"
  link_path="$2"
  temporary_link="$link_path.new.$$"
  /bin/rm -f -- "$temporary_link"
  ln -s "$link_target" "$temporary_link"
  mv -f "$temporary_link" "$link_path"
}
atomic_link \
  "../vpipe/$(basename "$APP")/Contents/Helpers/vpipe" "$BIN/vpipe"
atomic_link \
  "../vpipe/$(basename "$APP")/Contents/Helpers/vpipe-web-ui" "$BIN/vpipe-web-ui"

info "vpipe v${VPIPE_VERSION} 安装完成：$APP"
