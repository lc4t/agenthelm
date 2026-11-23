#!/usr/bin/env python3
"""Portable Agent Plugin / Skill / MCP inventory and transfer runtime.

The module intentionally uses only the Python standard library so it can run
from an Agent Plugin without creating a package-local virtual environment.
Writes are always opt-in through ``apply=True`` and collisions fail closed.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
from urllib.parse import parse_qsl, urlsplit
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
# Known credential prefixes; only a long identifier counts, so SK_KEY or GH_TOKEN remain valid variable names.
TOKEN_PREFIX_RE = re.compile(r"^(?:gh[pousr]_|github_pat_|sk[-_]|pk[-_]|rk[-_]|xox[abprs]|glpat|AKIA|eyJ|ya29)", re.IGNORECASE)


def looks_like_token(name: str) -> bool:
    """True when something spelled as an environment variable *name* is more likely a pasted credential value.

    Real variable names are short and conventional (``EXAMPLE_MCP_TOKEN``). A well-known credential prefix on a long
    identifier, a long mixed-case alphanumeric run, or an implausibly long name is treated as a credential.
    """
    if len(name) > 64:
        return True
    if len(name) >= 20 and TOKEN_PREFIX_RE.match(name):
        return True
    return (len(name) >= 24 and any(c.islower() for c in name) and any(c.isupper() for c in name)
            and any(c.isdigit() for c in name))


# A path-shaped argument (absolute, ``~``, drive letter, or one of our own path placeholders) names
# a location. Directory names such as ``credential`` or ``token`` in it say nothing about a secret
# value (#90), so only the value-shaped patterns apply to such arguments.
PATH_SHAPED_RE = re.compile(r"^(?:[/~]|[A-Za-z]:[\\/]|__PROJECT_DIR__|__REPO_ROOT__|\$\{(?:PLUGIN_ROOT|PLUGIN_DATA|HOME)\})")


def _secret_like_arg(text: str) -> bool:
    if SECRET_VALUE_RE.search(text):
        return True
    return not PATH_SHAPED_RE.match(text) and bool(SECRET_NAME_RE.search(text))


def _safe_env_reference(text: str) -> bool:
    """A value made of ``${NAME}`` references with nothing credential-like in the names or around them."""
    references = ENV_REFERENCE_RE.findall(text)
    return (bool(references) and not any(looks_like_token(name) for name in references)
            and not SECRET_VALUE_RE.search(ENV_REFERENCE_RE.sub("", text)))
# Placeholders the client resolves at launch time (immutable package root and
# client-managed writable state). They are never credentials and must not
# trigger the credential-env-file requirement or env-launcher wrapping.
RESERVED_ENV_PLACEHOLDERS = {"PLUGIN_ROOT", "PLUGIN_DATA"}
# Process / system variables. They describe the machine, not a credential, so they are never
# required in the credential env file. ``sync`` expands them to this machine's values while
# rendering the package (each machine syncs for itself), so ``${HOME}/tools/x.js`` is portable.
SYSTEM_ENV_PLACEHOLDERS = frozenset({
    "HOME", "USER", "LOGNAME", "TMPDIR",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
})
PLUGIN_ROOT_REF_RE = re.compile(r"\$\{PLUGIN_ROOT\}(/[\w./@+-]*)?")
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
    root: str = ""  # directory ``${PLUGIN_ROOT}`` means for this entry (its own source's plugin directory)


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
    # Scope declaration (#67): ``user`` serves every project, ``project`` only this one. Absent keys mean ``user``.
    default_scope: str = "user"
    mcp_scope: dict[str, str] = field(default_factory=dict)
    skill_scope: dict[str, str] = field(default_factory=dict)


SCOPES = ("user", "project")


def project_write_supported(client: str) -> bool:
    """Whether a sync can write a project-level store for this client. The one place that decides it; the answer is
    the ``project_mcp_write`` capability of the client's driver (``agent_pack_clients``), not a client name."""
    import agent_pack_clients  # noqa: PLC0415 - shipped next to this file; imported late (python3 -I path set in main)
    return agent_pack_clients.supports(client, "project_mcp_write")


def declared_scope(layout: ProjectLayout, kind: str, name: str) -> str:
    """The scope the project declares for one Skill (``kind="skill"``) or MCP server (``kind="mcp"``)."""
    mapping = layout.skill_scope if kind == "skill" else layout.mcp_scope
    return mapping.get(name, layout.default_scope)


def _parse_scopes(config: dict[str, Any]) -> tuple[str, dict[str, str], dict[str, str]]:
    default = config.get("default_scope", "user")
    if default not in SCOPES:
        raise AgentPackError(f"capabilities.default_scope must be one of {', '.join(SCOPES)}: {default!r}")
    parsed: dict[str, dict[str, str]] = {}
    for key in ("mcp_scope", "skill_scope"):
        value = config.get(key, {})
        if not isinstance(value, dict):
            raise AgentPackError(f"capabilities.{key} must be an object mapping names to user|project")
        for name, scope in value.items():
            if not isinstance(name, str) or not name or scope not in SCOPES:
                raise AgentPackError(
                    f"capabilities.{key}.{name} must be one of {', '.join(SCOPES)}: {scope!r}")
        parsed[key] = dict(value)
    return default, parsed["mcp_scope"], parsed["skill_scope"]


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
    default_scope, mcp_scope, skill_scope = _parse_scopes(config)
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
        default_scope=default_scope,
        mcp_scope=mcp_scope,
        skill_scope=skill_scope,
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
    item = json.loads(json.dumps(raw))
    # Cursor spells process environment interpolation as ${env:NAME}.
    if isinstance(item.get("headers"), dict):
        item["headers"] = {key: re.sub(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}", r"${\1}", value)
                           if isinstance(value, str) else value for key, value in item["headers"].items()}
    issues: list[str] = []
    # Canonical portable form uses headers with literal environment references.
    # Codex's native fields describe names, never values; normalize on import.
    for source_field in ("http_headers", "env_http_headers"):
        value = item.pop(source_field, None)
        if value is None:
            continue
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                 for k, v in value.items()):
            issues.append(f"{source_field} must be a string map")
            continue
        headers = dict(item.get("headers", {})) if isinstance(item.get("headers", {}), dict) else {}
        for key, value in value.items():
            if source_field == "env_http_headers":
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                    issues.append(f"invalid environment variable name in {source_field}.{key}")
                    continue
                if looks_like_token(value):
                    issues.append(f"{source_field}.{key} looks like a credential, not a variable name")
                    continue
                value = "${" + value + "}"
            if key in headers and headers[key] != value:
                issues.append(f"conflicting header: {key}")
            headers[key] = value
        item["headers"] = headers
    bearer = item.pop("bearer_token_env_var", None)
    if bearer is not None:
        if not isinstance(bearer, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", bearer):
            issues.append("bearer_token_env_var must be an environment variable name")
        elif looks_like_token(bearer):
            issues.append("bearer_token_env_var looks like a credential, not a variable name")
        else:
            headers = dict(item.get("headers", {})) if isinstance(item.get("headers", {}), dict) else {}
            auth = next((key for key in headers if key.lower() == "authorization"), "Authorization")
            value = "Bearer ${" + bearer + "}"
            if auth in headers and headers[auth] != value:
                issues.append("conflicting Authorization header")
            headers[auth] = value
            item["headers"] = headers
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
            if not isinstance(key, str) or not isinstance(value, str):
                issues.append(f"{container_name} must be a string map")
                continue
            safe_reference = _safe_env_reference(str(value))
            if (SECRET_NAME_RE.search(str(key)) or SECRET_VALUE_RE.search(str(value))) and not safe_reference:
                issues.append(f"possible secret in {container_name}.{key}")
            elif any(looks_like_token(name) for name in ENV_REFERENCE_RE.findall(str(value))):
                issues.append(f"possible secret in {container_name}.{key}")
    args = item.get("args", [])
    if isinstance(args, list):
        for index, value in enumerate(args):
            rendered = str(value)
            safe_reference = _safe_env_reference(rendered)
            if _secret_like_arg(rendered) and not safe_reference:
                issues.append(f"possible secret reference in args[{index}]")
            elif any(looks_like_token(name) for name in ENV_REFERENCE_RE.findall(rendered)):
                issues.append(f"possible secret reference in args[{index}]")
    if mcp_type == "stdio":
        allowed = {"type", "command", "args", "env", "cwd"}
        if not isinstance(item.get("command"), str) or not item["command"]:
            issues.append("stdio command is required")
    else:
        allowed = {"type", "url", "headers"}
        if not isinstance(item.get("url"), str) or not item["url"]:
            issues.append("remote URL is required")
        else:
            try:
                url = urlsplit(item["url"])
                if url.username or url.password or any(SECRET_NAME_RE.search(key) and value
                                                       for key, value in parse_qsl(url.query)):
                    issues.append("possible secret in URL")
            except ValueError:
                issues.append("invalid remote URL")
            if ENV_REFERENCE_RE.search(item["url"]):
                issues.append("remote environment references must be declared in headers")
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
                    record.name, record.config, source, record.portable, record.issues, "portable",
                    str(plugin_root),
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
                    record.name, record.config, str(mcp_source), record.portable, record.issues, authority,
                    str(mcp_source.parent),
                ))
                known_mcp_names.add(name)
        except AgentPackError as exc:
            inventory.issues.append(str(exc))

    _add_duplicate_issues(inventory)
    return inventory


def _client_skill_roots(client: str, home: Path) -> list[Path]:
    """The client's primary user-level Skills directory, from the driver registry (the first user store it declares)."""
    import agent_pack_clients  # noqa: PLC0415 - shipped next to this file; imported late (python3 -I path set in main)
    template = agent_pack_clients.primary_skill_stores().get((client, "user"))
    if template is not None:
        return [home / template[2:]]
    if client == "codebuddy":  # not a registered client: read-only inventory support kept as it was
        return [home / ".codebuddy" / "skills"]
    raise AgentPackError(f"unsupported client: {client}")


def _registry():
    import agent_pack_clients  # noqa: PLC0415 - shipped next to this file; imported late (python3 -I path set in main)
    return agent_pack_clients


def package_parent(client: str, home: Path) -> Path:
    """The directory ``sync`` puts this client's project package (or its install record) in, from the driver's ``sync``
    declaration; the package name is appended by the caller."""
    parent = _registry().package_dir(client, home)
    if parent is None:
        raise AgentPackError(f"sync adapter not implemented for client: {client}")
    return parent


def user_mcp_file(client: str, home: Path) -> Path:
    """The client's user-level MCP file (the first user store its driver declares in ``stores.mcp``)."""
    path = _registry().user_mcp_file(client, home)
    if path is None:
        raise AgentPackError(f"sync adapter not implemented for client: {client}")
    return path


def mcp_root_key(client: str) -> str:
    """The key holding the servers in the client's user-level MCP file (the ``root_key`` of that store)."""
    store = _registry().user_mcp_store(client)
    if store is None:
        raise AgentPackError(f"sync adapter not implemented for client: {client}")
    return store["root_key"]


def native_package_client() -> str:
    """The client whose driver declares ``sync.write: native-package`` (the first one, in registry order)."""
    found = _registry().ids_with_sync("native-package")
    if not found:
        raise AgentPackError("no client declares a native-package sync")
    return found[0]


def _declared_write(client: str | None, flag: str) -> bool:
    """Whether the client's driver allows the ``sync`` write named by capability ``flag`` (``user_skill_write``,
    ``user_mcp_write``, ``project_skill_write``). A call without a client, or for a client with no ``sync`` declaration,
    is not limited by a driver."""
    registry = _registry()
    return client is None or registry.sync_spec(client) is None or registry.supports(client, flag)


def _client_plugin_roots(client: str, home: Path) -> list[Path]:
    parent = _registry().package_dir(client, home)
    return [parent] if parent is not None else []


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value.split("+", 1)[0])
    return tuple(int(number) for number in numbers)


def _marketplace_records_json(base: Path) -> list[Path]:
    """Layout ``records-json``: ``plugins/installed_plugins.json`` holds a record per plugin with its install path."""
    found: list[Path] = []
    try:
        records = _read_json(base / "plugins" / "installed_plugins.json").get("plugins", {})
    except AgentPackError:
        records = {}
    for entries in records.values() if isinstance(records, dict) else ():
        for entry in entries if isinstance(entries, list) else ():
            path = Path(str(entry.get("installPath", "")))
            if (path / "plugin.json").is_file():
                found.append(path)
    return found


def _marketplace_cache_versioned(base: Path) -> list[Path]:
    """Layout ``cache-dir-versioned``: ``plugins/cache/<market>/<plugin>/<version>/plugin.json``; the newest version counts."""
    found: list[Path] = []
    cache = base / "plugins" / "cache"
    if cache.is_dir() and not cache.is_symlink():
        for plugin_dir in sorted(path for market in cache.iterdir() if market.is_dir() for path in market.iterdir()):
            versions = [path for path in plugin_dir.iterdir() if (path / "plugin.json").is_file()] if plugin_dir.is_dir() else []
            if versions:
                found.append(max(versions, key=lambda path: (_version_tuple(path.name), path.name)))
    return found


MARKETPLACE_READERS = {"records-json": _marketplace_records_json, "cache-dir-versioned": _marketplace_cache_versioned}


def _client_marketplace_plugins(client: str, home: Path) -> list[Path]:
    """Plugin roots the client installed from marketplaces (not agent-pack bootstrap copies): the reader is the one named by
    the driver's ``distribution.installed_state``, reading under the client's directory at its default location."""
    registry = _registry()
    driver = registry.get(client)
    state = registry.distribution(driver).get("installed_state") if driver else None
    base = registry.default_config_dir(client, home)
    reader = MARKETPLACE_READERS.get(state)
    return reader(base) if reader is not None and base is not None else []


def _native_user_mcps(client: str, home: Path) -> dict[str, Any]:
    """Servers the client itself keeps in its user-level MCP configuration (hand-added ones included).

    Which file, in which format and under which key all come from the driver declaration; the reader is chosen by the
    store ``format``. A missing, symlinked or unreadable file reads as no servers."""
    import agent_pack_clients  # noqa: PLC0415 - shipped next to this file; imported late (python3 -I path set in main)
    import agent_pack_store_read  # noqa: PLC0415

    declared = next((store for store in agent_pack_clients.stores(client, "mcp") if store["scope"] == "user"), None)
    if declared is not None:
        path, fmt, root_key = home / declared["path"][2:], declared["format"], declared["root_key"]
    elif client == "codebuddy":  # not a registered client: read-only inventory support kept as it was
        path, fmt, root_key = home / ".codebuddy" / "mcp.json", "json", "mcpServers"
    else:
        return {}
    if not path.is_file() or path.is_symlink():
        return {}
    data, _reason = agent_pack_store_read.read_object(fmt, path)
    return agent_pack_store_read.servers(data, root_key) or {}


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
    servers = _native_user_mcps(client, home)
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
    update: bool = False,
    recovery_root: Path | None = None,
) -> dict[str, Any]:
    """Import selected capabilities; replacement requires both --update and --apply.

    Previews contain changed paths and hashes, never MCP values or file contents.
    Stage everything before moving originals; roll back the whole selection on failure.
    """
    layout = load_project_layout(destination_project)
    requested_skills, requested_mcps = set(skill_names), set(mcp_names)
    selected_skills = [item for item in source.skills if item.name in requested_skills]
    selected_mcps = [item for item in source.mcps if item.name in requested_mcps]
    missing = (requested_skills - {item.name for item in selected_skills}) | (requested_mcps - {item.name for item in selected_mcps})
    if missing:
        raise AgentPackError("requested items missing: " + ", ".join(sorted(missing)))
    blocked = [f"skill:{item.name}" for item in selected_skills if not item.portable]
    blocked += [f"mcp:{item.name}" for item in selected_mcps if not item.portable]
    if blocked:
        raise AgentPackError("non-portable or secret-bearing items blocked: " + ", ".join(blocked))
    # A same-name source from two installed packages is ambiguous.
    if len(selected_skills) != len(requested_skills) or len(selected_mcps) != len(requested_mcps):
        raise AgentPackError("duplicate selected capability; choose a plugin source instead")
    operations: list[dict[str, Any]] = []
    skills_to_copy = []
    for item in selected_skills:
        destination = layout.destination_skill_root / item.name
        exists = destination.exists() or destination.is_symlink()
        if exists and not update:
            raise AgentPackError(f"destination already exists: {destination}; use --update to preview replacement")
        if exists and destination.is_symlink():
            raise AgentPackError(f"refusing symlinked destination: {destination}")
        before = _tree_fingerprint(destination) if exists else None
        after = _tree_fingerprint(Path(item.path))
        if before == after:
            continue
        skills_to_copy.append(item)
        operations.append({"action": "update-skill" if exists else "copy-skill", "name": item.name,
                           "to": str(destination), "before": before, "after": after})
    mcp_payload = _load_destination_mcp(layout.destination_mcp)
    original_mcp = json.loads(json.dumps(mcp_payload))
    servers = mcp_payload.setdefault("mcpServers", {})
    for item in selected_mcps:
        exists = item.name in servers
        if exists and not update:
            raise AgentPackError(f"MCP destination already exists: {item.name}; use --update to preview replacement")
        before = servers.get(item.name, {})
        if before == item.config:
            continue
        operations.append({"action": "update-mcp" if exists else "merge-mcp", "name": item.name,
                           "to": str(layout.destination_mcp),
                           "changed_keys": sorted(key for key in set(before) | set(item.config)
                                                  if before.get(key) != item.config.get(key))})
        servers[item.name] = item.config
    result: dict[str, Any] = {"ok": True, "applied": apply, "operations": operations}
    if not apply or not operations:
        return result
    if selected_mcps and layout.destination_mcp.suffix == ".jsonc":
        raise AgentPackError("writing legacy JSONC destinations is intentionally unsupported")
    recovery_base = (recovery_root or Path.home() / ".agent-pack" / "recovery").expanduser().resolve()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    transaction = recovery_base / f"{stamp}-transfer-{_package_name(layout.root)}"
    transaction.mkdir(parents=True, mode=0o700)
    moved, created = [], []
    mcp_written = False
    mcp_existed = layout.destination_mcp.exists()
    try:
        # Fully validate / copy Skill trees before changing any destination.
        with tempfile.TemporaryDirectory(prefix="agent-pack-transfer-") as temporary:
            staged = Path(temporary)
            for item in skills_to_copy:
                _copy_skill(Path(item.path), staged / item.name)
            for item in skills_to_copy:
                destination = layout.destination_skill_root / item.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    archived = transaction / "skills" / item.name
                    archived.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, archived)
                    moved.append((archived, destination))
                _copy_skill(staged / item.name, destination)
                created.append(destination)
            if mcp_payload != original_mcp:
                if mcp_existed:
                    shutil.copy2(layout.destination_mcp, transaction / "mcp.json")
                    (transaction / "mcp.json").chmod(0o600)
                _write_json_atomic(layout.destination_mcp, mcp_payload)
                mcp_written = True
    except Exception:
        for destination in reversed(created):
            shutil.rmtree(destination)
        for archived, destination in reversed(moved):
            os.replace(archived, destination)
        if mcp_written:
            if mcp_existed:
                shutil.copy2(transaction / "mcp.json", layout.destination_mcp)
            else:
                layout.destination_mcp.unlink()
        raise
    result["recovery"] = str(transaction)
    return result


def _mcp_allowed(layout: ProjectLayout, name: str, client: str) -> bool:
    policy = layout.mcp_client_policy.get(name, {})
    include = policy.get("include", ())
    exclude = policy.get("exclude", ())
    if not project_write_supported(client) and declared_scope(layout, "mcp", name) == "project":
        return False   # project-level write for these clients comes later; never fall back to the user level
    return (not include or client in include) and client not in exclude


def _env_references(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(ENV_REFERENCE_RE.findall(value)) - RESERVED_ENV_PLACEHOLDERS - SYSTEM_ENV_PLACEHOLDERS
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


def _system_env_values(home: Path | None) -> dict[str, str]:
    """This machine's value for every name in SYSTEM_ENV_PLACEHOLDERS (``home`` is the user's home)."""
    base = (home or Path.home()).expanduser().resolve()
    try:
        user = os.environ.get("USER") or getpass.getuser()
    except (KeyError, OSError):
        user = ""
    values = {
        "HOME": str(base),
        "USER": user,
        "LOGNAME": os.environ.get("LOGNAME") or user,
        "TMPDIR": os.environ.get("TMPDIR") or tempfile.gettempdir(),
        "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME") or str(base / ".config"),
        "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME") or str(base / ".local" / "share"),
        "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME") or str(base / ".cache"),
        "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME") or str(base / ".local" / "state"),
    }
    return {name: value for name, value in values.items() if value}


def _package_provides(inventory: Inventory, relative: str) -> bool:
    """Whether the merged project package contains ``relative`` (the files it is built from)."""
    parts = Path(relative).parts
    if not parts or ".." in parts:
        return False
    if len(parts) == 1:
        return parts[0] in {"plugin.json", "mcp.json"}
    if parts[0] == "runtime":
        return parts[1:] == ("mcp_env_launcher.py",)
    if parts[0] == "skills":
        for skill in inventory.skills:
            if skill.portable and skill.name == parts[1]:
                return len(parts) == 2 or (Path(skill.path) / Path(*parts[2:])).exists()
    return False


def _resolve_plugin_root(item: McpRecord, text: str, inventory: Inventory) -> str:
    """Resolve ``${PLUGIN_ROOT}/<path>`` against the entry's own source directory.

    The merged package holds only the Skills, ``mcp.json`` and the launcher, so a path that is
    not one of those cannot live under the package root. If the entry's source directory has the
    file, the reference becomes that file's absolute path; otherwise the entry is blocked.
    """
    def replace(match: re.Match[str]) -> str:
        relative = (match.group(1) or "").strip("/")
        if not relative or _package_provides(inventory, relative):
            return match.group(0)
        if not item.root:
            return match.group(0)  # not a project entry: nothing to resolve against
        source_dir = Path(item.root)
        candidate = source_dir / relative
        if ".." not in Path(relative).parts and candidate.exists():
            return str(candidate.resolve())
        raise AgentPackError(
            f"MCP {item.name} references ${{PLUGIN_ROOT}}/{relative}, which exists "
            f"neither in the merged package nor in its source directory {source_dir}"
        )

    return PLUGIN_ROOT_REF_RE.sub(replace, text)


def _mcp_plugin_root_problems(item: McpRecord, inventory: Inventory) -> list[str]:
    """Every unresolvable ``${PLUGIN_ROOT}`` path of one entry (what sync would block on)."""
    problems: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            try:
                _resolve_plugin_root(item, value, inventory)
            except AgentPackError as exc:
                problems.append(str(exc))
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)

    walk(item.config)
    return problems


def _portable_package_from_inventory(
    inventory: Inventory,
    package_name: str,
    destination: Path,
    *,
    client: str | None = None,
    home: Path | None = None,
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
        if (client is not None and not _declared_write(client, "project_skill_write") and layout is not None
                and declared_scope(layout, "skill", item.name) == "project"):
            continue   # project-native for these clients (.agents/skills); never copied to the user-level package
        if not _declared_write(client, "user_skill_write"):
            continue   # the driver does not declare user-level Skill writes
        _copy_skill(Path(item.path), skills_root / item.name, allowed_root=project_root)

    system_env = _system_env_values(home)

    def project_mcp_config(item: McpRecord) -> dict[str, Any]:
        source_path = Path(item.source)

        def expand(value: Any) -> Any:
            if isinstance(value, str):
                value = _resolve_plugin_root(item, value, inventory)
                value = ENV_REFERENCE_RE.sub(
                    lambda match: system_env.get(match.group(1), match.group(0))
                    if match.group(1) in SYSTEM_ENV_PLACEHOLDERS else match.group(0),
                    value,
                )
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
        and _declared_write(client, "user_mcp_write")
    ]
    required_env: set[str] = set()
    for item in selected_mcps:
        if item.config.get("type") == "stdio":
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
        if not references or config.get("type") != "stdio":
            if client is not None and _registry().mcp_spec(client) is not None:
                import agent_pack_mcp_json  # noqa: PLC0415 - the ``json`` MCP adapter, shipped next to this file
                config = agent_pack_mcp_json.package_entry(config, _registry().mcp_options(client))
            rendered_mcps[item.name] = config
            continue
        runtime_root = destination / "runtime"
        if not launcher_copied:
            runtime_root.mkdir(exist_ok=True)
            shutil.copy2(Path(__file__).with_name("mcp_env_launcher.py"), runtime_root / "mcp_env_launcher.py")
            launcher_copied = True
        config_name = hashlib.sha256(item.name.encode("utf-8")).hexdigest()[:16] + ".json"
        _write_json_atomic(runtime_root / config_name, config)
        assert layout is not None and layout.credential_env_file is not None
        launcher_args = [
            "${PLUGIN_ROOT}/runtime/mcp_env_launcher.py",
            "--env-file",
            str(layout.credential_env_file.resolve()),
            "--config",
            f"${{PLUGIN_ROOT}}/runtime/{config_name}",
        ]
        if "${PLUGIN_DATA}" in json.dumps(config):
            # The client expands placeholders in the launcher's own args, not in the config file it reads (#91).
            launcher_args += ["--plugin-data", "${PLUGIN_DATA}"]
        rendered_mcps[item.name] = {"type": "stdio", "command": "python3", "args": launcher_args}

    _write_json_atomic(destination / "mcp.json", {
        "$schema": AGENT_PLUGIN_MCP_SCHEMA,
        "mcpServers": rendered_mcps,
    })
    manifest = _read_json(destination / "plugin.json")
    content_fingerprint = _tree_fingerprint(destination)
    manifest["version"] = f"1.0.0+project.{content_fingerprint[:12]}"
    _write_json_atomic(destination / "plugin.json", manifest)


def _companion_spec(client: str) -> dict[str, Any] | None:
    """The ``sync.companions`` declaration of a client (the files its package carries beside the portable ones), or ``None``."""
    spec = _registry().sync_spec(client)
    return spec.get("companions") if spec else None


def _native_remote(client: str) -> bool:
    """Whether the client's remote MCP servers are written to its own user-level file instead of the package."""
    spec = _registry().mcp_spec(client)
    return bool(spec and spec.get("native_remote"))


def _write_companions(package_root: Path, companions: dict[str, Any], *, native_remote: bool = False) -> None:
    """Write the companion files a client's package declares (``sync.companions``) from the portable ``plugin.json`` and
    ``mcp.json``: a manifest in the client's own layout, and an ``mcp`` projection with prefixed placeholders."""
    import agent_pack_mcp_json  # noqa: PLC0415 - the ``json`` MCP adapter, shipped next to this file

    manifest = _read_json(package_root / "plugin.json")
    manifest_path = package_root / companions["manifest"]
    manifest_path.parent.mkdir(exist_ok=True)
    if manifest_path.exists():
        companion = _read_json(manifest_path)
    else:
        companion = {}
    # Identity fields are owned by the portable plugin.json and are always
    # regenerated here so the companion manifest can never drift from the
    # authoritative one (only client-specific presentation such as the
    # `interface` block is preserved as authored).
    companion["name"] = manifest["name"]
    companion["version"] = manifest.get("version", "1.0.0")
    companion.setdefault(
        "description", manifest.get("description", "Project capabilities")
    )
    companion.setdefault("skills", "./skills/")
    mcp_path = package_root / "mcp.json"
    if "mcp" not in companions or not mcp_path.exists():
        _write_json_atomic(manifest_path, companion)
        return
    companion.setdefault("mcpServers", "./" + companions["mcp"])
    _write_json_atomic(manifest_path, companion)
    mcp = _read_json(mcp_path)
    # Remote MCPs are synchronized through the client's own file with native env-name fields;
    # do not load a second copy from the plugin with ambiguous header expansion.
    remote = {name: config for name, config in mcp.get("mcpServers", {}).items()
              if native_remote and config.get("type") != "stdio"}
    if remote and "remote_mcp" in companions:
        _write_json_atomic(package_root / companions["remote_mcp"], {"mcpServers": remote})
        mcp["mcpServers"] = {name: config for name, config in mcp["mcpServers"].items() if name not in remote}
        _write_json_atomic(mcp_path, mcp)
    projected: dict[str, Any] = {}
    for name, config in mcp.get("mcpServers", {}).items():
        # The portable mcp.json carries ${PLUGIN_ROOT}; this projection is what the client loads, and it
        # only expands the prefixed names.
        projected[name] = agent_pack_mcp_json.companion_entry(
            config, companions.get("placeholder_prefix", ""), companions.get("type_names", {}))
    _write_json_atomic(package_root / companions["mcp"], {"mcpServers": projected})


def _package_name(root: Path) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-")
    name = name or "project-capabilities"
    if not PLUGIN_NAME_RE.fullmatch(name):
        raise AgentPackError(f"cannot derive portable package name from {root.name!r}")
    return name


def _client_package_destination(client: str, home: Path, package_name: str) -> Path:
    return package_parent(client, home) / package_name


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
    companions = _companion_spec(client)
    if companions is None:
        return _source_matches_destination(source, destination)
    with tempfile.TemporaryDirectory(prefix="agent-pack-match-") as temporary:
        staged = Path(temporary) / source.name
        shutil.copytree(source, staged, symlinks=False, ignore=shutil.ignore_patterns(*COPY_IGNORE))
        _write_companions(staged, companions)
        return _source_matches_destination(staged, destination)


def _build_package(inventory: Inventory, package_name: str, parent: Path, client: str,
                   home: Path | None = None) -> Path:
    package = parent / package_name
    _portable_package_from_inventory(inventory, package_name, package, client=client, home=home)
    companions = _companion_spec(client)
    if companions is not None:
        _write_companions(package, companions, native_remote=_native_remote(client))
    return package


def install_plugin(
    plugin: Path,
    client: str,
    *,
    home: Path | None = None,
    apply: bool = False,
    replace: bool = False,
    recovery_root: Path | None = None,
    skills: Iterable[str] = (),
    mcps: Iterable[str] = (),
    reset_overrides: bool = False,
) -> dict[str, Any]:
    """Bootstrap a portable plugin into a supported client without overwriting.

    For Claude Code, ``skills`` / ``mcps`` limit the install to those components
    and local credential values on MCP entries are kept unless ``reset_overrides``.
    """
    plugin = plugin.expanduser().resolve()
    manifest, issues = validate_plugin_manifest(plugin)
    if issues:
        raise AgentPackError("invalid plugin: " + "; ".join(issues))
    name = manifest["name"]
    _tree_fingerprint(plugin)
    home = (home or Path.home()).expanduser().resolve()
    if client in _registry().ids_with_sync("native-package"):
        return _install_plugin_native(
            plugin, name, client, home=home, apply=apply, replace=replace, recovery_root=recovery_root,
            select_skills=set(skills), select_mcps=set(mcps), reset_overrides=reset_overrides,
        )
    if skills or mcps:
        raise AgentPackError(f"--skill / --mcp selection is supported for the {_native_names()} client only")
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
                if _companion_spec(client) is not None:
                    _write_companions(staged, _companion_spec(client))
                os.replace(staged, destination)
            except Exception:
                if archived is not None and archived.exists() and not destination.exists():
                    os.replace(archived, destination)
                raise
            finally:
                if temp_parent.exists():
                    shutil.rmtree(temp_parent)
        _update_personal_marketplace(client, home, name)
    return {"ok": True, "applied": apply, "operations": [operation]}


def _native_names() -> str:
    """The clients whose driver declares a ``native-package`` sync, for the messages that name who supports a flag."""
    return " / ".join(_registry().ids_with_sync("native-package"))


def _native_mcp_config(client: str, config: dict[str, Any], plugin_root: Path, data_root: Path) -> dict[str, Any]:
    """Project a portable MCP entry onto the client's own MCP schema (the ``json`` adapter, with the client's options)."""
    import agent_pack_mcp_json  # noqa: PLC0415 - shipped next to this file

    return agent_pack_mcp_json.native_entry(config, plugin_root, data_root, _registry().mcp_options(client))


def _install_plugin_native(
    plugin: Path,
    name: str,
    client: str,
    *,
    home: Path,
    apply: bool,
    replace: bool,
    recovery_root: Path | None,
    select_skills: set[str] | None = None,
    select_mcps: set[str] | None = None,
    reset_overrides: bool = False,
) -> dict[str, Any]:
    """Install a plugin into a ``native-package`` client as live links plus user-scope MCP entries.

    Such a client has no Agent Plugins loader, so the plugin is projected onto the
    three things it does read: ``~/.claude/skills/<skill>`` (symlink to the
    source skill), ``~/.claude.json#mcpServers`` (user scope), and an install
    record ``~/.claude/agent-pack/plugins/<name>`` (symlink to the source root)
    that inventory uses to report the installed version.
    """
    import agent_pack_native_package  # noqa: PLC0415 - shipped next to this file

    select_skills, select_mcps = select_skills or set(), select_mcps or set()
    selective = bool(select_skills or select_mcps)
    record = package_parent(client, home) / name
    links: list[tuple[str, str, Path, Path]] = [("plugin", name, record, plugin)]
    available_skills: set[str] = set()
    skills_root = plugin / "skills"
    if skills_root.is_dir() and not skills_root.is_symlink():
        for child in sorted(skills_root.iterdir(), key=lambda item: item.name):
            if child.is_dir() and not child.name.startswith(".") and (child / "SKILL.md").is_file():
                available_skills.add(child.name)
                if not selective or child.name in select_skills:
                    links.append(("skill", child.name, home / ".claude" / "skills" / child.name, child))
    unknown_skills = select_skills - available_skills
    if unknown_skills:
        raise AgentPackError("plugin has no such Skill: " + ", ".join(sorted(unknown_skills)))

    config_path = user_mcp_file(client, home)
    root_key = mcp_root_key(client)
    servers: dict[str, dict[str, Any]] = {}
    mcp_path = plugin / "mcp.json"
    if mcp_path.is_file():
        data_root = home / ".claude" / "agent-pack" / "data" / name
        declared = _read_json(mcp_path).get("mcpServers", {})
        unknown_mcps = select_mcps - set(declared)
        if unknown_mcps:
            raise AgentPackError("plugin has no such MCP server: " + ", ".join(sorted(unknown_mcps)))
        for server, raw in sorted(declared.items()):
            if selective and server not in select_mcps:
                continue
            parsed = _portable_mcp(server, raw)
            if not parsed.portable:
                raise AgentPackError(f"MCP {server} is not portable: " + "; ".join(parsed.issues))
            servers[server] = _native_mcp_config(client, parsed.config, record, data_root)
    elif select_mcps:
        raise AgentPackError("plugin has no mcp.json: " + ", ".join(sorted(select_mcps)))

    config: dict[str, Any] = {}
    if servers and config_path.exists():
        if config_path.is_symlink():
            raise AgentPackError(f"refusing to edit symlinked Claude config: {config_path}")
        config = _read_json(config_path)
    existing_servers = config.get(root_key, {})
    if not isinstance(existing_servers, dict):
        raise AgentPackError(f"{root_key} must be an object: {config_path}")

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
        operations.append({"action": f"{action}-{kind}", "client": client, "name": item_name, "to": str(path)})
    entries: dict[str, dict[str, Any]] = {}
    for server, rendered in servers.items():
        state, entries[server], kept = agent_pack_native_package.classify_mcp(
            rendered, existing_servers.get(server), reset=reset_overrides)
        action = {"differ": "replace"}.get(state, state)
        if action == "replace":
            conflicts.append(f"mcp {server}: {config_path}")
        operation = {"action": f"{action}-mcp", "client": client, "name": server, "to": str(config_path)}
        if kept:
            operation["kept_overrides"] = kept
        operations.append(operation)
    if conflicts and not replace:
        raise AgentPackError("Claude client already has different content: " + "; ".join(conflicts))
    result: dict[str, Any] = {"ok": True, "applied": apply, "operations": operations}
    if not apply:
        return result

    transaction: Path | None = None
    if conflicts:
        recovery_base = (recovery_root or home / ".agent-pack" / "recovery").expanduser().resolve()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        transaction = recovery_base / f"{stamp}-{client}-{name}"
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
            server: entry for server, entry in entries.items()
            if existing_servers.get(server) != entry
        }
        if changed:
            replaced = {server: existing_servers[server] for server in changed if server in existing_servers}
            if replaced:
                assert transaction is not None
                _write_json_atomic(transaction / "replaced-mcp.json", {"mcpServers": replaced}, mode=0o600)
            config.setdefault(root_key, {}).update(changed)
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
        raise AgentPackError(f"{client} install rolled back: {exc}") from exc
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


def _marketplace_without(text: str, name: str) -> str:
    """The marketplace file text without the entry named ``name``; other entries and layout are kept."""
    payload = json.loads(text)
    payload["plugins"] = [item for item in payload["plugins"]
                          if not (isinstance(item, dict) and item.get("name") == name)]
    indent = 2
    match = re.search(r"\n([ \t]+)\S", text)
    if match:
        indent = "\t" if match.group(1).startswith("\t") else len(match.group(1))
    return json.dumps(payload, indent=indent, ensure_ascii=False) + ("\n" if text.endswith("\n") else "")


def _retirement_extras(client: str, home: Path, name: str, primary: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """A client that keeps a retired plugin in a personal copy, a marketplace entry, a cache and its own config file
    (``sync.marketplace`` and ``sync.cleanup``) leaves those behind after a retirement.

    The copy and the marketplace entry are archived and removed; the cache versions and the user's
    config file are listed as residuals with a manual step.
    """
    registry = _registry()
    spec = registry.sync_spec(client) or {}
    operations: list[dict[str, str]] = []
    residuals: list[dict[str, str]] = []
    copy = _client_package_destination(client, home, name)
    if (copy.exists() or copy.is_symlink()) and copy.resolve() != primary.resolve():
        operations.append({"action": "archive-plugin-copy", "name": name, "from": str(copy)})
    if "marketplace" in spec:
        market = registry.location(spec["marketplace"]["path"], home)
        if market.is_symlink():
            residuals.append({"client": client, "kind": "marketplace-entry", "name": name, "path": str(market),
                              "manual": f"remove the {name} entry from this file by hand (symlinked file, not edited automatically)"})
        elif market.is_file():
            try:
                plugins = json.loads(market.read_text(encoding="utf-8")).get("plugins", [])
                listed = isinstance(plugins, list) and any(isinstance(item, dict) and item.get("name") == name for item in plugins)
            except (OSError, ValueError, AttributeError):
                listed = False
                residuals.append({"client": client, "kind": "marketplace-entry", "name": name, "path": str(market),
                                  "manual": f"unreadable marketplace file; remove the {name} entry by hand if present"})
            if listed:
                operations.append({"action": "remove-marketplace-entry", "name": name, "from": str(market)})
    cleanup = spec.get("cleanup", {})
    if "cache_dir" in cleanup:
        cache = registry.location(cleanup["cache_dir"], home)
        if cache.is_dir() and not cache.is_symlink():
            for market_dir in sorted(path for path in cache.iterdir() if path.is_dir()):
                plugin_dir = market_dir / name
                if plugin_dir.is_dir():
                    left = [path for path in sorted(plugin_dir.iterdir()) if path.resolve() != primary.resolve()]
                    if left:
                        residuals.append({"client": client, "kind": "cache", "name": name, "path": str(plugin_dir),
                                          "manual": "other cached versions or copies remain; delete the directory when no longer needed"})
    if "config_file" in cleanup:
        config = registry.location(cleanup["config_file"], home)
        try:
            mentioned = config.is_file() and f"{name}@" in config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            mentioned = False
        if mentioned:
            residuals.append({"client": client, "kind": "config", "name": name, "path": str(config),
                              "manual": f"remove the [plugins.\"{name}@...\"] table by hand (user configuration, not edited automatically)"})
    return operations, residuals


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
    replace: bool = False,
    reset_overrides: bool = False,
    only: Iterable[str] | None = None,
    replace_mcp: bool = False,
    replace_skills: bool = False,
    scope: str = "all",
    project_store: str = "local",
) -> dict[str, Any]:
    """Reconcile one project package and explicitly selected legacy components.

    ``only`` (``plugins`` / ``skills`` / ``mcp``) and ``replace_mcp`` / ``replace_skills`` scope a Claude sync
    to those types (#68). Codex and Cursor install one package that holds every type, so they accept the full
    selection only.

    ``retire_only`` archives the selected legacy components without installing
    or replacing the project package itself. Claude Code is handled by
    ``agent_pack_native_package`` (dedicated package, MCP entries, Skill links);
    ``replace`` and ``reset_overrides`` only apply there.
    """
    if client in _registry().ids_with_sync("native-package"):
        import agent_pack_native_package  # noqa: PLC0415 - shipped next to this file

        return agent_pack_native_package.sync_project(
            project, home=home, apply=apply, replace=replace, reset_overrides=reset_overrides,
            recovery_root=recovery_root, retire_skills=retire_skills, retire_plugins=retire_plugins,
            retire_only=retire_only, only=only, replace_mcp=replace_mcp, replace_skills=replace_skills,
            scope=scope, project_store=project_store, client=client)
    import agent_pack_native_package  # noqa: PLC0415 - shipped next to this file

    native = _native_names()
    if scope != "all":
        raise AgentPackError(f"--scope user|project is supported for the {native} client only: {client} has no "
                             "project-level write yet")
    if project_store != "local":
        raise AgentPackError(f"--project-store is supported for the {native} client only: {client} has no "
                             "project-level write yet")

    if agent_pack_native_package.normalize_only(only) != frozenset(agent_pack_native_package.SYNC_TYPES) and not retire_only:
        raise AgentPackError(
            f"--only mcp|skills|plugins is supported for the {native} client only: {client} installs one package "
            "that holds Skills, MCP servers and the manifest together")
    if replace_skills or (replace_mcp and not _native_remote(client)):
        raise AgentPackError(
            f"--replace-skills / --replace-mcp are supported for the {native} client only"
            f" ({' / '.join(_native_remote_clients())} also accepts --replace-mcp for its native remote MCP entries)")
    replace = replace or replace_mcp
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
    else:
        # Build the package in a scratch directory for every preview and apply, so the
        # portability, secret and credential-key checks run before anything is written and a
        # preview reports exactly what ``--apply`` would (same ok, exit code and message).
        with tempfile.TemporaryDirectory(prefix="agent-pack-plan-") as temporary:
            expected = _build_package(authority, package_name, Path(temporary), client, home)
            if destination.exists() or destination.is_symlink():
                if _tree_fingerprint(expected) == _tree_fingerprint(destination):
                    package_action = "keep-plugin"
                    needs_package = False
                else:
                    package_action = "replace-plugin"

    native_plan = None
    if _native_remote(client) and not retire_only:
        import agent_pack_mcp_toml  # noqa: PLC0415 - the ``toml`` MCP adapter, shipped next to this file
        native_plan = agent_pack_mcp_toml.plan(project, home, client, replace=replace)
    operations: list[dict[str, str]] = [] if retire_only else [
        {"action": package_action, "client": client, "name": package_name, "to": str(destination)}
    ]
    residuals: list[dict[str, str]] = []
    for kind, name, path in retirement_targets:
        operations.append({"action": "archive-" + kind, "name": name, "from": str(path)})
        if kind == "plugin" and _has_retirement_extras(client):
            extra_operations, extra_residuals = _retirement_extras(client, home, name, path)
            operations.extend(extra_operations)
            residuals.extend(extra_residuals)
    if native_plan is not None:
        operations.extend(native_plan["operations"])
    extras = {"residuals": residuals} if residuals else {}
    skipped = _project_scope_skips(layout, authority, client)
    if skipped:   # declared project-level entries are skipped for this client, never written to the user level
        extras["skipped"] = skipped
        import agent_pack_sync_scope  # noqa: PLC0415 - shipped next to this file

        extras["warnings"] = [agent_pack_sync_scope.skipped_warning(item) for item in skipped]
    if not apply:
        return {"ok": True, "applied": False, "states": states, "operations": operations, **extras}

    recovery_base = (recovery_root or home / ".agent-pack" / "recovery").expanduser().resolve()
    recovery_base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    transaction = recovery_base / f"{stamp}-{client}-{package_name}"
    transaction.mkdir(mode=0o700)
    moved: list[tuple[Path, Path]] = []
    installed = False
    native_applied = False
    marketplace_backup: tuple[Path, bytes] | None = None
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
                staged = _build_package(authority, package_name, staging_parent, client, home)
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
        for operation in operations:
            if operation["action"] == "archive-plugin-copy":
                copy = Path(operation["from"])
                archived = transaction / "plugin-copies" / operation["name"]
                archived.parent.mkdir(parents=True, exist_ok=True)
                os.replace(copy, archived)
                moved.append((archived, copy))
            elif operation["action"] == "remove-marketplace-entry":
                market = Path(operation["from"])
                original_bytes = market.read_bytes()
                marketplace_backup = (market, original_bytes)
                (transaction / "marketplace").mkdir(parents=True, exist_ok=True)
                shutil.copy2(market, transaction / "marketplace" / market.name)
                market.write_text(_marketplace_without(original_bytes.decode("utf-8"), operation["name"]),
                                  encoding="utf-8")
        if native_plan is not None:
            agent_pack_mcp_toml.apply(native_plan, transaction)
            native_applied = True
        if needs_package:
            _update_personal_marketplace(client, home, package_name)
    except Exception as exc:
        if native_applied:
            agent_pack_mcp_toml.rollback(native_plan)
        if marketplace_backup is not None:
            marketplace_backup[0].write_bytes(marketplace_backup[1])
        if installed and destination.exists():
            shutil.rmtree(destination)
        for archived, original in reversed(moved):
            if archived.exists() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                os.replace(archived, original)
        raise AgentPackError(f"reconcile rolled back: {exc}") from exc
    return {
        "ok": True, "applied": True, "states": states, "operations": operations,
        "recovery": str(transaction), **extras,
    }


def sync_project(
    project: Path,
    client: str,
    *,
    home: Path | None = None,
    apply: bool = False,
    replace: bool = False,
    reset_overrides: bool = False,
    recovery_root: Path | None = None,
    only: Iterable[str] | None = None,
    replace_mcp: bool = False,
    replace_skills: bool = False,
    scope: str = "all",
    project_store: str = "local",
) -> dict[str, Any]:
    return reconcile_project(project, client, home=home, apply=apply, replace=replace,
                             reset_overrides=reset_overrides, recovery_root=recovery_root,
                             only=only, replace_mcp=replace_mcp, replace_skills=replace_skills, scope=scope,
                             project_store=project_store)


def _project_scope_skips(layout: ProjectLayout, authority: Inventory, client: str) -> list[dict[str, str]]:
    """Project-declared entries a codex / cursor sync leaves out (their project level is not written yet)."""
    skipped = [{"kind": "mcp", "name": item.name, "client": client, "reason": "client-project-unsupported"}
               for item in authority.mcps if item.portable and declared_scope(layout, "mcp", item.name) == "project"]
    skipped += [{"kind": "skill", "name": item.name, "client": client, "reason": "project-native"}
                for item in authority.skills if declared_scope(layout, "skill", item.name) == "project"]
    return sorted(skipped, key=lambda item: (item["kind"], item["name"]))


def _update_personal_marketplace(client: str, home: Path, package_name: str) -> None:
    """List the package in the client's personal marketplace file (``sync.marketplace``); no-op without that declaration."""
    spec = (_registry().sync_spec(client) or {}).get("marketplace")
    if spec is None:
        return
    marketplace = _registry().location(spec["path"], home)
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
        "source": {"source": "local", "path": f"{spec['source_dir']}/{package_name}"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Developer Tools",
    })
    _write_json_atomic(marketplace, payload)


def _has_retirement_extras(client: str) -> bool:
    spec = _registry().sync_spec(client) or {}
    return "marketplace" in spec or "cleanup" in spec


def _native_remote_clients() -> list[str]:
    return [item["id"] for item in _registry().BUILTIN if (item.get("mcp") or {}).get("native_remote")]


def _skill_fingerprints(package: Path) -> dict[str, str]:
    root = package / "skills"
    if not root.is_dir() or root.is_symlink():
        return {}
    found = {}
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.is_symlink() and (child / "SKILL.md").is_file():
            try:
                found[child.name] = _tree_fingerprint(child)
            except AgentPackError:
                found[child.name] = "unreadable"
    return found


def _package_mcps(package: Path) -> dict[str, Any]:
    path = package / "mcp.json"
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        servers = _read_json(path).get("mcpServers", {})
    except AgentPackError:
        return {}
    return servers if isinstance(servers, dict) else {}


def remote_missing_env(inventory: Inventory, layout: ProjectLayout, client: str) -> list[str]:
    return [f"MCP {item.name}: {name}" for item in inventory.mcps
            if item.portable and item.config.get("type") != "stdio" and _mcp_allowed(layout, item.name, client)
            for name in sorted(_env_references(item.config)) if not os.environ.get(name)]


def client_drift(project: Path, client: str, home: Path | None = None) -> dict[str, Any]:
    """Compare what a client has installed for this project with the project's authority.

    Codex and Cursor load the project package directory, so the installed package
    is compared with a freshly built one (Skills, MCP servers, the rest of the
    files). Claude Code is compared entry by entry by ``agent_pack_native_package``.
    Same-name MCP servers configured by hand in the client are listed as native.
    """
    if client in _registry().ids_with_sync("native-package"):
        import agent_pack_native_package  # noqa: PLC0415 - shipped next to this file

        report = agent_pack_native_package.drift(project, home, client=client)
        layout = load_project_layout(project)
        report["missing_env"] = remote_missing_env(inventory_project(layout.root), layout, client)
        report["in_sync"] = report["in_sync"] and not report["missing_env"]
        return report
    home = (home or Path.home()).expanduser().resolve()
    layout = load_project_layout(project)
    authority = inventory_project(layout.root)
    name = _package_name(layout.root)
    destination = _client_package_destination(client, home, name)
    report: dict[str, Any] = {"client": client, "package": name, "missing": [], "different": [], "overrides": [],
                              "stale": [], "native": [], "excluded": [], "dangling": [], "coupled": None}
    report["excluded"] = sorted(item.name for item in authority.mcps
                                if item.portable and not _mcp_allowed(layout, item.name, client))
    with tempfile.TemporaryDirectory(prefix="agent-pack-drift-") as temporary:
        expected = _build_package(authority, name, Path(temporary), client, home)
        if destination.is_symlink() and not destination.exists():
            report["dangling"].append(f"package {name}: {destination}")
        elif not destination.is_dir():
            report["missing"].append(f"package {name}")
            report["missing"] += [f"skill {skill}" for skill in _skill_fingerprints(expected)]
            report["missing"] += [f"mcp {server}" for server in _package_mcps(expected)]
        elif _tree_fingerprint(expected) != _tree_fingerprint(destination):
            want, have = _skill_fingerprints(expected), _skill_fingerprints(destination)
            report["missing"] += [f"skill {skill}" for skill in sorted(set(want) - set(have))]
            report["stale"] += [f"skill {skill}" for skill in sorted(set(have) - set(want))]
            report["different"] += [f"skill {skill}" for skill in sorted(set(want) & set(have))
                                    if want[skill] != have[skill]]
            want_mcps, have_mcps = _package_mcps(expected), _package_mcps(destination)
            report["missing"] += [f"mcp {server}" for server in sorted(set(want_mcps) - set(have_mcps))]
            report["stale"] += [f"mcp {server}" for server in sorted(set(have_mcps) - set(want_mcps))]
            report["different"] += [f"mcp {server}" for server in sorted(set(want_mcps) & set(have_mcps))
                                    if want_mcps[server] != have_mcps[server]]
            if not (report["missing"] or report["stale"] or report["different"]):
                report["different"].append(f"package {name}: other files differ")
        native = _native_user_mcps(client, home)
        report["native"] = sorted(native)
    if _native_remote(client):
        import agent_pack_mcp_toml  # noqa: PLC0415 - the ``toml`` MCP adapter, shipped next to this file
        for item in authority.mcps:
            if item.portable and item.config.get("type") != "stdio" and _mcp_allowed(layout, item.name, client):
                if item.name not in native:
                    report["missing"].append(f"mcp {item.name}")
                elif native[item.name] != agent_pack_mcp_toml.native_entry(
                        item.config, _registry().mcp_options(client), _registry().get(client)["display"]):
                    report["different"].append(f"mcp {item.name}")
    report["missing_env"] = remote_missing_env(authority, layout, client)
    report["in_sync"] = not (report["missing"] or report["different"] or report["stale"] or report["dangling"] or report["missing_env"])
    return report


def doctor(project: Path, clients: Iterable[str] = (), home: Path | None = None) -> dict[str, Any]:
    """Validate the project; with ``clients``, also report each client's drift from it."""
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
        for driver in _registry().BUILTIN:
            companions = (driver.get("sync") or {}).get("companions")
            if not companions:
                continue
            adapter_path = Path(plugin["path"]) / companions["manifest"]
            if not adapter_path.is_file() or adapter_path.is_symlink():
                continue
            try:
                adapter_version = _read_json(adapter_path).get("version")
            except AgentPackError as exc:
                issues.append(str(exc))
                continue
            if adapter_version != plugin["version"]:
                issues.append(
                    f"plugin {plugin['name']}: {driver['display']} adapter version {adapter_version!r} "
                    f"does not match plugin.json version {plugin['version']!r}"
                )
    for item in inventory.mcps:
        if item.portable:
            issues.extend(_mcp_plugin_root_problems(item, inventory))
    required_env: set[str] = set()
    for item in inventory.mcps:
        if item.portable and item.config.get("type") == "stdio":
            required_env.update(_env_references(item.config))
        elif item.portable:
            issues.extend(f"MCP {item.name}: missing environment variable: {name}"
                          for name in sorted(_env_references(item.config)) if not os.environ.get(name))
    if required_env:
        if layout.credential_env_file is None:
            issues.append("MCP environment references require capabilities.credential_env_file")
        else:
            try:
                missing = required_env - _dotenv_keys(layout.credential_env_file)
                issues.extend(f"credential env file missing key: {name}" for name in sorted(missing))
            except AgentPackError as exc:
                issues.append(str(exc))
    reports: dict[str, Any] = {}
    for client in dict.fromkeys(clients):
        if _registry().supports(client, "read_only"):  # read-only observation of the client's own file; never a drift issue
            import agent_pack_inventory  # noqa: PLC0415 - shipped next to this file

            report = agent_pack_inventory.desktop_report(home or Path.home())
            warnings.extend(report.pop("warnings"))
            reports[client] = report
            continue
        if _registry().sync_spec(client) is None:  # sync writes nothing for this client: say why, with the declared reasons
            reasons = [entry["reason"] for entry in _registry().get(client).get("unsupported", [])
                       if entry["feature"] in ("sync-skills", "sync-mcp")]
            warnings.append(f"{client}: not compared with the project, sync does not write this client"
                            + (f" ({'; '.join(reasons)})" if reasons else ""))
            reports[client] = {"client": client, "compared": False}
            continue
        report = client_drift(layout.root, client, home)
        reports[client] = report
        if client in _registry().ids_with_sync("native-package"):  # advisory only: it never changes ``ok`` or the exit code
            import agent_pack_inventory  # noqa: PLC0415 - shipped next to this file

            shadows = agent_pack_inventory.desktop_shadow_findings(
                home or Path.home(), [item.name for item in inventory.mcps if _mcp_allowed(layout, item.name, client)])
            if shadows:
                report["shadowed_by"] = shadows
                warnings.extend(agent_pack_inventory.shadow_warning(item) for item in shadows)
        for kind in ("missing", "different", "stale", "dangling", "missing_env"):
            issues.extend(f"{client}: {kind} {item}" for item in report[kind])
        if report["coupled"]:
            issues.append(f"{client}: install record points into another package directory: {report['coupled']}")
        warnings.extend(f"{client}: kept local override {item['name']} ({', '.join(item['keys'])})"
                        for item in report["overrides"])
    result: dict[str, Any] = {
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
    if reports:
        result["clients"] = reports
    return result


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
    summary = value.get("conflicts")
    if isinstance(summary, dict):
        print("conflicts: " + ", ".join(f"{key}={number}" for key, number in summary.items()))
    if "by_scope" in value:   # a project declares scopes (or --scope was given): group by store
        import agent_pack_sync_scope  # noqa: PLC0415 - shipped next to this file; imported late (python3 -I path set in main)

        for line in agent_pack_sync_scope.grouped_lines(value):
            print(line)
        for line in (agent_pack_sync_scope.narrowing_lines(value) + agent_pack_sync_scope.migration_lines(value)
                     + agent_pack_sync_scope.skipped_lines(value)):
            print(line)
        value = {**value, "operations": []}
    for operation in value.get("operations", []):
        target = operation.get("to") or operation.get("from", "")
        print(f"- {operation['action']}: {operation.get('name', '')} -> {target}")
    for item in value.get("residuals", []):
        print(f"! residual {item['kind']}: {item['name']} -> {item['path']} ({item['manual']})")
    for issue in value.get("issues", []):
        print(f"! {issue}")
    for warning in value.get("warnings", []):
        print(f"~ {warning}")


def _add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scope", choices=("user", "project", "all"), default="all",
                        help="claude: act on the user-level entries only (user), the entries the project declares "
                             "project-level only (project), or both (all, default). Combines with --only")
    parser.add_argument("--project-store", choices=("local", "file"), default="local",
                        help="claude: where project-level MCP entries go: local (~/.claude.json, under the project's "
                             "path; default) or file (the project's .mcp.json, only for entries that do not depend on "
                             "this machine)")
    parser.add_argument("--only", action="append", default=[], metavar="TYPE",
                        help="claude: sync only these types (repeat or comma separate): mcp, skills, plugins; "
                             "default is all. plugins = the package and its install record. Other types are left untouched")
    parser.add_argument("--replace-mcp", action="store_true",
                        help="claude: replace only conflicting user-scope MCP entries this package did not install "
                             "(--replace covers every type)")
    parser.add_argument("--replace-skills", action="store_true",
                        help="claude: replace only conflicting user-level Skills this package did not install; they "
                             "apply to every project (--replace covers every type)")


def build_parser() -> argparse.ArgumentParser:
    import agent_pack_clients  # noqa: PLC0415 - shipped next to this file; the ``--client`` choices come from its drivers
    parser = argparse.ArgumentParser(prog="agent-pack")
    parser.add_argument("--output", choices=("text", "json"), default="text")
    sub = parser.add_subparsers(dest="command", required=True)

    inventory_parser = sub.add_parser("inventory")
    inventory_parser.add_argument("--source", required=True)
    inventory_parser.add_argument("--home", type=Path)

    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--project", type=Path, default=Path.cwd())
    doctor_parser.add_argument("--client", action="append", default=[], choices=agent_pack_clients.all_ids(),
                               help="also compare this client's installed capabilities with the project (repeatable); "
                                    "claude-desktop only reads claude_desktop_config.json and must be named")
    doctor_parser.add_argument("--home", type=Path)

    transfer_parser = sub.add_parser("transfer")
    transfer_parser.add_argument("--from", dest="source", required=True)
    transfer_parser.add_argument("--to-project", type=Path, required=True)
    transfer_parser.add_argument("--skill", action="append", default=[])
    transfer_parser.add_argument("--mcp", action="append", default=[])
    transfer_parser.add_argument("--home", type=Path)
    transfer_parser.add_argument("--apply", action="store_true")
    transfer_parser.add_argument("--update", action="store_true", help="preview replacement of same-name project entries")
    transfer_parser.add_argument("--recovery-root", type=Path)

    sync_parser = sub.add_parser("sync")
    sync_parser.add_argument("--project", type=Path, default=Path.cwd())
    sync_parser.add_argument("--client", choices=agent_pack_clients.default_ids(), required=True)
    sync_parser.add_argument("--home", type=Path)
    sync_parser.add_argument("--apply", action="store_true")
    sync_parser.add_argument("--replace", action="store_true",
                             help="claude: also replace Skills / MCP entries this package did not install")
    sync_parser.add_argument("--reset-overrides", action="store_true",
                             help="claude: drop local credential values (headers / env) instead of keeping them")
    sync_parser.add_argument("--recovery-root", type=Path)
    _add_scope_arguments(sync_parser)

    bootstrap_parser = sub.add_parser("bootstrap")
    bootstrap_parser.add_argument("--plugin", type=Path, required=True)
    bootstrap_parser.add_argument("--client", choices=agent_pack_clients.default_ids(), required=True)
    bootstrap_parser.add_argument("--home", type=Path)
    bootstrap_parser.add_argument("--apply", action="store_true")
    bootstrap_parser.add_argument("--replace", action="store_true")
    bootstrap_parser.add_argument("--recovery-root", type=Path)
    bootstrap_parser.add_argument("--skill", action="append", default=[], help="claude: install only this Skill")
    bootstrap_parser.add_argument("--mcp", action="append", default=[], help="claude: install only this MCP server")
    bootstrap_parser.add_argument("--reset-overrides", action="store_true",
                                  help="claude: drop local credential values (headers / env) instead of keeping them")

    reconcile_parser = sub.add_parser("reconcile")
    reconcile_parser.add_argument("--project", type=Path, default=Path.cwd())
    reconcile_parser.add_argument("--client", choices=agent_pack_clients.default_ids(), required=True)
    reconcile_parser.add_argument("--replace", action="store_true",
                                  help="claude: also replace Skills / MCP entries this package did not install")
    reconcile_parser.add_argument("--reset-overrides", action="store_true",
                                  help="claude: drop local credential values (headers / env) instead of keeping them")
    reconcile_parser.add_argument("--home", type=Path)
    reconcile_parser.add_argument("--retire-skill", action="append", default=[])
    reconcile_parser.add_argument("--retire-plugin", action="append", default=[])
    reconcile_parser.add_argument("--recovery-root", type=Path)
    reconcile_parser.add_argument(
        "--retire-only", action="store_true",
        help="archive the selected legacy components without installing the project package",
    )
    reconcile_parser.add_argument("--apply", action="store_true")
    _add_scope_arguments(reconcile_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            result = inventory_source(args.source, args.home).to_dict()
            result["ok"] = not result["issues"]
        elif args.command == "doctor":
            result = doctor(args.project, args.client, args.home)
        elif args.command == "transfer":
            if not args.skill and not args.mcp:
                raise AgentPackError("select at least one --skill or --mcp")
            result = transfer_to_project(
                inventory_source(args.source, args.home),
                args.to_project,
                args.skill,
                args.mcp,
                apply=args.apply, update=args.update,
                recovery_root=args.recovery_root or (args.home / ".agent-pack" / "recovery" if args.home else None),
            )
        elif args.command == "sync":
            result = sync_project(
                args.project, args.client, home=args.home, apply=args.apply, replace=args.replace,
                reset_overrides=args.reset_overrides, recovery_root=args.recovery_root,
                only=args.only, replace_mcp=args.replace_mcp, replace_skills=args.replace_skills,
                scope=args.scope, project_store=args.project_store,
            )
        elif args.command == "bootstrap":
            result = install_plugin(
                args.plugin,
                args.client,
                home=args.home,
                apply=args.apply,
                replace=args.replace,
                recovery_root=args.recovery_root,
                skills=args.skill,
                mcps=args.mcp,
                reset_overrides=args.reset_overrides,
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
                replace=args.replace,
                reset_overrides=args.reset_overrides,
                only=args.only, replace_mcp=args.replace_mcp, replace_skills=args.replace_skills,
                scope=args.scope, project_store=args.project_store,
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
    # ``agent_pack_native_package`` and ``agent_pack_mcp_toml`` do ``import agent_pack_config`` (also lazily,
    # inside bootstrap/reconcile). Without help that loads a second copy of this file whose
    # ``AgentPackError`` is a different class, so ``main()``'s ``except AgentPackError`` could not catch
    # what they raise (a stack trace, exit 1). Register this running module under its import name so
    # they get the very same module object, and put the script's own directory on ``sys.path`` so those
    # sibling imports resolve even under ``python3 -I`` / ``-P`` / PYTHONSAFEPATH=1, which drop it
    # (#98). The directory added is the script's own, exactly what a plain ``python3 script.py`` run adds.
    _here = str(Path(__file__).resolve().parent)
    if _here not in sys.path:
        sys.path.insert(0, _here)
    sys.modules.setdefault("agent_pack_config", sys.modules[__name__])

    raise SystemExit(main())
