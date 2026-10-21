from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
