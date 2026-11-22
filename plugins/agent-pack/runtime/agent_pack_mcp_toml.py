"""``toml`` MCP format adapter: how a server entry is written into a client's TOML configuration file.

The adapter is named for the format. Which file (the first user-level ``stores.mcp`` entry), which table holds the servers
(its ``root_key``) and how a remote entry is shaped (``mcp.options``: ``remote_url_key``, ``remote_headers: split-env``
with ``header_fields``) come from the driver declaration; nothing here knows a client by name. Unrelated TOML is
preserved byte for byte, only named ``[<root_key>.NAME]`` tables the sync owns are replaced, and env values are never
expanded.

``label`` is the client's display name, used in messages only.
"""
from __future__ import annotations

import json
import os
import tempfile
import re
import stat
import tomllib
from pathlib import Path
from typing import Any

import agent_pack_config as core


def native_entry(config: dict[str, Any], options: dict[str, Any], label: str) -> dict[str, Any]:
    """A remote entry in the client's TOML shape. ``options.header_fields`` names the three fields the headers split into:
    ``plain`` (literal values), ``env`` (header name -> environment variable name) and ``bearer`` (an ``Authorization:
    Bearer ${NAME}`` header becomes just the variable name)."""
    fields = options["header_fields"]
    result: dict[str, Any] = {options["remote_url_key"]: config["url"]}
    plain, env = {}, {}
    for key, value in (config.get("headers") or {}).items():
        bearer = re.fullmatch(r"Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}", value, re.IGNORECASE)
        reference = core.ENV_REFERENCE_RE.fullmatch(value)
        if key.lower() == "authorization" and bearer:
            result[fields["bearer"]] = bearer.group(1)
        elif reference:
            env[key] = reference.group(1)
        elif core.ENV_REFERENCE_RE.search(value):
            raise core.AgentPackError(f"{label} header requires a single environment reference: {key}")
        else:
            plain[key] = value
    if plain:
        result[fields["plain"]] = plain
    if env:
        result[fields["env"]] = env
    return result


def _section_name(line: str, root_key: str) -> str | None:
    if not line.lstrip().startswith("["):
        return None
    try:
        table = tomllib.loads(line.strip() + "\n")
        servers = table.get(root_key)
        if isinstance(servers, dict) and len(servers) == 1:
            return next(iter(servers))
    except tomllib.TOMLDecodeError:
        pass
    return None


def _toml_string(value: str) -> str:
    """A TOML basic string. JSON's escapes are valid TOML; keep non-BMP characters literal because JSON would emit
    surrogate pairs, which TOML rejects, and escape DEL, which TOML forbids unescaped."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def render(original: str, entries: dict[str, dict], removed: set[str], root_key: str) -> str:
    """Replace only explicitly owned named tables; retain all other source bytes."""
    output: list[str] = []
    dropping = False
    for line in original.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            name = _section_name(line, root_key)
            dropping = name in removed or name in entries
        if not dropping:
            output.append(line)
    text = "".join(output).rstrip() + "\n" if output else ""
    for name, config in sorted(entries.items()):
        text += f"\n[{root_key}.{_toml_string(name)}]\n"
        for key, value in config.items():
            if isinstance(value, dict):
                encoded = "{ " + ", ".join(_toml_string(k) + " = " + _toml_string(v) for k, v in value.items()) + " }"
            else:
                encoded = _toml_string(value)
            text += f"{key} = {encoded}\n"
    return text


def plan(project: Path, home: Path, client: str, *, replace: bool = False) -> dict:
    import agent_pack_clients as registry  # noqa: PLC0415 - shipped next to this file

    label = registry.get(client)["display"]
    options = registry.mcp_options(client)
    root_key = core.mcp_root_key(client)
    layout = core.load_project_layout(project)
    name = core._package_name(layout.root)
    state_path = home / ".agent-pack" / "state" / f"{client}-remote-{name}.json"
    old = core._read_json(state_path).get("servers", {}) if state_path.exists() else {}
    if not isinstance(old, dict) or not all(isinstance(value, dict) for value in old.values()):
        raise core.AgentPackError(f"invalid {label} MCP ownership state")
    desired = {item.name: native_entry(item.config, options, label) for item in core.inventory_project(layout.root).mcps
               if item.portable and item.config.get("type") != "stdio" and core._mcp_allowed(layout, item.name, client)}
    config_path = core.user_mcp_file(client, home)
    if not desired and not old:
        return {"active": False, "operations": []}
    if config_path.is_symlink():
        raise core.AgentPackError(f"refusing symlinked {label} config: {config_path}")
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    try:
        current = tomllib.loads(original).get(root_key, {})
    except tomllib.TOMLDecodeError as exc:
        raise core.AgentPackError(f"invalid {label} {config_path.name}") from exc
    changes, operations = {}, []
    for server, entry in desired.items():
        if current.get(server) == entry:
            continue
        if server in current and current[server] != old.get(server) and not replace:
            raise core.AgentPackError(f"{label} MCP destination differs: {server}; preview with --replace")
        changes[server] = entry
        operations.append({"action": "update-native-mcp" if server in current else "add-native-mcp", "name": server})
    # A locally changed entry is no longer ours to remove.
    removed = {server for server in old if server not in desired and current.get(server) == old[server]}
    operations += [{"action": "remove-native-mcp", "name": server} for server in sorted(removed)]
    result = render(original, changes, removed, root_key) if operations else original
    try:
        parsed = tomllib.loads(result).get(root_key, {})
        if any(parsed.get(server) != entry for server, entry in desired.items()):
            raise ValueError()
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise core.AgentPackError(
            f"{label} MCP must use named [{root_key}.NAME] tables; convert inline entries first") from exc
    return {"active": True, "path": config_path, "state_path": state_path, "original": original, "text": result,
            "desired": desired, "operations": operations, "existed": config_path.exists(),
            "old_state": state_path.read_text(encoding="utf-8") if state_path.exists() else None,
            "client": client}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A rewrite keeps the file's permissions (mkstemp would otherwise narrow 0644 to 0600); new files stay 0600.
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    handle, temporary = tempfile.mkstemp(prefix=".agent-pack-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        if mode is not None:
            os.chmod(temporary, mode)
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
                _write(recovery / f"{plan['client']}-config.toml", plan["original"])
            _write(plan["path"], plan["text"])
        core._write_json_atomic(plan["state_path"], {"servers": plan["desired"]}, mode=0o600)
    except Exception:
        rollback(plan)
        raise
