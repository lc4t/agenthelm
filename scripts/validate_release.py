#!/usr/bin/env python3
"""Check the Cursor release manifest and archive before publishing either artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tarfile
from pathlib import Path, PurePosixPath


def validate(latest: dict, release: dict, archive: Path, *, tag: str, commit: str, repository: str | None = None) -> None:
    """A release must identify this build and ship every advertised plugin version.

    ``repository`` is the ``owner/name`` the release is published from (``$GITHUB_REPOSITORY``); without it the
    URL is checked against ``latest.json``'s repository, which fails in forks and renamed repositories.
    """
    if release.get("tag") != tag or not re.fullmatch(r"v\d+\.\d+\.\d+(?:-rc\.\d+)?", tag):
        raise ValueError("release.tag must match the version tag being published")
    if release.get("commit") != commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("release.commit must match the commit being published")
    if repository is not None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("repository must be owner/name")
        base = f"https://github.com/{repository}"
    else:
        base = str(latest.get("repository", "")).removesuffix(".git").rstrip("/")
    expected_url = f"{base}/releases/download/{tag}/agenthelm-{tag}.tar.gz"
    if not base.startswith("https://github.com/") or release.get("tarball_url") != expected_url:
        raise ValueError("release.tarball_url must name this repository's tagged archive")
    digest = release.get("tarball_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("release.tarball_sha256 must be a SHA-256 digest")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError("release.tarball_sha256 does not match the archive")
    prefix = f"agenthelm-{tag}/"
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            path = PurePosixPath(member.name)
            if member.isdir() and member.name.rstrip("/") == prefix.rstrip("/"):
                continue
            if not member.name.startswith(prefix) or path.is_absolute() or ".." in path.parts:
                raise ValueError(f"archive contains an unexpected path: {member.name}")
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"archive contains an unsupported entry: {member.name}")
        for name, item in latest.get("plugins", {}).items():
            member = bundle.extractfile(f"{prefix}plugins/{name}/plugin.json")
            if member is None:
                raise ValueError(f"archive is missing {name}/plugin.json")
            manifest = json.load(member)
            if manifest.get("name") != name or manifest.get("version") != item.get("version"):
                raise ValueError(f"archive does not ship the advertised version of {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("latest", type=Path)
    parser.add_argument("release", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY") or None,
                        help="owner/name the release is published from (default: $GITHUB_REPOSITORY)")
    args = parser.parse_args()
    try:
        validate(json.loads(args.latest.read_text()), json.loads(args.release.read_text()), args.archive,
                 tag=args.tag, commit=args.commit, repository=args.repository)
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        parser.exit(1, f"invalid release artifacts: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
