"""Preview (`sync`) and `sync --apply` must agree, for every client and without a stack trace.

The CLI is run as a real subprocess on the script path: in-process imports hide the
`__main__` double-module problem (two `AgentPackError` classes) that made the Claude path
print a traceback instead of a one-line error.
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

from test_agent_pack_config import make_project, write_json  # noqa: E402
from agent_pack_config import PROJECT_PROFILE_NAMESPACE  # noqa: E402

CLIENTS = ("claude", "codex", "cursor")
# Built at run time so the repository never contains a token-shaped literal.
TOKEN = "sk-ant-api03-" + "abcdefghij" * 4


def snapshot(root: Path) -> dict[str, str]:
    """Every path and file content under root (symlinks recorded, not followed)."""
    state: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            state[rel] = "link:" + os.readlink(path)
        elif path.is_file():
            state[rel] = path.read_text(encoding="utf-8", errors="replace")
        else:
            state[rel] = "dir"
    return state


class Case:
    """A project mutation; `blocked` is the expected error fragment (None = must succeed)."""

    def __init__(self, name: str, mutate, blocked: str | None) -> None:
        self.name, self.mutate, self.blocked = name, mutate, blocked


def _add_mcp(project: Path, name: str, entry: dict) -> None:
    path = project / ".agents" / "mcp.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mcpServers"][name] = entry
    write_json(path, data)


def _set_credential_file(project: Path, content: str) -> None:
    profile_path = project / ".agents" / PROJECT_PROFILE_NAMESPACE / "project.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["capabilities"]["credential_env_file"] = ".env"
    write_json(profile_path, profile)
    (project / ".env").write_text(content, encoding="utf-8")
    (project / ".env").chmod(0o600)


def _token_value(project: Path) -> None:
    _add_mcp(project, "tok", {"type": "stdio", "command": "node", "args": ["--token", TOKEN]})


def _env_ref_without_file(project: Path) -> None:
    _add_mcp(project, "needs-key", {"type": "stdio", "command": "node", "env": {"SERVICE_KEY": "${SERVICE_KEY}"}})


def _env_ref_missing_key(project: Path) -> None:
    _env_ref_without_file(project)
    _set_credential_file(project, "OTHER_KEY=value\n")


def _env_ref_present(project: Path) -> None:
    _env_ref_without_file(project)
    _set_credential_file(project, "SERVICE_KEY=value\n")


CASES = (
    Case("clean", lambda project: None, None),
    Case("credential-present", _env_ref_present, None),
    Case("token-value", _token_value, "cannot package non-portable items: tok"),
    Case("env-ref-without-credential-file", _env_ref_without_file,
         "MCP environment references require capabilities.credential_env_file: SERVICE_KEY"),
    Case("env-ref-missing-key", _env_ref_missing_key, "credential env file is missing keys: SERVICE_KEY"),
)


class SyncPreviewParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def cli(self, home: Path, *args: str) -> tuple[int, dict | None, str, str]:
        env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH"}}
        env["HOME"] = str(home)
        proc = subprocess.run(
            [sys.executable, str(RUNTIME / "agent_pack_config.py"), "--output", "json", *args, "--home", str(home)],
            capture_output=True, text=True, env=env, check=False,
        )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = None
        return proc.returncode, payload, proc.stdout, proc.stderr

    def project_for(self, case: Case, client: str) -> tuple[Path, Path]:
        # Neutral directory names: a path containing "credential"/"token" trips the secret heuristic
        # on the launcher's --env-file argument (a separate, pre-existing behaviour).
        base = self.root / f"c{CASES.index(case)}-{client}"
        project = make_project(base / "project", "parity")
        case.mutate(project)
        home = base / "home"
        home.mkdir()
        return project, home

    def assert_clean_failure(self, rc: int, payload: dict | None, out: str, err: str, where: str) -> None:
        self.assertEqual(rc, 2, where)
        self.assertNotIn("Traceback", err, where)
        self.assertNotIn("Traceback", out, where)
        self.assertIsNotNone(payload, where)
        self.assertIs(payload["ok"], False, where)
        self.assertNotIn(TOKEN, out + err, where)

    def test_preview_equals_apply_for_every_client(self) -> None:
        for case in CASES:
            for client in CLIENTS:
                with self.subTest(case=case.name, client=client):
                    project, home = self.project_for(case, client)
                    before = snapshot(home)
                    rc_p, pay_p, out_p, err_p = self.cli(home, "sync", "--project", str(project), "--client", client)
                    self.assertEqual(snapshot(home), before, "preview must not write")
                    rc_a, pay_a, out_a, err_a = self.cli(
                        home, "sync", "--project", str(project), "--client", client, "--apply")
                    if case.blocked is None:
                        self.assertEqual((rc_p, rc_a), (0, 0), out_p + out_a + err_p + err_a)
                        self.assertTrue(pay_p["ok"] and pay_a["ok"])
                        self.assertFalse(pay_p["applied"])
                        self.assertTrue(pay_a["applied"])
                    else:
                        self.assert_clean_failure(rc_p, pay_p, out_p, err_p, "preview")
                        self.assert_clean_failure(rc_a, pay_a, out_a, err_a, "apply")
                        self.assertEqual(pay_p, pay_a, "preview and apply report the same error")
                        self.assertIn(case.blocked, pay_p["error"])
                        self.assertEqual(snapshot(home), before, "blocked apply must not write")

    def test_preview_still_blocks_when_package_is_already_installed(self) -> None:
        # The pre-fix preview only validated when the destination already existed; keep that path covered.
        for client in CLIENTS:
            with self.subTest(client=client):
                project, home = self.project_for(CASES[0], client)
                self.assertEqual(self.cli(home, "sync", "--project", str(project), "--client", client, "--apply")[0], 0)
                _token_value(project)
                rc, payload, out, err = self.cli(home, "sync", "--project", str(project), "--client", client)
                self.assert_clean_failure(rc, payload, out, err, "preview")
                self.assertIn("non-portable", payload["error"])

    def test_doctor_reports_non_portable_mcp_as_one_line_error(self) -> None:
        for client in CLIENTS:
            with self.subTest(client=client):
                project, home = self.project_for(CASES[2], client)
                rc, payload, out, err = self.cli(home, "doctor", "--project", str(project), "--client", client)
                self.assert_clean_failure(rc, payload, out, err, "doctor")
                self.assertIn("non-portable", payload["error"])

    def test_text_output_is_one_error_line_without_stack(self) -> None:
        project, home = self.project_for(CASES[2], "claude")
        env = {**os.environ, "HOME": str(home)}
        proc = subprocess.run(
            [sys.executable, str(RUNTIME / "agent_pack_config.py"), "sync", "--project", str(project),
             "--client", "claude", "--home", str(home)],
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("error: cannot package non-portable items: tok", proc.stderr)
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
