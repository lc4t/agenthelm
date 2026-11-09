"""Placeholders in project MCP entries: system variables (#70) and ${PLUGIN_ROOT} per source (#71)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "runtime"))

from test_agent_pack_config import make_project, write_json  # noqa: E402
from test_sync_preview_parity import (  # noqa: E402
    CLIENTS, SyncPreviewParityTests, _set_credential_file,
)
import agent_pack_config as core  # noqa: E402
from agent_pack_config import PROJECT_PROFILE_NAMESPACE  # noqa: E402

# The explicit contract: these names are machine facts, never credential keys.
SYSTEM_VARIABLES = {
    "HOME", "USER", "LOGNAME", "TMPDIR",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
}


PACKAGE = "project"  # the package is named after the project directory made by make()


def package_mcp(home: Path, client: str) -> dict:
    """The merged package's mcp.json for codex/cursor; Claude entries live in ~/.claude.json."""
    if client == "claude":
        return json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    base = {"codex": home / ".agents" / "plugins" / "plugins" / PACKAGE,
            "cursor": home / ".cursor" / "plugins" / "local" / PACKAGE}
    return json.loads((base[client] / "mcp.json").read_text(encoding="utf-8"))


def servers(home: Path, client: str) -> dict:
    return package_mcp(home, client)["mcpServers"]


class PlaceholderTests(SyncPreviewParityTests):
    """Reuses the parity helpers (CLI subprocess, clean failure assertions)."""

    # The inherited parity tests already run in the parent class; skip re-running them here.
    test_preview_equals_apply_for_every_client = None  # type: ignore[assignment]
    test_preview_still_blocks_when_package_is_already_installed = None  # type: ignore[assignment]
    test_doctor_reports_non_portable_mcp_as_one_line_error = None  # type: ignore[assignment]
    test_text_output_is_one_error_line_without_stack = None  # type: ignore[assignment]

    def make(self, label: str, client: str) -> tuple[Path, Path]:
        base = self.root / f"{label}-{client}"
        project = make_project(base / "project", "parity")
        home = base / "home"
        home.mkdir()
        return project, home

    def add_entry(self, project: Path, name: str, entry: dict, source: str = ".agents/mcp.json") -> None:
        path = project / source
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"mcpServers": {}}
        data["mcpServers"][name] = entry
        write_json(path, data)

    def set_sources(self, project: Path, sources: list[str]) -> None:
        profile_path = project / ".agents" / PROJECT_PROFILE_NAMESPACE / "project.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["capabilities"]["mcp_sources"] = sources
        write_json(profile_path, profile)

    def sync_pair(self, project: Path, home: Path, client: str):
        base = ("sync", "--project", str(project), "--client", client)
        return self.cli(home, *base), self.cli(home, *base, "--apply")

    # ------------------------------------------------------------------ #70

    def test_system_variable_set_is_exactly_the_documented_one(self) -> None:
        self.assertEqual(set(core.SYSTEM_ENV_PLACEHOLDERS), SYSTEM_VARIABLES)
        self.assertFalse(SYSTEM_VARIABLES & core.RESERVED_ENV_PLACEHOLDERS)

    def test_system_variables_are_not_credential_keys(self) -> None:
        for client in CLIENTS:
            for name in sorted(SYSTEM_VARIABLES):
                with self.subTest(client=client, variable=name):
                    project, home = self.make(f"sys-{name.lower()}", client)
                    self.add_entry(project, "tool", {
                        "type": "stdio", "command": "node", "args": [f"${{{name}}}/tools/example/server.js"]})
                    _set_credential_file(project, "OTHER_KEY=value\n")  # no HOME/USER/... in the file
                    (rc_p, pay_p, _o, err_p), (rc_a, pay_a, _o2, err_a) = self.sync_pair(project, home, client)
                    self.assertEqual((rc_p, rc_a), (0, 0), f"{pay_p} {pay_a} {err_p}{err_a}")
                    self.assertTrue(pay_p["ok"] and pay_a["ok"])
                    args = servers(home, client)["tool"]["args"]
                    self.assertEqual(len(args), 1)
                    self.assertNotIn("${", args[0], "expanded at sync time")
                    self.assertTrue(args[0].endswith("/tools/example/server.js"))
                    if name == "HOME":
                        self.assertEqual(args[0], f"{home.resolve()}/tools/example/server.js")

    def test_home_in_args_works_after_sync_and_doctor_is_clean(self) -> None:
        for client in CLIENTS:
            with self.subTest(client=client):
                project, home = self.make("home-doctor", client)
                self.add_entry(project, "tool", {
                    "type": "stdio", "command": "node", "args": ["${HOME}/tools/example/server.js"]})
                _set_credential_file(project, "OTHER_KEY=value\n")
                (_, _, _, _), (rc, payload, _, _) = self.sync_pair(project, home, client)
                self.assertEqual(rc, 0, payload)
                rc, payload, out, err = self.cli(home, "doctor", "--project", str(project), "--client", client)
                self.assertEqual((rc, payload["issues"]), (0, []), out + err)

    def test_a_real_missing_key_is_still_reported_and_home_is_not(self) -> None:
        for client in CLIENTS:
            with self.subTest(client=client):
                project, home = self.make("real-missing", client)
                self.add_entry(project, "tool", {
                    "type": "stdio", "command": "node", "args": ["${HOME}/tools/server.js"],
                    "env": {"SERVICE_KEY": "${SERVICE_KEY}"}})
                _set_credential_file(project, "OTHER_KEY=value\n")
                (rc_p, pay_p, out_p, err_p), (rc_a, pay_a, out_a, err_a) = self.sync_pair(project, home, client)
                self.assert_clean_failure(rc_p, pay_p, out_p, err_p, "preview")
                self.assert_clean_failure(rc_a, pay_a, out_a, err_a, "apply")
                self.assertEqual(pay_p, pay_a)
                self.assertEqual(pay_p["error"], "credential env file is missing keys: SERVICE_KEY")
                rc, payload, _out, _err = self.cli(home, "doctor", "--project", str(project))
                self.assertIn("credential env file missing key: SERVICE_KEY", payload["issues"])
                self.assertNotIn("credential env file missing key: HOME", payload["issues"])
                rc, payload, out, err = self.cli(home, "doctor", "--project", str(project), "--client", client)
                self.assert_clean_failure(rc, payload, out, err, "doctor --client")
                self.assertEqual(payload["error"], "credential env file is missing keys: SERVICE_KEY")

    def test_home_alongside_a_credential_key_expands_home_and_keeps_the_launcher(self) -> None:
        project, home = self.make("mixed", "cursor")
        self.add_entry(project, "tool", {
            "type": "stdio", "command": "node", "args": ["${HOME}/tools/server.js"],
            "env": {"SERVICE_KEY": "${SERVICE_KEY}"}})
        _set_credential_file(project, "SERVICE_KEY=value\n")
        (_, _, _, _), (rc, payload, _, _) = self.sync_pair(project, home, "cursor")
        self.assertEqual(rc, 0, payload)
        entry = servers(home, "cursor")["tool"]
        self.assertIn("mcp_env_launcher.py", " ".join(entry["args"]))
        runtime = next((home / ".cursor" / "plugins" / "local" / PACKAGE / "runtime").glob("*.json"))
        config = json.loads(runtime.read_text(encoding="utf-8"))
        self.assertEqual(config["args"], [f"{home.resolve()}/tools/server.js"])
        self.assertEqual(config["env"], {"SERVICE_KEY": "${SERVICE_KEY}"})

    # ------------------------------------------------------------------ #71

    def two_source_project(self, client: str, label: str = "two-sources", *, with_server: bool = True):
        project, home = self.make(label, client)
        plugin_dir = project / "tools" / "p"
        if with_server:
            (plugin_dir / "runtime").mkdir(parents=True)
            (plugin_dir / "runtime" / "server.py").write_text("print('ok')\n", encoding="utf-8")
        self.add_entry(project, "from-plugin", {
            "type": "stdio", "command": "python3", "args": ["${PLUGIN_ROOT}/runtime/server.py"]},
            "tools/p/mcp.json")
        self.add_entry(project, "own", {"type": "stdio", "command": "python3", "args": ["-m", "own"]},
                       "tools/own/mcp.json")
        self.set_sources(project, ["tools/p/mcp.json", "tools/own/mcp.json"])
        return project, home, plugin_dir

    def test_plugin_root_is_expanded_per_source(self) -> None:
        for client in CLIENTS:
            with self.subTest(client=client):
                project, home, plugin_dir = self.two_source_project(client)
                (rc_p, pay_p, _o, _e), (rc_a, pay_a, _o2, _e2) = self.sync_pair(project, home, client)
                self.assertEqual((rc_p, rc_a), (0, 0), f"{pay_p} {pay_a}")
                args = servers(home, client)["from-plugin"]["args"]
                expected = str((plugin_dir / "runtime" / "server.py").resolve())
                self.assertEqual(args, [expected])
                self.assertTrue(Path(args[0]).is_file(), "the launched file exists")
                self.assertNotIn("agent-pack/plugins", args[0])

    def test_missing_plugin_root_file_blocks_sync_and_doctor_names_the_entry(self) -> None:
        for client in CLIENTS:
            with self.subTest(client=client):
                project, home, plugin_dir = self.two_source_project(client, "missing", with_server=False)
                before = {str(p) for p in home.rglob("*")}
                (rc_p, pay_p, out_p, err_p), (rc_a, pay_a, out_a, err_a) = self.sync_pair(project, home, client)
                self.assert_clean_failure(rc_p, pay_p, out_p, err_p, "preview")
                self.assert_clean_failure(rc_a, pay_a, out_a, err_a, "apply")
                self.assertEqual(pay_p, pay_a)
                self.assertIn("MCP from-plugin", pay_p["error"])
                self.assertIn("${PLUGIN_ROOT}/runtime/server.py", pay_p["error"])
                self.assertIn(f"source directory {plugin_dir.resolve()}", pay_p["error"])  # macOS: /var -> /private/var
                self.assertEqual({str(p) for p in home.rglob("*")}, before, "nothing written")
        project, home, _ = self.two_source_project("claude", "missing-doctor", with_server=False)
        rc, payload, _out, _err = self.cli(home, "doctor", "--project", str(project))
        self.assertEqual(rc, 1)
        self.assertTrue(any("MCP from-plugin" in issue and "${PLUGIN_ROOT}/runtime/server.py" in issue
                            for issue in payload["issues"]), payload["issues"])

    def test_plugin_root_pointing_at_a_package_skill_stays_a_placeholder(self) -> None:
        project, home = self.make("skill-ref", "claude")
        self.add_entry(project, "tool", {
            "type": "stdio", "command": "python3", "args": ["${PLUGIN_ROOT}/skills/sample-skill/SKILL.md"]})
        (_, _, _, _), (rc, payload, _, _) = self.sync_pair(project, home, "claude")
        self.assertEqual(rc, 0, payload)
        arg = servers(home, "claude")["tool"]["args"][0]
        self.assertTrue(arg.endswith("/skills/sample-skill/SKILL.md"))
        self.assertIn("agent-pack", arg)


if __name__ == "__main__":
    unittest.main()
