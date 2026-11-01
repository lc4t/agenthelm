#!/usr/bin/env python3
"""先预演四个 Plugin 的安装与项目初始化，再按授权应用并运行 agent-pack doctor。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
AGENT_PACK = PLUGIN_ROOT / "plugins" / "agent-pack" / "runtime" / "agent_pack_config.py"
INITIALIZER = PLUGIN_ROOT / "plugins" / "agents-init" / "skills" / "agents-init" / "scripts" / "init_project.py"
INSTALL_UNITS = (
    ("agents-init", PLUGIN_ROOT / "plugins" / "agents-init"),
    ("project-orchestrator", PLUGIN_ROOT / "plugins" / "project-orchestrator"),
    ("agent-pack", PLUGIN_ROOT / "plugins" / "agent-pack"),
    ("opinion-manager", PLUGIN_ROOT / "plugins" / "opinion-manager"),
)


class BootstrapInitError(RuntimeError):
    pass


def run_json(command: list[str]) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise BootstrapInitError(f"The subcommand did not return valid JSON: {detail}") from exc
    if not isinstance(payload, dict):
        raise BootstrapInitError("The JSON root of the subcommand must be an object")
    return completed.returncode, payload


def initializer_command(args: argparse.Namespace, *, apply: bool) -> list[str]:
    command = [
        sys.executable,
        str(INITIALIZER),
        "--project", str(args.project),
        "--name", args.name,
        "--project-type", args.project_type,
        "--vcs", args.vcs,
        "--stack", args.stack,
        "--runtime", args.runtime,
        "--agent-cli", args.agent_cli,
        "--runtime-plugin-root", str(PLUGIN_ROOT),
        "--output", "json",
    ]
    if args.slug:
        command.extend(("--slug", args.slug))
    if getattr(args, 'work_mode', None):
        command.extend(('--work-mode', args.work_mode))
    if apply:
        command.append("--apply")
    return command


def install_command(plugin: Path, args: argparse.Namespace, *, apply: bool) -> list[str]:
    command = [
        sys.executable,
        str(AGENT_PACK),
        "--output", "json",
        "bootstrap",
        "--plugin", str(plugin),
        "--client", args.client,
    ]
    if args.home:
        command.extend(("--home", str(args.home)))
    if apply:
        command.append("--apply")
    return command


def doctor_command(project: Path) -> list[str]:
    return [
        sys.executable,
        str(AGENT_PACK),
        "--output", "json",
        "doctor",
        "--project", str(project),
    ]


def install_all(args: argparse.Namespace, *, apply: bool) -> tuple[bool, dict[str, Any]]:
    results: dict[str, Any] = {}
    for name, plugin in INSTALL_UNITS:
        code, payload = run_json(install_command(plugin, args, apply=apply))
        results[name] = payload
        if code != 0 or not payload.get("ok"):
            return False, results
    return True, results


def execute(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    project = args.project.expanduser().resolve()
    args.project = project

    init_code, init_preview = run_json(initializer_command(args, apply=False))
    if init_code != 0 or not init_preview.get("ok"):
        return 1, {
            "ok": False,
            "applied": False,
            "stage": "initializer-preview",
            "initializer": init_preview,
        }

    plugins_ok, plugin_preview = install_all(args, apply=False)
    if not plugins_ok:
        return 1, {
            "ok": False,
            "applied": False,
            "stage": "plugin-preview",
            "plugins": plugin_preview,
            "initializer": init_preview,
        }

    result: dict[str, Any] = {
        "ok": True,
        "applied": False,
        "stage": "preview",
        "plugins": plugin_preview,
        "initializer": init_preview,
        "doctor": None,
    }
    if not args.apply:
        return 0, result

    plugins_ok, installed = install_all(args, apply=True)
    result["plugins"] = installed
    if not plugins_ok:
        result.update(ok=False, stage="plugin-apply")
        return 1, result

    init_code, initialized = run_json(initializer_command(args, apply=True))
    result["initializer"] = initialized
    if init_code != 0 or not initialized.get("ok") or not initialized.get("applied"):
        result.update(ok=False, stage="initializer-apply")
        return 1, result

    doctor_code, checked = run_json(doctor_command(project))
    result["doctor"] = checked
    result["applied"] = True
    result["stage"] = "complete" if doctor_code == 0 and checked.get("ok") else "doctor"
    result["ok"] = result["stage"] == "complete"
    return (0 if result["ok"] else 1), result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--client", choices=("codex", "cursor", "claude"), required=True)
    result.add_argument("--project", type=Path, required=True)
    result.add_argument("--name", required=True)
    result.add_argument("--slug")
    result.add_argument("--project-type", required=True)
    result.add_argument("--vcs", required=True)
    result.add_argument("--stack", required=True)
    result.add_argument("--runtime", required=True)
    result.add_argument("--agent-cli", required=True)
    result.add_argument('--work-mode', choices=('case-workspace', 'legacy'))
    result.add_argument("--home", type=Path, help="home for tests or an explicit client home; defaults to the current user's home")
    result.add_argument("--apply", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        code, payload = execute(args)
    except (BootstrapInitError, OSError) as exc:
        code, payload = 2, {"ok": False, "applied": False, "stage": "error", "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
