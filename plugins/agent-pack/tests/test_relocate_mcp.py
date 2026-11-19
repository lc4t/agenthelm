"""`agenthelm relocate` for MCP entries (Claude user / local / project file): preview = apply, never a value in output
or in the metadata we write, managed vs unmanaged source, byte-exact rollback."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(RUNTIME))

import agent_pack_claude as claude  # noqa: E402
import agent_pack_mcp_write as mcp_write  # noqa: E402
from test_project_store_write import older_runtime_runs  # noqa: E402
from test_scope_declaration import FAKE_TOKEN, ScopeBase  # noqa: E402

NAME = "demo-mcp"
ENTRY = {"type": "stdio", "command": "uvx", "args": ["demo-mcp-server"], "env": {"DEMO_API_TOKEN": FAKE_TOKEN}}
SHAREABLE = {"type": "stdio", "command": "uvx", "args": ["demo-mcp-server"], "env": {"DEMO_API_TOKEN": "${DEMO_API_TOKEN}"}}
USER_REF = f"mcp:{NAME}@claude:user"


class McpBase(ScopeBase):
    def setUp(self) -> None:
        super().setUp()
        self.config_path = self.home / ".claude.json"

    def write_config(self, servers: dict | None = None, **extra) -> bytes:
        data = {"numStartups": 7, "theme": "dark", "mcpServers": servers if servers is not None else {NAME: ENTRY}, **extra}
        self.config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.config_path, 0o600)
        return self.config_path.read_bytes()

    def own(self, scope: str, entry: dict | None = None) -> None:
        """Record the entry as owned by this project's agent-pack state (managed)."""
        state_dir = self.home / ".claude" / "agent-pack" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        digest = mcp_write.entry_hash(entry or ENTRY)
        state = {"schema_version": 1, "package": "proj-a", "skills": [], "mcps": {}}
        if scope == "user":
            state["mcps"] = {NAME: digest}
        else:
            state["scopes"] = {"skills": {}, "mcps": {NAME: mcp_write.PLACE[scope]}, "project_hashes": {NAME: digest}}
        (state_dir / "proj-a.json").write_text(json.dumps(state), encoding="utf-8")

    def reloc(self, ref: str, to: str, *args: str, lang: str = "en") -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTHELM_", "XDG_"))}
        env.update(HOME=str(self.home), AGENTHELM_LANG=lang)
        proc = subprocess.run([sys.executable, str(RUNTIME / "agenthelm.py"), "relocate", ref, "--to", to,
                               "--home", str(self.home), "--project", str(self.project), "--recovery-root", str(self.recovery),
                               *args], capture_output=True, text=True, env=env, check=False)
        return proc.returncode, proc.stdout + proc.stderr

    def preview(self, ref: str, to: str, *args: str) -> dict:
        code, out = self.reloc(ref, to, "--output", "json", *args)
        self.assertIn(code, (0, 1), out)
        return json.loads(out)

    def apply(self, ref: str, to: str, *args: str) -> tuple[int, dict]:
        digest = self.preview(ref, to, *args)["plan"]["plan_digest"]
        code, out = self.reloc(ref, to, "--output", "json", "--apply", "--expect-plan", digest, *args)
        self.assertIn(code, (0, 1), out)
        return code, json.loads(out)

    def local(self) -> dict:
        return self.user_config().get("projects", {}).get(str(self.project), {}).get("mcpServers", {})

    def snapshot(self) -> dict[str, bytes]:
        return {str(p): p.read_bytes() for base in (self.home, self.project) for p in sorted(base.rglob("*"))
                if p.is_file() and not p.is_symlink()}

    def metadata_files(self) -> list[Path]:
        """Everything we generate in the recovery directory except the user's own byte-exact copies and fragments."""
        return [p for p in self.recovery.rglob("*") if p.is_file() and p.name != "claude.json.before"
                and p.parent.name != "mcp" and p.name != "project-mcp.json.before"]


class PreviewTests(McpBase):
    def test_preview_writes_nothing_and_never_shows_a_value(self) -> None:
        self.write_config()
        self.own("user")
        before = self.snapshot()
        for lang in ("en", "zh"):
            code, out = self.reloc(USER_REF, "claude:local", lang=lang)
            self.assertEqual(code, 0, out)
            self.assertNotIn(FAKE_TOKEN, out)
        document = self.preview(USER_REF, "claude:local")
        self.assertNotIn(FAKE_TOKEN, json.dumps(document))
        plan = document["plan"]
        self.assertEqual((plan["kind"], plan["status"], plan["direction"]), ("mcp", "ready", "narrow"))
        self.assertTrue(plan["source"]["managed"] and plan["retire_source"])
        self.assertEqual([s["action"] for s in plan["steps"]], [
            "create-recovery-dir", "write-mcp-entry", "verify-mcp-readback", "retire-mcp-entry", "update-state", "confirm-in-client"])
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.recovery.exists())

    def test_digest_mismatch_refuses_and_writes_nothing(self) -> None:
        self.write_config()
        self.own("user")
        digest = self.preview(USER_REF, "claude:local")["plan"]["plan_digest"]
        # the source changes after the preview
        self.write_config({NAME: {**ENTRY, "args": ["another"]}})
        before = self.snapshot()
        code, out = self.reloc(USER_REF, "claude:local", "--output", "json", "--apply", "--expect-plan", digest)
        document = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(document["applied"]["reason"], "plan-digest-mismatch")
        self.assertEqual(before, self.snapshot())

    def test_apply_equals_preview(self) -> None:
        self.write_config()
        self.own("user")
        planned = self.preview(USER_REF, "claude:local")["plan"]
        code, document = self.apply(USER_REF, "claude:local")
        self.assertEqual(code, 0, document)
        applied_plan = document["plan"]
        self.assertEqual(planned["plan_digest"], applied_plan["plan_digest"])
        self.assertEqual(planned["steps"], applied_plan["steps"])


class ManagedMoveTests(McpBase):
    def test_managed_user_to_local_moves_retires_and_keeps_everything_else(self) -> None:
        original = self.write_config({NAME: ENTRY, "other": {"type": "http", "url": "https://example.invalid/x"}},
                                     projects={"/elsewhere": {"allowedTools": ["x"], "mcpServers": {}}})
        self.own("user")
        code, document = self.apply(USER_REF, "claude:local")
        self.assertEqual(code, 0, document)
        self.assertNotIn(FAKE_TOKEN, json.dumps(document))
        config = self.user_config()
        self.assertEqual(set(config["mcpServers"]), {"other"})
        self.assertEqual(self.local()[NAME], ENTRY)
        self.assertEqual((config["numStartups"], config["theme"]), (7, "dark"))
        self.assertEqual(config["projects"]["/elsewhere"], {"allowedTools": ["x"], "mcpServers": {}})
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        recovery = next(self.recovery.iterdir())
        self.assertEqual((recovery / "claude.json.before").read_bytes(), original)
        fragment = recovery / "mcp" / f"{NAME}@user.json"
        self.assertEqual(json.loads(fragment.read_text())["entry"], ENTRY)
        for path in (fragment, recovery / "claude.json.before"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(recovery.stat().st_mode), 0o700)
        for path in self.metadata_files():
            self.assertNotIn(FAKE_TOKEN, path.read_text(errors="replace"), path)
        state = self.state()
        self.assertNotIn(NAME, state["mcps"])
        self.assertEqual(state["scopes"]["mcps"], {NAME: "project:local"})
        self.assertIn(NAME, state["scopes"]["project_hashes"])
        self.assertTrue(document["applied"]["restore"])
        self.assertNotIn(FAKE_TOKEN, (recovery / "relocate.json").read_text())

    def test_managed_local_to_project_file_needs_confirmation_and_a_shareable_entry(self) -> None:
        self.write_config({}, projects={str(self.project): {"mcpServers": {NAME: SHAREABLE}}})
        self.own("local", SHAREABLE)
        ref = f"mcp:{NAME}@claude:local"
        plan = self.preview(ref, "claude:project")["plan"]
        self.assertEqual((plan["status"], plan["direction"]), ("ready", "same"))
        self.assertEqual(plan["confirmations"][0]["flag"], "--confirm-project-content")
        code, document = self.apply(ref, "claude:project")
        self.assertEqual((code, document["applied"]["reason"]), (1, "project-content-unconfirmed"))
        self.assertFalse((self.project / ".mcp.json").exists())
        code, document = self.apply(ref, "claude:project", "--confirm-project-content")
        self.assertEqual(code, 0, document)
        self.assertEqual(json.loads((self.project / ".mcp.json").read_text())["mcpServers"][NAME], SHAREABLE)
        self.assertEqual(self.local(), {})
        self.assertEqual(self.state()["scopes"]["mcps"], {NAME: "project:file"})

    def test_widening_needs_the_flag_and_is_marked(self) -> None:
        self.write_config({}, projects={str(self.project): {"mcpServers": {NAME: ENTRY}}})
        self.own("local")
        ref = f"mcp:{NAME}@claude:local"
        plan = self.preview(ref, "claude:user")["plan"]
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blocked"][0]["code"], "widen-needs-flag")
        plan = self.preview(ref, "claude:user", "--allow-widen")["plan"]
        self.assertEqual((plan["status"], plan["direction"]), ("ready", "widen"))
        self.assertEqual(plan["warnings"][0]["code"], "widen")
        code, document = self.apply(ref, "claude:user", "--allow-widen")
        self.assertEqual(code, 0, document)
        self.assertEqual(self.user_config()["mcpServers"][NAME], ENTRY)
        self.assertEqual(self.local(), {})
        state = self.state()
        self.assertEqual(set(state["mcps"]), {NAME})
        self.assertEqual(state["scopes"]["mcps"], {NAME: "user"})


class UnmanagedTests(McpBase):
    def test_unmanaged_source_is_copied_and_left_alone_with_a_manual_command(self) -> None:
        original = self.write_config()
        plan = self.preview(USER_REF, "claude:local")["plan"]
        self.assertFalse(plan["source"]["managed"] or plan["retire_source"])
        self.assertEqual(plan["manual"][0]["command"], f"claude mcp remove --scope user {NAME}")
        self.assertTrue(plan["manual"][0]["not_archivable_by_tool"])
        self.assertNotIn("retire-mcp-entry", [s["action"] for s in plan["steps"]])
        code, document = self.apply(USER_REF, "claude:local")
        self.assertEqual(code, 0, document)
        self.assertEqual(self.user_config()["mcpServers"][NAME], ENTRY, "the original place is untouched")
        self.assertEqual(self.local()[NAME], ENTRY)
        self.assertFalse((self.home / ".claude" / "agent-pack" / "state").exists())
        self.assertEqual((next(self.recovery.iterdir()) / "claude.json.before").read_bytes(), original)

    def test_an_entry_owned_by_another_package_is_unmanaged(self) -> None:
        self.write_config()
        self.own("user")
        state = self.home / ".claude" / "agent-pack" / "state"
        (state / "proj-a.json").rename(state / "someone-else.json")
        self.assertFalse(self.preview(USER_REF, "claude:local")["plan"]["retire_source"])


class BlockedTests(McpBase):
    def blocked_codes(self, ref: str, to: str, *args: str) -> list[str]:
        before = self.snapshot()
        plan = self.preview(ref, to, *args)["plan"]
        self.assertEqual(plan["status"], "blocked")
        code, out = self.reloc(ref, to, "--output", "json", "--apply", "--expect-plan", plan["plan_digest"], *args)
        self.assertEqual(code, 1)
        self.assertEqual(before, self.snapshot(), "nothing written")
        self.assertNotIn(FAKE_TOKEN, out)
        return [item["code"] for item in plan["blocked"]]

    def test_target_exists_is_never_overwritten(self) -> None:
        self.write_config({NAME: ENTRY}, projects={str(self.project): {"mcpServers": {NAME: ENTRY}}})
        self.own("user")
        self.assertEqual(self.blocked_codes(USER_REF, "claude:local"), ["target-exists-identical"])
        self.write_config({NAME: ENTRY}, projects={str(self.project): {"mcpServers": {NAME: {**ENTRY, "args": ["x"]}}}})
        self.assertEqual(self.blocked_codes(USER_REF, "claude:local"), ["target-exists-different"])

    def test_inline_secret_cannot_enter_the_project_file(self) -> None:
        self.write_config()
        self.own("user")
        self.assertIn("inline-secret-in-project-file", self.blocked_codes(USER_REF, "claude:project"))
        self.assertFalse((self.project / ".mcp.json").exists())

    def test_machine_path_cannot_enter_the_project_file(self) -> None:
        self.write_config({NAME: {**SHAREABLE, "command": str(self.home / "bin" / "tool")}})
        self.assertIn("machine-specific-in-project-file", self.blocked_codes(USER_REF, "claude:project"))

    def test_client_without_project_write_support(self) -> None:
        self.write_config()
        with mock.patch.object(mcp_write.core, "project_write_supported", return_value=False):
            home, project = self.home, self.project
            plan, _ctx = mcp_write.build_plan(home, project, USER_REF, "claude:local")
        self.assertEqual([b["code"] for b in plan["blocked"]], ["project-write-unsupported"])

    def test_other_inputs(self) -> None:
        self.write_config()
        self.assertEqual(self.blocked_codes(USER_REF, "claude:user"), ["target-is-source"])
        self.assertEqual(self.blocked_codes("mcp:missing@claude:user", "claude:local"), ["source-not-found"])
        self.assertIn("cross-client-unsupported", self.blocked_codes(USER_REF, "codex:local"))


class RollbackTests(McpBase):
    def test_failure_after_the_writes_restores_every_file_byte_for_byte(self) -> None:
        self.write_config()
        self.own("user")
        state_path = self.home / ".claude" / "agent-pack" / "state" / "proj-a.json"
        before = {p: p.read_bytes() for p in (self.config_path, state_path)}
        plan, ctx = mcp_write.build_plan(self.home, self.project, USER_REF, "claude:local", recovery_override=self.recovery)
        self.assertEqual(plan["status"], "ready")
        with mock.patch.object(mcp_write, "_write_state", side_effect=OSError("disk full")):
            applied = mcp_write.apply_plan(plan, ctx, False)
        self.assertEqual((applied["status"], applied["written"]), ("rolled-back", False))
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)

    def test_failure_of_the_project_file_write_restores_the_config(self) -> None:
        self.write_config({NAME: SHAREABLE})
        self.own("user", SHAREABLE)
        (self.project / ".mcp.json").write_text('{"mcpServers": {}}\n')
        before = {p: p.read_bytes() for p in (self.config_path, self.project / ".mcp.json")}
        plan, ctx = mcp_write.build_plan(self.home, self.project, USER_REF, "claude:project", recovery_override=self.recovery)
        self.assertEqual(plan["status"], "ready", plan["blocked"])
        real = claude.core._write_json_atomic

        def flaky(path, value, *, mode=0o644):
            if path.name == ".mcp.json":
                raise OSError("boom")
            return real(path, value, mode=mode)

        with mock.patch.object(claude.core, "_write_json_atomic", side_effect=flaky):
            applied = mcp_write.apply_plan(plan, ctx, True)
        self.assertEqual(applied["status"], "rolled-back")
        self.assertEqual(before, {p: p.read_bytes() for p in before})


class OlderRuntimeTests(McpBase):
    def test_older_runtime_is_unaffected_by_the_moved_state(self) -> None:
        self.write_config()
        self.own("user")
        code, document = self.apply(USER_REF, "claude:local")
        self.assertEqual(code, 0, document)
        older_runtime_runs(self)
        self.assertEqual(self.local()[NAME], ENTRY, "the project-level entry is untouched")


if __name__ == "__main__":
    unittest.main()
