"""check 的文本输出：同一插件的不兼容说明只打印一次；Claude 市场被固定在某个 ref 时的提示。"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agenthelm  # noqa: E402
import test_agenthelm_reporting as base  # noqa: E402

LATEST = {
    "schema": 1, "marketplace": "agenthelm", "released_at": "2026-10-21",
    "repository": "https://github.com/example/agenthelm",
    "plugins": {"agent-pack": {"version": "2.0.0"}},
    "migrations": [{"plugin": "agent-pack", "from": "1.0.0", "to": "2.0.0", "change_kind": "code", "breaking": True,
                    "date": "2026-10-21", "summary": "BREAKING-SUMMARY-X", "actions": ["ACTION-STEP-Y"]}],
}


class Fixture(base.Base):
    lang = "zh"

    def setUp(self) -> None:
        super().setUp()
        base.write(self.latest_file, LATEST)
        os.environ["AGENTHELM_LANG"] = self.lang

    def three_clients_behind(self) -> None:
        self.claude_plugin("1.0.0")
        self.codex_plugin("1.0.0")
        self.cursor_plugin("1.0.0")


class DedupeTests(Fixture):
    def test_breaking_notes_print_once_for_three_clients(self) -> None:
        self.three_clients_behind()
        _code, text = self.run_text("check", "--no-project")
        self.assertEqual(text.count("BREAKING-SUMMARY-X"), 1)
        self.assertEqual(text.count("ACTION-STEP-Y"), 1)
        self.assertEqual(text.count("不兼容：同上"), 2)
        first = [line for line in text.splitlines() if "BREAKING-SUMMARY-X" in line][0]
        self.assertIn("不兼容：BREAKING-SUMMARY-X", first)

    def test_json_keeps_the_per_client_detail(self) -> None:
        self.three_clients_behind()
        _code, payload = self.run_json("check", "--no-project")
        behind = [item for item in payload["plugins"] if item["status"] == "behind"]
        self.assertEqual(len(behind), 3)
        for item in behind:
            self.assertEqual(item["steps"][0]["summary"], "BREAKING-SUMMARY-X")
            self.assertEqual(item["steps"][0]["actions"], ["ACTION-STEP-Y"])

    def test_fewer_lines_than_repeating_everything(self) -> None:
        self.three_clients_behind()
        _code, text = self.run_text("check", "--no-project")
        self.claude_plugin("1.0.0")
        _code, single = self.run_text("check", "--no-project", "--client", "claude")
        self.assertLess(len(text.splitlines()), 3 * len(single.splitlines()))


class DedupeEnglishTests(Fixture):
    lang = "en"

    def test_english_text_has_each_step_once(self) -> None:
        self.three_clients_behind()
        _code, text = self.run_text("check", "--no-project")
        self.assertEqual(text.count("BREAKING-SUMMARY-X"), 1)
        self.assertEqual(text.count("same as above"), 2)
        self.assertNotIn("不兼容", text)


class PinnedTests(Fixture):
    def pin(self) -> None:
        base.write(self.home / ".claude/settings.json", {"extraKnownMarketplaces": {"agenthelm": {
            "source": {"source": "github", "repo": "example/agenthelm", "ref": "v1.0.0"}}}})
        base.write(self.home / ".claude/plugins/known_marketplaces.json", {"agenthelm": {"source": {
            "source": "github", "repo": "example/agenthelm", "ref": "v1.0.0"}}})  # Claude Code has registered it (else A1 blocks)
        self.claude_plugin("1.0.0")
        self.stub("claude")

    def test_preview_warns_about_the_pinned_ref_and_does_not_touch_settings(self) -> None:
        self.pin()
        settings = self.home / ".claude/settings.json"
        before = settings.read_text()
        _code, text = self.run_text("update", "--no-project", "--only", "plugins")
        self.assertIn("固定在 v1.0.0", text)
        self.assertIn("最新版本 2.0.0", text)
        self.assertIn("去掉 ref 跟随 main", text)
        self.assertEqual(settings.read_text(), before)

    def test_apply_without_change_explains_the_pin(self) -> None:
        self.pin()
        code, payload = self.run_json("update", "--apply", "--no-project", "--only", "plugins")
        step = [s for s in payload["steps"] if s["description"].startswith("Claude Code 更新")][0]
        self.assertEqual(step["status"], "unchanged")
        self.assertIn("固定在 v1.0.0", step["reason"])
        self.assertEqual(code, 1)
        _code, text = self.run_text("update", "--apply", "--no-project", "--only", "plugins")
        self.assertIn("[完成，无变化]", text)
        self.assertIn("原因：Claude 的 settings 把 agenthelm 市场固定在 v1.0.0", text)

    def test_unpinned_claude_has_no_pin_note(self) -> None:
        self.claude_plugin("1.0.0")
        self.stub("claude")
        _code, text = self.run_text("update", "--no-project", "--only", "plugins")
        self.assertNotIn("固定在", text)


class PinnedEnglishTests(PinnedTests):
    lang = "en"

    def test_preview_warns_about_the_pinned_ref_and_does_not_touch_settings(self) -> None:
        self.pin()
        _code, text = self.run_text("update", "--no-project", "--only", "plugins")
        self.assertIn("pin the agenthelm marketplace to v1.0.0", text)
        self.assertIn("latest version: 2.0.0", text)
        self.assertIn("remove the ref to follow main", text)

    def test_apply_without_change_explains_the_pin(self) -> None:
        self.pin()
        _code, payload = self.run_json("update", "--apply", "--no-project", "--only", "plugins")
        step = [s for s in payload["steps"] if s["description"].startswith("Claude Code: update")][0]
        self.assertIn("pin the agenthelm marketplace to v1.0.0", step["reason"])

    def test_unpinned_claude_has_no_pin_note(self) -> None:
        self.claude_plugin("1.0.0")
        self.stub("claude")
        _code, text = self.run_text("update", "--no-project", "--only", "plugins")
        self.assertNotIn("pin the agenthelm", text)


if __name__ == "__main__":
    unittest.main()
