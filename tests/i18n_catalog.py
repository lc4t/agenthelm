#!/usr/bin/env python3
"""Keep every plugin's English catalog (``locale/en.json``) in one canonical form.

Canonical form: keys sorted by Unicode code point, one ``"source": "English"`` entry per line,
``json.dumps(..., ensure_ascii=False, indent=1)`` plus a final newline. Because the order is fixed,
two branches that add different keys touch different lines and merge without a conflict.

    python3 tests/i18n_catalog.py            # check: list catalogs that are not canonical, exit 1
    python3 tests/i18n_catalog.py --write    # rewrite them in place (also the way to resolve a merge conflict:
                                             # keep both sides' entries, fix the JSON syntax, run --write)

The loaded ``source -> English`` mapping never changes; only the order of entries in the file does.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOGS = (
    "plugins/agent-pack/runtime/locale/en.json",
    "plugins/agents-init/skills/agents-init/scripts/locale/en.json",
    "plugins/opinion-manager/runtime/locale/en.json",
    "plugins/project-orchestrator/runtime/locale/en.json",
)


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate key: {key!r}")
        result[key] = value
    return result


def parse(text: str) -> dict[str, str]:
    """The catalog as a mapping; rejects duplicate keys and anything that is not ``{str: str}``."""
    data = json.loads(text, object_pairs_hook=_pairs)
    if not isinstance(data, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in data.items()):
        raise ValueError("a catalog must be a JSON object mapping strings to strings")
    return data


def canonical(text: str) -> str:
    return json.dumps(dict(sorted(parse(text).items())), ensure_ascii=False, indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", action="store_true", help="rewrite catalogs that are not canonical")
    args = parser.parse_args(argv)
    status = 0
    for relative in CATALOGS:
        path = ROOT / relative
        text = path.read_text(encoding="utf-8")
        try:
            wanted = canonical(text)
        except ValueError as error:
            print(f"{relative}: {error}", file=sys.stderr)
            status = 1
            continue
        if text == wanted:
            continue
        if args.write:
            path.write_text(wanted, encoding="utf-8")
            print(f"rewrote {relative}")
        else:
            print(f"{relative}: not in canonical order; run: python3 tests/i18n_catalog.py --write", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
