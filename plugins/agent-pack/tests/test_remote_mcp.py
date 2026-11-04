"""Issue #51: process env references, native projections and safe reverse sync."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from test_agent_pack_config import make_project, write_json
import agent_pack_config as core
import agent_pack_codex_mcp as codex
import agenthelm


class RemoteMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        patch = mock.patch.object(Path, "home", return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)
        self.project = make_project(self.root / "project", "project")
        self.config = self.project / ".agents" / "mcp.json"
        self.remote = {"type": "http", "url": "https://mcp.example.com/mcp",
                       "bearer_token_env_var": "EXAMPLE_MCP_TOKEN"}
        write_json(self.config, {"$schema": core.AGENT_PLUGIN_MCP_SCHEMA, "mcpServers": {"example": self.remote}})
        self.secret = "bearer-sensitive-value-never-written-123456"

    def test_remote_sync_all_clients_without_credential_file_or_token_value(self):
        with mock.patch.dict(os.environ, {"EXAMPLE_MCP_TOKEN": self.secret}):
            for client in ("claude", "codex", "cursor"):
                before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
                preview = core.sync_project(self.project, client, home=self.home)
                self.assertFalse(preview["applied"])
                self.assertEqual(before, sorted(path.relative_to(self.root) for path in self.root.rglob("*")))
                result = core.sync_project(self.project, client, home=self.home, apply=True)
                self.assertTrue(result["applied"])
                self.assertNotIn(self.secret, json.dumps(result))
                self.assertTrue(core.client_drift(self.project, client, self.home)["in_sync"])
            native = tomllib.loads((self.home / ".codex/config.toml").read_text())["mcp_servers"]["example"]
            self.assertEqual(native, {"url": self.remote["url"], "bearer_token_env_var": "EXAMPLE_MCP_TOKEN"})
            claude = json.loads((self.home / ".claude.json").read_text())["mcpServers"]["example"]
            self.assertEqual(claude["headers"]["Authorization"], "Bearer ${EXAMPLE_MCP_TOKEN}")
            cursor = json.loads((self.home / ".cursor/plugins/local/project/mcp.json").read_text())["mcpServers"]["example"]
            self.assertEqual(cursor["headers"]["Authorization"], "Bearer ${env:EXAMPLE_MCP_TOKEN}")
            for path in self.root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(self.secret.encode(), path.read_bytes(), str(path))

    def test_doctor_missing_env_clears_when_set_and_sync_still_succeeds(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            for client in ("claude", "codex", "cursor"):
                core.sync_project(self.project, client, home=self.home, apply=True)
                report = core.doctor(self.project, [client], self.home)
                self.assertFalse(report["ok"])
                self.assertIn("EXAMPLE_MCP_TOKEN", json.dumps(report))
                self.assertFalse(report["clients"][client]["in_sync"])
            with mock.patch.dict(os.environ, {"EXAMPLE_MCP_TOKEN": self.secret}):
                for client in ("claude", "codex", "cursor"):
                    self.assertTrue(core.doctor(self.project, [client], self.home)["ok"])

    def test_codex_and_claude_import_equivalent_and_cursor_roundtrip(self):
        (self.home / ".codex").mkdir()
        (self.home / ".codex/config.toml").write_text('[mcp_servers.example]\nurl = "https://mcp.example.com/mcp"\nbearer_token_env_var = "EXAMPLE_MCP_TOKEN"\n')
        headers = {"url": self.remote["url"], "headers": {"Authorization": "Bearer ${EXAMPLE_MCP_TOKEN}"}}
        write_json(self.home / ".claude.json", {"mcpServers": {"example": headers}})
        headers["headers"]["Authorization"] = "Bearer ${env:EXAMPLE_MCP_TOKEN}"
        write_json(self.home / ".cursor/mcp.json", {"mcpServers": {"example": headers}})
        configs = []
        for client in ("claude", "codex", "cursor"):
            inventory = core.inventory_client(client, self.home)
            self.assertTrue(inventory.mcps[0].portable)
            core.transfer_to_project(inventory, self.project, [], ["example"], update=True, apply=True)
            configs.append(json.loads(self.config.read_text())["mcpServers"]["example"])
        self.assertEqual(configs[0], configs[1])
        self.assertEqual(configs[1], configs[2])

    def test_native_header_fields_and_literal_secret_blocked(self):
        parsed = core._portable_mcp("example", {"url": self.remote["url"], "http_headers": {"Accept": "application/json"},
                                               "env_http_headers": {"X-Api-Key": "EXAMPLE_MCP_TOKEN"}})
        self.assertTrue(parsed.portable)
        self.assertEqual(codex.native_entry(parsed.config)["env_http_headers"], {"X-Api-Key": "EXAMPLE_MCP_TOKEN"})
        for raw in ({"url": self.remote["url"], "http_headers": {"Authorization": "Bearer " + self.secret}},
                    {"url": self.remote["url"], "bearer_token_env_var": "not a name"},
                    {"url": self.remote["url"], "env_http_headers": {"X-Key": "bad-name"}},
                    {"url": "https://mcp.example.com/mcp?token=" + self.secret},
                    {"url": "https://user:password@mcp.example.com/mcp"}):
            self.assertFalse(core._portable_mcp("unsafe", raw).portable)

    def test_transfer_update_preview_recovery_and_skill_update(self):
        source = make_project(self.root / "source", "source")
        skill = source / ".agents/skills/sample-skill/SKILL.md"
        skill.write_text(skill.read_text() + "New instructions.\n")
        write_json(source / ".agents/mcp.json", {"mcpServers": {"example": {**self.remote, "url": "https://other.example/mcp"}}})
        inventory = core.inventory_project(source)
        with self.assertRaises(core.AgentPackError):
            core.transfer_to_project(inventory, self.project, ["sample-skill"], ["example"])
        old_skill = (self.project / ".agents/skills/sample-skill/SKILL.md").read_bytes()
        old_mcp = self.config.read_bytes()
        preview = core.transfer_to_project(inventory, self.project, ["sample-skill"], ["example"], update=True)
        self.assertEqual(len(preview["operations"]), 2)
        self.assertEqual(self.config.read_bytes(), old_mcp)
        self.assertNotIn("https://other.example", json.dumps(preview))
        result = core.transfer_to_project(inventory, self.project, ["sample-skill"], ["example"], update=True, apply=True)
        recovery = Path(result["recovery"])
        self.assertEqual((recovery / "skills/sample-skill/SKILL.md").read_bytes(), old_skill)
        self.assertEqual((recovery / "mcp.json").read_bytes(), old_mcp)
        self.assertEqual(core.transfer_to_project(inventory, self.project, ["sample-skill"], ["example"], update=True)["operations"], [])

    def test_native_codex_preserves_other_sections_and_removes_only_owned_server(self):
        path = self.home / ".codex/config.toml"
        path.parent.mkdir()
        unrelated = 'model = "gpt-test"\n\n[mcp_servers.other]\ncommand = "other"\n\n[features]\nplugins = true\n'
        path.write_text(unrelated)
        core.sync_project(self.project, "codex", home=self.home, apply=True)
        data = tomllib.loads(path.read_text())
        self.assertEqual(data["model"], "gpt-test")
        self.assertEqual(data["mcp_servers"]["other"], {"command": "other"})
        write_json(self.config, {"mcpServers": {}})
        core.sync_project(self.project, "codex", home=self.home, apply=True)
        self.assertEqual(tomllib.loads(path.read_text()), tomllib.loads(unrelated))

    def test_native_codex_conflict_requires_replace_and_invalid_toml_stops_preview(self):
        path = self.home / ".codex/config.toml"
        path.parent.mkdir()
        path.write_text('[mcp_servers.example]\nurl = "https://local.example"\n')
        with self.assertRaisesRegex(core.AgentPackError, "--replace"):
            core.sync_project(self.project, "codex", home=self.home)
        core.sync_project(self.project, "codex", home=self.home, replace=True)
        path.write_text("broken = [")
        with self.assertRaisesRegex(core.AgentPackError, "invalid Codex"):
            core.sync_project(self.project, "codex", home=self.home)

    def test_native_codex_locally_edited_owned_config_requires_replace(self):
        core.sync_project(self.project, "codex", home=self.home, apply=True)
        path = self.home / ".codex/config.toml"
        path.write_text(path.read_text() + 'startup_timeout_sec = 60\n')
        original = path.read_bytes()
        with self.assertRaisesRegex(core.AgentPackError, "--replace"):
            core.sync_project(self.project, "codex", home=self.home, apply=True)
        self.assertEqual(path.read_bytes(), original)
        result = core.sync_project(self.project, "codex", home=self.home, apply=True, replace=True)
        self.assertNotIn("startup_timeout_sec", tomllib.loads(path.read_text())["mcp_servers"]["example"])
        self.assertEqual((Path(result["recovery"]) / "codex-config.toml").read_bytes(), original)

    def test_transfer_update_failure_restores_every_selected_skill(self):
        source = make_project(self.root / "source", "source")
        (source / ".agents/skills/sample-skill/SKILL.md").write_text("changed instructions")
        write_json(source / ".agents/mcp.json", {"mcpServers": {"example": self.remote}})
        # Ensure MCP also changes after the Skill is installed.
        write_json(self.config, {"mcpServers": {"example": {**self.remote, "url": "https://old.example/mcp"}}})
        before_skill = (self.project / ".agents/skills/sample-skill/SKILL.md").read_bytes()
        before_mcp = self.config.read_bytes()
        with mock.patch.object(core, "_write_json_atomic", side_effect=OSError("injected failure")):
            with self.assertRaises(OSError):
                core.transfer_to_project(core.inventory_project(source), self.project, ["sample-skill"], ["example"],
                                         update=True, apply=True)
        self.assertEqual((self.project / ".agents/skills/sample-skill/SKILL.md").read_bytes(), before_skill)
        self.assertEqual(self.config.read_bytes(), before_mcp)

    def test_native_codex_state_write_failure_rolls_back_config_and_package(self):
        path = self.home / ".codex/config.toml"
        path.parent.mkdir()
        path.write_text('model = "untouched"\n')
        original_write = core._write_json_atomic
        def fail_state(target, *args, **kwargs):
            if target.name == "codex-remote-project.json":
                raise OSError("injected state failure")
            return original_write(target, *args, **kwargs)
        with mock.patch.object(core, "_write_json_atomic", side_effect=fail_state):
            with self.assertRaisesRegex(core.AgentPackError, "rolled back"):
                core.sync_project(self.project, "codex", home=self.home, apply=True)
        self.assertEqual(path.read_text(), 'model = "untouched"\n')
        self.assertFalse((self.home / ".agents/plugins/plugins/project").exists())
        self.assertFalse((self.home / ".agent-pack/state/codex-remote-project.json").exists())

    def test_hook_discovers_drift_offline_and_per_client_disable(self):
        (self.home / ".codex").mkdir()
        env = agenthelm.Env(self.home, {"PATH": "", "AGENTHELM_LANG": "en"})
        reports = agenthelm.capability_findings(env, self.project)
        self.assertEqual([item["client"] for item in reports], ["codex"])
        out = io.StringIO()
        with mock.patch.object(agenthelm, "run_check", side_effect=agenthelm.CheckError("offline")), contextlib.redirect_stdout(out):
            self.assertEqual(agenthelm.hook(env, self.project, "text"), 0)
        self.assertIn("MCP / Skill", out.getvalue())
        self.assertIn("doctor --client", out.getvalue())
        with mock.patch.dict(os.environ, {"EXAMPLE_MCP_TOKEN": self.secret}):
            core.sync_project(self.project, "codex", home=self.home, apply=True)
            self.assertEqual(agenthelm.capability_findings(env, self.project), [])
            silent = io.StringIO()
            with mock.patch.object(agenthelm, "run_check", side_effect=agenthelm.CheckError("offline")), contextlib.redirect_stdout(silent):
                agenthelm.hook(env, self.project, "text")
            self.assertEqual(silent.getvalue(), "")
        write_json(env.config_file, {"capability_check_ignored_clients": ["codex"]})
        self.assertEqual(agenthelm.capability_findings(env, self.project), [])


if __name__ == "__main__":
    unittest.main()
