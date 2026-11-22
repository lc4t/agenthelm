"""#115: ``--mode check`` tells the user when a Profile field no longer matches the inputs the content lock recorded.

The notice is information only: it is outside ``findings`` so ``ok`` and the exit code do not change.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import init_project  # noqa: E402

SCRIPT = Path(__file__).with_name("init_project.py")
PROFILE = Path(".agents/sample-project/project.json")


class RenderInputDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        base = Path(self._temporary.name).resolve()
        self.root = base / "sample-project"
        self.root.mkdir()
        self.home = base / "home"
        self.home.mkdir()
        self.run_cli("--name", "Sample Project", "--project-type", "notes", "--vcs", "none", "--stack", "markdown",
                     "--runtime", "local", "--agent-cli", "codex", "--output", "json", "--apply")

    def run_cli(self, *args: str, lang: str = "zh") -> subprocess.CompletedProcess:
        env = {**os.environ, "HOME": str(self.home), "AGENTHELM_LANG": lang, "NO_COLOR": "1"}
        completed = subprocess.run([sys.executable, "-I", str(SCRIPT), "--project", str(self.root), *args],
                                   capture_output=True, text=True, check=False, env=env)
        if args and "--mode" not in args:
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return completed

    def profile_path(self) -> Path:
        return next((self.root / ".agents").glob("*/project.json"))

    def edit_profile(self, mutate) -> None:
        path = self.profile_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def check(self, lang: str = "zh") -> tuple[int, dict]:
        completed = self.run_cli("--mode", "check", "--output", "json", lang=lang)
        return completed.returncode, json.loads(completed.stdout)

    def test_consistent_project_has_no_notice(self) -> None:
        code, report = self.check()
        self.assertEqual((code, report["render_input_drift"], report["findings"]), (0, [], []))
        text = self.run_cli("--mode", "check").stdout
        self.assertNotIn("[信息]", text)

    def test_single_field_drift_is_reported_without_touching_exit_code(self) -> None:
        self.edit_profile(lambda p: p["profile"].update(agent_cli=["claude", "codex"]))
        code, report = self.check()
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"] and report["required_ok"])
        self.assertEqual(report["findings"], [])
        (row,) = report["render_input_drift"]
        self.assertEqual((row["field"], row["locked"], row["profile"], row["level"]),
                         ("agent_cli", ["codex"], ["claude", "codex"], "info"))
        self.assertIn("--mode refresh", row["message"])

    def test_every_profile_derived_field_is_covered(self) -> None:
        cases = {
            "name": lambda p: p.update(name="Other Name"),
            "project_type": lambda p: p["profile"].update(project_type=["notes", "cli"]),
            "stack": lambda p: p["profile"].update(stack=["markdown", "go"]),
            "vcs": lambda p: p["profile"].update(vcs="git"),
            "runtime": lambda p: p["profile"].update(runtime="py312"),
            "agent_cli": lambda p: p["profile"].update(agent_cli=["claude"]),
            "work_mode": lambda p: p["work"].update(mode="legacy" if p["work"]["mode"] != "legacy" else "case-workspace"),
            "language": lambda p: p.update(language="en" if p.get("language") != "en" else "zh"),
        }
        for field, mutate in cases.items():
            with self.subTest(field=field):
                self.setUp()
                self.edit_profile(mutate)
                code, report = self.check()
                self.assertEqual([row["field"] for row in report["render_input_drift"]], [field])
                self.assertEqual(code, 0, report["findings"])

    def test_several_fields_are_all_listed_in_lock_order(self) -> None:
        self.edit_profile(lambda p: p["profile"].update(stack=["markdown", "go"], vcs="git", agent_cli=["claude"]))
        code, report = self.check()
        self.assertEqual(code, 0)
        self.assertEqual([row["field"] for row in report["render_input_drift"]], ["stack", "vcs", "agent_cli"])

    def test_other_checks_keep_their_own_exit_code(self) -> None:
        (self.root / "docs/refs/README.md").unlink()
        self.edit_profile(lambda p: p["profile"].update(runtime="py312"))
        code, report = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(len(report["render_input_drift"]), 1)

    def test_lock_without_render_inputs_has_nothing_to_compare(self) -> None:
        lock = self.root / init_project.CONTENT_LOCK_PATH
        data = json.loads(lock.read_text(encoding="utf-8"))
        for entry in data["packs"].values():
            entry.pop("render_inputs", None)
        lock.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.edit_profile(lambda p: p["profile"].update(agent_cli=["claude"]))
        self.assertEqual(self.check()[1]["render_input_drift"], [])

    def test_refresh_apply_clears_the_notice(self) -> None:
        self.edit_profile(lambda p: p["profile"].update(agent_cli=["claude", "codex"]))
        self.assertEqual(len(self.check()[1]["render_input_drift"]), 1)
        refreshed = self.run_cli("--mode", "refresh", "--apply", "--output", "json")
        self.assertEqual(refreshed.returncode, 0, refreshed.stdout + refreshed.stderr)
        self.assertEqual(self.check()[1]["render_input_drift"], [])

    def test_text_output_in_chinese_and_english(self) -> None:
        self.edit_profile(lambda p: p["profile"].update(agent_cli=["claude", "codex"]))
        zh = self.run_cli("--mode", "check", lang="zh")
        en = self.run_cli("--mode", "check", lang="en")
        self.assertEqual((zh.returncode, en.returncode), (0, 0))
        self.assertIn('[信息] Profile 的 agent_cli 是 ["claude", "codex"]，内容锁记录的渲染输入是 ["codex"]', zh.stdout)
        self.assertIn("--mode refresh 预览并同步", zh.stdout)
        self.assertIn('[info] The Profile\'s agent_cli is ["claude", "codex"], but the content lock recorded ["codex"]',
                      en.stdout)
        self.assertIn("Run --mode refresh to preview and sync", en.stdout)
        self.assertFalse(any("一" <= ch <= "鿿" for ch in en.stdout), en.stdout)


if __name__ == "__main__":
    unittest.main()
