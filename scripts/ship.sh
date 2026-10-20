#!/usr/bin/env bash
# 一键提交并推送：scripts/ship.sh "fix: 修复 xxx"
#
# 约定（见 ~/.workbuddy/MEMORY.md）：
#   - 代码到可提交状态就直接推，不征询
#   - 提交信息用 Conventional Commits 前缀 + 中文正文
#   - 推前先跑测试：Python `./run.sh test`，Android `./gradlew assembleDebug`
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

MSG="${1:-}"
if [ -z "$MSG" ]; then
    echo "用法: scripts/ship.sh \"fix: 说明\"" >&2
    exit 2
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# 防护：明显不该入库的东西不上传
BLOCKED=$(git status --porcelain | awk '{print $2}' | grep -E '(^|/)(local\.properties|.*\.log|.*\.apk|.*\.keystore)$' || true)
if [ -n "$BLOCKED" ]; then
    echo "检测到不应入库的文件，请先加入 .gitignore 或移出：" >&2
    echo "$BLOCKED" >&2
    exit 3
fi

git add -A
if git diff --cached --quiet; then
    echo "无改动，跳过提交"
else
    printf '%s\n' "$MSG" | git commit -F - > /dev/null
    echo "已提交: $(git log --oneline -1)"
fi

git push origin "$BRANCH" 2>&1 | tail -2
echo "远程: $(git log --oneline "origin/$BRANCH" -1)"
