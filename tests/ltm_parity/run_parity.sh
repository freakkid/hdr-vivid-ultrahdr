#!/usr/bin/env bash
# LocalToneMapping 双实现对照：Kotlin（App 侧） vs Python（桌面侧）同输入同参数。
#
# 用法：bash tests/ltm_parity/run_parity.sh
# 依赖：numpy（Python 侧）；kotlinc（Kotlin 侧，缺失则跳过并提示，不算失败）
# 产物：tests/_tmp/ltm_parity/（gitignored）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$ROOT/tests/_tmp/ltm_parity"
KOTLIN_SRC="$ROOT/src/android/app/src/main/java/com/finn/hdr/LocalToneMapping.kt"

# Python：优先隔离 venv，否则系统 python3
PY=python3
for cand in "$HOME/.workbuddy/binaries/python/envs/default/bin/python"; do
  [ -x "$cand" ] && PY="$cand" && break
done

"$PY" "$ROOT/tests/ltm_parity/gen_input.py"

KOTLINC="$(command -v kotlinc || true)"
if [ -z "$KOTLINC" ] && [ -n "${KOTLINC_HOME:-}" ] && [ -x "$KOTLINC_HOME/bin/kotlinc" ]; then
  KOTLINC="$KOTLINC_HOME/bin/kotlinc"
fi
if [ -z "$KOTLINC" ]; then
  echo "SKIP: 未找到 kotlinc（安装 Kotlin 编译器后可跑双实现对照；Python 侧单跑不受影响）"
  exit 0
fi

mkdir -p "$TMP"
"$KOTLINC" "$ROOT/tests/ltm_parity/ParityTest.kt" "$KOTLIN_SRC" -include-runtime -d "$TMP/parity.jar" 2>/dev/null

for tag in local wechat; do
  java -jar "$TMP/parity.jar" "$tag" \
    "$TMP/input_$tag.bin" "$TMP/kotlin_out_$tag.bin" "$TMP/kotlin_stats_$tag.txt"
done

"$PY" "$ROOT/tests/ltm_parity/compare.py"
