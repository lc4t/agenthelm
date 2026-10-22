#!/usr/bin/env python3
"""Portable Agent Plugin / Skill / MCP inventory and transfer runtime.

The module intentionally uses only the Python standard library so it can run
from an Agent Plugin without creating a package-local virtual environment.
Writes are always opt-in through ``apply=True`` and collisions fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


AGENT_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
AGENT_PLUGIN_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
PROJECT_PROFILE_NAMESPACE = "moe.sakanano.agent-pack"
PROJECT_PROFILE_RELATIVE = Path(".agents") / PROJECT_PROFILE_NAMESPACE / "project.json"
PLUGIN_NAME_RE = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SECRET_NAME_RE = re.compile(
    r"(?:secret|token|password|passwd|api[_-]?key|authorization|cookie|credential)",
    re.IGNORECASE,
)
SECRET_VALUE_RE = re.compile(
    r"(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"AKIA[A-Z0-9]{16})",
    re.IGNORECASE,
)
ENV_REFERENCE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Placeholders the client resolves at launch time (immutable package root and
# client-managed writable state). They are never credentials and must not
# trigger the credential-env-file requirement or env-launcher wrapping.
RESERVED_ENV_PLACEHOLDERS = {"PLUGIN_ROOT", "PLUGIN_DATA"}
COPY_IGNORE = {".git", ".venv", "__pycache__", "node_modules", ".DS_Store"}
PLUGIN_FIELDS = {
    "$schema", "name", "version", "description", "author", "homepage",
    "repository", "license", "keywords", "extensions",
}


class AgentPackError(RuntimeError):
    """A user-actionable configuration or transfer error."""


@dataclass(frozen=True)
class SkillRecord:
    name: str
    path: str
    source: str
    portable: bool = True
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpRecord:
    name: str
    config: dict[str, Any]
    source: str
    portable: bool
    issues: tuple[str, ...] = ()
    authority: str = "portable"


@dataclass
class Inventory:
    source: str
    plugins: list[dict[str, Any]] = field(default_factory=list)
    skills: list[SkillRecord] = field(default_factory=list)
    mcps: list[McpRecord] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "plugins": self.plugins,
            "skills": [asdict(item) for item in self.skills],
            "mcps": [asdict(item) for item in self.mcps],
            "issues": self.issues,
        }


@dataclass(frozen=True)
class ProjectLayout:
    root: Path
    profile_path: Path | None
    plugin_roots: tuple[Path, ...]
    plugin_dirs: tuple[Path, ...]
    skill_roots: tuple[Path, ...]
    mcp_sources: tuple[Path, ...]
    native_mcp_sources: tuple[Path, ...]
    credential_env_file: Path | None
    mcp_client_policy: dict[str, dict[str, tuple[str, ...]]]
    destination_skill_root: Path
    destination_mcp: Path


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise AgentPackError(f"refusing symlinked JSON: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentPackError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AgentPackError(f"JSON root must be an object: {path}")
    return value


def _read_jsonc(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise AgentPackError(f"refusing symlinked JSONC: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AgentPackError(f"cannot read {path}: {exc}") from exc
    out: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(raw):
        char = raw[index]
        if escaped:
            out.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and in_string:
            out.append(char)
            escaped = True
            index += 1
            continue
        if char == '"':
            in_string = not in_string
            out.append(char)
            index += 1
            continue
        if not in_string and raw[index:index + 2] == "//":
            while index < len(raw) and raw[index] not in "\r\n":
                index += 1
            continue
        out.append(char)
        index += 1
    cleaned = re.sub(r",\s*([}\]])", r"\1", "".join(out))
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AgentPackError(f"invalid JSONC {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AgentPackError(f"JSONC root must be an object: {path}")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any], *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _contained(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AgentPackError(f"path escapes project root: {candidate}") from exc
    return resolved


def _resolve_profile_paths(root: Path, values: Any, defaults: list[str]) -> tuple[Path, ...]:
    raw_values = values if isinstance(values, list) else defaults
    paths: list[Path] = []
    for value in raw_values:
        if not isinstance(value, str) or not value.strip():
            raise AgentPackError("project profile paths must be non-empty strings")
        candidate = root / value
        _contained(root, candidate.parent if not candidate.exists() else candidate)
        paths.append(candidate)
    return tuple(paths)


def find_project_root(start: Path) -> Path:
    current = start.expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / PROJECT_PROFILE_RELATIVE).is_file():
            return candidate
        if (candidate / "AGENTS.md").is_file() and (candidate / ".git").exists():
            return candidate
    raise AgentPackError(f"no initialized project found from {start}")


def load_project_layout(project: Path) -> ProjectLayout:
    root = find_project_root(project)
    profile_path = root / PROJECT_PROFILE_RELATIVE
    config: dict[str, Any] = {}
    if profile_path.is_file():
        profile = _read_json(profile_path)
        if profile.get("schema_version") != "1.0":
            raise AgentPackError(f"unsupported project profile schema: {profile.get('schema_version')!r}")
        config_value = profile.get("capabilities", {})
        if isinstance(config_value, dict):
            config = config_value

    plugin_roots = _resolve_profile_paths(root, config.get("plugin_roots"), [".agents"])
    plugin_dirs = _resolve_profile_paths(root, config.get("plugin_dirs"), [])
    skill_roots = _resolve_profile_paths(root, config.get("skill_roots"), [".agents/skills"])
    mcp_sources = _resolve_profile_paths(root, config.get("mcp_sources"), [".agents/mcp.json"])
    native_mcp_sources = _resolve_profile_paths(root, config.get("native_mcp_sources"), [])
    credential_value = config.get("credential_env_file")
    credential_env_file = None
    if credential_value is not None:
        credential_env_file = _resolve_profile_paths(root, [credential_value], [])[0]
    policy_value = config.get("mcp_client_policy", {})
    if not isinstance(policy_value, dict):
        raise AgentPackError("mcp_client_policy must be an object")
    mcp_client_policy: dict[str, dict[str, tuple[str, ...]]] = {}
    for name, raw_policy in policy_value.items():
        if not isinstance(name, str) or not isinstance(raw_policy, dict):
            raise AgentPackError("MCP client policies must map names to objects")
        unknown = set(raw_policy) - {"include", "exclude"}
        if unknown:
            raise AgentPackError(f"MCP policy {name} has unknown fields: {', '.join(sorted(unknown))}")
        parsed: dict[str, tuple[str, ...]] = {}
        for mode in ("include", "exclude"):
            values = raw_policy.get(mode, [])
            if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
                raise AgentPackError(f"MCP policy {name}.{mode} must be a string array")
            parsed[mode] = tuple(values)
        if parsed["include"] and parsed["exclude"]:
            raise AgentPackError(f"MCP policy {name} cannot set both include and exclude")
        mcp_client_policy[name] = parsed
    destination_skill = _resolve_profile_paths(
        root, [config.get("destination_skill_root", ".agents/skills")], [".agents/skills"]
    )[0]
    destination_mcp = _resolve_profile_paths(
        root, [config.get("destination_mcp", ".agents/mcp.json")], [".agents/mcp.json"]
    )[0]
    return ProjectLayout(
        root=root,
        profile_path=profile_path if profile_path.is_file() else None,
        plugin_roots=plugin_roots,
        plugin_dirs=plugin_dirs,
        skill_roots=skill_roots,
        mcp_sources=mcp_sources,
        native_mcp_sources=native_mcp_sources,
        credential_env_file=credential_env_file,
        mcp_client_policy=mcp_client_policy,
        destination_skill_root=destination_skill,
        destination_mcp=destination_mcp,
    )


def validate_plugin_manifest(plugin_root: Path) -> tuple[dict[str, Any], list[str]]:
    manifest_path = plugin_root / "plugin.json"
    data = _read_json(manifest_path)
    issues: list[str] = []
    if data.get("$schema") != AGENT_PLUGIN_SCHEMA:
        issues.append("plugin.json must target Agent Plugins 1.0.0")
    name = data.get("name")
    if not isinstance(name, str) or not PLUGIN_NAME_RE.fullmatch(name):
        issues.append(f"invalid plugin name: {name!r}")
    unknown = sorted(set(data) - PLUGIN_FIELDS)
    if unknown:
        issues.append("unknown plugin.json fields: " + ", ".join(unknown))
    return data, issues


def _portable_mcp(name: str, raw: Any) -> McpRecord:
    if not isinstance(raw, dict):
        return McpRecord(name, {}, "", False, ("MCP entry must be an object",))
    item = dict(raw)
    issues: list[str] = []
    mcp_type = item.get("type")
    if mcp_type == "http":
        item["type"] = "streamable-http"
        mcp_type = "streamable-http"
    if mcp_type is None:
        if isinstance(item.get("command"), str):
            item["type"] = "stdio"
            mcp_type = "stdio"
        elif isinstance(item.get("url"), str):
            item["type"] = "streamable-http"
            mcp_type = "streamable-http"
    if mcp_type not in {"stdio", "streamable-http", "sse"}:
        issues.append(f"unsupported transport: {mcp_type!r}")

    for container_name in ("env", "headers"):
        container = item.get(container_name, {})
        if container is None:
            continue
        if not isinstance(container, dict):
            issues.append(f"{container_name} must be an object")
            continue
        for key, value in container.items():
            references = ENV_REFERENCE_RE.findall(str(value))
            safe_reference = bool(references) and not SECRET_VALUE_RE.search(
                ENV_REFERENCE_RE.sub("", str(value))
            )
            if (SECRET_NAME_RE.search(str(key)) or SECRET_VALUE_RE.search(str(value))) and not safe_reference:
                issues.append(f"possible secret in {container_name}.{key}")
    args = item.get("args", [])
    if isinstance(args, list):
        for index, value in enumerate(args):
            rendered = str(value)
            safe_reference = bool(ENV_REFERENCE_RE.search(rendered)) and not SECRET_VALUE_RE.search(
                ENV_REFERENCE_RE.sub("", rendered)
            )
            if (SECRET_NAME_RE.search(rendered) or SECRET_VALUE_RE.search(rendered)) and not safe_reference:
                issues.append(f"possible secret reference in args[{index}]")
    if mcp_type == "stdio":
        allowed = {"type", "command", "args", "env", "cwd"}
        if not isinstance(item.get("command"), str) or not item["command"]:
            issues.append("stdio command is required")
    else:
        allowed = {"type", "url", "headers"}
        if not isinstance(item.get("url"), str) or not item["url"]:
            issues.append("remote URL is required")
    unknown = sorted(set(item) - allowed)
    if unknown:
        issues.append("unsupported fields: " + ", ".join(unknown))
    return McpRecord(name, item, "", not issues, tuple(issues))


def _inventory_plugin(plugin_root: Path, source: str) -> Inventory:
    inventory = Inventory(source=source)
    try:
        manifest, manifest_issues = validate_plugin_manifest(plugin_root)
    except AgentPackError as exc:
        inventory.issues.append(str(exc))
        return inventory
    inventory.plugins.append({
        "name": manifest.get("name"),
        "version": manifest.get("version"),
        "path": str(plugin_root),
        "issues": manifest_issues,
    })
    inventory.issues.extend(manifest_issues)

    skills_root = plugin_root / "skills"
    if skills_root.is_dir() and not skills_root.is_symlink():
        for child in sorted(skills_root.iterdir(), key=lambda item: item.name):
            if child.name.startswith(".") or not child.is_dir():
                continue
            skill_file = child / "SKILL.md"
            issues: list[str] = []
            if not SKILL_NAME_RE.fullmatch(child.name):
                issues.append("non-portable skill directory name")
            if not skill_file.is_file() or skill_file.is_symlink():
                issues.append("SKILL.md missing or symlinked")
            inventory.skills.append(SkillRecord(
                child.name, str(child), source, not issues, tuple(issues)
            ))

    mcp_path = plugin_root / "mcp.json"
    if mcp_path.is_file():
        try:
            payload = _read_json(mcp_path)
            if payload.get("$schema") != AGENT_PLUGIN_MCP_SCHEMA:
                inventory.issues.append(f"{mcp_path}: MCP schema is not Agent Plugins 1.0.0")
            servers = payload.get("mcpServers", {})
            if not isinstance(servers, dict):
                raise AgentPackError(f"mcpServers must be an object: {mcp_path}")
            for name, raw in sorted(servers.items()):
                record = _portable_mcp(name, raw)
                inventory.mcps.append(McpRecord(
                    record.name, record.config, source, record.portable, record.issues, "portable"
                ))
        except AgentPackError as exc:
            inventory.issues.append(str(exc))
    return inventory


def _merge_inventory(target: Inventory, source: Inventory) -> None:
    target.plugins.extend(source.plugins)
    target.skills.extend(source.skills)
    target.mcps.extend(source.mcps)
    target.issues.extend(source.issues)


def inventory_project(project: Path) -> Inventory:
    layout = load_project_layout(project)
    inventory = Inventory(source=f"project:{layout.root}")
    visited_plugins: set[Path] = set()
    for plugin_root in layout.plugin_roots:
        if (plugin_root / "plugin.json").is_file():
            resolved = plugin_root.resolve()
            visited_plugins.add(resolved)
            _merge_inventory(inventory, _inventory_plugin(plugin_root, inventory.source))
    for plugin_dir in layout.plugin_dirs:
        if not plugin_dir.is_dir() or plugin_dir.is_symlink():
            continue
        for child in sorted(plugin_dir.iterdir(), key=lambda item: item.name):
            if child.is_dir() and (child / "plugin.json").is_file():
                resolved = child.resolve()
                if resolved not in visited_plugins:
                    visited_plugins.add(resolved)
                    _merge_inventory(inventory, _inventory_plugin(child, inventory.source))

    known_skill_paths = {Path(item.path).resolve() for item in inventory.skills}
    for skill_root in layout.skill_roots:
        if not skill_root.is_dir() or skill_root.is_symlink():
            continue
        for child in sorted(skill_root.iterdir(), key=lambda item: item.name):
            if not child.is_dir() or not (child / "SKILL.md").is_file():
                continue
            resolved = child.resolve()
            if resolved in known_skill_paths:
                continue
            issues = () if SKILL_NAME_RE.fullmatch(child.name) else ("non-portable skill name",)
            inventory.skills.append(SkillRecord(
                child.name, str(child), inventory.source, not issues, issues
            ))
            known_skill_paths.add(resolved)

    known_mcp_names = {item.name for item in inventory.mcps}
    for mcp_source in (*layout.mcp_sources, *layout.native_mcp_sources):
        if not mcp_source.is_file() or mcp_source.name == "mcp.json" and mcp_source.parent.resolve() in visited_plugins:
            continue
        try:
            payload = _read_jsonc(mcp_source) if mcp_source.suffix == ".jsonc" else _read_json(mcp_source)
            servers = payload.get("mcpServers", {})
            if not isinstance(servers, dict):
                raise AgentPackError(f"mcpServers must be an object: {mcp_source}")
            for name, raw in sorted(servers.items()):
                if name in known_mcp_names:
                    inventory.issues.append(f"duplicate MCP name: {name}")
                    continue
                record = _portable_mcp(name, raw)
                authority = "native" if mcp_source in layout.native_mcp_sources else "portable"
                inventory.mcps.append(McpRecord(
                    record.name, record.config, str(mcp_source), record.portable, record.issues, authority
                ))
                known_mcp_names.add(name)
        except AgentPackError as exc:
            inventory.issues.append(str(exc))

    _add_duplicate_issues(inventory)
    return inventory


def _client_skill_roots(client: str, home: Path) -> list[Path]:
    mapping = {
        "codex": [home / ".codex" / "skills"],
        "cursor": [home / ".cursor" / "skills"],
        "claude": [home / ".claude" / "skills"],
        "codebuddy": [home / ".codebuddy" / "skills"],
    }
    if client not in mapping:
        raise AgentPackError(f"unsupported client: {client}")
    return mapping[client]


def _client_plugin_roots(client: str, home: Path) -> list[Path]:
    if client == "cursor":
        return [home / ".cursor" / "plugins" / "local"]
    if client == "codex":
        return [home / ".agents" / "plugins" / "plugins"]
    if client == "claude":
        return [_claude_plugin_records(home)]
    return []


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value.split("+", 1)[0])
    return tuple(int(number) for number in numbers)


def _client_marketplace_plugins(client: str, home: Path) -> list[Path]:
    """Plugin roots the client installed from marketplaces (not agent-pack bootstrap copies)."""
    found: list[Path] = []
    if client == "claude":
        try:
            records = _read_json(home / ".claude" / "plugins" / "installed_plugins.json").get("plugins", {})
        except AgentPackError:
            records = {}
        for entries in records.values() if isinstance(records, dict) else ():
            for entry in entries if isinstance(entries, list) else ():
                path = Path(str(entry.get("installPath", "")))
                if (path / "plugin.json").is_file():
                    found.append(path)
    elif client == "codex":
        cache = home / ".codex" / "plugins" / "cache"
        if cache.is_dir() and not cache.is_symlink():
            for plugin_dir in sorted(path for market in cache.iterdir() if market.is_dir() for path in market.iterdir()):
                versions = [path for path in plugin_dir.iterdir() if (path / "plugin.json").is_file()] if plugin_dir.is_dir() else []
                if versions:
                    found.append(max(versions, key=lambda path: (_version_tuple(path.name), path.name)))
    return found


def _claude_plugin_records(home: Path) -> Path:
    """Install records for Claude Code: one symlink per plugin to its source root."""
    return home / ".claude" / "agent-pack" / "plugins"


def _claude_user_config(home: Path) -> Path:
    """Claude Code keeps user-scope MCP servers in ~/.claude.json#mcpServers."""
    return home / ".claude.json"


def _codex_mcps(home: Path) -> dict[str, Any]:
    path = home / ".codex" / "config.toml"
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    servers = data.get("mcp_servers", {})
    return servers if isinstance(servers, dict) else {}


def _json_client_mcps(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = _read_json(path)
    except AgentPackError:
        return {}
    servers = data.get("mcpServers", {})
    return servers if isinstance(servers, dict) else {}


def inventory_client(client: str, home: Path | None = None) -> Inventory:
    home = (home or Path.home()).expanduser().resolve()
    inventory = Inventory(source=f"client:{client}")
    for plugin_parent in _client_plugin_roots(client, home):
        if not plugin_parent.is_dir() or plugin_parent.is_symlink():
            continue
        for child in sorted(plugin_parent.iterdir(), key=lambda item: item.name):
            if child.is_dir() and (child / "plugin.json").is_file():
                _merge_inventory(inventory, _inventory_plugin(child, inventory.source))
    for child in _client_marketplace_plugins(client, home):
        _merge_inventory(inventory, _inventory_plugin(child, inventory.source))
    known = {Path(item.path).resolve() for item in inventory.skills}
    for skill_root in _client_skill_roots(client, home):
        if not skill_root.is_dir() or skill_root.is_symlink():
            continue
        for child in sorted(skill_root.iterdir(), key=lambda item: item.name):
            try:
                valid = child.is_dir() and (child / "SKILL.md").is_file()
            except OSError:
                valid = False
            if not valid:
                continue
            resolved = child.resolve()
            if resolved in known:
                continue
            issues = () if SKILL_NAME_RE.fullmatch(child.name) else ("non-portable skill name",)
            inventory.skills.append(SkillRecord(
                child.name, str(child), inventory.source, not issues, issues
            ))
            known.add(resolved)
    if client == "codex":
        servers = _codex_mcps(home)
    elif client == "cursor":
        servers = _json_client_mcps(home / ".cursor" / "mcp.json")
    elif client == "claude":
        servers = _json_client_mcps(_claude_user_config(home))
    else:
        servers = _json_client_mcps(home / ".codebuddy" / "mcp.json")
    for name, raw in sorted(servers.items()):
        record = _portable_mcp(name, raw)
        inventory.mcps.append(McpRecord(
            record.name, record.config, inventory.source, record.portable, record.issues, "client"
        ))
    _add_duplicate_issues(inventory)
    return inventory


def inventory_source(locator: str, home: Path | None = None) -> Inventory:
    if locator.startswith("client:"):
        return inventory_client(locator.split(":", 1)[1], home)
    if locator.startswith("project:"):
        return inventory_project(Path(locator.split(":", 1)[1]))
    if locator.startswith("plugin:"):
        path = Path(locator.split(":", 1)[1]).expanduser().resolve()
        return _inventory_plugin(path, locator)
    path = Path(locator).expanduser().resolve()
    if (path / PROJECT_PROFILE_RELATIVE).is_file() or (path / "AGENTS.md").is_file():
        return inventory_project(path)
    if (path / "plugin.json").is_file():
        return _inventory_plugin(path, f"plugin:{path}")
    raise AgentPackError(f"cannot classify source locator: {locator}")


def _add_duplicate_issues(inventory: Inventory) -> None:
    for label, values in (
        ("skill", [item.name for item in inventory.skills]),
        ("MCP", [item.name for item in inventory.mcps]),
    ):
        seen: set[str] = set()
        duplicates: set[str] = set()
        for value in values:
            if value in seen:
                duplicates.add(value)
            seen.add(value)
        for value in sorted(duplicates):
            inventory.issues.append(f"duplicate {label} name: {value}")


def _copy_skill(source: Path, destination: Path, *, allowed_root: Path | None = None) -> None:
    source = source.resolve()
    boundary = (allowed_root or source).resolve()
    for base, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = [name for name in dirs if name not in COPY_IGNORE]
        base_path = Path(base)
        for name in [*dirs, *files]:
            candidate = base_path / name
            if candidate.is_symlink():
                resolved = candidate.resolve()
                try:
                    resolved.relative_to(boundary)
                except ValueError as exc:
                    raise AgentPackError(f"skill symlink escapes allowed source root: {candidate}") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        shutil.copytree(
            source,
            temp,
            dirs_exist_ok=True,
            symlinks=False,
            ignore=lambda _base, names: {name for name in names if name in COPY_IGNORE},
        )
        if destination.exists() or destination.is_symlink():
            raise AgentPackError(f"destination already exists: {destination}")
        os.replace(temp, destination)
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def _load_destination_mcp(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"$schema": AGENT_PLUGIN_MCP_SCHEMA, "mcpServers": {}}
    payload = _read_jsonc(path) if path.suffix == ".jsonc" else _read_json(path)
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        raise AgentPackError(f"destination mcpServers must be an object: {path}")
    if path.suffix != ".jsonc":
        payload["$schema"] = AGENT_PLUGIN_MCP_SCHEMA
    return payload


def transfer_to_project(
    source: Inventory,
    destination_project: Path,
    skill_names: Iterable[str],
    mcp_names: Iterable[str],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    layout = load_project_layout(destination_project)
    requested_skills = set(skill_names)
    requested_mcps = set(mcp_names)
    selected_skills = [item for item in source.skills if item.name in requested_skills]
    selected_mcps = [item for item in source.mcps if item.name in requested_mcps]
    missing_skills = requested_skills - {item.name for item in selected_skills}
    missing_mcps = requested_mcps - {item.name for item in selected_mcps}
    if missing_skills or missing_mcps:
        raise AgentPackError(
            "requested items missing: "
            + ", ".join(sorted(missing_skills | missing_mcps))
        )
    blocked = [
        f"skill:{item.name}" for item in selected_skills if not item.portable
    ] + [
        f"mcp:{item.name}" for item in selected_mcps if not item.portable
    ]
    if blocked:
        raise AgentPackError("non-portable or secret-bearing items blocked: " + ", ".join(blocked))

    operations: list[dict[str, str]] = []
    for item in selected_skills:
        destination = layout.destination_skill_root / item.name
        if destination.exists() or destination.is_symlink():
            raise AgentPackError(f"destination already exists: {destination}")
        operations.append({"action": "copy-skill", "name": item.name, "to": str(destination)})

    mcp_payload = _load_destination_mcp(layout.destination_mcp)
    servers = mcp_payload.setdefault("mcpServers", {})
    for item in selected_mcps:
        if item.name in servers:
            raise AgentPackError(f"MCP destination already exists: {item.name}")
        operations.append({"action": "merge-mcp", "name": item.name, "to": str(layout.destination_mcp)})

    if apply:
        for item in selected_skills:
            _copy_skill(Path(item.path), layout.destination_skill_root / item.name)
        for item in selected_mcps:
            servers[item.name] = item.config
        if selected_mcps:
            if layout.destination_mcp.suffix == ".jsonc":
                raise AgentPackError("writing legacy JSONC destinations is intentionally unsupported")
            _write_json_atomic(layout.destination_mcp, mcp_payload)
    return {"ok": True, "applied": apply, "operations": operations}


def _mcp_allowed(layout: ProjectLayout, name: str, client: str) -> bool:
    policy = layout.mcp_client_policy.get(name, {})
    include = policy.get("include", ())
    exclude = policy.get("exclude", ())
    return (not include or client in include) and client not in exclude


def _env_references(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(ENV_REFERENCE_RE.findall(value)) - RESERVED_ENV_PLACEHOLDERS
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_env_references(item))
        return result
    if isinstance(value, dict):
        result: set[str] = set()
        for item in value.values():
            result.update(_env_references(item))
        return result
    return set()


def _dotenv_keys(path: Path) -> set[str]:
    if path.is_symlink() or not path.is_file():
        raise AgentPackError(f"credential env file missing or unsafe: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise AgentPackError(f"credential env file must be mode 0600 or stricter: {path}")
    keys: set[str] = set()
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, _value = line.partition("=")
            key = key.strip()
            if separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                keys.add(key)
    except (OSError, UnicodeDecodeError) as exc:
        raise AgentPackError(f"cannot inspect credential env file {path}: {exc}") from exc
    return keys


def _portable_package_from_inventory(
    inventory: Inventory,
    package_name: str,
    destination: Path,
    *,
    client: str | None = None,
) -> None:
    invalid = [item.name for item in inventory.skills if not item.portable]
    invalid += [
        item.name for item in inventory.mcps
        if item.authority == "portable" and not item.portable
    ]
    if invalid:
        raise AgentPackError("cannot package non-portable items: " + ", ".join(sorted(invalid)))
    project_root: Path | None = None
    layout: ProjectLayout | None = None
    if inventory.source.startswith("project:"):
        project_root = Path(inventory.source.split(":", 1)[1])
        layout = load_project_layout(project_root)
    destination.mkdir(parents=True, exist_ok=False)
    _write_json_atomic(destination / "plugin.json", {
        "$schema": AGENT_PLUGIN_SCHEMA,
        "name": package_name,
        "version": "1.0.0",
        "description": f"Project capabilities exported by agent-pack ({package_name}).",
        "keywords": ["agent-pack", "agent-skills", "mcp"],
    })
    skills_root = destination / "skills"
    skills_root.mkdir()
    for item in inventory.skills:
        _copy_skill(Path(item.path), skills_root / item.name, allowed_root=project_root)

    def project_mcp_config(item: McpRecord) -> dict[str, Any]:
        source_path = Path(item.source)

        def expand(value: Any) -> Any:
            if isinstance(value, str):
                if "__PROJECT_DIR__" in value:
                    if not source_path.is_file():
                        raise AgentPackError(f"cannot resolve __PROJECT_DIR__ for MCP {item.name}")
                    value = value.replace("__PROJECT_DIR__", str(source_path.parent.resolve()))
                if "__REPO_ROOT__" in value:
                    if project_root is None:
                        raise AgentPackError(f"cannot resolve __REPO_ROOT__ for MCP {item.name}")
                    value = value.replace("__REPO_ROOT__", str(project_root.resolve()))
                return value
            if isinstance(value, list):
                return [expand(child) for child in value]
            if isinstance(value, dict):
                return {key: expand(child) for key, child in value.items()}
            return value

        return expand(item.config)

    selected_mcps = [
        item for item in inventory.mcps
        if item.portable and (client is None or layout is None or _mcp_allowed(layout, item.name, client))
    ]
    required_env: set[str] = set()
    for item in selected_mcps:
        required_env.update(_env_references(item.config))
    if required_env:
        if layout is None or layout.credential_env_file is None:
            raise AgentPackError(
                "MCP environment references require capabilities.credential_env_file: "
                + ", ".join(sorted(required_env))
            )
        missing = required_env - _dotenv_keys(layout.credential_env_file)
        if missing:
            raise AgentPackError("credential env file is missing keys: " + ", ".join(sorted(missing)))

    rendered_mcps: dict[str, dict[str, Any]] = {}
    launcher_copied = False
    for item in selected_mcps:
        config = project_mcp_config(item)
        references = _env_references(config)
        if not references:
            rendered_mcps[item.name] = config
            continue
        if config.get("type") != "stdio":
            raise AgentPackError(f"environment-referenced remote MCP is unsupported: {item.name}")
        runtime_root = destination / "runtime"
        if not launcher_copied:
            runtime_root.mkdir(exist_ok=True)
            shutil.copy2(Path(__file__).with_name("mcp_env_launcher.py"), runtime_root / "mcp_env_launcher.py")
            launcher_copied = True
        config_name = hashlib.sha256(item.name.encode("utf-8")).hexdigest()[:16] + ".json"
        _write_json_atomic(runtime_root / config_name, config)
        assert layout is not None and layout.credential_env_file is not None
        rendered_mcps[item.name] = {
            "type": "stdio",
            "command": "python3",
            "args": [
                "${PLUGIN_ROOT}/runtime/mcp_env_launcher.py",
                "--env-file",
                str(layout.credential_env_file.resolve()),
                "--config",
                f"${{PLUGIN_ROOT}}/runtime/{config_name}",
            ],
        }

    _write_json_atomic(destination / "mcp.json", {
        "$schema": AGENT_PLUGIN_MCP_SCHEMA,
        "mcpServers": rendered_mcps,
    })
    manifest = _read_json(destination / "plugin.json")
    content_fingerprint = _tree_fingerprint(destination)
    manifest["version"] = f"1.0.0+project.{content_fingerprint[:12]}"
    _write_json_atomic(destination / "plugin.json", manifest)


def _codex_adapter(package_root: Path) -> None:
    manifest = _read_json(package_root / "plugin.json")
    codex_dir = package_root / ".codex-plugin"
    codex_dir.mkdir(exist_ok=True)
    codex_manifest_path = codex_dir / "plugin.json"
    if codex_manifest_path.exists():
        codex_manifest = _read_json(codex_manifest_path)
    else:
        codex_manifest = {}
    # Identity fields are owned by the portable plugin.json and are always
    # regenerated here so the Codex adapter can never drift from the
    # authoritative manifest (only Codex-specific presentation such as the
    # `interface` block is preserved as authored).
    codex_manifest["name"] = manifest["name"]
    codex_manifest["version"] = manifest.get("version", "1.0.0")
    codex_manifest.setdefault(
        "description", manifest.get("description", "Project capabilities")
    )
    codex_manifest.setdefault("skills", "./skills/")
    mcp_path = package_root / "mcp.json"
    if not mcp_path.exists():
        _write_json_atomic(codex_manifest_path, codex_manifest)
        return
    codex_manifest.setdefault("mcpServers", "./.mcp.json")
    _write_json_atomic(codex_manifest_path, codex_manifest)
    mcp = _read_json(mcp_path)
    codex_mcps: dict[str, Any] = {}
    for name, config in mcp.get("mcpServers", {}).items():
        # Codex reads the portable mcp.json (${PLUGIN_ROOT}); this projection is
        # what Claude Code and Cursor load, and they only expand CLAUDE_* names.
        item = _claude_plugin_placeholders(dict(config))
        mcp_type = item.pop("type", None)
        if mcp_type == "streamable-http":
            item["type"] = "http"
        elif mcp_type == "sse":
            item["type"] = "sse"
        codex_mcps[name] = item
    _write_json_atomic(package_root / ".mcp.json", {"mcpServers": codex_mcps})


def _claude_plugin_placeholders(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("${PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}").replace(
            "${PLUGIN_DATA}", "${CLAUDE_PLUGIN_DATA}"
        )
    if isinstance(value, list):
        return [_claude_plugin_placeholders(child) for child in value]
    if isinstance(value, dict):
        return {key: _claude_plugin_placeholders(child) for key, child in value.items()}
    return value


def _package_name(root: Path) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-")
    name = name or "project-capabilities"
    if not PLUGIN_NAME_RE.fullmatch(name):
        raise AgentPackError(f"cannot derive portable package name from {root.name!r}")
    return name


def _client_package_destination(client: str, home: Path, package_name: str) -> Path:
    if client == "cursor":
        return home / ".cursor" / "plugins" / "local" / package_name
    if client == "codex":
        return home / ".agents" / "plugins" / "plugins" / package_name
    if client == "claude":
        return _claude_plugin_records(home) / package_name
    raise AgentPackError(f"sync adapter not implemented for client: {client}")


def _tree_fingerprint(root: Path) -> str:
    """Return a stable content fingerprint without following symlinks."""
    digest = hashlib.sha256()
    if root.is_symlink() or not root.is_dir():
        raise AgentPackError(f"expected a real directory: {root}")
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in COPY_IGNORE)
        for name in sorted(files):
            path = Path(base) / name
            relative = path.relative_to(root)
            if path.is_symlink():
                raise AgentPackError(f"package contains symlink: {path}")
            digest.update(str(relative).encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError as exc:
                raise AgentPackError(f"cannot fingerprint {path}: {exc}") from exc
            digest.update(b"\0")
    return digest.hexdigest()


def _source_matches_destination(source: Path, destination: Path) -> bool:
    if source.is_symlink() or destination.is_symlink() or not destination.is_dir():
        return False
    for base, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = [name for name in dirs if name not in COPY_IGNORE]
        for name in files:
            path = Path(base) / name
            if path.is_symlink():
                return False
            target = destination / path.relative_to(source)
            try:
                if target.is_symlink() or not target.is_file() or target.read_bytes() != path.read_bytes():
                    return False
            except OSError:
                return False
    return True


def _source_matches_client_destination(
    source: Path,
    destination: Path,
    client: str,
) -> bool:
    if client != "codex":
        return _source_matches_destination(source, destination)
    with tempfile.TemporaryDirectory(prefix="agent-pack-match-") as temporary:
        staged = Path(temporary) / source.name
        shutil.copytree(source, staged, symlinks=False, ignore=shutil.ignore_patterns(*COPY_IGNORE))
        _codex_adapter(staged)
        return _source_matches_destination(staged, destination)


def _build_package(inventory: Inventory, package_name: str, parent: Path, client: str) -> Path:
    package = parent / package_name
    _portable_package_from_inventory(inventory, package_name, package, client=client)
    if client == "codex":
        _codex_adapter(package)
    return package


def install_plugin(
    plugin: Path,
    client: str,
    *,
    home: Path | None = None,
    apply: bool = False,
    replace: bool = False,
    recovery_root: Path | None = None,
) -> dict[str, Any]:
    """Bootstrap a portable plugin into a supported client without overwriting."""
    plugin = plugin.expanduser().resolve()
    manifest, issues = validate_plugin_manifest(plugin)
    if issues:
        raise AgentPackError("invalid plugin: " + "; ".join(issues))
    name = manifest["name"]
    _tree_fingerprint(plugin)
    home = (home or Path.home()).expanduser().resolve()
    if client == "claude":
        return _install_plugin_claude(
            plugin, name, home=home, apply=apply, replace=replace, recovery_root=recovery_root
        )
    destination = _client_package_destination(client, home, name)
    action = "install-plugin"
    if destination.exists() or destination.is_symlink():
        if not _source_matches_client_destination(plugin, destination, client):
            if not replace:
                raise AgentPackError(f"client package already exists with different content: {destination}")
            action = "replace-plugin"
        else:
            action = "keep-plugin"
    operation = {"action": action, "client": client, "name": name, "to": str(destination)}
    if apply:
        archived: Path | None = None
        if action in {"install-plugin", "replace-plugin"}:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if action == "replace-plugin":
                recovery_base = (recovery_root or home / ".agent-pack" / "recovery").expanduser().resolve()
                recovery_base.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
                archived = recovery_base / f"{stamp}-{client}-{name}" / "replaced-plugin" / name
                archived.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, archived)
            temp_parent = Path(tempfile.mkdtemp(prefix=".agent-pack-bootstrap-", dir=destination.parent))
            staged = temp_parent / name
            try:
                shutil.copytree(plugin, staged, symlinks=False, ignore=shutil.ignore_patterns(*COPY_IGNORE))
                if client == "codex":
                    _codex_adapter(staged)
                os.replace(staged, destination)
            except Exception:
                if archived is not None and archived.exists() and not destination.exists():
                    os.replace(archived, destination)
                raise
            finally:
                if temp_parent.exists():
                    shutil.rmtree(temp_parent)
        if client == "codex":
            _update_personal_marketplace(home, name)
    return {"ok": True, "applied": apply, "operations": [operation]}


def _claude_mcp_config(config: dict[str, Any], plugin_root: Path, data_root: Path) -> dict[str, Any]:
    """Project a portable MCP entry onto Claude Code's ``mcpServers`` schema."""

    def expand(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("${PLUGIN_ROOT}", str(plugin_root)).replace(
                "${PLUGIN_DATA}", str(data_root)
            )
        if isinstance(value, list):
            return [expand(child) for child in value]
        if isinstance(value, dict):
            return {key: expand(child) for key, child in value.items()}
        return value

    item = expand(dict(config))
    # Claude Code stdio entries have no working-directory field; plugin MCPs
    # must address their files through ${PLUGIN_ROOT} instead.
    item.pop("cwd", None)
    if item.get("type") == "streamable-http":
        item["type"] = "http"
    return item


def _install_plugin_claude(
    plugin: Path,
    name: str,
    *,
    home: Path,
    apply: bool,
    replace: bool,
    recovery_root: Path | None,
) -> dict[str, Any]:
    """Install a plugin into Claude Code as live links plus user-scope MCP entries.

    Claude Code has no Agent Plugins loader, so the plugin is projected onto the
    three things it does read: ``~/.claude/skills/<skill>`` (symlink to the
    source skill), ``~/.claude.json#mcpServers`` (user scope), and an install
    record ``~/.claude/agent-pack/plugins/<name>`` (symlink to the source root)
    that inventory uses to report the installed version.
    """
    record = _claude_plugin_records(home) / name
    links: list[tuple[str, str, Path, Path]] = [("plugin", name, record, plugin)]
    skills_root = plugin / "skills"
    if skills_root.is_dir() and not skills_root.is_symlink():
        for child in sorted(skills_root.iterdir(), key=lambda item: item.name):
            if child.is_dir() and not child.name.startswith(".") and (child / "SKILL.md").is_file():
                links.append(("skill", child.name, home / ".claude" / "skills" / child.name, child))

    config_path = _claude_user_config(home)
    servers: dict[str, dict[str, Any]] = {}
    mcp_path = plugin / "mcp.json"
    if mcp_path.is_file():
        data_root = home / ".claude" / "agent-pack" / "data" / name
        for server, raw in sorted(_read_json(mcp_path).get("mcpServers", {}).items()):
            parsed = _portable_mcp(server, raw)
            if not parsed.portable:
                raise AgentPackError(f"MCP {server} is not portable: " + "; ".join(parsed.issues))
            servers[server] = _claude_mcp_config(parsed.config, record, data_root)

    config: dict[str, Any] = {}
    if servers and config_path.exists():
        if config_path.is_symlink():
            raise AgentPackError(f"refusing to edit symlinked Claude config: {config_path}")
        config = _read_json(config_path)
    existing_servers = config.get("mcpServers", {})
    if not isinstance(existing_servers, dict):
        raise AgentPackError(f"mcpServers must be an object: {config_path}")

    operations: list[dict[str, str]] = []
    conflicts: list[str] = []
    for kind, item_name, path, target in links:
        if path.is_symlink() and path.resolve() == target.resolve():
            action = "keep"
        elif path.exists() or path.is_symlink():
            action = "replace"
            conflicts.append(f"{kind} {item_name}: {path}")
        else:
            action = "link"
        operations.append({"action": f"{action}-{kind}", "client": "claude", "name": item_name, "to": str(path)})
    for server, rendered in servers.items():
        current = existing_servers.get(server)
        if current is None:
            action = "add"
        elif current == rendered:
            action = "keep"
        else:
            action = "replace"
            conflicts.append(f"mcp {server}: {config_path}")
        operations.append({"action": f"{action}-mcp", "client": "claude", "name": server, "to": str(config_path)})
    if conflicts and not replace:
        raise AgentPackError("Claude client already has different content: " + "; ".join(conflicts))
    result: dict[str, Any] = {"ok": True, "applied": apply, "operations": operations}
    if not apply:
        return result

    transaction: Path | None = None
    if conflicts:
        recovery_base = (recovery_root or home / ".agent-pack" / "recovery").expanduser().resolve()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        transaction = recovery_base / f"{stamp}-claude-{name}"
        transaction.mkdir(parents=True, mode=0o700)
        result["recovery"] = str(transaction)
    moved: list[tuple[Path, Path]] = []
    created: list[Path] = []
    original_config = json.loads(json.dumps(config))
    config_written = False
    try:
        for (kind, item_name, path, target), operation in zip(links, operations):
            if operation["action"].startswith("keep-"):
                continue
            if operation["action"].startswith("replace-"):
                assert transaction is not None
                archived = transaction / f"replaced-{kind}s" / item_name
                archived.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, archived)
                moved.append((archived, path))
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, path, target_is_directory=True)
            created.append(path)
        changed = {
            server: rendered for server, rendered in servers.items()
            if existing_servers.get(server) != rendered
        }
        if changed:
            replaced = {server: existing_servers[server] for server in changed if server in existing_servers}
            if replaced:
                assert transaction is not None
                _write_json_atomic(transaction / "replaced-mcp.json", {"mcpServers": replaced}, mode=0o600)
            config.setdefault("mcpServers", {}).update(changed)
            mode = stat.S_IMODE(config_path.stat().st_mode) if config_path.exists() else 0o600
            _write_json_atomic(config_path, config, mode=mode)
            config_written = True
    except Exception as exc:
        if config_written:
            _write_json_atomic(config_path, original_config, mode=0o600)
        for path in reversed(created):
            if path.is_symlink():
                path.unlink()
        for archived, original in reversed(moved):
            if archived.exists() and not original.exists():
                os.replace(archived, original)
        raise AgentPackError(f"claude install rolled back: {exc}") from exc
    return result


def _component_state(
    authority: Inventory,
    client_inventory: Inventory,
    *,
    client: str,
    layout: ProjectLayout,
) -> dict[str, list[dict[str, Any]]]:
    states: dict[str, list[dict[str, Any]]] = {
        "exact": [], "conflict": [], "missing": [], "native_only": [], "blocked": []
    }
    expected_skills = {item.name: item for item in authority.skills}
    actual_skills: dict[str, list[SkillRecord]] = {}
    for item in client_inventory.skills:
        actual_skills.setdefault(item.name, []).append(item)
    for name, expected in sorted(expected_skills.items()):
        actual = actual_skills.pop(name, [])
        if not actual:
            states["missing"].append({"kind": "skill", "name": name})
            continue
        try:
            expected_hash = _tree_fingerprint(Path(expected.path))
            hashes = [_tree_fingerprint(Path(item.path)) for item in actual]
        except AgentPackError:
            states["conflict"].append({"kind": "skill", "name": name, "copies": len(actual)})
            continue
        target = "exact" if len(actual) == 1 and hashes[0] == expected_hash else "conflict"
        states[target].append({"kind": "skill", "name": name, "copies": len(actual)})
    for name, values in sorted(actual_skills.items()):
        states["native_only"].append({"kind": "skill", "name": name, "copies": len(values)})

    expected_mcps = {
        item.name: item for item in authority.mcps
        if item.portable and _mcp_allowed(layout, item.name, client)
    }
    blocked_mcps = {
        item.name: item for item in authority.mcps
        if not item.portable and _mcp_allowed(layout, item.name, client)
    }
    actual_mcps: dict[str, list[McpRecord]] = {}
    for item in client_inventory.mcps:
        actual_mcps.setdefault(item.name, []).append(item)
    for name, expected in sorted(expected_mcps.items()):
        actual = actual_mcps.pop(name, [])
        if not actual:
            states["missing"].append({"kind": "mcp", "name": name})
        elif len(actual) == 1 and actual[0].config == expected.config:
            states["exact"].append({"kind": "mcp", "name": name, "copies": 1})
        else:
            states["conflict"].append({"kind": "mcp", "name": name, "copies": len(actual)})
    for name, item in sorted(blocked_mcps.items()):
        actual = actual_mcps.pop(name, [])
        states["blocked"].append({
            "kind": "mcp", "name": name, "authority": item.authority,
            "installed": bool(actual), "issues": list(item.issues),
        })
    for name, values in sorted(actual_mcps.items()):
        states["native_only"].append({"kind": "mcp", "name": name, "copies": len(values)})
    return states


def reconcile_project(
    project: Path,
    client: str,
    *,
    home: Path | None = None,
    retire_skills: Iterable[str] = (),
    retire_plugins: Iterable[str] = (),
    recovery_root: Path | None = None,
    apply: bool = False,
    retire_only: bool = False,
) -> dict[str, Any]:
    """Reconcile one project package and explicitly selected legacy components.

    ``retire_only`` archives the selected legacy components without installing
    or replacing the project package itself.
    """
    layout = load_project_layout(project)
    authority = inventory_project(layout.root)
    package_name = _package_name(layout.root)
    home = (home or Path.home()).expanduser().resolve()
    destination = _client_package_destination(client, home, package_name)
    client_inventory = inventory_client(client, home)
    states = _component_state(authority, client_inventory, client=client, layout=layout)

    retirement_targets: list[tuple[str, str, Path]] = []
    actual_skills = {item.name: Path(item.path) for item in client_inventory.skills}
    actual_plugins = {
        str(item.get("name")): Path(str(item.get("path")))
        for item in client_inventory.plugins if item.get("name") and item.get("path")
    }
    for name in sorted(set(retire_skills)):
        path = actual_skills.get(name)
        if path is None:
            raise AgentPackError(f"retire Skill not installed: {name}")
        if destination.exists():
            try:
                path.resolve().relative_to(destination.resolve())
            except ValueError:
                pass
            else:
                raise AgentPackError(f"cannot retire Skill inside active project package: {name}")
        retirement_targets.append(("skill", name, path))
    for name in sorted(set(retire_plugins)):
        path = actual_plugins.get(name)
        if path is None:
            raise AgentPackError(f"retire plugin not installed: {name}")
        retirement_targets.append(("plugin", name, path))

    if retire_only and not retirement_targets:
        raise AgentPackError("retire-only reconcile needs at least one --retire-skill or --retire-plugin")
    package_action = "install-plugin"
    needs_package = not retire_only
    if retire_only:
        package_action = "skip-plugin"
    elif destination.exists() or destination.is_symlink():
        with tempfile.TemporaryDirectory(prefix="agent-pack-plan-") as temporary:
            expected = _build_package(authority, package_name, Path(temporary), client)
            if _tree_fingerprint(expected) == _tree_fingerprint(destination):
                package_action = "keep-plugin"
                needs_package = False
            else:
                package_action = "replace-plugin"

    operations: list[dict[str, str]] = [] if retire_only else [
        {"action": package_action, "client": client, "name": package_name, "to": str(destination)}
    ]
    operations.extend(
        {"action": "archive-" + kind, "name": name, "from": str(path)}
        for kind, name, path in retirement_targets
    )
    if not apply:
        return {"ok": True, "applied": False, "states": states, "operations": operations}

    recovery_base = (recovery_root or home / ".agent-pack" / "recovery").expanduser().resolve()
    recovery_base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    transaction = recovery_base / f"{stamp}-{client}-{package_name}"
    transaction.mkdir(mode=0o700)
    moved: list[tuple[Path, Path]] = []
    installed = False
    try:
        if needs_package:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                archived = transaction / "replaced-plugin" / package_name
                archived.parent.mkdir(parents=True)
                os.replace(destination, archived)
                moved.append((archived, destination))
            staging_parent = Path(tempfile.mkdtemp(prefix=".agent-pack-", dir=destination.parent))
            try:
                staged = _build_package(authority, package_name, staging_parent, client)
                os.replace(staged, destination)
                installed = True
            finally:
                if staging_parent.exists():
                    shutil.rmtree(staging_parent)
        for kind, name, path in retirement_targets:
            if path == destination:
                raise AgentPackError(f"cannot retire active project package: {name}")
            archived = transaction / (kind + "s") / name
            archived.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, archived)
            moved.append((archived, path))
        if client == "codex" and needs_package:
            _update_personal_marketplace(home, package_name)
    except Exception as exc:
        if installed and destination.exists():
            shutil.rmtree(destination)
        for archived, original in reversed(moved):
            if archived.exists() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                os.replace(archived, original)
        raise AgentPackError(f"reconcile rolled back: {exc}") from exc
    return {
        "ok": True, "applied": True, "states": states, "operations": operations,
        "recovery": str(transaction),
    }


def sync_project(
    project: Path,
    client: str,
    *,
    home: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    return reconcile_project(project, client, home=home, apply=apply)


def _update_personal_marketplace(home: Path, package_name: str) -> None:
    marketplace = home / ".agents" / "plugins" / "marketplace.json"
    if marketplace.exists():
        payload = _read_json(marketplace)
    else:
        payload = {"name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}
    plugins = payload.setdefault("plugins", [])
    if not isinstance(plugins, list):
        raise AgentPackError(f"marketplace plugins must be an array: {marketplace}")
    plugins[:] = [item for item in plugins if not isinstance(item, dict) or item.get("name") != package_name]
    plugins.append({
        "name": package_name,
        "source": {"source": "local", "path": f"./.agents/plugins/plugins/{package_name}"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Developer Tools",
    })
    _write_json_atomic(marketplace, payload)


def doctor(project: Path) -> dict[str, Any]:
    layout = load_project_layout(project)
    inventory = inventory_project(layout.root)
    issues = list(inventory.issues)
    warnings: list[str] = []
    for item in inventory.skills:
        issues.extend(f"skill {item.name}: {issue}" for issue in item.issues)
    for item in inventory.mcps:
        target = warnings if item.authority == "native" else issues
        target.extend(f"MCP {item.name}: {issue}" for issue in item.issues)
    configured_names = {item.name for item in inventory.mcps}
    unknown_policies = set(layout.mcp_client_policy) - configured_names
    issues.extend(f"MCP client policy references unknown server: {name}" for name in sorted(unknown_policies))
    for plugin in inventory.plugins:
        adapter_path = Path(plugin["path"]) / ".codex-plugin" / "plugin.json"
        if not adapter_path.is_file() or adapter_path.is_symlink():
            continue
        try:
            adapter_version = _read_json(adapter_path).get("version")
        except AgentPackError as exc:
            issues.append(str(exc))
            continue
        if adapter_version != plugin["version"]:
            issues.append(
                f"plugin {plugin['name']}: Codex adapter version {adapter_version!r} "
                f"does not match plugin.json version {plugin['version']!r}"
            )
    required_env: set[str] = set()
    for item in inventory.mcps:
        if item.portable:
            required_env.update(_env_references(item.config))
    if required_env:
        if layout.credential_env_file is None:
            issues.append("MCP environment references require capabilities.credential_env_file")
        else:
            try:
                missing = required_env - _dotenv_keys(layout.credential_env_file)
                issues.extend(f"credential env file missing key: {name}" for name in sorted(missing))
            except AgentPackError as exc:
                issues.append(str(exc))
    return {
        "ok": not issues,
        "source": inventory.source,
        "counts": {
            "plugins": len(inventory.plugins),
            "skills": len(inventory.skills),
            "mcps": len(inventory.mcps),
        },
        "issues": sorted(set(issues)),
        "warnings": sorted(set(warnings)),
    }


def _print(value: dict[str, Any], output: str) -> None:
    if output == "json":
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    print(f"ok: {str(value.get('ok', True)).lower()}")
    if "applied" in value:
        print(f"applied: {str(value['applied']).lower()}")
    counts = value.get("counts")
    if isinstance(counts, dict):
        print("counts: " + ", ".join(f"{key}={number}" for key, number in counts.items()))
    for operation in value.get("operations", []):
        target = operation.get("to") or operation.get("from", "")
        print(f"- {operation['action']}: {operation.get('name', '')} -> {target}")
    for issue in value.get("issues", []):
        print(f"! {issue}")
    for warning in value.get("warnings", []):
        print(f"~ {warning}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-pack")
    parser.add_argument("--output", choices=("text", "json"), default="text")
    sub = parser.add_subparsers(dest="command", required=True)

    inventory_parser = sub.add_parser("inventory")
    inventory_parser.add_argument("--source", required=True)
    inventory_parser.add_argument("--home", type=Path)

    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--project", type=Path, default=Path.cwd())

    transfer_parser = sub.add_parser("transfer")
    transfer_parser.add_argument("--from", dest="source", required=True)
    transfer_parser.add_argument("--to-project", type=Path, required=True)
    transfer_parser.add_argument("--skill", action="append", default=[])
    transfer_parser.add_argument("--mcp", action="append", default=[])
    transfer_parser.add_argument("--home", type=Path)
    transfer_parser.add_argument("--apply", action="store_true")

    sync_parser = sub.add_parser("sync")
    sync_parser.add_argument("--project", type=Path, default=Path.cwd())
    sync_parser.add_argument("--client", choices=("codex", "cursor"), required=True)
    sync_parser.add_argument("--home", type=Path)
    sync_parser.add_argument("--apply", action="store_true")

    bootstrap_parser = sub.add_parser("bootstrap")
    bootstrap_parser.add_argument("--plugin", type=Path, required=True)
    bootstrap_parser.add_argument("--client", choices=("codex", "cursor", "claude"), required=True)
    bootstrap_parser.add_argument("--home", type=Path)
    bootstrap_parser.add_argument("--apply", action="store_true")
    bootstrap_parser.add_argument("--replace", action="store_true")
    bootstrap_parser.add_argument("--recovery-root", type=Path)

    reconcile_parser = sub.add_parser("reconcile")
    reconcile_parser.add_argument("--project", type=Path, default=Path.cwd())
    reconcile_parser.add_argument("--client", choices=("codex", "cursor"), required=True)
    reconcile_parser.add_argument("--home", type=Path)
    reconcile_parser.add_argument("--retire-skill", action="append", default=[])
    reconcile_parser.add_argument("--retire-plugin", action="append", default=[])
    reconcile_parser.add_argument("--recovery-root", type=Path)
    reconcile_parser.add_argument(
        "--retire-only", action="store_true",
        help="archive the selected legacy components without installing the project package",
    )
    reconcile_parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            result = inventory_source(args.source, args.home).to_dict()
            result["ok"] = not result["issues"]
        elif args.command == "doctor":
            result = doctor(args.project)
        elif args.command == "transfer":
            if not args.skill and not args.mcp:
                raise AgentPackError("select at least one --skill or --mcp")
            result = transfer_to_project(
                inventory_source(args.source, args.home),
                args.to_project,
                args.skill,
                args.mcp,
                apply=args.apply,
            )
        elif args.command == "sync":
            result = sync_project(
                args.project, args.client, home=args.home, apply=args.apply
            )
        elif args.command == "bootstrap":
            result = install_plugin(
                args.plugin,
                args.client,
                home=args.home,
                apply=args.apply,
                replace=args.replace,
                recovery_root=args.recovery_root,
            )
        else:
            result = reconcile_project(
                args.project,
                args.client,
                home=args.home,
                retire_skills=args.retire_skill,
                retire_plugins=args.retire_plugin,
                recovery_root=args.recovery_root,
                apply=args.apply,
                retire_only=args.retire_only,
            )
        _print(result, args.output)
        return 0 if result.get("ok", True) else 1
    except AgentPackError as exc:
        error = {"ok": False, "error": str(exc)}
        _print(error, args.output)
        if args.output == "text":
            print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
