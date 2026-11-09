"""#79：Profile 的 language 与已渲染文件的语言不一致时，check / update --only content 指向预览入口，且不改写任何文件。"""
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
INIT = REPO / "plugins/agents-init/skills/agents-init/scripts/init_project.py"
CLI = REPO / "plugins/agent-pack/runtime/agenthelm.py"
LATEST = REPO / "latest.json"
LOCK = ".agents/moe.sakanano.agent-pack/content.lock.json"
PROFILE = ".agents/moe.sakanano.agent-pack/project.json"
CJK = re.compile(r"[一-鿿]")


class LanguageDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        (self.base / "home").mkdir()
        self.root = self.base / "proj"
        self.root.mkdir()
        self.env = {"HOME": str(self.base / "home"), "PATH": os.environ.get("PATH", "")}

    def init(self, *extra: str) -> None:
        done = subprocess.run([sys.executable, str(INIT), "--project", str(self.root), "--name", "T", "--project-type", "notes",
                               "--vcs", "none", "--stack", "markdown", "--runtime", "local", "--agent-cli", "claude",
                               "--output", "json", "--apply", *extra],
                              capture_output=True, text=True, env={**self.env, "AGENTHELM_LANG": "zh"}, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def cli(self, *args: str, lang: str = "zh") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(CLI), *args, "--project", str(self.root), "--latest", str(LATEST)],
                              capture_output=True, text=True, check=False, env={**self.env, "AGENTHELM_LANG": lang}, timeout=120)

    def switch_to_english(self) -> None:
        done = subprocess.run([sys.executable, str(CLI), "config", "project-language", "en", "--apply", "--project", str(self.root),
                               "--output", "json"], capture_output=True, text=True, check=False,
                              env={**self.env, "AGENTHELM_LANG": "en"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def tree(self) -> dict[Path, bytes]:
        return {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def drift(self, lang: str = "zh") -> dict:
        return json.loads(self.cli("check", "--output", "json", lang=lang).stdout)["language"]

    def test_switching_the_profile_language_is_reported_with_a_preview_command(self) -> None:
        self.init("--language", "zh")
        self.assertEqual(self.drift()["drift"], [])
        self.switch_to_english()
        before = self.tree()
        for lang in ("zh", "en"):
            text = self.cli("check", lang=lang)
            self.assertEqual(text.returncode, 0, text.stdout)
            self.assertIn("fix update-content", text.stdout)
            self.assertIn("init_project.py --mode refresh", text.stdout)
            if lang == "en":
                self.assertFalse(CJK.search(text.stdout), text.stdout)
            section = self.drift(lang)
            self.assertEqual([(item["profile"], item["rendered"]) for item in section["drift"]], [("en", "zh")] * len(section["drift"]))
            self.assertTrue(section["drift"])
            self.assertTrue(any("fix update-content" in hint for hint in section["hints"]))
        self.assertEqual(self.tree(), before)  # check writes nothing

    def test_the_hint_is_advisory_for_exit_code_strict_and_summary(self) -> None:
        self.init("--language", "zh")
        quiet = self.cli("check", "--output", "json", "--strict")
        self.assertEqual(quiet.returncode, 0, quiet.stdout)
        self.switch_to_english()
        loud = self.cli("check", "--output", "json", "--strict")
        self.assertEqual(loud.returncode, quiet.returncode)
        before, after = json.loads(quiet.stdout), json.loads(loud.stdout)
        self.assertEqual(after["summary"], before["summary"])
        self.assertEqual(after["scaffold"]["findings"], before["scaffold"]["findings"])
        self.assertTrue(after["language"]["drift"])
        self.assertFalse(before["language"]["drift"])

    def test_update_content_points_at_the_preview_instead_of_nothing_to_do(self) -> None:
        self.init("--language", "zh")
        self.switch_to_english()
        before = self.tree()
        for lang in ("zh", "en"):
            done = self.cli("update", "--only", "content", lang=lang)
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertIn("fix update-content", done.stdout)
            if lang == "en":
                self.assertFalse(CJK.search(done.stdout), done.stdout)
        self.assertEqual(self.tree(), before)

    def test_projects_that_agree_or_never_declared_a_language_get_no_hint(self) -> None:
        self.init("--language", "en")
        self.assertEqual(self.drift("en")["drift"], [])
        self.assertNotIn("fix update-content", self.cli("check", lang="en").stdout)
        self.assertNotIn("fix update-content", self.cli("update", "--only", "content", lang="en").stdout)

        (self.base / "other").mkdir()
        self.root = self.base / "other"
        self.init("--language", "zh")
        profile = self.root / PROFILE
        data = json.loads(profile.read_text(encoding="utf-8"))
        data.pop("language")  # a project that predates the field: zh
        profile.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.drift("en")["drift"], [])
        lock = self.root / LOCK
        content = json.loads(lock.read_text(encoding="utf-8"))
        for entry in content["packs"].values():
            entry["render_inputs"].pop("language", None)  # a pre-6.9 lock: rendered in zh
        lock.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.drift("en")["drift"], [])
        for entry in content["packs"].values():
            entry.pop("render_inputs")  # nothing recorded: cannot judge, no false alarm
        lock.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        data["language"] = "en"
        profile.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.drift("en")["drift"], [])

    def test_a_pre_6_9_lock_without_the_language_key_drifts_once_the_profile_says_en(self) -> None:
        self.init("--language", "zh")
        content = json.loads((self.root / LOCK).read_text(encoding="utf-8"))
        for entry in content["packs"].values():
            entry["render_inputs"].pop("language", None)
        (self.root / LOCK).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.switch_to_english()
        self.assertTrue(self.drift("en")["drift"])

    def test_the_hint_goes_away_after_the_refresh_it_points_to(self) -> None:
        self.init("--language", "zh")
        self.switch_to_english()
        self.assertTrue(self.drift("en")["drift"])
        done = subprocess.run([sys.executable, str(INIT), "--project", str(self.root), "--mode", "refresh", "--apply", "--output", "json",
                               "--recovery-dir", str(self.base / "rec")],
                              capture_output=True, text=True, env={**self.env, "AGENTHELM_LANG": "en"}, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.drift("en")["drift"], [])


if __name__ == "__main__":
    unittest.main()
