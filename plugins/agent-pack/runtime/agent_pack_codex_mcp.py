"""Native Codex remote MCP projection. Preserve unrelated TOML and never expand env values."""
from __future__ import annotations

import json
import os
import tempfile
import re
import tomllib
from pathlib import Path
from typing import Any

import agent_pack_config as core


def native_entry(config: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"url": config["url"]}
    plain, env = {}, {}
    for key, value in (config.get("headers") or {}).items():
        bearer = re.fullmatch(r"Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}", value, re.IGNORECASE)
        reference = core.ENV_REFERENCE_RE.fullmatch(value)
        if key.lower() == "authorization" and bearer:
            result["bearer_token_env_var"] = bearer.group(1)
        elif reference:
            env[key] = reference.group(1)
        elif core.ENV_REFERENCE_RE.search(value):
            raise core.AgentPackError(f"Codex header requires a single environment reference: {key}")
        else:
            plain[key] = value
    if plain:
        result["http_headers"] = plain
    if env:
        result["env_http_headers"] = env
    return result


def _section_name(line: str) -> str | None:
    if not line.lstrip().startswith("["):
        return None
    try:
        table = tomllib.loads(line.strip() + "\n")
        servers = table.get("mcp_servers")
        if isinstance(servers, dict) and len(servers) == 1:
            return next(iter(servers))
    except tomllib.TOMLDecodeError:
        pass
    return None


def render(original: str, entries: dict[str, dict], removed: set[str]) -> str:
    """Replace only explicitly owned named tables; retain all other source bytes."""
    output: list[str] = []
    dropping = False
    for line in original.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            name = _section_name(line)
            dropping = name in removed or name in entries
        if not dropping:
            output.append(line)
    text = "".join(output).rstrip() + "\n" if output else ""
    for name, config in sorted(entries.items()):
        text += f"\n[mcp_servers.{json.dumps(name)}]\n"
        for key, value in config.items():
            if isinstance(value, dict):
                encoded = "{ " + ", ".join(json.dumps(k) + " = " + json.dumps(v) for k, v in value.items()) + " }"
            else:
                encoded = json.dumps(value)
            text += f"{key} = {encoded}\n"
    return text


def plan(project: Path, home: Path, *, replace: bool = False) -> dict:
    layout = core.load_project_layout(project)
    name = core._package_name(layout.root)
    state_path = home / ".agent-pack" / "state" / f"codex-remote-{name}.json"
    old = core._read_json(state_path).get("servers", {}) if state_path.exists() else {}
    if not isinstance(old, dict) or not all(isinstance(value, dict) for value in old.values()):
        raise core.AgentPackError("invalid Codex MCP ownership state")
    desired = {item.name: native_entry(item.config) for item in core.inventory_project(layout.root).mcps
               if item.portable and item.config.get("type") != "stdio" and core._mcp_allowed(layout, item.name, "codex")}
    config_path = home / ".codex" / "config.toml"
    if not desired and not old:
        return {"active": False, "operations": []}
    if config_path.is_symlink():
        raise core.AgentPackError(f"refusing symlinked Codex config: {config_path}")
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    try:
        current = tomllib.loads(original).get("mcp_servers", {})
    except tomllib.TOMLDecodeError as exc:
        raise core.AgentPackError("invalid Codex config.toml") from exc
    changes, operations = {}, []
    for server, entry in desired.items():
        if current.get(server) == entry:
            continue
        if server in current and current[server] != old.get(server) and not replace:
            raise core.AgentPackError(f"Codex MCP destination differs: {server}; preview with --replace")
        changes[server] = entry
        operations.append({"action": "update-native-mcp" if server in current else "add-native-mcp", "name": server})
    # A locally changed entry is no longer ours to remove.
    removed = {server for server in old if server not in desired and current.get(server) == old[server]}
    operations += [{"action": "remove-native-mcp", "name": server} for server in sorted(removed)]
    result = render(original, changes, removed) if operations else original
    try:
        parsed = tomllib.loads(result).get("mcp_servers", {})
        if any(parsed.get(server) != entry for server, entry in desired.items()):
            raise ValueError()
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise core.AgentPackError("Codex MCP must use named [mcp_servers.NAME] tables; convert inline entries first") from exc
    return {"active": True, "path": config_path, "state_path": state_path, "original": original, "text": result,
            "desired": desired, "operations": operations, "existed": config_path.exists(),
            "old_state": state_path.read_text(encoding="utf-8") if state_path.exists() else None}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".agent-pack-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def rollback(plan: dict) -> None:
    if not plan["active"]:
        return
    if plan["existed"]:
        _write(plan["path"], plan["original"])
    elif plan["path"].exists():
        plan["path"].unlink()
    if plan["old_state"] is not None:
        _write(plan["state_path"], plan["old_state"])
    elif plan["state_path"].exists():
        plan["state_path"].unlink()


def apply(plan: dict, recovery: Path) -> None:
    if not plan["active"]:
        return
    changed = plan["text"] != plan["original"]
    try:
        if changed:
            if plan["existed"]:
                _write(recovery / "codex-config.toml", plan["original"])
            _write(plan["path"], plan["text"])
        core._write_json_atomic(plan["state_path"], {"servers": plan["desired"]}, mode=0o600)
    except Exception:
        rollback(plan)
        raise
