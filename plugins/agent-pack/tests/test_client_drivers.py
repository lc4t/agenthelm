"""Client-driver registry (#134 Phase 1): derived enumerations, `agenthelm clients`, user declarations (validate only)."""
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
sys.path.insert(0, str(RUNTIME))

import agent_pack_clients as registry  # noqa: E402
import agent_pack_inventory as inv  # noqa: E402
import agent_pack_nudge as nudge  # noqa: E402
import agenthelm  # noqa: E402

AGENTHELM = RUNTIME / "agenthelm.py"


def valid(**changes) -> dict:
    data = {"schema_version": 1, "id": "example-ide", "display": "Example IDE",
            "detect": {"executables": ["example-ide"], "dirs": ["~/.example-ide"]},
            "distribution": "directory-dropin",
            "stores": {"skills": [{"scope": "user", "path": "~/.example-ide/skills", "format": "dir"}]},
            "capabilities": {"project_mcp_write": False}}
    data.update(changes)
    return data


class RegistryTests(unittest.TestCase):
    def test_enumerations_keep_their_content_and_order(self) -> None:
        self.assertEqual(registry.default_ids(), ("claude", "codex", "cursor"))
        self.assertEqual(agenthelm.CLIENTS, ("claude", "codex", "cursor"))
        self.assertEqual(agenthelm.CLIENT_KEYS, ("claude", "codex", "cursor"))
        self.assertEqual(agenthelm.CLIENT_NAMES, {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"})
        self.assertEqual(nudge.CLIENTS, ("claude", "codex", "cursor"))
        self.assertEqual(inv.CLIENTS, ("claude", "codex", "cursor"))
        self.assertEqual(inv.ALL_CLIENTS, ("claude", "codex", "cursor", "claude-desktop"))

    def test_desktop_is_read_only_and_outside_the_default_set(self) -> None:
        desktop = registry.get("claude-desktop")
        self.assertEqual(desktop["distribution"], "none")
        self.assertTrue(desktop["capabilities"]["read_only"])
        self.assertNotIn("claude-desktop", registry.default_ids())

    def test_capability_lookup(self) -> None:
        self.assertTrue(registry.supports("claude", "project_mcp_write"))
        self.assertFalse(registry.supports("codex", "project_mcp_write"))
        self.assertFalse(registry.supports("nope", "project_mcp_write"))
        self.assertFalse(registry.supports("claude", "nope"))

    def test_project_write_support_comes_from_the_driver_capability(self) -> None:
        import agent_pack_config as config
        self.assertEqual({client: config.project_write_supported(client) for client in registry.all_ids()},
                         {"claude": True, "codex": False, "cursor": False, "claude-desktop": False})
        for client in registry.all_ids():
            self.assertEqual(config.project_write_supported(client), registry.supports(client, "project_mcp_write"))
        self.assertFalse(config.project_write_supported("unknown-client"))

    def test_declared_stores_match_the_inventory_tables(self) -> None:
        for driver in registry.BUILTIN:
            if driver["id"] not in inv.SKILL_STORES:
                continue
            skills = tuple((s["scope"], s["path"].replace("<project>", inv.PROJECT)) for s in driver["stores"]["skills"])
            mcp = tuple((s["scope"], s["path"].replace("<project>", inv.PROJECT)) for s in driver["stores"]["mcp"])
            self.assertEqual(skills, inv.SKILL_STORES[driver["id"]], driver["id"])
            self.assertEqual(mcp, inv.MCP_STORES[driver["id"]], driver["id"])

    def test_builtin_values_are_in_the_documented_vocabulary(self) -> None:
        for driver in registry.BUILTIN:
            self.assertIn(driver["distribution"], registry.DISTRIBUTIONS)
            self.assertEqual(set(driver["capabilities"]), set(registry.CAPABILITIES))


class DeclarationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.folder = self.home / ".config" / "agenthelm" / "clients.d"
        self.folder.mkdir(parents=True)

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "AGENTHELM_LANG": "en"}
        return subprocess.run([sys.executable, str(AGENTHELM), *args, "--home", str(self.home)], env=env,
                              capture_output=True, text=True)

    def clients(self) -> dict:
        result = self.run_cli("clients", "--output", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_valid_declaration_is_listed_but_not_installable(self) -> None:
        (self.folder / "example.json").write_text(json.dumps(valid()), encoding="utf-8")
        data = self.clients()
        self.assertEqual(data["warnings"], [])
        item = next(entry for entry in data["clients"] if entry["id"] == "example-ide")
        self.assertEqual(item["source"], "declared")
        self.assertFalse(item["install_supported"])
        text = self.run_cli("clients").stdout
        self.assertIn("declared; install is not supported in this version", text)

    def test_declared_id_is_not_a_usable_client_choice(self) -> None:
        (self.folder / "example.json").write_text(json.dumps(valid()), encoding="utf-8")
        result = self.run_cli("check", "--client", "example-ide", "--offline")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice: 'example-ide'", result.stderr)
        message = result.stderr.rsplit("invalid choice", 1)[1]  # argparse quotes the choices differently per Python
        for client in registry.default_ids():
            self.assertIn(client, message)
        self.assertNotIn("claude-desktop", message)

    def test_invalid_declarations_warn_and_are_ignored(self) -> None:
        bad = {
            "not-object.json": "[]",
            "broken.json": "{",
            "extra-field.json": json.dumps(valid(command="touch /tmp/should-not-exist")),
            "builtin-id.json": json.dumps(valid(id="claude")),
            "bad-id.json": json.dumps(valid(id="Bad Id")),
            "bad-distribution.json": json.dumps(valid(distribution="shell")),
            "path-escape.json": json.dumps(valid(detect={"dirs": ["~/../etc"]})),
            "exe-with-args.json": json.dumps(valid(detect={"executables": ["tool --flag"]})),
            "bad-flag.json": json.dumps(valid(capabilities={"run_hook": True})),
        }
        for name, content in bad.items():
            (self.folder / name).write_text(content, encoding="utf-8")
        data = self.clients()
        self.assertEqual({entry["file"] for entry in data["warnings"]}, set(bad))
        self.assertEqual([entry["id"] for entry in data["clients"]], list(registry.all_ids()))
        self.assertIn("Warning: ignoring declaration", self.run_cli("clients").stdout)

    def test_duplicate_declared_ids_keep_the_first(self) -> None:
        (self.folder / "a.json").write_text(json.dumps(valid()), encoding="utf-8")
        (self.folder / "b.json").write_text(json.dumps(valid()), encoding="utf-8")
        data = self.clients()
        self.assertEqual([entry["file"] for entry in data["warnings"]], ["b.json"])

    def test_declaration_content_is_never_executed(self) -> None:
        marker = self.home / "executed"
        script = self.home / "bin" / "example-ide"
        script.parent.mkdir()
        script.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        script.chmod(0o755)
        (self.folder / "cmd.json").write_text(json.dumps(valid(command=f"touch {marker}")), encoding="utf-8")
        (self.folder / "ok.json").write_text(json.dumps(valid(id="other-ide", detect={"executables": ["example-ide"]})),
                                              encoding="utf-8")
        env = {"PATH": f"{script.parent}:/usr/bin:/bin", "HOME": str(self.home)}
        result = subprocess.run([sys.executable, str(AGENTHELM), "clients", "--output", "json", "--home", str(self.home)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        data = json.loads(result.stdout)
        other = next(entry for entry in data["clients"] if entry["id"] == "other-ide")
        self.assertTrue(other["detected"])  # found on PATH, still never run

    def test_clients_command_writes_nothing(self) -> None:
        before = sorted(path.relative_to(self.home).as_posix() for path in self.home.rglob("*"))
        self.clients()
        self.assertEqual(sorted(path.relative_to(self.home).as_posix() for path in self.home.rglob("*")), before)

    def test_detection_reports_the_evidence(self) -> None:
        (self.home / ".cursor").mkdir()
        data = self.clients()
        cursor = next(entry for entry in data["clients"] if entry["id"] == "cursor")
        self.assertEqual(cursor["evidence"], {"kind": "directory", "name": "~/.cursor"})
        self.assertTrue(cursor["detected"])


if __name__ == "__main__":
    unittest.main()
