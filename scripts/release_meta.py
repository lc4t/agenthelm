#!/usr/bin/env python3
"""Generate and verify the release metadata derived from each plugin.json.

Each plugin's own ``plugin.json`` is the only hand-written version. Everything
a client reads to decide "is there something newer" is derived from it:

* ``.claude-plugin/marketplace.json`` - read by Claude Code, Codex and Cursor.
  Claude Code decides updates from the entry ``version`` (a root plugin.json
  version is ignored once the entry has none), so the entry must be generated.
* ``latest.json`` - the cloud version source consumed by ``check``/``update``.

Usage::

    python3 scripts/release_meta.py           # verify; exit 1 on drift
    python3 scripts/release_meta.py --write   # regenerate the derived files

``latest.json`` deliberately carries no commit or tarball digest: those exist
only once a tag is cut, so the release step adds them.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE_NAME = "agenthelm"
MARKETPLACE_FILE = ".claude-plugin/marketplace.json"
LATEST_FILE = "latest.json"
MIGRATIONS_FILE = "release/migrations.json"
CATEGORY = "Developer Tools"
POLICY = {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}
BARE_PLUGIN_ROOT = re.compile(r"\$\{PLUGIN_ROOT\}")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def plugin_roots() -> list[Path]:
    """The root plugin first, then every plugins/<id>, in a stable order."""
    return [ROOT, *sorted(path.parent for path in (ROOT / "plugins").glob("*/plugin.json"))]


def source_of(root: Path) -> str:
    return "./" if root == ROOT else "./" + root.relative_to(ROOT).as_posix()


def load_plugins() -> list[dict]:
    plugins = []
    for root in plugin_roots():
        manifest = read_json(root / "plugin.json")
        plugins.append({"root": root, "manifest": manifest, "source": source_of(root)})
    return plugins


def build_marketplace(plugins: list[dict]) -> dict:
    root_manifest = plugins[0]["manifest"]
    entries = []
    for item in plugins:
        manifest = item["manifest"]
        entries.append({
            "name": manifest["name"],
            "source": item["source"],
            "description": manifest["description"],
            "version": manifest["version"],
            "author": manifest["author"],
            "repository": manifest["repository"],
            "keywords": manifest.get("keywords", []),
            "category": CATEGORY,
            "policy": POLICY,
        })
    return {
        "name": MARKETPLACE_NAME,
        "owner": root_manifest["author"],
        "metadata": {"description": root_manifest["description"]},
        "plugins": entries,
    }


def build_latest(plugins: list[dict]) -> dict:
    index = read_json(ROOT / "index.json")
    migrations_path = ROOT / MIGRATIONS_FILE
    migrations = read_json(migrations_path) if migrations_path.is_file() else []
    return {
        "schema": 1,
        "marketplace": MARKETPLACE_NAME,
        "repository": plugins[0]["manifest"]["repository"],
        "released_at": index["site"]["updated_at"],
        "plugins": {
            item["manifest"]["name"]: {
                "version": item["manifest"]["version"],
                "source": item["source"],
            }
            for item in plugins
        },
        "migrations": migrations,
    }


def consistency_problems(plugins: list[dict]) -> list[str]:
    """Drift between hand-written files that must agree with plugin.json."""
    problems: list[str] = []
    index = read_json(ROOT / "index.json")
    indexed = {item["id"]: item["version"] for item in index["skills"]}
    for item in plugins:
        name, version = item["manifest"]["name"], item["manifest"]["version"]
        root = item["root"]
        adapter = root / ".codex-plugin/plugin.json"
        if adapter.is_file() and read_json(adapter).get("version") != version:
            problems.append(f"{name}: .codex-plugin/plugin.json version differs from plugin.json")
        if indexed.get(name) != version:
            problems.append(f"{name}: index.json version differs from plugin.json")
        mcp = root / ".mcp.json"
        if mcp.is_file() and BARE_PLUGIN_ROOT.search(mcp.read_text(encoding="utf-8")):
            problems.append(
                f"{name}: .mcp.json uses ${{PLUGIN_ROOT}}, which Claude Code and Cursor do not "
                "expand; use ${CLAUDE_PLUGIN_ROOT}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", action="store_true", help="regenerate the derived files")
    args = parser.parse_args(argv)

    plugins = load_plugins()
    generated = {
        MARKETPLACE_FILE: dump(build_marketplace(plugins)),
        LATEST_FILE: dump(build_latest(plugins)),
    }
    problems = consistency_problems(plugins)
    for relative, content in generated.items():
        path = ROOT / relative
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        elif not path.is_file() or path.read_text(encoding="utf-8") != content:
            problems.append(f"{relative}: out of date; run scripts/release_meta.py --write")
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
