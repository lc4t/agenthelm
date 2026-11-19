"""inbox brief 的确认模式：协议按 ask / digest / quiet 生成，体积有上限，命令行可被 shlex 拆开。"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))

import inbox_text  # noqa: E402

SCRIPT = RUNTIME / "opinion_manager.py"
SESSION = "session-0123456789abcdef"
ASK_SENTENCES = {"zh": "任务完成或即将完成", "en": "the task is done or nearly done"}


def protocol(language: str, mode: str, **options: object) -> str:
    return inbox_text.protocol(language, runtime=SCRIPT, project="/tmp/project", lang=language, session=SESSION, mode=mode, **options)


class ConfirmModeTests(unittest.TestCase):
    def test_ask_mode_gates_presents_and_adopts_from_the_preview(self) -> None:
        for language in inbox_text.LANGUAGES:
            text = protocol(language, "ask")
            for needle in ("inbox gate", f"--session {SESSION}", "inbox present",
                           "shown_matches", "--apply --confirm", ".opinion/profiles/", ASK_SENTENCES[language]):
                self.assertIn(needle, text, (language, needle))
            self.assertNotIn("undo", text)
            self.assertNotIn("--cooldown-hours", text, "defaults are left out of the command line")
            self.assertNotIn("--max-per-session", text)

    def test_digest_and_quiet_never_ask_at_task_completion(self) -> None:
        for language in inbox_text.LANGUAGES:
            for mode in ("digest", "quiet"):
                text = protocol(language, mode)
                self.assertNotIn(ASK_SENTENCES[language], text, (language, mode))
                self.assertNotIn("inbox gate", text, (language, mode))
                self.assertIn("shown_matches", text)

    def test_options_reach_the_ask_text(self) -> None:
        text = protocol("zh", "ask", cooldown=6, limit=2)
        self.assertIn("--cooldown-hours 6", text)
        self.assertIn("--max-per-session 2", text)

    def test_chinese_protocol_stays_within_the_budget_in_every_mode(self) -> None:
        for mode in ("ask", "digest", "quiet"):
            self.assertLessEqual(len(protocol("zh", mode).encode("utf-8")), 3000, mode)

    def test_budget_holds_with_a_long_checkout_path(self) -> None:
        runtime = Path("/home/runner/work/agenthelm/agenthelm/plugins/opinion-manager/runtime/opinion_manager.py")
        text = inbox_text.protocol("zh", runtime=runtime, project="/tmp/project", lang="zh", session=SESSION, mode="ask")
        self.assertLessEqual(len(text.encode("utf-8")), 3000)

    def test_no_command_argument_contains_square_brackets(self) -> None:
        for language in inbox_text.LANGUAGES:
            for mode in ("ask", "digest", "quiet"):
                for line in protocol(language, mode).splitlines():
                    if "python3" in line:
                        for command in line.split("python3")[1:]:
                            words = shlex.split("python3" + command.split("；")[0].split(";")[0])
                            self.assertFalse([word for word in words if "[" in word or "]" in word], (language, mode, line))


class BriefCommandTests(unittest.TestCase):
    def brief(self, *options: str) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            done = subprocess.run([sys.executable, str(SCRIPT), "inbox", "brief", "--project", directory, "--session", SESSION, *options],
                                  capture_output=True, text=True, check=False, env={**os.environ, "AGENTHELM_LANG": "zh"})
        return json.loads(done.stdout) if done.returncode == 0 else {"error": done.stderr or done.stdout}

    def test_default_is_ask_and_modes_are_reported(self) -> None:
        self.assertEqual(self.brief()["mode"], "ask")
        self.assertIn("inbox gate", self.brief()["text"])
        for mode in ("digest", "quiet"):
            result = self.brief("--mode", mode)
            self.assertEqual(result["mode"], mode)
            self.assertNotIn("inbox gate", result["text"])

    def test_cooldown_and_limit_options_are_validated_and_used(self) -> None:
        self.assertIn("--cooldown-hours 6", self.brief("--cooldown-hours", "6", "--max-per-session", "2")["text"])
        self.assertIn("error", self.brief("--cooldown-hours", "-1"))
        self.assertIn("error", self.brief("--max-per-session", "0"))


if __name__ == "__main__":
    unittest.main()
