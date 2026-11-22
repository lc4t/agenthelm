"""#116: user-visible refusals in init_project.py go through ``_()``: Chinese by default, the old English text in English."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "init_project.py"
CJK = lambda text: any("一" <= ch <= "鿿" for ch in text)  # noqa: E731
BASE = ("--name", "d", "--project-type", "code", "--vcs", "git", "--stack", "python", "--runtime", "local",
        "--agent-cli", "codex")


class RefusalMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.root = base / "proj"
        self.root.mkdir()

    def run_init(self, *args: str, lang: str, root: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "HOME": str(self.home), "AGENTHELM_LANG": lang}
        return subprocess.run([sys.executable, str(SCRIPT), "--project", str(root or self.root), *args],
                              check=False, capture_output=True, text=True, env=env)

    def refuse(self, *args: str, root: Path | None = None) -> dict[str, str]:
        out = {}
        for lang in ("zh", "en"):
            done = self.run_init(*args, lang=lang, root=root)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            out[lang] = done.stderr
        self.assertTrue(CJK(out["zh"]), out["zh"])
        self.assertFalse(CJK(out["en"]), out["en"])
        return out

    def test_missing_project_directory(self) -> None:
        missing = self.root / "nowhere"
        got = self.refuse(*BASE, root=missing)
        self.assertIn(f"项目目录不存在：{missing}", got["zh"])
        self.assertIn(f"project directory does not exist: {missing}", got["en"])

    def test_name_without_ascii_slug(self) -> None:
        args = ("--name", "项目", "--project-type", "code", "--vcs", "git", "--stack", "python", "--runtime", "local",
                "--agent-cli", "codex")
        got = self.refuse(*args)
        self.assertIn("--name 无法推导出 ASCII slug，请用 --slug 指定", got["zh"])
        self.assertIn("--name needs an ASCII --slug override", got["en"])

    def test_slug_must_be_kebab_case(self) -> None:
        got = self.refuse(*BASE, "--slug", "Not_Kebab")
        self.assertIn("--slug 必须是小写 ASCII 短横线格式（kebab-case）", got["zh"])
        self.assertIn("--slug must be lowercase ASCII kebab-case", got["en"])

    def test_replace_is_for_migrate_only(self) -> None:
        got = self.refuse(*BASE, "--replace", "AGENTS.md")
        self.assertIn("--replace 只能在 migrate 模式使用", got["zh"])
        self.assertIn("--replace is available only in migrate mode", got["en"])

    def test_skip_is_for_migrate_only(self) -> None:
        got = self.refuse(*BASE, "--skip", "AGENTS.md")
        self.assertIn("--skip 只能在 migrate 模式使用", got["zh"])
        self.assertIn("--skip is available only in migrate mode", got["en"])

    def test_symlinked_parent_in_the_project(self) -> None:
        outside = Path(self.tmp.name).resolve() / "outside"
        outside.mkdir()
        (self.root / "docs").symlink_to(outside, target_is_directory=True)
        got = self.refuse(*BASE, "--apply")
        self.assertIn("拒绝处理符号链接的上级目录：docs", got["zh"])
        self.assertIn("refusing symlinked parent: docs", got["en"])

    def test_migrate_refuses_replace_through_symlinked_parent(self) -> None:
        outside = Path(self.tmp.name).resolve() / "outside"
        outside.mkdir()
        (self.root / ".agents").mkdir()
        (self.root / ".agents/skills").symlink_to(outside, target_is_directory=True)
        got = self.refuse("--mode", "migrate", "--replace", ".agents/skills/.gitkeep", "--recovery-dir",
                          str(Path(self.tmp.name).resolve() / "bk"), "--apply", *BASE)
        self.assertIn("拒绝通过符号链接的上级目录替换：.agents/skills/.gitkeep", got["zh"])
        self.assertIn("refusing to replace through symlinked parent: .agents/skills/.gitkeep", got["en"])

    def test_empty_multi_value_flag(self) -> None:
        for lang, message in (("zh", "至少需要一个非空值"), ("en", "expected at least one non-empty value")):
            done = self.run_init("--name", "d", "--stack", ",", lang=lang)
            self.assertEqual(done.returncode, 2)
            self.assertIn(message, done.stderr)


if __name__ == "__main__":
    unittest.main()
