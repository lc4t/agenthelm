"""项目入口的公共约定：错误、接口版本、状态分组与 Profile 中的 ``entry`` 段。

演示与真实两种数据来源（project_demo / project_real）都只依赖这里，界面层（project_cli /
project_menu）不关心数据从哪来。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from i18n_orchestrator import _  # noqa: E402
from i18n_orchestrator import _  # noqa: E402

# 运行时支持的项目入口接口版本范围；项目在 Profile 的 entry.api 声明自己需要的版本。
RUNTIME_API = (1, 1)
# 当前启动器模板的接口版本；旧启动器仍可用，只提示更新。
LAUNCHER_API = 1
PROFILE = Path(".agents/moe.sakanano.agent-pack/project.json")
DEFAULT_TOOLS = Path(".agents/moe.sakanano.agent-pack/tools.json")
DEFAULT_LAUNCHER = "menu"
LAUNCHER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# 默认 Case 状态即通用分组；项目可在 entry.items.status_map 把自己的状态名映射过来。
GROUP_KEYS = ("active", "waiting", "captured", "suspended", "done", "abandoned")


class EntryError(Exception):
    """预期内的失败：``code`` 决定退出码，``next_steps`` 告诉人下一步。"""

    def __init__(self, message: str, code: str = "usage", next_steps: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.next_steps = next_steps or []


def find_profile_root(start: Path | None, explicit: bool) -> Path:
    base = (start or Path.cwd()).expanduser().resolve()
    candidates = [base] if explicit else [base, *base.parents]
    for candidate in candidates:
        if (candidate / PROFILE).is_file():
            return candidate
    where = str(base) if explicit else _("{0} 及其上级目录").format(base)
    raise EntryError(_("{0}里没有 agenthelm 项目（缺少 {1}）").format(where, PROFILE), "not-a-project",
                     [_("到项目目录里运行，或用 --project <目录> 指定"),
                      _("新项目：用 agents-init 初始化（会生成项目入口）"),
                      _("只想看看效果：agenthelm project --demo study")])


def read_profile(root: Path) -> dict:
    path = root / PROFILE
    if path.is_symlink():
        raise EntryError(_("拒绝符号链接：{0}").format(PROFILE), "profile-invalid")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise EntryError(_("Project Profile 无法解析：{0}").format(exc), "profile-invalid",
                         [_("检查 {0} 是否为合法 JSON").format(PROFILE)]) from exc
    if not isinstance(data, dict):
        raise EntryError(_("Project Profile 根节点必须是对象"), "profile-invalid")
    return data


def inside(root: Path, value: Any, label: str) -> Path:
    """A project-relative path that stays inside the project and passes no symlink."""
    if not isinstance(value, str) or not value:
        raise EntryError(_("{0} 必须是项目内相对路径").format(label), "profile-invalid")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise EntryError(_("{0} 必须是项目内相对路径：{1}").format(label, value), "profile-invalid")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise EntryError(_("{0} 不能经过符号链接：{1}").format(label, value), "profile-invalid")
    return root / relative


def entry_section(profile: dict) -> dict:
    """The optional ``entry`` block with defaults; invalid values fail loudly instead of being guessed."""
    entry = profile.get("entry")
    if entry is None:
        return {"declared": False, "api": 1, "launcher": None, "about": "", "guide": None, "tools": None,
                "items": None, "where": [], "start": []}
    if not isinstance(entry, dict):
        raise EntryError(_("Profile 的 entry 必须是对象"), "profile-invalid")
    api = entry.get("api", 1)
    if not isinstance(api, int) or isinstance(api, bool) or api < 1:
        raise EntryError(_("entry.api 必须是正整数"), "profile-invalid")
    launcher = entry.get("launcher")
    if launcher is not None and (not isinstance(launcher, str) or not LAUNCHER_RE.fullmatch(launcher)):
        raise EntryError(_("entry.launcher 只能是根目录下的文件名（字母、数字、. _ -）"), "profile-invalid")
    items = entry.get("items")
    if items is not None:
        command = items.get("command") if isinstance(items, dict) else None
        if not (isinstance(command, list) and command and all(isinstance(part, str) and part for part in command)):
            raise EntryError(_("entry.items.command 必须是非空的参数列表（不经过 shell）"), "profile-invalid")
        status_map = items.get("status_map") or {}
        if not isinstance(status_map, dict) or not all(value in GROUP_KEYS for value in status_map.values()):
            raise EntryError(_("entry.items.status_map 的值只能是 ") + " / ".join(GROUP_KEYS), "profile-invalid")
    where = entry.get("where") or []
    start = entry.get("start") or []
    if not (isinstance(where, list) and all(isinstance(item, dict) and isinstance(item.get("label"), str)
                                            and isinstance(item.get("path"), str) for item in where)):
        raise EntryError(_("entry.where 必须是 {label, path} 列表"), "profile-invalid")
    if not (isinstance(start, list) and all(isinstance(item, str) for item in start)):
        raise EntryError(_("entry.start 必须是字符串列表"), "profile-invalid")
    return {"declared": True, "api": api, "launcher": launcher, "about": str(entry.get("about") or ""),
            "guide": entry.get("guide"), "tools": entry.get("tools"), "items": items,
            "where": where, "start": start}


SOP_ERROR_CODES = {"usage": "usage", "invalid": "usage", "not-found": "not-found", "ambiguous": "ambiguous",
                   "conflict": "conflict", "unavailable": "unavailable", "failed": "failed"}


def sop_call(argv: list[str], root: Path) -> tuple[int, dict, list[str]]:
    """在项目 ``root`` 上运行 sop.py 的子命令；真实与演示两种来源共用，错误转成入口的 EntryError。"""
    import sop as sop_tool  # noqa: PLC0415 - 只有用到 SOP 时才加载

    try:
        return sop_tool.execute([*argv, "--project", str(root)])
    except sop_tool.SopError as exc:
        raise EntryError(str(exc), SOP_ERROR_CODES.get(exc.code, "usage"), exc.next_steps) from exc
    except SystemExit as exc:
        raise EntryError(_("命令参数不正确，用 sop <子命令> --help 查看写法"), "usage") from exc
