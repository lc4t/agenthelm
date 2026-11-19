"""opinion.nudge.confirm 与 prompt_cooldown_hours：确认模式、跨会话冷却、gate 状态损坏时的静默。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))

import agent_pack_nudge as nudge  # noqa: E402
import agenthelm  # noqa: E402


def merged(**layer) -> dict:
    return nudge.merge_config(layer or None, None)[0]


class ConfirmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name).resolve()
        self.home, self.project = base / "home", base / "project"
        self.home.mkdir()
        (self.project / ".opinion").mkdir(parents=True)
        isolated = {key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "AGENTHELM_", "CLAUDE_", "CODEX_"))}
        patcher = mock.patch.dict(os.environ, {**isolated, "AGENTHELM_LANG": "zh"}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = agenthelm.current_env(self.home)
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / ".gitignore").write_text(".opinion/\n", encoding="utf-8")
        self.manager = RUNTIME / "agenthelm.py"

    def capture(self, title: str, session: str) -> None:
        done = subprocess.run([sys.executable, str(nudge.find_manager(agenthelm, self.env)), "inbox", "add", "--project", str(self.project),
                               "--title", title, "--text", f"{title}的完整规则", "--scope", "回答", "--quote", title, "--session", session],
                              capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(done.stdout)["ok"])

    def stop(self, session: str, config: dict | None = None, client: str = "claude", **extra):
        key = "conversation_id" if client == "cursor" else "session_id"
        payload = {key: session, "cwd": str(self.project), "last_assistant_message": "已完成修改", **extra}
        return nudge.stop(agenthelm, self.env, payload, client, self.project, config or merged())

    def start(self, config: dict, session: str = "s1") -> str:
        payload = {"session_id": session, "cwd": str(self.project), "source": "startup"}
        return nudge.session_start(agenthelm, self.env, payload, "claude", self.project, config)["hookSpecificOutput"]["additionalContext"]

    # -- configuration
    def test_defaults_and_valid_values(self) -> None:
        config = merged()
        self.assertEqual((config["confirm"], config["prompt_cooldown_hours"]), ("ask", 24))
        config = merged(confirm="digest", prompt_cooldown_hours=0)
        self.assertEqual((config["confirm"], config["prompt_cooldown_hours"]), ("digest", 0))

    def test_invalid_values_warn_and_fall_back(self) -> None:
        for layer in ({"confirm": "loud"}, {"confirm": 3}, {"prompt_cooldown_hours": -1}, {"prompt_cooldown_hours": True},
                      {"prompt_cooldown_hours": "24"}):
            with self.subTest(layer=layer):
                config, warnings = nudge.merge_config(layer, None)
                self.assertEqual((config["confirm"], config["prompt_cooldown_hours"]), ("ask", 24))
                self.assertEqual(len(warnings), 1)
                self.assertIn(next(iter(layer)), warnings[0])
        _, warnings = nudge.merge_config({"language": "en", "confirm": "loud"}, None)
        self.assertEqual(warnings, ["Project Profile opinion.nudge.confirm must be ask/digest/quiet; ignored"])

    def test_user_config_overrides_the_project_profile(self) -> None:
        config, warnings = nudge.merge_config({"confirm": "quiet"}, {"confirm": "digest", "prompt_cooldown_hours": 6})
        self.assertEqual((config["confirm"], config["prompt_cooldown_hours"], warnings), ("digest", 6, []))

    # -- session start passes the mode
    def test_session_start_injects_the_protocol_of_the_configured_mode(self) -> None:
        ask = self.start(merged(prompt_cooldown_hours=6, max_prompts_per_session=2))
        self.assertIn("inbox gate", ask)
        self.assertIn("--cooldown-hours 6", ask)
        self.assertIn("--max-per-session 2", ask)
        for mode in ("digest", "quiet"):
            text = self.start(merged(confirm=mode))
            self.assertNotIn("inbox gate", text, mode)
            self.assertNotIn("任务完成或即将完成", text, mode)

    # -- stop
    def test_digest_and_quiet_never_request_at_stop(self) -> None:
        self.capture("先给结论", "s1")
        for mode in ("digest", "quiet"):
            self.assertIsNone(self.stop("s1", merged(confirm=mode, stop_prompt="always")), mode)
        self.assertIsNotNone(self.stop("s1"))

    def test_cooldown_spans_sessions_of_the_same_project(self) -> None:
        self.capture("规则一", "s1")
        self.assertIsNotNone(self.stop("s1"))
        self.capture("规则二", "s2")
        self.assertIsNone(self.stop("s2"), "同项目第二个会话在冷却内不再请求")
        listing = subprocess.run([sys.executable, str(nudge.find_manager(agenthelm, self.env)), "inbox", "list", "--project", str(self.project),
                                  "--unpresented", "--session", "s2"], capture_output=True, text=True, check=True)
        self.assertEqual(len(json.loads(listing.stdout)["value"]), 1, "被冷却挡住的候选保持未展示")
        self.assertIsNotNone(self.stop("s2", merged(prompt_cooldown_hours=0)))

    def test_peek_does_not_record_when_nothing_is_requested(self) -> None:
        self.assertIsNone(self.stop("s1"))
        self.assertFalse((self.project / ".opinion" / "ask-state.json").exists())
        self.capture("规则一", "s1")
        self.assertIsNotNone(self.stop("s1"))
        state = json.loads((self.project / ".opinion" / "ask-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["sessions"]["s1"]["count"], 1)

    def test_a_damaged_gate_state_keeps_the_hook_silent(self) -> None:
        self.capture("规则一", "s1")
        (self.project / ".opinion" / "ask-state.json").write_text("{broken", encoding="utf-8")
        self.assertIsNone(self.stop("s1"))
        payload = {"session_id": "s1", "cwd": str(self.project), "last_assistant_message": "已完成修改"}
        import argparse
        arguments = argparse.Namespace(event="stop", client="claude", home=self.home)
        self.assertIsNone(nudge.run_hook(arguments, agenthelm, payload))

    def test_stop_behaviour_for_each_client_payload(self) -> None:
        self.capture("先给结论", "s1")
        claude = self.stop("s1", client="claude")
        self.assertEqual(claude["hookSpecificOutput"]["hookEventName"], "Stop")
        self.capture("用英文", "s2")
        self.assertIsNone(self.stop("s2", client="claude"), "冷却内")
        cooled = merged(prompt_cooldown_hours=0)
        codex = self.stop("s2", cooled, client="codex")
        self.assertEqual(codex["decision"], "block")
        self.capture("简短回答", "c1")
        self.assertIsNone(self.stop("c1", cooled, client="cursor"), "Cursor 的 stop_prompt 默认 off")
        enabled = merged(prompt_cooldown_hours=0, stop_prompt={"cursor": "on-completion"})
        self.assertEqual(list(self.stop("c1", enabled, client="cursor", status="completed")), ["followup_message"])
        self.assertIsNone(self.stop("c1", enabled, client="cursor", status="completed"), "每会话上限")

    # -- status
    def test_status_shows_the_effective_mode_and_cooldown(self) -> None:
        config_file = self.home / ".config" / "agenthelm" / "config.json"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(json.dumps({"nudge": {"confirm": "digest", "prompt_cooldown_hours": 6, "language": "en"}}), encoding="utf-8")
        done = subprocess.run([sys.executable, str(self.manager), "nudge", "status", "--project", str(self.project)], capture_output=True,
                              text=True, check=True, env={**os.environ, "HOME": str(self.home)})
        self.assertIn("Confirmation mode: digest", done.stdout)
        self.assertIn("6 hours", done.stdout)
        as_json = subprocess.run([sys.executable, str(self.manager), "nudge", "status", "--project", str(self.project), "--output", "json"],
                                 capture_output=True, text=True, check=False, env={**os.environ, "HOME": str(self.home)})
        if as_json.returncode == 0:
            self.assertEqual(json.loads(as_json.stdout)["config"]["confirm"], "digest")


if __name__ == "__main__":
    unittest.main()
