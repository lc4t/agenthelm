#!/usr/bin/env python3
"""GitHub Release notes: the CHANGELOG.md section for the tag being released.

``python3 scripts/release_notes.py v6.11.0-rc.2`` prints the notes. The section is the one whose heading names
the tag's version (``## 2026-10-31 — 6.11.0-rc.2：…``); for a candidate without its own section, the section of
the release it leads to (``## … — agenthelm 6.11.0 / …``). With no matching section the notes are the fixed
sentence that pointed at CHANGELOG.md and release/migrations.json before.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FALLBACK = "版本与迁移说明见 CHANGELOG.md 与 release/migrations.json。"
# GitHub rejects release bodies above 125000 characters.
LIMIT = 60000


def sections(changelog: str) -> list[tuple[str, str]]:
    """(heading, body) for every ``## `` section, in file order."""
    found: list[tuple[str, list[str]]] = []
    for line in changelog.splitlines():
        if line.startswith("## "):
            found.append((line[3:].strip(), []))
        elif found:
            found[-1][1].append(line)
    return [(heading, "\n".join(body).strip("\n")) for heading, body in found]


def names(heading: str, version: str) -> bool:
    """The heading is about this version: "— <version>" or "agenthelm <version>", not a longer one (6.11.0-rc.2)."""
    pattern = rf"(?:— |agenthelm ){re.escape(version)}(?![\w.-])"
    return re.search(pattern, heading) is not None


def notes_for(changelog: str, tag: str) -> str | None:
    version = tag[1:] if tag.startswith("v") else tag
    wanted = [version]
    base = version.split("-", 1)[0]
    if base != version:
        wanted.append(base)
    for candidate in wanted:
        for heading, body in sections(changelog):
            if names(heading, candidate):
                return f"## {heading}\n\n{body}".rstrip() + "\n"
    return None


def release_notes(changelog: str | None, tag: str) -> str:
    section = notes_for(changelog, tag) if changelog else None
    if section is None:
        return FALLBACK + "\n"
    if len(section) > LIMIT:
        section = section[:LIMIT].rsplit("\n", 1)[0] + "\n\n…（已截断，全文见 CHANGELOG.md）\n"
    return section + "\n---\n\n" + FALLBACK + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_notes.py <tag>", file=sys.stderr)
        return 2
    path = ROOT / "CHANGELOG.md"
    try:
        changelog = path.read_text(encoding="utf-8")
    except OSError:
        changelog = None
    sys.stdout.write(release_notes(changelog, argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
