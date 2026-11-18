"""Claude Desktop (`claude-desktop`) in `agenthelm inventory` and `doctor`: read-only, a separate target (#69)."""
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

import agent_pack_inventory as inv  # noqa: E402
from test_agent_pack_config import make_project  # noqa: E402

SECRET = "desktop-secret-value-123"


class DesktopTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = make_project(self.root / "proj", "proj")

    def config_path(self, platform: str | None = None) -> Path:
        return inv.resolve_store(inv.desktop_store(platform), self.home, None)

    def write_config(self, data, platform: str | None = None) -> None:
        path = self.config_path(platform)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def sample(self) -> dict:
        return {"mcpServers": {
            "local-check": {"command": "node", "args": ["a.js", "--api-key", SECRET], "env": {"API_TOKEN": SECRET}},
            "other": {"command": "uvx", "args": ["${HOME}/x"]}}}

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("AGENTHELM_", "XDG_", "PYTHONPATH"))}
        env.update(HOME=str(self.home), AGENTHELM_LANG="en")
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
        return proc.returncode, proc.stdout, proc.stderr

    def inventory_cli(self, *args: str) -> tuple[int, str, str]:
        return self.run_cli(sys.executable, str(RUNTIME / "agenthelm.py"), "inventory", "--home", str(self.home),
                            "--project", str(self.project), *args)

    def doctor_cli(self, *args: str, isolated: bool = False) -> tuple[int, dict, str]:
        base = [sys.executable, *(["-I"] if isolated else []), str(RUNTIME / "agent_pack_config.py"), "--output", "json",
                "doctor", "--project", str(self.project), "--home", str(self.home)]
        code, out, err = self.run_cli(*base, *args)
        return code, json.loads(out), err

    # ------------------------------------------------------------ paths

    def test_documented_paths_per_platform(self) -> None:
        self.assertEqual(inv.desktop_store("darwin"), "~/Library/Application Support/Claude/claude_desktop_config.json")
        self.assertEqual(inv.desktop_store("linux"), "~/.config/Claude/claude_desktop_config.json")
        self.assertIsNone(inv.desktop_store("win32"))
        entries, unknown = inv.observe_desktop(self.home, "win32")
        self.assertEqual((entries, [item["reason"] for item in unknown]), ([], ["platform-unsupported"]))

    # ------------------------------------------------------------ inventory

    def test_not_in_the_default_client_set(self) -> None:
        self.write_config(self.sample())
        default = inv.observe(self.home, self.project)
        self.assertEqual(default["clients"], ["claude", "codex", "cursor"])
        self.assertNotIn("claude-desktop", default["summary"]["per_client"])
        self.assertEqual([item for item in default["mcps"] if item["client"] == "claude-desktop"], [])
        code, out, _err = self.inventory_cli("--output", "json")
        self.assertNotIn("claude-desktop", json.loads(out)["clients"])

    def test_named_or_all_reads_the_file_as_its_own_client(self) -> None:
        self.write_config(self.sample())
        for clients in (["claude-desktop"], ["all"]):
            report = inv.observe(self.home, self.project, clients)
            entries = [item for item in report["mcps"] if item["client"] == "claude-desktop"]
            self.assertEqual([item["name"] for item in entries], ["local-check", "other"])
            self.assertEqual((entries[0]["scope"], entries[0]["store"]), ("user", inv.desktop_store()))
            self.assertEqual(report["summary"]["per_client"]["claude-desktop"]["mcp_count"], 2)
            self.assertEqual(report["summary"]["per_client"]["claude-desktop"]["skill_count"], 0)
        code, out, _err = self.inventory_cli("--client", "claude-desktop", "--output", "json")
        self.assertEqual((code, json.loads(out)["clients"]), (0, ["claude-desktop"]))
        self.assertEqual(json.loads(self.inventory_cli("--client", "all", "--output", "json")[1])["clients"],
                         ["claude", "codex", "cursor", "claude-desktop"])

    def test_not_merged_into_claude(self) -> None:
        self.write_config({"mcpServers": {"only-desktop": {"command": "node"}}})
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"only-cli": {"command": "node"}}}), encoding="utf-8")
        report = inv.observe(self.home, self.project, ["all"])
        by_client = {client: sorted(item["name"] for item in report["mcps"] if item["client"] == client)
                     for client in ("claude", "claude-desktop")}
        self.assertEqual(by_client, {"claude": ["only-cli"], "claude-desktop": ["only-desktop"]})
        self.assertEqual(report["duplicates"]["mcps"], [])

    def test_secrets_never_printed(self) -> None:
        self.write_config(self.sample())
        texts = [json.dumps(inv.observe(self.home, self.project, ["all"])), self.inventory_cli("--client", "all")[1],
                 self.inventory_cli("--client", "all", "--output", "json")[1], json.dumps(self.doctor_cli("--client", "claude-desktop")[1])]
        for text in texts:
            self.assertNotIn(SECRET, text)
        entry = next(item for item in inv.observe(self.home, self.project, ["claude-desktop"])["mcps"] if item["name"] == "local-check")
        self.assertEqual(entry["env_names"], ["API_TOKEN"])

    def test_variable_expansion_note(self) -> None:
        self.write_config(self.sample())
        notes = {item["name"]: item["notes"] for item in inv.observe(self.home, self.project, ["claude-desktop"])["mcps"]}
        self.assertEqual(notes, {"local-check": [], "other": ["variable-expansion-unconfirmed"]})

    def test_missing_and_corrupt_file_are_unknown(self) -> None:
        report = inv.observe(self.home, self.project, ["claude-desktop"])
        self.assertEqual([(item["reason"], item["store"]) for item in report["unknown"]], [("not-found", inv.desktop_store())])
        for text, reason in (("{ nope", "invalid-json"), ("[1]", "unexpected-structure")):
            self.write_config(text)
            report = inv.observe(self.home, self.project, ["claude-desktop"])
            self.assertEqual((report["mcps"], [item["reason"] for item in report["unknown"]]), ([], [reason]))
        self.write_config({"mcpServers": []})
        self.assertEqual([item["reason"] for item in inv.observe(self.home, self.project, ["claude-desktop"])["unknown"]],
                         ["unexpected-structure"])
        code, text, _err = self.inventory_cli("--client", "claude-desktop")
        self.assertEqual(code, 0)
        self.assertIn("Undetermined", text)

    def test_scope_project_and_type_skills_skip_it(self) -> None:
        self.write_config(self.sample())
        self.assertEqual(inv.observe(self.home, self.project, ["claude-desktop"], scope="project")["mcps"], [])
        self.assertEqual(inv.observe(self.home, self.project, ["claude-desktop"], types="skills")["mcps"], [])

    # ------------------------------------------------------------ doctor

    def test_doctor_claude_desktop_is_read_only_and_never_an_issue(self) -> None:
        before = sorted(self.home.rglob("*"))
        code, payload, _err = self.doctor_cli("--client", "claude-desktop")
        self.assertEqual(sorted(self.home.rglob("*")), before)
        self.assertEqual((code, payload["ok"]), (0, True))
        report = payload["clients"]["claude-desktop"]
        self.assertEqual((report["servers"], [item["reason"] for item in report["unknown"]]), ([], ["not-found"]))
        self.write_config("{ nope")
        code, payload, _err = self.doctor_cli("--client", "claude-desktop")
        self.assertEqual((code, payload["ok"]), (0, True))
        self.assertTrue(any("claude_desktop_config.json" in item or "claude-desktop" in item for item in payload["warnings"]))
        self.write_config(self.sample())
        report = self.doctor_cli("--client", "claude-desktop")[1]["clients"]["claude-desktop"]
        self.assertEqual(report["servers"], ["local-check", "other"])
        self.assertEqual(report["evidence"], "file")

    def test_doctor_claude_reports_shadowed_by_as_advisory(self) -> None:
        baseline_code, baseline, _err = self.doctor_cli("--client", "claude")
        self.assertNotIn("shadowed_by", baseline["clients"]["claude"])
        self.write_config(self.sample())
        code, payload, _err = self.doctor_cli("--client", "claude")
        self.assertEqual((code, payload["ok"], payload["issues"]), (baseline_code, baseline["ok"], baseline["issues"]))
        shadows = payload["clients"]["claude"]["shadowed_by"]
        self.assertEqual([item["name"] for item in shadows], ["local-check"])  # "other" is not a project-managed server
        item = shadows[0]
        self.assertEqual((item["by"], item["applies_to"], item["cli_affected"], item["severity"], item["evidence"]),
                         ("claude_desktop_config.json", "desktop-local-code-tab", False, "advisory", "file"))
        self.assertFalse(item["precedence_confirmed"])
        self.assertTrue(any("local-check" in warning and "Code tab" in warning for warning in payload["warnings"]))
        self.assertNotIn(SECRET, json.dumps(payload))

    def test_doctor_other_clients_and_default_do_not_read_desktop(self) -> None:
        self.write_config(self.sample())
        for args in ((), ("--client", "codex")):
            payload = self.doctor_cli(*args)[1]
            self.assertNotIn("claude-desktop", payload.get("clients", {}))
            self.assertFalse(any("claude_desktop_config" in warning for warning in payload["warnings"]))

    def test_isolated_entry_still_works(self) -> None:
        self.write_config(self.sample())
        code, payload, err = self.doctor_cli("--client", "claude", "--client", "claude-desktop", isolated=True)
        self.assertEqual(err, "")
        self.assertIn("shadowed_by", payload["clients"]["claude"])
        self.assertIn("claude-desktop", payload["clients"])


if __name__ == "__main__":
    unittest.main()
