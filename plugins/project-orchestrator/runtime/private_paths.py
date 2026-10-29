"""私有目录：Profile 的 ``privacy.forbidden_default_reads`` 声明，缺省约定为项目根目录的 ``private/``。

约定（只有两条，其余照常）：
- 读取前须经用户确认——Agent 默认不读，读之前向用户列出要读的路径；
- 内容默认不进入 git——目录里放一个 ``.gitignore``（``*`` 加 ``!.gitignore``），随项目走，不改项目根的 .gitignore。

用到私有目录的 Case 标为 ``sensitivity: private``。本模块只依赖标准库，不依赖 case.py / sop.py，三者可以互相调用。
资料索引（find）不做特殊处理：``private/`` 不是资料根时本来就不会被扫描。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

PROFILE = Path(".agents/moe.sakanano.agent-pack/project.json")
CONVENTION = "private"
IGNORE_FILE = ".gitignore"
# 与 agents-init 新项目生成的 private/.gitignore 相同（tests/test_private_paths_sync.py 核对两处一致）。
IGNORE_TEXT = "# agenthelm: 本目录内容默认不进入 git；读取前须经用户确认。\n*\n!.gitignore\n"
GIT_TIMEOUT = 10


def clean(value: Any) -> str | None:
    """Profile 里的一项规范成项目内相对路径（不带首尾斜杠）；绝对路径、~、含 .. 的返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith(("~", "/")) or re.match(r"^[A-Za-z]:[\\/]", text):
        return None
    normal = os.path.normpath(text)
    if normal in (".", "") or normal.startswith("..") or Path(normal).is_absolute():
        return None
    return Path(normal).as_posix()


def declared(project: Path) -> list[str]:
    """项目的私有路径：Profile 声明的 + 约定的 private/（目录存在时）。"""
    found: list[str] = []
    try:
        data = json.loads((project / PROFILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    privacy = data.get("privacy") if isinstance(data, dict) else None
    items = privacy.get("forbidden_default_reads") if isinstance(privacy, dict) else None
    for value in items if isinstance(items, list) else []:
        path = clean(value)
        if path and path not in found:
            found.append(path)
    convention = project / CONVENTION
    if CONVENTION not in found and convention.is_dir() and not convention.is_symlink():
        found.append(CONVENTION)
    return found


def relative(project: Path, value: str | Path) -> str | None:
    """项目内的路径（相对或绝对）→ 项目相对路径；在项目外返回 None。"""
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        try:
            return Path(os.path.normpath(path)).relative_to(Path(os.path.normpath(project.resolve()))).as_posix()
        except ValueError:
            try:
                return path.resolve().relative_to(project.resolve()).as_posix()
            except ValueError:
                return None
    return clean(str(value))


def is_private(project: Path, value: str | Path, dirs: list[str] | None = None) -> bool:
    rel = relative(project, value)
    if rel is None:
        return False
    return any(rel == item or rel.startswith(item + "/") for item in (declared(project) if dirs is None else dirs))


def mentions(text: str, dirs: list[str]) -> list[str]:
    """文本里把哪些私有路径当作路径用到了（``private/…``、``./private/…``）。目录要带斜杠；声明的是文件名（带扩展名）时按文件名匹配。"""
    found = []
    for item in dirs:
        is_file = "." in item.rsplit("/", 1)[-1]
        tail = r"(?:/|(?![\w\-]))" if is_file else "/"
        if re.search(r"(?<![\w.\-/])(?:\./)?" + re.escape(item) + tail, text):
            found.append(item)
    return found


def git_state(project: Path, directory: str) -> dict:
    """私有路径是否被 git 忽略、是否已有文件被跟踪。不在 git 仓库或没有 git 时 ignored 为 None。"""
    result: dict[str, Any] = {"dir": directory, "exists": (project / directory).exists(), "ignored": None, "tracked": []}
    probe = f"{directory}/.agenthelm-probe"
    try:
        checked = subprocess.run(["git", "-C", str(project), "check-ignore", "-q", "--", probe],
                                 capture_output=True, check=False, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return result
    if checked.returncode not in (0, 1):
        return result
    result["ignored"] = checked.returncode == 0
    try:
        listed = subprocess.run(["git", "-C", str(project), "ls-files", "-z", "--", directory],
                                capture_output=True, check=False, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return result
    names = [item for item in listed.stdout.decode("utf-8", errors="replace").split("\0") if item]
    result["tracked"] = [name for name in names if name != f"{directory}/{IGNORE_FILE}"]
    return result


def plan_ignore(project: Path, directory: str) -> list[dict]:
    """需要写入的 .gitignore（只在它不存在时创建；已有的从不修改）。"""
    target = project / directory
    if target.exists() and not target.is_dir():
        return []
    path = target / IGNORE_FILE
    return [] if path.exists() else [{"path": path, "content": IGNORE_TEXT, "expect": None}]
