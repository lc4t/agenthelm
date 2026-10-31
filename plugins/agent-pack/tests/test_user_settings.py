"""#35：界面语言未设置时的提示、``agenthelm config`` 与项目文件语言。

所有路径、名称与内容都是虚构的通用示例；每个用例用自己的 HOME。
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
import agenthelm  # noqa: E402
import user_settings  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
INIT = REPO / "plugins/agents-init/skills/agents-init/scripts/init_project.py"
CLI = RUNTIME / "agenthelm.py"
PROFILE = ".agents/moe.sakanano.agent-pack/project.json"


class StatusTests(unittest.TestCase):
    def test_where_the_interface_language_comes_from(self) -> None:
        cases = [
            ({}, {}, ("en", "default", False, True)),
            ({"LANG": "zh_CN.UTF-8"}, {}, ("zh", "locale", False, True)),
            ({"LANG": "fr_FR.UTF-8"}, {}, ("en", "locale", False, True)),
            ({"LANG": "C"}, {}, ("en", "default", False, True)),
            ({"AGENTHELM_LANG": "zh", "LANG": "en_US"}, {}, ("zh", "env", True, False)),
            ({"LANG": "en_US"}, {"language": "zh"}, ("zh", "config", True, False)),
            ({"AGENTHELM_LANG": "en"}, {"language": "zh"}, ("en", "env", True, False)),
            ({}, {"language_hint": False}, ("en", "default", False, False)),
            ({"AGENTHELM_LANG": "auto"}, {"language": "auto"}, ("en", "default", False, True)),  # auto means unspecified
        ]
        for variables, settings, expected in cases:
            with self.subTest(variables=variables, settings=settings):
                status = user_settings.language_status(variables, settings)
                self.assertEqual((status["language"], status["source"], status["explicit"], status["hint"]), expected)

    def test_a_chinese_locale_user_stays_in_chinese_and_is_told_so(self) -> None:
        status = user_settings.language_status({"LANG": "zh_CN.UTF-8"}, {})
        text = user_settings.interface_hint(status)
        self.assertIn("当前使用 中文（来自系统 locale）", text)
        self.assertNotIn("English（来自", text)

    def test_the_report_section_reads_the_project_language_from_the_scaffold_check(self) -> None:
        none_declared = {"project_language": {"declared": None, "effective": "zh"}}
        section = user_settings.report_section({"AGENTHELM_LANG": "en"}, {}, none_declared)
        self.assertEqual(section["project"], {"declared": None, "effective": "zh"})
        self.assertEqual(len(section["hints"]), 1)  # only the project one: the interface language is set
        self.assertIn("project-language en", section["hints"][0])
        declared = {"project_language": {"declared": "en", "effective": "en"}}
        self.assertEqual(user_settings.report_section({"AGENTHELM_LANG": "en"}, {}, declared)["hints"], [])
        self.assertEqual(user_settings.report_section({"AGENTHELM_LANG": "en"}, {"language_hint": False}, none_declared)["hints"], [])
        # an older agents-init reports nothing about it: no hint, and no pretending
        self.assertIsNone(user_settings.report_section({"AGENTHELM_LANG": "en"}, {}, {"findings": []})["project"])
        self.assertIsNone(user_settings.report_section({"AGENTHELM_LANG": "en"}, {}, None)["project"])


class CommandCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.config = self.home / ".config/agenthelm/config.json"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("XDG_", "AGENTHELM_", "LC_", "LANG", "CLAUDE_", "CODEX_"))}
        # A Chinese locale and nothing configured: Chinese output, and the language still counts as "not set".
        self.env.update(HOME=str(self.home), LANG="zh_CN.UTF-8")

    def run_cli(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, check=False,
                              env=env or self.env, timeout=120)

    def config_data(self) -> dict:
        return json.loads(self.config.read_text(encoding="utf-8"))


class ConfigCommandTests(CommandCase):
    def test_a_preview_writes_nothing(self) -> None:
        done = self.run_cli("config", "language", "zh")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("预览", done.stdout)
        self.assertIn('language: （未设置） → "zh"', done.stdout)
        self.assertIn("--apply", done.stdout)
        self.assertFalse(self.config.exists())

    def test_apply_writes_the_key_keeps_the_others_and_backs_up_the_original(self) -> None:
        self.config.parent.mkdir(parents=True)
        original = json.dumps({"update_check": False, "nudge": {"enabled": False}, "local_only": {"limits": {"warn_mb": 50}}}, indent=2)
        self.config.write_text(original, encoding="utf-8")
        done = self.run_cli("config", "language", "zh", "--apply", "--output", "json")
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        self.assertTrue(payload["applied"])
        self.assertEqual(self.config_data(), {**json.loads(original), "language": "zh"})  # nothing else touched
        folder = Path(payload["recovery"])
        self.assertEqual(folder.parent, self.home / ".agent-pack/recovery")
        self.assertEqual((folder / "config.json").read_text(encoding="utf-8"), original)

    def test_a_missing_file_is_created_without_a_backup(self) -> None:
        done = self.run_cli("config", "language", "en", "--apply", "--output", "json")
        self.assertEqual(json.loads(done.stdout)["applied"], True)
        self.assertEqual(self.config_data(), {"language": "en"})
        self.assertFalse((self.home / ".agent-pack").exists())

    def test_the_same_value_is_a_no_op_and_auto_removes_the_key(self) -> None:
        self.run_cli("config", "language", "zh", "--apply")
        before = self.config.read_text(encoding="utf-8")
        again = self.run_cli("config", "language", "zh", "--apply")
        self.assertIn("没有需要改的", again.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)
        self.assertFalse((self.home / ".agent-pack").exists())  # nothing changed, so nothing backed up
        done = self.run_cli("config", "language", "auto", "--apply")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("language", self.config_data())

    def test_a_file_that_cannot_be_parsed_is_never_overwritten(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text("{ not json", encoding="utf-8")
        for arguments in (("language", "zh", "--apply"), ("language-hint", "off", "--apply")):
            done = self.run_cli("config", *arguments)
            self.assertEqual(done.returncode, 2, done.stdout)
            self.assertIn("无法读取", done.stdout)
            self.assertEqual(self.config.read_text(encoding="utf-8"), "{ not json")
        self.config.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(self.run_cli("config", "language", "zh", "--apply").returncode, 2)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "[1, 2]")

    def test_show_reports_the_effective_settings(self) -> None:
        done = self.run_cli("config", "show", "--output", "json")
        payload = json.loads(done.stdout)
        self.assertEqual((payload["language"]["effective"], payload["language"]["source"], payload["language_hint"]),
                         ("zh", "locale", True))
        payload = json.loads(self.run_cli("config", "show", "--output", "json", env={**self.env, "LANG": "C"}).stdout)
        self.assertEqual((payload["language"]["effective"], payload["language"]["source"]), ("en", "default"))
        self.run_cli("config", "language", "zh", "--apply")
        self.run_cli("config", "language-hint", "off", "--apply")
        payload = json.loads(self.run_cli("config", "show", "--output", "json").stdout)
        self.assertEqual((payload["language"]["effective"], payload["language"]["source"], payload["language_hint"]),
                         ("zh", "config", False))
        env = {**self.env, "AGENTHELM_LANG": "en"}
        payload = json.loads(self.run_cli("config", "show", "--output", "json", env=env).stdout)
        self.assertEqual((payload["language"]["effective"], payload["language"]["source"]), ("en", "env"))

    def test_hint_switch_round_trips(self) -> None:
        self.run_cli("config", "language-hint", "off", "--apply")
        self.assertEqual(self.config_data(), {"language_hint": False})
        self.run_cli("config", "language-hint", "on", "--apply")
        self.assertEqual(self.config_data(), {})


class CheckHintTests(CommandCase):
    """check 在没有任何语言配置时提示，设置后消失；退出码与“是否最新”不受影响。"""

    def setUp(self) -> None:
        super().setUp()
        self.root = self.base / "project"
        self.root.mkdir()
        done = subprocess.run([sys.executable, str(INIT), "--project", str(self.root), "--name", "Notes", "--project-type", "study",
                               "--vcs", "none", "--stack", "none", "--runtime", "local", "--agent-cli", "none", "--no-entry",
                               "--apply", "--output", "json"], capture_output=True, text=True, check=False, env=self.env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        manifest = json.loads((REPO / "plugins/agents-init/plugin.json").read_text())
        pack = json.loads((REPO / "plugins/agents-init/content.json").read_text())["packs"][0]
        self.latest = self.base / "latest.json"
        self.latest.write_text(json.dumps({"schema": 1, "marketplace": "agenthelm", "released_at": "2026-10-31", "migrations": [],
                                           "plugins": {"agents-init": {"version": manifest["version"], "content": {
                                               pack["id"]: {"version": pack["version"], "kind": "template"}}}}}), encoding="utf-8")

    def check(self, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        return self.run_cli("check", "--project", str(self.root), "--latest", str(self.latest), *extra, env=env)

    def language(self, *extra: str, env: dict | None = None) -> dict:
        return json.loads(self.check("--output", "json", *extra, env=env).stdout)["language"]

    def test_unset_language_is_explained_and_setting_it_makes_the_hint_go_away(self) -> None:
        done = self.check()
        self.assertIn("[语言] 界面语言未设置，当前使用 中文（来自系统 locale）", done.stdout)  # a Chinese locale stays Chinese, and is told so
        self.assertIn("agenthelm config language zh", done.stdout)
        self.assertIn("agenthelm config language-hint off", done.stdout)
        self.assertEqual(len(self.language()["hints"]), 1)
        english = self.check(env={**self.env, "LANG": "C"})
        self.assertIn("[language] The interface language is not set; it is currently English (from the default).", english.stdout)
        self.run_cli("config", "language", "en", "--apply")
        self.assertEqual(self.language()["hints"], [])
        self.assertNotIn("[语言]", self.check().stdout)

    def test_the_environment_variable_also_counts_as_setting_it(self) -> None:
        self.assertEqual(self.language(env={**self.env, "AGENTHELM_LANG": "zh"})["hints"], [])

    def test_the_hint_can_be_turned_off_for_good(self) -> None:
        self.run_cli("config", "language-hint", "off", "--apply")
        section = self.language()
        self.assertEqual((section["hints"], section["hint_dismissed"]), ([], True))
        self.assertNotIn("[语言]", self.check().stdout)

    def test_the_hint_changes_neither_the_exit_code_nor_the_summary_and_writes_nothing(self) -> None:
        before = {path: path.read_bytes() for path in sorted(self.root.rglob("*")) if path.is_file()}
        plain = self.check("--output", "json", "--strict")
        self.run_cli("config", "language", "en", "--apply")
        silent = self.check("--output", "json", "--strict")
        self.assertEqual(plain.returncode, silent.returncode)
        self.assertEqual(json.loads(plain.stdout)["summary"], json.loads(silent.stdout)["summary"])
        self.assertEqual({path: path.read_bytes() for path in sorted(self.root.rglob("*")) if path.is_file()}, before)

    def test_a_project_without_a_language_is_told_so_and_check_only_reads(self) -> None:
        profile_path = self.root / PROFILE
        profile = json.loads(profile_path.read_text())
        profile.pop("language")
        profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        before = profile_path.read_bytes()
        env = {**self.env, "AGENTHELM_LANG": "en"}
        section = self.language(env=env)
        self.assertEqual(section["project"], {"declared": None, "effective": "zh"})
        self.assertEqual(len(section["hints"]), 1)
        self.assertIn("project-language en", section["hints"][0])
        self.assertIn("[language]", self.check(env=env).stdout)
        self.assertEqual(profile_path.read_bytes(), before)  # check never rewrites a project
        declared = self.base / "declared"
        shutil_copy = json.loads(profile_path.read_text())
        shutil_copy["language"] = "en"
        profile_path.write_text(json.dumps(shutil_copy, indent=2), encoding="utf-8")
        self.assertEqual(self.language(env=env)["hints"], [])

    def test_project_language_command_previews_then_changes_only_the_profile(self) -> None:
        profile_path = self.root / PROFILE
        profile = json.loads(profile_path.read_text())
        profile.pop("language")
        profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        files = {path: path.read_bytes() for path in sorted(self.root.rglob("*")) if path.is_file()}
        preview = self.run_cli("config", "project-language", "en", "--project", str(self.root))
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        self.assertIn("预览", preview.stdout)
        self.assertIn('+  "language": "en"', preview.stdout)
        self.assertEqual({path: path.read_bytes() for path in files}, files)
        done = self.run_cli("config", "project-language", "en", "--project", str(self.root), "--apply", "--output", "json")
        payload = json.loads(done.stdout)
        self.assertTrue(payload["applied"], done.stdout)
        self.assertEqual(json.loads(profile_path.read_text())["language"], "en")
        changed = {path for path, content in files.items() if path.read_bytes() != content}
        self.assertEqual(changed, {profile_path})  # no other project file was rewritten
        self.assertEqual((Path(payload["recovery"]) / PROFILE).read_bytes(), files[profile_path])

    def test_project_language_command_needs_a_project(self) -> None:
        empty = self.base / "empty"
        empty.mkdir()
        done = self.run_cli("config", "project-language", "en", "--project", str(empty))
        self.assertEqual(done.returncode, 2, done.stdout)


class SessionStartTests(unittest.TestCase):
    """会话开始：语言未设置时多一行；同一条一天至多一次。"""

    def run_hook(self, report: dict, env: agenthelm.Env) -> str:
        output = io.StringIO()
        with mock.patch.object(agenthelm, "run_check", return_value=report), redirect_stdout(output):
            agenthelm.hook(env, None, "claude-hook")
        return output.getvalue()

    def test_the_hook_mentions_the_language_once_a_day_and_not_when_it_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env = agenthelm.Env(home=Path(temporary), variables={})
            quiet = {"summary": {"up_to_date": True}, "language": {"session_hint": None}}
            self.assertEqual(self.run_hook(quiet, env), "")
            unset = {"summary": {"up_to_date": True}, "language": {"session_hint": user_settings.short_hint(
                {"language": "en", "source": "default"})}}
            first = self.run_hook(unset, env)
            message = json.loads(first)["systemMessage"]
            self.assertIn("agenthelm config language zh|en", message)
            self.assertIn("language-hint off", message)
            self.assertEqual(self.run_hook(unset, env), "")  # the same notice is shown at most once a day


if __name__ == "__main__":
    unittest.main()
