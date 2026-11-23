"""Guard two (#134, design 7.3): a client that no code knows is added by data alone and every command copes with it.

The test writes a driver declaration with a random, unregistered id and a small fixture (a stub program, a configuration tree,
a plugin source) into a temporary directory, points ``AGENTHELM_DRIVERS_DIR`` at it (honoured only together with
``AGENTHELM_TEST_DRIVERS=1``) and runs ``clients``, ``install`` (preview), ``inventory``, ``doctor``, ``relocate`` (preview) and
a ``sync`` preview as real processes. **No code is changed for it**: if a command had to know the client by name, one of these
steps would fail. The stub is the one the Antigravity tests use; it answers the ``plugin`` subcommands.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent
RUNTIME = PLUGIN / "runtime"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(RUNTIME))

import agent_pack_clients as registry  # noqa: E402
from test_agent_pack_config import make_project  # noqa: E402
from test_agy_client import install_agy_stub  # noqa: E402

SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"


def declaration(client: str, program: str) -> dict:
    """A complete driver for ``client``: detection (program, directory, plugin-list probe), a command-line distribution
    that installs a directory, user and project Skills stores, a JSON MCP store and a package-install ``sync``."""
    return {
        "schema_version": 2, "id": client, "display": "Synthetic Client", "lifecycle": "active",
        "detect": {"executables": [program], "dirs": [{"path": f"~/.{client}"}],
                   "cli": {"argv": ["plugin", "list", "--json"], "stream": "stdout", "parse": "json"}},
        "distribution": {
            "type": "marketplace-cli", "source": "bundle-dir", "interactive": "none", "installed_state": "cli-list-json",
            "config_dir": {"path": f"~/.{client}"},
            "steps": {"install": ["{cli}", "plugin", "install", "{source_dir}"],
                      "uninstall": ["{cli}", "plugin", "uninstall", "{plugin}"]},
            "messages": {"recovery": "reinstall-no-backup", "unchanged": "reinstall-version-unchanged",
                         "describe_update": "update-by-reinstall"}},
        "stores": {
            "skills": [{"scope": "user", "path": f"~/.{client}/skills", "format": "dir", "write_target": True},
                       {"scope": "project", "path": f"<project>/.{client}/skills", "format": "dir", "write_target": True}],
            "mcp": [{"scope": "user", "path": f"~/.{client}/mcp.json", "format": "json", "root_key": "mcpServers"}]},
        "capabilities": {"default_set": False, "read_only": False, "project_mcp_write": False, "user_mcp_write": True,
                         "user_skill_write": True, "project_skill_write": False, "update_by_reinstall": True},
        "precedence": [{"kind": "skills", "rule": "precedence-undocumented", "evidence": "unknown", "source_url": None},
                       {"kind": "mcp", "rule": "precedence-undocumented", "evidence": "unknown", "source_url": None}],
        "sync": {"write": "plugin-install", "package_dir": f"~/.agent-pack/packages/{client}",
                 "companions": {"mcp": "mcp_config.json"}},
        "mcp": {"adapter": "json", "write": "package", "options": {"type_field_required": False}},
    }


class SyntheticClientTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.client = "zz" + uuid.uuid4().hex[:8]
        self.program = f"{self.client}-cli"
        self.assertNotIn(self.client, registry.all_ids())
        self.drivers = self.root / "drivers"
        self.drivers.mkdir()
        (self.drivers / f"{self.client}.json").write_text(json.dumps(declaration(self.client, self.program)), encoding="utf-8")
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        stub = install_agy_stub(self.bin)
        stub.rename(self.bin / self.program)
        (self.home / f".{self.client}" / "skills" / "foo").mkdir(parents=True)
        (self.home / f".{self.client}" / "skills" / "foo" / "SKILL.md").write_text(
            "---\nname: foo\ndescription: A skill of the synthetic client.\n---\n# Foo\n", encoding="utf-8")
        (self.home / f".{self.client}" / "mcp.json").write_text(
            json.dumps({"mcpServers": {"srv": {"command": "echo", "args": ["x"]}}}), encoding="utf-8")
        self.source = self.root / "source"
        self.latest = self.root / "latest.json"
        self.latest.write_text(json.dumps({
            "schema": 1, "marketplace": "agenthelm", "released_at": "2026-10-21", "repository": "https://github.com/example/agenthelm",
            "plugins": {"agent-pack": {"version": "2.0.0"}}, "migrations": [],
            "release": {"tag": "v2.0.0", "tarball_sha256": "x"}}), encoding="utf-8")
        (self.source / "plugins" / "agent-pack").mkdir(parents=True)
        (self.source / "plugins" / "agent-pack" / "plugin.json").write_text(
            json.dumps({"$schema": SCHEMA, "name": "agent-pack", "version": "2.0.0"}), encoding="utf-8")
        self.project = make_project(self.root / "proj", "proj")

    def run_tool(self, script: str, *args: str, drivers: bool = True) -> tuple[int, str]:
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "AGENTHELM_LANG": "en", "AGY_STUB_LOG": str(self.log)}
        if drivers:
            env.update({"AGENTHELM_DRIVERS_DIR": str(self.drivers), "AGENTHELM_TEST_DRIVERS": "1"})
        done = subprocess.run([sys.executable, str(RUNTIME / script), *args], cwd=self.project, env=env, capture_output=True,
                              text=True, timeout=120, check=False)
        return done.returncode, done.stdout

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_clients_lists_it_with_the_declared_facts(self) -> None:
        code, out = self.run_tool("agenthelm.py", "clients", "--home", str(self.home), "--output", "json")
        self.assertEqual(code, 0)
        row = next(item for item in json.loads(out)["clients"] if item["id"] == self.client)
        self.assertEqual((row["display"], row["detected"], row["distribution"], row["install_supported"]),
                         ("Synthetic Client", True, "marketplace-cli", True))
        self.assertFalse(row["capabilities"]["default_set"])

    def test_the_environment_variable_alone_changes_nothing(self) -> None:
        env_only = subprocess.run([sys.executable, str(RUNTIME / "agenthelm.py"), "clients", "--home", str(self.home), "--output", "json"],
                                  env={"PATH": "/usr/bin:/bin", "HOME": str(self.home), "AGENTHELM_DRIVERS_DIR": str(self.drivers)},
                                  capture_output=True, text=True, check=False, timeout=60)
        self.assertNotIn(self.client, env_only.stdout)

    def test_install_preview_plans_its_declared_command(self) -> None:
        code, out = self.run_tool("agenthelm.py", "install", "--client", self.client, "--source", str(self.source),
                                  "--home", str(self.home), "--latest", str(self.latest), "--output", "json")
        self.assertEqual(code, 0, out)
        step = json.loads(out)["steps"][0]
        self.assertEqual((step["client"], step["status"]), (self.client, "preview"))
        self.assertEqual(step["command"], f"{self.bin / self.program} plugin install {self.source / 'plugins/agent-pack'}")
        self.assertNotIn("plugin install", " ".join(self.calls()))   # a preview runs no install

    def test_inventory_reads_its_stores_and_asks_its_plugin_list(self) -> None:
        code, out = self.run_tool("agenthelm.py", "inventory", "--client", self.client, "--home", str(self.home),
                                  "--project", str(self.project), "--output", "json")
        self.assertEqual(code, 0, out)
        report = json.loads(out)
        self.assertEqual([(i["client"], i["name"], i["scope"]) for i in report["skills"]], [(self.client, "foo", "user")])
        self.assertEqual([(i["client"], i["name"], i["evidence"]) for i in report["mcps"]], [(self.client, "srv", "file")])
        self.assertEqual(report["cli"][self.client], {"evidence": "cli", "ok": True, "reason": None, "plugins": []})

    def test_doctor_reports_the_client_without_a_line_of_code_for_it(self) -> None:
        code, out = self.run_tool("agent_pack_config.py", "--output", "json", "doctor", "--project", str(self.project),
                                  "--client", self.client, "--home", str(self.home))
        report = json.loads(out)
        self.assertIn(code, (0, 1), out)
        entry = report["clients"][self.client]
        self.assertEqual(entry["client"], self.client)
        self.assertEqual(entry["cli"]["evidence"], "cli")
        code, out = self.run_tool("agenthelm.py", "doctor", "--home", str(self.home), "--latest", str(self.latest), "--output", "json")
        self.assertEqual(code, 0, out)

    def test_relocate_preview_plans_a_move_between_its_own_scopes(self) -> None:
        code, out = self.run_tool("agenthelm.py", "relocate", f"skill:foo@{self.client}:user", "--to", f"{self.client}:project",
                                  "--home", str(self.home), "--project", str(self.project), "--allow-widen", "--output", "json")
        plan = json.loads(out)["plan"]
        self.assertEqual((code, plan["blocked"], plan["target"]["client"]), (0, [], self.client))
        self.assertEqual(plan["target"]["store"], f"<project>/.{self.client}/skills")
        self.assertFalse((self.project / f".{self.client}").exists())   # a preview writes nothing

    def test_sync_preview_builds_the_package_and_names_the_install(self) -> None:
        code, out = self.run_tool("agent_pack_config.py", "--output", "json", "sync", "--project", str(self.project),
                                  "--client", self.client, "--home", str(self.home))
        self.assertEqual(code, 0, out)
        result = json.loads(out)
        self.assertEqual([op["action"] for op in result["operations"]], ["install-plugin", "run-client-command"])
        self.assertEqual(result["operations"][1]["to"].split(" plugin install ")[0], self.program)


if __name__ == "__main__":
    unittest.main()
