#!/usr/bin/env python3
"""Declarative client-driver registry (#134).

A *driver* describes how a client is recognised and how it takes plugins, as data: detection evidence (executable
names, configuration directories), the distribution driver type (``marketplace-cli`` | ``directory-dropin`` |
``none``), where Skills and MCP servers are stored, and capability flags. Code asks the registry (``default_ids``,
``supports``, ``detect``, ``store_table``) instead of listing client names.

Built-in drivers are data files, ``drivers/<id>.json`` next to this module (schema_version 1 or 2); this module only
loads, validates and queries them. A built-in file that is missing or invalid is a programming error and raises
``DriverDataError`` at import; there is no fallback.

User declarations in ``~/.config/agenthelm/clients.d/*.json`` are read and validated only. They never run a command,
never write a file and never enter the ``--client`` set in this version; an invalid one is a warning and is ignored.
Standard library only, so every entry point can import it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from i18n_agent_pack import _

DISTRIBUTIONS = ("marketplace-cli", "directory-dropin", "none")
SCOPES = ("user", "local", "project")
FORMATS = ("json", "toml", "dir")
CAPABILITIES = ("default_set", "read_only", "project_mcp_write")
LIFECYCLES = ("active", "deprecated")
STORE_STATUSES = ("observed", "documented", "undetermined")
SCHEMA_VERSIONS = (1, 2)
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
DECLARATION_KEYS = {"schema_version", "id", "display", "detect", "distribution", "stores", "capabilities"}
MAX_DECLARATION_BYTES = 64 * 1024
STORE_KEYS = {"scope", "path", "format", "status", "write_target", "root_key", "managed_by", "trust_required"}
MANAGED_BY = ("agent-pack-state",)  # entries of this store are recognised through agent-pack's own sync state records
EVIDENCE_LEVELS = ("file", "setting", "cli", "unknown")
PRECEDENCE_KINDS = ("skills", "mcp")
RULES = ("local-over-project-over-user", "personal-over-project", "both-load", "precedence-undocumented", "unknown",
         "project-approval-unknown", "project-trust-unknown")  # the last two refine ``unknown`` (design section 4.5)
WHEN_MATCHES = ("exact", "includes")
PRECEDENCE_KEYS = {"kind", "rule", "evidence", "source_url", "when", "winner"}
CLI_STREAMS = ("stdout", "stderr")
CLI_PARSES = ("json", "text")
CLI_ARG = re.compile(r"[A-Za-z0-9._:=@/+-]{1,128}")
DRIVERS_DIR = Path(__file__).resolve().parent / "drivers"


class DriverDataError(RuntimeError):
    """A built-in driver data file is missing, unreadable or invalid. A programming error: never swallowed."""


def _is_text(value: Any, limit: int = 64) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value)


# ---------------------------------------------------------------- built-in drivers (data files, strict)

def _check(condition: Any, source: str, message: str) -> None:
    if not condition:
        raise DriverDataError(f"driver data {source}: {message}")


def _check_stores(entries: Any, source: str, kind: str) -> None:
    _check(isinstance(entries, list), source, f"stores.{kind} must be a list")
    for entry in entries:
        _check(isinstance(entry, dict) and {"scope", "path", "format"} <= set(entry) <= STORE_KEYS, source,
               f"stores.{kind}[] needs scope, path, format (optional: status, write_target)")
        _check(entry["scope"] in SCOPES, source, f"stores.{kind}[].scope must be one of {', '.join(SCOPES)}")
        _check(entry["format"] in FORMATS, source, f"stores.{kind}[].format must be one of {', '.join(FORMATS)}")
        _check(_is_text(entry["path"], 200), source, f"stores.{kind}[].path must be a one-line string")
        _check(entry.get("status", "observed") in STORE_STATUSES, source,
               f"stores.{kind}[].status must be one of {', '.join(STORE_STATUSES)}")
        _check(isinstance(entry.get("write_target", False), bool), source, f"stores.{kind}[].write_target must be a boolean")
        _check(not entry.get("write_target") or kind == "skills", source, "write_target applies to skills stores only")
        _check(entry.get("managed_by", MANAGED_BY[0]) in MANAGED_BY, source,
               f"stores.{kind}[].managed_by must be one of {', '.join(MANAGED_BY)}")
        _check(isinstance(entry.get("trust_required", False), bool) and (kind == "mcp" or "trust_required" not in entry),
               source, "trust_required must be a boolean and applies to mcp stores only")
        reads_keys = kind == "mcp" and entry["format"] in ("json", "toml")
        _check(reads_keys == ("root_key" in entry) and (not reads_keys or _is_text(entry["root_key"])), source,
               "root_key (the key holding the servers) is required for json / toml mcp stores and only for them")
        _check("#" not in entry["path"] or (kind == "mcp" and entry["format"] == "json"), source,
               "a #section in a path applies to json mcp stores only")


def probe_problem(probe: Any) -> str | None:
    """Why ``probe`` is not a valid ``detect.cli`` declaration (``None`` when valid): a fixed read-only argument list
    (the executable comes from ``detect.executables``), the stream the output is on and how to parse it."""
    if not isinstance(probe, dict) or set(probe) != {"argv", "stream", "parse"}:
        return "detect.cli must be an object with exactly argv, stream, parse"
    argv = probe["argv"]
    if not (isinstance(argv, list) and 1 <= len(argv) <= 16
            and all(isinstance(item, str) and CLI_ARG.fullmatch(item) for item in argv)):
        return "detect.cli.argv must be 1-16 plain arguments (letters, digits and . _ : = @ / + -)"
    if probe["stream"] not in CLI_STREAMS:
        return f"detect.cli.stream must be one of {', '.join(CLI_STREAMS)}"
    if probe["parse"] not in CLI_PARSES:
        return f"detect.cli.parse must be one of {', '.join(CLI_PARSES)}"
    return None


def _check_precedence(entries: Any, source: str) -> None:
    _check(isinstance(entries, list), source, "precedence must be a list")
    closed: set[str] = set()  # kinds that already have an unconditional entry: nothing after it can be reached
    for entry in entries:
        _check(isinstance(entry, dict) and {"kind", "rule", "evidence", "source_url"} <= set(entry) <= PRECEDENCE_KEYS,
               source, "precedence[] needs kind, rule, evidence, source_url (optional: when, winner)")
        _check(entry["kind"] in PRECEDENCE_KINDS, source, f"precedence[].kind must be one of {', '.join(PRECEDENCE_KINDS)}")
        _check(entry["kind"] not in closed, source, f"precedence[] has an entry after an unconditional {entry['kind']} entry")
        _check(entry["rule"] in RULES, source, f"precedence[].rule must be one of {', '.join(RULES)}")
        _check(entry["evidence"] in EVIDENCE_LEVELS, source, f"precedence[].evidence must be one of {', '.join(EVIDENCE_LEVELS)}")
        url = entry["source_url"]
        _check(url is None or (_is_text(url, 300) and url.startswith("https://")), source,
               "precedence[].source_url must be an https URL or null")
        when = entry.get("when")
        if when is None:
            _check("winner" not in entry, source, "precedence[].winner needs a when condition")
            closed.add(entry["kind"])
            continue
        _check(isinstance(when, dict) and set(when) == {"scopes", "match"} and when["match"] in WHEN_MATCHES
               and isinstance(when["scopes"], list) and when["scopes"] and all(x in SCOPES for x in when["scopes"]),
               source, "precedence[].when must be {scopes: [...], match: exact | includes}")
        _check(entry.get("winner", when["scopes"][0]) in when["scopes"], source,
               "precedence[].winner must be one of the when.scopes")


def validate_builtin(data: Any, source: str) -> dict:
    """Return ``data`` when it is a valid built-in driver; raise ``DriverDataError`` naming ``source`` otherwise."""
    _check(isinstance(data, dict), source, "the top level must be an object")
    _check(set(data) <= DECLARATION_KEYS | {"lifecycle", "precedence"} and DECLARATION_KEYS <= set(data), source,
           f"keys must be {', '.join(sorted(DECLARATION_KEYS))} (+ lifecycle, precedence); got {', '.join(sorted(data))}")
    _check(data["schema_version"] in SCHEMA_VERSIONS and not isinstance(data["schema_version"], bool), source,
           f"schema_version must be one of {', '.join(map(str, SCHEMA_VERSIONS))}")
    _check(isinstance(data["id"], str) and ID_PATTERN.match(data["id"]) is not None and data["id"] != "all", source,
           "id must match ^[a-z][a-z0-9-]{0,31}$")
    _check(_is_text(data["display"]), source, "display must be a one-line string")
    _check(data.get("lifecycle", "active") in LIFECYCLES, source, f"lifecycle must be one of {', '.join(LIFECYCLES)}")
    detect_part = data["detect"]
    _check(isinstance(detect_part, dict) and set(detect_part) <= {"executables", "dirs", "cli"}, source,
           "detect must be an object with executables / dirs (/ cli)")
    executables, dirs = detect_part.get("executables", []), detect_part.get("dirs", [])
    _check(isinstance(executables, list) and all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", x)
                                                 for x in executables), source, "detect.executables must be file names")
    _check(isinstance(dirs, list), source, "detect.dirs must be a list")
    for item in dirs:
        _check(isinstance(item, dict) and {"path"} <= set(item) <= {"path", "env"} and _is_text(item["path"], 200)
               and (item.get("env") is None or _is_text(item["env"])), source, "detect.dirs[] needs path (optional env)")
    _check(executables or dirs, source, "detect needs at least one fact")
    if "cli" in detect_part:
        _check(probe_problem(detect_part["cli"]) is None, source, probe_problem(detect_part["cli"]) or "")
        _check(bool(executables), source, "detect.cli needs detect.executables (the program the probe runs)")
    _check(data["distribution"] in DISTRIBUTIONS, source, f"distribution must be one of {', '.join(DISTRIBUTIONS)}")
    stores = data["stores"]
    _check(isinstance(stores, dict) and set(stores) == {"skills", "mcp"}, source, "stores must have exactly skills and mcp")
    for kind in ("skills", "mcp"):
        _check_stores(stores[kind], source, kind)
    for scope in SCOPES:  # at most one write target per scope: relocate has to pick one
        _check(sum(1 for e in stores["skills"] if e["scope"] == scope and e.get("write_target")) <= 1, source,
               f"more than one skills write_target for scope {scope}")
    capabilities = data["capabilities"]
    _check(isinstance(capabilities, dict) and set(capabilities) == set(CAPABILITIES)
           and all(isinstance(v, bool) for v in capabilities.values()), source,
           f"capabilities must hold exactly the booleans {', '.join(CAPABILITIES)}")
    _check_precedence(data.get("precedence", []), source)
    return data


def load_builtin(directory: Path | None = None) -> tuple[dict, ...]:
    """Load ``<directory>/*.json`` (default: the ``drivers`` directory shipped next to this file).

    Order: the default set first, then the others, each group by id. A missing directory, no files, a file that is
    not valid JSON, an invalid driver or a file whose name differs from its id raises ``DriverDataError``;
    there is no fallback."""
    folder = Path(directory) if directory is not None else DRIVERS_DIR
    _check(folder.is_dir(), str(folder), "the drivers directory is missing")
    drivers = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise DriverDataError(f"driver data {path.name}: cannot read: {error}") from error
        validate_builtin(data, path.name)
        _check(data["id"] == path.stem, path.name, f"the file name must be {data['id']}.json")
        drivers.append(data)
    _check(bool(drivers), str(folder), "no driver files (*.json)")
    _check(len({item["id"] for item in drivers}) == len(drivers), str(folder), "duplicate driver ids")
    return tuple(sorted(drivers, key=lambda item: (not item["capabilities"]["default_set"], item["id"])))


BUILTIN: tuple[dict, ...] = load_builtin()
BUILTIN_IDS = tuple(item["id"] for item in BUILTIN)


def default_ids() -> tuple[str, ...]:
    return tuple(item["id"] for item in BUILTIN if item["capabilities"]["default_set"])


def all_ids() -> tuple[str, ...]:
    return BUILTIN_IDS


def get(client: str) -> dict | None:
    return next((item for item in BUILTIN if item["id"] == client), None)


def display_names() -> dict[str, str]:
    """Display names of the default set (what the old ``CLIENT_NAMES`` held)."""
    return {item["id"]: item["display"] for item in BUILTIN if item["capabilities"]["default_set"]}


def supports(client: str, capability: str) -> bool:
    """Capability flag of a built-in driver; an unknown client or flag is ``False``."""
    driver = get(client)
    return bool(driver and driver["capabilities"].get(capability))


def store_table(kind: str) -> dict[str, tuple[tuple[str, str], ...]]:
    """``{client: ((scope, path), ...)}`` for ``kind`` (``skills`` | ``mcp``), in declaration order, for the clients that
    declare at least one such store. ``<project>`` stays a placeholder."""
    return {item["id"]: tuple((store["scope"], store["path"]) for store in item["stores"][kind])
            for item in BUILTIN if item["stores"][kind]}


def stores(client: str, kind: str) -> tuple[dict, ...]:
    """The declared ``kind`` stores of ``client`` (``skills`` | ``mcp``) with all their attributes, in declaration order."""
    driver = get(client)
    return tuple(driver["stores"][kind]) if driver else ()


def per_platform_ids() -> tuple[str, ...]:
    """Drivers whose MCP file location depends on the platform (declared as ``<per-platform>/<file>``): their store is
    resolved by a platform table, not from a path template."""
    return tuple(item["id"] for item in BUILTIN
                 if any(store["path"].startswith("<per-platform>") for store in item["stores"]["mcp"]))


def precedence(client: str, kind: str) -> tuple[dict, ...]:
    """The ordered precedence entries a driver declares for ``kind`` (``skills`` | ``mcp``); empty when it declares none."""
    driver = get(client)
    return tuple(item for item in driver.get("precedence", []) if item["kind"] == kind) if driver else ()


def skill_write_targets() -> dict[tuple[str, str], str]:
    """``{(client, scope): path}``: the skills store a ``relocate`` writes to (the entry marked ``write_target``)."""
    return {(item["id"], store["scope"]): store["path"]
            for item in BUILTIN for store in item["stores"]["skills"] if store.get("write_target")}


def primary_skill_stores() -> dict[tuple[str, str], str]:
    """``{(client, scope): path}``: the first declared skills store of each scope (refs in other stores get a suffix)."""
    table: dict[tuple[str, str], str] = {}
    for item in BUILTIN:
        for store in item["stores"]["skills"]:
            table.setdefault((item["id"], store["scope"]), store["path"])
    return table


# ---------------------------------------------------------------- detection (facts, not names)

def _expand(template: str, home: Path) -> Path:
    return home / template[2:] if template.startswith("~/") else Path(template)


def detect(driver: dict, home: Path, getenv: Callable[[str], str | None],
           find_cli: Callable[[str], str | None]) -> dict:
    """``{detected, evidence}``: the first matching fact (executable, then directory), else not detected."""
    for name in driver["detect"].get("executables", []):
        found = find_cli(name)
        if found:
            return {"detected": True, "evidence": {"kind": "executable", "name": name}}
    for item in driver["detect"].get("dirs", []):
        override = getenv(item["env"]) if item.get("env") else None
        path = Path(override) if override else _expand(item["path"], home)
        if path.is_dir():
            return {"detected": True, "evidence": {"kind": "directory", "name": item["path"]}}
    return {"detected": False, "evidence": None}


# ---------------------------------------------------------------- user declarations (validate only)

def declaration_dir(home: Path, getenv: Callable[[str], str | None]) -> Path:
    base = getenv("XDG_CONFIG_HOME")
    return (Path(base) if base else home / ".config") / "agenthelm" / "clients.d"


def validate_declaration(data: Any) -> str | None:
    """The reason a declaration is invalid (``None`` when valid). Only data fields are accepted."""
    if not isinstance(data, dict):
        return _("顶层必须是对象")
    extra = sorted(set(data) - DECLARATION_KEYS)
    if extra:
        return _("含不支持的字段：{0}").format(_("、").join(map(str, extra)))
    if data.get("schema_version") != 1:
        return _("schema_version 必须为 1")
    if not isinstance(data.get("id"), str) or not ID_PATTERN.match(data["id"]):
        return _("id 必须匹配 ^[a-z][a-z0-9-]{0,31}$")
    if data["id"] in BUILTIN_IDS or data["id"] == "all":
        return _("id 与内置客户端冲突：{0}").format(data["id"])
    if not _is_text(data.get("display")):
        return _("display 必须是 1–64 字符的单行文本")
    detect_part = data.get("detect")
    if not isinstance(detect_part, dict) or set(detect_part) - {"executables", "dirs"}:
        return _("detect 必须是只含 executables / dirs 的对象")
    executables = detect_part.get("executables", [])
    if not isinstance(executables, list) or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", x)
                                                    for x in executables):
        return _("detect.executables 必须是可执行文件名列表（不含路径与参数）")
    dirs = detect_part.get("dirs", [])
    if not isinstance(dirs, list) or not all(isinstance(x, str) and x.startswith("~/") and ".." not in x.split("/")
                                             and _is_text(x, 200) for x in dirs):
        return _("detect.dirs 必须是 ~/ 开头、不含 .. 的路径列表")
    if not executables and not dirs:
        return _("detect 至少要有一项检测依据")
    if data.get("distribution") not in DISTRIBUTIONS:
        return _("distribution 必须是：{0}").format(_("、").join(DISTRIBUTIONS))
    stores = data.get("stores", {})
    if not isinstance(stores, dict) or set(stores) - {"skills", "mcp"}:
        return _("stores 必须是只含 skills / mcp 的对象")
    for kind in stores:
        entries = stores[kind]
        if not isinstance(entries, list) or not all(
                isinstance(e, dict) and set(e) == {"scope", "path", "format"} and e["scope"] in SCOPES
                and e["format"] in FORMATS and _is_text(e["path"], 200) for e in entries):
            return _("stores.{0} 的每一项必须是 scope / path / format（取值见文档）").format(kind)
    capabilities = data.get("capabilities", {})
    if not isinstance(capabilities, dict) or not all(k in CAPABILITIES and isinstance(v, bool)
                                                     for k, v in capabilities.items()):
        return _("capabilities 只能含布尔值的：{0}").format(_("、").join(CAPABILITIES))
    return None


def load_declarations(home: Path, getenv: Callable[[str], str | None]) -> tuple[list[dict], list[dict]]:
    """(valid declarations, warnings). Reads ``clients.d/*.json`` only; nothing in them is ever executed."""
    folder = declaration_dir(home, getenv)
    declarations: list[dict] = []
    warnings: list[dict] = []
    if not folder.is_dir():
        return declarations, warnings
    seen: set[str] = set()
    for path in sorted(folder.glob("*.json")):
        label = path.name
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DECLARATION_BYTES:
                raise ValueError(_("不是普通文件或超过 {0} 字节").format(MAX_DECLARATION_BYTES))
            data = json.loads(path.read_text(encoding="utf-8"))
            reason = validate_declaration(data)
            if reason is None and data["id"] in seen:
                reason = _("id 重复：{0}").format(data["id"])
        except (OSError, ValueError, UnicodeDecodeError) as error:
            reason = _("无法读取：{0}").format(error)
        if reason:
            warnings.append({"file": label, "reason": reason})
            continue
        seen.add(data["id"])
        declarations.append(data)
    return declarations, warnings


# ---------------------------------------------------------------- the ``agenthelm clients`` report

def report(home: Path, getenv: Callable[[str], str | None], find_cli: Callable[[str], str | None]) -> dict:
    clients = []
    for item in BUILTIN:
        found = detect(item, home, getenv, find_cli)
        clients.append({"id": item["id"], "display": item["display"], "source": "builtin", **found,
                        "distribution": item["distribution"], "capabilities": dict(item["capabilities"]),
                        "install_supported": item["distribution"] != "none"})
    declared, warnings = load_declarations(home, getenv)
    for item in declared:
        probe = {"detect": {"executables": item["detect"].get("executables", []),
                            "dirs": [{"path": path} for path in item["detect"].get("dirs", [])]}}
        found = detect(probe, home, getenv, find_cli)  # looks up executables and directories; runs nothing
        clients.append({"id": item["id"], "display": item["display"], "source": "declared", **found,
                        "distribution": item["distribution"],
                        "capabilities": {key: bool(item.get("capabilities", {}).get(key, False)) for key in CAPABILITIES},
                        "install_supported": False})
    return {"schema_version": 1, "clients": clients, "warnings": warnings}


def render_text(data: dict) -> str:
    lines = [_("客户端驱动（内置与用户声明）：")]
    for item in data["clients"]:
        evidence = item["evidence"]
        seen = (_("已检测到（{0}：{1}）").format(_("可执行文件") if evidence["kind"] == "executable" else _("目录"),
                                            evidence["name"]) if item["detected"] else _("未检测到"))
        if item["source"] == "declared":
            seen = _("已声明，本版未支持安装")
        flags = [key for key, value in item["capabilities"].items() if value]
        lines.append("  " + _("{0}（{1}）：{2}；分发 {3}；能力 {4}").format(
            item["id"], item["display"], seen, item["distribution"], ", ".join(flags) or "-"))
    for warning in data["warnings"]:
        lines.append(_("警告：忽略声明 {0}：{1}").format(warning["file"], warning["reason"]))
    return "\n".join(lines) + "\n"
