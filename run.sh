#!/usr/bin/env bash
# 一键入口：demo / test / export / validate
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Windows(Git Bash) 下 PYTHONPATH 需用 ';' 分隔，且路径要转成 C:/ 形式
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    SEP=";"
    ROOT_PY="$(cygpath -m "$ROOT")"
    ;;
  *)
    SEP=":"
    ROOT_PY="$ROOT"
    ;;
esac

PY="${PYTHON:-python3}"

# 优先使用隔离 venv
if [ -x "$HOME/.workbuddy/binaries/python/envs/default/Scripts/python.exe" ]; then
  PY="$HOME/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
elif [ -x "$HOME/.workbuddy/binaries/python/envs/default/bin/python" ]; then
  PY="$HOME/.workbuddy/binaries/python/envs/default/bin/python"
fi

export PYTHONPATH="$ROOT_PY/src/python${PYTHONPATH:+$SEP$PYTHONPATH}"

case "${1:-demo}" in
  demo)
    "$PY" -m uhdr.cli demo --root "$ROOT"
    ;;
  test)
    "$PY" -m unittest discover -s "$ROOT/tests" -v
    ;;
  export)
    shift
    "$PY" -m uhdr.cli export "$@"
    ;;
  validate)
    shift
    "$PY" -m uhdr.cli validate "$@"
    ;;
  inspect)
    shift
    "$PY" -m uhdr.cli inspect "$@"
    ;;
  *)
    echo "用法: $0 {demo|test|export|validate|inspect} [args...]"
    exit 2
    ;;
esac
