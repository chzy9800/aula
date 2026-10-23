#!/bin/sh
# 安装 git 钩子（符号链接，改 scripts/hooks/ 下的文件即刻生效）。
#
# 用法：bash scripts/install_git_hooks.sh
set -e

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

if [ ! -d .git ]; then
  echo "✘ 当前目录不是 git 仓库：$ROOT"
  exit 1
fi

chmod +x scripts/hooks/post-commit scripts/hooks/commit-msg

# 相对路径的符号链接：仓库整体搬走也不会断
ln -sf ../../scripts/hooks/post-commit .git/hooks/post-commit
ln -sf ../../scripts/hooks/commit-msg .git/hooks/commit-msg

echo "✔ 已安装："
echo "   .git/hooks/post-commit  → 每次提交后自动 push origin/main"
echo "   .git/hooks/commit-msg   → 校验提交信息格式 type(scope): 描述"
echo ""
echo "临时跳过自动推送：AULA_NO_AUTOSYNC=1 git commit ..."
echo "手动补推：scripts/git-sync.sh"
