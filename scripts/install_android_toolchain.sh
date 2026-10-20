#!/usr/bin/env bash
# 在 dl.google.com 不可达的环境下，安装 JDK 17 + Android SDK 34（手工布局，不依赖 sdkmanager）
#
# 用法： bash scripts/install_android_toolchain.sh
# 覆盖： JDK_DIR=... SDK_DIR=... bash scripts/install_android_toolchain.sh
set -euo pipefail

JDK_DIR="${JDK_DIR:-$HOME/.workbuddy/binaries/jdk/17}"
SDK_DIR="${SDK_DIR:-$HOME/Android/Sdk}"
TMP="${TMP:-$HOME/AppData/Local/Temp/hdrdl}"

JDK_URL="https://mirrors.tuna.tsinghua.edu.cn/Adoptium/17/jdk/x64/windows/OpenJDK17U-jdk_x64_windows_hotspot_17.0.20.1_1.zip"
SDK_BASE="https://mirrors.cloud.tencent.com/AndroidSDK"

mkdir -p "$TMP"

fetch() {
    local url="$1" out="$2"
    if [ -s "$out" ]; then echo "已存在，跳过：$out"; return 0; fi
    echo "下载：$url"
    curl -sSL --max-time 1800 -o "$out" "$url"
    echo "  -> $(du -h "$out" | cut -f1)"
}

fetch "$JDK_URL" "$TMP/jdk17.zip"
fetch "$SDK_BASE/platform-34-ext7_r03.zip" "$TMP/platform-34.zip"
fetch "$SDK_BASE/build-tools_r34-windows.zip" "$TMP/build-tools.zip"
fetch "$SDK_BASE/platform-tools_r37.0.1-win.zip" "$TMP/platform-tools.zip"

echo "解压到：JDK=$JDK_DIR  SDK=$SDK_DIR"
python3 - "$TMP" "$JDK_DIR" "$SDK_DIR" <<'PY'
import sys, zipfile, pathlib, shutil

tmp, jdk_dir, sdk_dir = sys.argv[1], sys.argv[2], sys.argv[3]

def extract(zip_path, dest, strip_top):
    dest = pathlib.Path(dest); dest.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            parts = info.filename.split('/')
            if parts[0] != strip_top or len(parts) < 2:
                continue
            rel = '/'.join(parts[1:])
            if not rel:
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as s, open(out, 'wb') as d:
                shutil.copyfileobj(s, d)
            n += 1
    return n

T = pathlib.Path(tmp)
print("  JDK      :", extract(T / "jdk17.zip", jdk_dir, "jdk-17.0.20.1+1"))
print("  platform :", extract(T / "platform-34.zip", f"{sdk_dir}/platforms/android-34", "android-34"))
print("  buildtool:", extract(T / "build-tools.zip", f"{sdk_dir}/build-tools/34.0.0", "android-14"))
print("  pltools  :", extract(T / "platform-tools.zip", f"{sdk_dir}/platform-tools", "platform-tools"))

lic = pathlib.Path(sdk_dir) / "licenses"
lic.mkdir(parents=True, exist_ok=True)
(lic / "android-sdk-license").write_text(
    "8933bad161af4178b1185d1a37fbf41ea5269c55\n"
    "d56f5187479451eabf01fb78af6dfcb131a6481e\n"
    "24333f8a63b6825ea9c5514f83c2829b004d1fee\n", encoding="utf-8")
print("  licenses written")
PY

cat > "$(dirname "$SDK_DIR")/../.android_env" 2>/dev/null <<EOF || true
export JAVA_HOME="$JDK_DIR"
export ANDROID_HOME="$SDK_DIR"
export ANDROID_SDK_ROOT="$SDK_DIR"
EOF

echo
echo "安装完成。构建前请设置："
echo "  export JAVA_HOME=\"$JDK_DIR\""
echo "  export ANDROID_HOME=\"$SDK_DIR\""
echo "  export ANDROID_SDK_ROOT=\"$SDK_DIR\""
echo
echo "验证："
echo "  \"\$JAVA_HOME/bin/java\" -version"
echo "  ls \"\$ANDROID_HOME/platforms/android-34/android.jar\""
