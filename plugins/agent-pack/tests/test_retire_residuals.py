"""#75: retiring a plugin must remove every entry the client keeps for it, or list what is left."""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "runtime"))

from test_agent_pack_config import make_project, write_json  # noqa: E402
from test_sync_preview_parity import CLIENTS, SyncPreviewParityTests, snapshot  # noqa: E402
from agent_pack_config import AGENT_PLUGIN_SCHEMA  # noqa: E402

RETIRED = "demo-plug"
OTHER = "other-plug"


def make_plugin(root: Path, name: str, skill: str) -> Path:
    write_json(root / "plugin.json", {"$schema": AGENT_PLUGIN_SCHEMA, "name": name, "version": "1.0.0",
                                      "description": "retire fixture"})
    (root / "skills" / skill).mkdir(parents=True)
    (root / "skills" / skill / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: retire fixture skill.\n---\n\n# {skill}\n", encoding="utf-8")
    return root


class RetireResidualTests(SyncPreviewParityTests):
    # Only the helpers are reused; the inherited parity cases already run in their own class.
    test_preview_equals_apply_for_every_client = None  # type: ignore[assignment]
    test_preview_still_blocks_when_package_is_already_installed = None  # type: ignore[assignment]
    test_doctor_reports_non_portable_mcp_as_one_line_error = None  # type: ignore[assignment]
    test_text_output_is_one_error_line_without_stack = None  # type: ignore[assignment]

    def fixture(self, client: str):
        base = self.root / client
        project = make_project(base / "project", "proj")
        home = base / "home"
        home.mkdir()
        recovery = base / "recovery"
        retired = make_plugin(base / "src" / RETIRED, RETIRED, "demo-skill")
        other = make_plugin(base / "src" / OTHER, OTHER, "other-skill")
        for plugin in (retired, other):
            rc, payload, out, err = self.cli(home, "bootstrap", "--plugin", str(plugin), "--client", client, "--apply")
            self.assertEqual(rc, 0, out + err)
        if client == "codex":
            # What `codex plugin install` leaves besides the personal copy: a cache directory.
            cache = home / ".codex" / "plugins" / "cache" / "personal" / RETIRED / "1.0.0"
            shutil.copytree(retired, cache)
            (home / ".codex" / "config.toml").write_text(f'[plugins."{RETIRED}@personal"]\nenabled = true\n',
                                                         encoding="utf-8")
        return project, home, recovery, retired

    def retire(self, project, home, recovery, client, *extra):
        return self.cli(home, "reconcile", "--project", str(project), "--client", client,
                        "--retire-plugin", RETIRED, "--retire-only", "--recovery-root", str(recovery), *extra)

    def entries(self, home: Path) -> dict:
        found = {}
        marketplace = home / ".agents" / "plugins" / "marketplace.json"
        if marketplace.is_file():
            found["marketplace"] = [item["name"] for item in json.loads(marketplace.read_text())["plugins"]]
        skills = home / ".claude" / "skills"
        if skills.is_dir():
            found["skills"] = sorted(path.name for path in skills.iterdir())
        return found

    # ------------------------------------------------------------------ claude

    def test_claude_skill_link_into_the_retired_plugin_is_removed(self) -> None:
        project, home, recovery, retired = self.fixture("claude")
        link = home / ".claude" / "skills" / "demo-skill"
        self.assertTrue(link.is_symlink())
        (home / ".claude" / "skills" / "hand-made").mkdir()          # the user's own Skill
        (home / ".claude" / "skills" / "hand-made" / "SKILL.md").write_text("x", encoding="utf-8")
        elsewhere = self.root / "elsewhere" / "stray"
        elsewhere.mkdir(parents=True)
        (home / ".claude" / "skills" / "stray").symlink_to(elsewhere, target_is_directory=True)
        before = self.entries(home)
        rc, preview, out, err = self.retire(project, home, recovery, "claude")
        self.assertEqual(rc, 0, out + err)
        listed = {(op["action"], op["name"]) for op in preview["operations"]}
        self.assertIn(("archive-skill", "demo-skill"), listed)
        self.assertNotIn(("archive-skill", "other-skill"), listed)
        self.assertNotIn(("archive-skill", "hand-made"), listed)
        self.assertNotIn(("archive-skill", "stray"), listed)
        self.assertTrue(link.is_symlink(), "preview writes nothing")
        rc, applied, out, err = self.retire(project, home, recovery, "claude", "--apply")
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(link.is_symlink() or link.exists())
        remaining = self.entries(home)["skills"]
        self.assertEqual(sorted(remaining), sorted(set(before["skills"]) - {"demo-skill"}))
        self.assertIn("other-skill", remaining)
        archived = list(Path(applied["recovery"]).rglob("demo-skill"))
        self.assertTrue(archived, "the link is archived, not just deleted")

    def test_claude_project_package_links_are_never_touched(self) -> None:
        project, home, recovery, _ = self.fixture("claude")
        rc, _p, out, err = self.cli(home, "sync", "--project", str(project), "--client", "claude", "--apply")
        self.assertEqual(rc, 0, out + err)
        skills = home / ".claude" / "skills"
        self.assertTrue((skills / "sample-skill").is_symlink())
        rc, preview, out, err = self.retire(project, home, recovery, "claude", "--apply")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue((skills / "sample-skill").is_symlink())
        self.assertTrue((skills / "other-skill").is_symlink())

    def test_claude_mcp_entries_of_the_retired_plugin_are_listed_not_edited(self) -> None:
        base = self.root / "claude-mcp"
        project = make_project(base / "project", "proj")
        home = base / "home"
        home.mkdir()
        plugin = make_plugin(base / "src" / RETIRED, RETIRED, "demo-skill")
        write_json(plugin / "mcp.json", {"mcpServers": {"demo-server": {
            "type": "stdio", "command": "python3", "args": ["${PLUGIN_ROOT}/server.py"]}}})
        rc, _p, out, err = self.cli(home, "bootstrap", "--plugin", str(plugin), "--client", "claude", "--apply")
        self.assertEqual(rc, 0, out + err)
        config = (home / ".claude.json").read_text()
        rc, payload, out, err = self.retire(project, home, base / "recovery", "claude", "--apply")
        self.assertEqual(rc, 0, out + err)
        kinds = [(item["kind"], item["name"]) for item in payload["residuals"]]
        self.assertEqual(kinds, [("mcp", "demo-server")])
        self.assertIn("claude mcp remove", payload["residuals"][0]["manual"])
        self.assertEqual((home / ".claude.json").read_text(), config, "the user config is not edited")

    def claude_mcp_fixture(self, name: str, servers: dict):
        base = self.root / name
        project = make_project(base / "project", "proj")
        home = base / "home"
        home.mkdir()
        plugin = make_plugin(base / "src" / RETIRED, RETIRED, "demo-skill")
        write_json(plugin / "mcp.json", {"mcpServers": servers})
        rc, _p, out, err = self.cli(home, "bootstrap", "--plugin", str(plugin), "--client", "claude", "--apply")
        self.assertEqual(rc, 0, out + err)
        return project, home, base / "recovery"

    PLAIN = {"type": "stdio", "command": "python3", "args": ["-m", "checker"]}

    def test_claude_mcp_without_plugin_path_is_listed_when_identical_to_what_bootstrap_wrote(self) -> None:
        # #96: the entry has no path into the plugin and no ownership record, only its content gives it away.
        project, home, recovery = self.claude_mcp_fixture("claude-mcp-plain", {
            "local-check": self.PLAIN,
            "demo-server": {"type": "stdio", "command": "python3", "args": ["${PLUGIN_ROOT}/server.py"]}})
        config = (home / ".claude.json").read_text()
        for extra in ((), ("--apply",)):
            with self.subTest(apply=bool(extra)):
                rc, payload, out, err = self.retire(project, home, recovery, "claude", *extra)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(sorted((i["kind"], i["name"]) for i in payload["residuals"]),
                                 [("mcp", "demo-server"), ("mcp", "local-check")], "each entry listed once")
                item = next(i for i in payload["residuals"] if i["name"] == "local-check")
                self.assertEqual(Path(item["path"]).resolve(), (home / ".claude.json").resolve())
                self.assertIn("claude mcp remove --scope user local-check", item["manual"])
                self.assertEqual((home / ".claude.json").read_text(), config, "the user config is not edited")

    def test_claude_mcp_changed_by_the_user_is_not_claimed_by_the_retired_plugin(self) -> None:
        project, home, recovery = self.claude_mcp_fixture("claude-mcp-edited", {"local-check": self.PLAIN})
        path = home / ".claude.json"
        data = json.loads(path.read_text())
        data["mcpServers"]["local-check"]["args"] = ["-m", "my-own-checker"]
        write_json(path, data)
        for extra in ((), ("--apply",)):
            with self.subTest(apply=bool(extra)):
                rc, payload, out, err = self.retire(project, home, recovery, "claude", *extra)
                self.assertEqual(rc, 0, out + err)
                self.assertNotIn("local-check", [i["name"] for i in payload.get("residuals", [])])
                self.assertEqual(json.loads(path.read_text()), data)

    def test_claude_retiring_a_plugin_without_mcp_has_no_mcp_residuals(self) -> None:
        project, home, recovery, _ = self.fixture("claude")
        write_json(home / ".claude.json", {"mcpServers": {"mine": self.PLAIN}})
        rc, payload, out, err = self.retire(project, home, recovery, "claude", "--apply")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([i for i in payload.get("residuals", []) if i["kind"] == "mcp"], [])

    def test_claude_text_output_lists_the_mcp_residual(self) -> None:
        import subprocess
        project, home, _ = self.claude_mcp_fixture("claude-mcp-text", {"local-check": self.PLAIN})
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["HOME"] = str(home)
        done = subprocess.run(
            [sys.executable, str(TESTS.parent / "runtime" / "agent_pack_config.py"), "reconcile",
             "--project", str(project), "--client", "claude", "--retire-plugin", RETIRED, "--retire-only",
             "--home", str(home)], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("! residual mcp: local-check", done.stdout)

    # ------------------------------------------------------------------ codex

    def test_codex_marketplace_entry_and_copy_are_removed(self) -> None:
        project, home, recovery, _ = self.fixture("codex")
        market = home / ".agents" / "plugins" / "marketplace.json"
        original = json.loads(market.read_text())
        self.assertEqual(sorted(item["name"] for item in original["plugins"]), [RETIRED, OTHER])
        copy = home / ".agents" / "plugins" / "plugins" / RETIRED
        self.assertTrue(copy.is_dir())
        rc, preview, out, err = self.retire(project, home, recovery, "codex")
        self.assertEqual(rc, 0, out + err)
        actions = [op["action"] for op in preview["operations"]]
        self.assertIn("remove-marketplace-entry", actions)
        self.assertTrue(copy.is_dir() and RETIRED in market.read_text(), "preview writes nothing")
        rc, applied, out, err = self.retire(project, home, recovery, "codex", "--apply")
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(copy.exists())
        after = json.loads(market.read_text())
        self.assertEqual([item["name"] for item in after["plugins"]], [OTHER])
        self.assertEqual(after["plugins"][0], next(i for i in original["plugins"] if i["name"] == OTHER))
        self.assertEqual({k: v for k, v in after.items() if k != "plugins"},
                         {k: v for k, v in original.items() if k != "plugins"})
        self.assertTrue((home / ".agents" / "plugins" / "plugins" / OTHER).is_dir())
        recovered = Path(applied["recovery"])
        self.assertTrue(any(recovered.rglob("marketplace.json")), "the original entry is recoverable")
        self.assertTrue(any(recovered.rglob("plugin.json")), "the plugin copy is archived")

    def test_codex_unrelated_marketplace_formatting_is_preserved(self) -> None:
        project, home, recovery, _ = self.fixture("codex")
        market = home / ".agents" / "plugins" / "marketplace.json"
        data = json.loads(market.read_text())
        market.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
        rc, _p, out, err = self.retire(project, home, recovery, "codex", "--apply")
        self.assertEqual(rc, 0, out + err)
        text = market.read_text()
        self.assertIn('\n    "name"', text)
        self.assertTrue(text.endswith("\n"))

    def test_codex_residuals_are_listed_with_a_manual_step(self) -> None:
        project, home, recovery, _ = self.fixture("codex")
        for flags in ((), ("--apply",)):
            rc, payload, out, err = self.retire(project, home, recovery, "codex", *flags)
            self.assertEqual(rc, 0, out + err)
            residuals = payload["residuals"]
            self.assertTrue(any("config.toml" in item["path"] for item in residuals), residuals)
            for item in residuals:
                self.assertTrue(item["manual"] and item["path"], item)
            self.assertNotIn("enabled", json.dumps(residuals), "file contents are never echoed")

    def test_unrelated_plugin_marketplace_entry_with_similar_name_stays(self) -> None:
        project, home, recovery, _ = self.fixture("codex")
        market = home / ".agents" / "plugins" / "marketplace.json"
        data = json.loads(market.read_text())
        data["plugins"].append({"name": RETIRED + "-extra", "source": {"source": "local", "path": "./x"}})
        write_json(market, data)
        rc, _p, out, err = self.retire(project, home, recovery, "codex", "--apply")
        self.assertEqual(rc, 0, out + err)
        names = [item["name"] for item in json.loads(market.read_text())["plugins"]]
        self.assertEqual(sorted(names), sorted([OTHER, RETIRED + "-extra"]))

    # ------------------------------------------------------------------ all clients

    def test_every_client_preview_is_zero_write_and_matches_apply(self) -> None:
        for client in CLIENTS:
            with self.subTest(client=client):
                project, home, recovery, _ = self.fixture(client)
                before = snapshot(home)
                rc, preview, out, err = self.retire(project, home, recovery, client)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(snapshot(home), before)
                self.assertFalse(recovery.exists())
                rc, applied, out, err = self.retire(project, home, recovery, client, "--apply")
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(preview["operations"], applied["operations"])
                self.assertEqual(preview.get("residuals"), applied.get("residuals"))

    def test_nothing_of_the_retired_plugin_is_left_for_any_client(self) -> None:
        for client in CLIENTS:
            with self.subTest(client=client):
                project, home, recovery, _ = self.fixture(client)
                rc, applied, out, err = self.retire(project, home, recovery, client, "--apply")
                self.assertEqual(rc, 0, out + err)
                leftovers = [str(path) for path in home.rglob("*")
                             if RETIRED in path.name or path.name == "demo-skill"]
                leftovers = [p for p in leftovers if ".codex/plugins/cache/personal/demo-plug" not in p]
                self.assertEqual(leftovers, [], applied)

    def test_text_output_lists_residuals(self) -> None:
        import subprocess
        project, home, recovery, _ = self.fixture("codex")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["HOME"] = str(home)
        done = subprocess.run(
            [sys.executable, str(TESTS.parent / "runtime" / "agent_pack_config.py"), "reconcile",
             "--project", str(project), "--client", "codex", "--retire-plugin", RETIRED, "--retire-only",
             "--home", str(home)], capture_output=True, text=True, env=env, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("residual", done.stdout.lower())


if __name__ == "__main__":
    unittest.main()
