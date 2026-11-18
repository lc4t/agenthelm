#!/usr/bin/env python3
"""Read-only observation of the Skills and MCP servers each client can see (``agenthelm inventory``).

The JSON shape is a contract (``schema_version``, documented in ``references/inventory-contract.md``): later write
commands and menus build on it, so keys are only ever appended. Nothing here writes, runs a client command line or
reads a settings key; every observation is ``evidence: file`` (read from a file) or ``unknown`` (could not be
decided, for example a corrupt file). MCP ``env`` values, ``headers`` values and tokens never appear in any output.

Reuses ``agent_pack_config`` for JSON/TOML readers, the MCP normaliser's secret patterns and version parsing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))
import agent_pack_config as core  # noqa: E402
from i18n_agent_pack import _, N_  # noqa: E402

SCHEMA_VERSION = 1
CLIENTS = ("claude", "codex", "cursor")
DESKTOP = "claude-desktop"  # a separate target: the standalone CLI does not read claude_desktop_config.json
ALL_CLIENTS = (*CLIENTS, DESKTOP)  # the default set is CLIENTS; claude-desktop must be named (or ``all``)
TYPES = ("skills", "mcp", "all")
SCOPES = ("project", "user", "all")
EVIDENCE = ("file", "setting", "cli", "unknown")  # setting / cli are reserved: this version never reads them
AGENTHELM_PLUGINS = frozenset({"agent-pack", "agents-init", "opinion-manager", "project-orchestrator"})
AGENTHELM_REPOSITORY = "lc4t/agenthelm"
OVERLAP_THRESHOLD = 0.8  # token Jaccard similarity; a hint only, never a semantic judgement
OVERLAP_MIN_TOKENS = 5
MAX_SKILL_MD = 4 * 1024 * 1024
MAX_CONFIG = 32 * 1024 * 1024
PROJECT = "<project>"

# Official documentation the precedence conclusions rest on (read 2026-11-11, see references/inventory-contract.md).
DOC_CLAUDE_MCP = "https://code.claude.com/docs/en/mcp"
DOC_CLAUDE_SKILLS = "https://code.claude.com/docs/en/skills"
DOC_CODEX_SKILLS = "https://learn.chatgpt.com/docs/build-skills"
DOC_CODEX_MCP = "https://learn.chatgpt.com/docs/extend/mcp?surface=cli"
DOC_DESKTOP_PATHS = "https://modelcontextprotocol.io/docs/develop/connect-local-servers"
DOC_DESKTOP_MCP = "https://code.claude.com/docs/en/desktop#mcp-servers-from-the-claude-desktop-chat-app"
DESKTOP_FILE = "claude_desktop_config.json"
DESKTOP_STORES = {  # official documentation (MCP site, read 2026-11-18); Windows (%APPDATA%\\Claude) is documentation only
    "darwin": f"~/Library/Application Support/Claude/{DESKTOP_FILE}",
    "linux": f"~/.config/Claude/{DESKTOP_FILE}",
}

# (scope, store) per client, in listing order. ``~`` is the home directory, ``<project>`` the project root.
SKILL_STORES = {
    "claude": (("user", "~/.claude/skills"), ("project", f"{PROJECT}/.claude/skills")),
    "codex": (("user", "~/.codex/skills"), ("user", "~/.agents/skills"), ("project", f"{PROJECT}/.agents/skills")),
    "cursor": (("user", "~/.cursor/skills"), ("user", "~/.agents/skills"),
               ("project", f"{PROJECT}/.cursor/skills"), ("project", f"{PROJECT}/.agents/skills")),
}
MCP_STORES = {
    "claude": (("user", "~/.claude.json"), ("local", "~/.claude.json#projects"), ("project", f"{PROJECT}/.mcp.json")),
    "codex": (("user", "~/.codex/config.toml"), ("project", f"{PROJECT}/.codex/config.toml")),
    "cursor": (("user", "~/.cursor/mcp.json"), ("project", f"{PROJECT}/.cursor/mcp.json")),
}
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


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ file reading (never raises)

def _read_bytes(path: Path, limit: int) -> tuple[bytes | None, str | None]:
    try:
        if path.stat().st_size > limit:
            return None, "too-large"
        return path.read_bytes(), None
    except OSError:
        return None, "unreadable"


def read_json_object(path: Path) -> tuple[dict | None, str | None]:
    """(object, None) | (None, None) when absent | (None, reason) when present but unusable."""
    if not path.exists():
        return None, None
    if not path.is_file():
        return None, "unreadable"
    raw, reason = _read_bytes(path, MAX_CONFIG)
    if raw is None:
        return None, reason
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "invalid-json"
    return (data, None) if isinstance(data, dict) else (None, "unexpected-structure")


def read_toml_object(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, None
    if not path.is_file():
        return None, "unreadable"
    raw, reason = _read_bytes(path, MAX_CONFIG)
    if raw is None:
        return None, reason
    try:
        return tomllib.loads(raw.decode("utf-8")), None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None, "invalid-toml"


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


def _skill_origin(entry: Path, client: str, scope: str, name: str, versions: dict[str, tuple[str, ...]],
                  state: dict[str, str]) -> tuple[dict, str | None, Any]:
    """(origin, version, stale): only an entry whose source and version can be read gets a stale verdict."""
    try:
        real = entry.resolve()
    except OSError:
        return {"kind": "unknown", "detail": None}, None, "unknown"
    manifest = _plugin_manifest(real.parent.parent) if real.parent.name == "skills" else None
    if (manifest and manifest.get("name") in AGENTHELM_PLUGINS and isinstance(manifest.get("version"), str)
            and AGENTHELM_REPOSITORY in str(manifest.get("repository", ""))):
        plugin, version = manifest["name"], manifest["version"]
        known = [*versions.get(plugin, ()), version]
        newest = max(known, key=core._version_tuple)
        stale: Any = core._version_tuple(version) < core._version_tuple(newest)
        return {"kind": "agenthelm-plugin", "detail": f"{plugin}@{version}"}, version, stale
    if client == "claude" and scope == "user" and name in state and entry.is_symlink():
        return {"kind": "state-record", "detail": state[name]}, None, "unknown"
    return {"kind": "unknown", "detail": None}, None, "unknown"


def observe_skill(entry: Path, client: str, scope: str, store: str, home: Path, project: Path | None,
                  versions: dict[str, tuple[str, ...]], state: dict[str, str]) -> dict | None:
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
        "origin": {"kind": "unknown", "detail": None},
        "symlink": {"is_symlink": link, "target": target},
        "version": None, "stale": "unknown", "evidence": "file", "issues": [],
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
    record["origin"], record["version"], record["stale"] = _skill_origin(entry, client, scope, name, versions, state)
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
    meta = parse_frontmatter(raw.decode("utf-8", errors="replace"))
    if meta is None:
        record["issues"].append("frontmatter-missing")
        return record
    record["frontmatter"] = {"name": meta.get("name"), "description": meta.get("description")}
    description = meta.get("description")
    if description is None:
        record["issues"].append("description-missing")
    else:
        record["description_bytes"] = len(description.encode("utf-8"))
    if meta.get("name") is not None and meta["name"] != name:
        record["issues"].append("name-mismatch")
    return record


def _scan_skill_store(client: str, scope: str, store: str, root: Path, home: Path, project: Path | None,
                      versions: dict[str, tuple[str, ...]], state: dict[str, str]) -> list[dict]:
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
        record = observe_skill(child, client, scope, store, home, project, versions, state)
        if record is not None:
            found.append(record)
    return found


# ------------------------------------------------------------------ MCP

def _names(mapping: Any) -> list[str]:
    return sorted(str(key) for key in mapping) if isinstance(mapping, dict) else []


def normalize_mcp(name: str, raw: Any, client: str, scope: str, store: str, home: Path,
                  state: dict[str, str]) -> dict:
    """A server entry reduced to what may be shown: no ``env`` / ``headers`` values, no tokens."""
    item = raw if isinstance(raw, dict) else {}
    command = item.get("command") if isinstance(item.get("command"), str) else None
    url = item.get("url") if isinstance(item.get("url"), str) else None
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
    record: dict[str, Any] = {
        "name": name, "client": client, "scope": scope, "store": store, "transport": transport,
        "command": shown_command, "args": args,
        "url": shown_url, "env_names": sorted(env_names), "header_names": sorted(header_names),
        "fingerprint": fingerprint,
        "origin": {"kind": "unknown", "detail": None},
        "evidence": "file", "notes": [],
    }
    if client == "claude" and scope == "user" and name in state:
        record["origin"] = {"kind": "state-record", "detail": state[name]}
    if client == "codex" and scope == "project":
        record["notes"].append("codex-project-requires-trust")
    if not command and not url:
        record["evidence"] = "unknown"
    return record


def _servers(data: dict | None, key: str = "mcpServers") -> dict | None:
    value = (data or {}).get(key)
    return value if isinstance(value, dict) else None


def _claude_local_servers(data: dict, project: Path) -> tuple[dict | None, str | None]:
    """Entries kept under the project's own path in ``~/.claude.json`` (``projects[<absolute path>].mcpServers``).

    The documentation says only "under that project's path", so anything else than that shape is ``unknown``.
    """
    projects = data.get("projects")
    if projects is None:
        return {}, None
    if not isinstance(projects, dict):
        return None, "claude-local-structure-unrecognized"
    for key in dict.fromkeys((str(project), str(project.resolve()))):
        if key in projects:
            section = projects[key]
            if not isinstance(section, dict):
                return None, "claude-local-structure-unrecognized"
            servers = section.get("mcpServers", {})
            if servers is None:
                return {}, None
            return (servers, None) if isinstance(servers, dict) else (None, "claude-local-structure-unrecognized")
    return {}, None


def _observe_mcp_store(client: str, scope: str, store: str, home: Path, project: Path | None,
                       state: dict[str, str], unknown: list[dict]) -> list[dict]:
    path = resolve_store(store, home, project)
    if path is None:
        return []
    if client == "codex":
        data, reason = read_toml_object(path)
        servers = _servers(data, "mcp_servers")
    else:
        data, reason = read_json_object(path)
        if scope == "local":
            if data is None:
                servers = None
            else:
                servers, local_reason = _claude_local_servers(data, project) if project else ({}, None)
                reason = local_reason or reason
        else:
            servers = _servers(data)
    if reason:
        unknown.append({"store": store, "client": client, "reason": reason})
        return []
    if servers is None:
        if data is not None and scope != "local" and (data.get("mcpServers" if client != "codex" else "mcp_servers") is not None):
            unknown.append({"store": store, "client": client, "reason": "unexpected-structure"})
        return []
    return [normalize_mcp(str(name), raw, client, scope, store, home, state) for name, raw in sorted(servers.items())]


# ------------------------------------------------------------------ Claude Desktop (claude_desktop_config.json)

def desktop_store(platform: str | None = None) -> str | None:
    """The documented ``claude_desktop_config.json`` location for this platform (``None`` where it is not resolved)."""
    name = sys.platform if platform is None else platform
    return DESKTOP_STORES.get("darwin" if name == "darwin" else "linux" if name.startswith("linux") else "")


def observe_desktop(home: Path, platform: str | None = None) -> tuple[list[dict], list[dict]]:
    """(entries, unknown) of ``claude_desktop_config.json``; a missing or corrupt file is ``unknown``, never an error."""
    store = desktop_store(platform)
    if store is None:
        return [], [{"store": f"%APPDATA%\\Claude\\{DESKTOP_FILE}", "client": DESKTOP, "reason": "platform-unsupported"}]
    path = resolve_store(store, home, None)
    data, reason = read_json_object(path) if path is not None else (None, "unreadable")
    if reason is None and data is None:
        reason = "not-found"
    if reason:
        return [], [{"store": store, "client": DESKTOP, "reason": reason}]
    servers = _servers(data)
    if servers is None:
        return [], ([{"store": store, "client": DESKTOP, "reason": "unexpected-structure"}] if "mcpServers" in data else [])
    entries = [normalize_mcp(str(name), raw, DESKTOP, "user", store, home, {}) for name, raw in sorted(servers.items())]
    for entry in entries:
        if any("${" in str(value) for value in (entry["command"], entry["url"], *entry["args"])):
            entry["notes"].append("variable-expansion-unconfirmed")
    return entries, []


def desktop_report(home: Path, platform: str | None = None) -> dict:
    """What ``doctor --client claude-desktop`` shows: read-only, nothing here is a drift issue."""
    entries, unknown = observe_desktop(home.expanduser().resolve(), platform)
    store = desktop_store(platform)
    report = {"client": DESKTOP, "store": store, "evidence": "unknown" if unknown else "file",
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
        description = (item.get("frontmatter") or {}).get("description")
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


def _skill_effective(client: str, group: list[dict]) -> dict:
    scopes = {item["scope"] for item in group}
    if client == "claude" and scopes == {"user", "project"}:
        # Claude Code: enterprise > personal > project (enterprise level is not read here).
        return {"store": next(item["store"] for item in group if item["scope"] == "user"), "rule": "personal-over-project",
                "evidence": "file", "source_url": DOC_CLAUDE_SKILLS}
    if client == "codex":
        return {"store": None, "rule": "both-load", "evidence": "file", "source_url": DOC_CODEX_SKILLS}
    return {"store": None, "rule": "precedence-undocumented" if client == "cursor" else "unknown",
            "evidence": "unknown", "source_url": None}


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
    stores = {item["scope"]: item["store"] for item in group}
    if client == "claude":
        # Claude Code: local > project > user (entry used whole, no field merge). A project-file winner depends on
        # the per-project approval (a setting this version does not read), so only a local winner is concluded.
        if "local" in stores:
            return {"store": stores["local"], "rule": "local-over-project-over-user", "evidence": "file",
                    "source_url": DOC_CLAUDE_MCP}
        return {"store": None, "rule": "project-approval-unknown", "evidence": "unknown", "source_url": DOC_CLAUDE_MCP}
    if client == "codex":
        return {"store": None, "rule": "project-trust-unknown", "evidence": "unknown", "source_url": DOC_CODEX_MCP}
    return {"store": None, "rule": "precedence-undocumented", "evidence": "unknown", "source_url": None}


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


# ------------------------------------------------------------------ entry point

def observe(home: Path, project: Path | None = None, clients: list[str] | None = None, types: str = "all",
            scope: str = "all", platform: str | None = None) -> dict:
    """Everything the chosen clients can see, as the ``schema_version`` 1 document. Read-only and never raises on
    bad input files (they become ``unknown`` entries)."""
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
            for item_scope, store in SKILL_STORES[client]:
                root = resolve_store(store, home, project)
                if root is None or not wanted(item_scope):
                    continue
                for record in _scan_skill_store(client, item_scope, store, root, home, project, versions, skill_state):
                    skills.append(record)
                    real = _real(root / record["name"])
                    if real:
                        real_paths[record["path"]] = real
        if want_mcp:
            for item_scope, store in MCP_STORES[client]:
                if wanted(item_scope):
                    mcps.extend(_observe_mcp_store(client, item_scope, store, home, project, mcp_state, unknown))
    order = {client: index for index, client in enumerate(ALL_CLIENTS)}
    skills.sort(key=lambda item: (order[item["client"]], SCOPE_ORDER[item["scope"]], item["store"], item["name"]))
    mcps.sort(key=lambda item: (order[item["client"]], SCOPE_ORDER[item["scope"]], item["store"], item["name"]))
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
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "agenthelm inventory",
        "project": display_path(project, home, None) if project else None,
        "clients": chosen,
        "filters": {"type": types, "scope": scope},
        "skills": skills,
        "mcps": mcps,
        "summary": {"per_client": summary},
        "duplicates": {"skills": skill_duplicates(skills, real_paths), "mcps": mcp_duplicates(mcps)},
        "overlap_hints": overlap_hints(skills),
        "unknown": unknown,
    }


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
    if not (report["skills"] or report["mcps"] or report["unknown"]):
        lines.append(_("没有发现任何 Skill 或 MCP 配置。"))
    return "\n".join(lines) + "\n"
