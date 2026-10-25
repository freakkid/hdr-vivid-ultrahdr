#!/usr/bin/env bash
# LocalToneMapping 双实现对照：Kotlin（App 侧） vs Python（桌面侧）同输入同参数。
#
# 用法：bash tests/ltm_parity/run_parity.sh
# 依赖：numpy（Python 侧）；kotlinc（Kotlin 侧）
# 退出码（三态，docs/07 §2-6）：0 通过 / 1 编译失败或数值不一致（FAIL）/
#   2 工具缺失（无 kotlinc，SKIP —— verify_bake.py 据此记 SKIP 而非 PASS）
# 产物：tests/_tmp/ltm_parity/（gitignored）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$ROOT/tests/_tmp/ltm_parity"
KOTLIN_SRC="$ROOT/src/android/app/src/main/java/com/finn/hdr/LocalToneMapping.kt"

# kotlinc：环境变量 KOTLINC → PATH → KOTLINC_HOME → workbuddy 固定目录（含 Windows 的 .bat）。
# 显式指定的 KOTLINC 无效时不回落：门禁跑的和用户以为在跑的必须是同一个编译器。
_env_kotlinc="${KOTLINC:-}"
KOTLINC=""
if [ -n "$_env_kotlinc" ]; then
  [ -f "$_env_kotlinc" ] && KOTLINC="$_env_kotlinc"
elif cand="$(command -v kotlinc 2>/dev/null)"; then
  KOTLINC="$cand"
fi
if [ -z "$KOTLINC" ]; then
  for cand in \
    "${KOTLINC_HOME:-/nonexistent}/bin/kotlinc" \
    "${KOTLINC_HOME:-/nonexistent}/bin/kotlinc.bat" \
    "$HOME/.workbuddy/binaries/kotlinc/kotlinc/bin/kotlinc" \
    "$HOME/.workbuddy/binaries/kotlinc/kotlinc/bin/kotlinc.bat"
  do
    [ -f "$cand" ] && KOTLINC="$cand" && break
  done
fi
if [ -z "$KOTLINC" ]; then
  echo "SKIP: 未找到 kotlinc（设 KOTLINC=<path> 或 PATH 后可跑双实现对照）"
  exit 2
fi

# java：JAVA_HOME → workbuddy jdk17 → PATH；kotlinc 自身也需要 JAVA_HOME
JAVA=java
if [ -z "${JAVA_HOME:-}" ] && [ -d "$HOME/.workbuddy/binaries/jdk/17" ]; then
  export JAVA_HOME="$HOME/.workbuddy/binaries/jdk/17"
  export PATH="$JAVA_HOME/bin:$PATH"
fi
[ -n "${JAVA_HOME:-}" ] && [ -f "$JAVA_HOME/bin/java.exe" ] && JAVA="$JAVA_HOME/bin/java.exe"

# Python：优先隔离 venv。Windows 的 venv 在 Scripts/python.exe（HANDOVER §5.1：
# 此前只探测 POSIX 路径 bin/python，Windows 上回落到无 numpy 的系统 python3）
PY=python3
for cand in \
  "$HOME/.workbuddy/binaries/python/envs/default/bin/python" \
  "$HOME/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
do
  [ -f "$cand" ] && PY="$cand" && break
done

mkdir -p "$TMP"
"$PY" "$ROOT/tests/ltm_parity/gen_input.py"

echo "[build] kotlinc → $TMP/parity.jar"
if ! "$KOTLINC" "$ROOT/tests/ltm_parity/ParityTest.kt" "$KOTLIN_SRC" -include-runtime -d "$TMP/parity.jar"; then
  echo "FAIL: kotlinc 编译失败（报错见上）"
  exit 1
fi

for tag in local wechat; do
  "$JAVA" -jar "$TMP/parity.jar" "$tag" \
    "$TMP/input_$tag.bin" "$TMP/kotlin_out_$tag.bin" "$TMP/kotlin_stats_$tag.txt"
done

"$PY" "$ROOT/tests/ltm_parity/compare.py"
