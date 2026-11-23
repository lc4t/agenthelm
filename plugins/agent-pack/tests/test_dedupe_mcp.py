"""`agenthelm dedupe --keep --retire-others` for MCP copies: managed entries are retired by the tool (fragment in the
0600 recovery directory, byte-exact backup, atomic edit, rollback); every other copy stays a manual command."""
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

import agent_pack_dedupe as dedupe  # noqa: E402
import agent_pack_mcp_move as mcp_write  # noqa: E402
from test_relocate_mcp import ENTRY, FAKE_TOKEN, NAME, SHAREABLE, McpBase  # noqa: E402

KEEP = f"mcp:{NAME}@claude:project"


class DedupeBase(McpBase):
    def dedupe(self, *args: str, lang: str = "en") -> tuple[int, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTHELM_", "XDG_"))}
        env.update(HOME=str(self.home), AGENTHELM_LANG=lang)
        proc = subprocess.run([sys.executable, str(RUNTIME / "agenthelm.py"), "dedupe", "--home", str(self.home),
                               "--project", str(self.project), "--recovery-root", str(self.recovery), *args],
                              capture_output=True, text=True, env=env, check=False)
        return proc.returncode, proc.stdout + proc.stderr

    def group(self) -> str:
        code, out = self.dedupe("--list", "--output", "json")
        self.assertEqual(code, 0, out)
        return next(g["id"] for g in json.loads(out)["groups"] if g["kind"] == "mcp" and g["client"] == "claude")

    def pre(self, *extra: str) -> dict:
        code, out = self.dedupe(self.group(), "--keep", KEEP, "--retire-others", "--output", "json", *extra)
        self.assertIn(code, (0, 1), out)
        return json.loads(out)

    def run_apply(self) -> tuple[int, dict]:
        digest = self.pre()["plan"]["plan_digest"]
        code, out = self.dedupe(self.group(), "--keep", KEEP, "--retire-others", "--output", "json", "--apply",
                                "--expect-plan", digest)
        return code, json.loads(out)

    def setup_pair(self, managed: bool) -> bytes:
        original = self.write_config()
        (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": {NAME: SHAREABLE}}), encoding="utf-8")
        if managed:
            self.own("user")
        return original


class RetireTests(DedupeBase):
    def test_managed_copy_is_retired_and_everything_is_recoverable(self) -> None:
        original = self.setup_pair(True)
        before = self.snapshot()
        document = self.pre()
        plan = document["plan"]
        self.assertEqual((plan["status"], plan["manual"]), ("ready", []))
        self.assertEqual(plan["retire"][0]["ref"], USER := f"mcp:{NAME}@claude:user")
        self.assertNotIn(FAKE_TOKEN, json.dumps(document))
        for lang in ("en", "zh"):
            self.assertNotIn(FAKE_TOKEN, self.dedupe(self.group(), "--keep", KEEP, "--retire-others", lang=lang)[1])
        self.assertEqual(before, self.snapshot(), "preview writes nothing")
        code, applied = self.run_apply()
        self.assertEqual(code, 0, applied)
        self.assertNotIn(FAKE_TOKEN, json.dumps(applied))
        self.assertNotIn(NAME, self.user_config()["mcpServers"])
        self.assertEqual(json.loads((self.project / ".mcp.json").read_text())["mcpServers"][NAME], SHAREABLE, "the kept copy is untouched")
        config = self.user_config()
        self.assertEqual((config["numStartups"], config["theme"]), (7, "dark"))
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        recovery = next(self.recovery.iterdir())
        self.assertEqual((recovery / "claude.json.before").read_bytes(), original)
        fragment = recovery / "mcp" / f"{NAME}@user.json"
        self.assertEqual(json.loads(fragment.read_text())["entry"], ENTRY)
        self.assertEqual(stat.S_IMODE(fragment.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(recovery.stat().st_mode), 0o700)
        for path in self.metadata_files():
            self.assertNotIn(FAKE_TOKEN, path.read_text(errors="replace"), path)
        self.assertNotIn(NAME, self.state()["mcps"])
        self.assertTrue(applied["applied"]["restore"])
        self.assertFalse(any(self.recovery.rglob("restore*")), "no restore command or file is added")

    def test_unmanaged_copy_stays_a_manual_command_and_nothing_is_written(self) -> None:
        self.setup_pair(False)
        before = self.snapshot()
        plan = self.pre()["plan"]
        self.assertEqual((plan["status"], plan["retire"]), ("manual-only", []))
        self.assertEqual(plan["manual"][0]["command"], f"claude mcp remove --scope user {NAME}")
        code, applied = self.run_apply()
        self.assertEqual((code, applied["applied"]["status"]), (0, "manual-only"))
        self.assertEqual(before, self.snapshot())

    def test_mixed_group_retires_the_managed_copy_and_lists_the_other_by_hand(self) -> None:
        self.write_config({NAME: ENTRY}, projects={str(self.project): {"mcpServers": {NAME: {**ENTRY, "args": ["x"]}}}})
        (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": {NAME: SHAREABLE}}), encoding="utf-8")
        self.own("local", {**ENTRY, "args": ["x"]})
        plan = self.pre()["plan"]
        self.assertEqual([r["ref"] for r in plan["retire"]], [f"mcp:{NAME}@claude:local"])
        self.assertEqual([m["ref"] for m in plan["manual"]], [f"mcp:{NAME}@claude:user"])
        code, applied = self.run_apply()
        self.assertEqual(code, 0, applied)
        self.assertEqual(self.local(), {})
        self.assertIn(NAME, self.user_config()["mcpServers"], "the unmanaged copy is left alone")
        self.assertEqual(self.state()["scopes"]["mcps"], {})

    def test_digest_mismatch_refuses_with_zero_writes(self) -> None:
        self.setup_pair(True)
        digest = self.pre()["plan"]["plan_digest"]
        self.write_config({NAME: {**ENTRY, "args": ["changed"]}})
        before = self.snapshot()
        code, out = self.dedupe(self.group(), "--keep", KEEP, "--retire-others", "--output", "json", "--apply", "--expect-plan", digest)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["applied"]["reason"], "plan-digest-mismatch")
        self.assertEqual(before, self.snapshot())

    def test_failure_rolls_every_file_back_byte_for_byte(self) -> None:
        self.setup_pair(True)
        state_path = self.home / ".claude" / "agent-pack" / "state" / "proj-a.json"
        before = {p: p.read_bytes() for p in (self.config_path, state_path)}
        report = dedupe.inv.observe(self.home, self.project, None, "all", "all")
        gid = next(g["id"] for g in dedupe.list_groups(report) if g["kind"] == "mcp" and g["client"] == "claude")
        plan, ctx = dedupe.build_plan(self.home, self.project, gid, KEEP, True, False, False, False, None, self.recovery)
        self.assertEqual(plan["status"], "ready")
        with mock.patch.object(mcp_write, "_write_state", side_effect=OSError("disk full")):
            applied = dedupe.apply_plan(plan, ctx)
        self.assertEqual((applied["status"], applied["written"]), ("rolled-back", False))
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_cross_client_same_fingerprint_is_only_advice(self) -> None:
        self.setup_pair(True)
        codex = self.home / ".codex"
        codex.mkdir()
        (codex / "config.toml").write_text(f'[mcp_servers.{NAME}]\ncommand = "uvx"\nargs = ["demo-mcp-server"]\n', encoding="utf-8")
        plan = self.pre()["plan"]
        self.assertIn("cross-client-keep-recommended", [w["code"] for w in plan["warnings"]])
        code, out = self.dedupe("--list", "--output", "json")
        self.assertFalse([g for g in json.loads(out)["groups"] if g["client"] == "codex" and g["kind"] == "mcp"],
                         "a cross-client copy forms no retire group")


if __name__ == "__main__":
    unittest.main()
