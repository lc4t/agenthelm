#!/usr/bin/env python3
"""Shared building blocks of the Skill write commands (``agenthelm relocate`` / ``dedupe``).

Plan shape, ``plan_digest``, directory-tree digest and read-back, synced-folder heuristic, and the archive step
(a move into the recovery directory, never a delete) live here so both commands behave the same way. Nothing in
this module prints a secret: it only ever sees Skill directories and paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from i18n_agent_pack import _

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

SCHEMA_VERSION = 1
PROMISES = ("certain", "best-effort", "cannot-promise")
MAX_TREE_FILES = 20000
# Folder names of the common file-sync products. A heuristic: a match demotes a symlink to a copy and the plan says
# so; no match proves nothing (the official documentation on symlinks inside synced folders was not found).
SYNC_FOLDER_MARKERS = ("icloud drive", "mobile documents", "dropbox", "onedrive", "google drive", "googledrive",
                       "cloudstorage", "box sync", "pcloud", "nextcloud", "syncthing")


def unsupported_block(text: str | None, feature: str) -> dict | None:
    """A ``blocked`` entry when ``text`` (a reference ``kind:name@client:scope`` or a target ``client:scope``) names a client whose
    driver declares ``feature`` unsupported (``unsupported`` in the driver); ``None`` otherwise. The reason is the driver's own."""
    import agent_pack_clients as registry  # noqa: PLC0415 - shipped next to this file

    if not text:
        return None
    client = (text.rpartition("@")[2] if "@" in text else text).partition(":")[0].split("#", 1)[0]
    reason = registry.unsupported(client, feature)
    return blocked("unsupported-client", _("{0} 不支持 {1}：{2}").format(client, feature, reason)) if reason else None


class ExtError(Exception):
    """A failure that must roll back and be reported without a traceback."""


def canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def plan_digest(plan: dict) -> str:
    """SHA-256 over the plan without its own digest and without per-run values (timestamps)."""
    body = {key: value for key, value in plan.items() if key not in ("plan_digest", "recovery_dir", "next")}
    return hashlib.sha256(canonical(_without_text(body)).encode("utf-8")).hexdigest()


def _without_text(value: Any) -> Any:
    """Localized wording (``message`` / ``text``) is not part of the digest: it must not depend on the language."""
    if isinstance(value, dict):
        return {key: _without_text(item) for key, item in value.items() if key not in ("message", "text")}
    if isinstance(value, list):
        return [_without_text(item) for item in value]
    return value


def warning(code: str, message: str, **detail: Any) -> dict:
    return {"code": code, "message": message, **detail}


def step(step_id: str, action: str, promise: str, **detail: Any) -> dict:
    assert promise in PROMISES
    return {"id": step_id, "action": action, "promise": promise, **detail}


def blocked(code: str, message: str, **detail: Any) -> dict:
    return {"code": code, "message": message, **detail}


# ------------------------------------------------------------------ directory trees

def tree_manifest(root: Path) -> list[list[str]] | None:
    """``[kind, relative path, digest-or-target]`` for every entry below ``root`` (a directory, or a link to one).

    Files carry their SHA-256 and executable bit, symbolic links their literal target, empty directories only the
    path. ``None`` when the tree has more than ``MAX_TREE_FILES`` entries. Symbolic links are never followed.
    """
    top = Path(os.path.realpath(root))
    rows: list[list[str]] = []
    for base, dirs, files in os.walk(top, followlinks=False):
        dirs.sort()
        base_path = Path(base)
        relative_base = base_path.relative_to(top)
        for name in sorted(dirs):
            full = base_path / name
            if full.is_symlink():
                rows.append(["l", (relative_base / name).as_posix(), os.readlink(full)])
        if not dirs and not files and base_path != top:
            rows.append(["d", relative_base.as_posix(), ""])
        for name in sorted(files):
            full = base_path / name
            rel = (relative_base / name).as_posix()
            if full.is_symlink():
                rows.append(["l", rel, os.readlink(full)])
                continue
            digest = hashlib.sha256()
            with full.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            rows.append(["f", rel, digest.hexdigest() + (":x" if os.access(full, os.X_OK) else "")])
        if len(rows) > MAX_TREE_FILES:
            return None
    rows.sort(key=lambda row: (row[1], row[0]))
    return rows


def tree_digest(manifest: list[list[str]]) -> str:
    return hashlib.sha256(canonical(manifest).encode("utf-8")).hexdigest()


def external_links(root: Path, manifest: list[list[str]]) -> list[str]:
    """Relative paths of symbolic links inside the tree that point outside it (or are absolute)."""
    top = Path(os.path.realpath(root))
    bad = []
    for kind, rel, target in manifest:
        if kind != "l":
            continue
        resolved = Path(os.path.realpath(top / rel))
        inside = resolved == top or str(resolved).startswith(str(top) + os.sep)
        if os.path.isabs(target) or not inside:
            bad.append(rel)
    return bad


def copy_tree(source: Path, destination: Path) -> None:
    """Copy keeping inner symbolic links as links and file modes; ``destination`` must not exist."""
    shutil.copytree(Path(os.path.realpath(source)), destination, symlinks=True)


def readback_matches(source_manifest: list[list[str]], target: Path) -> bool:
    """Per-file comparison of relative paths and content digests (includes ``SKILL.md``)."""
    manifest = tree_manifest(target)
    return manifest is not None and manifest == source_manifest


# ------------------------------------------------------------------ paths

def display(path: Path | str, home: Path, project: Path | None) -> str:
    text = str(path)
    for base, label in ((project, "<project>"), (home, "~")):
        if base is None:
            continue
        prefix = str(base)
        if text == prefix:
            return label
        if text.startswith(prefix + os.sep):
            return label + text[len(prefix):]
    return text


def shell_path(path: Path | str, home: Path) -> str:
    """A path that can be pasted into a shell: ``~/`` kept outside the quotes so the shell expands it."""
    text = str(path)
    prefix = str(home) + os.sep
    if text.startswith(prefix):
        return "~/" + shlex.quote(text[len(prefix):])
    return shlex.quote(text)


def sync_folder_hint(path: Path) -> str | None:
    """Name of the sync-product folder ``path`` sits in (checked on the literal and the resolved path), else ``None``."""
    for candidate in (Path(os.path.abspath(path)), Path(os.path.realpath(path))):
        for part in candidate.parts:
            lowered = part.lower()
            for marker in SYNC_FOLDER_MARKERS:
                if lowered == marker or lowered.startswith(marker + " ") or lowered.startswith(marker + "-"):
                    return part
    return None


def recovery_base(home: Path, override: Path | None = None) -> Path:
    return (override or home / ".agent-pack" / "recovery").expanduser().resolve()


def new_recovery_dir(base: Path, label: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = base / f"{stamp}-{label}"
    directory.mkdir(parents=True, mode=0o700)
    os.chmod(directory, 0o700)
    return directory


# ------------------------------------------------------------------ archive (a move, never a delete)

def archive(path: Path, recovery: Path, bucket: str, name: str) -> Path:
    """Move ``path`` (a directory or a symbolic link) to ``recovery/<bucket>/<name>``; returns the new place."""
    destination = recovery / bucket / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ExtError(f"archive destination exists: {destination.name}")
    shutil.move(str(path), str(destination))
    return destination


def restore_line(archived: Path, original: Path, home: Path) -> str:
    return f"mv {shell_path(archived, home)} {shell_path(original, home)}"


def rollback_moves(moved: list[tuple[Path, Path]]) -> None:
    """Undo archive moves in reverse order; an original that reappeared meanwhile is never overwritten."""
    for archived, original in reversed(moved):
        if (archived.exists() or archived.is_symlink()) and not (original.exists() or original.is_symlink()):
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(archived), str(original))


def remove_created(path: Path) -> None:
    """Remove something this run created (a link, or a directory copy)."""
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def remove_empty_parents(paths: list[Path]) -> None:
    for path in reversed(paths):
        try:
            path.rmdir()
        except OSError:
            pass


def make_parents(directory: Path) -> list[Path]:
    """``mkdir -p`` that returns the directories it created, outermost first."""
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    missing.reverse()
    for item in missing:
        item.mkdir()
    return missing


def write_private_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def use_color(stream=None) -> bool:
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")


def red(text: str, color: bool) -> str:
    return f"\033[31m{text}\033[0m" if color else text
