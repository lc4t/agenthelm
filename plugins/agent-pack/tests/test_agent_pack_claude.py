"""Claude Code: dedicated package sync / reconcile, kept local credentials, component
selection for bootstrap, and client drift in doctor. Everything runs in a temporary HOME.
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_pack_config import (  # noqa: E402
    AGENT_PLUGIN_MCP_SCHEMA,
    PROJECT_PROFILE_NAMESPACE,
    AgentPackError,
    doctor,
    install_plugin,
    main,
    reconcile_project,
    sync_project,
)
from mcp_server import TOOLS  # noqa: E402
from test_agent_pack_config import make_project, write_json  # noqa: E402

SECRET = "Bearer local-only-value"


class ClaudeCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.config_path = self.home / ".claude.json"
        self.project = make_project(self.root / "notes-lab", "notes-lab")
        self.set_mcps({
            "local-check": {"type": "stdio", "command": "python3", "args": ["-m", "checker"]},
            "docs-search": {"type": "streamable-http", "url": "https://docs.example.test/mcp"},
            "chat-bridge": {"type": "stdio", "command": "chat-bridge"},
        })
        self.set_policy({"chat-bridge": {"exclude": ["claude"]}})

    def set_mcps(self, servers: dict) -> None:
        write_json(self.project / ".agents/mcp.json", {"$schema": AGENT_PLUGIN_MCP_SCHEMA, "mcpServers": servers})

    def set_policy(self, policy: dict, **extra: object) -> None:
        path = self.project / ".agents" / PROJECT_PROFILE_NAMESPACE / "project.json"
        profile = json.loads(path.read_text(encoding="utf-8"))
        profile["capabilities"]["mcp_client_policy"] = policy
        profile["capabilities"].update(extra)
        write_json(path, profile)

    def config(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def sync(self, **options: object) -> dict:
        return sync_project(self.project, "claude", home=self.home, recovery_root=self.root / "recovery", **options)

    def actions(self, result: dict) -> dict[str, str]:
        return {item["name"]: item["action"] for item in result["operations"]
                if item["action"].endswith(("-mcp", "-skill"))}


class SyncTests(ClaudeCase):
    def test_preview_then_apply_builds_a_claude_package(self) -> None:
        write_json(self.config_path, {"theme": "dark", "mcpServers": {"my-tool": {"type": "stdio", "command": "mine"}}})
        preview = self.sync()
        self.assertFalse(preview["applied"])
        self.assertEqual(self.actions(preview), {"sample-skill": "link-skill", "local-check": "add-mcp",
                                                 "docs-search": "add-mcp"})
        self.assertFalse((self.home / ".claude/agent-pack").exists(), "预览不写入")
        result = self.sync(apply=True)
        package = self.home / ".claude/agent-pack/packages/notes-lab"
        record = self.home / ".claude/agent-pack/plugins/notes-lab"
        self.assertTrue(package.is_dir() and not package.is_symlink())
        self.assertEqual(record.resolve(), package.resolve())
        self.assertEqual((self.home / ".claude/skills/sample-skill").resolve(), (package / "skills/sample-skill").resolve())
        servers = self.config()["mcpServers"]
        self.assertEqual(sorted(servers), ["docs-search", "local-check", "my-tool"])
        self.assertEqual(servers["docs-search"]["type"], "http")
        self.assertEqual(self.config()["theme"], "dark")
        self.assertEqual(result["native"], ["my-tool"])
        again = self.sync()
        self.assertTrue(all(item["action"].startswith("keep-") for item in again["operations"]), again)

    def test_project_changes_add_and_remove_only_owned_entries(self) -> None:
        write_json(self.config_path, {"mcpServers": {"my-tool": {"type": "stdio", "command": "mine"}}})
        self.sync(apply=True)
        self.set_mcps({
            "local-check": {"type": "stdio", "command": "python3", "args": ["-m", "checker"]},
            "calendar": {"type": "streamable-http", "url": "https://calendar.example.test/mcp"},
        })
        preview = self.sync()
        self.assertEqual(self.actions(preview)["calendar"], "add-mcp")
        self.assertEqual(self.actions(preview)["docs-search"], "remove-mcp")
        result = self.sync(apply=True)
        servers = self.config()["mcpServers"]
        self.assertEqual(sorted(servers), ["calendar", "local-check", "my-tool"], "用户自己的条目不动")
        removed = json.loads((Path(result["recovery"]) / "replaced-mcp.json").read_text(encoding="utf-8"))
        self.assertIn("docs-search", removed["mcpServers"])

    def test_local_credentials_survive_updates(self) -> None:
        self.sync(apply=True)
        config = self.config()
        config["mcpServers"]["docs-search"]["headers"] = {"Authorization": SECRET}
        write_json(self.config_path, config)
        kept = self.sync()
        self.assertEqual(self.actions(kept)["docs-search"], "keep-override-mcp")
        self.assertEqual(kept["overrides"], [{"name": "docs-search", "keys": ["headers.Authorization"]}])
        self.assertNotIn(SECRET, json.dumps(kept), "结果里只出现键名，不出现值")
        self.set_mcps({
            "local-check": {"type": "stdio", "command": "python3", "args": ["-m", "checker"]},
            "docs-search": {"type": "streamable-http", "url": "https://docs.example.test/v2/mcp"},
        })
        result = self.sync(apply=True)
        entry = self.config()["mcpServers"]["docs-search"]
        self.assertEqual(entry["url"], "https://docs.example.test/v2/mcp")
        self.assertEqual(entry["headers"], {"Authorization": SECRET}, "项目更新后仍保留本机认证")
        self.assertNotIn(SECRET, json.dumps(result))
        reset = self.sync(apply=True, reset_overrides=True)
        self.assertNotIn("headers", self.config()["mcpServers"]["docs-search"])
        archived = Path(reset["recovery"]) / "replaced-mcp.json"
        self.assertEqual(json.loads(archived.read_text())["mcpServers"]["docs-search"]["headers"]["Authorization"], SECRET)
        self.assertEqual(archived.stat().st_mode & 0o777, 0o600)

    def test_policy_exclusion_is_respected(self) -> None:
        write_json(self.config_path, {"mcpServers": {"chat-bridge": {"type": "stdio", "command": "my-own-bridge"}}})
        result = self.sync(apply=True)
        self.assertEqual(self.config()["mcpServers"]["chat-bridge"]["command"], "my-own-bridge")
        self.assertEqual(result["excluded"], ["chat-bridge"])
        self.set_policy({"docs-search": {"exclude": ["claude"]}, "chat-bridge": {"exclude": ["claude"]}})
        preview = self.sync()
        removal = next(item for item in preview["operations"] if item["name"] == "docs-search")
        self.assertEqual((removal["action"], removal["reason"]), ("remove-mcp", "excluded for claude"))

    def test_entries_the_package_did_not_install_need_replace(self) -> None:
        own_skill = self.home / ".claude/skills/sample-skill"
        own_skill.mkdir(parents=True)
        (own_skill / "SKILL.md").write_text("# mine\n", encoding="utf-8")
        write_json(self.config_path, {"mcpServers": {"local-check": {
            "type": "stdio", "command": "old-checker", "env": {"CHECKER_TOKEN": "local-token"}}}})
        with self.assertRaises(AgentPackError):
            self.sync(apply=True)
        self.assertFalse(own_skill.is_symlink())
        result = self.sync(apply=True, replace=True)
        entry = self.config()["mcpServers"]["local-check"]
        self.assertEqual(entry["command"], "python3")
        self.assertEqual(entry["env"], {"CHECKER_TOKEN": "local-token"}, "替换时也保留本机凭据")
        recovery = Path(result["recovery"])
        self.assertEqual((recovery / "replaced-skills/sample-skill/SKILL.md").read_text(), "# mine\n")
        self.assertEqual(json.loads((recovery / "replaced-mcp.json").read_text())["mcpServers"]["local-check"]["command"],
                         "old-checker")

    def test_failure_rolls_back_config_links_and_package(self) -> None:
        write_json(self.config_path, {"mcpServers": {"my-tool": {"type": "stdio", "command": "mine"}}})
        before = self.config_path.read_text(encoding="utf-8")
        import agent_pack_claude

        original = agent_pack_claude.core._write_json_atomic

        def failing(path, value, mode=0o644):
            if Path(path).name.endswith(".json") and "state" in Path(path).parts:
                raise OSError("disk full")
            return original(path, value, mode=mode)

        agent_pack_claude.core._write_json_atomic = failing
        try:
            with self.assertRaises(AgentPackError):
                self.sync(apply=True)
        finally:
            agent_pack_claude.core._write_json_atomic = original
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertFalse((self.home / ".claude/skills/sample-skill").exists())
        self.assertFalse((self.home / ".claude/agent-pack/packages/notes-lab").exists())

    def test_reconcile_claude_retires_selected_skill(self) -> None:
        legacy = self.home / ".claude/skills/old-helper"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("---\nname: old-helper\ndescription: old\n---\n", encoding="utf-8")
        result = reconcile_project(self.project, "claude", home=self.home, retire_skills=["old-helper"],
                                   recovery_root=self.root / "recovery", apply=True)
        self.assertFalse(legacy.exists())
        self.assertTrue((Path(result["recovery"]) / "skills/old-helper/SKILL.md").is_file())
        self.assertIn("local-check", self.config()["mcpServers"])


class LegacyInstallTests(ClaudeCase):
    def test_install_linked_to_the_codex_package_is_migrated(self) -> None:
        (self.project / ".env.local").write_text("SEARCH_API_KEY=placeholder\n", encoding="utf-8")
        (self.project / ".env.local").chmod(0o600)
        self.set_policy({}, credential_env_file=".env.local")
        self.set_mcps({
            "local-check": {"type": "stdio", "command": "python3", "args": ["-m", "checker"]},
            "search-api": {"type": "stdio", "command": "search-api", "env": {"API_KEY": "${SEARCH_API_KEY}"}},
        })
        sync_project(self.project, "codex", home=self.home, apply=True)
        codex_package = self.home / ".agents/plugins/plugins/notes-lab"
        install_plugin(codex_package, "claude", home=self.home, apply=True)
        report = doctor(self.project, ["claude"], self.home)
        self.assertFalse(report["ok"])
        self.assertEqual(report["clients"]["claude"]["coupled"], str(codex_package))
        result = self.sync(apply=True)
        self.assertEqual(result["migrated_from"], str(codex_package))
        package = self.home / ".claude/agent-pack/packages/notes-lab"
        self.assertEqual((self.home / ".claude/agent-pack/plugins/notes-lab").resolve(), package.resolve())
        self.assertEqual((self.home / ".claude/skills/sample-skill").resolve(), (package / "skills/sample-skill").resolve())
        shutil.rmtree(codex_package)  # Codex 端的包消失，Claude 不受影响
        after = doctor(self.project, ["claude"], self.home)
        self.assertTrue(after["ok"], after)
        args = self.config()["mcpServers"]["search-api"]["args"]
        launcher_config = Path(args[args.index("--config") + 1])
        self.assertTrue(launcher_config.is_file(), "启动器配置在 Claude 自己的包里")


class BootstrapSelectionTests(ClaudeCase):
    def plugin(self) -> Path:
        plugin = self.root / "plugin-src"
        shutil.copytree(self.project / ".agents", plugin)
        return plugin

    def test_single_mcp_install_keeps_local_auth(self) -> None:
        plugin = self.plugin()
        write_json(self.config_path, {"mcpServers": {"docs-search": {
            "type": "http", "url": "https://docs.example.test/mcp", "headers": {"Authorization": SECRET}}}})
        preview = install_plugin(plugin, "claude", home=self.home, mcps=["docs-search"])
        actions = {item["name"]: item["action"] for item in preview["operations"]}
        self.assertEqual(actions, {"notes-lab": "link-plugin", "docs-search": "keep-override-mcp"})
        install_plugin(plugin, "claude", home=self.home, mcps=["docs-search"], apply=True)
        self.assertEqual(self.config()["mcpServers"]["docs-search"]["headers"]["Authorization"], SECRET)
        self.assertFalse((self.home / ".claude/skills/sample-skill").exists(), "没选的 Skill 不装")

    def test_replace_keeps_auth_unless_reset(self) -> None:
        plugin = self.plugin()
        write_json(self.config_path, {"mcpServers": {"docs-search": {
            "type": "http", "url": "https://old.example.test/mcp", "headers": {"Authorization": SECRET}}}})
        with self.assertRaises(AgentPackError):
            install_plugin(plugin, "claude", home=self.home, mcps=["docs-search"], apply=True)
        install_plugin(plugin, "claude", home=self.home, mcps=["docs-search"], apply=True, replace=True,
                       recovery_root=self.root / "recovery")
        entry = self.config()["mcpServers"]["docs-search"]
        self.assertEqual((entry["url"], entry["headers"]["Authorization"]), ("https://docs.example.test/mcp", SECRET))
        install_plugin(plugin, "claude", home=self.home, mcps=["docs-search"], apply=True, replace=True,
                       reset_overrides=True, recovery_root=self.root / "recovery")
        self.assertNotIn("headers", self.config()["mcpServers"]["docs-search"])

    def test_unknown_selection_and_other_clients_are_rejected(self) -> None:
        plugin = self.plugin()
        with self.assertRaises(AgentPackError):
            install_plugin(plugin, "claude", home=self.home, mcps=["nope"])
        with self.assertRaises(AgentPackError):
            install_plugin(plugin, "claude", home=self.home, skills=["nope"])
        with self.assertRaises(AgentPackError):
            install_plugin(plugin, "codex", home=self.home, mcps=["docs-search"])


class DoctorDriftTests(ClaudeCase):
    def test_reports_missing_override_native_and_stale(self) -> None:
        self.assertNotIn("clients", doctor(self.project), "不加 --client 时行为不变")
        self.sync(apply=True)
        config = self.config()
        del config["mcpServers"]["local-check"]
        config["mcpServers"]["docs-search"]["headers"] = {"Authorization": SECRET}
        config["mcpServers"]["my-tool"] = {"type": "stdio", "command": "mine"}
        write_json(self.config_path, config)
        report = doctor(self.project, ["claude"], self.home)
        claude = report["clients"]["claude"]
        self.assertFalse(report["ok"])
        self.assertEqual(claude["missing"], ["mcp local-check"])
        self.assertEqual(claude["overrides"], [{"name": "docs-search", "keys": ["headers.Authorization"]}])
        self.assertEqual(claude["native"], ["my-tool"])
        self.assertIn("claude: missing mcp local-check", report["issues"])
        self.assertTrue(any("kept local override docs-search" in item for item in report["warnings"]))
        self.assertNotIn(SECRET, json.dumps(report))

    def test_codex_package_drift(self) -> None:
        report = doctor(self.project, ["codex"], self.home)["clients"]["codex"]
        self.assertIn("package notes-lab", report["missing"])
        sync_project(self.project, "codex", home=self.home, apply=True)
        self.assertTrue(doctor(self.project, ["codex"], self.home)["clients"]["codex"]["in_sync"])
        self.set_mcps({
            "local-check": {"type": "stdio", "command": "python3", "args": ["-m", "checker", "--strict"]},
            "docs-search": {"type": "streamable-http", "url": "https://docs.example.test/mcp"},
            "chat-bridge": {"type": "stdio", "command": "chat-bridge"},
            "calendar": {"type": "streamable-http", "url": "https://calendar.example.test/mcp"},
        })
        report = doctor(self.project, ["codex"], self.home)["clients"]["codex"]
        self.assertEqual((report["missing"], report["different"]), (["mcp calendar"], ["mcp local-check"]))


class InterfaceTests(ClaudeCase):
    def test_cli_and_mcp_tools_accept_claude(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--output", "json", "sync", "--project", str(self.project), "--client", "claude",
                         "--home", str(self.home)])
        self.assertEqual(code, 0, output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["applied"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--output", "json", "doctor", "--project", str(self.project),
                                   "--client", "claude", "--home", str(self.home)]), 1)
        schemas = {tool["name"]: tool["inputSchema"]["properties"] for tool in TOOLS}
        self.assertIn("claude", schemas["agent_pack_sync"]["client"]["enum"])
        self.assertIn("claude", schemas["agent_pack_reconcile"]["client"]["enum"])
        self.assertIn("clients", schemas["agent_pack_doctor"])
        self.assertIn("mcps", schemas["agent_pack_bootstrap"])


if __name__ == "__main__":
    unittest.main()
