from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_meta", ROOT / "scripts/release_meta.py")
release_meta = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_meta)


class ReleaseMetaTests(unittest.TestCase):
    def test_derived_files_are_in_sync(self) -> None:
        self.assertEqual(release_meta.main([]), 0)

    def test_marketplace_versions_come_from_each_plugin_manifest(self) -> None:
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(marketplace["name"], "agenthelm")
        entries = {entry["name"]: entry for entry in marketplace["plugins"]}
        roots = sorted(path.parent for path in (ROOT / "plugins").glob("*/plugin.json"))
        self.assertEqual(len(entries), len(roots))
        for root in roots:
            manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
            # Claude Code decides updates from this entry version only, so it must never be blank.
            self.assertEqual(entries[manifest["name"]]["version"], manifest["version"])

    def test_marketplace_uses_sources_every_client_understands(self) -> None:
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        for entry in marketplace["plugins"]:
            # Codex silently skips {"source": "github"} entries; relative paths work everywhere.
            self.assertIsInstance(entry["source"], str)
            self.assertTrue(entry["source"].startswith("./"))

    def test_latest_json_lists_every_plugin_version(self) -> None:
        latest = json.loads((ROOT / "latest.json").read_text(encoding="utf-8"))
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {name: item["version"] for name, item in latest["plugins"].items()},
            {entry["name"]: entry["version"] for entry in marketplace["plugins"]},
        )

    def test_hand_written_claude_mcp_config_expands_in_claude_code_and_cursor(self) -> None:
        for path in (ROOT / "plugins").glob("*/.mcp.json"):
            self.assertNotIn("${PLUGIN_ROOT}", path.read_text(encoding="utf-8"), path.as_posix())

    def test_content_packs_are_listed_in_latest_json(self) -> None:
        latest = json.loads((ROOT / "latest.json").read_text(encoding="utf-8"))
        for path in (ROOT / "plugins").glob("*/content.json"):
            content = json.loads(path.read_text(encoding="utf-8"))
            plugin = path.parent.name
            for pack in content["packs"]:
                self.assertTrue(pack["files"], pack["id"])
                self.assertEqual(latest["plugins"][plugin]["content"][pack["id"]]["version"], pack["version"])

    def test_migration_entries_are_validated(self) -> None:
        bad = [{"plugin": "nope", "from": "1", "to": "1.0.0", "change_kind": "docs", "breaking": "no", "date": "x"}]
        with mock.patch.object(release_meta, "read_migrations", return_value=bad):
            problems = release_meta.migration_problems(release_meta.load_plugins())
        for expected in ("unknown plugin", "from must be", "change_kind", "breaking", "date", "summary"):
            self.assertTrue(any(expected in problem for problem in problems), expected)


class SinceGateTests(unittest.TestCase):
    """--since compares a work tree with a release tag in a throwaway repository."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.git("init", "-q")
        self.write("plugins/demo/plugin.json", {"name": "demo", "version": "1.0.0"})
        self.write("plugins/demo/templates/a.md", "one\n")
        self.write("plugins/demo/tests/test_demo.py", "")
        # A released content.json always carries the digests --write computed.
        self.write("plugins/demo/content.json", {"schema_version": 1, "packs": [{
            "id": "demo/templates", "version": "1.0.0", "kind": "template", "sources": ["templates"],
            "materializes": [], "project_lock": "agenthelm",
            "files": release_meta.pack_files(self.root / "plugins/demo", ["templates"])}]})
        self.patch = mock.patch.object(release_meta, "ROOT", self.root)
        self.patch.start()
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1")
        self.git("tag", "v1")

    def tearDown(self) -> None:
        self.patch.stop()
        self.temporary.cleanup()

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def write(self, relative: str, data) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def problems(self) -> list[str]:
        return release_meta.since_problems("v1", release_meta.load_plugins())

    def test_tests_only_change_needs_no_bump(self) -> None:
        self.write("plugins/demo/tests/test_demo.py", "# more\n")
        self.assertEqual(self.problems(), [])

    def test_shipped_change_without_bump_fails(self) -> None:
        self.write("plugins/demo/templates/a.md", "two\n")
        problems = self.problems()
        self.assertTrue(any("without a version bump" in problem for problem in problems))
        self.assertTrue(any("demo/templates" in problem for problem in problems))

    def test_bump_requires_a_migration_entry(self) -> None:
        self.write("plugins/demo/templates/a.md", "two\n")
        self.write("plugins/demo/plugin.json", {"name": "demo", "version": "1.0.1"})
        content = json.loads((self.root / "plugins/demo/content.json").read_text())
        content["packs"][0]["version"] = "1.0.1"
        self.write("plugins/demo/content.json", content)
        self.assertTrue(any("has no release/migrations.json entry" in p for p in self.problems()))
        self.write("release/migrations.json", [{"plugin": "demo", "from": "1.0.0", "to": "1.0.1",
                                                "change_kind": "content", "breaking": False,
                                                "date": "2026-10-21", "summary": "template wording"}])
        self.assertEqual(self.problems(), [])


if __name__ == "__main__":
    unittest.main()
