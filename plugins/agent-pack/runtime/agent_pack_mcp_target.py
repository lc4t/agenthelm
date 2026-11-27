#!/usr/bin/env python3
"""Which service an MCP entry points at (``target``), and whether two entries with the same fingerprint point at the same one.

The inventory fingerprint is a digest of the *redacted* ``command + args`` (or ``url``), so two entries that differ only
where redaction replaced the value (a path longer than 64 characters, a token-shaped service id, a query value), or only in
what the fingerprint never reads (``env`` values, ``cwd``), get the same fingerprint although they start different services.
The fingerprint stays as it is (group ids and ``ext-decisions.json`` are keyed by it); this module compares the *targets*
in process, from the raw values, and hands out only class names and sequence numbers: a raw value never leaves it.

Pure functions, no file access, no client ids: everything is derived from the entry's own keys and the launcher tables below.
Contract: ``references/inventory-contract.md`` (``target``, ``target_class``, ``mcp_hints``) and design ``docs/design/inventory-recommendations.md`` §4.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable, NamedTuple
from urllib.parse import parse_qsl, urlsplit

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))
import agent_pack_config as core  # noqa: E402

REDACTED = "<redacted>"
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")
_VAR_DEFAULT_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-[^}]*\}")
CATEGORIES = ("target", "args", "env", "cwd", "url")  # the field classes ``differs_in`` may name, in this order
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}

# Launcher tables (data, so a launcher outside them degrades to ``binary`` and never to a guess). A package launcher runs
# the first positional argument as a package spec; ``flags`` take no value, ``valued`` take the next argument and belong to
# the target (``--from`` changes what runs). An interpreter runs the first positional argument as a script.
PACKAGE_LAUNCHERS: dict[str, dict[str, Any]] = {
    "npx": {"eco": "npm", "sub": (), "flags": ("-y", "--yes", "-q", "--quiet", "--no", "--no-install", "--prefer-online",
                                              "--prefer-offline"),
            "valued": ("-p", "--package", "--prefix", "--registry", "--cache", "--userconfig")},
    "bunx": {"eco": "npm", "sub": (), "flags": ("--bun", "--silent", "--verbose"), "valued": ("-p", "--package")},
    "uvx": {"eco": "pypi", "sub": (), "flags": ("--isolated", "--no-cache", "-q", "--quiet", "--refresh", "--no-config"),
            "valued": ("--from", "--with", "-p", "--python", "--index", "--index-url", "--extra-index-url",
                       "--with-requirements", "--env-file")},
    "pipx": {"eco": "pypi", "sub": ("run",), "flags": ("--no-cache",), "valued": ("--spec", "--python", "--index-url")},
    "pnpm": {"eco": "npm", "sub": ("dlx",), "flags": ("--silent", "-s"), "valued": ("--package", "-p", "--registry")},
    "yarn": {"eco": "npm", "sub": ("dlx",), "flags": ("-q", "--quiet"), "valued": ("-p", "--package")},
    "npm": {"eco": "npm", "sub": ("exec", "x"), "flags": ("-y", "--yes", "--no", "--silent"),
            "valued": ("-p", "--package", "--registry")},
}
INTERPRETERS = frozenset({"python", "node", "bash", "sh", "zsh", "ruby", "bun", "deno", "perl"})
INTERPRETER_SUBCOMMANDS = {"bun": ("run",), "deno": ("run",)}
INTERPRETER_FLAGS = frozenset({"-u", "-B", "-E", "-s", "-S", "-I", "-O", "-OO", "-q", "-v", "-A", "--allow-all", "--no-warnings",
                               "--enable-source-maps", "--experimental-vm-modules"})
INLINE_CODE_FLAGS = frozenset({"-c", "-e", "--eval", "-p", "--print"})
MODULE_FLAGS = frozenset({"-m"})
# Commands whose option set is too large to read reliably: always ``opaque``.
OPAQUE_COMMANDS = frozenset({"docker", "podman", "nerdctl", "env", "sudo", "cmd", "powershell", "pwsh", "wsl"})
_PYTHON_RE = re.compile(r"^python[0-9.]*$")
_EXE_SUFFIX_RE = re.compile(r"\.(?:exe|cmd|bat)$")
_PYPI_SPLIT_RE = re.compile(r"[=<>!~\[;@ ]")


def plain_variables(text: str) -> str:
    """``${VAR:-default}`` keeps only the variable name: a default value is a value."""
    return _VAR_DEFAULT_RE.sub(lambda match: "${" + match.group(1) + "}", text)


def safe_reference(text: str) -> bool:
    return core._safe_env_reference(text)


# ------------------------------------------------------------------ argument classification (shared with the redaction)

class Arg(NamedTuple):
    """One launch argument: ``shown`` is what the inventory prints; ``kind`` is ``plain``, ``token-like`` (redacted by
    length / shape only) or ``credential`` (redacted by a secret name or a strong secret shape); ``text`` is the argument
    with ``${VAR:-x}`` reduced to ``${VAR}``; ``secret`` is the part a credential redaction replaced."""
    shown: str
    kind: str
    text: str
    secret: str | None


def scan_args(args: list[Any]) -> list[Arg]:
    """Every argument with how it is redacted. ``[arg.shown for arg in scan_args(x)]`` is the redacted argument list the
    fingerprint is made of (``agent_pack_inventory._redact_args``)."""
    result: list[Arg] = []
    previous_secret_flag = False
    for raw in args:
        text = plain_variables(str(raw))
        if previous_secret_flag and not text.startswith("-") and not safe_reference(text):
            result.append(Arg(REDACTED, "credential", text, text))
            previous_secret_flag = False
            continue
        previous_secret_flag = False
        if text.startswith("-") and "=" in text:
            flag, value = text.split("=", 1)
            if (core.SECRET_NAME_RE.search(flag) or core._secret_like_arg(value)) and not safe_reference(value):
                result.append(Arg(f"{flag}={REDACTED}", "credential", flag + "=", value))
                continue
        elif text.startswith("-") and core.SECRET_NAME_RE.search(text):
            previous_secret_flag = True
        if core.SECRET_VALUE_RE.search(text) and not safe_reference(text):
            result.append(Arg(REDACTED, "credential", text, text))
        elif not text.startswith("-") and not safe_reference(text) and core.looks_like_token(text):
            result.append(Arg(REDACTED, "token-like", text, None))
        else:
            result.append(Arg(text, "plain", text, None))
    return result


# ------------------------------------------------------------------ analysis of one entry

def _exe(command: str) -> str:
    return _EXE_SUFFIX_RE.sub("", Path(command.replace("\\", "/")).name.lower())


def _label(text: str) -> str:
    """A label is shown: nothing credential- or token-shaped may be in it."""
    if not text or core.SECRET_VALUE_RE.search(text) or core.looks_like_token(text):
        return REDACTED
    return text


def _package_identity(spec: str, eco: str) -> str:
    """``name@version`` of a package spec, the name normalised (npm: lower case; PyPI: PEP 503)."""
    if eco == "pypi":
        cut = _PYPI_SPLIT_RE.search(spec)
        name, rest = (spec[:cut.start()], spec[cut.start():]) if cut else (spec, "")
        return re.sub(r"[-_.]+", "-", name).lower() + rest
    at = spec.rfind("@")
    name, rest = (spec[:at], spec[at:]) if at > 0 else (spec, "")
    return name.lower() + rest


def _comparable(arg: Arg) -> str:
    """The argument as compared: a credential's value is masked (it is compared apart, in the credential positions)."""
    return "" if arg.kind == "credential" and arg.secret == arg.text else arg.text


def _stdio_target(command: str, args: list[Arg], shorten: Callable[[str], str]) -> dict:
    """``{kind, identity, label, basis, used}``: what a ``command + args`` entry starts and how many arguments that consumed
    (the rest are the service's own arguments). ``opaque`` when the option layout is not one the tables cover."""
    exe = _exe(command)
    shown_command = shorten(command)

    def opaque() -> dict:
        return {"kind": "opaque", "identity": (shown_command, *[_comparable(a) for a in args]), "label": _label(exe),
                "basis": ["command"], "used": len(args)}

    if exe in OPAQUE_COMMANDS:
        return opaque()
    spec = PACKAGE_LAUNCHERS.get(exe)
    interpreter = exe in INTERPRETERS or bool(_PYTHON_RE.match(exe))
    if spec is None and not interpreter:
        return {"kind": "binary", "identity": (shown_command,), "label": _label(shown_command), "basis": ["command"],
                "used": 0}
    index, options = 0, []
    if spec is not None:
        if spec["sub"]:
            if not args or args[0].text not in spec["sub"]:
                return {"kind": "binary", "identity": (shown_command,), "label": _label(shown_command),
                        "basis": ["command"], "used": 0}
            index = 1
    elif args and args[0].text in INTERPRETER_SUBCOMMANDS.get(exe, ()):
        index = 1
    flags = set(spec["flags"]) if spec else INTERPRETER_FLAGS
    valued = set(spec["valued"]) if spec else set()
    while index < len(args):
        text = args[index].text
        if text == "--":
            index += 1
            break
        if not text.startswith("-"):
            break
        name = text.split("=", 1)[0]
        if spec is None and name in MODULE_FLAGS and "=" not in text:
            if index + 1 >= len(args) or args[index + 1].kind == "credential":
                return opaque()
            module = args[index + 1].text
            return {"kind": "script", "identity": ("module", module, *options), "label": _label(module),
                    "basis": ["command", f"args[{index + 1}]"], "used": index + 2}
        if name in INLINE_CODE_FLAGS and spec is None:
            return opaque()
        if text in flags:
            options.append(text)
            index += 1
        elif name in valued:
            if "=" in text:
                options.append(text)
                index += 1
            elif index + 1 < len(args) and args[index + 1].kind != "credential":
                options.append(f"{text} {args[index + 1].text}")
                index += 2
            else:
                return opaque()
        else:
            return opaque()
    if index >= len(args) or args[index].kind == "credential":
        return opaque()
    first = args[index]
    basis = ["command", f"args[{index}]"]
    if spec is not None:
        identity = _package_identity(first.text, spec["eco"])
        return {"kind": "package", "identity": (identity, *options),
                "label": REDACTED if first.kind == "token-like" else _label(identity), "basis": basis, "used": index + 1}
    path = shorten(first.text)
    return {"kind": "script", "identity": (path, *options),
            "label": REDACTED if first.kind == "token-like" else _label(path), "basis": basis, "used": index + 1}


def _remote_target(url: str) -> tuple[dict, tuple, list[tuple[str, str]]] | None:
    """``(target, url_part, credentials)`` of a remote entry, or ``None`` when the URL cannot be read. Scheme / host case,
    a default port and a trailing slash are no difference; a path, a query, a non-default port are. Credential-shaped query
    values and user info go to ``credentials`` (masked in the comparison)."""
    try:
        parts = urlsplit(plain_variables(url))
        scheme, host, port = parts.scheme.lower(), (parts.hostname or "").lower(), parts.port
        username, password = parts.username, parts.password
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return None
    if port is not None and DEFAULT_PORTS.get(scheme) == port:
        port = None
    path = parts.path.rstrip("/")
    credentials: list[tuple[str, str]] = []
    if username or password:
        credentials.append(("url:userinfo", f"{username or ''}:{password or ''}"))
    query: list[tuple[str, str]] = []
    for key, value in pairs:
        if not safe_reference(value) and (core.SECRET_NAME_RE.search(key) or core.SECRET_VALUE_RE.search(value)):
            credentials.append((f"url:query:{key}", value))
            query.append((key, ""))
        else:
            query.append((key, value))
    shown = host + path
    target = {"kind": "remote", "label": _label(shown), "basis": ["url"]}
    return target, (scheme, host, port, path, tuple(query)), credentials


def _env_part(item: dict) -> tuple[tuple, list[tuple[str, str]]]:
    """``(comparable env, credentials)``: entries under a secret-named key or with a strong secret shape are credential
    positions (compared apart, as ``credential-differs``); the other values are compared; ``env_vars`` (names a client forwards) compare by name."""
    pairs: list[tuple[str, str]] = []
    credentials: list[tuple[str, str]] = []
    env = item.get("env")
    if isinstance(env, dict):
        env = {str(key): raw for key, raw in env.items()}
        for key in sorted(env):
            value = str(env[key])
            if core.SECRET_NAME_RE.search(key) or (core.SECRET_VALUE_RE.search(value) and not safe_reference(value)):
                credentials.append((f"env:{key}", plain_variables(value)))   # a credential position: apart, never in the env part
            else:
                pairs.append((key, plain_variables(value)))
    forwarded = item.get("env_vars")
    names = tuple(sorted(str(v) for v in forwarded if isinstance(v, str))) if isinstance(forwarded, list) else ()
    return (tuple(pairs), names), credentials


def analyze(item: Any, command: str | None, url: str | None, branch: str, url_key: str,
            shorten: Callable[[str], str]) -> dict | None:
    """The private analysis of one entry (``branch`` is ``stdio`` or ``remote``, the one its fingerprint was made from);
    ``None`` when the entry has no fingerprint or could not be read. Holds raw values: never output it."""
    if not isinstance(item, dict) or branch not in ("stdio", "remote"):
        return None
    try:
        env, credentials = _env_part(item)
        cwd = item.get("cwd") if isinstance(item.get("cwd"), str) else None
        if branch == "stdio" and command:
            raw_args = item.get("args") if isinstance(item.get("args"), list) else []
            args = scan_args(raw_args)
            found = _stdio_target(command, args, shorten)
            for position, arg in enumerate(args):
                if arg.kind == "credential":
                    credentials.append((f"args[{position}]", arg.secret or ""))
            opaque = found["kind"] == "opaque"
            parts = {"target": (found["kind"], *found["identity"]),
                     "args": () if opaque else tuple(_comparable(arg) for arg in args[found["used"]:]),
                     "env": env, "cwd": cwd, "url": None}
            target = {"kind": found["kind"], "label": found["label"], "basis": found["basis"]}
        elif branch == "remote" and url:
            remote = _remote_target(url)
            if remote is None:
                return None
            target, url_part, url_credentials = remote
            credentials.extend(url_credentials)
            parts = {"target": ("remote",), "args": (), "env": env, "cwd": cwd, "url": url_part}
        else:
            return None
    except Exception:  # noqa: BLE001 - an entry that cannot be analysed is reported as unreadable, never an error
        return None
    return {"target": target, "parts": parts, "credentials": tuple(sorted(credentials)),
            "opaque": target["kind"] == "opaque",
            "class_key": tuple(parts[name] for name in CATEGORIES)}


# ------------------------------------------------------------------ comparison

def relation(analyses: list[dict | None]) -> dict:
    """``{relation, differs_in, reasons}`` of the entries that share a fingerprint.

    * ``different``: a non-masked field differs (``differs_in`` lists the classes); with an ``opaque`` entry only ``env``,
      ``cwd`` and ``url`` count, because its target and arguments cannot be told apart reliably.
    * ``unknown``: an entry could not be read (``unreadable``), is ``opaque`` (``opaque-launcher``), or the only difference is
      in credential positions (``credential-differs``): the same service under another account cannot be told from another
      service.
    * ``same``: one equivalence class, no ``opaque``, identical credential positions."""
    if not analyses or any(item is None for item in analyses):
        return {"relation": "unknown", "differs_in": [], "reasons": ["unreadable"]}
    opaque = any(item["opaque"] for item in analyses)
    differs = [name for name in CATEGORIES
               if len({item["parts"][name] for item in analyses}) > 1 and not (opaque and name in ("target", "args"))]
    if differs:
        return {"relation": "different", "differs_in": differs, "reasons": []}
    reasons = []
    if opaque:
        reasons.append("opaque-launcher")
    if len({item["credentials"] for item in analyses}) > 1:
        reasons.append("credential-differs")
    return {"relation": "unknown" if reasons else "same", "differs_in": [], "reasons": reasons}


def assign_classes(mcps: list[dict]) -> None:
    """``target_class`` (``t-<fingerprint[:8]>.<n>``) of every entry: equal values mean equal targets. The sequence number
    follows the order of ``mcps`` (the inventory's own sort), so one input always gives the same names; it means something
    only inside one report."""
    seen: dict[str, dict[tuple, int]] = {}
    for item in mcps:
        analysis = item.get("_target")
        fingerprint = item.get("fingerprint")
        if analysis is None or not fingerprint:
            item["target_class"] = None
            continue
        table = seen.setdefault(fingerprint, {})
        number = table.setdefault(analysis["class_key"], len(table) + 1)
        item["target_class"] = f"t-{fingerprint[:8]}.{number}"
