"""``private/.gitignore`` written in the other language than the project file language (found in the 6.14.0 acceptance).

Projects initialised with ``--language en`` by v6.13.1 or older got the Chinese text: the file is not in the content
lock, so refresh and entry never touched it. Now refresh and entry replace it, after a backup, when its text is exactly
one the tool wrote in the other language; ``check`` only says so (information, exit code unchanged).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "init_project.py"
REPO = SCRIPTS.parents[4]
sys.path.insert(0, str(SCRIPTS))
import init_project  # noqa: E402

CJK = re.compile("[一-鿿]")
ZH, EN = init_project.PRIVATE_IGNORE, init_project.PRIVATE_IGNORE_EN
INIT = ("--name", "Demo", "--project-type", "code", "--vcs", "git", "--stack", "python", "--runtime", "local",
        "--agent-cli", "codex")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)


def old_release_scripts(tag: str, into: Path) -> Path:
    """The initializer of a published tag, extracted next to nothing else (it is only ever run, never imported)."""
    if git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}").returncode != 0:
        raise unittest.SkipTest(f"release tag {tag} is not in this clone; run `git fetch --tags origin`")
    into.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(REPO), "archive", tag, "plugins"], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive.stdout, check=True)
    return into / "plugins/agents-init/skills/agents-init/scripts/init_project.py"


class PrivateIgnoreLanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.root = base / "proj"
        self.root.mkdir()
        self.recovery = base / "recovery"
        self.target = self.root / "private/.gitignore"

    def run_script(self, script: Path, *args: str, lang: str = "en") -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "HOME": str(self.home), "AGENTHELM_LANG": lang, "NO_COLOR": "1"}
        return subprocess.run([sys.executable, "-I", str(script), "--project", str(self.root), *args],
                              capture_output=True, text=True, check=False, env=env)

    def run_json(self, *args: str, script: Path = SCRIPT, lang: str = "en") -> tuple[int, dict]:
        done = self.run_script(script, *args, "--output", "json", lang=lang)
        return done.returncode, json.loads(done.stdout)

    def init_current(self, language: str) -> None:
        done = self.run_script(SCRIPT, *INIT, "--language", language, "--apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def init_v6131_english(self) -> None:
        old = old_release_scripts("v6.13.1", Path(self.tmp.name) / "old")
        done = self.run_script(old, *INIT, "--language", "en", "--apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.target.read_text(encoding="utf-8"), ZH, "v6.13.1 wrote the Chinese text for --language en")

    # ------------------------------------------------------------ v6.13.1 english project
    def test_check_says_so_without_changing_the_exit_code(self) -> None:
        self.init_v6131_english()
        code, report = self.run_json("--mode", "check")
        self.assertEqual((code, report["findings"], report["ok"]), (0, [], True))
        (row,) = report["private_ignore_language"]
        self.assertEqual((row["state"], row["found"], row["language"], row["level"]), ("mismatch", "zh", "en", "info"))
        self.assertIn("--mode refresh", row["message"])
        text = {lang: self.run_script(SCRIPT, "--mode", "check", lang=lang).stdout for lang in ("zh", "en")}
        self.assertIn("[信息] private/.gitignore 是工具写过的中文原文，与项目文件语言（英文）不符", text["zh"])
        self.assertIn("[info] private/.gitignore holds the Chinese text the tool wrote", text["en"])
        self.assertFalse(CJK.search(text["en"]), text["en"])

    def test_refresh_previews_then_replaces_after_a_backup(self) -> None:
        self.init_v6131_english()
        code, preview = self.run_json("--mode", "refresh")
        self.assertEqual(code, 0)
        self.assertEqual(preview["private_ignore"]["state"], "mismatch")
        self.assertIn("-# agenthelm: 本目录", preview["private_ignore"]["diff"])
        self.assertEqual(self.target.read_text(encoding="utf-8"), ZH, "preview writes nothing")
        lines = {lang: self.run_script(SCRIPT, "--mode", "refresh", lang=lang).stdout for lang in ("zh", "en")}
        self.assertIn("private/.gitignore：内容是工具写过的中文原文，与项目文件语言（英文）不符", lines["zh"])
        self.assertIn("private/.gitignore: it holds the Chinese text the tool wrote", lines["en"])

        code, applied = self.run_json("--mode", "refresh", "--apply", "--recovery-dir", str(self.recovery))
        self.assertEqual(code, 0, applied)
        self.assertTrue(applied["private_ignore"]["applied"])
        self.assertEqual(self.target.read_text(encoding="utf-8"), EN)
        self.assertIn("private/.gitignore", applied["recovery_files"])
        backup = self.recovery / "private/.gitignore"
        self.assertEqual(backup.read_text(encoding="utf-8"), ZH)
        shutil.copy2(backup, self.target)  # the documented restore: copy it back
        self.assertEqual(self.target.read_text(encoding="utf-8"), ZH)
        # and a second refresh --apply does the same again; once replaced there is nothing left to say
        self.run_json("--mode", "refresh", "--apply", "--recovery-dir", str(self.recovery / "again"))
        code, report = self.run_json("--mode", "check")
        self.assertEqual((code, report["private_ignore_language"]), (0, []))
        self.assertIsNone(self.run_json("--mode", "refresh")[1]["private_ignore"])

    def test_entry_previews_then_replaces_after_a_backup(self) -> None:
        self.init_v6131_english()
        entry = ("--mode", "entry", "--entry-name", "menu")
        # the old launcher differs from the template: without --accept the entry command is blocked and writes nothing
        code, blocked = self.run_json(*entry, "--apply", "--recovery-dir", str(self.recovery))
        self.assertEqual((code, blocked["applied"]), (1, False))
        self.assertEqual(self.target.read_text(encoding="utf-8"), ZH)

        code, preview = self.run_json(*entry, "--accept", "menu")
        self.assertEqual(code, 0)
        self.assertEqual(preview["private_ignore"]["state"], "mismatch")
        self.assertIn("private/.gitignore", preview["entry"]["writes"])
        self.assertEqual(self.target.read_text(encoding="utf-8"), ZH, "preview writes nothing")
        text = self.run_script(SCRIPT, *entry, "--accept", "menu", "--apply", "--recovery-dir", str(self.recovery),
                               lang="en").stdout
        self.assertIn("private/.gitignore: the Chinese text was replaced with the English version", text)
        self.assertEqual(self.target.read_text(encoding="utf-8"), EN)
        self.assertEqual((self.recovery / "private/.gitignore").read_text(encoding="utf-8"), ZH)
        self.assertTrue((self.recovery / "menu").is_file(), "the replaced launcher is backed up in the same folder")

    def test_a_file_the_user_edited_is_left_alone_and_said_so(self) -> None:
        self.init_v6131_english()
        edited = ZH + "secret-notes/\n"
        self.target.write_text(edited, encoding="utf-8")
        code, report = self.run_json("--mode", "check")
        self.assertEqual((code, report["private_ignore_language"][0]["state"]), (0, "modified"))
        for mode_args in (("--mode", "refresh"), ("--mode", "entry", "--entry-name", "menu", "--accept", "menu")):
            code, result = self.run_json(*mode_args, "--apply", "--recovery-dir", str(self.recovery))
            self.assertEqual(code, 0, result)
            self.assertEqual(result["private_ignore"]["state"], "modified")
            self.assertFalse(result["private_ignore"]["applied"])
            self.assertEqual(self.target.read_text(encoding="utf-8"), edited)
        self.assertFalse((self.recovery / "private").exists(), "nothing was replaced, nothing to back up")
        out = {lang: self.run_script(SCRIPT, "--mode", "refresh", lang=lang).stdout for lang in ("zh", "en")}
        self.assertIn("内容已被修改，未改动", out["zh"])
        self.assertIn("has been edited; left unchanged", out["en"])

    # ------------------------------------------------------------ projects that need nothing
    def test_projects_already_in_the_right_language_see_no_change(self) -> None:
        for language, expected in (("en", EN), ("zh", ZH)):
            with self.subTest(language=language):
                shutil.rmtree(self.root)
                self.root.mkdir()
                self.init_current(language)
                self.assertEqual(self.target.read_text(encoding="utf-8"), expected)
                code, report = self.run_json("--mode", "check")
                self.assertEqual((code, report["private_ignore_language"]), (0, []))
                for mode_args in (("--mode", "refresh"), ("--mode", "entry", "--entry-name", "menu")):
                    _, result = self.run_json(*mode_args)
                    self.assertIsNone(result["private_ignore"])
                text = self.run_script(SCRIPT, "--mode", "refresh").stdout
                self.assertNotIn("private/.gitignore", text)

    def test_a_chinese_project_with_the_english_text_is_replaced_the_other_way(self) -> None:
        self.init_current("zh")
        self.target.write_text(EN, encoding="utf-8")
        code, applied = self.run_json("--mode", "refresh", "--apply", "--recovery-dir", str(self.recovery))
        self.assertEqual(code, 0, applied)
        self.assertEqual(self.target.read_text(encoding="utf-8"), ZH)
        self.assertEqual((self.recovery / "private/.gitignore").read_text(encoding="utf-8"), EN)

    def test_the_users_own_file_and_links_are_not_this_tools_business(self) -> None:
        self.init_current("en")
        self.target.write_text("# mine\n*\n", encoding="utf-8")
        self.assertIsNone(self.run_json("--mode", "refresh")[1]["private_ignore"])
        self.assertEqual(self.run_json("--mode", "check")[1]["private_ignore_language"], [])
        self.target.unlink()
        outside = Path(self.tmp.name) / "outside-ignore"
        outside.write_text(ZH, encoding="utf-8")
        self.target.symlink_to(outside)
        self.assertIsNone(self.run_json("--mode", "refresh", "--apply", "--recovery-dir", str(self.recovery))[1]["private_ignore"])
        self.assertEqual(outside.read_text(encoding="utf-8"), ZH)
        self.assertTrue(self.target.is_symlink())

    # ------------------------------------------------------------ the list of texts the tool ever wrote
    def test_every_text_released_so_far_is_recognised(self) -> None:
        tags = [tag for tag in git("tag", "--list", "v6.*").stdout.split() if re.fullmatch(r"v6\.(1[1-9]|[2-9]\d)\..*", tag)]
        if not tags:
            self.skipTest("no release tag from v6.11.0-rc.1 on is in this clone; run `git fetch --tags origin`")
        pattern = re.compile(r'^(?:PRIVATE_IGNORE|PRIVATE_IGNORE_EN|IGNORE_TEXT|IGNORE_TEXT_EN) = (".*")$', re.MULTILINE)
        seen: set[str] = set()
        for tag in tags:
            for path in ("plugins/agents-init/skills/agents-init/scripts/init_project.py",
                         "plugins/project-orchestrator/runtime/private_paths.py"):
                shown = git("show", f"{tag}:{path}")
                if shown.returncode == 0:
                    seen |= {json.loads(match) for match in pattern.findall(shown.stdout)}
        self.assertTrue(seen)
        self.assertLessEqual(seen, set(init_project.PRIVATE_IGNORE_KNOWN.values()),
                             "a released private/.gitignore text is missing from PRIVATE_IGNORE_KNOWN")


if __name__ == "__main__":
    unittest.main()
