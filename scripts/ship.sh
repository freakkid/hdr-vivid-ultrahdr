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

# ---- 本地自检门禁（约定：每次打包/提交前必须本地测试通过）----
# 找不到解释器或样例时按「环境缺失」跳过（返回码 2），不算失败；
# 但只要自检返回 1（真失败），就阻止提交与推送。
PY="${PYTHON:-python3}"
for c in \
    "$HOME/.workbuddy/binaries/python/envs/default/Scripts/python.exe" \
    "$HOME/.workbuddy/binaries/python/envs/default/bin/python"; do
    if [ -x "$c" ]; then PY="$c"; break; fi
done
if command -v "$PY" >/dev/null 2>&1 || [ -x "$PY" ]; then
    echo "== 本地自检（verify_bake.py --no-pull）"
    set +e
    "$PY" scripts/verify_bake.py --no-pull
    GATE=$?
    set -e
    if [ "$GATE" -eq 1 ]; then
        echo "自检未通过（exit=1）→ 已阻止提交与推送" >&2
        exit 5
    elif [ "$GATE" -eq 2 ]; then
        echo "自检环境缺失（exit=2）→ 跳过门禁（真机项需先跑 verify_bake 拉样例）" >&2
    fi
else
    echo "找不到 Python（$PY）→ 跳过自检门禁" >&2
fi

git add -A
if git diff --cached --quiet; then
    echo "无改动，跳过提交"
else
    printf '%s\n' "$MSG" | git commit -F - > /dev/null
    echo "已提交: $(git log --oneline -1)"
fi

# 推送。GitHub 的 22 端口在部分网络下会被阻断（本机实测直接挂起），
# 官方提供 443 备用通道（ssh.github.com:443），失败后自动回退一次。
KEY="$HOME/.ssh/id_ed25519_github"
# 注意：不要写 -o UserKnownHostsFile=/dev/null。
# Git Bash(MSYS) 会把 /dev/null 改写成当前目录下的**真实文件 `NUL`**，
# 于是仓库里多出一个未被跟踪的 NUL 文件，直接让 `git add -A` 报
# "unable to index file 'NUL'" 而中止提交。用真实文件最稳。
KNOWN_HOSTS="$HOME/.ssh/known_hosts_github"
touch "$KNOWN_HOSTS" 2>/dev/null || true
push_via_443() {
    GIT_SSH_COMMAND="ssh -o HostName=ssh.github.com -p 443 -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=$KNOWN_HOSTS" git push origin "$BRANCH"
}

if git push origin "$BRANCH" 2>&1 | tail -2; then
    :
else
    echo "22 端口推送失败，改用 443 通道（ssh.github.com）…" >&2
    if push_via_443 2>&1 | tail -2; then
        echo "（已通过 443 通道推送成功）"
    else
        echo "443 通道也失败。检查网络或 SSH key：ssh -T git@ssh.github.com -p 443" >&2
        exit 4
    fi
fi
echo "远程: $(git log --oneline "origin/$BRANCH" -1)"
