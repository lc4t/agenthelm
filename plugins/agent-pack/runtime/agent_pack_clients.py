#!/usr/bin/env python3
"""Declarative client-driver registry (#134 Phase 1).

A *driver* describes how a client is recognised and how it takes plugins, as data: detection evidence (executable
names, configuration directories), the distribution driver type (``marketplace-cli`` | ``directory-dropin`` |
``none``), where Skills and MCP servers are stored, and capability flags. Code asks the registry (``default_ids``,
``supports``, ``detect``) instead of listing client names.

User declarations in ``~/.config/agenthelm/clients.d/*.json`` are read and validated only. They never run a command,
never write a file and never enter the ``--client`` set in this version; an invalid one is a warning and is ignored.
Standard library only, so every entry point can import it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from i18n_agent_pack import _

DISTRIBUTIONS = ("marketplace-cli", "directory-dropin", "none")
SCOPES = ("user", "local", "project")
FORMATS = ("json", "toml", "dir")
CAPABILITIES = ("default_set", "read_only", "project_mcp_write")
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
DECLARATION_KEYS = {"schema_version", "id", "display", "detect", "distribution", "stores", "capabilities"}
MAX_DECLARATION_BYTES = 64 * 1024


def _store(scope: str, path: str, fmt: str) -> dict:
    return {"scope": scope, "path": path, "format": fmt}


def _driver(id_: str, display: str, executables: list[str], dirs: list[dict], distribution: str,
            skills: list[dict], mcp: list[dict], *, default: bool = True, read_only: bool = False,
            project_mcp: bool = False) -> dict:
    return {
        "schema_version": 1, "id": id_, "display": display,
        "detect": {"executables": executables, "dirs": dirs},
        "distribution": distribution,
        "stores": {"skills": skills, "mcp": mcp},
        "capabilities": {"default_set": default, "read_only": read_only, "project_mcp_write": project_mcp},
    }


# Order is the order the previous hard-coded tuple had; claude-desktop follows the default set.
# ``project_mcp_write`` mirrors what agenthelm writes today (Claude only); W-a reads it as the single source.
BUILTIN: tuple[dict, ...] = (
    _driver("claude", "Claude Code", ["claude"], [{"path": "~/.claude", "env": "CLAUDE_CONFIG_DIR"}], "marketplace-cli",
            [_store("user", "~/.claude/skills", "dir"), _store("project", "<project>/.claude/skills", "dir")],
            [_store("user", "~/.claude.json", "json"), _store("local", "~/.claude.json#projects", "json"),
             _store("project", "<project>/.mcp.json", "json")], project_mcp=True),
    _driver("codex", "Codex", ["codex"], [{"path": "~/.codex", "env": "CODEX_HOME"}], "marketplace-cli",
            [_store("user", "~/.codex/skills", "dir"), _store("user", "~/.agents/skills", "dir"),
             _store("project", "<project>/.agents/skills", "dir")],
            [_store("user", "~/.codex/config.toml", "toml"), _store("project", "<project>/.codex/config.toml", "toml")]),
    _driver("cursor", "Cursor", [], [{"path": "~/.cursor"}], "directory-dropin",
            [_store("user", "~/.cursor/skills", "dir"), _store("user", "~/.agents/skills", "dir"),
             _store("project", "<project>/.cursor/skills", "dir"), _store("project", "<project>/.agents/skills", "dir")],
            [_store("user", "~/.cursor/mcp.json", "json"), _store("project", "<project>/.cursor/mcp.json", "json")]),
    _driver("claude-desktop", "Claude Desktop", [],
            [{"path": "~/Library/Application Support/Claude"}, {"path": "~/.config/Claude"}], "none",
            [], [_store("user", "<per-platform>/claude_desktop_config.json", "json")], default=False, read_only=True),
)
BUILTIN_IDS = tuple(item["id"] for item in BUILTIN)


def default_ids() -> tuple[str, ...]:
    return tuple(item["id"] for item in BUILTIN if item["capabilities"]["default_set"])


def all_ids() -> tuple[str, ...]:
    return BUILTIN_IDS


def get(client: str) -> dict | None:
    return next((item for item in BUILTIN if item["id"] == client), None)


def display_names() -> dict[str, str]:
    """Display names of the default set (what the old ``CLIENT_NAMES`` held)."""
    return {item["id"]: item["display"] for item in BUILTIN if item["capabilities"]["default_set"]}


def supports(client: str, capability: str) -> bool:
    """Capability flag of a built-in driver; an unknown client or flag is ``False``."""
    driver = get(client)
    return bool(driver and driver["capabilities"].get(capability))


# ---------------------------------------------------------------- detection (facts, not names)

def _expand(template: str, home: Path) -> Path:
    return home / template[2:] if template.startswith("~/") else Path(template)


def detect(driver: dict, home: Path, getenv: Callable[[str], str | None],
           find_cli: Callable[[str], str | None]) -> dict:
    """``{detected, evidence}``: the first matching fact (executable, then directory), else not detected."""
    for name in driver["detect"].get("executables", []):
        found = find_cli(name)
        if found:
            return {"detected": True, "evidence": {"kind": "executable", "name": name}}
    for item in driver["detect"].get("dirs", []):
        override = getenv(item["env"]) if item.get("env") else None
        path = Path(override) if override else _expand(item["path"], home)
        if path.is_dir():
            return {"detected": True, "evidence": {"kind": "directory", "name": item["path"]}}
    return {"detected": False, "evidence": None}


# ---------------------------------------------------------------- user declarations (validate only)

def declaration_dir(home: Path, getenv: Callable[[str], str | None]) -> Path:
    base = getenv("XDG_CONFIG_HOME")
    return (Path(base) if base else home / ".config") / "agenthelm" / "clients.d"


def _is_text(value: Any, limit: int = 64) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value)


def validate_declaration(data: Any) -> str | None:
    """The reason a declaration is invalid (``None`` when valid). Only data fields are accepted."""
    if not isinstance(data, dict):
        return _("顶层必须是对象")
    extra = sorted(set(data) - DECLARATION_KEYS)
    if extra:
        return _("含不支持的字段：{0}").format(_("、").join(map(str, extra)))
    if data.get("schema_version") != 1:
        return _("schema_version 必须为 1")
    if not isinstance(data.get("id"), str) or not ID_PATTERN.match(data["id"]):
        return _("id 必须匹配 ^[a-z][a-z0-9-]{0,31}$")
    if data["id"] in BUILTIN_IDS or data["id"] == "all":
        return _("id 与内置客户端冲突：{0}").format(data["id"])
    if not _is_text(data.get("display")):
        return _("display 必须是 1–64 字符的单行文本")
    detect_part = data.get("detect")
    if not isinstance(detect_part, dict) or set(detect_part) - {"executables", "dirs"}:
        return _("detect 必须是只含 executables / dirs 的对象")
    executables = detect_part.get("executables", [])
    if not isinstance(executables, list) or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", x)
                                                    for x in executables):
        return _("detect.executables 必须是可执行文件名列表（不含路径与参数）")
    dirs = detect_part.get("dirs", [])
    if not isinstance(dirs, list) or not all(isinstance(x, str) and x.startswith("~/") and ".." not in x.split("/")
                                             and _is_text(x, 200) for x in dirs):
        return _("detect.dirs 必须是 ~/ 开头、不含 .. 的路径列表")
    if not executables and not dirs:
        return _("detect 至少要有一项检测依据")
    if data.get("distribution") not in DISTRIBUTIONS:
        return _("distribution 必须是：{0}").format(_("、").join(DISTRIBUTIONS))
    stores = data.get("stores", {})
    if not isinstance(stores, dict) or set(stores) - {"skills", "mcp"}:
        return _("stores 必须是只含 skills / mcp 的对象")
    for kind in stores:
        entries = stores[kind]
        if not isinstance(entries, list) or not all(
                isinstance(e, dict) and set(e) == {"scope", "path", "format"} and e["scope"] in SCOPES
                and e["format"] in FORMATS and _is_text(e["path"], 200) for e in entries):
            return _("stores.{0} 的每一项必须是 scope / path / format（取值见文档）").format(kind)
    capabilities = data.get("capabilities", {})
    if not isinstance(capabilities, dict) or not all(k in CAPABILITIES and isinstance(v, bool)
                                                     for k, v in capabilities.items()):
        return _("capabilities 只能含布尔值的：{0}").format(_("、").join(CAPABILITIES))
    return None


def load_declarations(home: Path, getenv: Callable[[str], str | None]) -> tuple[list[dict], list[dict]]:
    """(valid declarations, warnings). Reads ``clients.d/*.json`` only; nothing in them is ever executed."""
    folder = declaration_dir(home, getenv)
    declarations: list[dict] = []
    warnings: list[dict] = []
    if not folder.is_dir():
        return declarations, warnings
    seen: set[str] = set()
    for path in sorted(folder.glob("*.json")):
        label = path.name
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DECLARATION_BYTES:
                raise ValueError(_("不是普通文件或超过 {0} 字节").format(MAX_DECLARATION_BYTES))
            data = json.loads(path.read_text(encoding="utf-8"))
            reason = validate_declaration(data)
            if reason is None and data["id"] in seen:
                reason = _("id 重复：{0}").format(data["id"])
        except (OSError, ValueError, UnicodeDecodeError) as error:
            reason = _("无法读取：{0}").format(error)
        if reason:
            warnings.append({"file": label, "reason": reason})
            continue
        seen.add(data["id"])
        declarations.append(data)
    return declarations, warnings


# ---------------------------------------------------------------- the ``agenthelm clients`` report

def report(home: Path, getenv: Callable[[str], str | None], find_cli: Callable[[str], str | None]) -> dict:
    clients = []
    for item in BUILTIN:
        found = detect(item, home, getenv, find_cli)
        clients.append({"id": item["id"], "display": item["display"], "source": "builtin", **found,
                        "distribution": item["distribution"], "capabilities": dict(item["capabilities"]),
                        "install_supported": item["distribution"] != "none"})
    declared, warnings = load_declarations(home, getenv)
    for item in declared:
        probe = {"detect": {"executables": item["detect"].get("executables", []),
                            "dirs": [{"path": path} for path in item["detect"].get("dirs", [])]}}
        found = detect(probe, home, getenv, find_cli)  # looks up executables and directories; runs nothing
        clients.append({"id": item["id"], "display": item["display"], "source": "declared", **found,
                        "distribution": item["distribution"],
                        "capabilities": {key: bool(item.get("capabilities", {}).get(key, False)) for key in CAPABILITIES},
                        "install_supported": False})
    return {"schema_version": 1, "clients": clients, "warnings": warnings}


def render_text(data: dict) -> str:
    lines = [_("客户端驱动（内置与用户声明）：")]
    for item in data["clients"]:
        evidence = item["evidence"]
        seen = (_("已检测到（{0}：{1}）").format(_("可执行文件") if evidence["kind"] == "executable" else _("目录"),
                                            evidence["name"]) if item["detected"] else _("未检测到"))
        if item["source"] == "declared":
            seen = _("已声明，本版未支持安装")
        flags = [key for key, value in item["capabilities"].items() if value]
        lines.append("  " + _("{0}（{1}）：{2}；分发 {3}；能力 {4}").format(
            item["id"], item["display"], seen, item["distribution"], ", ".join(flags) or "-"))
    for warning in data["warnings"]:
        lines.append(_("警告：忽略声明 {0}：{1}").format(warning["file"], warning["reason"]))
    return "\n".join(lines) + "\n"
