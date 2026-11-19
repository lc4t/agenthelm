"""#67 / #82 I-4 (step 2): project-level MCP written to Claude local (``~/.claude.json`` under the project's path)
or to the project file ``.mcp.json``; never to the user level, never with a value in output or metadata."""
from __future__ import annotations

import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(RUNTIME))

import agent_pack_claude as claude  # noqa: E402
import agent_pack_config as core  # noqa: E402
from test_agent_pack_config import write_json  # noqa: E402
from test_scope_declaration import FAKE_TOKEN, MCP, SKILL, ScopeBase, declare  # noqa: E402
from test_sync_preview_parity import snapshot  # noqa: E402


class ProjectStoreBase(ScopeBase):
    def setUp(self) -> None:
        super().setUp()
        declare(self.project, mcp_scope={MCP: "project"})

    @property
    def config_path(self) -> Path:
        return self.home / ".claude.json"

    def local(self, project: Path | None = None) -> dict:
        return self.user_config().get("projects", {}).get(str(project or self.project), {}).get("mcpServers", {})

    def set_mcp(self, entry: dict) -> None:
        write_json(self.project / ".agents" / "mcp.json", {
            "$schema": json.loads((self.project / ".agents" / "mcp.json").read_text())["$schema"],
            "mcpServers": {MCP: entry}})


class LocalStoreTests(ProjectStoreBase):
    def test_entry_lands_under_the_project_path_and_nowhere_else(self) -> None:
        other = self.root / "proj-b"
        other.mkdir()
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.local()[MCP]["command"], "python3")
        config = self.user_config()
        self.assertNotIn(MCP, config.get("mcpServers", {}))
        self.assertEqual(self.local(other), {}, "another project does not see it")
        self.assertFalse((self.project / ".mcp.json").exists())
        op = next(item for item in payload["operations"] if item["action"] == "add-project-mcp")
        self.assertEqual((op["scope"], op["store"]), ("project", "claude-local"))
        state = self.state()
        self.assertEqual(state["scopes"]["mcps"], {MCP: "project:local"})
        self.assertNotIn(MCP, state["mcps"], "the pre-existing mcps key stays user-level only")
        self.assertIn(MCP, state["scopes"]["project_hashes"])
        self.assertEqual(payload["by_scope"]["project"]["mcp"], 1)

    def test_other_keys_and_permissions_are_kept_and_the_original_is_backed_up(self) -> None:
        original = {"numStartups": 7, "mcpServers": {"mine": {"type": "stdio", "command": "node"}},
                    "projects": {str(self.project): {"allowedTools": ["Bash"], "mcpServers": {
                        "theirs": {"type": "stdio", "command": "deno", "env": {"API_TOKEN": FAKE_TOKEN}}}}}}
        write_json(self.config_path, original)
        os.chmod(self.config_path, 0o640)
        before_bytes = self.config_path.read_bytes()
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        config = self.user_config()
        self.assertEqual(config["numStartups"], 7)
        self.assertEqual(config["mcpServers"], original["mcpServers"])
        section = config["projects"][str(self.project)]
        self.assertEqual(section["allowedTools"], ["Bash"])
        self.assertEqual(section["mcpServers"]["theirs"], original["projects"][str(self.project)]["mcpServers"]["theirs"])
        self.assertIn(MCP, section["mcpServers"])
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o640)
        backup = Path(payload["recovery"]) / "claude.json.before"
        self.assertEqual(backup.read_bytes(), before_bytes)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        for path in Path(payload["recovery"]).rglob("*"):
            if path.is_file() and path != backup:
                self.assertNotIn(FAKE_TOKEN, path.read_text(errors="ignore"), path.name)
        self.assertNotIn(FAKE_TOKEN, out)

    def test_preview_writes_nothing_and_equals_apply(self) -> None:
        before = snapshot(self.home)
        rc, preview, out = self.cli()
        self.assertEqual(rc, 0, out)
        self.assertEqual(before, snapshot(self.home))
        rc, applied, out = self.cli("--apply")
        self.assertEqual(preview["operations"], applied["operations"])
        changed = {name for name, value in snapshot(self.home).items() if before.get(name) != value}
        self.assertIn(".claude.json", changed)
        rc, again, out = self.cli("--apply")
        self.assertEqual([item["action"] for item in again["operations"] if item["name"] == MCP], ["keep-project-mcp"])
        self.assertTrue(again["applied"])

    def test_failure_rolls_the_file_back_byte_for_byte(self) -> None:
        write_json(self.config_path, {"numStartups": 1, "projects": {}})
        os.chmod(self.config_path, 0o640)
        original = self.config_path.read_bytes()
        real = core._write_json_atomic

        def failing(path, value, *, mode=0o644):
            if Path(path).parent.name == "state":
                raise OSError("simulated failure")
            return real(path, value, mode=mode)

        with mock.patch.object(core, "_write_json_atomic", failing):
            with self.assertRaises(core.AgentPackError) as raised:
                claude.sync_project(self.project, home=self.home, apply=True, recovery_root=self.recovery)
        self.assertIn("rolled back", str(raised.exception))
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o640)

    def test_unrecognized_structure_skips_and_never_falls_back_to_user(self) -> None:
        write_json(self.config_path, {"projects": ["not", "an", "object"]})
        original = self.config_path.read_bytes()
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.config_path.read_bytes(), original)
        (item,) = payload["skipped"]
        self.assertEqual((item["name"], item["reason"]), (MCP, "local-structure-unrecognized"))
        self.assertTrue(any(op["action"] == "skip-mcp" for op in payload["operations"]))
        self.assertNotIn(MCP, self.user_config().get("mcpServers", {}))
        self.assertNotIn(MCP, self.state()["mcps"])

    def test_unwritable_target_skips_with_a_warning(self) -> None:
        with mock.patch.object(claude.os, "access", return_value=False):
            result = claude.sync_project(self.project, home=self.home, apply=True, recovery_root=self.recovery)
        (item,) = result["skipped"]
        self.assertEqual(item["reason"], "target-not-writable")
        self.assertFalse(self.config_path.exists())

    def test_conflict_needs_replace_and_keeps_local_credentials(self) -> None:
        write_json(self.config_path, {"projects": {str(self.project): {"mcpServers": {
            MCP: {"type": "stdio", "command": "node", "env": {"API_TOKEN": FAKE_TOKEN}}}}}})
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"project-mcp {MCP}", payload["error"])
        self.assertNotIn(FAKE_TOKEN, out)
        rc, payload, out = self.cli("--apply", "--replace-mcp")
        self.assertEqual(rc, 0, out)
        self.assertEqual(payload["conflicts"], {"project_mcp": 1})
        self.assertNotIn(FAKE_TOKEN, out)
        entry = self.local()[MCP]
        self.assertEqual(entry["command"], "python3")
        self.assertEqual(entry["env"], {"API_TOKEN": FAKE_TOKEN}, "local credential values survive")
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.state()))

    def test_removing_the_server_from_the_project_removes_only_the_managed_local_entry(self) -> None:
        write_json(self.config_path, {"mcpServers": {MCP: {"type": "stdio", "command": "mine"}}})
        rc, _, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        write_json(self.project / ".agents" / "mcp.json", {
            "$schema": json.loads((self.project / ".agents" / "mcp.json").read_text())["$schema"], "mcpServers": {}})
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn(("remove-project-mcp", MCP), {(item["action"], item["name"]) for item in payload["operations"]})
        self.assertNotIn(MCP, self.local())
        self.assertEqual(self.user_config()["mcpServers"][MCP]["command"], "mine", "the user's own entry is never touched")

    def test_project_and_user_entries_are_owned_separately(self) -> None:
        declare(self.project, mcp_scope={})
        rc, _, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn(MCP, self.user_config()["mcpServers"])
        declare(self.project, mcp_scope={MCP: "project"})
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn(MCP, self.user_config()["mcpServers"], "the old user-level entry is not retired automatically")
        self.assertIn(MCP, self.local())
        (note,) = payload["narrowing"]
        self.assertEqual(note["manual"], f"claude mcp remove --scope user {MCP}")
        self.assertEqual(self.state()["scopes"]["mcps"], {MCP: "project:local"})

    def test_scope_user_keeps_project_ownership_in_state(self) -> None:
        self.cli("--apply")
        rc, _, out = self.cli("--scope", "user", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state()["scopes"]["mcps"], {MCP: "project:local"})
        self.assertIn(MCP, self.local())

    def test_non_claude_clients_reject_project_store(self) -> None:
        rc, payload, out = self.cli("--project-store", "file", client="cursor")
        self.assertEqual(rc, 2, out)
        self.assertIn("claude client only", payload["error"])


def older_runtime_runs(test: "ScopeBase") -> None:
    """Run ``sync``, ``reconcile`` and ``doctor`` of the latest released runtime on the test's project: they must not
    fail, and must not remove anything the project-level scope recorded (state keeps it only under ``scopes``)."""
    import subprocess
    import tempfile
    base = Path(os.environ["AGENTHELM_OLD_RUNTIME"]) if os.environ.get("AGENTHELM_OLD_RUNTIME") else None
    if base is None:
        root = TESTS.parents[2]
        tag = subprocess.run(["git", "-C", str(root), "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
                             capture_output=True, text=True, check=False).stdout.strip()
        if not tag:
            test.skipTest("no release tag to take an older runtime from")
        scratch = Path(tempfile.mkdtemp(dir=test.root))
        archive = subprocess.run(["git", "-C", str(root), "archive", tag, "plugins/agent-pack/runtime"],
                                 capture_output=True, check=False)
        if archive.returncode:
            test.skipTest("cannot read the tagged runtime")
        subprocess.run(["tar", "-x", "-C", str(scratch)], input=archive.stdout, check=True)
        base = scratch / "plugins" / "agent-pack" / "runtime"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    for command in ("sync", "reconcile", "doctor"):
        args = [sys.executable, str(base / "agent_pack_config.py"), "--output", "json", command,
                "--project", str(test.project), "--home", str(test.home), "--client", "claude"]
        if command != "doctor":
            args += ["--recovery-root", str(test.recovery), "--replace"]
        proc = subprocess.run(args, capture_output=True, text=True, env=env, check=False)
        test.assertIn(proc.returncode, (0, 1), proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        test.assertNotIn("error", payload, proc.stdout)
        removed = {op.get("name") for op in payload.get("operations", []) if str(op.get("action", "")).startswith("remove-")}
        test.assertFalse(removed, f"{command} would remove {removed}")


class OlderRuntimeTests(ProjectStoreBase):
    """A runtime that predates the scope keys reads this state: it must not see the project-level entries."""

    def test_older_runtime_sync_and_doctor_leave_project_level_entries_alone(self) -> None:
        rc, _, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        older_runtime_runs(self)
        self.assertIn(MCP, self.local(), "the project-level entry is untouched")


class ProjectFileTests(ProjectStoreBase):
    def test_portable_entry_goes_to_mcp_json_only(self) -> None:
        write_json(self.project / ".mcp.json", {"mcpServers": {"team": {"type": "http", "url": "https://example.invalid/mcp"}}})
        os.chmod(self.project / ".mcp.json", 0o664)
        rc, payload, out = self.cli("--apply", "--project-store", "file")
        self.assertEqual(rc, 0, out)
        data = json.loads((self.project / ".mcp.json").read_text())
        self.assertEqual(set(data["mcpServers"]), {"team", MCP})
        self.assertEqual(stat.S_IMODE((self.project / ".mcp.json").stat().st_mode), 0o664)
        self.assertEqual(self.local(), {})
        self.assertNotIn(MCP, self.user_config().get("mcpServers", {}))
        op = next(item for item in payload["operations"] if item["action"] == "add-project-mcp")
        self.assertEqual(op["store"], "project-file")
        self.assertEqual(self.state()["scopes"]["mcps"], {MCP: "project:file"})
        self.assertTrue((Path(payload["recovery"]) / "project-mcp.json.before").is_file())

    def test_entries_with_machine_paths_are_refused_with_the_field_name_only(self) -> None:
        secret_path = "/opt/private-tools/checker"
        self.set_mcp({"type": "stdio", "command": "python3", "args": [secret_path]})
        before = snapshot(self.home)
        for flags in ((), ("--apply",)):
            rc, payload, out = self.cli(*flags, "--project-store", "file")
            self.assertEqual(rc, 2, out)
            self.assertIn("args[0]", payload["error"])
            self.assertIn("--project-store local", payload["error"])
            self.assertNotIn(secret_path, out)
        self.assertEqual(before, snapshot(self.home))
        self.assertFalse((self.project / ".mcp.json").exists())
        rc, _, out = self.cli("--apply")   # the same entry is fine in local
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.local()[MCP]["args"], [secret_path])

    def test_home_and_project_paths_inside_arguments_are_machine_specific(self) -> None:
        entry = {"type": "stdio", "command": "python3", "args": [f"--root={self.project}", f"{self.home}/x"]}
        self.assertEqual(claude.machine_specific(entry, self.home, self.project), ["args[0]", "args[1]"])
        self.assertEqual(claude.machine_specific(
            {"type": "stdio", "command": "npx", "args": ["-y", "pkg@1.0.0"], "env": {"A": "${A}"}},
            self.home, self.project), [])

    def test_symlinked_project_file_is_skipped(self) -> None:
        target = self.root / "elsewhere.json"
        write_json(target, {"mcpServers": {}})
        os.symlink(target, self.project / ".mcp.json")
        rc, payload, out = self.cli("--apply", "--project-store", "file")
        self.assertEqual(rc, 0, out)
        self.assertEqual(payload["skipped"][0]["reason"], "mcp-json-symlink")
        self.assertEqual(json.loads(target.read_text()), {"mcpServers": {}})

    def test_file_entries_do_not_keep_local_credential_values(self) -> None:
        write_json(self.project / ".mcp.json", {"mcpServers": {MCP: {
            "type": "stdio", "command": "python3", "args": ["-m", "checker"], "env": {"API_TOKEN": FAKE_TOKEN}}}})
        rc, payload, out = self.cli("--apply", "--project-store", "file", "--replace-mcp")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("env", json.loads((self.project / ".mcp.json").read_text())["mcpServers"][MCP])
        self.assertNotIn(FAKE_TOKEN, out)


class CarrierTests(ProjectStoreBase):
    """An entry that launches through the user-level package (the credential launcher does) needs that package."""

    def launcher_entry(self, name: str):
        record = claude.record_path(self.home, name)
        return {MCP: {"type": "stdio", "command": "python3", "args": [str(record / "runtime" / "launcher.py")]}}

    def test_project_scope_alone_needs_the_package_when_the_entry_launches_through_it(self) -> None:
        with mock.patch.object(claude, "_render_package_mcps", lambda built, home, name: self.launcher_entry(name)):
            with self.assertRaises(core.AgentPackError) as raised:
                claude.sync_project(self.project, home=self.home, scope="project", recovery_root=self.recovery)
            self.assertIn("project package", str(raised.exception))
            result = claude.sync_project(self.project, home=self.home, scope="all", recovery_root=self.recovery)
            self.assertTrue(result["ok"], "the full sync installs the package in the same transaction")
            claude.sync_project(self.project, home=self.home, scope="user", apply=True, recovery_root=self.recovery)
            result = claude.sync_project(self.project, home=self.home, scope="project", apply=True,
                                         recovery_root=self.recovery)
        self.assertTrue(result["applied"])
        self.assertIn("agent-pack/plugins/proj-a", self.local()[MCP]["args"][0])


if __name__ == "__main__":
    unittest.main()
