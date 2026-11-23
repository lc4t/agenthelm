#!/usr/bin/env python3
"""``plugin-install`` sync write: build the project package, then let the client install it with its own command line.

Some clients take a plugin only through a command (``<cli> plugin install <directory>``) that copies the directory into
their own store and records it, so ``sync`` cannot write that store itself. The driver says so with ``sync.write:
plugin-install``; the commands come from its ``distribution.steps`` (``install`` with ``{source_dir}``, ``uninstall``,
``disable``). The package is built in a staging directory of agent-pack's own (``sync.package_dir``), compared with the
copy built last time, and handed to the client. Nothing here knows a client by name: the program, the arguments and the file
names (``sync.companions``) all come from the declaration. A client without an update command has its package
uninstalled and installed again; one that was disabled before is disabled again afterwards.

The client's command lines run only on ``--apply`` (a preview runs the read-only plugin list at most, to learn whether the
package is installed). Standard library only.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))
import agent_pack_config as core  # noqa: E402
import agent_pack_probe as probe  # noqa: E402

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]
OUTPUT_TAIL = 400


def _registry():
    return core._registry()


def _command(driver: dict, step: str, executable: str, **values: str) -> list[str] | None:
    return _registry().expand_step(driver, step, cli=executable, **values)


def _run(argv: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    child = dict(os.environ)
    child["HOME"] = str(home)
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, env=child,
                          timeout=120)


def installed_entry(driver: dict, name: str, home: Path) -> dict | None | bool:
    """The client's own plugin-list entry for ``name`` (``cli`` evidence); ``None`` when it lists no such plugin and ``False``
    when the list could not be read (no program, an unreadable answer): then nothing is known."""
    result = probe.run_driver_probe(driver, home)
    if not result["ok"]:
        return False
    return next((item for item in probe.plugin_entries(result) if item["name"] == name), None)


def _fail(what: str, argv: list[str], done: subprocess.CompletedProcess[str]) -> core.AgentPackError:
    tail = (done.stderr or done.stdout or "").strip()[-OUTPUT_TAIL:]
    return core.AgentPackError(f"{what} failed: {' '.join(argv)} (exit {done.returncode})" + (f": {tail}" if tail else ""))


def sync_project(project: Path, client: str, *, home: Path | None = None, apply: bool = False,
                 recovery_root: Path | None = None, retire_skills=(), retire_plugins=(), retire_only: bool = False,
                 only=None, replace: bool = False, replace_mcp: bool = False, replace_skills: bool = False,
                 scope: str = "all", project_store: str = "local", run: Runner | None = None) -> dict[str, Any]:
    """Plan (and with ``apply`` carry out) the install of the project package into ``client``. The operations in the result
    are ``install-plugin`` / ``replace-plugin`` / ``keep-plugin`` for the staged package and ``run-client-command`` for each
    command the client would run."""
    registry = _registry()
    driver = registry.get(client)
    if driver is None or (driver.get("sync") or {}).get("write") != "plugin-install":
        raise core.AgentPackError(f"sync adapter not implemented for client: {client}")
    native = core._native_names()
    for flag, given in (("--scope user|project", scope != "all"), ("--project-store", project_store != "local"),
                        ("--only mcp|skills|plugins", bool(only)), ("--replace-skills", replace_skills),
                        ("--retire-skill / --retire-plugin / --retire-only",
                         bool(list(retire_skills) or list(retire_plugins) or retire_only))):
        if given:
            raise core.AgentPackError(f"{flag} is supported for the {native} client only: {client} installs one package "
                                      "that holds Skills, MCP servers and the manifest together, through its own command")
    home = (home or Path.home()).expanduser().resolve()
    layout = core.load_project_layout(project)
    authority = core.inventory_project(layout.root)
    name = core._package_name(layout.root)
    staged = core._client_package_destination(client, home, name)
    executable = probe.find_cli(driver["detect"]["executables"][0], home)
    shown = executable or driver["detect"]["executables"][0]

    with tempfile.TemporaryDirectory(prefix="agent-pack-plan-") as temporary:  # the checks run for a preview too
        expected = core._build_package(authority, name, Path(temporary), client, home)
        if staged.exists() or staged.is_symlink():
            package_action = ("keep-plugin" if core._tree_fingerprint(expected) == core._tree_fingerprint(staged)
                              else "replace-plugin")
        else:
            package_action = "install-plugin"
    entry = installed_entry(driver, name, home) if executable else False
    changed = package_action != "keep-plugin"
    reinstall = changed or entry is None   # the package changed, or the client does not list it
    uninstall = reinstall and bool(entry)
    was_disabled = isinstance(entry, dict) and entry.get("enabled") is False
    commands: list[tuple[str, list[str]]] = []
    if uninstall:
        commands.append(("uninstall", _command(driver, "uninstall", shown, plugin=name)))
    if reinstall:
        commands.append(("install", _command(driver, "install", shown, plugin=name, source_dir=str(staged))))
        if was_disabled:
            commands.append(("disable", _command(driver, "disable", shown, plugin=name)))
    operations: list[dict[str, str]] = [{"action": package_action, "client": client, "name": name, "to": str(staged)}]
    program = driver["detect"]["executables"][0]   # the preview names the program, not where it was found
    operations += [{"action": "run-client-command", "client": client, "name": name, "to": " ".join([program, *argv[1:]])}
                   for _step, argv in commands if argv]
    extras: dict[str, Any] = {}
    skipped = core._project_scope_skips(layout, authority, client)
    if skipped:
        extras["skipped"] = skipped
        import agent_pack_sync_scope  # noqa: PLC0415 - shipped next to this file

        extras["warnings"] = [agent_pack_sync_scope.skipped_warning(item) for item in skipped]
    states = core._component_state(authority, core.inventory_client(client, home), client=client, layout=layout)
    if not apply:
        return {"ok": True, "applied": False, "states": states, "operations": operations, **extras}
    if commands and executable is None:
        raise core.AgentPackError(f"{driver['detect']['executables'][0]} was not found (PATH and the usual locations; "
                                  f"AGENTHELM_{driver['detect']['executables'][0].upper()} names it explicitly)")

    runner = run or (lambda argv: _run(argv, home))
    recovery_base = (recovery_root or home / ".agent-pack" / "recovery").expanduser().resolve()
    recovery_base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    transaction = recovery_base / f"{stamp}-{client}-{name}"
    transaction.mkdir(mode=0o700)
    archived: Path | None = None
    placed = False
    done_steps: list[str] = []
    try:
        if changed:
            staged.parent.mkdir(parents=True, exist_ok=True)
            if staged.exists() or staged.is_symlink():
                archived = transaction / "replaced-plugin" / name
                archived.parent.mkdir(parents=True)
                os.replace(staged, archived)
            staging_parent = Path(tempfile.mkdtemp(prefix=".agent-pack-", dir=staged.parent))
            try:
                os.replace(core._build_package(authority, name, staging_parent, client, home), staged)
                placed = True
            finally:
                if staging_parent.exists():
                    shutil.rmtree(staging_parent)
        for step, argv in commands:
            result = runner(argv)
            if result.returncode != 0:
                raise _fail(step, argv, result)
            done_steps.append(step)
    except Exception as exc:
        if placed and staged.exists():
            shutil.rmtree(staged)
        if archived is not None and archived.exists() and not staged.exists():
            os.replace(archived, staged)
            if "uninstall" in done_steps:   # the client lost the old package: put it back
                try:
                    runner(_command(driver, "install", shown, plugin=name, source_dir=str(staged)) or [])
                except Exception:  # noqa: BLE001 - best effort; the failure reported is the original one
                    pass
        raise core.AgentPackError(f"reconcile rolled back: {exc}") from exc
    return {"ok": True, "applied": True, "states": states, "operations": operations, "recovery": str(transaction),
            **extras}
