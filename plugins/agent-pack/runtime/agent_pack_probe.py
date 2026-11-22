#!/usr/bin/env python3
"""Runner for a driver's read-only ``detect.cli`` probe (#134, design section 4.2).

A probe is ``{argv, stream, parse}``: fixed arguments for the client's own executable (found through
``detect.executables``), the stream the answer is on (some clients print JSON or help text on ``stderr``) and how to
read it (``json`` | ``text``). A successful probe is ``cli`` evidence.

**Detection never calls this module.** Only a command the user asked for (``inventory``, ``doctor``) may, and this
version wires none of them to it. The runner takes the subprocess function as a parameter, so tests use a stub and no
test needs a real client. The default runner starts the program as a list (no shell), with empty standard input and a
timeout, and reads output only; it never writes a file and the argument list is fixed by the declaration.
Standard library only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))
import agent_pack_clients as registry  # noqa: E402

DEFAULT_TIMEOUT = 10.0
MAX_OUTPUT = 1024 * 1024  # bytes read from the chosen stream; more than this is ``too-large``, never parsed

# ``reason`` of a failed probe (English identifiers, data): the caller decides how to word them.
REASONS = ("invalid-probe", "not-found", "timeout", "unreadable", "nonzero-exit", "too-large", "unexpected-format")


def default_runner(argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    """(exit code, stdout, stderr). Raises ``FileNotFoundError`` / ``PermissionError`` / ``subprocess.TimeoutExpired``."""
    done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, shell=False, check=False)
    return done.returncode, done.stdout, done.stderr


Runner = Callable[[list[str], float], "tuple[int, bytes | str, bytes | str]"]


def _bytes(value: Any) -> bytes:
    return value if isinstance(value, bytes) else str(value or "").encode("utf-8")


def run_probe(probe: Any, executable: str | Path, runner: Runner | None = None,
              timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Run ``probe`` against ``executable`` and read the declared stream.

    Returns ``{"ok": True, "evidence": "cli", "stream", "parse", "data"}`` where ``data`` is the parsed JSON value or the
    decoded text, or ``{"ok": False, "evidence": "unknown", "reason": <one of REASONS>}``. Never raises: a missing
    program, a timeout, a non-zero exit, output over ``MAX_OUTPUT`` bytes, output that is not text, or JSON that does
    not parse is a failed probe, and a failed probe is not evidence of anything."""
    def failed(reason: str) -> dict:
        return {"ok": False, "evidence": "unknown", "reason": reason}

    if registry.probe_problem(probe) is not None:
        return failed("invalid-probe")
    try:
        code, out, err = (runner or default_runner)([str(executable), *probe["argv"]], timeout)
    except FileNotFoundError:
        return failed("not-found")
    except subprocess.TimeoutExpired:
        return failed("timeout")
    except (OSError, ValueError):
        return failed("unreadable")
    if code != 0:
        return failed("nonzero-exit")
    raw = _bytes(out if probe["stream"] == "stdout" else err)
    if len(raw) > MAX_OUTPUT:
        return failed("too-large")
    try:
        text = raw.decode("utf-8")
        data: Any = json.loads(text) if probe["parse"] == "json" else text
    except (UnicodeDecodeError, ValueError):
        return failed("unexpected-format")
    if probe["parse"] == "text" and not text.strip():
        return failed("unexpected-format")  # a text probe expects some text on the declared stream
    return {"ok": True, "evidence": "cli", "stream": probe["stream"], "parse": probe["parse"], "data": data}
