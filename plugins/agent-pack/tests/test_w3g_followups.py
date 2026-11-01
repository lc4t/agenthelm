"""W3-G：write_json_atomic 保留软链接、备份失败清楚报错并中止、runtime use 校验版本名、init_project 文本标签随语言。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RUNTIME = REPO / "plugins/agent-pack/runtime"
CLI = RUNTIME / "agenthelm.py"
INIT = REPO / "plugins/agents-init/skills/agents-init/scripts/init_project.py"
CJK = re.compile(r"[一-鿿]")
sys.path.insert(0, str(RUNTIME))
import agenthelm  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()

    def run_cli(self, *args: str, lang: str = "en") -> subprocess.CompletedProcess[str]:
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", ""), "AGENTHELM_LANG": lang}
        return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, check=False, env=env, timeout=120)


class AtomicWriteKeepsSymlinks(Base):
    def test_a_symlinked_json_stays_a_symlink_and_its_target_is_updated(self) -> None:
        real = self.base / "dotfiles" / "hooks.json"
        real.parent.mkdir()
        real.write_text("{}\n", encoding="utf-8")
        link = self.base / "home" / "hooks.json"
        link.symlink_to(real)
        agenthelm.write_json_atomic(link, {"hooks": {"x": 1}})
        self.assertTrue(link.is_symlink())
        self.assertEqual(json.loads(real.read_text(encoding="utf-8")), {"hooks": {"x": 1}})
        self.assertEqual(sorted(path.name for path in real.parent.iterdir()), ["hooks.json"], "temporary file is gone")

    def test_a_plain_file_and_a_new_file_behave_as_before(self) -> None:
        path = self.base / "new" / "a.json"
        agenthelm.write_json_atomic(path, {"a": 1})
        agenthelm.write_json_atomic(path, {"a": 2})
        self.assertFalse(path.is_symlink())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 2})

    def test_a_dangling_link_is_written_at_its_target(self) -> None:
        target = self.base / "later" / "x.json"
        target.parent.mkdir()
        link = self.base / "x.json"
        link.symlink_to(target)
        agenthelm.write_json_atomic(link, {"ok": True})
        self.assertTrue(link.is_symlink() and target.is_file())


class BackupFailures(Base):
    def test_nudge_install_hooks_stops_before_rewriting_when_the_first_backup_cannot_be_written(self) -> None:
        hooks = self.home / ".cursor" / "hooks.json"
        hooks.parent.mkdir()
        hooks.write_text('{"version": 1, "hooks": {}}\n', encoding="utf-8")
        (hooks.parent / "hooks.json.agenthelm-bak").symlink_to(self.base / "missing-dir" / "bak")  # dangling: cannot be written
        before = hooks.read_bytes()
        for lang in ("en", "zh"):
            done = self.run_cli("nudge", "install-hooks", "--apply", lang=lang)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertNotIn("Traceback", done.stderr)
            self.assertIn("hooks.json", done.stderr)
            self.assertEqual(bool(CJK.search(done.stderr)), lang == "zh", done.stderr)
        self.assertEqual(hooks.read_bytes(), before)

    def test_nudge_backup_keeps_the_write_once_semantics(self) -> None:
        hooks = self.home / ".cursor" / "hooks.json"
        hooks.parent.mkdir()
        hooks.write_text('{"version": 1, "hooks": {}}\n', encoding="utf-8")
        done = self.run_cli("nudge", "install-hooks", "--apply", "--output", "json")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        backup = hooks.parent / "hooks.json.agenthelm-bak"
        first = backup.read_text(encoding="utf-8")
        self.assertEqual(first, '{"version": 1, "hooks": {}}\n')
        self.run_cli("nudge", "install-hooks", "--remove", "--apply")
        self.assertEqual(backup.read_text(encoding="utf-8"), first, "the first original is never refreshed")

    def test_config_stops_before_rewriting_when_the_backup_folder_cannot_be_created(self) -> None:
        self.assertEqual(self.run_cli("config", "language", "en", "--apply").returncode, 0)
        config = self.home / ".config" / "agenthelm" / "config.json"
        before = config.read_bytes()
        (self.home / ".agent-pack").write_text("not a directory", encoding="utf-8")
        for lang in ("en", "zh"):
            done = self.run_cli("config", "language", "zh", "--apply", lang=lang)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertNotIn("Traceback", done.stderr)
            text = done.stdout + done.stderr
            self.assertIn("agent-pack", text)
            self.assertEqual(bool(CJK.search(text)), lang == "zh", text)
        self.assertEqual(config.read_bytes(), before)


class RuntimeUseValidatesTheVersionName(Base):
    def test_path_like_versions_are_rejected_in_both_languages(self) -> None:
        outside = self.home / ".agent-pack" / "outside"
        outside.mkdir(parents=True)
        for value in ("..", "../outside", "a/b", "/etc", ".", "a\\b"):
            for lang in ("en", "zh"):
                done = self.run_cli("runtime", "use", value, "--apply", lang=lang)
                self.assertEqual(done.returncode, 2, f"{value!r}: {done.stdout}{done.stderr}")
                self.assertNotIn("Traceback", done.stderr)
                self.assertEqual(bool(CJK.search(done.stderr)), lang == "zh", done.stderr)
        self.assertFalse((self.home / ".agent-pack" / "runtime").exists() and any((self.home / ".agent-pack" / "runtime").glob("current*")),
                         "nothing was switched")

    def test_an_unknown_but_plain_version_still_gets_the_old_message(self) -> None:
        done = self.run_cli("runtime", "use", "9.9.9")
        self.assertEqual(done.returncode, 2)
        self.assertIn("9.9.9", done.stderr)


class InitProjectTextLabelsFollowTheLanguage(Base):
    def run_init(self, lang: str, *args: str) -> str:
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", ""), "AGENTHELM_LANG": lang}
        done = subprocess.run([sys.executable, str(INIT), "--project", str(self.base / "p"), "--name", "T", "--project-type", "notes",
                               "--vcs", "none", "--stack", "markdown", "--runtime", "local", "--agent-cli", "claude", *args],
                              capture_output=True, text=True, check=False, env=env, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def test_init_refresh_and_entry_labels_are_translated(self) -> None:
        (self.base / "p").mkdir()
        en = self.run_init("en")
        zh = self.run_init("zh")
        self.assertTrue(en.startswith("Dry-run:") and not CJK.search(en), en)
        self.assertTrue(zh.startswith("仅预览："), zh)
        self.run_init("en", "--apply")
        en = self.run_init("en", "--mode", "refresh")
        zh = self.run_init("zh", "--mode", "refresh")
        self.assertRegex(en, r"^(Dry-run|Applied|Blocked): ", en)
        self.assertFalse(CJK.search(en), en)
        self.assertRegex(zh, r"^(仅预览|已应用|已阻塞)：", zh)


if __name__ == "__main__":
    unittest.main()
