#!/usr/bin/env python3
"""Read adapters for client configuration files, named by *format* (``json``, ``toml``) and never by client (#134).

A driver declares where a store is, its ``format`` and the key that holds the servers (``root_key``); these functions
only know how to parse a format. Every reader is read-only and never raises on bad input: it returns
``(object, None)`` when the file holds an object, ``(None, None)`` when the file is absent and ``(None, reason)`` when
it exists but is unusable. ``reason`` is one of the English identifiers ``agent_pack_inventory`` renders:
``unreadable``, ``too-large``, ``invalid-json``, ``invalid-toml``, ``unexpected-structure``.

A **0-byte file is an empty object**, not a corrupt one: some clients create their global MCP configuration empty
(Antigravity's ``mcp_config.json`` starts as a 0-byte file). A file holding only whitespace is still invalid.
Standard library only.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, Callable

MAX_CONFIG = 32 * 1024 * 1024


def read_bytes(path: Path, limit: int) -> tuple[bytes | None, str | None]:
    try:
        if path.stat().st_size > limit:
            return None, "too-large"
        return path.read_bytes(), None
    except OSError:
        return None, "unreadable"


def _read(path: Path, parse: Callable[[bytes], Any], invalid: str) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, None
    if not path.is_file():
        return None, "unreadable"
    raw, reason = read_bytes(path, MAX_CONFIG)
    if raw is None:
        return None, reason
    if raw == b"":
        return {}, None
    try:
        data = parse(raw)
    except (UnicodeDecodeError, ValueError):  # JSONDecodeError and TOMLDecodeError are ValueErrors
        return None, invalid
    return (data, None) if isinstance(data, dict) else (None, "unexpected-structure")


def read_json_object(path: Path) -> tuple[dict | None, str | None]:
    return _read(path, lambda raw: json.loads(raw.decode("utf-8")), "invalid-json")


def read_toml_object(path: Path) -> tuple[dict | None, str | None]:
    return _read(path, lambda raw: tomllib.loads(raw.decode("utf-8")), "invalid-toml")


ADAPTERS: dict[str, Callable[[Path], tuple[dict | None, str | None]]] = {"json": read_json_object, "toml": read_toml_object}


def read_object(fmt: str, path: Path) -> tuple[dict | None, str | None]:
    """Parse ``path`` with the adapter of the store ``format`` (``dir`` stores are directories, not parsed here)."""
    adapter = ADAPTERS.get(fmt)
    if adapter is None:
        return None, "unreadable"
    return adapter(path)


def servers(data: dict | None, root_key: str) -> dict | None:
    """The server table under ``root_key`` (``None`` when it is absent or not an object)."""
    value = (data or {}).get(root_key)
    return value if isinstance(value, dict) else None


def section_servers(data: dict, section: str, project: Path, root_key: str) -> tuple[dict | None, bool]:
    """Servers kept per project inside one file: ``data[section][<absolute project path>][root_key]``.

    ``(servers, True)`` when the shape is the expected one (``servers`` is ``{}`` when there is no section or entry for
    the project); ``(None, False)`` when it is anything else, because the layout is documented only as "under that
    project's path" and a different shape must be reported as undetermined, not guessed."""
    projects = data.get(section)
    if projects is None:
        return {}, True
    if not isinstance(projects, dict):
        return None, False
    for key in dict.fromkeys((str(project), str(project.resolve()))):
        if key in projects:
            entry = projects[key]
            if not isinstance(entry, dict):
                return None, False
            found = entry.get(root_key, {})
            if found is None:
                return {}, True
            return (found, True) if isinstance(found, dict) else (None, False)
    return {}, True
