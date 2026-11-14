"""#100 (second item): the adopt preview names the backup location that apply will really use."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "init_project.py"
ARGS = ("--mode", "adopt", "--agents-policy", "append", "--name", "d", "--project-type", "code", "--vcs", "git",
        "--stack", "python", "--runtime", "local", "--agent-cli", "codex")


class AdoptRecoveryPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.root = base / "proj"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text("# mine\n", encoding="utf-8")
        self.outside = base / "elsewhere"

    def run_init(self, *extra: str, lang: str = "zh", home: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "HOME": str(home or self.home), "AGENTHELM_LANG": lang}
        return subprocess.run([sys.executable, str(SCRIPT), "--project", str(self.root), *ARGS, *extra],
                              check=False, capture_output=True, text=True, env=env)

    def test_default_location_is_named_with_the_real_home_and_a_timestamp_placeholder(self) -> None:
        for lang, marker in (("zh", "默认目录"), ("en", "default folder")):
            with self.subTest(lang=lang):
                done = self.run_init(lang=lang)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn(marker, done.stdout)
                self.assertIn(str(self.home / ".agent-pack" / "recovery" / "<UTC time>-agenthelm-adopt"), done.stdout)

    def test_explicit_recovery_dir_is_shown_instead_of_the_default(self) -> None:
        for lang, marker in (("zh", "（--recovery-dir）"), ("en", "(--recovery-dir)")):
            with self.subTest(lang=lang):
                done = self.run_init("--recovery-dir", str(self.outside), lang=lang)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn(str(self.outside), done.stdout)
                self.assertIn(marker, done.stdout)
                self.assertNotIn(".agent-pack/recovery", done.stdout)

    def test_recovery_dir_inside_the_project_is_refused_at_preview_like_apply(self) -> None:
        inside = self.root / "bk"
        preview = self.run_init("--recovery-dir", str(inside))
        applied = self.run_init("--recovery-dir", str(inside), "--apply")
        self.assertEqual(preview.returncode, 2)
        self.assertEqual(preview.stderr, applied.stderr)
        self.assertIn("must be outside the project root", preview.stderr)
        self.assertEqual((self.root / "AGENTS.md").read_text(encoding="utf-8"), "# mine\n")

    def test_default_location_inside_the_project_is_refused_at_preview(self) -> None:
        done = self.run_init(home=self.root)
        self.assertEqual(done.returncode, 2)
        self.assertIn("--recovery-dir", done.stderr)

    def test_the_apply_backup_lands_where_the_preview_said(self) -> None:
        done = self.run_init("--recovery-dir", str(self.outside), "--apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue((self.outside / "AGENTS.md").is_file())

    def test_no_recovery_line_when_nothing_is_modified(self) -> None:
        (self.root / "AGENTS.md").unlink()
        done = self.run_init()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(".agent-pack/recovery", done.stdout)


if __name__ == "__main__":
    unittest.main()
