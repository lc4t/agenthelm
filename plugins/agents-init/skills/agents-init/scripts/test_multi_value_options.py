"""#101: --project-type / --stack / --agent-cli accept repeated flags, comma lists, or both (ordered, de-duplicated)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "init_project.py"
BASE = ("--name", "demo", "--project-type", "code", "--vcs", "git", "--stack", "python", "--runtime", "local")


class MultiValueOptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)

    def run_init(self, root: Path, *extra: str, lang: str = "zh") -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "HOME": self.home.name, "AGENTHELM_LANG": lang}
        return subprocess.run([sys.executable, str(SCRIPT), "--project", str(root), *extra],
                              check=False, capture_output=True, text=True, env=env)

    def profile(self, root: Path) -> dict:
        return json.loads(next(root.glob(".agents/*/project.json")).read_text(encoding="utf-8"))["profile"]

    def init_with(self, *args: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            done = self.run_init(root, *BASE, *args, "--apply")
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            return self.profile(root)

    def test_repeat_comma_and_mixed_give_the_same_ordered_value(self) -> None:
        for args in (("--agent-cli", "claude", "--agent-cli", "codex"),
                     ("--agent-cli", "claude,codex"),
                     ("--agent-cli", "claude,codex", "--agent-cli", "codex"),
                     ("--agent-cli", "claude", "--agent-cli", "codex,claude")):
            with self.subTest(args=args):
                self.assertEqual(self.init_with(*args)["agent_cli"], ["claude", "codex"])

    def test_project_type_and_stack_accumulate_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = ("--name", "demo", "--project-type", "code", "--project-type", "docs,code", "--vcs", "git",
                    "--stack", "python", "--stack", "go,python", "--runtime", "local", "--agent-cli", "codex", "--apply")
            done = self.run_init(root, *args)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            profile = self.profile(root)
            self.assertEqual(profile["project_type"], ["code", "docs"])
            self.assertEqual(profile["stack"], ["python", "go"])

    def test_single_value_unchanged_and_empty_value_rejected(self) -> None:
        self.assertEqual(self.init_with("--agent-cli", "codex")["agent_cli"], ["codex"])
        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_init(Path(tmp), *BASE, "--agent-cli", " , ")
            self.assertNotEqual(done.returncode, 0)

    def test_help_documents_both_spellings_in_both_languages(self) -> None:
        zh = self.run_init(Path(self.home.name), "--help").stdout
        self.assertIn("重复传参", zh)
        self.assertIn("逗号分隔", zh)
        en = self.run_init(Path(self.home.name), "--help", lang="en").stdout
        self.assertIn("repeat the flag", en)
        self.assertIn("comma-separate", en)

    def test_modes_without_these_flags_keep_their_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self.run_init(root, *BASE, "--agent-cli", "codex", "--apply").returncode, 0)
            for mode in ("check", "refresh"):
                with self.subTest(mode=mode):
                    done = self.run_init(root, "--mode", mode)
                    self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "AGENTS.md").write_text("# mine\n", encoding="utf-8")
            done = self.run_init(Path(tmp), "--mode", "scan")
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
