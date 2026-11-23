#!/usr/bin/env python3
"""Read-only observation of the Skills and MCP servers each client can see (``agenthelm inventory``).

The JSON shape is a contract (``schema_version``, documented in ``references/inventory-contract.md``): later write
commands and menus build on it, so keys are only ever appended. Nothing here writes or reads a settings key, and it runs
no client command line itself: a driver's read-only ``detect.cli`` probe runs only when the caller passes its result in
(``probes``, for a client the user named), and its answer is reported apart as ``cli`` evidence. Every Skill / MCP observation
is ``evidence: file`` (read from a file) or ``unknown`` (could not be decided, for example a corrupt file). MCP ``env`` values, ``headers`` values and tokens never appear in any output.

Reuses ``agent_pack_config`` for JSON/TOML readers, the MCP normaliser's secret patterns and version parsing.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))
import agent_pack_config as core  # noqa: E402
from i18n_agent_pack import _, N_  # noqa: E402
import agent_pack_clients as client_registry  # noqa: E402
import agent_pack_store_read as store_read  # noqa: E402

SCHEMA_VERSION = 1
CLIENTS = client_registry.default_ids()
# A separate, read-only target whose MCP file location depends on the platform (declared as ``<per-platform>/<file>``):
# the standalone CLI does not read ``claude_desktop_config.json``. Exactly one built-in driver declares such a store.
DESKTOP = client_registry.per_platform_ids()[0]
ALL_CLIENTS = client_registry.all_ids()  # the default set is CLIENTS; the read-only DESKTOP target must be named (or ``all``)
TYPES = ("skills", "mcp", "all")
SCOPES = ("project", "user", "all")
EVIDENCE = ("file", "setting", "cli", "unknown")  # ``setting`` is reserved: this version never reads a settings key
AGENTHELM_PLUGINS = frozenset({"agent-pack", "agents-init", "opinion-manager", "project-orchestrator"})
AGENTHELM_REPOSITORY = "lc4t/agenthelm"
OVERLAP_THRESHOLD = 0.8  # token Jaccard similarity; a hint only, never a semantic judgement
OVERLAP_MIN_TOKENS = 5
MAX_SKILL_MD = 4 * 1024 * 1024
MAX_CONFIG = store_read.MAX_CONFIG
PROJECT = "<project>"
DESCRIPTION_LIMIT = 1536  # characters of description text shown with --include-description
MAX_DIR_FILES = 20000
# refs of entries in any store but the first one a client declares per scope carry a ``#agents`` suffix, so a ref stays unique
PRIMARY_SKILL_STORE = client_registry.primary_skill_stores()

# The official documentation the precedence conclusions rest on (read 2026-11-11, see references/inventory-contract.md)
# is the ``source_url`` of the ``precedence`` entries in the driver declarations.
DOC_DESKTOP_PATHS = "https://modelcontextprotocol.io/docs/develop/connect-local-servers"
DOC_DESKTOP_MCP = "https://code.claude.com/docs/en/desktop#mcp-servers-from-the-claude-desktop-chat-app"
DESKTOP_FILE = "claude_desktop_config.json"
DESKTOP_STORE_NAME = "desktop-config"  # the ``store`` label of Desktop entries; the real path is in ``desktop.path``
DESKTOP_STORES = {  # official documentation (MCP site, read 2026-11-18); Windows (%APPDATA%\\Claude) is documentation only
    "darwin": f"~/Library/Application Support/Claude/{DESKTOP_FILE}",
    "linux": f"~/.config/Claude/{DESKTOP_FILE}",
}

# (scope, store) per client, in listing order, read from the driver registry. ``~`` is the home directory,
# ``<project>`` the project root (``PROJECT``). ``*_DECLS`` hold the same stores with all declared attributes
# (``format``, ``root_key``, ``managed_by``, ``trust_required``): what the readers below act on.
SKILL_STORES = client_registry.store_table("skills")
MCP_STORES = {client: stores for client, stores in client_registry.store_table("mcp").items() if client != DESKTOP}
SKILL_DECLS = {client: client_registry.stores(client, "skills") for client in SKILL_STORES}
MCP_DECLS = {client: client_registry.stores(client, "mcp") for client in MCP_STORES}
SCOPE_ORDER = {"user": 0, "local": 1, "project": 2}

# Reason codes in ``unknown[]`` and per-entry ``issues[]`` / ``notes[]`` stay English identifiers (data); the text
# renderer translates them.
REASON_TEXT = {
    "invalid-json": N_("JSON 无法解析"),
    "invalid-toml": N_("TOML 无法解析"),
    "unreadable": N_("无法读取"),
    "too-large": N_("文件过大，未读取"),
    "unexpected-structure": N_("结构不是预期的对象"),
    "not-found": N_("文件不存在"),
    "platform-unsupported": N_("本命令只解析 macOS 与 Linux 的路径"),
    "claude-local-structure-unrecognized": N_("~/.claude.json 里按项目路径存放的条目结构无法识别"),
    "broken-symlink": N_("符号链接已失效"),
    "frontmatter-missing": N_("SKILL.md 没有可读的 frontmatter"),
    "description-missing": N_("frontmatter 没有 description"),
    "name-mismatch": N_("frontmatter 的 name 与目录名不同"),
    "skill-md-too-large": N_("SKILL.md 过大，未解析"),
    "variable-expansion-unconfirmed": N_("官方文档没有说明该文件是否展开 ${VAR}"),
    "dir-too-large": N_("目录文件过多，未统计大小与文件数"),
    "codex-project-requires-trust": N_("Codex 只在受信任的项目里读取项目级配置；本工具读不到信任记录"),
}


# ------------------------------------------------------------------ paths and redaction

def display_path(path: Path | str, home: Path, project: Path | None) -> str:
    """``~``-abbreviated and ``<project>``-relative form of an absolute path, so output never leaks the home layout."""
    text = str(path)
    for base, label in ((project, PROJECT), (home, "~")):
        if base is None:
            continue
        prefix = str(base)
        if text == prefix:
            return label
        if text.startswith(prefix + os.sep):
            return label + text[len(prefix):]
    return text


def resolve_store(store: str, home: Path, project: Path | None) -> Path | None:
    if store.startswith(PROJECT):
        return None if project is None else project / store[len(PROJECT) + 1:]
    if store.startswith("~"):
        return home / store[2:].split("#", 1)[0]
    return Path(store)


ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")
_VAR_DEFAULT_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-[^}]*\}")
REDACTED = "<redacted>"


def _plain_variables(text: str) -> str:
    """``${VAR:-default}`` keeps only the variable name: a default value is a value."""
    return _VAR_DEFAULT_RE.sub(lambda match: "${" + match.group(1) + "}", text)


def _safe_reference(text: str) -> bool:
    return core._safe_env_reference(text)


def _redact_args(args: list[Any]) -> list[str]:
    """Arguments with credential-looking values replaced; ``${VAR}`` references (names) stay."""
    result: list[str] = []
    previous_secret_flag = False
    for raw in args:
        text = _plain_variables(str(raw))
        secret = False
        if previous_secret_flag and not text.startswith("-") and not _safe_reference(text):
            result.append(REDACTED)
            previous_secret_flag = False
            continue
        previous_secret_flag = False
        if text.startswith("-") and "=" in text:
            flag, value = text.split("=", 1)
            if (core.SECRET_NAME_RE.search(flag) or core._secret_like_arg(value)) and not _safe_reference(value):
                result.append(f"{flag}={REDACTED}")
                continue
        elif text.startswith("-") and core.SECRET_NAME_RE.search(text):
            previous_secret_flag = True
        if core.SECRET_VALUE_RE.search(text) and not _safe_reference(text):
            secret = True
        elif not text.startswith("-") and not _safe_reference(text) and core.looks_like_token(text):
            secret = True
        result.append(REDACTED if secret else text)
    return result


def _credential_query(key: str, value: str) -> bool:
    return bool(core.SECRET_NAME_RE.search(key) or core.SECRET_VALUE_RE.search(value) or core.looks_like_token(value))


def _url_has_credential(url: str | None) -> bool:
    if not url:
        return False
    try:
        parts = urlsplit(_plain_variables(url))
        return bool(parts.username or parts.password or any(
            not _safe_reference(val) and _credential_query(key, val) for key, val in parse_qsl(parts.query)))
    except ValueError:
        return False


def _redact_url(value: str) -> str:
    text = _plain_variables(value)
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        netloc = host + (f":{parts.port}" if parts.port else "")
        query = "&".join(
            f"{key}={val if _safe_reference(val) else REDACTED}" if val else key
            for key, val in parse_qsl(parts.query, keep_blank_values=True))
        return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
    except ValueError:
        return REDACTED


def _host(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ file reading (never raises)

# Adapters named by format live in ``agent_pack_store_read``; a 0-byte file reads as an empty object.
_read_bytes = store_read.read_bytes
read_json_object = store_read.read_json_object
read_toml_object = store_read.read_toml_object


# ------------------------------------------------------------------ skills

_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$")


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Top-level ``key: value`` pairs of a leading ``---`` block (plain, quoted and ``>`` / ``|`` block scalars)."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        return None
    result: dict[str, str] = {}
    index = 1
    while index < end:
        match = _KEY_RE.match(lines[index])
        index += 1
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        block: list[str] = []
        while index < end and (lines[index].startswith((" ", "\t")) or not lines[index].strip()):
            block.append(lines[index].strip())
            index += 1
        if value[:1] in (">", "|"):
            joiner = "\n" if value[:1] == "|" else " "
            value = joiner.join(part for part in block if part)
        elif not value:
            value = " ".join(part for part in block if part)
        elif block:
            value = " ".join([value, *[part for part in block if part]])
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def _plugin_manifest(path: Path) -> dict | None:
    data, _reason = read_json_object(path / "plugin.json")
    return data


def known_plugin_versions(home: Path) -> dict[str, tuple[str, ...]]:
    """Versions of each agenthelm plugin found in the client plugin roots of this machine."""
    found: dict[str, list[str]] = {}
    roots: list[Path] = []
    for client in CLIENTS:
        try:
            for parent in core._client_plugin_roots(client, home):
                if parent.is_dir():
                    roots.extend(child for child in sorted(parent.iterdir()) if child.is_dir())
            roots.extend(core._client_marketplace_plugins(client, home))
        except (OSError, core.AgentPackError):
            continue
    for root in roots:
        manifest = _plugin_manifest(root)
        if manifest and manifest.get("name") in AGENTHELM_PLUGINS and isinstance(manifest.get("version"), str):
            found.setdefault(manifest["name"], []).append(manifest["version"])
    return {name: tuple(versions) for name, versions in found.items()}


def _state_skill_names(home: Path) -> dict[str, str]:
    """Skill name -> package name, from the Claude sync state records (``~/.claude/agent-pack/state/<package>.json``)."""
    owned: dict[str, str] = {}
    directory = home / ".claude" / "agent-pack" / "state"
    if not directory.is_dir():
        return owned
    for path in sorted(directory.glob("*.json")):
        data, _reason = read_json_object(path)
        if not data or data.get("schema_version") != 1:
            continue
        skills = data.get("skills")
        for name in skills if isinstance(skills, list) else ():
            if isinstance(name, str):
                owned.setdefault(name, path.stem)
    return owned


def _state_mcp_names(home: Path) -> dict[str, str]:
    owned: dict[str, str] = {}
    directory = home / ".claude" / "agent-pack" / "state"
    if not directory.is_dir():
        return owned
    for path in sorted(directory.glob("*.json")):
        data, _reason = read_json_object(path)
        if not data or data.get("schema_version") != 1:
            continue
        mcps = data.get("mcps")
        for name in mcps if isinstance(mcps, dict) else ():
            owned.setdefault(str(name), path.stem)
    return owned


def _origin(kind: str, detail: str | None, otype: str, oid: str | None, version: str | None, latest: str,
            evidence: str = "file") -> dict:
    """``kind`` / ``detail`` are the first-version keys; ``type`` / ``id`` / ``version`` / ``latest_known`` /
    ``evidence`` are the structured form (``latest_known`` is the highest version found on this machine, offline)."""
    return {"kind": kind, "detail": detail, "type": otype, "id": oid, "version": version, "latest_known": latest,
            "evidence": evidence}


def _git_root(real: Path, home: Path) -> Path | None:
    for parent in real.parents:
        if parent == home or parent == parent.parent:
            return None
        if (parent / ".git").exists():
            return parent
    return None


def _skill_origin(entry: Path, name: str, versions: dict[str, tuple[str, ...]],
                  state: dict[str, str], home: Path) -> tuple[dict, str | None, Any]:
    """(origin, version, stale): only an entry whose source and version can be read gets a stale verdict."""
    unknown = _origin("unknown", None, "unknown", None, None, "unknown", "unknown")
    try:
        real = entry.resolve()
    except OSError:
        return unknown, None, "unknown"
    manifest = _plugin_manifest(real.parent.parent) if real.parent.name == "skills" else None
    if (manifest and manifest.get("name") in AGENTHELM_PLUGINS and isinstance(manifest.get("version"), str)
            and AGENTHELM_REPOSITORY in str(manifest.get("repository", ""))):
        plugin, version = manifest["name"], manifest["version"]
        newest = max([*versions.get(plugin, ()), version], key=core._version_tuple)
        stale: Any = core._version_tuple(version) < core._version_tuple(newest)
        return _origin("agenthelm-plugin", f"{plugin}@{version}", "plugin", plugin, version, newest), version, stale
    if name in state and entry.is_symlink():  # ``state`` is only handed to a store declared ``managed_by``
        return _origin("state-record", state[name], "unknown", state[name], None, "unknown"), None, "unknown"
    if manifest and isinstance(manifest.get("name"), str) and any(part in real.parts for part in ("cache", "marketplaces")) \
            and "plugins" in real.parts:
        version = manifest.get("version") if isinstance(manifest.get("version"), str) else None
        return _origin("unknown", None, "marketplace", manifest["name"], version, "unknown"), version, "unknown"
    if entry.is_symlink():
        root = _git_root(real, home)
        if root is not None:
            return _origin("unknown", None, "git", root.name, None, "unknown"), None, "unknown"
    return unknown, None, "unknown"


def _link_kind(entry: Path, home: Path) -> str | None:
    if not entry.is_symlink():
        return None
    try:
        raw = os.readlink(entry)
    except OSError:
        return None
    if not os.path.isabs(raw):
        return "relative"
    return "absolute" if raw == str(home) or raw.startswith(str(home) + os.sep) else "outside-home"


def _dir_stats(entry: Path) -> tuple[int | None, int | None]:
    """(total bytes, file count) of the skill directory; symlinks inside are counted, not followed."""
    total = count = 0
    try:
        for current, dirs, files in os.walk(entry, followlinks=False):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
            for file in files:
                count += 1
                if count > MAX_DIR_FILES:
                    return None, None
                try:
                    total += os.lstat(os.path.join(current, file)).st_size
                except OSError:
                    pass
    except OSError:
        return None, None
    return total, count


def observe_skill(entry: Path, client: str, scope: str, store: str, home: Path, project: Path | None,
                  versions: dict[str, tuple[str, ...]], state: dict[str, str],
                  include_description: bool = False) -> dict | None:
    """One skill directory entry, or ``None`` when it is not a skill (no ``SKILL.md``)."""
    name = entry.name
    link = entry.is_symlink()
    target = None
    if link:
        try:
            target = display_path(os.readlink(entry), home, project)
        except OSError:
            target = None
    record: dict[str, Any] = {
        "name": name, "client": client, "scope": scope, "store": store,
        "path": f"{store}/{name}",
        "frontmatter": None, "description_bytes": 0, "skill_md_bytes": 0, "content_sha256": None,
        "origin": _origin("unknown", None, "unknown", None, None, "unknown", "unknown"),
        "symlink": {"is_symlink": link, "target": target},
        "version": None, "stale": "unknown", "evidence": "file", "issues": [],
        "description_chars": 0, "when_to_use_chars": 0, "when_to_use_bytes": 0, "skill_md_lines": 0,
        "dir_bytes": None, "file_count": None, "is_symlink": link, "link_target_kind": _link_kind(entry, home),
        "name_matches_dir": None, "frontmatter_ok": False,
        "loaded": "unknown", "precedence": "unique", "shadowed_by": None,
    }
    skill_md = entry / "SKILL.md"
    try:
        isdir = entry.is_dir()
        has_md = skill_md.is_file()
    except OSError:
        isdir = has_md = False
    if link and not isdir:
        record["issues"].append("broken-symlink")
        record["evidence"] = "unknown"
        return record
    if not isdir or not has_md:
        return None
    record["origin"], record["version"], record["stale"] = _skill_origin(entry, name, versions, state, home)
    record["dir_bytes"], record["file_count"] = _dir_stats(entry.resolve() if link else entry)
    if record["file_count"] is None:
        record["issues"].append("dir-too-large")
    raw, reason = _read_bytes(skill_md, MAX_SKILL_MD)
    if raw is None:
        record["issues"].append("skill-md-too-large" if reason == "too-large" else "frontmatter-missing")
        record["evidence"] = "unknown"
        try:
            record["skill_md_bytes"] = skill_md.stat().st_size
        except OSError:
            pass
        return record
    record["skill_md_bytes"] = len(raw)
    record["content_sha256"] = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace")
    record["skill_md_lines"] = len(text.splitlines())
    meta = parse_frontmatter(text)
    if meta is None:
        record["issues"].append("frontmatter-missing")
        return record
    record["frontmatter"] = {"name": meta.get("name")}
    description = meta.get("description")
    if description is None:
        record["issues"].append("description-missing")
    else:
        record["description_bytes"] = len(description.encode("utf-8"))
        record["description_chars"] = len(description)
        record["_description"] = description  # for the overlap hints; removed before output
        if include_description:
            record["frontmatter"]["description"] = description[:DESCRIPTION_LIMIT]
    when = meta.get("when_to_use")
    if when is not None:
        record["when_to_use_chars"], record["when_to_use_bytes"] = len(when), len(when.encode("utf-8"))
    record["name_matches_dir"] = meta.get("name") == name if meta.get("name") is not None else None
    if meta.get("name") is not None and meta["name"] != name:
        record["issues"].append("name-mismatch")
    record["frontmatter_ok"] = bool(meta.get("name") and description)
    return record


def _scan_skill_store(client: str, scope: str, store: str, root: Path, home: Path, project: Path | None,
                      versions: dict[str, tuple[str, ...]], state: dict[str, str],
                      include_description: bool = False) -> list[dict]:
    try:
        if not root.is_dir():
            return []
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    found = []
    for child in children:
        if child.name.startswith("."):
            continue
        record = observe_skill(child, client, scope, store, home, project, versions, state, include_description)
        if record is not None:
            found.append(record)
    return found


# ------------------------------------------------------------------ MCP

def _names(mapping: Any) -> list[str]:
    return sorted(str(key) for key in mapping) if isinstance(mapping, dict) else []


def normalize_mcp(name: str, raw: Any, client: str, scope: str, store: str, home: Path,
                  state: dict[str, str], trust_required: bool = False, url_key: str = "url",
                  disabled_key: str | None = None) -> dict:
    """A server entry reduced to what may be shown: no ``env`` / ``headers`` values, no tokens.

    ``state`` is the sync-state record of a store declared ``managed_by`` (empty otherwise); ``trust_required`` is the
    store's declaration that the client reads it only in a trusted project. ``url_key`` is the key the client keeps a remote
    URL under and ``disabled_key`` the key it marks a switched-off server with (``mcp.options`` of its driver)."""
    item = raw if isinstance(raw, dict) else {}
    command = item.get("command") if isinstance(item.get("command"), str) else None
    url = item.get(url_key) if isinstance(item.get(url_key), str) else None
    declared = str(item.get("type") or item.get("transport") or "").lower()
    if declared in ("stdio", "sse", "http"):
        transport = declared
    elif declared in ("streamable-http", "streamable_http"):
        transport = "http"
    elif command:
        transport = "stdio"
    elif url:
        transport = "http"
    else:
        transport = "unknown"
    args = _redact_args(item["args"]) if isinstance(item.get("args"), list) else []
    env_names = set(_names(item.get("env")))
    env_vars = item.get("env_vars")
    if isinstance(env_vars, list):
        env_names.update(str(value) for value in env_vars if isinstance(value, str))
    header_names = set(_names(item.get("headers"))) | set(_names(item.get("http_headers"))) \
        | set(_names(item.get("env_http_headers")))
    if item.get("bearer_token_env_var"):
        header_names.add("Authorization")
    shown_command = _plain_variables(display_path(command, home, None)) if command else None
    shown_url = _redact_url(url) if url else None
    if transport == "stdio" and shown_command:
        fingerprint = _digest("stdio", shown_command, *args)
    elif shown_url:
        fingerprint = _digest("url", shown_url)
    else:
        fingerprint = None
    refs = set(ENV_REF.findall(" ".join(filter(None, [command or "", url or "", *map(str, item["args"] if isinstance(item.get("args"), list) else [])]))))
    for container in ("env", "headers", "http_headers"):
        if isinstance(item.get(container), dict):
            for value in item[container].values():
                refs.update(ENV_REF.findall(str(value)))
    secret = any(part == REDACTED or part.endswith("=" + REDACTED) for part in args) or _url_has_credential(url)
    for container in ("env", "headers", "http_headers"):
        if isinstance(item.get(container), dict):
            for key, value in item[container].items():
                text = str(value)
                if not _safe_reference(text) and not ENV_REF.fullmatch(text) and (
                        core.SECRET_NAME_RE.search(str(key)) or core.SECRET_VALUE_RE.search(text) or core.looks_like_token(text)):
                    secret = True
    record: dict[str, Any] = {
        "name": name, "client": client, "scope": scope, "store": store, "transport": transport,
        "command": shown_command, "args": args,
        "url": shown_url, "env_names": sorted(env_names), "header_names": sorted(header_names),
        "fingerprint": fingerprint,
        "origin": {"kind": "unknown", "detail": None},
        "evidence": "file", "notes": [],
        "env_var_names": sorted(env_names | refs), "has_inline_secret": secret,
        "fingerprint_fields": ["command", "args"] if fingerprint and transport == "stdio" else (["url"] if fingerprint else []),
        "url_host": _host(shown_url) if shown_url and transport != "stdio" else None,
        "loaded": "unknown", "precedence": "unique", "shadowed_by": None,
    }
    if name in state:
        record["origin"] = {"kind": "state-record", "detail": state[name]}
    if trust_required:
        record["notes"].append("codex-project-requires-trust")
    if disabled_key and item.get(disabled_key) is True:
        record["loaded"] = "no"   # the file itself switches this server off
    if not command and not url:
        record["evidence"] = "unknown"
    return record


_servers = store_read.servers


def _observe_mcp_store(client: str, decl: dict, home: Path, project: Path | None,
                       state: dict[str, str], unknown: list[dict]) -> list[dict]:
    """Entries of one declared MCP store, read with the adapter of its ``format`` from its ``root_key``.

    A ``#section`` in the path (``~/.file.json#projects``) means the servers are kept per project inside that file:
    ``data[section][<absolute project path>][root_key]``; any other shape of it is ``unknown``."""
    scope, store, root_key = decl["scope"], decl["path"], decl["root_key"]
    path = resolve_store(store, home, project)
    if path is None:
        return []
    data, reason = store_read.read_object(decl["format"], path)
    section = store.split("#", 1)[1] if "#" in store else None
    if section:
        if data is None:
            servers = None
        elif project is None:
            servers = {}
        else:
            servers, shaped = store_read.section_servers(data, section, project, root_key)
            reason = reason or (None if shaped else "claude-local-structure-unrecognized")
    else:
        servers = _servers(data, root_key)
    if reason:
        unknown.append({"store": store, "client": client, "reason": reason})
        return []
    if servers is None:
        if data is not None and not section and data.get(root_key) is not None:
            unknown.append({"store": store, "client": client, "reason": "unexpected-structure"})
        return []
    scoped = state if decl.get("managed_by") else {}
    options = client_registry.mcp_options(client)
    return [normalize_mcp(str(name), raw, client, scope, store, home, scoped, bool(decl.get("trust_required")),
                          options["remote_url_key"], options["disabled_key"])
            for name, raw in sorted(servers.items())]


# ------------------------------------------------------------------ Claude Desktop (claude_desktop_config.json)

def desktop_store(platform: str | None = None) -> str | None:
    """The documented ``claude_desktop_config.json`` location for this platform (``None`` where it is not resolved)."""
    name = sys.platform if platform is None else platform
    return DESKTOP_STORES.get("darwin" if name == "darwin" else "linux" if name.startswith("linux") else "")


def desktop_info(home: Path, platform: str | None = None) -> dict:
    """``{path, exists, parse_ok}`` of ``claude_desktop_config.json`` (``parse_ok`` is ``null`` when absent)."""
    store = desktop_store(platform)
    path = resolve_store(store, home, None) if store else None
    if path is None:
        return {"path": None, "exists": False, "parse_ok": None}
    exists = path.exists()
    data, reason = read_json_object(path) if exists else (None, None)
    return {"path": display_path(path, home, None), "exists": exists,
            "parse_ok": (reason is None and data is not None) if exists else None}


def observe_desktop(home: Path, platform: str | None = None) -> tuple[list[dict], list[dict]]:
    """(entries, unknown) of ``claude_desktop_config.json``; a missing or corrupt file is ``unknown``, never an error."""
    store = desktop_store(platform)
    label = DESKTOP_STORE_NAME
    if store is None:
        return [], [{"store": label, "client": DESKTOP, "reason": "platform-unsupported"}]
    path = resolve_store(store, home, None)
    data, reason = read_json_object(path) if path is not None else (None, "unreadable")
    if reason is None and data is None:
        reason = "not-found"
    if reason:
        return [], [{"store": label, "client": DESKTOP, "reason": reason}]
    root_key = client_registry.stores(DESKTOP, "mcp")[0]["root_key"]
    servers = _servers(data, root_key)
    if servers is None:
        return [], ([{"store": label, "client": DESKTOP, "reason": "unexpected-structure"}] if root_key in data else [])
    entries = [normalize_mcp(str(name), raw, DESKTOP, "user", label, home, {}) for name, raw in sorted(servers.items())]
    for entry in entries:
        if any("${" in str(value) for value in (entry["command"], entry["url"], *entry["args"])):
            entry["notes"].append("variable-expansion-unconfirmed")
    return entries, []


def desktop_report(home: Path, platform: str | None = None) -> dict:
    """What ``doctor --client claude-desktop`` shows: read-only, nothing here is a drift issue."""
    entries, unknown = observe_desktop(home.expanduser().resolve(), platform)
    report = {"client": DESKTOP, "store": DESKTOP_STORE_NAME, "path": desktop_info(home.expanduser().resolve(), platform)["path"],
              "evidence": "unknown" if unknown else "file",
              "servers": [entry["name"] for entry in entries], "unknown": unknown, "source_url": DOC_DESKTOP_PATHS}
    report["warnings"] = [_("{0}：{1}（{2}）").format(DESKTOP, item["store"], _(REASON_TEXT.get(item["reason"], item["reason"])))
                          for item in unknown if item["reason"] != "not-found"]
    return report


def desktop_shadow_findings(home: Path, names: list[str], platform: str | None = None) -> list[dict]:
    """Advisory: servers the project manages (``names``) that ``claude_desktop_config.json`` also defines.

    The Desktop app loads that file's servers into local Code tab sessions alongside ``~/.claude.json`` and ``.mcp.json``
    (https://code.claude.com/docs/en/desktop, read 2026-11-18). An earlier reading (design #82, 2026-11-11) quoted a
    sentence that the Code tab then uses the ``claude_desktop_config.json`` definition; the page read on 2026-11-18 no
    longer contains it, so the precedence is reported as unconfirmed. Applies to the Desktop local Code tab only; the
    standalone CLI is unaffected.
    """
    entries, _unknown = observe_desktop(home.expanduser().resolve(), platform)
    wanted = set(names)
    return [{"name": entry["name"], "by": DESKTOP_FILE, "store": entry["store"], "applies_to": "desktop-local-code-tab",
             "cli_affected": False, "severity": "advisory", "evidence": "file", "precedence_confirmed": False,
             "source_url": DOC_DESKTOP_MCP} for entry in entries if entry["name"] in wanted]


def shadow_warning(item: dict) -> str:
    return _("claude：{0} 在 claude_desktop_config.json 里也有定义；Claude Desktop 的本地 Code 标签可能改用那一份（官方文档当前没有明确同名优先级），独立 CLI 不受影响").format(item["name"])


# ------------------------------------------------------------------ duplicates

def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    cjk = re.findall("[\\u4e00-\\u9fff]", text)
    bigrams = [a + b for a, b in zip(cjk, cjk[1:])]
    return set(words) | set(bigrams)


def overlap_hints(skills: list[dict]) -> list[dict]:
    """Different names with strongly overlapping descriptions (token Jaccard). A mechanical hint, not a judgement."""
    hints = []
    by_client: dict[str, list[dict]] = {}
    for item in skills:
        description = item.get("_description")
        if description:
            by_client.setdefault(item["client"], []).append({**item, "_t": _tokens(description)})
    for client, items in sorted(by_client.items()):
        for index, left in enumerate(items):
            for right in items[index + 1:]:
                if left["name"] == right["name"] or min(len(left["_t"]), len(right["_t"])) < OVERLAP_MIN_TOKENS:
                    continue
                union = left["_t"] | right["_t"]
                similarity = len(left["_t"] & right["_t"]) / len(union) if union else 0.0
                if similarity >= OVERLAP_THRESHOLD:
                    hints.append({"client": client, "names": sorted([left["name"], right["name"]]),
                                  "paths": sorted([left["path"], right["path"]]),
                                  "similarity": round(similarity, 2), "basis": "description-token-jaccard",
                                  "hint_only": True})
    return sorted(hints, key=lambda hint: (hint["client"], hint["names"], hint["paths"]))


def _real(entry_path: Path | None) -> str | None:
    try:
        return str(entry_path.resolve()) if entry_path is not None else None
    except OSError:
        return None


def _effective(client: str, kind: str, group: list[dict]) -> dict:
    """The precedence conclusion for a same-name group, from the driver's declared ``precedence`` entries.

    Entries of ``kind`` are tried in order; one without ``when`` always applies. ``when`` is ``{scopes, match}``: the
    group's set of scopes must equal (``exact``) or contain every one of (``includes``) the listed scopes. ``winner``
    names the scope whose store is the effective one (the first store of that scope in the group). Nothing declared
    means ``unknown``."""
    scopes = {item["scope"] for item in group}
    for entry in client_registry.precedence(client, kind):
        when = entry.get("when")
        if when:
            wanted = set(when["scopes"])
            if not (scopes == wanted if when["match"] == "exact" else wanted <= scopes):
                continue
        winner = entry.get("winner")
        store = next(item["store"] for item in group if item["scope"] == winner) if winner else None
        return {"store": store, "rule": entry["rule"], "evidence": entry["evidence"], "source_url": entry["source_url"]}
    return {"store": None, "rule": "unknown", "evidence": "unknown", "source_url": None}


def _skill_effective(client: str, group: list[dict]) -> dict:
    return _effective(client, "skills", group)


def skill_duplicates(skills: list[dict], real_paths: dict[str, str]) -> list[dict]:
    result = []
    by_key: dict[tuple[str, str], list[dict]] = {}
    for item in skills:
        by_key.setdefault((item["client"], item["name"]), []).append(item)
    for (client, name), group in sorted(by_key.items()):
        stores = sorted({item["path"] for item in group})
        if len(stores) < 2:
            continue
        reals = {real_paths.get(item["path"]) for item in group}
        hashes = {item["content_sha256"] for item in group}
        if None in hashes:
            relation = "unknown"
        elif len(reals) == 1 and None not in reals:
            relation = "same-file"
        elif len(hashes) == 1:
            relation = "identical"
        else:
            relation = "different"
        result.append({"client": client, "name": name, "stores": stores, "relation": relation,
                       "effective": _skill_effective(client, group) if relation != "same-file" else None})
    return result


def _mcp_effective(client: str, group: list[dict]) -> dict:
    return _effective(client, "mcp", group)


def mcp_duplicates(mcps: list[dict]) -> list[dict]:
    result = []
    by_name: dict[tuple[str, str], list[dict]] = {}
    by_print: dict[tuple[str, str], list[dict]] = {}
    for item in mcps:
        by_name.setdefault((item["client"], item["name"]), []).append(item)
        if item["fingerprint"]:
            by_print.setdefault((item["client"], item["fingerprint"]), []).append(item)
    for (client, name), group in sorted(by_name.items()):
        stores = sorted({item["store"] for item in group})
        if len(stores) < 2:
            continue
        prints = {item["fingerprint"] for item in group}
        relation = "unknown" if None in prints else ("identical" if len(prints) == 1 else "different")
        result.append({"kind": "same-name", "client": client, "names": [name], "stores": stores,
                       "relation": relation, "effective": _mcp_effective(client, group)})
    for (client, fingerprint), group in sorted(by_print.items()):
        names = sorted({item["name"] for item in group})
        if len(names) < 2:
            continue
        result.append({"kind": "same-fingerprint", "client": client, "names": names,
                       "stores": sorted({item["store"] for item in group}), "relation": "identical",
                       "fingerprint": fingerprint, "effective": None})
    return result


def entry_ref(item: dict, kind: str) -> str:
    """``<kind>:<name>@<client>:<scope>``; a skill outside the client's primary store for that scope gets ``#agents``."""
    ref = f"{kind}:{item['name']}@{item['client']}:{item['scope']}"
    if kind == "skill" and item["store"] != PRIMARY_SKILL_STORE.get((item["client"], item["scope"]), item["store"]):
        ref += "#agents"
    return ref


def _annotate(skills: list[dict], mcps: list[dict], duplicates: dict) -> None:
    """Stable ``ref`` for every entry, and ``precedence`` / ``shadowed_by`` from the duplicate groups' ``effective``."""
    for kind, items, groups in (("skill", skills, duplicates["skills"]), ("mcp", mcps, duplicates["mcps"])):
        for item in items:
            item["ref"] = entry_ref(item, kind)
        for group in groups:
            if group.get("kind", "same-name") != "same-name" or group["relation"] == "same-file":
                continue
            effective = group.get("effective") or {}
            for item in items:
                if item["client"] != group["client"] or item["name"] not in (group.get("names") or [group.get("name")]):
                    continue
                if (item["path"] if kind == "skill" else item["store"]) not in group["stores"]:
                    continue
                if effective.get("rule") == "both-load":
                    item["precedence"] = "both-load"
                elif effective.get("store"):
                    won = item["store"] == effective["store"]
                    item["precedence"] = "winner" if won else "shadowed"
                    if not won:
                        item["shadowed_by"] = {"store": effective["store"], "rule": effective["rule"],
                                               "evidence": effective["evidence"], "source_url": effective["source_url"]}
                else:
                    item["precedence"] = "unknown"


# ------------------------------------------------------------------ entry point

def clients_with(client: str) -> list[str] | None:
    """What an observation that must see ``client`` covers: ``None`` (the default clients) when it is one of them, else the
    default clients plus it when it is another registered client (a reference to an entry of a client outside the default
    set resolves; the default clients' entries stay what they were)."""
    return None if client in CLIENTS or client not in ALL_CLIENTS else [*CLIENTS, client]


def observe(home: Path, project: Path | None = None, clients: list[str] | None = None, types: str = "all",
            scope: str = "all", platform: str | None = None, include_description: bool = False,
            probes: dict[str, dict] | None = None) -> dict:
    """Everything the chosen clients can see, as the ``schema_version`` 1 document. Read-only and never raises on
    bad input files (they become ``unknown`` entries). ``probes`` maps a client id to the result of its ``detect.cli`` probe
    (``agent_pack_probe.run_probe``); when given, the report gains a ``cli`` key with that evidence."""
    home = home.expanduser().resolve()
    project = project.expanduser().resolve() if project is not None else None
    requested = list(ALL_CLIENTS) if clients and "all" in clients else (clients or list(CLIENTS))
    chosen = [client for client in ALL_CLIENTS if client in requested]
    want_skills, want_mcp = types in ("skills", "all"), types in ("mcp", "all")

    def wanted(item_scope: str) -> bool:
        return scope == "all" or item_scope == scope or (scope == "project" and item_scope == "local")

    versions = known_plugin_versions(home) if want_skills else {}
    skill_state = _state_skill_names(home) if want_skills else {}
    mcp_state = _state_mcp_names(home) if want_mcp else {}
    skills: list[dict] = []
    mcps: list[dict] = []
    unknown: list[dict] = []
    real_paths: dict[str, str] = {}
    for client in chosen:
        if client == DESKTOP:
            if want_mcp and wanted("user"):  # Desktop has no Skill directory and no project level
                found, missing = observe_desktop(home, platform)
                mcps.extend(found)
                unknown.extend(missing)
            continue
        if want_skills:
            for decl in SKILL_DECLS.get(client, ()):
                item_scope, store = decl["scope"], decl["path"]
                root = resolve_store(store, home, project)
                if root is None or not wanted(item_scope):
                    continue
                for record in _scan_skill_store(client, item_scope, store, root, home, project, versions,
                                                skill_state if decl.get("managed_by") else {}, include_description):
                    skills.append(record)
                    real = _real(root / record["name"])
                    if real:
                        real_paths[record["path"]] = real
        if want_mcp:
            for decl in MCP_DECLS.get(client, ()):
                if wanted(decl["scope"]):
                    mcps.extend(_observe_mcp_store(client, decl, home, project, mcp_state, unknown))
    order = {client: index for index, client in enumerate(ALL_CLIENTS)}
    skills.sort(key=lambda item: (order[item["client"]], SCOPE_ORDER[item["scope"]], item["store"], item["name"]))
    mcps.sort(key=lambda item: (order[item["client"]], SCOPE_ORDER[item["scope"]], item["store"], item["name"]))
    duplicates = {"skills": skill_duplicates(skills, real_paths), "mcps": mcp_duplicates(mcps)}
    hints = overlap_hints(skills)
    _annotate(skills, mcps, duplicates)
    for item in skills:
        item.pop("_description", None)
    summary: dict[str, dict] = {}
    for client in chosen:
        mine = [item for item in skills if item["client"] == client]
        distinct: dict[str, int] = {}
        for item in mine:
            distinct.setdefault(real_paths.get(item["path"], item["path"]), item["description_bytes"])
        summary[client] = {
            "skill_count": len(mine),
            "description_bytes_total": sum(item["description_bytes"] for item in mine),
            "description_bytes_distinct_files": sum(distinct.values()),
            "mcp_count": sum(1 for item in mcps if item["client"] == client),
        }
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "agenthelm inventory",
        "project": display_path(project, home, None) if project else None,
        "clients": chosen,
        "filters": {"type": types, "scope": scope},
        "skills": skills,
        "mcps": mcps,
        "summary": {"per_client": summary},
        "duplicates": duplicates,
        "overlap_hints": hints,
        "unknown": unknown,
    }
    if DESKTOP in chosen:
        report["desktop"] = desktop_info(home, platform)
    if probes:
        report["cli"] = {client: cli_section(result, home, project) for client, result in probes.items() if client in chosen}
    return report


def cli_section(result: dict, home: Path, project: Path | None) -> dict:
    """One client's ``cli`` evidence: the plugins its own plugin list reports (with their enabled state and what each carries),
    or why the list could not be read (a failed probe is no evidence of anything)."""
    import agent_pack_probe  # noqa: PLC0415 - shipped next to this file

    if not result.get("ok"):
        return {"evidence": "unknown", "ok": False, "reason": result.get("reason", "unreadable"), "plugins": []}
    plugins = []
    for entry in agent_pack_probe.plugin_entries(result):
        plugins.append({
            "name": entry["name"], "enabled": entry.get("enabled") is not False, "version": entry.get("version"),
            "path": display_path(entry["path"], home, project) if isinstance(entry.get("path"), str) else None,
            "skills": sorted(x for x in entry.get("skills", []) if isinstance(x, str)),
            "mcp_servers": sorted(x for x in entry.get("mcpServers", []) if isinstance(x, str)),
            "hooks": sorted(x for x in entry.get("hooks", []) if isinstance(x, str)),
        })
    return {"evidence": "cli", "ok": True, "reason": None, "plugins": plugins}


# ------------------------------------------------------------------ text rendering

RULE_TEXT = {
    "personal-over-project": N_("用户级优先于项目级（官方文档）"),
    "both-load": N_("两份都会出现在选择器里（官方文档）"),
    "precedence-undocumented": N_("官方文档没有说明优先级"),
    "local-over-project-over-user": N_("local 优先于 project 优先于 user（官方文档）"),
    "project-approval-unknown": N_("项目级条目是否已批准未读取，无法判定哪份生效"),
    "project-trust-unknown": N_("项目是否受信任未读取，无法判定哪份生效"),
    "unknown": N_("无法判定"),
}
RELATION_TEXT = {"same-file": N_("同一个文件"), "identical": N_("内容相同"), "different": N_("内容不同"),
                 "unknown": N_("无法比较")}


def render_text(report: dict) -> str:
    lines: list[str] = []
    if report["project"]:
        lines.append(_("项目：{0}").format(report["project"]))
    for client, numbers in report["summary"]["per_client"].items():
        lines.append(_("[{0}] Skill {1} 个，每次会话开始加载的 description 共 {2} 字节；MCP {3} 个").format(
            client, numbers["skill_count"], numbers["description_bytes_total"], numbers["mcp_count"]))
    for item in report["skills"]:
        note = f" [{', '.join(_(REASON_TEXT.get(code, code)) for code in item['issues'])}]" if item["issues"] else ""
        lines.append(f"  skill {item['client']} {item['name']}  {item['path']}  {item['description_bytes']}B{note}")
    for item in report["mcps"]:
        target = item["url"] or " ".join(filter(None, [item["command"], *item["args"]]))
        lines.append(f"  mcp   {item['client']} {item['name']}  {item['store']}  {item['transport']}  {target}")
    for group in report["duplicates"]["skills"] + report["duplicates"]["mcps"]:
        label = ", ".join(group.get("names") or [group["name"]])
        verdict = (group.get("effective") or {}).get("rule")
        lines.append(_("重复 [{0}] {1}（{2}）：{3}").format(
            group["client"], label, _(RELATION_TEXT[group["relation"]]), ", ".join(group["stores"]))
            + (f" — {_(RULE_TEXT.get(verdict, verdict))}" if verdict else ""))
    for hint in report["overlap_hints"]:
        lines.append(_("相似提示（仅机械比对，不是语义判断）[{0}] {1}：{2}").format(
            hint["client"], " / ".join(hint["names"]), hint["similarity"]))
    for item in report["unknown"]:
        lines.append(_("无法判定：{0}（{1}）").format(item["store"], _(REASON_TEXT.get(item["reason"], item["reason"]))))
    for client, section in (report.get("cli") or {}).items():
        if not section["ok"]:
            lines.append(_("[{0}] 客户端自己的插件列表读不到（{1}）：不据此下结论").format(client, section["reason"]))
            continue
        for plugin in section["plugins"]:
            lines.append(_("  plugin {0} {1}  {2}  Skill：{3}；MCP：{4}；hooks：{5}（来自客户端命令，cli）").format(
                client, plugin["name"], _("已启用") if plugin["enabled"] else _("已停用"),
                ", ".join(plugin["skills"]) or "-", ", ".join(plugin["mcp_servers"]) or "-", ", ".join(plugin["hooks"]) or "-"))
    if not (report["skills"] or report["mcps"] or report["unknown"] or any(
            section["plugins"] for section in (report.get("cli") or {}).values())):
        lines.append(_("没有发现任何 Skill 或 MCP 配置。"))
    return "\n".join(lines) + "\n"
