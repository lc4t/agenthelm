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
import agent_pack_scope as scoping

OVERRIDE_CONTAINERS = ("headers", "env")
STATE_SCHEMA = 1


# ---------------------------------------------------------------- locations

def package_root(home: Path, name: str) -> Path:
    return home / ".claude" / "agent-pack" / "packages" / name


def record_path(home: Path, name: str) -> Path:
    return core._claude_plugin_records(home) / name


SYNC_TYPES = ("plugins", "skills", "mcp")   # what ``--only`` can select; the default is all of them


def normalize_only(only: Iterable[str] | None) -> frozenset[str]:
    """``--only`` values (repeatable or comma separated) as a set of types; empty or None means every type."""
    types: set[str] = set()
    for value in only or ():
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            if part not in SYNC_TYPES:
                raise core.AgentPackError(f"unknown --only type: {part} (choose from {', '.join(SYNC_TYPES)})")
            types.add(part)
    return frozenset(types or SYNC_TYPES)


def _compose_package(existing: Path, built: Path, only: frozenset[str], scratch: Path) -> Path:
    """The package tree a partial sync installs: what is already installed, with only the selected parts replaced.

    ``skills`` owns ``skills/``; ``mcp`` owns ``mcp.json`` and ``runtime/`` (the launcher and its configs);
    ``plugins`` owns everything else (the manifest). Unselected parts keep exactly what is installed.
    """
    composed = scratch / "composed" / built.name
    composed.parent.mkdir(parents=True, exist_ok=True)
    if existing.is_dir() and not existing.is_symlink():
        shutil.copytree(existing, composed, symlinks=False)
    else:
        composed.mkdir()
    parts = {"skills": ("skills",), "mcp": ("mcp.json", "runtime")}
    owned = {name for names in parts.values() for name in names}

    def replace(name: str) -> None:
        target = composed / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        if (built / name).is_dir():
            shutil.copytree(built / name, target, symlinks=False)
        elif (built / name).is_file():
            shutil.copy2(built / name, target)

    for kind, names in parts.items():
        if kind in only:
            for name in names:
                replace(name)
    if "plugins" in only or not existing.is_dir():
        for child in sorted(built.iterdir()):
            if child.name not in owned:
                replace(child.name)
    return composed


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


# ---------------------------------------------------------------- project-level stores

PROJECT_STORES = ("local", "file")
LOCAL_UNRECOGNIZED = "local-structure-unrecognized"
PROJECT_ACTIONS = ("add-project-mcp", "update-project-mcp", "conflict-project-mcp", "keep-project-mcp",
                   "keep-override-project-mcp")


def normalize_project_store(value: str | None) -> str:
    store = value or "local"
    if store not in PROJECT_STORES:
        raise core.AgentPackError(f"unknown --project-store: {store} (choose from {', '.join(PROJECT_STORES)})")
    return store


def local_section(config: dict[str, Any], root: Path) -> tuple[str | None, dict[str, Any], str | None]:
    """(key, servers, problem) of ``~/.claude.json`` ``projects[<absolute path>].mcpServers`` for this project.

    The documentation says only that the entry lives "under that project's path"; any shape other than
    ``projects`` -> path -> ``mcpServers`` objects is a ``problem`` and nothing is written there.
    """
    projects = config.get("projects")
    if projects is None:
        return str(root), {}, None
    if not isinstance(projects, dict):
        return None, {}, LOCAL_UNRECOGNIZED
    for key in dict.fromkeys((str(root), str(root.resolve()))):
        if key in projects:
            section = projects[key]
            if not isinstance(section, dict):
                return None, {}, LOCAL_UNRECOGNIZED
            servers = section.get("mcpServers")
            if servers is None:
                return key, {}, None
            return (key, servers, None) if isinstance(servers, dict) else (None, {}, LOCAL_UNRECOGNIZED)
    return str(root.resolve()), {}, None


def _entry_strings(entry: dict[str, Any]) -> Iterable[tuple[str, str]]:
    """(field path, text) of the command, arguments, working directory and environment values: field paths only,
    never the text, are ever reported."""
    for field in ("command", "cwd"):
        if isinstance(entry.get(field), str):
            yield field, entry[field]
    for index, text in enumerate(entry.get("args") or []):
        if isinstance(text, str):
            yield f"args[{index}]", text
    for container in ("env", "headers"):
        values = entry.get(container)
        for key, text in (values.items() if isinstance(values, dict) else ()):
            if isinstance(text, str):
                yield f"{container}.{key}", text


def machine_specific(entry: dict[str, Any], home: Path, root: Path) -> list[str]:
    """Fields of a rendered entry that tie it to this machine (absolute or home paths, this project's path).

    A project file (``.mcp.json``) travels with the repository, so such an entry is refused there.
    Returns field names only.
    """
    anchors = [str(home), str(root), str(root.resolve())]
    found: list[str] = []
    for field, text in _entry_strings(entry):
        if text.startswith(("/", "~")) or any(anchor and anchor != "/" and anchor in text for anchor in anchors):
            found.append(field)
    return found


def _project_file(root: Path) -> Path:
    return root / ".mcp.json"


def _read_project_file(path: Path) -> tuple[dict[str, Any], str | None]:
    """(servers, problem) of the project's ``.mcp.json``."""
    if not path.exists() and not path.is_symlink():
        return {}, None
    if path.is_symlink():
        return {}, "mcp-json-symlink"
    try:
        data = core._read_json(path)
    except core.AgentPackError:
        return {}, "mcp-json-unreadable"
    servers = data.get("mcpServers", {})
    return (servers, None) if isinstance(servers, dict) else ({}, "mcp-json-unreadable")


def _writable(path: Path) -> bool:
    target = path if path.exists() else path.parent
    while not target.exists() and target != target.parent:
        target = target.parent
    return os.access(target, os.W_OK)


def _state_scopes(state: dict[str, Any], kind: str) -> dict[str, str]:
    scopes = state.get("scopes")
    value = scopes.get(kind) if isinstance(scopes, dict) else None
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------- plan

def _plugin_skill_links(home: Path, plugin_path: Path) -> list[Path]:
    """User-level Skill links that point into the retired plugin's directory and nowhere else.

    Only symlinks are considered; a real directory (the user's own Skill) and links to any other
    place (project package, other plugins, hand-made links) are never returned.
    """
    skills_root = home / ".claude" / "skills"
    if not skills_root.is_dir() or skills_root.is_symlink():
        return []
    roots = {plugin_path.resolve()}
    found: list[Path] = []
    for child in sorted(skills_root.iterdir(), key=lambda item: item.name):
        target = _link_target(child)
        if target is not None and any(_inside(target, root) for root in roots):
            found.append(child)
    return found


def _installed_plugin_mcps(home: Path, plugin: str, plugin_path: Path, config: dict[str, Any]) -> set[str]:
    """Servers in ``~/.claude.json`` that ``bootstrap`` wrote for this plugin, judged by content.

    Bootstrap leaves no per-server ownership record, so a name counts only when the entry equals what the
    plugin's ``mcp.json`` renders to today; a same-named entry the user changed may be theirs and is not claimed.
    """
    mcp_path = plugin_path / "mcp.json"
    if not mcp_path.is_file():
        return set()
    try:
        declared = core._read_json(mcp_path).get("mcpServers", {})
    except (core.AgentPackError, AttributeError):
        return set()
    owned: set[str] = set()
    for server, raw in declared.items():
        if server not in config:
            continue
        parsed = core._portable_mcp(server, raw)
        if parsed.portable and core._claude_mcp_config(
                parsed.config, record_path(home, plugin), data_root(home, plugin)) == config[server]:
            owned.add(server)
    return owned


def _plugin_residuals(home: Path, plugin: str, plugin_path: Path, config: dict[str, Any]) -> list[dict[str, str]]:
    """Entries a retirement leaves on purpose: files Claude Code owns or that may hold local credentials."""
    residuals: list[dict[str, str]] = []
    prefixes = {str(plugin_path), str(plugin_path.resolve())}
    config_path = core._claude_user_config(home)
    installed = _installed_plugin_mcps(home, plugin, plugin_path, config)
    for server, entry in sorted(config.items()):
        if any(text.startswith(prefix + os.sep) or text == prefix for text in _strings(entry) for prefix in prefixes):
            residuals.append({
                "client": "claude", "kind": "mcp", "name": server, "path": str(config_path),
                "manual": f"MCP server {server} still points into the retired plugin; remove it with "
                          f"`claude mcp remove --scope user {server}` (the file may hold local credentials, so it is not edited automatically)"})
        elif server in installed:
            residuals.append({
                "client": "claude", "kind": "mcp", "name": server, "path": str(config_path),
                "manual": f"MCP server {server} was installed by the retired plugin (identical to what its mcp.json "
                          f"renders to); remove it with `claude mcp remove --scope user {server}` (the file may hold "
                          f"local credentials, so it is not edited automatically)"})
    managed = home / ".claude" / "plugins"
    if _inside(plugin_path, managed):
        residuals.append({
            "client": "claude", "kind": "installed-record", "name": plugin,
            "path": str(managed / "installed_plugins.json"),
            "manual": f"Claude Code owns this install record; run `claude plugin uninstall {plugin}`"})
    return residuals


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
                 retire_only: bool = False, only: frozenset[str] = frozenset(SYNC_TYPES),
                 package_tree: Path | None = None, scope: str = "all",
                 project_store: str = "local") -> dict[str, Any]:
    """Every change a Claude sync would make, without writing; conflicts are listed, not raised.

    ``only`` limits the sync to those types (#68): ``plugins`` is the package and its install record,
    ``skills`` the user-level Skill links, ``mcp`` the user-scope MCP entries. ``package_tree`` is the
    tree that will be installed as the package (default: ``built``). Explicit retirements are never filtered.

    ``scope`` (#67) narrows the plan to the user-level entries (``user``), the entries the project declares as
    project-level (``project``) or both (``all``). A project-declared entry is never written to the user level;
    ``project`` MCP entries go to ``project_store``: ``local`` (``~/.claude.json`` under the project's path) or
    ``file`` (the project's ``.mcp.json``, only for entries that do not depend on this machine). A target that is not
    writable, or whose structure is not recognised, makes the entry ``skip-*`` with a warning: never a fallback to
    the user level. Project-declared Skills are still ``skip-skill`` here (``.claude/skills`` arrives with a later change).
    """
    project_store = normalize_project_store(project_store)
    package_tree = package_tree or built
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
    mcp_places = _state_scopes(state, "mcps")
    owned_project_mcps = {server for server in state.get("mcps", {}) if mcp_places.get(server, "user") != "user"}
    owned_mcps = set(state.get("mcps", {})) - owned_project_mcps    # user-level ownership only
    owned_mcps |= {server for server, entry in existing.items() if _references(entry, record)}
    owned_skills = set(state.get("skills", []))
    project_skills = {item for item in desired_skills if core.declared_scope(layout, "skill", item) == "project"}
    project_mcps = {item for item in desired_mcps if core.declared_scope(layout, "mcp", item) == "project"}
    # Scope fields appear only once the project declares a scope (or --scope is given); older output is unchanged.
    aware = bool(project_skills or project_mcps or layout.mcp_scope or layout.skill_scope
                 or layout.default_scope != "user") or scope != "all"
    do_user, do_project = scope != "project", scope != "user"

    operations: list[dict[str, Any]] = []
    conflicts: list[str] = []
    overrides: list[dict[str, Any]] = []
    residuals: list[dict[str, str]] = []

    sync_skills, sync_mcps = "skills" in only, "mcp" in only
    others = scoping.other_projects(home, name) if aware else []
    narrowing: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    if not retire_only:
        # The package is the carrier of the other two types, so a partial sync still installs it (composed
        # from what is installed plus the selected parts); the record link follows the package.
        if not do_user:
            pass
        elif package.exists() and not package.is_symlink():
            same = core._tree_fingerprint(package) == core._tree_fingerprint(package_tree)
            operations.append({"action": "keep-package" if same else "replace-package", "name": name, "to": str(package)})
        else:
            operations.append({"action": "install-package", "name": name, "to": str(package)})
        if not do_user:
            pass
        elif old_target is not None and package.is_dir() and old_target.resolve() == package.resolve():
            operations.append({"action": "keep-record", "name": name, "to": str(record)})
        elif record.is_symlink() or record.exists():
            detail = {"from": str(old_target)} if old_target is not None else {}
            operations.append({"action": "relink-record", "name": name, "to": str(record), **detail})
        else:
            operations.append({"action": "link-record", "name": name, "to": str(record)})

        skills_root = home / ".claude" / "skills"
        for skill in sorted((desired_skills - project_skills) if sync_skills and do_user else ()):
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
        for skill in sorted(owned_skills - desired_skills if sync_skills and do_user else ()):
            path = skills_root / skill
            if path.is_symlink() or path.exists():
                operations.append({"action": "remove-skill", "name": skill, "from": str(path)})
        if sync_skills and do_user and legacy_root is not None and skills_root.is_dir():
            for path in sorted(skills_root.iterdir()):
                link = _link_target(path)
                if (path.name not in desired_skills and path.name not in owned_skills and link is not None
                        and _inside(link, legacy_root)):
                    operations.append({"action": "remove-skill", "name": path.name, "from": str(path)})

        for server, rendered in (desired_mcps.items() if sync_mcps and do_user else ()):
            if server in project_mcps:
                continue
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
        for server in sorted(owned_mcps - set(desired_mcps) if sync_mcps and do_user else ()):
            if server in existing:
                operations.append({"action": "remove-mcp", "name": server,
                                   "reason": "excluded for claude" if server in excluded else "no longer in project"})

    if not retire_only and do_project:
        project_dir = layout.root
        for skill in sorted(project_skills if sync_skills else ()):
            target = project_dir / ".claude" / "skills" / skill
            operations.append({"action": "skip-skill", "name": skill, "to": str(target)})
            skipped.append({"kind": "skill", "name": skill, "client": "claude", "reason": "project-write-unavailable"})
            if (skills_root / skill).is_symlink() or (skills_root / skill).exists():
                narrowing.append(scoping.narrowing_entry("skill", skill, scoping.USER_SKILLS,
                                                         scoping.PROJECT_SKILLS, others))
        local_key, local_servers, local_problem = local_section(config, layout.root)
        file_path = _project_file(layout.root)
        file_servers, file_problem = _read_project_file(file_path)
        target_store = scoping.LOCAL_MCP if project_store == "local" else scoping.FILE_MCP
        target_path = (f"{core._claude_user_config(home)}#projects[{local_key or layout.root}].mcpServers"
                       if project_store == "local" else str(file_path))
        problem = (local_problem if project_store == "local" else file_problem) or (
            None if _writable(core._claude_user_config(home) if project_store == "local" else file_path)
            else "target-not-writable")
        place = "project:local" if project_store == "local" else "project:file"
        wanted = sorted(project_mcps) if sync_mcps else []
        if scope == "project" and any(_references(desired_mcps[server], record) for server in wanted) \
                and not package.is_dir():
            raise core.AgentPackError(
                "project-level MCP entries launch through the project package, which is not installed yet: "
                "sync the package first (--scope user, or --only plugins)")
        if project_store == "file":
            bad = {server: machine_specific(desired_mcps[server], home, layout.root) for server in wanted}
            bad = {server: fields for server, fields in bad.items() if fields}
            if bad:
                raise core.AgentPackError(
                    "MCP entries that depend on this machine cannot be written to the project file .mcp.json "
                    "(use --project-store local or declare them user-level): "
                    + "; ".join(f"{server} ({', '.join(fields)})" for server, fields in sorted(bad.items())))
        current_servers = local_servers if project_store == "local" else file_servers
        for server in wanted:
            if problem:
                operations.append({"action": "skip-mcp", "name": server, "to": target_path,
                                   "scope": "project", "store": target_store})
                skipped.append({"kind": "mcp", "name": server, "client": "claude", "reason": problem})
                continue
            state_name, entry, kept = classify_mcp(desired_mcps[server], current_servers.get(server),
                                                   reset=reset_overrides or project_store == "file")
            if state_name == "differ":
                if mcp_places.get(server) == place and server in owned_project_mcps:
                    state_name = "update"
                else:
                    state_name = "conflict"
                    conflicts.append(f"project-mcp {server}: {target_path}")
            if kept:
                overrides.append({"name": server, "keys": kept})
            operations.append({"action": f"{state_name}-project-mcp", "name": server, "to": target_path,
                               "keys": kept, "scope": "project", "store": target_store, "_entry": entry})
            if server in existing:
                narrowing.append(scoping.narrowing_entry("mcp", server, scoping.USER_MCP, target_store, others)
                                 | {"manual": f"claude mcp remove --scope user {server}"})
        for server in sorted(owned_project_mcps - set(desired_mcps) if sync_mcps else ()):
            if mcp_places.get(server) == place and server in current_servers and not problem:
                operations.append({"action": "remove-project-mcp", "name": server, "to": target_path,
                                   "scope": "project", "store": target_store, "reason": "no longer in project"})

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
        taken = {item["name"] for item in operations if item["action"].endswith("-skill")}
        for link in _plugin_skill_links(home, path):
            if link.name not in taken:
                operations.append({"action": "archive-skill", "name": link.name, "from": str(link),
                                   "reason": f"links into retired plugin {plugin}"})
        residuals.extend(_plugin_residuals(home, plugin, path, existing))
    if retire_only and not (retire_skills or retire_plugins):
        raise core.AgentPackError("retire-only reconcile needs at least one --retire-skill or --retire-plugin")

    native = sorted(server for server in existing if server not in desired_mcps and server not in owned_mcps)
    # What the state records as owned: what this plan puts at the user level, plus project-declared entries that
    # already sit there under this package's ownership (they stay until a later change moves them).
    state_skills = sorted((desired_skills - project_skills) | (project_skills & owned_skills))
    carried_mcps = sorted(server for server in project_mcps & owned_mcps if server in existing)
    undeclared = sorted((set(layout.mcp_scope) - set(desired_mcps) - set(item.name for item in authority.mcps))
                        | (set(layout.skill_scope) - desired_skills - set(item.name for item in authority.skills)))
    return {"name": name, "aware": aware, "scope": scope, "do_user": do_user, "do_project": do_project, "skipped": skipped, "narrowing": narrowing,
            "others": others, "undeclared": undeclared,
            "state_skills": state_skills, "carried_mcps": carried_mcps,
            "scope_counts": {"user": {"skills": len(desired_skills - project_skills) if sync_skills and do_user else 0,
                                      "mcp": len(set(desired_mcps) - project_mcps) if sync_mcps and do_user else 0},
                             "project": {"skills": len(project_skills) if sync_skills and do_project else 0,
                                         "mcp": len(project_mcps) if sync_mcps and do_project else 0}}, "layout": layout, "operations": operations, "conflicts": conflicts,
            "retire_only": retire_only, "residuals": residuals, "only": sorted(only),
            "overrides": overrides if sync_mcps else [],
            "excluded": [server for server in excluded if server in existing] if sync_mcps else [],
            "native": native, "legacy_root": str(legacy_root) if legacy_root else None,
            "desired_skills": sorted(desired_skills), "desired_mcps": sorted(desired_mcps)}


def _public(operations: list[dict[str, Any]], aware: bool = False) -> list[dict[str, Any]]:
    result = [{key: value for key, value in item.items() if not key.startswith("_") and value != []}
              | {"client": "claude"} for item in operations]
    if aware:   # scope and store only appear once a project declares a scope (older output stays byte-identical)
        for item in result:
            for key, value in _scope_of_operation(item).items():
                item.setdefault(key, value)
    return result


def _scope_of_operation(item: dict[str, Any]) -> dict[str, str]:
    action = item["action"]
    if action.endswith("-skill") and action.startswith("skip-"):
        return {"scope": "project", "store": scoping.PROJECT_SKILLS}
    if action.startswith("skip-") and action.endswith("-mcp"):
        return {"scope": "project", "store": scoping.LOCAL_MCP}
    if action.endswith("-project-mcp"):
        return {"scope": "project", "store": scoping.LOCAL_MCP}
    if action.endswith("-mcp"):
        return {"scope": "user", "store": scoping.USER_MCP}
    if action.endswith("-skill"):
        return {"scope": "user", "store": scoping.USER_SKILLS}
    return {"scope": "user"}   # the package and its install record are the user-level carrier


# ---------------------------------------------------------------- apply

def _conflict_summary(conflicts: list[str]) -> dict[str, int]:
    """Conflicts counted per type; a conflict string starts with ``skill`` or ``mcp``."""
    counts = {"skills": sum(1 for item in conflicts if item.startswith("skill ")),
              "mcp": sum(1 for item in conflicts if item.startswith("mcp ")),
              "project_mcp": sum(1 for item in conflicts if item.startswith("project-mcp "))}
    return {key: number for key, number in counts.items() if number}


def _global_warnings(home: Path, summary: dict[str, int]) -> list[str]:
    """What a replace reaches beyond this project: user-level Skills and MCP entries serve every project."""
    warnings = []
    if summary.get("skills"):
        warnings.append(f"replaces {summary['skills']} user-level Skill(s) in {home / '.claude' / 'skills'}; "
                        "they apply to every project (the replaced originals go to recovery)")
    if summary.get("mcp"):
        warnings.append(f"replaces {summary['mcp']} user-scope MCP entr{'y' if summary['mcp'] == 1 else 'ies'} in "
                        f"{core._claude_user_config(home)}; they apply to every project (the replaced originals go to recovery)")
    if summary.get("project_mcp"):
        warnings.append(scoping.project_replace_warning(summary["project_mcp"]))
    return warnings


def sync_project(project: Path, *, home: Path | None = None, apply: bool = False, replace: bool = False,
                 reset_overrides: bool = False, recovery_root: Path | None = None,
                 retire_skills: Iterable[str] = (), retire_plugins: Iterable[str] = (),
                 retire_only: bool = False, only: Iterable[str] | None = None,
                 replace_mcp: bool = False, replace_skills: bool = False, scope: str = "all",
                 project_store: str = "local") -> dict[str, Any]:
    home = (home or Path.home()).expanduser().resolve()
    try:
        scope = scoping.normalize_scope(scope)
    except ValueError as exc:
        raise core.AgentPackError(str(exc)) from exc
    retire_skills, retire_plugins = list(retire_skills), list(retire_plugins)
    types = normalize_only(only)
    with tempfile.TemporaryDirectory(prefix="agent-pack-claude-") as temporary:
        layout = core.load_project_layout(project)
        authority = core.inventory_project(layout.root)
        name = core._package_name(layout.root)
        built = core._build_package(authority, name, Path(temporary), "claude", home)
        tree = built if types == frozenset(SYNC_TYPES) else _compose_package(
            package_root(home, name), built, types, Path(temporary))
        plan = plan_project(project, home, built=built, reset_overrides=reset_overrides,
                            retire_skills=retire_skills, retire_plugins=retire_plugins, retire_only=retire_only,
                            only=types, package_tree=tree, scope=scope, project_store=project_store)
        blocking = [item for item in plan["conflicts"]
                    if not (replace or (replace_mcp and item.startswith(("mcp ", "project-mcp "))) or
                            (replace_skills and item.startswith("skill ")))]
        if blocking:
            raise core.AgentPackError("Claude client already has different content not installed by this package "
                                      "(use --replace, or --replace-mcp / --replace-skills for one type; "
                                      "replaced items go to recovery): " + "; ".join(blocking))
        result: dict[str, Any] = {
            "ok": True, "applied": False, "client": "claude", "package": name,
            "operations": _public(plan["operations"], plan["aware"]), "overrides": plan["overrides"],
            "excluded": plan["excluded"], "native": plan["native"],
            "notes": ["restart Claude Code to load changed Skills and MCP servers"],
        }
        summary = _conflict_summary(plan["conflicts"])
        if summary:
            result["conflicts"] = summary
            result["warnings"] = _global_warnings(home, summary)
        if types != frozenset(SYNC_TYPES):
            result["only"] = sorted(types)
        if plan["aware"]:
            result["scope"] = scope
            result["by_scope"] = plan["scope_counts"]
            result["narrowing"] = plan["narrowing"]
            result["skipped"] = plan["skipped"]
            notes = [scoping.narrowing_note()] if plan["narrowing"] else []
            if plan["undeclared"]:
                notes.append(scoping.undeclared_warning(plan["undeclared"]))
            if notes:
                result["warnings"] = [*result.get("warnings", []), *notes]
        if plan["residuals"]:
            result["residuals"] = plan["residuals"]
        if plan["legacy_root"]:
            result["migrated_from"] = plan["legacy_root"]
        changes = [item for item in plan["operations"] if not item["action"].startswith(("keep-", "skip-"))]
        if not apply or not changes:
            result["applied"] = apply and not changes
            return result
        result["recovery"] = str(_apply(plan, tree, home, recovery_root))
        result["applied"] = True
        return result


def _final_mcp_state(plan: dict[str, Any], before: dict[str, Any], user: dict[str, str], project: dict[str, tuple[str, str]],
                     removed: set[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Owned MCP entries after an apply and where each sits: ``(name -> hash, name -> place)``.

    A scope the plan did not handle (``--scope``) keeps what the previous state recorded for it.
    """
    old_hashes = before.get("mcps") if isinstance(before.get("mcps"), dict) else {}
    old_places = _state_scopes(before, "mcps")
    hashes: dict[str, str] = {}
    places: dict[str, str] = {}
    for name, digest in old_hashes.items():
        place = old_places.get(name, "user")
        handled = plan["do_user"] if place == "user" else plan["do_project"]
        if not handled and name not in removed:
            hashes[name], places[name] = digest, place
        elif place != "user" and name not in removed and name not in project:
            hashes[name], places[name] = digest, place    # a project entry the plan left alone (skipped or other store)
    if plan["do_user"]:
        for name, digest in user.items():
            hashes[name], places[name] = digest, "user"
    for name, (digest, place) in project.items():
        hashes[name], places[name] = digest, place
    return hashes, places


def _scopes_record(plan: dict[str, Any], before: dict[str, Any], skills: list[str], places: dict[str, str] | None) -> dict[str, Any]:
    """``scopes``: where each owned entry actually sits. Written only once a project declares a scope; a state
    without it reads as all ``user``."""
    if not plan["aware"] and "scopes" not in before:
        return {}
    old = before.get("scopes") if isinstance(before.get("scopes"), dict) else {}
    mcps = dict(places) if places is not None else dict(old.get("mcps", {}))
    return {"scopes": {"skills": {item: "user" for item in skills}, "mcps": mcps}}


def _restore_bytes(path: Path, data: bytes | None, mode: int) -> None:
    """Put a file back exactly as it was (or remove it when it did not exist)."""
    if data is None:
        if path.exists() or path.is_symlink():
            path.unlink()
        return
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


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
    config_bytes = config_path.read_bytes() if config_path.is_file() else None
    config_mode = stat.S_IMODE(config_path.stat().st_mode) if config_path.is_file() else 0o600
    project_file = _project_file(plan["layout"].root)
    file_bytes = project_file.read_bytes() if project_file.is_file() else None
    file_mode = stat.S_IMODE(project_file.stat().st_mode) if project_file.is_file() else 0o644
    file_written = False
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
        # A sync that does not include mcp never opens ~/.claude.json for writing (#68).
        user_mcp_ops = any(item["action"].endswith("-mcp") and not item["action"].endswith("-project-mcp")
                           and not item["action"].startswith("skip-") for item in plan["operations"])
        # (a scope-aware plan with no user-level MCP operation leaves ~/.claude.json's user section alone)
        servers = (config.setdefault("mcpServers", {})
                   if "mcp" in plan["only"] and plan["do_user"] and (user_mcp_ops or not plan["aware"]) else {})
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
        # Project-level entries: Claude local (inside ~/.claude.json, written with the user-level change in one
        # atomic replace) and the project file .mcp.json.
        project_state: dict[str, tuple[str, str]] = {}
        removed_project: set[str] = set()
        local_set: dict[str, Any] = {}
        file_set: dict[str, Any] = {}
        local_drop: set[str] = set()
        file_drop: set[str] = set()
        for item in plan["operations"]:
            action, server = item["action"], item["name"]
            if action in PROJECT_ACTIONS:
                local = item["store"] == scoping.LOCAL_MCP
                project_state[server] = (hashlib.sha256(json.dumps(item["_entry"], sort_keys=True).encode("utf-8")).hexdigest(),
                                         "project:local" if local else "project:file")
                if not action.startswith("keep-"):
                    (local_set if local else file_set)[server] = item["_entry"]
            elif action == "remove-project-mcp":
                removed_project.add(server)
                (local_drop if item["store"] == scoping.LOCAL_MCP else file_drop).add(server)
        if local_set or local_drop:
            key = local_section(config, plan["layout"].root)[0]
            if key is None:
                raise core.AgentPackError("claude local MCP store changed shape since the preview")
            if config_bytes is not None:   # the original file, before any write (it may hold the user's own values)
                backup = transaction / "claude.json.before"
                backup.write_bytes(config_bytes)
                os.chmod(backup, 0o600)
            section = config.setdefault("projects", {}).setdefault(key, {})
            local_servers = section.setdefault("mcpServers", {})
            local_servers.update(local_set)
            for server in local_drop:
                local_servers.pop(server, None)
        if config != original_config:
            core._write_json_atomic(config_path, config, mode=config_mode)
            config_written = True
        if file_set or file_drop:
            if file_bytes is not None:
                backup = transaction / "project-mcp.json.before"
                backup.write_bytes(file_bytes)
                os.chmod(backup, 0o600)
            data = core._read_json(project_file) if file_bytes is not None else {}
            file_servers = data.setdefault("mcpServers", {})
            file_servers.update(file_set)
            for server in file_drop:
                file_servers.pop(server, None)
            core._write_json_atomic(project_file, data, mode=file_mode)
            file_written = True
        for server in plan["carried_mcps"]:   # project-declared, already owned at the user level: keep owning it
            if "mcp" in plan["only"]:
                mcp_state.setdefault(server, (_read_state(home, name).get("mcps") or {}).get(server)
                                     or hashlib.sha256(json.dumps(servers.get(server), sort_keys=True).encode("utf-8")).hexdigest())
        if not plan["retire_only"]:
            # A partial sync records only the types it handled; the others keep their recorded ownership.
            before = _read_state(home, name)
            only = set(plan["only"])
            final_mcps, places = _final_mcp_state(plan, before, mcp_state, project_state, removed_project) \
                if "mcp" in only else (before.get("mcps", {}), None)
            skills_final = plan["state_skills"] if "skills" in only and plan["do_user"] else before.get("skills", [])
            core._write_json_atomic(state_path(home, name), {
                "schema_version": STATE_SCHEMA, "package": name,
                "skills": skills_final, "mcps": final_mcps,
                "updated_at": stamp} | _scopes_record(plan, before, skills_final, places), mode=0o600)
    except Exception as exc:
        if config_written:
            _restore_bytes(config_path, config_bytes, config_mode)
        if file_written:
            _restore_bytes(project_file, file_bytes, file_mode)
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
        built = core._build_package(core.inventory_project(layout.root), name, Path(temporary), "claude", home)
        plan = plan_project(project, home, built=built)
    report: dict[str, Any] = {"client": "claude", "package": name, "missing": [], "different": [],
                              "overrides": plan["overrides"], "stale": [], "native": plan["native"],
                              "excluded": plan["excluded"], "dangling": [], "coupled": plan["legacy_root"]}
    for item in plan["operations"]:
        action, item_name = item["action"], item["name"]
        if action in ("install-package", "link-record", "link-skill", "add-mcp", "add-project-mcp"):
            report["missing"].append(f"{action.split('-')[1]} {item_name}")
        elif action in ("replace-package", "relink-skill", "conflict-skill", "update-mcp", "conflict-mcp",
                        "update-project-mcp", "conflict-project-mcp"):
            report["different"].append(f"{action.split('-')[-1]} {item_name}")
        elif action in ("remove-skill", "remove-mcp", "remove-project-mcp"):
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
