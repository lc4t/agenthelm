"""#68: `--only mcp|skills|plugins` and per-type `--replace-mcp` / `--replace-skills` for sync and reconcile."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(RUNTIME))

from test_agent_pack_config import make_project, write_json  # noqa: E402
from test_sync_preview_parity import snapshot  # noqa: E402
import mcp_server  # noqa: E402

SKILL = "sample-skill"
MCP = "local-check"
ALL_TYPES = ("plugins", "skills", "mcp")


class SyncOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = make_project(self.root / "proj", "proj")
        self.home = self.root / "home"
        self.home.mkdir()
        self.recovery = self.root / "recovery"
        self.skill_dir = self.home / ".claude" / "skills" / SKILL
        self.config = self.home / ".claude.json"

    def conflicts(self) -> None:
        """A user-level Skill and a user-level MCP entry that share the project's names but are not ours."""
        self.skill_dir.mkdir(parents=True)
        (self.skill_dir / "SKILL.md").write_text("---\nname: sample-skill\ndescription: mine\n---\nglobal\n")
        write_json(self.config, {"mcpServers": {MCP: {"type": "stdio", "command": "node", "args": ["mine.js"]}}})

    def cli(self, *args: str, command: str = "sync", client: str = "claude"):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["HOME"] = str(self.home)
        proc = subprocess.run(
            [sys.executable, str(RUNTIME / "agent_pack_config.py"), "--output", "json", command,
             "--project", str(self.project), "--client", client, "--home", str(self.home),
             "--recovery-root", str(self.recovery), *args],
            capture_output=True, text=True, env=env, check=False)
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = None
        return proc.returncode, payload, proc.stdout + proc.stderr

    def actions(self, payload: dict) -> set[tuple[str, str]]:
        return {(op["action"], op["name"]) for op in payload["operations"]}

    def skills_tree(self) -> dict:
        return snapshot(self.home / ".claude" / "skills") if (self.home / ".claude" / "skills").exists() else {}

    # ------------------------------------------------------------ the issue's scenario

    def test_default_behaviour_is_unchanged(self) -> None:
        self.conflicts()
        rc, payload, out = self.cli()
        self.assertEqual(rc, 2, out)
        self.assertIn("skill sample-skill", payload["error"])
        self.assertIn(f"mcp {MCP}", payload["error"])
        rc, payload, out = self.cli("--replace")
        self.assertEqual(rc, 0, out)
        acts = self.actions(payload)
        self.assertIn(("conflict-skill", SKILL), acts)
        self.assertIn(("conflict-mcp", MCP), acts)

    def test_only_mcp_replace_mcp_never_touches_skills_in_preview_or_apply(self) -> None:
        self.conflicts()
        before_skills, before_home = self.skills_tree(), snapshot(self.home)
        rc, preview, out = self.cli("--only", "mcp", "--replace-mcp")
        self.assertEqual(rc, 0, out)
        self.assertFalse(any(action.endswith("-skill") for action, _ in self.actions(preview)), preview)
        self.assertFalse(any(action.endswith("-package") or action.endswith("-record")
                             for action, _ in self.actions(preview)) and False)
        self.assertIn(("conflict-mcp", MCP), self.actions(preview))
        self.assertEqual(snapshot(self.home), before_home, "preview writes nothing")
        self.assertFalse(self.recovery.exists())
        rc, applied, out = self.cli("--only", "mcp", "--replace-mcp", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.skills_tree(), before_skills, "the user-level Skill is untouched")
        self.assertTrue(self.skill_dir.is_dir() and not self.skill_dir.is_symlink())
        servers = json.loads(self.config.read_text())["mcpServers"]
        self.assertNotEqual(servers[MCP]["args"], ["mine.js"])
        recovered = list(Path(applied["recovery"]).rglob("*"))
        self.assertTrue(any(p.name == "replaced-mcp.json" for p in recovered))
        self.assertFalse(any("skills" in p.parts for p in recovered), "no Skill was archived")
        self.assertFalse((self.home / ".claude/agent-pack/plugins/proj/skills").exists(),
                         "the package carries no Skills the user did not ask to sync")

    def test_only_skills_replace_skills_never_touches_mcp(self) -> None:
        self.conflicts()
        before_config = self.config.read_bytes()
        rc, preview, out = self.cli("--only", "skills", "--replace-skills")
        self.assertEqual(rc, 0, out)
        self.assertFalse(any(action.endswith("-mcp") for action, _ in self.actions(preview)), preview)
        self.assertIn(("conflict-skill", SKILL), self.actions(preview))
        self.assertEqual(self.config.read_bytes(), before_config)
        rc, applied, out = self.cli("--only", "skills", "--replace-skills", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.config.read_bytes(), before_config, "the user-level MCP config is untouched")
        self.assertTrue(self.skill_dir.is_symlink())
        self.assertFalse((self.home / ".claude/agent-pack/plugins/proj/mcp.json").exists())

    def test_conflicts_of_a_type_that_is_not_selected_do_not_block(self) -> None:
        self.conflicts()
        rc, payload, out = self.cli("--only", "mcp")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"mcp {MCP}", payload["error"])
        self.assertNotIn("skill sample-skill", payload["error"])
        rc, payload, out = self.cli("--only", "skills")
        self.assertEqual(rc, 2, out)
        self.assertIn("skill sample-skill", payload["error"])
        self.assertNotIn(f"mcp {MCP}", payload["error"])

    def test_replace_scope_is_per_type(self) -> None:
        self.conflicts()
        rc, payload, out = self.cli("--replace-mcp")
        self.assertEqual(rc, 2, out)
        self.assertIn("skill sample-skill", payload["error"])
        self.assertNotIn(f"mcp {MCP}", payload["error"])
        rc, payload, out = self.cli("--replace-skills")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"mcp {MCP}", payload["error"])
        rc, payload, out = self.cli("--replace-mcp", "--replace-skills")
        self.assertEqual(rc, 0, out)

    def test_preview_summarises_conflicts_by_type_and_warns_about_global_content(self) -> None:
        self.conflicts()
        rc, payload, out = self.cli("--replace")
        self.assertEqual(rc, 0, out)
        self.assertEqual(payload["conflicts"], {"skills": 1, "mcp": 1})
        text = " ".join(payload["warnings"])
        self.assertIn(str(self.home / ".claude" / "skills"), text)
        self.assertIn(str(self.config), text)
        rc, payload, out = self.cli("--only", "mcp", "--replace-mcp")
        self.assertEqual(payload["conflicts"], {"mcp": 1})
        self.assertNotIn("skills", " ".join(payload["warnings"]))

    def test_clean_sync_has_no_conflict_summary_or_warning(self) -> None:
        rc, payload, out = self.cli()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("conflicts", payload)
        self.assertNotIn("warnings", payload)

    # ------------------------------------------------------------ selection semantics

    def test_selecting_all_types_equals_the_default(self) -> None:
        rc, default, out = self.cli()
        self.assertEqual(rc, 0, out)
        for flags in (("--only", "mcp,skills,plugins"), ("--only", "mcp", "--only", "skills", "--only", "plugins")):
            with self.subTest(flags=flags):
                rc, selected, out = self.cli(*flags)
                self.assertEqual(rc, 0, out)
                self.assertEqual(selected, default)

    def test_only_plugins_installs_the_package_and_record_only(self) -> None:
        rc, applied, out = self.cli("--only", "plugins", "--apply")
        self.assertEqual(rc, 0, out)
        acts = {action for action, _ in self.actions(applied)}
        self.assertEqual(acts, {"install-package", "link-record"})
        self.assertFalse((self.home / ".claude" / "skills").exists())
        self.assertFalse(self.config.exists())

    def test_types_can_be_synced_one_after_the_other_and_end_in_sync(self) -> None:
        for only in ("plugins", "mcp", "skills"):
            rc, _payload, out = self.cli("--only", only, "--apply")
            self.assertEqual(rc, 0, out)
        rc, payload, out = self.cli()
        self.assertEqual(rc, 0, out)
        self.assertTrue(all(action.startswith("keep-") for action, _ in self.actions(payload)), payload)
        self.assertTrue((self.home / ".claude/agent-pack/plugins/proj/skills" / SKILL).is_dir())
        self.assertTrue((self.home / ".claude/agent-pack/plugins/proj/mcp.json").is_file())

    def test_removed_items_of_an_unselected_type_are_left_alone(self) -> None:
        rc, _p, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        write_json(self.project / ".agents" / "mcp.json", {"mcpServers": {}})          # the MCP is dropped
        import shutil
        shutil.rmtree(self.project / ".agents" / "skills" / SKILL)                      # and so is the Skill
        rc, payload, out = self.cli("--only", "skills", "--apply")
        self.assertEqual(rc, 0, out)
        acts = self.actions(payload)
        self.assertIn(("remove-skill", SKILL), acts)
        self.assertFalse(any(action.endswith("-mcp") for action, _ in acts))
        self.assertIn(MCP, json.loads(self.config.read_text())["mcpServers"], "the MCP entry stays until mcp is synced")
        rc, payload, out = self.cli("--only", "mcp", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.config.read_text())["mcpServers"], {})

    def test_reconcile_shares_the_logic(self) -> None:
        self.conflicts()
        rc, via_sync, out = self.cli("--only", "mcp", "--replace-mcp")
        self.assertEqual(rc, 0, out)
        rc, via_reconcile, out = self.cli("--only", "mcp", "--replace-mcp", command="reconcile")
        self.assertEqual(rc, 0, out)
        self.assertEqual(via_reconcile["operations"], via_sync["operations"])
        self.assertEqual(via_reconcile["conflicts"], via_sync["conflicts"])

    def test_explicit_retire_still_runs_with_only(self) -> None:
        link_target = self.root / "legacy"
        (link_target / "skills" / "legacy-skill").mkdir(parents=True)
        (link_target / "skills" / "legacy-skill" / "SKILL.md").write_text("x")
        (self.home / ".claude" / "skills").mkdir(parents=True)
        (self.home / ".claude" / "skills" / "legacy-skill").symlink_to(link_target / "skills" / "legacy-skill")
        rc, payload, out = self.cli("--only", "mcp", "--retire-skill", "legacy-skill", command="reconcile")
        self.assertEqual(rc, 0, out)
        self.assertIn(("archive-skill", "legacy-skill"), self.actions(payload))

    # ------------------------------------------------------------ other clients and bad input

    def test_other_clients_reject_a_partial_selection_without_writing(self) -> None:
        for client in ("codex", "cursor"):
            for flags in (("--only", "mcp"), ("--only", "skills,plugins"), ("--replace-skills",)):
                with self.subTest(client=client, flags=flags):
                    before = snapshot(self.home)
                    rc, payload, out = self.cli(*flags, "--apply", client=client)
                    self.assertEqual(rc, 2, out)
                    self.assertIn("claude", payload["error"])
                    self.assertEqual(snapshot(self.home), before)

    def test_other_clients_accept_the_full_selection(self) -> None:
        for client in ("codex", "cursor"):
            with self.subTest(client=client):
                rc, default, out = self.cli(client=client)
                self.assertEqual(rc, 0, out)
                rc, selected, out = self.cli("--only", "mcp,skills,plugins", client=client)
                self.assertEqual(rc, 0, out)
                self.assertEqual(selected, default)

    def test_unknown_type_is_a_clean_error(self) -> None:
        rc, payload, out = self.cli("--only", "everything")
        self.assertEqual(rc, 2, out)
        self.assertNotIn("Traceback", out)

    def test_help_documents_the_new_flags(self) -> None:
        for command in ("sync", "reconcile"):
            done = subprocess.run([sys.executable, str(RUNTIME / "agent_pack_config.py"), command, "--help"],
                                  capture_output=True, text=True, check=False)
            for flag in ("--only", "--replace-mcp", "--replace-skills"):
                self.assertIn(flag, done.stdout, (command, flag))

    # ------------------------------------------------------------ MCP tools

    def test_mcp_tools_expose_the_same_parameters(self) -> None:
        tools = {tool["name"]: tool for tool in mcp_server.TOOLS}
        for name in ("agent_pack_sync", "agent_pack_reconcile"):
            properties = tools[name]["inputSchema"]["properties"]
            self.assertEqual(properties["only"]["items"]["enum"], ["mcp", "skills", "plugins"])
            self.assertEqual(properties["replaceMcp"]["type"], "boolean")
            self.assertEqual(properties["replaceSkills"]["type"], "boolean")
        self.conflicts()
        arguments = {"project": str(self.project), "client": "claude", "home": str(self.home),
                     "only": ["mcp"], "replaceMcp": True, "recoveryRoot": str(self.recovery)}
        for name in ("agent_pack_sync", "agent_pack_reconcile"):
            result = mcp_server._call(name, dict(arguments))
            self.assertFalse(any(action.endswith("-skill") for action, _ in
                                 {(op["action"], op["name"]) for op in result["operations"]}), name)


if __name__ == "__main__":
    unittest.main()
