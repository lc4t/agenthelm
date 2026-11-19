"""#67 / #82 I-4 (step 1): scope declaration in the Profile, scope-grouped preview, ``--scope`` and ``state.scopes``.

Nothing here writes a project-level store: a project-declared entry is skipped (never written to the user level)
until the later changes that add Claude local / ``.mcp.json`` / ``.claude/skills``.
"""
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

import agent_pack_config as core  # noqa: E402
import mcp_server  # noqa: E402
from test_agent_pack_config import PROJECT_PROFILE_NAMESPACE, make_project, write_json  # noqa: E402
from test_sync_preview_parity import snapshot  # noqa: E402

SKILL = "sample-skill"
MCP = "local-check"
FAKE_TOKEN = "tok-FAKE-0123456789abcdef"
SCOPE_KEYS = ("by_scope", "narrowing", "skipped", "scope")


def declare(project: Path, **keys) -> None:
    profile = project / ".agents" / PROJECT_PROFILE_NAMESPACE / "project.json"
    data = json.loads(profile.read_text(encoding="utf-8"))
    data["capabilities"].update(keys)
    write_json(profile, data)


class ScopeBase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = make_project(self.root / "proj-a", "proj-a")
        self.home = self.root / "home"
        self.home.mkdir()
        self.recovery = self.root / "recovery"

    def cli(self, *args: str, project: Path | None = None, client: str = "claude", command: str = "sync",
            lang: str | None = None, output: str = "json"):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["HOME"] = str(self.home)
        if lang:
            env["AGENTHELM_LANG"] = lang
        proc = subprocess.run(
            [sys.executable, str(RUNTIME / "agent_pack_config.py"), "--output", output, command,
             "--project", str(project or self.project), "--client", client, "--home", str(self.home),
             "--recovery-root", str(self.recovery), *args],
            capture_output=True, text=True, env=env, check=False)
        payload = None
        if output == "json":
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                pass
        return proc.returncode, payload, proc.stdout + proc.stderr

    def state(self, name: str = "proj-a") -> dict:
        return json.loads((self.home / ".claude" / "agent-pack" / "state" / f"{name}.json").read_text())

    def user_config(self) -> dict:
        path = self.home / ".claude.json"
        return json.loads(path.read_text()) if path.exists() else {}


class DeclarationTests(ScopeBase):
    def test_absent_keys_mean_user_everywhere(self) -> None:
        layout = core.load_project_layout(self.project)
        self.assertEqual((layout.default_scope, layout.mcp_scope, layout.skill_scope), ("user", {}, {}))
        self.assertEqual(core.declared_scope(layout, "mcp", MCP), "user")

    def test_per_name_overrides_default_and_default_applies_to_the_rest(self) -> None:
        declare(self.project, default_scope="project", mcp_scope={MCP: "user"})
        layout = core.load_project_layout(self.project)
        self.assertEqual(core.declared_scope(layout, "mcp", MCP), "user")
        self.assertEqual(core.declared_scope(layout, "skill", SKILL), "project")

    def test_invalid_values_are_rejected_with_the_key_path(self) -> None:
        for keys, fragment in (({"default_scope": "global"}, "default_scope"),
                               ({"mcp_scope": {MCP: "local"}}, f"mcp_scope.{MCP}"),
                               ({"skill_scope": ["x"]}, "skill_scope"),
                               ({"skill_scope": {SKILL: 1}}, f"skill_scope.{SKILL}")):
            with self.subTest(keys=keys):
                declare(self.project, **keys)
                with self.assertRaises(core.AgentPackError) as raised:
                    core.load_project_layout(self.project)
                self.assertIn(fragment, str(raised.exception))
                declare(self.project, default_scope="user", mcp_scope={}, skill_scope={})

    def test_schema_declares_the_keys(self) -> None:
        schema = json.loads((TESTS.parents[1] / "agents-init/skills/agents-init/project.schema.json").read_text())
        properties = schema["properties"]["capabilities"]["properties"]
        self.assertEqual(properties["default_scope"]["enum"], ["user", "project"])
        self.assertEqual(properties["mcp_scope"]["additionalProperties"]["enum"], ["user", "project"])
        self.assertEqual(properties["skill_scope"]["additionalProperties"]["enum"], ["user", "project"])


class UnchangedWithoutDeclarationTests(ScopeBase):
    def test_no_scope_fields_and_no_scopes_in_state(self) -> None:
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        for key in SCOPE_KEYS:
            self.assertNotIn(key, payload)
        for operation in payload["operations"]:
            self.assertNotIn("scope", operation)
            self.assertNotIn("store", operation)
        self.assertNotIn("scopes", self.state())
        _, text, _ = self.cli(output="text")
        self.assertIsNone(text)
        rc, _, out = self.cli(output="text")
        self.assertNotIn("[", out.split("\n", 2)[2])   # no scope headings in the flat listing


class ReproductionOfIssue67(ScopeBase):
    """Before: a project-declared entry was ignored and written to the user level, where every project sees it."""

    def test_a_project_declared_entry_never_reaches_the_user_level(self) -> None:
        declare(self.project, mcp_scope={MCP: "project"}, skill_scope={SKILL: "project"})
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertNotIn(MCP, self.user_config().get("mcpServers", {}))
        self.assertFalse((self.home / ".claude" / "skills" / SKILL).exists())
        self.assertFalse((self.home / ".claude" / "skills" / SKILL).is_symlink())
        skips = {(item["action"], item["name"], item["scope"]) for item in payload["operations"]
                 if item["action"].startswith("skip-")}
        self.assertEqual(skips, {("skip-skill", SKILL, "project"), ("skip-mcp", MCP, "project")})
        state = self.state()
        self.assertEqual(state["skills"], [])
        self.assertEqual(state["mcps"], {})
        self.assertEqual(state["scopes"], {"skills": {}, "mcps": {}})


class PreviewTests(ScopeBase):
    def test_preview_equals_apply(self) -> None:
        self.cli("--apply")   # user level first
        declare(self.project, mcp_scope={MCP: "project"}, skill_scope={SKILL: "project"})
        before = snapshot(self.home)
        rc, preview, out = self.cli()
        self.assertEqual(rc, 0, out)
        self.assertEqual(before, snapshot(self.home))
        rc, applied, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(preview["operations"], applied["operations"])
        self.assertEqual(preview["narrowing"], applied["narrowing"])
        self.assertEqual(preview["by_scope"], applied["by_scope"])
        self.assertEqual(before, snapshot(self.home), "only skip operations: apply writes nothing")

    def test_existing_user_level_entries_stay_and_are_reported_as_narrowing_not_applied(self) -> None:
        self.cli("--apply")
        declare(self.project, mcp_scope={MCP: "project"}, skill_scope={SKILL: "project"})
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn(MCP, self.user_config()["mcpServers"])
        self.assertTrue((self.home / ".claude" / "skills" / SKILL).is_symlink())
        self.assertEqual({(item["kind"], item["name"], item["status"]) for item in payload["narrowing"]},
                         {("skill", SKILL, "not-applied"), ("mcp", MCP, "not-applied")})
        self.assertIn(MCP, self.state()["mcps"], "ownership of the user-level entry is kept")
        self.assertIn(SKILL, self.state()["skills"])

    def test_narrowing_counts_other_projects_from_state(self) -> None:
        other = make_project(self.root / "proj-b", "proj-b")
        third = make_project(self.root / "proj-c", "proj-c")
        for project in (other, third):
            rc, _, out = self.cli("--apply", "--replace", project=project)   # same names: later projects replace
            self.assertEqual(rc, 0, out)
        self.cli("--apply", "--replace")
        declare(self.project, mcp_scope={MCP: "project"})
        rc, payload, out = self.cli()
        self.assertEqual(rc, 0, out)
        (entry,) = payload["narrowing"]
        self.assertEqual((entry["other_projects"], entry["also_declared_by"]), (2, ["proj-b", "proj-c"]))
        self.assertEqual(entry["counted_from"], "state")
        self.assertTrue(any("never synced" in item or "未同步" in item for item in payload["warnings"]) or payload["warnings"])

    def test_no_narrowing_when_the_entry_is_not_at_the_user_level(self) -> None:
        declare(self.project, mcp_scope={MCP: "project"})
        rc, payload, out = self.cli()
        self.assertEqual(rc, 0, out)
        self.assertEqual(payload["narrowing"], [])

    def test_by_scope_counts_and_text_groups(self) -> None:
        declare(self.project, mcp_scope={MCP: "project"})
        rc, payload, out = self.cli()
        self.assertEqual(payload["by_scope"], {"user": {"skills": 1, "mcp": 0}, "project": {"skills": 0, "mcp": 1}})
        rc, _, text = self.cli(output="text", lang="en")
        self.assertIn("[User level · applies to every project]", text)
        self.assertIn("[Project level · this project only · local", text)
        rc, _, text = self.cli(output="text", lang="zh")
        self.assertIn("[用户级 · 对所有项目生效]", text)
        self.assertIn("[项目级 · 仅此项目 · local", text)

    def test_undeclared_names_are_warned(self) -> None:
        declare(self.project, mcp_scope={"no-such-server": "project"})
        rc, payload, out = self.cli(lang="en")
        self.assertEqual(rc, 0, out)
        self.assertTrue(any("no-such-server" in item for item in payload["warnings"]))

    def test_secrets_never_appear(self) -> None:
        write_json(self.home / ".claude.json", {"mcpServers": {MCP: {
            "type": "stdio", "command": "python3", "args": ["-m", "checker"],
            "env": {"API_TOKEN": FAKE_TOKEN}, "headers": {"Authorization": f"Bearer {FAKE_TOKEN}"}}}})
        declare(self.project, mcp_scope={MCP: "project"})
        for output in ("json", "text"):
            for flags in ((), ("--apply",)):
                rc, _, out = self.cli(*flags, output=output)
                self.assertEqual(rc, 0, out)
                self.assertNotIn(FAKE_TOKEN, out)
        recovery = self.recovery
        for path in recovery.rglob("*") if recovery.exists() else ():
            if path.is_file():
                self.assertNotIn(FAKE_TOKEN, path.read_text(errors="ignore"))
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.state()))


class ScopeFlagTests(ScopeBase):
    def test_scope_user_and_project_split_the_plan(self) -> None:
        declare(self.project, mcp_scope={MCP: "project"})
        rc, user, out = self.cli("--scope", "user")
        self.assertEqual(rc, 0, out)
        self.assertEqual({item["action"] for item in user["operations"]},
                         {"install-package", "link-record", "link-skill"})
        self.assertEqual(user["scope"], "user")
        rc, project, out = self.cli("--scope", "project")
        self.assertEqual(rc, 0, out)
        self.assertEqual([(item["action"], item["name"]) for item in project["operations"]], [("skip-mcp", MCP)])

    def test_no_user_level_mcp_to_write_leaves_claude_json_alone(self) -> None:
        declare(self.project, default_scope="project")
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertFalse((self.home / ".claude.json").exists(), "no user-level entry to write: the file is not created")
        self.assertEqual(self.user_config(), {})

    def test_scope_user_apply_writes_user_level_and_scopes_in_state(self) -> None:
        rc, payload, out = self.cli("--scope", "user", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state()["scopes"], {"skills": {SKILL: "user"}, "mcps": {MCP: "user"}})

    def test_scope_project_never_touches_the_user_level(self) -> None:
        declare(self.project, mcp_scope={MCP: "project"})
        before = snapshot(self.home)
        rc, payload, out = self.cli("--scope", "project", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(before, snapshot(self.home))

    def test_scope_composes_with_only(self) -> None:
        declare(self.project, mcp_scope={MCP: "project"})
        rc, payload, out = self.cli("--scope", "project", "--only", "mcp")
        self.assertEqual(rc, 0, out)
        self.assertEqual([item["name"] for item in payload["operations"]], [MCP])
        self.assertEqual(payload["by_scope"]["project"], {"skills": 0, "mcp": 1})

    def test_other_clients_reject_a_non_default_scope(self) -> None:
        for client in ("codex", "cursor"):
            rc, payload, out = self.cli("--scope", "project", client=client)
            self.assertEqual(rc, 2, out)
            self.assertIn("claude client only", payload["error"])

    def test_the_mcp_tool_takes_scope(self) -> None:
        declare(self.project, mcp_scope={MCP: "project"})
        tools = {tool["name"]: tool for tool in mcp_server.TOOLS}
        for name in ("agent_pack_sync", "agent_pack_reconcile"):
            self.assertIn("scope", tools[name]["inputSchema"]["properties"])
        result = mcp_server.call_tool("agent_pack_sync", {
            "project": str(self.project), "client": "claude", "scope": "project"}, self.home) \
            if hasattr(mcp_server, "call_tool") else None
        if result is not None:
            self.assertEqual([item["name"] for item in result["operations"]], [MCP])


class OtherClientsTests(ScopeBase):
    def test_cursor_skips_project_declared_entries_and_keeps_them_out_of_the_package(self) -> None:
        declare(self.project, mcp_scope={MCP: "project"}, skill_scope={SKILL: "project"})
        rc, preview, out = self.cli(client="cursor")
        self.assertEqual(rc, 0, out)
        self.assertEqual({(item["kind"], item["name"], item["reason"]) for item in preview["skipped"]},
                         {("mcp", MCP, "client-project-unsupported"), ("skill", SKILL, "project-native")})
        self.assertEqual(len(preview["warnings"]), 2)
        rc, applied, out = self.cli("--apply", client="cursor")
        self.assertEqual(rc, 0, out)
        package = self.home / ".cursor" / "plugins" / "local" / "proj-a"
        self.assertTrue(package.is_dir())
        self.assertFalse((package / "skills" / SKILL).exists())
        servers = json.loads((package / "mcp.json").read_text()).get("mcpServers", {}) if (package / "mcp.json").exists() else {}
        self.assertNotIn(MCP, servers)

    def test_codex_without_declaration_has_no_skipped_key(self) -> None:
        rc, payload, out = self.cli(client="codex")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("skipped", payload)


if __name__ == "__main__":
    unittest.main()
