"""PR3：展示快照（present → adopt 预览的 shown_matches / added_lines）与询问节流 gate。"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_inbox import InboxBase, cli  # noqa: E402
from test_inbox_fields import set_language  # noqa: E402

import inbox  # noqa: E402


class ShownSnapshotTests(InboxBase):
    def stash(self, title: str = "先给结论", text: str = "回答先给结论再展开细节", **options) -> str:
        options.setdefault("trigger", "写报告时")
        options.setdefault("purpose", "省时间")
        return self.add(title=title, text=text, **options)["id"]

    def stored(self, identity: str) -> dict:
        return json.loads((self.root / ".opinion/inbox.json").read_text(encoding="utf-8"))["candidates"][identity]

    def test_present_records_a_snapshot_of_the_current_rendered_line(self) -> None:
        identity = self.stash()
        self.assertNotIn("shown", self.stored(identity))
        self.run_action("present", identity)
        snapshot = self.stored(identity)["shown"]
        self.assertEqual(set(snapshot), {"at", "language", "line_sha256"})
        self.assertEqual(snapshot["language"], "zh")
        self.assertEqual(snapshot["line_sha256"], inbox.versioned.text_digest(inbox.candidate_rule(self.stored(identity))))

    def test_preview_returns_added_lines_and_shown_matches_after_present(self) -> None:
        first, second = self.stash(), self.stash(title="用中文", text="使用中文回复用户", trigger="回复用户时")
        self.run_action("present", first, second)
        preview = self.run_action("adopt", first, second)
        self.assertEqual(preview["added_lines"], ["- **先给结论**（适用：回答；条件：写报告时；目的：省时间）：回答先给结论再展开细节",
                                                 "- **用中文**（适用：回答；条件：回复用户时；目的：省时间）：使用中文回复用户"])
        self.assertTrue(preview["shown_matches"])
        self.assertEqual(preview["shown"], {first: True, second: True})
        for line in preview["added_lines"]:
            self.assertIn(line, preview["content"])

    def test_never_presented_candidates_do_not_match(self) -> None:
        identity = self.stash()
        preview = self.run_action("adopt", identity)
        self.assertEqual((preview["shown_matches"], preview["shown"]), (False, {identity: False}))

    def test_one_unpresented_candidate_makes_the_batch_not_match(self) -> None:
        first, second = self.stash(), self.stash(title="用中文", text="使用中文回复用户")
        self.run_action("present", first)
        preview = self.run_action("adopt", first, second)
        self.assertEqual((preview["shown_matches"], preview["shown"]), (False, {first: True, second: False}))

    def test_revising_after_present_breaks_the_match(self) -> None:
        identity = self.stash()
        self.run_action("present", identity)
        self.run_action("revise", identity, purpose="读者省时间", confirmed=True)
        self.assertFalse(self.run_action("adopt", identity)["shown_matches"])
        self.run_action("present", identity)
        self.assertTrue(self.run_action("adopt", identity)["shown_matches"])

    def test_project_language_change_breaks_the_match(self) -> None:
        identity = self.stash()
        self.run_action("present", identity)
        set_language(self.root, "en")
        self.assertFalse(self.run_action("adopt", identity)["shown_matches"])

    def test_blocked_preview_never_reports_a_match(self) -> None:
        first = self.stash()
        preview = self.run_action("adopt", first)
        self.run_action("adopt", first, apply=True, confirm=preview["confirmation_sha256"])
        (self.root / "OPINION.md").write_text((self.root / "OPINION.md").read_text(encoding="utf-8") + "\n- 人工加的一行\n", encoding="utf-8")
        second = self.stash(title="用中文", text="使用中文回复用户")
        self.run_action("present", second)
        blocked = self.run_action("adopt", second)
        self.assertTrue(blocked["blocked"])
        self.assertEqual((blocked["shown"], blocked["shown_matches"]), ({second: True}, False))

    def test_old_records_without_a_snapshot_read_as_not_shown(self) -> None:
        identity = self.stash()
        path = self.root / ".opinion/inbox.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["candidates"][identity]["presented"] = 3
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(self.run_action("adopt", identity)["shown_matches"])

    def test_snapshot_does_not_change_the_confirmation_fingerprint_or_the_write_path(self) -> None:
        identity = self.stash()
        before = self.run_action("adopt", identity)["confirmation_sha256"]
        self.run_action("present", identity)
        preview = self.run_action("adopt", identity)
        self.assertEqual(preview["confirmation_sha256"], before)
        with self.assertRaises(inbox.InboxError):
            self.run_action("adopt", identity, apply=True)
        self.assertEqual(self.run_action("adopt", identity, apply=True, confirm=before)["status"], "written")

    def test_english_project_added_lines_have_no_chinese(self) -> None:
        set_language(self.root, "en")
        identity = self.stash(title="Reply first", text="Give the conclusion first", scope=["replies"], trigger="writing answers", purpose="save time")
        self.run_action("present", identity)
        preview = self.run_action("adopt", identity)
        self.assertEqual(preview["added_lines"], ["- **Reply first** (Applies to: replies; When: writing answers; Purpose: save time): Give the conclusion first"])
        self.assertTrue(preview["shown_matches"])


class GateTests(InboxBase):
    def gate(self, **options):
        return self.run_action("gate", **{"cooldown_hours": 24, "max_per_session": 1, "peek": False, **options})

    def state(self) -> dict:
        return json.loads((self.root / ".opinion/ask-state.json").read_text(encoding="utf-8"))

    def test_first_ask_is_allowed_and_recorded(self) -> None:
        result = self.gate(session="s1")
        self.assertEqual((result["allowed"], result["reason"], result["recorded"], result["status"]), (True, "ok", True, "written"))
        self.assertEqual(self.state()["sessions"]["s1"]["count"], 1)
        self.assertFalse((self.root / ".opinion/inbox.json").exists())

    def test_same_session_is_limited_even_after_the_cooldown(self) -> None:
        self.gate(session="s1")
        self.moment += timedelta(hours=48)
        result = self.gate(session="s1")
        self.assertEqual((result["allowed"], result["reason"], result["recorded"]), (False, "session_limit", False))
        self.assertEqual(self.gate(session="s1", max_per_session=2)["reason"], "ok")

    def test_other_session_waits_for_the_project_cooldown(self) -> None:
        self.gate(session="s1")
        self.moment += timedelta(hours=23, minutes=59)
        result = self.gate(session="s2")
        self.assertEqual((result["allowed"], result["reason"], result["next_allowed_at"]), (False, "cooldown", "2026-10-02T08:00:00Z"))
        self.moment += timedelta(minutes=1)
        self.assertEqual((self.gate(session="s2")["allowed"], self.state()["sessions"]["s2"]["count"]), (True, 1))

    def test_cooldown_hours_is_configurable(self) -> None:
        self.gate()
        self.moment += timedelta(hours=2)
        self.assertEqual(self.gate(cooldown_hours=1)["allowed"], True)
        self.assertEqual(self.gate(cooldown_hours=1)["reason"], "cooldown")
        self.assertEqual(self.gate(cooldown_hours=0)["allowed"], True)

    def test_without_a_session_only_the_cooldown_applies(self) -> None:
        self.assertTrue(self.gate()["allowed"])
        self.assertEqual(self.gate()["reason"], "cooldown")
        self.assertEqual(self.state()["sessions"], {})

    def test_peek_records_nothing_and_creates_no_files(self) -> None:
        result = self.gate(session="s1", peek=True)
        self.assertEqual((result["allowed"], result["recorded"], result["status"]), (True, False, "read-only"))
        self.assertEqual(list(self.root.iterdir()), [])
        self.gate(session="s1")
        before = self.state()
        self.assertFalse(self.gate(session="s2", peek=True)["allowed"])
        self.assertEqual(self.state(), before)

    def test_invalid_numbers_and_broken_state_are_errors(self) -> None:
        for options in ({"cooldown_hours": -1}, {"max_per_session": 0}):
            with self.subTest(options=options), self.assertRaises(inbox.InboxError):
                self.gate(**options)
        (self.root / ".opinion").mkdir()
        (self.root / ".opinion/ask-state.json").write_text('{"sessions": []}', encoding="utf-8")
        with self.assertRaises(inbox.InboxError):
            self.gate()

    def test_old_sessions_are_pruned(self) -> None:
        for number in range(inbox.MAX_ASK_SESSIONS + 5):
            self.moment += timedelta(hours=25)
            self.gate(session=f"s{number}")
        self.assertEqual(len(self.state()["sessions"]), inbox.MAX_ASK_SESSIONS)
        self.assertNotIn("s0", self.state()["sessions"])

    def test_symlinked_state_is_refused(self) -> None:
        (self.root / ".opinion").mkdir()
        (self.root / "elsewhere.json").write_text("{}", encoding="utf-8")
        (self.root / ".opinion/ask-state.json").symlink_to(self.root / "elsewhere.json")
        with self.assertRaises(inbox.InboxError):
            self.gate(session="s1")

    def test_command_line_defaults(self) -> None:
        code, result = cli("gate", "--project", str(self.root), "--session", "c1", "--lang", "zh")
        self.assertEqual((code, result["allowed"], result["cooldown_hours"], result["max_per_session"]), (0, True, 24.0, 1))
        code, result = cli("gate", "--project", str(self.root), "--session", "c2", "--peek", "--lang", "zh")
        self.assertEqual((result["allowed"], result["reason"]), (False, "cooldown"))
        self.assertIn("本次不要询问", result["message"])


if __name__ == "__main__":
    unittest.main()
