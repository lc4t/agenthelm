#!/usr/bin/env python3
"""Generate and verify the release metadata derived from each plugin.json.

Each plugin's own ``plugin.json`` is the only hand-written version, and each
content pack declared in ``content.json`` carries its own version. Everything a
client reads to decide "is there something newer" is derived from them:

* ``.claude-plugin/marketplace.json`` - read by Claude Code, Codex and Cursor.
  Claude Code decides updates from the entry ``version`` alone, so the entry
  must be generated.
* ``latest.json`` - the cloud version source consumed by ``check``/``update``.
* the ``files`` digests in every ``content.json``.

Usage::

    python3 scripts/release_meta.py                 # verify; exit 1 on drift
    python3 scripts/release_meta.py --write         # regenerate the derived files
    python3 scripts/release_meta.py --since v6.4.0  # also gate against a release

``--since`` fails when a plugin or a content pack changed without a version
bump, and when a bumped plugin has no matching ``release/migrations.json``
entry. ``latest.json`` carries no commit or tarball digest: those exist only
once a tag is cut, so the release step adds them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE_NAME = "agenthelm"
MARKETPLACE_FILE = ".claude-plugin/marketplace.json"
LATEST_FILE = "latest.json"
MIGRATIONS_FILE = "release/migrations.json"
CONTENT_FILE = "content.json"
CLI_PACKAGE = "packages/cli/package.json"
CATEGORY = "Developer Tools"
POLICY = {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}
OWNER = {"name": "lc4t"}
DESCRIPTION = "跨 Agent 的项目管理插件集：项目契约、生命周期、扩展能力与行为规则。"
# Installing agents-init alone must bring its companions (Claude Code installs
# marketplace-entry dependencies; other clients ignore the field).
DEPENDENCIES = {"agents-init": ["project-orchestrator", "agent-pack", "opinion-manager"]}
BARE_PLUGIN_ROOT = re.compile(r"\$\{PLUGIN_ROOT\}")
CHANGE_KINDS = {"code", "content", "both"}
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Files that ship in a plugin directory but never reach users' behaviour.
UNSHIPPED_PARTS = {"tests", "__pycache__"}
IGNORED_NAMES = {".DS_Store"}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def plugin_roots() -> list[Path]:
    return sorted(path.parent for path in (ROOT / "plugins").glob("*/plugin.json"))


def source_of(root: Path) -> str:
    return "./" + root.relative_to(ROOT).as_posix()


def load_plugins() -> list[dict]:
    plugins = []
    for root in plugin_roots():
        content_path = root / CONTENT_FILE
        plugins.append({
            "root": root,
            "manifest": read_json(root / "plugin.json"),
            "source": source_of(root),
            "content": read_json(content_path) if content_path.is_file() else None,
        })
    return plugins


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pack_files(root: Path, sources: list[str]) -> dict[str, str]:
    files: dict[str, str] = {}
    for source in sources:
        target = root / source
        candidates = [target] if target.is_file() else sorted(target.rglob("*"))
        for path in candidates:
            if not path.is_file() or path.name in IGNORED_NAMES or path.suffix == ".pyc":
                continue
            if UNSHIPPED_PARTS & set(path.relative_to(root).parts):
                continue
            files[path.relative_to(root).as_posix()] = sha256_file(path)
    return dict(sorted(files.items()))


def refreshed_content(item: dict) -> dict | None:
    """The plugin's content.json with every pack's file digests recomputed."""
    content = item["content"]
    if content is None:
        return None
    packs = [{**pack, "files": pack_files(item["root"], pack["sources"])} for pack in content["packs"]]
    return {**content, "packs": packs}


def build_marketplace(plugins: list[dict]) -> dict:
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
            "license": manifest.get("license"),
            "keywords": manifest.get("keywords", []),
            "category": CATEGORY,
            "policy": POLICY,
            **({"dependencies": DEPENDENCIES[manifest["name"]]} if manifest["name"] in DEPENDENCIES else {}),
        })
    return {
        "name": MARKETPLACE_NAME,
        "owner": OWNER,
        "metadata": {"description": DESCRIPTION},
        "plugins": entries,
    }


def read_migrations() -> list[dict]:
    path = ROOT / MIGRATIONS_FILE
    return read_json(path) if path.is_file() else []


def build_latest(plugins: list[dict]) -> dict:
    index = read_json(ROOT / "index.json")
    entries = {}
    for item in plugins:
        entry = {"version": item["manifest"]["version"], "source": item["source"]}
        if item["content"] is not None:
            entry["content"] = {
                pack["id"]: {"version": pack["version"], "kind": pack["kind"]}
                for pack in item["content"]["packs"]
            }
        entries[item["manifest"]["name"]] = entry
    return {
        "schema": 1,
        "marketplace": MARKETPLACE_NAME,
        "repository": plugins[0]["manifest"]["repository"],
        "released_at": index["site"]["updated_at"],
        "plugins": entries,
        "migrations": read_migrations(),
    }


def migration_problems(plugins: list[dict]) -> list[str]:
    names = {item["manifest"]["name"] for item in plugins}
    problems = []
    for position, entry in enumerate(read_migrations()):
        label = f"{MIGRATIONS_FILE}[{position}]"
        if entry.get("plugin") not in names:
            problems.append(f"{label}: unknown plugin {entry.get('plugin')!r}")
        for key in ("from", "to"):
            if not VERSION_RE.fullmatch(str(entry.get(key, ""))):
                problems.append(f"{label}: {key} must be MAJOR.MINOR.PATCH")
        if entry.get("change_kind") not in CHANGE_KINDS:
            problems.append(f"{label}: change_kind must be one of {sorted(CHANGE_KINDS)}")
        if not isinstance(entry.get("breaking"), bool):
            problems.append(f"{label}: breaking must be true or false")
        if not DATE_RE.fullmatch(str(entry.get("date", ""))):
            problems.append(f"{label}: date must be YYYY-MM-DD")
        if not str(entry.get("summary", "")).strip():
            problems.append(f"{label}: summary is required")
    return problems


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
        for pack in (item["content"] or {}).get("packs", []):
            if not pack["id"].startswith(f"{name}/"):
                problems.append(f"{name}: content pack {pack['id']} must be named {name}/<pack>")
            if not VERSION_RE.fullmatch(pack["version"]):
                problems.append(f"{pack['id']}: version must be MAJOR.MINOR.PATCH")
    # One number per release: the git tag, the site and the npm CLI all carry it.
    # It moves on its own; plugins keep independent versions.
    suite = index["plugin"]["version"]
    if not VERSION_RE.fullmatch(suite):
        problems.append("index.json plugin.version (the release version) must be MAJOR.MINOR.PATCH")
    cli = read_json(ROOT / CLI_PACKAGE)["version"]
    if cli != suite:
        problems.append(f"{CLI_PACKAGE} version {cli} must equal the release version {suite}")
    return problems + migration_problems(plugins)


def git(*args: str) -> str | None:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def json_at(ref: str, relative: str) -> dict | None:
    text = git("show", f"{ref}:{relative}")
    return json.loads(text) if text is not None else None


def shipped_changes(ref: str, root: Path) -> list[str]:
    relative = root.relative_to(ROOT).as_posix()
    changed = (git("diff", "--name-only", ref, "--", relative) or "").splitlines()
    untracked = (git("ls-files", "--others", "--exclude-standard", "--", relative) or "").splitlines()
    return sorted(
        path for path in {*changed, *untracked}
        if not UNSHIPPED_PARTS & set(Path(path).parts) and Path(path).name not in IGNORED_NAMES
    )


def since_problems(ref: str, plugins: list[dict]) -> list[str]:
    if git("rev-parse", "--verify", f"{ref}^{{commit}}") is None:
        return [f"--since {ref}: not a git revision"]
    problems: list[str] = []
    migrations = {(entry.get("plugin"), entry.get("from"), entry.get("to")) for entry in read_migrations()}
    for item in plugins:
        name, version = item["manifest"]["name"], item["manifest"]["version"]
        relative = item["root"].relative_to(ROOT).as_posix()
        previous = json_at(ref, f"{relative}/plugin.json")
        if previous is None:
            # agents-init lived at the repository root before it moved under plugins/.
            legacy = json_at(ref, "plugin.json")
            previous = legacy if legacy and legacy.get("name") == name else None
        if previous is None:
            continue  # a new plugin; nothing to compare against
        if previous["version"] == version:
            changed = shipped_changes(ref, item["root"])
            if changed:
                problems.append(
                    f"{name}: shipped files changed since {ref} without a version bump: "
                    + ", ".join(changed[:5]) + (" ..." if len(changed) > 5 else "")
                )
        elif (name, previous["version"], version) not in migrations:
            problems.append(
                f"{name}: {previous['version']} -> {version} has no {MIGRATIONS_FILE} entry"
            )
        old_packs = {pack["id"]: pack for pack in (json_at(ref, f"{relative}/{CONTENT_FILE}") or {}).get("packs", [])}
        for pack in (refreshed_content(item) or {}).get("packs", []):
            old = old_packs.get(pack["id"])
            if old and old["version"] == pack["version"] and old["files"] != pack["files"]:
                problems.append(f"{pack['id']}: content changed since {ref} without a pack version bump")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", action="store_true", help="regenerate the derived files")
    parser.add_argument("--since", metavar="REF", help="also gate version bumps against a release ref")
    args = parser.parse_args(argv)

    plugins = load_plugins()
    generated = {}
    for item in plugins:
        content = refreshed_content(item)
        if content is not None:
            generated[(item["root"] / CONTENT_FILE).relative_to(ROOT).as_posix()] = dump(content)
            item["content"] = content
    generated[MARKETPLACE_FILE] = dump(build_marketplace(plugins))
    generated[LATEST_FILE] = dump(build_latest(plugins))
    problems = consistency_problems(plugins)
    for relative, content in generated.items():
        path = ROOT / relative
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        elif not path.is_file() or path.read_text(encoding="utf-8") != content:
            problems.append(f"{relative}: out of date; run scripts/release_meta.py --write")
    if args.since:
        problems += since_problems(args.since, plugins)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
