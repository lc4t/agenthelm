"""Batch D2 (#134): what ``sync`` writes for the three existing clients, pinned through the public API only.

These cases were written against the code before ``sync`` read the client declarations and passed there unchanged:
Codex TOML writing and residual cleanup, the Cursor ``headers`` rewrite and Claude's user, local and project scopes.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "runtime"))

import agent_pack_config as core  # noqa: E402
from test_agent_pack_config import PROJECT_PROFILE_NAMESPACE, make_project, write_json  # noqa: E402

REMOTE = {"type": "streamable-http", "url": "https://mcp.example.com/mcp",
          "headers": {"Authorization": "Bearer ${EXAMPLE_MCP_TOKEN}", "X-Api-Key": "${API_KEY_ENV}", "X-Static": "1"}}
STDIO = {"type": "stdio", "command": "python3", "args": ["-m", "checker"]}


def declare(project: Path, **keys) -> None:
    profile = project / ".agents" / PROJECT_PROFILE_NAMESPACE / "project.json"
    data = json.loads(profile.read_text(encoding="utf-8"))
    data["capabilities"].update(keys)
    write_json(profile, data)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.recovery = self.root / "recovery"
        self.project = make_project(self.root / "proj", "proj")
        self.env = mock.patch.dict(os.environ, {"EXAMPLE_MCP_TOKEN": "t-dummy", "API_KEY_ENV": "k-dummy"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def servers(self, mapping: dict) -> None:
        write_json(self.project / ".agents" / "mcp.json", {"$schema": core.AGENT_PLUGIN_MCP_SCHEMA, "mcpServers": mapping})

    def sync(self, client: str, apply: bool = False, **kwargs):
        return core.sync_project(self.project, client, home=self.home, apply=apply, recovery_root=self.recovery, **kwargs)

    def reconcile(self, client: str, apply: bool = False, **kwargs):
        return core.reconcile_project(self.project, client, home=self.home, apply=apply, recovery_root=self.recovery, **kwargs)

    def read_json(self, path: Path):
        return json.loads(path.read_text(encoding="utf-8"))


class CodexTomlWriting(Base):
    CONFIG = Path(".codex") / "config.toml"

    def setUp(self) -> None:
        super().setUp()
        self.servers({"example": REMOTE, "local-check": STDIO})
        self.config = self.home / self.CONFIG

    def test_a_preview_writes_nothing_and_an_apply_adds_one_named_table_and_the_owner_record(self) -> None:
        preview = self.sync("codex")
        self.assertFalse(preview["applied"])
        self.assertIn({"action": "add-native-mcp", "name": "example"}, preview["operations"])
        self.assertFalse(self.config.exists())
        self.sync("codex", apply=True)
        table = tomllib.loads(self.config.read_text(encoding="utf-8"))["mcp_servers"]
        self.assertEqual(table["example"], {"url": REMOTE["url"], "bearer_token_env_var": "EXAMPLE_MCP_TOKEN",
                                            "http_headers": {"X-Static": "1"},
                                            "env_http_headers": {"X-Api-Key": "API_KEY_ENV"}})
        self.assertNotIn("local-check", table)   # a stdio server travels in the package, not in config.toml
        state = self.read_json(self.home / ".agent-pack" / "state" / "codex-remote-proj.json")
        self.assertEqual(sorted(state["servers"]), ["example"])
        self.assertEqual(stat.S_IMODE((self.home / ".agent-pack/state/codex-remote-proj.json").stat().st_mode), 0o600)
        self.assertEqual(self.sync("codex", apply=True)["operations"][0]["action"], "keep-plugin")

    def test_unrelated_toml_is_kept_byte_for_byte_and_the_original_goes_to_recovery(self) -> None:
        original = '# my comment\nmodel = "x"\n\n[mcp_servers.hand]\ncommand = "h"\n\n[plugins."other@mk"]\nenabled = true\n'
        self.config.parent.mkdir()
        self.config.write_text(original, encoding="utf-8")
        os.chmod(self.config, 0o644)
        result = self.sync("codex", apply=True)
        written = self.config.read_text(encoding="utf-8")
        self.assertTrue(written.startswith(original.rstrip() + "\n"))
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o644)
        self.assertEqual((Path(result["recovery"]) / "codex-config.toml").read_text(encoding="utf-8"), original)

    def test_a_zero_byte_config_is_an_empty_table_set(self) -> None:
        self.config.parent.mkdir()
        self.config.write_text("", encoding="utf-8")
        self.sync("codex", apply=True)
        self.assertIn("example", tomllib.loads(self.config.read_text(encoding="utf-8"))["mcp_servers"])

    def test_a_different_destination_entry_is_refused_until_replace(self) -> None:
        self.config.parent.mkdir()
        self.config.write_text('[mcp_servers.example]\nurl = "https://old.example.com"\n', encoding="utf-8")
        with self.assertRaises(core.AgentPackError) as caught:
            self.sync("codex")
        self.assertEqual(str(caught.exception), "Codex MCP destination differs: example; preview with --replace")
        self.sync("codex", apply=True, replace=True)
        self.assertEqual(tomllib.loads(self.config.read_text())["mcp_servers"]["example"]["url"], REMOTE["url"])

    def test_an_owned_entry_is_removed_when_the_project_drops_it_but_a_local_edit_stays(self) -> None:
        self.sync("codex", apply=True)
        self.servers({"local-check": STDIO})
        preview = self.sync("codex")
        self.assertIn({"action": "remove-native-mcp", "name": "example"}, preview["operations"])
        self.sync("codex", apply=True)
        self.assertNotIn("example", tomllib.loads(self.config.read_text()).get("mcp_servers", {}))
        self.servers({"example": REMOTE, "local-check": STDIO})
        self.sync("codex", apply=True)
        self.config.write_text(self.config.read_text().replace(REMOTE["url"], "https://edited.example.com"), encoding="utf-8")
        self.servers({"local-check": STDIO})
        self.assertNotIn("remove-native-mcp", [item["action"] for item in self.sync("codex")["operations"]])

    def test_broken_symlinked_and_inline_configs_are_refused_with_the_documented_messages(self) -> None:
        self.config.parent.mkdir()
        self.config.write_text("[[[ nope", encoding="utf-8")
        with self.assertRaises(core.AgentPackError) as caught:
            self.sync("codex")
        self.assertEqual(str(caught.exception), "invalid Codex config.toml")
        self.config.write_text('mcp_servers = { example = { url = "https://x" } }\n', encoding="utf-8")
        with self.assertRaises(core.AgentPackError) as caught:
            self.sync("codex", replace=True)
        self.assertIn("Codex MCP must use named [mcp_servers.NAME] tables", str(caught.exception))
        self.config.unlink()
        target = self.root / "elsewhere.toml"
        target.write_text("", encoding="utf-8")
        self.config.symlink_to(target)
        with self.assertRaises(core.AgentPackError) as caught:
            self.sync("codex")
        self.assertTrue(str(caught.exception).startswith("refusing symlinked Codex config: "))

    def test_the_package_and_the_personal_marketplace_entry(self) -> None:
        self.sync("codex", apply=True)
        package = self.home / ".agents/plugins/plugins/proj"
        manifest = self.read_json(package / ".codex-plugin" / "plugin.json")
        self.assertEqual((manifest["name"], manifest["skills"], manifest["mcpServers"]), ("proj", "./skills/", "./.mcp.json"))
        self.assertEqual(sorted(self.read_json(package / ".mcp.json")["mcpServers"]), ["local-check"])
        self.assertEqual(sorted(self.read_json(package / "codex-remote-mcp.json")["mcpServers"]), ["example"])
        market = self.read_json(self.home / ".agents/plugins/marketplace.json")
        self.assertEqual(market["plugins"][-1]["source"], {"source": "local", "path": "./.agents/plugins/plugins/proj"})


class CodexResidualCleanup(Base):
    def setUp(self) -> None:
        super().setUp()
        legacy = self.home / ".agents/plugins/plugins/legacyp"
        write_json(legacy / "plugin.json", {"name": "legacyp", "version": "0.1.0"})
        write_json(self.home / ".agents/plugins/marketplace.json",
                   {"name": "personal", "plugins": [{"name": "legacyp"}, {"name": "keep"}]})
        write_json(self.home / ".codex/plugins/cache/mk/legacyp/1.0.0/plugin.json", {"name": "legacyp", "version": "1.0.0"})
        write_json(self.home / ".codex/plugins/cache/mk/legacyp/0.9.0/plugin.json", {"name": "legacyp", "version": "0.9.0"})
        (self.home / ".codex/config.toml").write_text('[plugins."legacyp@mk"]\nenabled = true\n', encoding="utf-8")
        self.legacy = legacy

    def test_the_copy_and_the_marketplace_entry_are_archived_and_the_rest_listed_as_residuals(self) -> None:
        preview = self.reconcile("codex", retire_plugins=["legacyp"])
        actions = [item["action"] for item in preview["operations"]]
        self.assertEqual((actions.count("archive-plugin"), actions.count("archive-plugin-copy")), (1, 1))
        self.assertIn("remove-marketplace-entry", actions)
        self.assertEqual(sorted(item["kind"] for item in preview.get("residuals", [])), ["cache", "config"])
        self.assertTrue(self.legacy.exists())
        result = self.reconcile("codex", apply=True, retire_plugins=["legacyp"])
        self.assertFalse(self.legacy.exists())
        market = self.read_json(self.home / ".agents/plugins/marketplace.json")
        self.assertIn("keep", [item["name"] for item in market["plugins"]])
        self.assertNotIn("legacyp", [item["name"] for item in market["plugins"] if item["name"] != "proj"])
        self.assertTrue((Path(result["recovery"]) / "marketplace" / "marketplace.json").is_file())
        self.assertIn('plugins."legacyp@mk"', (self.home / ".codex/config.toml").read_text())   # never edited

    def test_a_symlinked_or_unreadable_marketplace_is_a_residual_not_an_edit(self) -> None:
        market = self.home / ".agents/plugins/marketplace.json"
        market.write_text("garbage", encoding="utf-8")
        preview = self.reconcile("codex", retire_plugins=["legacyp"])
        kinds = [(item["kind"], item["manual"]) for item in preview["residuals"] if item["kind"] == "marketplace-entry"]
        self.assertEqual(len(kinds), 1)
        self.assertTrue(kinds[0][1].startswith("unreadable marketplace file"))
        market.unlink()
        market.symlink_to(self.root / "nowhere.json")
        preview = self.reconcile("codex", retire_plugins=["legacyp"])
        self.assertIn("symlinked file, not edited automatically",
                      [item["manual"] for item in preview["residuals"] if item["kind"] == "marketplace-entry"][0])


class CursorHeaders(Base):
    def setUp(self) -> None:
        super().setUp()
        self.servers({"example": REMOTE, "bare": {"type": "streamable-http", "url": "https://bare.example.com/mcp"},
                      "local-check": STDIO})

    def package_servers(self, client: str) -> dict:
        self.sync(client, apply=True)
        return self.read_json(self.home / ".cursor/plugins/local/proj/mcp.json")["mcpServers"]

    def test_remote_header_references_are_written_in_the_env_namespace(self) -> None:
        servers = self.package_servers("cursor")
        self.assertEqual(servers["example"]["headers"], {
            "Authorization": "Bearer ${env:EXAMPLE_MCP_TOKEN}", "X-Api-Key": "${env:API_KEY_ENV}", "X-Static": "1"})
        self.assertNotIn("headers", servers["bare"])
        self.assertEqual(servers["local-check"], {"type": "stdio", "command": "python3", "args": ["-m", "checker"]})

    def test_the_other_clients_keep_the_reference_as_written(self) -> None:
        self.sync("claude", apply=True)
        entry = self.read_json(self.home / ".claude.json")["mcpServers"]["example"]
        self.assertEqual(entry["headers"]["Authorization"], "Bearer ${EXAMPLE_MCP_TOKEN}")
        self.assertEqual(entry["type"], "http")
        scratch = self.root / "scratch"
        scratch.mkdir()
        built = core._build_package(core.inventory_project(self.project), "proj", scratch, "codex", self.home)
        remote = self.read_json(built / "codex-remote-mcp.json")["mcpServers"]["example"]   # moved out of the package
        self.assertEqual(remote["headers"]["Authorization"], "Bearer ${EXAMPLE_MCP_TOKEN}")


class ClaudeScopes(Base):
    def setUp(self) -> None:
        super().setUp()
        self.servers({"local-check": STDIO, "projmcp": {"type": "stdio", "command": "python3", "args": ["-m", "p"]}})

    def config(self) -> dict:
        return self.read_json(self.home / ".claude.json")

    def test_user_scope_is_the_default_and_goes_to_the_user_file_and_links(self) -> None:
        result = self.sync("claude", apply=True)
        self.assertEqual(sorted(self.config()["mcpServers"]), ["local-check", "projmcp"])
        link = self.home / ".claude/skills/sample-skill"
        self.assertTrue(link.is_symlink())
        self.assertTrue((self.home / ".claude/agent-pack/plugins/proj").is_symlink())
        self.assertEqual({item["client"] for item in result["operations"]}, {"claude"})
        state = self.read_json(self.home / ".claude/agent-pack/state/proj.json")
        self.assertEqual(sorted(state["mcps"]), ["local-check", "projmcp"])

    def test_local_scope_writes_under_the_project_path_in_the_user_file(self) -> None:
        declare(self.project, mcp_scope={"projmcp": "project"})
        result = self.sync("claude", apply=True)
        data = self.config()
        self.assertEqual(sorted(data["mcpServers"]), ["local-check"])
        self.assertEqual(sorted(data["projects"][str(self.project)]["mcpServers"]), ["projmcp"])
        self.assertEqual(result["by_scope"]["project"]["mcp"], 1)

    def test_project_file_store_writes_the_project_mcp_json(self) -> None:
        declare(self.project, mcp_scope={"projmcp": "project"})
        self.sync("claude", apply=True, project_store="file")
        self.assertEqual(sorted(self.read_json(self.project / ".mcp.json")["mcpServers"]), ["projmcp"])
        self.assertNotIn("projects", self.config())

    def test_project_declared_skills_are_linked_into_the_project(self) -> None:
        declare(self.project, skill_scope={"sample-skill": "project"})
        self.sync("claude", apply=True)
        self.assertTrue((self.project / ".claude/skills/sample-skill").exists())
        self.assertFalse((self.home / ".claude/skills/sample-skill").exists())

    def test_scope_user_leaves_project_declared_entries_alone_and_scope_project_the_user_ones(self) -> None:
        declare(self.project, mcp_scope={"projmcp": "project"})
        self.sync("claude", apply=True, scope="user")
        self.assertNotIn("projects", self.config())
        self.sync("claude", apply=True, scope="project")
        self.assertEqual(sorted(self.config()["projects"][str(self.project)]["mcpServers"]), ["projmcp"])

    def test_the_flags_only_the_native_client_takes_are_named_in_the_error(self) -> None:
        for client in ("codex", "cursor"):
            with self.assertRaises(core.AgentPackError) as caught:
                self.sync(client, scope="user")
            self.assertEqual(str(caught.exception), f"--scope user|project is supported for the claude client only: "
                                                    f"{client} has no project-level write yet")
        with self.assertRaises(core.AgentPackError) as caught:
            self.sync("cursor", replace_mcp=True)
        self.assertIn("(codex also accepts --replace-mcp for its native remote MCP entries)", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
