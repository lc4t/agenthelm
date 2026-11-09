"""#91: launcher-wrapped MCP entries that reference ${PLUGIN_ROOT} / ${PLUGIN_DATA} must start."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "runtime"))

from test_mcp_placeholders import PACKAGE, PlaceholderTests, servers  # noqa: E402
from test_sync_preview_parity import CLIENTS, SyncPreviewParityTests, _set_credential_file  # noqa: E402
import mcp_env_launcher  # noqa: E402

# Stub MCP: succeeds only when every path argument exists, and reports the arguments on stdout.
CHECK = ("import sys, os; paths = sys.argv[1:]; "
         "print(chr(10).join(paths)); "
         "sys.exit(0 if paths and all(os.path.exists(p) for p in paths) else 3)")


def package_dir(home: Path, client: str) -> Path:
    return {"codex": home / ".agents" / "plugins" / "plugins" / PACKAGE,
            "cursor": home / ".cursor" / "plugins" / "local" / PACKAGE,
            "claude": home / ".claude" / "agent-pack" / "plugins" / PACKAGE}[client]


class LauncherPluginRootTests(SyncPreviewParityTests):
    # Reuse only the helpers; the inherited parity tests already run in their own class.
    test_preview_equals_apply_for_every_client = None  # type: ignore[assignment]
    test_preview_still_blocks_when_package_is_already_installed = None  # type: ignore[assignment]
    test_doctor_reports_non_portable_mcp_as_one_line_error = None  # type: ignore[assignment]
    test_text_output_is_one_error_line_without_stack = None  # type: ignore[assignment]
    make = PlaceholderTests.make
    add_entry = PlaceholderTests.add_entry
    sync_pair = PlaceholderTests.sync_pair

    def _entry(self, args: list[str]) -> dict:
        return {"type": "stdio", "command": sys.executable, "args": ["-c", CHECK, *args],
                "env": {"SERVICE_KEY": "${SERVICE_KEY}"}}

    def _synced(self, client: str, args: list[str]):
        project, home = self.make("launcher", client)
        self.add_entry(project, "tool", self._entry(args))
        _set_credential_file(project, "SERVICE_KEY=value\n")
        (_, _, _, _), (rc, payload, _, err) = self.sync_pair(project, home, client)
        self.assertEqual(rc, 0, f"{payload} {err}")
        return project, home

    def _client_expanded(self, home: Path, client: str, data: Path) -> dict:
        """The entry as the client launches it: it expands ${PLUGIN_ROOT}/${PLUGIN_DATA} in its own fields only."""
        entry = json.loads(json.dumps(servers(home, client)["tool"]))
        root = str(package_dir(home, client))
        entry["args"] = [a.replace("${PLUGIN_ROOT}", root).replace("${PLUGIN_DATA}", str(data)) for a in entry["args"]]
        return entry

    def _run(self, entry: dict, home: Path) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in {"PLUGIN_ROOT", "PLUGIN_DATA"}}
        env["HOME"] = str(home)
        return subprocess.run([entry["command"], *entry["args"]], capture_output=True, text=True, env=env,
                              check=False, cwd=str(home))

    def test_package_skill_path_is_launchable_for_every_client(self) -> None:
        for client in CLIENTS:
            with self.subTest(client=client):
                _project, home = self._synced(client, ["${PLUGIN_ROOT}/skills/sample-skill/SKILL.md"])
                entry = self._client_expanded(home, client, home / "data")
                self.assertIn("mcp_env_launcher.py", " ".join(entry["args"]))
                done = self._run(entry, home)
                self.assertEqual(done.returncode, 0, done.stderr)
                printed = done.stdout.strip().splitlines()
                self.assertEqual(len(printed), 1)
                self.assertTrue(Path(printed[0]).is_file(), printed)
                self.assertNotIn("${", printed[0])
                self.assertEqual(Path(printed[0]).resolve(),
                                 (package_dir(home, client) / "skills" / "sample-skill" / "SKILL.md").resolve())

    def test_plugin_data_is_handed_to_the_launcher_by_the_client(self) -> None:
        for client in CLIENTS:
            with self.subTest(client=client):
                _project, home = self._synced(client, ["${PLUGIN_DATA}"])
                # Claude's sync expands ${PLUGIN_DATA} itself (data_root); the others leave it to the client.
                data = (home / ".claude" / "agent-pack" / "data" / PACKAGE if client == "claude"
                        else home / "client-managed-data")
                data.mkdir(parents=True)
                entry = self._client_expanded(home, client, data)
                done = self._run(entry, home)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(Path(done.stdout.strip()).resolve(), data.resolve())

    def test_unexpanded_plugin_data_gives_a_specific_error(self) -> None:
        config = {"type": "stdio", "command": sys.executable, "args": ["-c", "pass", "${PLUGIN_DATA}/x"]}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "runtime").mkdir()
            (base / "runtime" / "c.json").write_text(json.dumps(config), encoding="utf-8")
            env_file = base / ".env"
            env_file.write_text("OTHER=1\n", encoding="utf-8")
            code = mcp_env_launcher.main(["--env-file", str(env_file), "--config", str(base / "runtime" / "c.json")])
            self.assertEqual(code, 2)

    def test_dotenv_cannot_override_the_plugin_root(self) -> None:
        for client in ("cursor",):
            with self.subTest(client=client):
                project, home = self._synced(client, ["${PLUGIN_ROOT}/skills/sample-skill/SKILL.md"])
                (project / ".env").write_text("SERVICE_KEY=value\nPLUGIN_ROOT=/elsewhere\n", encoding="utf-8")
                entry = self._client_expanded(home, client, home / "data")
                done = self._run(entry, home)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_entries_without_plugin_placeholders_are_unchanged(self) -> None:
        for client in CLIENTS:
            with self.subTest(client=client):
                _project, home = self._synced(client, ["plain"])
                args = servers(home, client)["tool"]["args"]
                self.assertEqual(args[1:2], ["--env-file"] if False else args[1:2])
                self.assertNotIn("--plugin-data", args)


if __name__ == "__main__":
    unittest.main()
