"""Regression groups for the install / update / legacy paths of the three built-in clients (#134 batch C).

Written before the paths were moved onto the distribution declarations: they pin what Cursor's directory drop-in
(unpack into ``~/.cursor/plugins/local``) and the cleanup of Codex's residues (the pre-marketplace personal marketplace)
do today, so the move cannot change either. HOME is a temporary directory and every client command is a stub script.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_agenthelm_reporting as base  # noqa: E402
from test_agenthelm_reporting import write  # noqa: E402

SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
PLUGINS = ("agent-pack", "agents-init")
LATEST = {**base.LATEST, "plugins": {name: {"version": "2.0.0"} for name in PLUGINS},
          "release": {"tag": "v2.0.0", "tarball_sha256": "x"}}


class Fixture(base.Base):
    def setUp(self) -> None:
        super().setUp()
        base.write(self.latest_file, LATEST)
        os.environ["AGENTHELM_LANG"] = "zh"
        self.source = self.root / "source"
        for name in PLUGINS:
            write(self.source / "plugins" / name / "plugin.json", {"$schema": SCHEMA, "name": name, "version": "2.0.0"})
            write(self.source / "plugins" / name / "skills" / "s" / "SKILL.md", "new\n")

    def cursor_dir(self, plugin: str) -> Path:
        return self.home / ".cursor/plugins/local" / plugin


class CursorDirectoryDropinTests(Fixture):
    def test_install_preview_names_the_target_directory_and_writes_nothing(self) -> None:
        (self.home / ".cursor").mkdir()
        code, payload = self.run_json("install", "--client", "cursor", "--source", str(self.source))
        self.assertEqual(code, 0)
        step = payload["steps"][0]
        self.assertEqual((step["kind"], step["client"], step["status"]), ("install", "cursor", "preview"))
        self.assertEqual(step["description"],
                         "Cursor 安装 agent-pack、agents-init 到 ~/.cursor/plugins/local（重启 Cursor 生效）")
        self.assertNotIn("command", step)
        self.assertEqual([op["action"] for r in step["result"] for op in r["operations"]],
                         ["install-plugin", "install-plugin"])
        self.assertFalse(self.cursor_dir("agent-pack").exists())

    def test_install_apply_unpacks_every_plugin_and_reads_the_versions_back(self) -> None:
        (self.home / ".cursor").mkdir()
        code, payload = self.run_json("install", "--client", "cursor", "--source", str(self.source), "--apply")
        self.assertEqual(code, 0)
        step = payload["steps"][0]
        self.assertEqual(step["status"], "applied")
        self.assertEqual([(c["plugin"], c["before"], c["after"]) for c in step["changes"]],
                         [("agent-pack", None, "2.0.0"), ("agents-init", None, "2.0.0")])
        for plugin in PLUGINS:
            self.assertEqual(json.loads((self.cursor_dir(plugin) / "plugin.json").read_text())["version"], "2.0.0")
            self.assertEqual((self.cursor_dir(plugin) / "skills/s/SKILL.md").read_text(), "new\n")
        self.assertEqual(self.called(), [])  # no client command is involved

    def test_update_replaces_a_changed_directory_and_backs_the_old_one_up(self) -> None:
        self.cursor_plugin("1.0.0")
        write(self.cursor_dir("agent-pack") / "skills/s/SKILL.md", "old\n")
        recovery = self.root / "recovery"
        code, payload = self.run_json("update", "--no-project", "--only", "plugins", "--source", str(self.source),
                                      "--recovery-dir", str(recovery), "--apply")
        self.assertEqual(code, 0, payload)
        step = [s for s in payload["steps"] if s["client"] == "cursor"][0]
        self.assertEqual(step["status"], "applied")
        self.assertEqual((step["before"], step["after"]), ("1.0.0", "2.0.0"))
        self.assertEqual(json.loads((self.cursor_dir("agent-pack") / "plugin.json").read_text())["version"], "2.0.0")
        moved = sorted(recovery.glob("*-cursor-agent-pack/replaced-plugin/agent-pack/skills/s/SKILL.md"))
        self.assertEqual([p.read_text() for p in moved], ["old\n"])

    def test_update_preview_plans_the_backup_and_the_install_note_says_what_is_overwritten(self) -> None:
        self.cursor_plugin("1.0.0")
        _c, payload = self.run_json("install", "--client", "cursor", "--source", str(self.source))
        step = payload["steps"][0]
        self.assertEqual(step["note"], "已安装 agent-pack 1.0.0；会用发布包里的版本覆盖它们")
        _c, payload = self.run_json("update", "--no-project", "--only", "plugins", "--source", str(self.source))
        step = [s for s in payload["steps"] if s["client"] == "cursor"][0]
        self.assertEqual(step["description"], "Cursor 更新 agent-pack 到 ~/.cursor/plugins/local（重启 Cursor 生效）")
        backup = step["result"][0]["backup"]
        self.assertTrue(backup["planned"])
        self.assertTrue(backup["path"].endswith("-cursor-agent-pack/replaced-plugin/agent-pack"))
        self.assertEqual(json.loads((self.cursor_dir("agent-pack") / "plugin.json").read_text())["version"], "1.0.0")

    def test_a_symlinked_working_copy_is_left_alone(self) -> None:
        work = self.root / "work" / "agent-pack"
        write(work / "plugin.json", {"name": "agent-pack", "version": "1.0.0"})
        (self.home / ".cursor/plugins/local").mkdir(parents=True)
        self.cursor_dir("agent-pack").symlink_to(work)
        _c, payload = self.run_json("update", "--no-project", "--only", "plugins", "--source", str(self.source))
        steps = [s for s in payload["steps"] if s["client"] == "cursor"]
        self.assertTrue(steps and all(s.get("untouched") for s in steps), steps)
        self.assertTrue(self.cursor_dir("agent-pack").is_symlink())

    def test_without_a_release_the_plugin_is_unavailable_with_the_next_step(self) -> None:
        self.cursor_plugin("1.0.0")
        base.write(self.latest_file, {**LATEST, "release": {"tag": "v2.0.0"}})
        code, payload = self.run_json("update", "--no-project", "--only", "plugins", "--apply")
        step = [s for s in payload["steps"] if s["client"] == "cursor"][0]
        self.assertEqual(step["status"], "unavailable")
        self.assertIn("--source", step["error"])
        self.assertIn("config ignore-client cursor --apply", step["next"])
        _c, check = self.run_json("check", "--no-project", "--client", "cursor")
        self.assertTrue(check["plugins"][0]["unavailable"])
        self.assertEqual(check["plugins"][0]["status"], "behind")


class CodexResidueCleanupTests(Fixture):
    def residues(self) -> None:
        write(self.home / ".agents/plugins/plugins/agent-pack/plugin.json", {"name": "agent-pack", "version": "0.9.0"})
        write(self.home / ".agents/plugins/marketplace.json", {"name": "personal", "plugins": [
            {"name": "agent-pack", "source": {"path": "x"}}, {"name": "other", "source": {"path": "y"}}]})
        write(self.home / ".codex/config.toml",
              'model = "m"\n\n[plugins."agent-pack@personal"]\nenabled = true\n\n[other]\na = 1\n')
        write(self.home / ".codex/plugins/cache/personal/agent-pack/0.9.0/plugin.json",
              {"name": "agent-pack", "version": "0.9.0"})

    def test_preview_lists_every_residue_and_changes_nothing(self) -> None:
        self.residues()
        before = (self.home / ".codex/config.toml").read_text()
        code, payload = self.run_json("migrate")
        self.assertEqual(code, 0)
        kinds = sorted(item["kind"] for item in payload["legacy"])
        self.assertEqual(kinds, ["codex-config-section", "codex-personal-cache", "codex-personal-entry", "plugin-copy"])
        self.assertEqual((self.home / ".codex/config.toml").read_text(), before)
        self.assertTrue((self.home / ".codex/plugins/cache/personal/agent-pack").is_dir())

    def test_apply_removes_the_residues_keeps_the_rest_and_records_a_manifest(self) -> None:
        self.residues()
        code, payload = self.run_json("migrate", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(item["status"] for item in payload["retired"]), ["edited", "edited", "moved", "moved"])
        self.assertFalse((self.home / ".codex/plugins/cache/personal/agent-pack").exists())
        self.assertFalse((self.home / ".agents/plugins/plugins/agent-pack").exists())
        self.assertEqual((self.home / ".codex/config.toml").read_text(), 'model = "m"\n\n[other]\na = 1\n')
        market = json.loads((self.home / ".agents/plugins/marketplace.json").read_text())
        self.assertEqual([e["name"] for e in market["plugins"]], ["other"])
        folder = sorted((self.home / ".agent-pack/recovery").glob("*-agenthelm-migrate"))
        self.assertEqual(len(folder), 1)
        manifest = json.loads((folder[0] / "migration.json").read_text())
        self.assertEqual(len(manifest["artifacts"]), 4)
        self.assertIn('[plugins."agent-pack@personal"]',
                      (folder[0] / "files" / ".codex/config.toml").read_text())

    def test_a_marketplace_install_next_to_the_residues_is_reported_as_a_duplicate(self) -> None:
        self.residues()
        self.codex_plugin("2.0.0")
        _c, payload = self.run_json("doctor", "--no-project")
        self.assertIn("duplicate", [i["issue"] for i in payload["issues"]])

    def test_the_legacy_entry_belongs_to_codex_by_its_recorded_path(self) -> None:
        self.residues()
        self.stub("codex")
        _c, payload = self.run_json("check", "--no-project", "--client", "codex")
        legacy = [p for p in payload["plugins"] if p["client"] == "legacy"]
        self.assertEqual([p["owner"] for p in legacy], ["codex"])
        _c, payload = self.run_json("check", "--no-project", "--client", "claude")
        legacy = [p for p in payload["plugins"] if p["client"] == "legacy"]
        self.assertEqual([p["owner"] for p in legacy], ["codex"])


if __name__ == "__main__":
    unittest.main()
