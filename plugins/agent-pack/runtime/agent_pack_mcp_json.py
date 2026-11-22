"""``json`` MCP format adapter: how a server entry is written for a client whose MCP configuration is JSON.

The adapter is named for the format. Which key holds the servers (``root_key``, from the client's ``stores.mcp``) and
how an entry is shaped for that client (``remote_url_key``, ``remote_headers``, ``env_interpolation``,
``type_field_required``, ``type_names``, from the client's ``mcp.options``) come from the driver declaration; nothing
here knows a client by name. Pure helpers: they take plain data and return plain data, and write nothing.

``remote_headers`` is ``inline`` (a ``headers`` object travels with the entry), ``split-env`` (the toml adapter splits
them into fields) or ``none`` (the client takes no headers: such an entry is refused). ``env_interpolation`` records what
the client expands inside a value; only ``declared-variables`` changes what is written: a host-environment reference
``${NAME}`` becomes ``${env:NAME}`` because that client expands only references in its own namespace. ``none``,
``plugin-root-only`` and ``shell-env`` all keep the reference as written.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))
import agent_pack_config as core  # noqa: E402

PLUGIN_PLACEHOLDERS = ("PLUGIN_ROOT", "PLUGIN_DATA")


def is_remote(config: dict[str, Any]) -> bool:
    return config.get("type") != "stdio"


def package_entry(config: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """The entry as written into a package's ``mcp.json`` for a client with ``options``.

    Only a remote entry's ``headers`` are shaped here (the package file itself is the portable Agent Plugins format).
    """
    result = dict(config)
    headers = result.get("headers")
    if is_remote(result) and isinstance(headers, dict) and options["remote_headers"] != "split-env":
        if options["remote_headers"] == "none":
            raise core.AgentPackError("this client takes no remote MCP headers")
        if options["env_interpolation"] == "declared-variables":
            result["headers"] = {key: core.ENV_REFERENCE_RE.sub(r"${env:\1}", value) for key, value in headers.items()}
    return result


def native_entry(config: dict[str, Any], plugin_root: Path, data_root: Path, options: dict[str, Any]) -> dict[str, Any]:
    """Project a portable entry onto the client's own ``root_key`` object: the plugin placeholders become absolute paths,
    ``cwd`` is dropped (a plugin entry addresses its files through the plugin root), the type is renamed through
    ``type_names`` (or dropped when the client takes no ``type`` field) and a remote URL goes under ``remote_url_key``."""

    def expand(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("${PLUGIN_ROOT}", str(plugin_root)).replace("${PLUGIN_DATA}", str(data_root))
        if isinstance(value, list):
            return [expand(child) for child in value]
        if isinstance(value, dict):
            return {key: expand(child) for key, child in value.items()}
        return value

    item = expand(dict(config))
    item.pop("cwd", None)
    if item.get("type") in options["type_names"]:
        item["type"] = options["type_names"][item["type"]]
    if not options["type_field_required"]:
        item.pop("type", None)
    if options["remote_url_key"] != "url" and "url" in item:
        item[options["remote_url_key"]] = item.pop("url")
    return item


def prefixed_placeholders(value: Any, prefix: str) -> Any:
    """``${PLUGIN_ROOT}`` / ``${PLUGIN_DATA}`` with ``prefix`` in front of the name (for a client that expands only those)."""
    if isinstance(value, str):
        for name in PLUGIN_PLACEHOLDERS:
            value = value.replace("${" + name + "}", "${" + prefix + name + "}")
        return value
    if isinstance(value, list):
        return [prefixed_placeholders(child, prefix) for child in value]
    if isinstance(value, dict):
        return {key: prefixed_placeholders(child, prefix) for key, child in value.items()}
    return value


def companion_entry(config: dict[str, Any], prefix: str, type_names: dict[str, str]) -> dict[str, Any]:
    """The entry of a package's companion ``mcp`` file: prefixed placeholders; the type is kept only when ``type_names``
    maps it (a type that is not mapped, a plain stdio entry, carries none)."""
    item = prefixed_placeholders(dict(config), prefix)
    kind = item.pop("type", None)
    if kind in type_names:
        item["type"] = type_names[kind]
    return item


def servers(data: Any, root_key: str) -> dict[str, Any]:
    """The servers object of a parsed file; a missing key is no servers, a key that holds anything but an object is an error."""
    found = data.get(root_key, {}) if isinstance(data, dict) else {}
    if not isinstance(found, dict):
        raise core.AgentPackError(f"{root_key} must be an object")
    return found


def with_servers(data: dict[str, Any], root_key: str, changed: dict[str, Any], removed: set[str] = frozenset()) -> dict[str, Any]:
    """``data`` with ``changed`` entries set and ``removed`` names dropped under ``root_key``; every other key is kept."""
    result = dict(data)
    current = dict(servers(result, root_key))
    for name in removed:
        current.pop(name, None)
    current.update(changed)
    result[root_key] = current
    return result
