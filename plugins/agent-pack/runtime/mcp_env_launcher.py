#!/usr/bin/env python3
"""Launch one stdio MCP after loading a project-local dotenv file.

The launcher never prints dotenv values. Configuration contains only variable
references and is generated into a client-local project capability package.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
RESERVED = ("PLUGIN_ROOT", "PLUGIN_DATA")


def plugin_root() -> Path:
    """The installed package root: this launcher lives in ``<plugin_root>/runtime/``.

    Deliberately not resolved: keep the path the client itself launched, so a symlinked
    install (Claude) still refers to the same files the client sees.
    """
    return Path(__file__).absolute().parent.parent


def load_dotenv(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"credential env file missing or unsafe: {path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def expand(value: Any, environment: dict[str, str], reserved: dict[str, str] | None = None) -> Any:
    reserved = reserved or {}
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in RESERVED and name in reserved:
                return reserved[name]
            if name in RESERVED:
                raise RuntimeError(f"{name} was not provided by the client")
            if name not in environment:
                raise RuntimeError(f"credential env file missing key: {name}")
            return environment[name]

        return REFERENCE.sub(replace, value)
    if isinstance(value, list):
        return [expand(item, environment, reserved) for item in value]
    if isinstance(value, dict):
        return {key: expand(item, environment, reserved) for key, item in value.items()}
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    # The client expands ${PLUGIN_DATA} in this launcher's own args; the config file it never sees.
    parser.add_argument("--plugin-data", default=None)
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or config.get("type") != "stdio":
            raise RuntimeError("launcher config must be a stdio MCP object")
        environment = dict(os.environ)
        environment.update(load_dotenv(args.env_file))
        reserved = {"PLUGIN_ROOT": str(plugin_root())}
        data = args.plugin_data
        if data and not REFERENCE.search(data):
            reserved["PLUGIN_DATA"] = data
        # Reserved names are never read from the process or dotenv: a credential file must not redirect the package.
        config = expand(config, environment, reserved)
        command = config.get("command")
        command_args = config.get("args", [])
        if not isinstance(command, str) or not command:
            raise RuntimeError("stdio command is required")
        if not isinstance(command_args, list) or not all(isinstance(item, str) for item in command_args):
            raise RuntimeError("stdio args must be a string array")
        child_env = dict(environment)
        raw_env = config.get("env", {})
        if not isinstance(raw_env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in raw_env.items()):
            raise RuntimeError("stdio env must be a string object")
        child_env.update(raw_env)
        cwd = config.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                raise RuntimeError("stdio cwd must be a string")
            os.chdir(cwd)
        os.execvpe(command, [command, *command_args], child_env)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"agent-pack MCP launch failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
