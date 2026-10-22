#!/usr/bin/env python3
"""Attach the newest GitHub release to the latest.json being published on Pages.

The release workflow uploads ``release.json`` (tag, commit, tarball URL and
SHA-256) next to the tarball. It is attached only when that tag ships exactly
the plugin versions this latest.json lists; otherwise Cursor installs, which
use the tarball, could diverge from the marketplace that tracks main.

Usage: python3 scripts/attach_release.py _site/latest.json   (needs gh and GH_TOKEN)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*command: str) -> str | None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def versions(latest: dict) -> dict:
    return {name: item.get("version") for name, item in latest.get("plugins", {}).items()}


def main(argv: list[str]) -> int:
    target = Path(argv[1])
    latest = json.loads(target.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as temporary:
        if run("gh", "release", "download", "--pattern", "release.json", "--dir", temporary) is None:
            print("no release with release.json yet; latest.json published without a release", file=sys.stderr)
            return 0
        release = json.loads((Path(temporary) / "release.json").read_text(encoding="utf-8"))
    tagged = run("git", "show", f"{release['tag']}:latest.json")
    if tagged is None or versions(json.loads(tagged)) != versions(latest):
        print(f"{release['tag']} does not ship the versions on main; release not attached", file=sys.stderr)
        return 0
    latest["release"] = release
    target.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"attached {release['tag']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
