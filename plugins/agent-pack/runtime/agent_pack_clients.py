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
import os
import re
from pathlib import Path
from typing import Any, Callable

from i18n_agent_pack import _

DISTRIBUTIONS = ("marketplace-cli", "directory-dropin", "none")
SCOPES = ("user", "local", "project")
FORMATS = ("json", "toml", "dir")
CAPABILITIES = ("default_set", "read_only", "project_mcp_write")
# Flags added in schema v2 (design section 4.8); built-in drivers only, optional (absent means false). ``clients`` keeps
# reporting the three above, so its output does not change when a flag is added here.
EXTENDED_CAPABILITIES = ("legacy_cleanup", "marketplace_update", "reinstall_rewrites_all", "market_pin",
                         "user_mcp_write", "project_skill_write", "user_skill_write", "project_trust_required")
LIFECYCLES = ("active", "deprecated")
STORE_STATUSES = ("observed", "documented", "undetermined")
SCHEMA_VERSIONS = (1, 2)
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
INSTALLED_STATES = ("records-json", "cache-dir-versioned", "dropin-dir", "extensions-dir",
                    "cli-list-json")  # the readers agenthelm.py implements
INTERACTIVE = ("none", "trust-prompt")
SOURCES = ("marketplace", "bundle-dir")
STEP_NAMES = ("add_source", "install", "update_source", "update", "uninstall", "disable", "verify")
PLACEHOLDERS = ("cli", "plugin", "marketplace", "source_dir", "version", "slug")  # slug: design section 4.3 deviation
MESSAGE_SLOTS = ("recovery", "applied_recovery", "unchanged", "unchanged_next", "describe_update_source",
                 "describe_update", "describe_dropin", "unavailable_next")
DISTRIBUTION_KEYS = {"type", "steps", "interactive", "source", "installed_state", "config_dir", "scope_flag",
                     "messages", "owned_dirs", "cli_locations"}
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
STEP_ARG = re.compile(r"(?:[A-Za-z0-9._:=@/+-]|\{(?:" + "|".join(PLACEHOLDERS) + r")\}){1,128}")
DECLARATION_KEYS = {"schema_version", "id", "display", "detect", "distribution", "stores", "capabilities"}
MAX_DECLARATION_BYTES = 64 * 1024
OPTIONAL_BUILTIN_KEYS = {"lifecycle", "precedence", "runtime_cache", "hooks", "unsupported", "sync", "mcp", "content"}
HOOK_EVENTS = ("session-start", "prompt-submit", "stop")  # canonical event names (design section 4.7)
# Output adapters by output format, never by client id; agent_pack_nudge.ADAPTERS implements exactly these.
HOOK_OUTPUTS = ("hookSpecificOutput", "additional_context", "followup_message", "decision_block", "injectSteps")
HOOK_CONFIG_FORMATS = ("group-event-handlers", "flat-event-commands")
HOOKS_KEYS = {"events", "output", "stop_prompt", "shows_system_message", "stop_status_gated", "config", "legacy_sniff"}
HOOK_CONFIG_KEYS = {"format", "plugin_file", "user", "project"}
SNIFF_KEYS = {"priority", "payload_keys", "env", "fallback"}
STOP_MODES = ("off", "on-completion", "always")
# Feature names of the support matrix (design section 6); ``hook-event:*`` are the canonical hook events.
UNSUPPORTED_FEATURES = (*(f"hook-event:{event}" for event in HOOK_EVENTS), "relocate", "dedupe", "sync-skills",
                        "sync-mcp", "sync-skills-project", "sync-mcp-project", "agents-init-agent-cli",
                        "nudge-install-hooks")
# ``content`` (design section 4.4): which manifest protocols a client reads and which companion files it requires.
MANIFESTS = ("agent-plugins-1.0", "claude-plugin", "codex-plugin", "cursor-plugin", "gemini-extension", "antigravity-plugin")
COMPANION_ROLES = ("manifest", "mcp", "hooks")
COMPANION_ENTRY_KEYS = {"path", "role", "required_when", "shipped", "optional"}
CONTENT_KEYS = {"manifest", "companions", "skills", "context"}
CONTENT_SKILLS = ("agent-skills",)
CONTENT_CONTEXTS = ("agents-md", "pointer-file", "unsupported")
CACHE_KEYS = {"base", "env", "glob"}
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
# ``sync`` and ``mcp`` (batch D2, design sections 4.5, 4.6, 4.8): how ``sync`` writes a project's package and MCP entries.
SYNC_WRITES = ("native-package", "plugin-dir", "plugin-install")  # links plus user-file entries plus state | a package
# directory | a package directory the client installs itself (``distribution.steps.install`` with ``{source_dir}``)
SYNC_KEYS = {"write", "package_dir", "companions", "marketplace", "cleanup"}
COMPANION_KEYS = {"manifest", "mcp", "remote_mcp", "placeholder_prefix", "type_names"}
MARKETPLACE_KEYS = {"path", "source_dir"}
CLEANUP_KEYS = {"cache_dir", "config_file"}
MCP_ADAPTERS = ("json", "toml")  # named by file format, never by client
MCP_WRITES = ("package", "user-file")
MCP_KEYS = {"adapter", "write", "native_remote", "options", "manual_remove"}
REMOTE_HEADERS = ("inline", "split-env", "none")
ENV_INTERPOLATIONS = ("none", "plugin-root-only", "declared-variables", "shell-env")
MCP_OPTION_KEYS = {"remote_url_key", "remote_headers", "env_interpolation", "type_field_required", "type_names",
                   "header_fields", "disabled_key"}
HEADER_FIELDS = ("plain", "env", "bearer")
MCP_OPTION_DEFAULTS = {"remote_url_key": "url", "remote_headers": "inline", "env_interpolation": "shell-env",
                       "type_field_required": True, "type_names": {}, "header_fields": {}, "disabled_key": None}
MANUAL_REMOVE_ARG = re.compile(r"(?:[A-Za-z0-9._:=@/+-]|\{(?:scope|name)\}){1,128}")
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


def _check_location(entry: Any, source: str, label: str) -> None:
    _check(isinstance(entry, dict) and {"path"} <= set(entry) <= {"path", "env"} and _is_text(entry["path"], 200)
           and (entry["path"].startswith("~/") or entry["path"].startswith("/")) and ".." not in entry["path"].split("/")
           and (entry.get("env") is None or _is_text(entry["env"])), source,
           f"{label} needs path (~/ or absolute, no ..), optional env")


def step_problem(argv: Any) -> str | None:
    """Why ``argv`` is not a valid step template (``None`` when valid): ``{cli}`` first, then 0-15 plain arguments, each
    made of plain characters and the placeholders in ``PLACEHOLDERS`` (nothing else in braces)."""
    if not (isinstance(argv, list) and 1 <= len(argv) <= 16 and all(isinstance(x, str) for x in argv)):
        return "a step is a list of 1-16 strings"
    if argv[0] != "{cli}":
        return "a step starts with {cli}"
    if not all(STEP_ARG.fullmatch(x) for x in argv):
        return f"a step argument holds plain characters and the placeholders {', '.join(PLACEHOLDERS)} only"
    return None


def _check_distribution(value: Any, source: str, executables: list) -> None:
    if isinstance(value, str):  # the v1 spelling: the type only
        _check(value in DISTRIBUTIONS, source, f"distribution must be one of {', '.join(DISTRIBUTIONS)}")
        return
    _check(isinstance(value, dict) and "type" in value and set(value) <= DISTRIBUTION_KEYS, source,
           f"distribution must be a type name or an object with type (optional: {', '.join(sorted(DISTRIBUTION_KEYS - {'type'}))})")
    _check(value["type"] in DISTRIBUTIONS, source, f"distribution.type must be one of {', '.join(DISTRIBUTIONS)}")
    kind = value["type"]
    steps = value.get("steps")
    _check(steps is None or kind == "marketplace-cli", source, "distribution.steps applies to marketplace-cli only")
    if steps is not None:
        _check(isinstance(steps, dict) and "install" in steps and set(steps) <= set(STEP_NAMES), source,
               f"distribution.steps needs install (a missing step is left out, never an empty list); others: "
               f"{', '.join(STEP_NAMES)}")
        _check(bool(executables), source, "distribution.steps needs detect.executables (the program {cli} stands for)")
        for name, argv in steps.items():
            _check(step_problem(argv) is None, source, f"distribution.steps.{name}: {step_problem(argv)}")
    elif kind == "marketplace-cli":
        _check(False, source, "a marketplace-cli distribution needs steps")
    _check(value.get("interactive", INTERACTIVE[0]) in INTERACTIVE, source,
           f"distribution.interactive must be one of {', '.join(INTERACTIVE)}")
    _check(value.get("source", SOURCES[0]) in SOURCES, source, f"distribution.source must be one of {', '.join(SOURCES)}")
    state = value.get("installed_state")
    _check(state is None or state in INSTALLED_STATES, source,
           f"distribution.installed_state must be one of {', '.join(INSTALLED_STATES)}")
    _check((state is None) == (kind == "none"), source, "installed_state is declared for every distribution but none")
    if "config_dir" in value:
        _check_location(value["config_dir"], source, "distribution.config_dir")
    _check(state is None or "config_dir" in value, source, "installed_state needs distribution.config_dir")
    _check(value.get("scope_flag") is None or (isinstance(value["scope_flag"], str)
                                                and CLI_ARG.fullmatch(value["scope_flag"])), source,
           "distribution.scope_flag must be a plain option name")
    messages = value.get("messages", {})
    _check(isinstance(messages, dict) and set(messages) <= set(MESSAGE_SLOTS)
           and all(_is_text(key, 80) for key in messages.values()), source,
           f"distribution.messages maps {', '.join(MESSAGE_SLOTS)} to message keys")
    for name in ("owned_dirs", "cli_locations"):
        entries = value.get(name, [])
        _check(isinstance(entries, list), source, f"distribution.{name} must be a list")
        for entry in entries:
            _check_location(entry if name == "owned_dirs" else {"path": entry}, source, f"distribution.{name}[]")


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


def _check_content(value: Any, source: str) -> None:
    _check(isinstance(value, dict) and {"manifest"} <= set(value) <= CONTENT_KEYS, source,
           f"content needs manifest (optional: {', '.join(sorted(CONTENT_KEYS - {'manifest'}))})")
    manifests = value["manifest"]
    _check(isinstance(manifests, list) and manifests and all(item in MANIFESTS for item in manifests), source,
           f"content.manifest lists protocols from {', '.join(MANIFESTS)}")
    companions = value.get("companions", [])
    _check(isinstance(companions, list), source, "content.companions must be a list")
    for entry in companions:
        _check(isinstance(entry, dict) and {"path", "role"} <= set(entry) <= COMPANION_ENTRY_KEYS
               and _is_text(entry["path"], 100) and not entry["path"].startswith("/") and ".." not in entry["path"].split("/")
               and entry["role"] in COMPANION_ROLES and (entry.get("required_when") is None or _is_text(entry["required_when"]))
               and isinstance(entry.get("shipped", True), bool) and isinstance(entry.get("optional", False), bool), source,
               f"content.companions[] needs a relative path and a role ({', '.join(COMPANION_ROLES)}); "
               "optional: required_when, shipped (false: this release does not ship the file), optional (true: a plugin may "
               "leave the file out, as most do; only the plugins that need it carry it)")
    _check(value.get("skills", CONTENT_SKILLS[0]) in CONTENT_SKILLS, source, "content.skills must be agent-skills")
    _check(value.get("context", CONTENT_CONTEXTS[0]) in CONTENT_CONTEXTS, source,
           f"content.context must be one of {', '.join(CONTENT_CONTEXTS)}")


def _check_hooks(data: dict, source: str) -> None:
    unsupported = data.get("unsupported", [])
    _check(isinstance(unsupported, list), source, "unsupported must be a list")
    gone: set[str] = set()
    for entry in unsupported:
        _check(isinstance(entry, dict) and set(entry) == {"feature", "reason"} and entry["feature"] in UNSUPPORTED_FEATURES
               and _is_text(entry["reason"], 300), source,
               f"unsupported[] must be {{feature, reason}} with feature one of {', '.join(UNSUPPORTED_FEATURES)}")
        _check(entry["feature"] not in gone, source, f"unsupported[] lists {entry['feature']} twice")
        gone.add(entry["feature"])
    gone_events = {name for name in gone if name.startswith("hook-event:")}
    if "hooks" not in data:
        _check(not gone_events, source, "unsupported hook events need a hooks declaration")
        return
    hooks_part = data["hooks"]
    _check(isinstance(hooks_part, dict) and {"events", "output", "stop_prompt"} <= set(hooks_part) <= HOOKS_KEYS, source,
           f"hooks needs events, output, stop_prompt (optional: {', '.join(sorted(HOOKS_KEYS - {'events', 'output', 'stop_prompt'}))})")
    events, output = hooks_part["events"], hooks_part["output"]
    _check(isinstance(events, dict) and set(events) <= set(HOOK_EVENTS) and all(_is_text(v) for v in events.values()), source,
           f"hooks.events maps canonical events ({', '.join(HOOK_EVENTS)}) to native event names")
    for event in HOOK_EVENTS:  # each canonical event is either mapped or declared unsupported, never both, never neither
        _check((event in events) != (f"hook-event:{event}" in gone_events), source,
               f"hook event {event} must be in hooks.events or in unsupported (exactly one)")
    _check(isinstance(output, dict) and set(output) == set(events) and all(v in HOOK_OUTPUTS for v in output.values()), source,
           f"hooks.output must name an adapter ({', '.join(HOOK_OUTPUTS)}) for exactly the mapped events")
    _check(hooks_part["stop_prompt"] in STOP_MODES, source, f"hooks.stop_prompt must be one of {', '.join(STOP_MODES)}")
    for flag in ("shows_system_message", "stop_status_gated"):
        _check(isinstance(hooks_part.get(flag, False), bool), source, f"hooks.{flag} must be a boolean")
    config = hooks_part.get("config")
    if config is not None:
        _check(isinstance(config, dict) and "format" in config and set(config) <= HOOK_CONFIG_KEYS
               and config["format"] in HOOK_CONFIG_FORMATS and all(_is_text(v, 200) for v in config.values()), source,
               f"hooks.config needs format ({', '.join(HOOK_CONFIG_FORMATS)}), optional plugin_file / user / project")
        if config["format"] == "flat-event-commands":
            _check("user" in config and "project" in config, source, "flat-event-commands needs user and project locations")
    sniff = hooks_part.get("legacy_sniff")
    if sniff is not None:
        _check(isinstance(sniff, dict) and set(sniff) <= SNIFF_KEYS and (sniff.get("fallback") is True or "priority" in sniff)
               and isinstance(sniff.get("priority", 0), int) and not isinstance(sniff.get("priority", 0), bool)
               and all(isinstance(sniff.get(k, []), list) and all(_is_text(x) for x in sniff.get(k, [])) for k in ("payload_keys", "env"))
               and isinstance(sniff.get("fallback", False), bool), source,
               "hooks.legacy_sniff is {priority, payload_keys, env} or {fallback: true}")


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


def _check_sync(value: Any, source: str) -> None:
    _check(isinstance(value, dict) and {"write", "package_dir"} <= set(value) <= SYNC_KEYS, source,
           f"sync needs write and package_dir (optional: {', '.join(sorted(SYNC_KEYS - {'write', 'package_dir'}))})")
    _check(value["write"] in SYNC_WRITES, source, f"sync.write must be one of {', '.join(SYNC_WRITES)}")
    _check_location({"path": value["package_dir"]}, source, "sync.package_dir")
    plugin_dir = value["write"] == "plugin-dir"
    packaged = value["write"] in ("plugin-dir", "plugin-install")
    for key in ("marketplace", "cleanup"):
        _check(key not in value or plugin_dir, source, f"sync.{key} applies to plugin-dir writes only")
    _check("companions" not in value or packaged, source, "sync.companions applies to plugin-dir and plugin-install writes only")
    if "companions" in value:
        companions = value["companions"]
        needed = {"manifest"} if plugin_dir else {"mcp"}  # a package the client installs keeps its own plugin.json
        _check(isinstance(companions, dict) and needed <= set(companions) <= COMPANION_KEYS, source,
               f"sync.companions needs {', '.join(sorted(needed))} (optional: {', '.join(sorted(COMPANION_KEYS - needed))})")
        for key in ("manifest", "mcp", "remote_mcp"):
            if key in companions:
                _check(_is_text(companions[key], 100) and not companions[key].startswith("/")
                       and ".." not in companions[key].split("/"), source,
                       f"sync.companions.{key} must be a relative file path without ..")
        _check("remote_mcp" not in companions or "mcp" in companions, source, "sync.companions.remote_mcp needs mcp")
        _check(companions.get("placeholder_prefix", "") == "" or (isinstance(companions["placeholder_prefix"], str)
               and re.fullmatch(r"[A-Z_]{1,32}", companions["placeholder_prefix"])), source,
               "sync.companions.placeholder_prefix must be upper-case letters and underscores")
        names = companions.get("type_names", {})
        _check(isinstance(names, dict) and all(_is_text(k, 32) and _is_text(v, 32) for k, v in names.items()), source,
               "sync.companions.type_names must map type names to type names")
    if "marketplace" in value:
        market = value["marketplace"]
        _check(isinstance(market, dict) and set(market) == MARKETPLACE_KEYS, source,
               "sync.marketplace needs exactly path and source_dir")
        _check_location({"path": market["path"]}, source, "sync.marketplace.path")
        _check(_is_text(market["source_dir"], 100) and market["source_dir"].startswith("./")
               and ".." not in market["source_dir"].split("/"), source, "sync.marketplace.source_dir must be ./relative")
    if "cleanup" in value:
        cleanup = value["cleanup"]
        _check(isinstance(cleanup, dict) and set(cleanup) <= CLEANUP_KEYS and cleanup, source,
               f"sync.cleanup holds {', '.join(sorted(CLEANUP_KEYS))}")
        for key, path in cleanup.items():
            _check_location({"path": path}, source, f"sync.cleanup.{key}")


def mcp_option_problem(options: Any) -> str | None:
    """Why ``options`` is not a valid ``mcp.options`` (``None`` when valid)."""
    if not isinstance(options, dict) or not set(options) <= MCP_OPTION_KEYS:
        return f"mcp.options holds only {', '.join(sorted(MCP_OPTION_KEYS))}"
    if "remote_url_key" in options and not (isinstance(options["remote_url_key"], str)
                                            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", options["remote_url_key"])):
        return "mcp.options.remote_url_key must be a plain key name"
    if "disabled_key" in options and not (isinstance(options["disabled_key"], str)
                                          and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", options["disabled_key"])):
        return "mcp.options.disabled_key must be a plain key name"
    if options.get("remote_headers", REMOTE_HEADERS[0]) not in REMOTE_HEADERS:
        return f"mcp.options.remote_headers must be one of {', '.join(REMOTE_HEADERS)}"
    if options.get("env_interpolation", MCP_OPTION_DEFAULTS["env_interpolation"]) not in ENV_INTERPOLATIONS:
        return f"mcp.options.env_interpolation must be one of {', '.join(ENV_INTERPOLATIONS)}"
    if not isinstance(options.get("type_field_required", True), bool):
        return "mcp.options.type_field_required must be a boolean"
    names = options.get("type_names", {})
    if not (isinstance(names, dict) and all(_is_text(k, 32) and _is_text(v, 32) for k, v in names.items())):
        return "mcp.options.type_names must map type names to type names"
    fields = options.get("header_fields", {})
    if not (isinstance(fields, dict) and set(fields) <= set(HEADER_FIELDS)
            and all(isinstance(v, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", v) for v in fields.values())):
        return f"mcp.options.header_fields maps {', '.join(HEADER_FIELDS)} to plain key names"
    if (options.get("remote_headers") == "split-env") != bool(fields) or (fields and set(fields) != set(HEADER_FIELDS)):
        return "mcp.options.header_fields is required, with plain / env / bearer, exactly for remote_headers split-env"
    return None


def _check_mcp(value: Any, source: str, stores: dict) -> None:
    _check(isinstance(value, dict) and {"adapter", "write"} <= set(value) <= MCP_KEYS, source,
           f"mcp needs adapter and write (optional: {', '.join(sorted(MCP_KEYS - {'adapter', 'write'}))})")
    _check(value["adapter"] in MCP_ADAPTERS, source, f"mcp.adapter must be one of {', '.join(MCP_ADAPTERS)}")
    _check(value["write"] in MCP_WRITES, source, f"mcp.write must be one of {', '.join(MCP_WRITES)}")
    _check(isinstance(value.get("native_remote", False), bool), source, "mcp.native_remote must be a boolean")
    problem = mcp_option_problem(value.get("options", {}))
    _check(problem is None, source, problem or "")
    remove = value.get("manual_remove")
    _check(remove is None or (isinstance(remove, list) and 1 <= len(remove) <= 16
                              and all(isinstance(x, str) and MANUAL_REMOVE_ARG.fullmatch(x) for x in remove)), source,
           "mcp.manual_remove is a list of plain arguments and the placeholders {scope} and {name}")
    user_stores = [s for s in stores["mcp"] if s["scope"] == "user"]
    _check(bool(user_stores) and user_stores[0]["format"] == value["adapter"], source,
           "mcp.adapter must be the format of the first user-level mcp store")


def validate_builtin(data: Any, source: str) -> dict:
    """Return ``data`` when it is a valid built-in driver; raise ``DriverDataError`` naming ``source`` otherwise."""
    _check(isinstance(data, dict), source, "the top level must be an object")
    _check(set(data) <= DECLARATION_KEYS | OPTIONAL_BUILTIN_KEYS and DECLARATION_KEYS <= set(data), source,
           f"keys must be {', '.join(sorted(DECLARATION_KEYS))} (+ {', '.join(sorted(OPTIONAL_BUILTIN_KEYS))}); "
           f"got {', '.join(sorted(data))}")
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
    _check_distribution(data["distribution"], source, executables)
    cache = data.get("runtime_cache", [])
    _check(isinstance(cache, list), source, "runtime_cache must be a list")
    for entry in cache:
        _check(isinstance(entry, dict) and {"base", "glob"} <= set(entry) <= CACHE_KEYS, source,
               "runtime_cache[] needs base, glob (optional: env)")
        _check(_is_text(entry["base"], 200) and (entry["base"].startswith("~/") or entry["base"].startswith("/")), source,
               "runtime_cache[].base must be ~/ or absolute")
        _check(_is_text(entry["glob"], 200) and not entry["glob"].startswith("/") and ".." not in entry["glob"].split("/"),
               source, "runtime_cache[].glob must be a relative pattern without ..")
        _check(entry.get("env") is None or _is_text(entry["env"]), source, "runtime_cache[].env must be a variable name")
    stores = data["stores"]
    _check(isinstance(stores, dict) and set(stores) == {"skills", "mcp"}, source, "stores must have exactly skills and mcp")
    for kind in ("skills", "mcp"):
        _check_stores(stores[kind], source, kind)
    for scope in SCOPES:  # at most one write target per scope: relocate has to pick one
        _check(sum(1 for e in stores["skills"] if e["scope"] == scope and e.get("write_target")) <= 1, source,
               f"more than one skills write_target for scope {scope}")
    capabilities = data["capabilities"]
    _check(isinstance(capabilities, dict) and set(CAPABILITIES) <= set(capabilities) <= {*CAPABILITIES, *EXTENDED_CAPABILITIES}
           and all(isinstance(v, bool) for v in capabilities.values()), source,
           f"capabilities must hold the booleans {', '.join(CAPABILITIES)} (optional: {', '.join(EXTENDED_CAPABILITIES)})")
    _check_precedence(data.get("precedence", []), source)
    for flag in ("user_mcp_write", "user_skill_write"):
        _check(not capabilities.get(flag) or "sync" in data, source, f"capabilities.{flag} needs a sync declaration")
    for flag in ("project_mcp_write", "project_skill_write"):
        _check(not capabilities.get(flag) or data.get("sync", {}).get("write") == "native-package", source,
               f"capabilities.{flag} needs sync.write native-package (a plugin-dir write has no project-level store)")
    if "sync" in data:
        _check_sync(data["sync"], source)
        if data["sync"]["write"] == "plugin-install":
            dist = data["distribution"]
            _check(isinstance(dist, dict) and dist["type"] == "marketplace-cli" and dist.get("source") == "bundle-dir"
                   and any("{source_dir}" in part for part in dist["steps"]["install"]), source,
                   "sync.write plugin-install needs a marketplace-cli distribution with source bundle-dir whose install "
                   "step names {source_dir}")
    if "mcp" in data:
        _check_mcp(data["mcp"], source, stores)
    _check(("sync" in data) == ("mcp" in data) or "sync" not in data, source, "sync needs mcp (the MCP format it writes)")
    _check_hooks(data, source)
    if "content" in data:
        _check_content(data["content"], source)
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


def _load_all() -> tuple[dict, ...]:
    """The shipped drivers, plus the ones in ``AGENTHELM_DRIVERS_DIR`` when ``AGENTHELM_TEST_DRIVERS=1`` is set too. The extra
    directory exists for tests: a driver loaded from it goes through the same validation, may not reuse a shipped id and
    is data like any other (the synthetic-client guard adds one without touching any code). Without the switch the
    variable is ignored, so an environment cannot swap in a driver (whose ``steps`` are commands) by accident."""
    shipped = load_builtin()
    extra = os.environ.get("AGENTHELM_DRIVERS_DIR")
    if not extra or os.environ.get("AGENTHELM_TEST_DRIVERS") != "1":
        return shipped
    added = load_builtin(Path(extra))
    _check(not ({item["id"] for item in added} & {item["id"] for item in shipped}), extra,
           "a test driver may not reuse the id of a shipped driver")
    return tuple(sorted((*shipped, *added), key=lambda item: (not item["capabilities"]["default_set"], item["id"])))


BUILTIN: tuple[dict, ...] = _load_all()
BUILTIN_IDS = tuple(item["id"] for item in BUILTIN)


def distribution(driver: dict) -> dict:
    """The distribution declaration of a driver as an object (the v1 spelling, a bare type name, becomes ``{type}``)."""
    value = driver["distribution"]
    return {"type": value} if isinstance(value, str) else value


def distribution_type(driver: dict) -> str:
    return distribution(driver)["type"]


def lifecycle(driver: dict) -> str:
    """``active`` (the default) or ``deprecated``: the client's vendor has replaced it (design section 4.1)."""
    return driver.get("lifecycle", LIFECYCLES[0])


def deprecated_ids() -> tuple[str, ...]:
    return tuple(item["id"] for item in BUILTIN if lifecycle(item) == "deprecated")


def installable_ids() -> tuple[str, ...]:
    """The built-in drivers ``install`` and ``update`` can address by name: every distribution but ``none``. The default
    set comes first; the others (opt-in, possibly deprecated) are only used when named or with ``--client all``."""
    return tuple(item["id"] for item in BUILTIN if distribution_type(item) != "none")


def interactive(driver: dict) -> str:
    """``none`` or ``trust-prompt``: whether the install command asks the user for a confirmation (design section 4.3)."""
    return distribution(driver).get("interactive", INTERACTIVE[0])


def source_kind(driver: dict) -> str:
    """``marketplace`` (the client fetches the source) or ``bundle-dir`` (agenthelm hands it a plugin directory)."""
    return distribution(driver).get("source", SOURCES[0])


def companions(driver: dict, role: str | None = None) -> tuple[dict, ...]:
    """The companion files ``content.companions`` declares (optionally of one ``role``) that this release ships."""
    found = driver.get("content", {}).get("companions", [])
    return tuple(item for item in found if (role is None or item["role"] == role) and item.get("shipped", True))


def required_files(driver: dict) -> tuple[str, ...]:
    """Files every plugin directory handed to this client must hold: the declared companions that are not ``optional``,
    shipped or not."""
    return tuple(item["path"] for item in driver.get("content", {}).get("companions", []) if not item.get("optional", False))


def manifest_file(driver: dict) -> str | None:
    """The declared companion with role ``manifest`` (the file naming and versioning the plugin for this client)."""
    return next((item["path"] for item in driver.get("content", {}).get("companions", []) if item["role"] == "manifest"), None)


def ids_of_type(kind: str) -> tuple[str, ...]:
    """The built-in drivers whose distribution type is ``kind``, in registry order."""
    return tuple(item["id"] for item in BUILTIN if distribution_type(item) == kind)


def ids_with(capability: str) -> tuple[str, ...]:
    """The built-in drivers declaring the capability flag ``capability``, in registry order."""
    return tuple(item["id"] for item in BUILTIN if item["capabilities"].get(capability))


def hooks(client: str) -> dict:
    """The ``hooks`` declaration of a built-in driver; ``{}`` for an unknown client or one without hooks."""
    driver = get(client)
    return driver.get("hooks", {}) if driver else {}


def hook_event_name(client: str, event: str) -> str | None:
    """The client's native name for the canonical ``event``; ``None`` when the client has no such hook."""
    return hooks(client).get("events", {}).get(event)


def hook_output(client: str, event: str) -> str | None:
    """The output adapter name the client's ``event`` hook speaks; ``None`` when the event is not mapped."""
    return hooks(client).get("output", {}).get(event)


def hook_flag(client: str, key: str) -> bool:
    return bool(hooks(client).get(key))


def hook_config_format(client: str) -> str | None:
    return hooks(client).get("config", {}).get("format")


def ids_with_hook_config(fmt: str) -> tuple[str, ...]:
    """The built-in drivers whose hook file has format ``fmt``, in registry order."""
    return tuple(item["id"] for item in BUILTIN if hook_config_format(item["id"]) == fmt)


def stop_prompt_defaults() -> dict[str, str]:
    """The default stop-prompt mode of each default-set driver that declares hooks."""
    return {item["id"]: item["hooks"]["stop_prompt"] for item in BUILTIN if item["capabilities"]["default_set"] and "hooks" in item}


def sniff_order() -> list[tuple[str, dict]]:
    """``(id, legacy_sniff)`` of the drivers that can be recognised from an old hook command's payload: by ascending
    priority, the fallback driver last. Only installs from before hook commands carried ``--client`` rely on this."""
    found = [(item["id"], item["hooks"]["legacy_sniff"]) for item in BUILTIN if "legacy_sniff" in item.get("hooks", {})]
    return sorted(found, key=lambda pair: (pair[1].get("fallback", False), pair[1].get("priority", 0)))


def expand_step(driver: dict, name: str, **values: str) -> list[str] | None:
    """The argument list of step ``name`` with the placeholders in ``values`` replaced (a placeholder without a value stays
    as written). ``None`` when the driver declares no such step (a missing step is not an empty one)."""
    argv = distribution(driver).get("steps", {}).get(name)
    if argv is None:
        return None
    return [PLACEHOLDER.sub(lambda found: values.get(found.group(1), found.group(0)), item) for item in argv]


def message_key(driver: dict, slot: str) -> str | None:
    """The key of the message a driver names for ``slot`` (the text itself lives with the code that shows it)."""
    return distribution(driver).get("messages", {}).get(slot)


def config_dir(driver: dict, home: Path, getenv: Callable[[str], str | None]) -> Path | None:
    """The client's own configuration directory (``distribution.config_dir``): the environment override, else the default."""
    location = distribution(driver).get("config_dir")
    return _location(location, home, getenv) if location else None


def default_ids() -> tuple[str, ...]:
    return tuple(item["id"] for item in BUILTIN if item["capabilities"]["default_set"])


def all_ids() -> tuple[str, ...]:
    return BUILTIN_IDS


def sync_client_ids() -> tuple[str, ...]:
    """The installable clients that declare a ``sync`` (what ``sync`` and ``reconcile`` accept with ``--client``)."""
    return tuple(item["id"] for item in BUILTIN if item["id"] in installable_ids() and "sync" in item)


def hook_client_ids() -> tuple[str, ...]:
    """The selectable clients that declare hooks, in registry order (what ``nudge hook --client`` accepts)."""
    installable = installable_ids()
    return tuple(item["id"] for item in BUILTIN if item["id"] in installable and "hooks" in item)


def unsupported(client: str, feature: str) -> str | None:
    """The reason the driver of ``client`` gives for not supporting ``feature`` (a name of the support matrix); ``None``
    when it declares no such limit (or the client is unknown)."""
    driver = get(client)
    entry = next((item for item in (driver or {}).get("unsupported", []) if item["feature"] == feature), None)
    return entry["reason"] if entry else None


def get(client: str) -> dict | None:
    return next((item for item in BUILTIN if item["id"] == client), None)


def display_names() -> dict[str, str]:
    """Display names of the default set (what the old ``CLIENT_NAMES`` held)."""
    return {item["id"]: item["display"] for item in BUILTIN if item["capabilities"]["default_set"]}


def supports(client: str, capability: str) -> bool:
    """Capability flag of a built-in driver; an unknown client or flag is ``False``."""
    driver = get(client)
    return bool(driver and driver["capabilities"].get(capability))


def _determined(entries: list[dict]) -> tuple[dict, ...]:
    """The stores whose location is known: an ``undetermined`` store is shown by ``clients`` only, never read or written."""
    return tuple(store for store in entries if store.get("status") != "undetermined")


def store_table(kind: str) -> dict[str, tuple[tuple[str, str], ...]]:
    """``{client: ((scope, path), ...)}`` for ``kind`` (``skills`` | ``mcp``), in declaration order, for the clients that
    declare at least one such store (an ``undetermined`` store is left out). ``<project>`` stays a placeholder."""
    return {item["id"]: tuple((store["scope"], store["path"]) for store in _determined(item["stores"][kind]))
            for item in BUILTIN if _determined(item["stores"][kind])}


def stores(client: str, kind: str, *, with_undetermined: bool = False) -> tuple[dict, ...]:
    """The declared ``kind`` stores of ``client`` (``skills`` | ``mcp``) with all their attributes, in declaration order.
    A store with ``status: undetermined`` is left out unless ``with_undetermined``."""
    driver = get(client)
    if not driver:
        return ()
    return tuple(driver["stores"][kind]) if with_undetermined else _determined(driver["stores"][kind])


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
            for item in BUILTIN for store in _determined(item["stores"]["skills"]) if store.get("write_target")}


def primary_skill_stores() -> dict[tuple[str, str], str]:
    """``{(client, scope): path}``: the first declared skills store of each scope (refs in other stores get a suffix)."""
    table: dict[tuple[str, str], str] = {}
    for item in BUILTIN:
        for store in _determined(item["stores"]["skills"]):
            table.setdefault((item["id"], store["scope"]), store["path"])
    return table


# ---------------------------------------------------------------- sync write declarations (batch D2)

def sync_spec(client: str) -> dict | None:
    """The ``sync`` declaration (how ``sync`` writes the project package) of a built-in driver, or ``None``."""
    driver = get(client)
    return driver.get("sync") if driver else None


def ids_with_sync(kind: str) -> tuple[str, ...]:
    """The built-in drivers whose ``sync.write`` is ``kind`` (``native-package`` | ``plugin-dir``), in registry order."""
    return tuple(item["id"] for item in BUILTIN if item.get("sync", {}).get("write") == kind)


def package_dir(client: str, home: Path) -> Path | None:
    """Where ``sync`` puts the project package (``plugin-dir``) or its install record (``native-package``): the parent
    directory; the package name is appended by the caller. ``None`` for a client without a ``sync`` declaration."""
    spec = sync_spec(client)
    return _expand(spec["package_dir"], home) if spec else None


def location(path: str, home: Path) -> Path:
    """A declared ``~/`` or absolute path as a ``Path`` (no environment override)."""
    return _expand(path, home)


def mcp_spec(client: str) -> dict | None:
    """The ``mcp`` declaration (format adapter, write mode, options) of a built-in driver, or ``None``."""
    driver = get(client)
    return driver.get("mcp") if driver else None


def mcp_options(client: str) -> dict:
    """The adapter options of ``client`` with the documented defaults filled in (an undeclared client gets the defaults)."""
    spec = mcp_spec(client) or {}
    return {**MCP_OPTION_DEFAULTS, **spec.get("options", {})}


def user_mcp_store(client: str) -> dict | None:
    """The first user-level MCP store a driver declares (the file ``sync`` and ``relocate`` read and edit)."""
    return next((store for store in stores(client, "mcp") if store["scope"] == "user"), None)


def user_mcp_file(client: str, home: Path) -> Path | None:
    store = user_mcp_store(client)
    return _expand(store["path"].split("#", 1)[0], home) if store else None


def default_config_dir(client: str, home: Path) -> Path | None:
    """``distribution.config_dir`` at its default location (the environment override is not consulted)."""
    driver = get(client)
    location_ = distribution(driver).get("config_dir") if driver else None
    return _expand(location_["path"], home) if location_ else None


# ---------------------------------------------------------------- detection (facts, not names)

def _expand(template: str, home: Path) -> Path:
    return home / template[2:] if template.startswith("~/") else Path(template)


def _location(entry: dict, home: Path, getenv: Callable[[str], str | None]) -> Path:
    override = getenv(entry["env"]) if entry.get("env") else None
    return Path(override) if override else _expand(entry["path"], home)


def owned_dirs(driver: dict, home: Path, getenv: Callable[[str], str | None]) -> list[Path]:
    """Directories whose contents belong to the client (``distribution.owned_dirs``); an entry with ``env`` follows that
    variable when it is set, so an override and the default location are two entries."""
    found: list[Path] = []
    for entry in distribution(driver).get("owned_dirs", []):
        path = _location(entry, home, getenv)
        if path not in found:
            found.append(path)
    return found


def cli_locations(executable: str, home: Path) -> list[Path]:
    """Where the command line named ``executable`` lives when it is not on PATH: the declared locations of the driver that
    owns the name (``distribution.cli_locations``), else the usual per-user places."""
    for item in BUILTIN:
        if executable in item["detect"].get("executables", []) and distribution(item).get("cli_locations"):
            return [_expand(path, home) for path in distribution(item)["cli_locations"]]
    return [home / ".local/bin" / executable, Path("/opt/homebrew/bin") / executable, Path("/usr/local/bin") / executable]


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


# ---------------------------------------------------------------- runtime caches (where clients keep plugin copies)

def _cache_patterns(home: Path, getenv: Callable[[str], str | None], plugin: str,
                    *, with_defaults: bool = True, clients: Any = None) -> list[Path]:
    """Glob patterns (absolute) of every declared client cache for ``plugin`` (``*`` when ``plugin`` is ``*``).

    The environment override replaces the base directory; the default base is kept too, so a copy written before the
    variable was set is still found."""
    patterns: list[Path] = []
    for driver in BUILTIN:
        if clients is not None and driver["id"] not in clients:
            continue
        for entry in driver.get("runtime_cache", []):
            bases = []
            override = getenv(entry["env"]) if entry.get("env") else None
            if override:
                bases.append(Path(override))
            if with_defaults or not bases:
                bases.append(_expand(entry["base"], home))
            for base in bases:
                pattern = base / entry["glob"].replace("<plugin>", plugin)
                if pattern not in patterns:
                    patterns.append(pattern)
    return patterns


def runtime_cache_copies(home: Path, getenv: Callable[[str], str | None], plugin: str,
                         clients: Any = None) -> list[Path]:
    """Existing directories of ``plugin`` in the declared client caches (not filtered by content, not ordered).
    ``clients`` limits the search to those client ids (default: every declared cache)."""
    found: list[Path] = []
    for pattern in _cache_patterns(home, getenv, plugin, clients=clients):
        anchor = Path(pattern.anchor)
        relative = pattern.relative_to(anchor).as_posix()
        for path in sorted(anchor.glob(relative)):
            if path.is_dir() and path not in found:
                found.append(path)
    return found


def in_runtime_cache(path: Path, home: Path, getenv: Callable[[str], str | None]) -> bool:
    """Whether ``path`` lies inside one of the declared client caches (any plugin, any version)."""
    import fnmatch
    parts = path.parts
    for pattern in _cache_patterns(home, getenv, "*"):
        wanted = pattern.parts
        if len(parts) > len(wanted) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(parts, wanted)):
            return True
    return False


# ---------------------------------------------------------------- user declarations (validate only)

def declaration_dir(home: Path, getenv: Callable[[str], str | None]) -> Path:
    base = getenv("XDG_CONFIG_HOME")
    return (Path(base) if base else home / ".config") / "agenthelm" / "clients.d"


def _holds_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_holds_key(item, key) for item in value.values())
    return isinstance(value, list) and any(_holds_key(item, key) for item in value)


def validate_declaration(data: Any) -> str | None:
    """The reason a declaration is invalid (``None`` when valid). Only data fields are accepted."""
    if not isinstance(data, dict):
        return _("顶层必须是对象")
    if _holds_key(data, "steps"):  # D-R1: a user file never decides which command runs
        return _("含 steps（命令步骤）：用户声明不能决定运行什么命令，只有随包的内置声明可以")
    if _holds_key(data, "hooks"):  # D-R1: hook declarations pick the commands hooks run and the output they emit
        return _("含 hooks（hook 事件与输出格式）：用户声明不能决定 hook 运行什么，只有随包的内置声明可以")
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
        entry = {"id": item["id"], "display": item["display"], "source": "builtin", **found,
                 "distribution": distribution_type(item),
                 "capabilities": {key: item["capabilities"][key] for key in CAPABILITIES},
                 "install_supported": distribution_type(item) != "none"}
        if lifecycle(item) != LIFECYCLES[0]:  # reported only when it differs from the default: other entries are unchanged
            entry["lifecycle"] = lifecycle(item)
        clients.append(entry)
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
            item["id"], item["display"], seen, item["distribution"], ", ".join(flags) or "-")
            + (_("；已弃用（厂商已用其他客户端取代它）") if item.get("lifecycle") == "deprecated" else ""))
    for warning in data["warnings"]:
        lines.append(_("警告：忽略声明 {0}：{1}").format(warning["file"], warning["reason"]))
    return "\n".join(lines) + "\n"
