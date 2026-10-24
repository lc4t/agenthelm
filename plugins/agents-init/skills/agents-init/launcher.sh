#!/bin/sh
# 项目入口（由 agents-init 生成，文件名可以改）：./<本文件> 打开项目菜单，
# ./<本文件> --help 查看不进菜单时可用的命令。不调用模型，不联网下载代码。
#
# 按顺序查找 agenthelm 运行时，找到第一个就交给它：
#   1. 环境变量 AGENTHELM_BIN 指向的命令
#   2. 用户运行时目录 ${XDG_DATA_HOME:-~/.local/share}/agenthelm/current
#   3. PATH 上的 agenthelm
#   4. 已安装的 AI 客户端里随插件带来的 agent-pack（需要 Python 3.11+）
# 都找不到时说明原因和下一步，退出码 3。
set -u
LAUNCHER_API=1
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

python_ok() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

find_python() {
  for candidate in "${AGENTHELM_PYTHON:-}" python3 python; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if python_ok "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

if [ -n "${AGENTHELM_BIN:-}" ]; then
  if [ -x "$AGENTHELM_BIN" ] || command -v "$AGENTHELM_BIN" >/dev/null 2>&1; then
    exec "$AGENTHELM_BIN" project --project "$root" --via-launcher "$LAUNCHER_API" "$@"
  fi
  echo "AGENTHELM_BIN 指向的命令不存在：${AGENTHELM_BIN}" >&2
  echo "  · 改正或删除这个环境变量后重试" >&2
  exit 3
fi

current="${XDG_DATA_HOME:-$HOME/.local/share}/agenthelm/current/bin/agenthelm"
if [ -x "$current" ]; then
  exec "$current" project --project "$root" --via-launcher "$LAUNCHER_API" "$@"
fi

if command -v agenthelm >/dev/null 2>&1; then
  exec agenthelm project --project "$root" --via-launcher "$LAUNCHER_API" "$@"
fi

python=$(find_python) || python=""
if [ -n "$python" ]; then
  # 客户端插件缓存：Claude Code、Codex 为 <插件>/<版本>/，Cursor 为本地目录；取版本最高的一个。
  best=$("$python" - "$HOME" "${CLAUDE_CONFIG_DIR:-$HOME/.claude}" "${CODEX_HOME:-$HOME/.codex}" <<'PY'
import re, sys
from pathlib import Path
home, claude, codex = map(Path, sys.argv[1:4])
patterns = [(claude / "plugins/cache/agenthelm/agent-pack", "*/runtime/agenthelm.py"),
            (codex / "plugins/cache/agenthelm/agent-pack", "*/runtime/agenthelm.py"),
            (home / ".cursor/plugins/local/agent-pack", "runtime/agenthelm.py")]
def key(path):
    match = re.search(r"/(\d+)\.(\d+)\.(\d+)/runtime/", path.as_posix())
    return tuple(map(int, match.groups())) if match else (0, 0, 0)
found = [path for base, pattern in patterns for path in base.glob(pattern)]
print(max(found, key=key) if found else "")
PY
)
  if [ -n "$best" ]; then
    exec "$python" "$best" project --project "$root" --via-launcher "$LAUNCHER_API" "$@"
  fi
fi

{
  echo "打不开项目菜单：本机还没有 agenthelm 运行时。"
  if [ -z "$python" ]; then
    old=$(python3 -c 'import platform; print(platform.python_version())' 2>/dev/null || true)
    if [ -n "$old" ]; then
      echo "原因：找到的 Python 是 ${old}，需要 3.11 或更高版本（agenthelm 的日常功能只需要它，不需要 AI 客户端）。"
    else
      echo "原因：没有找到 Python 3.11 或更高版本（agenthelm 的日常功能只需要它，不需要 AI 客户端）。"
    fi
    echo "下一步："
    echo "  1. 安装 Python 3.11+（macOS 可用 python.org 安装包或 Homebrew；Linux 用发行版的包管理器）"
    echo "  2. 已装在别处时：export AGENTHELM_PYTHON=/path/to/python3"
    echo "  3. 然后安装运行时（见下）"
  else
    echo "原因：没有找到 agenthelm 运行时；日常使用只需要 Python 3.11+，不需要 AI 客户端。"
  fi
  echo "安装运行时（只需一次，之后打开菜单不再联网）："
  echo "  · 联网：npx @sakanano-moe/agenthelm runtime install --apply"
  echo "  · 离线：解压发布包（agenthelm-v<版本>.tar.gz）后运行"
  echo "      python3 <解压目录>/plugins/agent-pack/runtime/agenthelm.py runtime install --apply"
  echo "  · 已有运行时：export AGENTHELM_BIN=/path/to/agenthelm"
} >&2
exit 3
