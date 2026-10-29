#!/usr/bin/env python3
"""MCP stdio adapter for agent-pack capability-management operations.

Primary protocol: MCP 2026-07-28 (stateless, per-request metadata).
Compatibility: legacy initialize/notifications/initialized clients.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from agent_pack_config import (
    AgentPackError,
    doctor,
    install_plugin,
    inventory_source,
    reconcile_project,
    sync_project,
    transfer_to_project,
)


PROTOCOL_VERSION = "2026-07-28"
LEGACY_VERSION = "2025-06-18"
def _plugin_version() -> str:
    """Single source of truth: the sibling plugin.json version."""
    try:
        manifest = json.loads((Path(__file__).resolve().parents[1] / "plugin.json").read_text(encoding="utf-8"))
        version = manifest.get("version")
        return version if isinstance(version, str) else "0.0.0"
    except (OSError, json.JSONDecodeError):
        return "0.0.0"


SERVER_INFO = {"name": "agent-pack", "version": _plugin_version()}
SERVER_META = {"io.modelcontextprotocol/serverInfo": SERVER_INFO}


TOOLS = [
    {
        "name": "agent_pack_bootstrap",
        "title": "Install Agent Plugin",
        "description": "Dry-run or install a portable plugin, including agent-pack itself, into Codex, Cursor, or Claude Code without overwriting an existing package. For Claude Code, skills / mcps limit the install to those components, and local credential values on MCP entries are kept unless resetOverrides.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "plugin": {"type": "string"},
                "client": {"type": "string", "enum": ["codex", "cursor", "claude"]},
                "home": {"type": "string"},
                "apply": {"type": "boolean", "default": False},
                "replace": {"type": "boolean", "default": False},
                "recoveryRoot": {"type": "string"},
                "skills": {"type": "array", "items": {"type": "string"}, "default": []},
                "mcps": {"type": "array", "items": {"type": "string"}, "default": []},
                "resetOverrides": {"type": "boolean", "default": False},
            },
            "required": ["plugin", "client"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "agent_pack_reconcile",
        "title": "Reconcile Project and Client Capabilities",
        "description": "Classify capability drift, safely replace the managed project package, and archive explicitly selected legacy Skills or plugins with rollback. Claude Code gets its own package, MCP entries and Skill links; local credential values are kept unless resetOverrides. apply defaults to false.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "client": {"type": "string", "enum": ["codex", "cursor", "claude"]},
                "replace": {"type": "boolean", "default": False},
                "resetOverrides": {"type": "boolean", "default": False},
                "home": {"type": "string"},
                "retireSkills": {"type": "array", "items": {"type": "string"}, "default": []},
                "retirePlugins": {"type": "array", "items": {"type": "string"}, "default": []},
                "recoveryRoot": {"type": "string"},
                "retireOnly": {"type": "boolean", "default": False},
                "apply": {"type": "boolean", "default": False},
            },
            "required": ["project", "client"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": True, "idempotentHint": False},
    },
    {
        "name": "agent_pack_inventory",
        "title": "Inventory Agent Capabilities",
        "description": "Discover portable Agent Plugins, Skills, and MCP servers from a project, plugin directory, or supported client.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "project:/path, plugin:/path, client:codex, client:cursor, or client:claude"},
                "home": {"type": "string"},
            },
            "required": ["source"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agent_pack_doctor",
        "title": "Validate Project Capabilities",
        "description": "Validate an initialized project's Agent Plugin, Skill, MCP, duplicate-name, portability, and secret boundaries. With clients, also report each client's missing, stale and different components and kept local overrides.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "clients": {"type": "array", "items": {"type": "string", "enum": ["codex", "cursor", "claude"]},
                            "default": []},
                "home": {"type": "string"},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agent_pack_cloud_sync_safety_check",
        "title": "Check Cloud-Sync Safety of a Project",
        "description": "Read-only. Inspect a project that lives (or may live) inside an iCloud Drive, Dropbox, OneDrive or Google Drive folder and report anything that should stay on this machine but would be synced: .venv and other virtualenvs, node_modules, tool caches, SQLite -wal/-shm files, plus conflict copies, directories close to or over the configured size limits, npm/pnpm use (their node_modules cannot be moved out of the synced folder; yarn classic and bun can), whether externalized links point at this machine's local store and still exist (a missing target means dependencies are not installed on this device), and whether HOME matches the project's recorded HOME (a hard requirement for the links). Returns structured findings with ids, severity (attention=true means act now), the evidence, and the exact CLI command to run for each fix. It never changes files; to fix, run the returned `agenthelm local-only fix` command (preview by default, --apply to write) or ask the user. Findings the user already dismissed on this machine are left out and listed under `skipped`. Projects that are not in a synced folder and have no local-only configuration return no findings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project directory (any folder inside an agenthelm project works)."},
                "mode": {"type": "string", "enum": ["quick", "standard", "full"], "default": "standard",
                         "description": "quick: shallow scan without sizes (about 1.5 s); standard: complete scan within a 6 s budget; full: complete scan, up to 2 minutes."},
                "home": {"type": "string", "description": "Treat this directory as HOME (testing or isolation)."},
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agent_pack_transfer",
        "title": "Transfer Skills and MCP Servers",
        "description": "Dry-run or copy explicitly selected portable Skills and MCP definitions into another initialized project. apply defaults to false.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "toProject": {"type": "string"},
                "skills": {"type": "array", "items": {"type": "string"}, "default": []},
                "mcps": {"type": "array", "items": {"type": "string"}, "default": []},
                "home": {"type": "string"},
                "apply": {"type": "boolean", "default": False},
            },
            "required": ["source", "toProject"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "agent_pack_sync",
        "title": "Sync Project Capabilities to an Agent Client",
        "description": "Dry-run or install a project's portable capability package into Codex, Cursor or Claude Code. apply defaults to false; replaced content goes to the recovery directory. Claude Code keeps local credential values unless resetOverrides, and only replaces entries it did not install with replace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "client": {"type": "string", "enum": ["codex", "cursor", "claude"]},
                "home": {"type": "string"},
                "apply": {"type": "boolean", "default": False},
                "replace": {"type": "boolean", "default": False},
                "resetOverrides": {"type": "boolean", "default": False},
                "recoveryRoot": {"type": "string"},
            },
            "required": ["project", "client"],
            "additionalProperties": False,
        },
        "annotations": {"destructiveHint": False, "idempotentHint": False},
    },
]


def _modern(params: dict[str, Any]) -> bool:
    meta = params.get("_meta", {})
    return isinstance(meta, dict) and meta.get("io.modelcontextprotocol/protocolVersion") == PROTOCOL_VERSION


def _result(payload: dict[str, Any], modern: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
        "isError": not payload.get("ok", True),
    }
    if modern:
        result["resultType"] = "complete"
        result["_meta"] = SERVER_META
    return result


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    home_value = arguments.get("home")
    home = Path(home_value) if isinstance(home_value, str) else None
    if name == "agent_pack_bootstrap":
        recovery_value = arguments.get("recoveryRoot")
        recovery = Path(recovery_value) if isinstance(recovery_value, str) else None
        return install_plugin(
            Path(arguments["plugin"]),
            arguments["client"],
            home=home,
            apply=bool(arguments.get("apply", False)),
            replace=bool(arguments.get("replace", False)),
            recovery_root=recovery,
            skills=arguments.get("skills", []),
            mcps=arguments.get("mcps", []),
            reset_overrides=bool(arguments.get("resetOverrides", False)),
        )
    if name == "agent_pack_reconcile":
        recovery_value = arguments.get("recoveryRoot")
        recovery = Path(recovery_value) if isinstance(recovery_value, str) else None
        return reconcile_project(
            Path(arguments["project"]),
            arguments["client"],
            home=home,
            retire_skills=arguments.get("retireSkills", []),
            retire_plugins=arguments.get("retirePlugins", []),
            recovery_root=recovery,
            apply=bool(arguments.get("apply", False)),
            retire_only=bool(arguments.get("retireOnly", False)),
            replace=bool(arguments.get("replace", False)),
            reset_overrides=bool(arguments.get("resetOverrides", False)),
        )
    if name == "agent_pack_inventory":
        inventory = inventory_source(arguments["source"], home)
        payload = inventory.to_dict()
        payload["ok"] = not payload["issues"]
        return payload
    if name == "agent_pack_doctor":
        return doctor(Path(arguments["project"]), arguments.get("clients", []), home)
    if name == "agent_pack_transfer":
        source = inventory_source(arguments["source"], home)
        return transfer_to_project(
            source,
            Path(arguments["toProject"]),
            arguments.get("skills", []),
            arguments.get("mcps", []),
            apply=bool(arguments.get("apply", False)),
        )
    if name == "agent_pack_sync":
        recovery_value = arguments.get("recoveryRoot")
        return sync_project(
            Path(arguments["project"]),
            arguments["client"],
            home=home,
            apply=bool(arguments.get("apply", False)),
            replace=bool(arguments.get("replace", False)),
            reset_overrides=bool(arguments.get("resetOverrides", False)),
            recovery_root=Path(recovery_value) if isinstance(recovery_value, str) else None,
        )
    if name == "agent_pack_cloud_sync_safety_check":
        import agenthelm  # noqa: PLC0415 - shipped next to this file

        mode = arguments.get("mode", "standard")
        if mode not in ("quick", "standard", "full"):
            raise AgentPackError("mode must be quick, standard or full")
        report = agenthelm.local_only_report(agenthelm.current_env(home), Path(arguments["project"]), mode=mode)
        report["ok"] = True
        return report
    raise AgentPackError(f"unknown tool: {name}")


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    if message.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in message:
        return None
    request_id = message["id"]
    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    modern = _modern(params)
    try:
        if method == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": [PROTOCOL_VERSION],
                "capabilities": {"tools": {}},
                "_meta": SERVER_META,
                "instructions": "Inventory first. Transfer and sync default to dry-run; set apply=true only after user approval.",
                "ttlMs": 3600000,
                "cacheScope": "public",
            }
        elif method == "initialize":
            result = {
                "protocolVersion": LEGACY_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
                "instructions": "Inventory first; write operations require apply=true.",
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
            if modern:
                result.update({
                    "resultType": "complete",
                    "ttlMs": 300000,
                    "cacheScope": "public",
                    "_meta": SERVER_META,
                })
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise AgentPackError("tools/call requires string name and object arguments")
            result = _result(_call(name, arguments), modern)
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except (AgentPackError, KeyError, TypeError, ValueError) as exc:
        payload = {"ok": False, "error": str(exc)}
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": request_id, "result": _result(payload, modern)}
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}}


def main() -> int:
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("message must be an object")
            response = handle_message(message)
        except (json.JSONDecodeError, ValueError) as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
