#!/usr/bin/env bash
# 拉取 libultrahdr 到 third_party/（仅在需要 native 路线时执行）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/third_party/libultrahdr"

if [ -d "$DEST/.git" ]; then
    echo "已存在：$DEST"
    git -C "$DEST" pull --ff-only || true
    exit 0
fi

mkdir -p "$ROOT/third_party"

# 主源 + 国内镜像兜底
SOURCES=(
    "https://github.com/google/libultrahdr.git"
    "https://gitee.com/mirrors_google/libultrahdr.git"
)

for src in "${SOURCES[@]}"; do
    echo "尝试克隆：$src"
    if git clone --depth 1 "$src" "$DEST" 2>/dev/null; then
        echo "完成：$DEST"
        echo
        echo "接下来："
        echo "  cd src/android && ./gradlew assembleDebug -PwithNative=true"
        exit 0
    fi
    rm -rf "$DEST"
done

echo "所有源均失败。可手动下载后解压到：$DEST" >&2
exit 1
