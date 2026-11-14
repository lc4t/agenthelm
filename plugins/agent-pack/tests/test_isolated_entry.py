"""``agent_pack_config.py`` must start under ``python3 -I`` / ``-P`` / PYTHONSAFEPATH=1 (#98).

Only this one entry is supported in isolated mode; its sibling imports (``agent_pack_claude`` ...) must
still resolve and share one ``AgentPackError`` class, so a Claude-path failure stays a one-line error.
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
SCRIPT = RUNTIME / "agent_pack_config.py"
sys.path.insert(0, str(TESTS))

from test_agent_pack_config import make_project  # noqa: E402
from test_sync_preview_parity import CASES  # noqa: E402

MODES = {
    "plain": ([], {}),
    "-I": (["-I"], {}),
    "-P": (["-P"], {}),
    "PYTHONSAFEPATH": ([], {"PYTHONSAFEPATH": "1"}),
}


class IsolatedEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def run_cli(self, mode: str, home: Path, *args: str) -> subprocess.CompletedProcess:
        flags, extra = MODES[mode]
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update(extra, HOME=str(home))
        # cwd elsewhere: nothing may rely on the working directory being importable either.
        return subprocess.run(
            [sys.executable, *flags, str(SCRIPT), "--output", "json", *args, "--home", str(home)],
            capture_output=True, text=True, env=env, check=False, cwd=str(self.root),
        )

    def project(self, case_index: int, mode: str) -> tuple[Path, Path]:
        base = self.root / f"p{case_index}-{mode.strip('-')}"
        project = make_project(base / "project", "iso")
        CASES[case_index].mutate(project)
        home = base / "home"
        home.mkdir()
        return project, home

    def test_help_starts_in_every_mode(self) -> None:
        for mode in MODES:
            with self.subTest(mode=mode):
                proc = subprocess.run(
                    [sys.executable, *MODES[mode][0], str(SCRIPT), "--help"], capture_output=True, text=True,
                    env={**os.environ, **MODES[mode][1]}, check=False, cwd=str(self.root))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertNotIn("ModuleNotFoundError", proc.stderr)
                self.assertIn("usage:", proc.stdout)

    def test_doctor_with_claude_client_runs_in_every_mode(self) -> None:
        # ``--client claude`` imports agent_pack_claude lazily: it must be found in isolated mode too.
        for mode in MODES:
            with self.subTest(mode=mode):
                project, home = self.project(0, mode)
                proc = self.run_cli(mode, home, "doctor", "--project", str(project), "--client", "claude")
                # Nothing is installed yet, so doctor reports issues (exit 1); the point is that it ran.
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertIn("claude", json.loads(proc.stdout)["clients"])

    def test_non_portable_mcp_is_one_line_error_in_every_mode(self) -> None:
        index = next(i for i, case in enumerate(CASES) if case.name == "token-value")
        for mode in MODES:
            with self.subTest(mode=mode):
                project, home = self.project(index, mode)
                proc = self.run_cli(mode, home, "doctor", "--project", str(project), "--client", "claude")
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr + proc.stdout)
                payload = json.loads(proc.stdout)
                self.assertIs(payload["ok"], False)
                self.assertIn("non-portable", payload["error"])

    def test_text_mode_error_is_a_single_stderr_line_when_isolated(self) -> None:
        index = next(i for i, case in enumerate(CASES) if case.name == "token-value")
        project, home = self.project(index, "-I")
        proc = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "doctor", "--project", str(project), "--client", "claude",
             "--home", str(home)],
            capture_output=True, text=True, env={**os.environ, "HOME": str(home)}, check=False,
            cwd=str(self.root))
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(len([line for line in proc.stderr.splitlines() if line.strip()]), 1, proc.stderr)
        self.assertIn("error: cannot package non-portable items", proc.stderr)


if __name__ == "__main__":
    unittest.main()
