"""Scope view of a Claude sync (#67): which entries are user-level, which the project declares as project-level,
what a narrowing would take away from other projects, and how the preview groups them.

Pure helpers over plain data so the preview and ``--apply`` read one plan: ``agent_pack_claude.plan_project``
decides, this module only describes. Nothing here writes. Output never carries configuration values.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sys

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))
from i18n_agent_pack import _, N_  # noqa: E402

SCOPE_FILTERS = ("user", "project", "all")
STATE_SCHEMA = 1

# store ids (the ``store`` field of an operation); a later store is added only with the code that writes it
USER_SKILLS, USER_MCP = "claude-user-skills", "claude-user"
PROJECT_SKILLS, LOCAL_MCP, FILE_MCP = "project-skills", "claude-local", "project-file"

STORE_TITLE = {
    USER_SKILLS: N_("用户级 · 对所有项目生效"),
    USER_MCP: N_("用户级 · 对所有项目生效"),
    PROJECT_SKILLS: N_("项目级 · 仅此项目 · .claude/skills"),
    LOCAL_MCP: N_("项目级 · 仅此项目 · local（~/.claude.json，不进版本库）"),
    FILE_MCP: N_("项目级 · 仅此项目 · 项目文件 .mcp.json"),
}
STORE_ORDER = (USER_SKILLS, USER_MCP, LOCAL_MCP, FILE_MCP, PROJECT_SKILLS)


def normalize_scope(scope: str | None) -> str:
    value = scope or "all"
    if value not in SCOPE_FILTERS:
        raise ValueError(f"unknown --scope: {value} (choose from {', '.join(SCOPE_FILTERS)})")
    return value


def other_projects(home: Path, package: str) -> list[dict[str, Any]]:
    """Ownership records of the other projects synced on this machine (``~/.claude/agent-pack/state/*.json``).

    Only projects that were synced here are visible; a project never synced on this machine cannot be counted.
    """
    directory = home / ".claude" / "agent-pack" / "state"
    found: list[dict[str, Any]] = []
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.json")):
        if path.stem == package or path.is_symlink():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("schema_version") != STATE_SCHEMA:
            continue
        skills = data.get("skills")
        mcps = data.get("mcps")
        found.append({"package": path.stem,
                      "skills": {name for name in skills if isinstance(name, str)} if isinstance(skills, list) else set(),
                      "mcps": {str(name) for name in mcps} if isinstance(mcps, dict) else set()})
    return found


def narrowing_entry(kind: str, name: str, from_store: str, to_store: str, others: list[dict[str, Any]]) -> dict[str, Any]:
    key = "skills" if kind == "skill" else "mcps"
    return {"kind": kind, "name": name, "from_store": from_store, "to_store": to_store,
            "other_projects": len(others),
            "also_declared_by": sorted(item["package"] for item in others if name in item[key]),
            "counted_from": "state", "status": "not-applied"}


def narrowing_note() -> str:
    return _("只统计在本机同步过的项目（~/.claude/agent-pack/state），未同步过的项目看不到，也无法计入")


# ---------------------------------------------------------------- preview text

def _line(operation: dict[str, Any]) -> str:
    target = operation.get("to") or operation.get("from", "")
    return f"- {operation['action']}: {operation.get('name', '')} -> {target}"


def grouped_lines(value: dict[str, Any]) -> list[str]:
    """Operations of a scope-aware preview, one heading per store (user level first)."""
    lines: list[str] = []
    operations = value.get("operations", [])
    unscoped = [item for item in operations if "store" not in item]
    for item in unscoped:
        lines.append(_line(item))
    for store in STORE_ORDER:
        group = [item for item in operations if item.get("store") == store]
        if not group:
            continue
        lines.append(f"[{_(STORE_TITLE[store])}]")
        lines.extend("  " + _line(item) for item in group)
    return lines


def narrowing_lines(value: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for item in value.get("narrowing", []):
        known = item["other_projects"]
        lines.append("~ " + _("收窄：{n} 个项目将不再看到 {kind} {name}").format(
            n=known, kind=item["kind"], name=item["name"]))
        if item["also_declared_by"]:
            lines.append("  " + _("其中也声明了它的项目记录：{names}").format(names=", ".join(item["also_declared_by"])))
        if item.get("status") == "not-applied":
            lines.append("  " + _("本次不改动用户级原条目（项目级写入尚未提供或未选择）"))
    return lines


def skipped_lines(value: dict[str, Any]) -> list[str]:
    return ["~ " + _("跳过 {kind} {name}（{client}）：{reason}").format(
        kind=item["kind"], name=item["name"], client=item["client"], reason=_(REASONS.get(item["reason"], item["reason"])))
        for item in value.get("skipped", [])]


REASONS = {
    "project-write-unavailable": N_("项目级写入尚未提供；不会回退写入用户级"),
    "project-native": N_("Skill 由项目 .agents/skills 原生加载，不写用户级包"),
    "client-project-unsupported": N_("该客户端的项目级 MCP 写入尚未提供；不会回退写入用户级"),
}


def skipped_warning(item: dict[str, str]) -> str:
    """The warning for one skipped project-declared entry (a client whose project level is not written yet)."""
    return _("{kind} {name} 声明为项目级，对 {client} 跳过：{reason}").format(
        kind=item["kind"], name=item["name"], client=item["client"], reason=_(REASONS.get(item["reason"], item["reason"])))


def undeclared_warning(names: list[str]) -> str:
    return _("为项目里不存在的条目声明了作用域：{names}").format(names=", ".join(names))
