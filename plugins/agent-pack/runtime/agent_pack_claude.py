"""Claude Code projection of a project's capabilities: a dedicated package, sync /
reconcile, preserved local credential overrides, and drift reports.

Claude Code has no Agent Plugins loader, so a package is projected onto what it
reads: ``~/.claude/skills/<skill>`` (links), ``~/.claude.json#mcpServers`` (user
scope) and the install record ``~/.claude/agent-pack/plugins/<package>`` (a link
inventory uses to report the version).

``sync`` / ``reconcile`` build a package filtered by the project's Claude policy
into ``~/.claude/agent-pack/packages/<package>/`` instead of borrowing another
client's package directory. ``~/.claude/agent-pack/state/<package>.json`` lists
the Skills and MCP servers this package put into Claude; only those are updated
or removed, entries the user added are never touched. Local credential values
(``headers`` / ``env``) on an MCP entry survive updates unless reset explicitly.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import agent_pack_config as core

OVERRIDE_CONTAINERS = ("headers", "env")
STATE_SCHEMA = 1


# ---------------------------------------------------------------- locations

def package_root(home: Path, name: str) -> Path:
    return home / ".claude" / "agent-pack" / "packages" / name


def record_path(home: Path, name: str) -> Path:
    return core._claude_plugin_records(home) / name


def state_path(home: Path, name: str) -> Path:
    return home / ".claude" / "agent-pack" / "state" / f"{name}.json"


def data_root(home: Path, name: str) -> Path:
    return home / ".claude" / "agent-pack" / "data" / name


# ---------------------------------------------------------------- local overrides

def _placeholder(value: Any) -> bool:
    return value in (None, "") or (isinstance(value, str) and "${" in value)


def merge_overrides(rendered: dict[str, Any], current: Any, *, reset: bool = False) -> tuple[dict[str, Any], list[str]]:
    """Keep the user's local credential values on top of the project's entry.

    A ``headers`` / ``env`` key keeps its local value when the project entry
    lacks it, leaves it as a placeholder, or the key is credential-like. Other
    differences follow the project. Returns the merged entry and the kept key
    names (never their values).
    """
    merged = json.loads(json.dumps(rendered))
    if reset or not isinstance(current, dict):
        return merged, []
    kept: list[str] = []
    for container in OVERRIDE_CONTAINERS:
        local = current.get(container)
        if not isinstance(local, dict):
            continue
        target = dict(merged.get(container) or {}) if isinstance(merged.get(container) or {}, dict) else {}
        for key, value in local.items():
            ours = target.get(key)
            credential = bool(core.SECRET_NAME_RE.search(str(key)))
            if ours != value and (key not in target or _placeholder(ours) or credential):
                target[key] = value
                kept.append(f"{container}.{key}")
        if target:
            merged[container] = target
    return merged, sorted(kept)


def classify_mcp(rendered: dict[str, Any], current: Any, *, reset: bool = False) -> tuple[str, dict[str, Any], list[str]]:
    """(state, entry to write, kept override keys); state is add / keep / keep-override / differ."""
    if current is None:
        return "add", json.loads(json.dumps(rendered)), []
    merged, kept = merge_overrides(rendered, current, reset=reset)
    if merged == current:
        return ("keep-override" if kept else "keep"), merged, kept
    return "differ", merged, kept


# ---------------------------------------------------------------- reading the client

def _read_state(home: Path, name: str) -> dict[str, Any]:
    path = state_path(home, name)
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = core._read_json(path)
    except core.AgentPackError:
        return {}
    return data if data.get("schema_version") == STATE_SCHEMA else {}


def _read_config(home: Path) -> dict[str, Any]:
    path = core._claude_user_config(home)
    if not path.exists():
        return {}
    if path.is_symlink():
        raise core.AgentPackError(f"refusing to edit symlinked Claude config: {path}")
    config = core._read_json(path)
    if not isinstance(config.get("mcpServers", {}), dict):
        raise core.AgentPackError(f"mcpServers must be an object: {path}")
    return config


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)


def _references(entry: Any, base: Path) -> bool:
    prefix = str(base) + os.sep
    return any(text == str(base) or prefix in text or text.startswith(prefix) for text in _strings(entry))


def _link_target(path: Path) -> Path | None:
    if not path.is_symlink():
        return None
    target = Path(os.readlink(path))
    return target if target.is_absolute() else (path.parent / target)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------- plan

def _render_package_mcps(package: Path, home: Path, name: str) -> dict[str, dict[str, Any]]:
    servers: dict[str, dict[str, Any]] = {}
    mcp_path = package / "mcp.json"
    if not mcp_path.is_file():
        return servers
    for server, raw in sorted(core._read_json(mcp_path).get("mcpServers", {}).items()):
        parsed = core._portable_mcp(server, raw)
        if not parsed.portable:
            raise core.AgentPackError(f"MCP {server} is not portable: " + "; ".join(parsed.issues))
        servers[server] = core._claude_mcp_config(parsed.config, record_path(home, name), data_root(home, name))
    return servers


def plan_project(project: Path, home: Path, *, built: Path, reset_overrides: bool = False,
                 retire_skills: Iterable[str] = (), retire_plugins: Iterable[str] = (),
                 retire_only: bool = False) -> dict[str, Any]:
    """Every change a Claude sync would make, without writing; conflicts are listed, not raised."""
    layout = core.load_project_layout(project)
    authority = core.inventory_project(layout.root)
    name = core._package_name(layout.root)
    package, record = package_root(home, name), record_path(home, name)
    state = _read_state(home, name)
    config = _read_config(home)
    existing = config.get("mcpServers", {})
    old_target = _link_target(record)
    legacy_root = old_target if old_target is not None and not _inside(old_target, package) else None

    desired_skills = {child.name for child in (built / "skills").iterdir()
                      if child.is_dir() and (child / "SKILL.md").is_file()} if (built / "skills").is_dir() else set()
    desired_mcps = _render_package_mcps(built, home, name)
    excluded = sorted(item.name for item in authority.mcps
                      if item.portable and not core._mcp_allowed(layout, item.name, "claude"))
    owned_mcps = set(state.get("mcps", {}))
    owned_mcps |= {server for server, entry in existing.items() if _references(entry, record)}
    owned_skills = set(state.get("skills", []))

    operations: list[dict[str, Any]] = []
    conflicts: list[str] = []
    overrides: list[dict[str, Any]] = []

    if not retire_only:
        if package.exists() and not package.is_symlink():
            same = core._tree_fingerprint(package) == core._tree_fingerprint(built)
            operations.append({"action": "keep-package" if same else "replace-package", "name": name, "to": str(package)})
        else:
            operations.append({"action": "install-package", "name": name, "to": str(package)})
        if old_target is not None and package.is_dir() and old_target.resolve() == package.resolve():
            operations.append({"action": "keep-record", "name": name, "to": str(record)})
        elif record.is_symlink() or record.exists():
            detail = {"from": str(old_target)} if old_target is not None else {}
            operations.append({"action": "relink-record", "name": name, "to": str(record), **detail})
        else:
            operations.append({"action": "link-record", "name": name, "to": str(record)})

        skills_root = home / ".claude" / "skills"
        for skill in sorted(desired_skills):
            path, target = skills_root / skill, package / "skills" / skill
            link = _link_target(path)
            ours = skill in owned_skills or (link is not None and (
                _inside(link, package) or (legacy_root is not None and _inside(link, legacy_root))))
            if not path.exists() and not path.is_symlink():
                action = "link-skill"
            elif link is not None and link == target:
                action = "keep-skill"
            elif ours:
                action = "relink-skill"
            else:
                action = "conflict-skill"
                conflicts.append(f"skill {skill}: {path}")
            operations.append({"action": action, "name": skill, "to": str(path)})
        for skill in sorted(owned_skills - desired_skills):
            path = skills_root / skill
            if path.is_symlink() or path.exists():
                operations.append({"action": "remove-skill", "name": skill, "from": str(path)})
        if legacy_root is not None and skills_root.is_dir():
            for path in sorted(skills_root.iterdir()):
                link = _link_target(path)
                if (path.name not in desired_skills and path.name not in owned_skills and link is not None
                        and _inside(link, legacy_root)):
                    operations.append({"action": "remove-skill", "name": path.name, "from": str(path)})

        for server, rendered in desired_mcps.items():
            state_name, entry, kept = classify_mcp(rendered, existing.get(server), reset=reset_overrides)
            if state_name == "differ":
                if server in owned_mcps:
                    state_name = "update"
                else:
                    state_name = "conflict"
                    conflicts.append(f"mcp {server}: {core._claude_user_config(home)}")
            if kept:
                overrides.append({"name": server, "keys": kept})
            operations.append({"action": f"{state_name}-mcp", "name": server, "keys": kept, "_entry": entry})
        for server in sorted(owned_mcps - set(desired_mcps)):
            if server in existing:
                operations.append({"action": "remove-mcp", "name": server,
                                   "reason": "excluded for claude" if server in excluded else "no longer in project"})

    client_inventory = core.inventory_client("claude", home)
    actual_skills = {item.name: Path(item.path) for item in client_inventory.skills}
    actual_plugins = {str(item.get("name")): Path(str(item.get("path")))
                      for item in client_inventory.plugins if item.get("name") and item.get("path")}
    for skill in sorted(set(retire_skills)):
        if skill in desired_skills:
            raise core.AgentPackError(f"cannot retire Skill provided by the project package: {skill}")
        path = actual_skills.get(skill, home / ".claude" / "skills" / skill)
        if not path.exists() and not path.is_symlink():
            raise core.AgentPackError(f"retire Skill not installed: {skill}")
        operations.append({"action": "archive-skill", "name": skill, "from": str(path)})
    for plugin in sorted(set(retire_plugins)):
        if plugin == name:
            raise core.AgentPackError(f"cannot retire active project package: {plugin}")
        path = actual_plugins.get(plugin)
        if path is None:
            raise core.AgentPackError(f"retire plugin not installed: {plugin}")
        operations.append({"action": "archive-plugin", "name": plugin, "from": str(path)})
    if retire_only and not (retire_skills or retire_plugins):
        raise core.AgentPackError("retire-only reconcile needs at least one --retire-skill or --retire-plugin")

    native = sorted(server for server in existing if server not in desired_mcps and server not in owned_mcps)
    return {"name": name, "layout": layout, "operations": operations, "conflicts": conflicts,
            "retire_only": retire_only,
            "overrides": overrides, "excluded": [server for server in excluded if server in existing],
            "native": native, "legacy_root": str(legacy_root) if legacy_root else None,
            "desired_skills": sorted(desired_skills), "desired_mcps": sorted(desired_mcps)}


def _public(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if not key.startswith("_") and value != []}
            | {"client": "claude"} for item in operations]


# ---------------------------------------------------------------- apply

def sync_project(project: Path, *, home: Path | None = None, apply: bool = False, replace: bool = False,
                 reset_overrides: bool = False, recovery_root: Path | None = None,
                 retire_skills: Iterable[str] = (), retire_plugins: Iterable[str] = (),
                 retire_only: bool = False) -> dict[str, Any]:
    home = (home or Path.home()).expanduser().resolve()
    retire_skills, retire_plugins = list(retire_skills), list(retire_plugins)
    with tempfile.TemporaryDirectory(prefix="agent-pack-claude-") as temporary:
        layout = core.load_project_layout(project)
        authority = core.inventory_project(layout.root)
        name = core._package_name(layout.root)
        built = core._build_package(authority, name, Path(temporary), "claude")
        plan = plan_project(project, home, built=built, reset_overrides=reset_overrides,
                            retire_skills=retire_skills, retire_plugins=retire_plugins, retire_only=retire_only)
        if plan["conflicts"] and not replace:
            raise core.AgentPackError("Claude client already has different content not installed by this package "
                                      "(use --replace; replaced items go to recovery): " + "; ".join(plan["conflicts"]))
        result: dict[str, Any] = {
            "ok": True, "applied": False, "client": "claude", "package": name,
            "operations": _public(plan["operations"]), "overrides": plan["overrides"],
            "excluded": plan["excluded"], "native": plan["native"],
            "notes": ["restart Claude Code to load changed Skills and MCP servers"],
        }
        if plan["legacy_root"]:
            result["migrated_from"] = plan["legacy_root"]
        changes = [item for item in plan["operations"] if not item["action"].startswith("keep-")]
        if not apply or not changes:
            result["applied"] = apply and not changes
            return result
        result["recovery"] = str(_apply(plan, built, home, recovery_root))
        result["applied"] = True
        return result


def _apply(plan: dict[str, Any], built: Path, home: Path, recovery_root: Path | None) -> Path:
    name = plan["name"]
    package, record = package_root(home, name), record_path(home, name)
    recovery_base = (recovery_root or home / ".agent-pack" / "recovery").expanduser().resolve()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    transaction = recovery_base / f"{stamp}-claude-{name}"
    transaction.mkdir(parents=True, mode=0o700)
    config_path = core._claude_user_config(home)
    config = _read_config(home)
    original_config = json.loads(json.dumps(config))
    config_written = False
    moved: list[tuple[Path, Path]] = []   # (archived, original)
    created: list[Path] = []
    actions = {(item["action"], item["name"]): item for item in plan["operations"]}

    def archive(path: Path, bucket: str, item_name: str) -> None:
        archived = transaction / bucket / item_name
        archived.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, archived)
        moved.append((archived, path))

    def link(path: Path, target: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path, target_is_directory=True)
        created.append(path)

    try:
        if ("install-package", name) in actions or ("replace-package", name) in actions:
            package.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".agent-pack-", dir=package.parent))
            try:
                staged = staging / name
                shutil.copytree(built, staged, symlinks=False)
                if package.exists():
                    archive(package, "replaced-package", name)
                os.replace(staged, package)
                created.append(package)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        if ("relink-record", name) in actions or ("link-record", name) in actions:
            if record.is_symlink() or record.exists():
                previous = _link_target(record)
                core._write_json_atomic(transaction / "record.json",
                                        {"record": str(record), "target": str(previous) if previous else None}, mode=0o600)
                archive(record, "replaced-records", name)
            link(record, package)
        for item in plan["operations"]:
            action, item_name = item["action"], item["name"]
            if action in ("relink-skill", "conflict-skill", "remove-skill", "archive-skill"):
                path = Path(item.get("to") or item["from"])
                if path.is_symlink() or path.exists():
                    archive(path, "replaced-skills" if action != "archive-skill" else "skills", item_name)
            if action in ("link-skill", "relink-skill", "conflict-skill"):
                link(Path(item["to"]), package / "skills" / item_name)
            if action == "archive-plugin":
                archive(Path(item["from"]), "plugins", item_name)
        servers = config.setdefault("mcpServers", {})
        replaced: dict[str, Any] = {}
        mcp_state: dict[str, str] = {}
        for item in plan["operations"]:
            action, server = item["action"], item["name"]
            if action in ("add-mcp", "update-mcp", "conflict-mcp", "keep-mcp", "keep-override-mcp"):
                if action in ("update-mcp", "conflict-mcp") and server in servers:
                    replaced[server] = servers[server]
                servers[server] = item["_entry"]
                mcp_state[server] = hashlib.sha256(
                    json.dumps(item["_entry"], sort_keys=True).encode("utf-8")).hexdigest()
            elif action == "remove-mcp":
                replaced[server] = servers.pop(server)
        if replaced:
            core._write_json_atomic(transaction / "replaced-mcp.json", {"mcpServers": replaced}, mode=0o600)
        if config != original_config:
            mode = stat.S_IMODE(config_path.stat().st_mode) if config_path.exists() else 0o600
            core._write_json_atomic(config_path, config, mode=mode)
            config_written = True
        if not plan["retire_only"]:
            core._write_json_atomic(state_path(home, name), {
                "schema_version": STATE_SCHEMA, "package": name, "skills": plan["desired_skills"],
                "mcps": mcp_state, "updated_at": stamp}, mode=0o600)
    except Exception as exc:
        if config_written:
            core._write_json_atomic(config_path, original_config, mode=0o600)
        for path in reversed(created):
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        for archived, original in reversed(moved):
            if (archived.exists() or archived.is_symlink()) and not (original.exists() or original.is_symlink()):
                original.parent.mkdir(parents=True, exist_ok=True)
                os.replace(archived, original)
        raise core.AgentPackError(f"claude sync rolled back: {exc}") from exc
    return transaction


# ---------------------------------------------------------------- drift (doctor)

def drift(project: Path, home: Path | None = None) -> dict[str, Any]:
    home = (home or Path.home()).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="agent-pack-claude-") as temporary:
        layout = core.load_project_layout(project)
        name = core._package_name(layout.root)
        built = core._build_package(core.inventory_project(layout.root), name, Path(temporary), "claude")
        plan = plan_project(project, home, built=built)
    report: dict[str, Any] = {"client": "claude", "package": name, "missing": [], "different": [],
                              "overrides": plan["overrides"], "stale": [], "native": plan["native"],
                              "excluded": plan["excluded"], "dangling": [], "coupled": plan["legacy_root"]}
    for item in plan["operations"]:
        action, item_name = item["action"], item["name"]
        if action in ("install-package", "link-record", "link-skill", "add-mcp"):
            report["missing"].append(f"{action.split('-')[1]} {item_name}")
        elif action in ("replace-package", "relink-skill", "conflict-skill", "update-mcp", "conflict-mcp"):
            report["different"].append(f"{action.split('-')[-1]} {item_name}")
        elif action in ("remove-skill", "remove-mcp"):
            report["stale"].append(f"{action.split('-')[1]} {item_name}")
    skills_root = home / ".claude" / "skills"
    if skills_root.is_dir():
        report["dangling"] += [f"skill {path.name}" for path in sorted(skills_root.iterdir())
                               if path.is_symlink() and not path.exists()]
    record = record_path(home, name)
    config = _read_config(home).get("mcpServers", {})
    for server, entry in sorted(config.items()):
        for text in _strings(entry):
            if text.startswith(str(record) + os.sep) and not Path(text).exists():
                report["dangling"].append(f"mcp {server}: {text}")
                break
    report["in_sync"] = not (report["missing"] or report["different"] or report["stale"]
                             or report["dangling"] or report["coupled"])
    return report
