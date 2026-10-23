#!/bin/sh
# 手动同步：推送当前分支，并报告与远程的差距。
#
# 用途：post-commit 自动推送失败后（断网、凭据过期）恢复网络时补推。
# 也可以当日常「我现在推到哪了」的检查命令。
set -e

cd "$(cd "$(dirname "$0")/.." && pwd)"

BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "main")
BEHIND=$(git rev-list --count "origin/$BRANCH..$BRANCH" 2>/dev/null || echo "?")

echo "分支：$BRANCH    待推送提交：$BEHIND"
if [ "$BEHIND" = "0" ]; then
  echo "✔ 与 origin/$BRANCH 已同步"
  rm -f .git/autosync-pending
  exit 0
fi

git log --oneline "origin/$BRANCH..$BRANCH" | head -15

echo ""
echo "→ 推送中…"
if git push origin "$BRANCH"; then
  rm -f .git/autosync-pending
  echo "✔ 已同步到 origin/$BRANCH"
else
  echo ""
  echo "✘ 推送失败。常见原因："
  echo "   · 网络不可达 —— 检查代理：git config --get http.proxy"
  echo "   · 凭据过期   —— 重新登录：git credential-osxkeychain erase 后重试"
  echo "   · 远程有新提交 —— 先 git pull --rebase origin $BRANCH"
  echo ""
  echo "本地提交全部保留，不会丢。"
  exit 1
fi
