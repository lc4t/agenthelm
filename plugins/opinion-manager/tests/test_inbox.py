from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
import inbox  # noqa: E402

SCRIPT = RUNTIME / "opinion_manager.py"
START = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def cli(*arguments: str) -> tuple[int, dict]:
    completed = subprocess.run([sys.executable, str(SCRIPT), "inbox", *arguments], capture_output=True, text=True, check=False)
    return completed.returncode, json.loads(completed.stdout)


class InboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.moment = START
        patcher = mock.patch.object(inbox, "now", lambda: self.moment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_action(self, action: str, *ids: str, **options):
        defaults = dict(project=self.root, title=None, text=None, scope=[], quote=None, client=None, session=None,
                        status=None, unpresented=False, to=None, confirmed=False, note=None, days=None,
                        max_chars=6000, output="json")
        defaults.update(options)
        import argparse
        return inbox.run(argparse.Namespace(action=action, ids=list(ids), **defaults))

    def add(self, title: str = "先给结论", text: str = "回答先给结论再展开", **options):
        options.setdefault("scope", ["回答"])
        options.setdefault("quote", "以后都先说结论")
        return self.run_action("add", title=title, text=text, **options)

    def test_add_is_unconfirmed_pending_and_never_writes_stable_rules(self) -> None:
        result = self.add(session="s1", client="claude")
        self.assertEqual((result["status"], result["state"], result["id"]), ("added", "pending", "cand-20261001-001"))
        self.assertFalse(result["writes_stable_rules"])
        self.assertEqual([path.name for path in self.root.iterdir()], [".opinion"])
        stored = json.loads((self.root / ".opinion/inbox.json").read_text(encoding="utf-8"))
        candidate = stored["candidates"]["cand-20261001-001"]
        self.assertEqual((candidate["status"], candidate["quotes"], candidate["session"]), ("pending", ["以后都先说结论"], "s1"))

    def test_duplicate_text_is_merged_and_keeps_new_evidence(self) -> None:
        self.add()
        again = self.add(title="先给结论 ", text="回答先给结论  再展开", quote="再说一遍，先说结论")
        self.assertEqual((again["status"], again["id"]), ("duplicate", "cand-20261001-001"))
        (item,) = self.run_action("list")["value"]
        self.assertEqual(item["quotes"], ["以后都先说结论", "再说一遍，先说结论"])

    def test_identifiers_count_up_per_day(self) -> None:
        first, second = self.add(), self.add(title="用中文", text="使用中文回复")
        self.moment += timedelta(days=1)
        third = self.add(title="表格不超过三列", text="表格列数不超过三列")
        self.assertEqual([first["id"], second["id"], third["id"]],
                         ["cand-20261001-001", "cand-20261001-002", "cand-20261002-001"])

    def test_add_requires_complete_content_and_scope(self) -> None:
        for options in ({"title": "", "text": "x"}, {"title": "x", "text": ""}, {"title": "x", "text": "y", "scope": []},
                        {"title": "x" * 500, "text": "y"}, {"title": "x", "text": "y" * 5000}):
            with self.assertRaises(inbox.InboxError):
                self.run_action("add", **{"scope": ["回答"], **options})

    def test_decide_needs_explicit_confirmation_and_records_history(self) -> None:
        identity = self.add()["id"]
        with self.assertRaisesRegex(inbox.InboxError, "明确表态"):
            self.run_action("decide", identity, to="trial")
        self.moment += timedelta(days=2)
        result = self.run_action("decide", identity, to="trial", confirmed=True, note="先试一周")
        self.assertEqual((result["changed"], result["to"]), ([identity], "trial"))
        (item,) = self.run_action("list", status="trial")["value"]
        self.assertEqual(item["trial_since"], "2026-10-03T08:00:00Z")
        stored = json.loads((self.root / ".opinion/inbox.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["to"] for entry in stored["candidates"][identity]["history"]], ["pending", "trial"])

    def test_decide_is_all_or_nothing_for_a_batch(self) -> None:
        identity = self.add()["id"]
        with self.assertRaisesRegex(inbox.InboxError, "没有这个候选"):
            self.run_action("decide", identity, "cand-missing", to="rejected", confirmed=True)
        self.assertEqual(self.run_action("list", status="pending")["value"][0]["id"], identity)

    def test_formal_is_only_bookkeeping(self) -> None:
        identity = self.add()["id"]
        result = self.run_action("decide", identity, to="formal", confirmed=True)
        self.assertIn("完整预览", result["reminder"])
        self.assertFalse(result["writes_stable_rules"])
        self.assertFalse((self.root / "OPINION.md").exists())

    def test_expire_returns_old_trials_to_pending_but_keeps_staleness(self) -> None:
        old, recent = self.add()["id"], self.add(title="用中文", text="使用中文回复")["id"]
        self.run_action("decide", old, to="trial", confirmed=True)
        self.moment += timedelta(days=20)
        self.run_action("decide", recent, to="trial", confirmed=True)
        self.moment += timedelta(days=11)
        result = self.run_action("expire", days=30)
        self.assertEqual(result["reverted"], [old])
        states = {item["id"]: item["status"] for item in self.run_action("list")["value"]}
        self.assertEqual(states, {old: "pending", recent: "trial"})
        summary = self.run_action("pending")
        self.assertEqual(summary["oldest_pending_days"], 31.0)
        self.assertEqual(self.run_action("expire", days=30)["status"], "unchanged")

    def test_pending_follows_the_provider_contract(self) -> None:
        mine = self.add(session="s1")["id"]
        self.add(title="用中文", text="使用中文回复", session="s2")
        rejected = self.add(title="别用表情", text="不要使用表情")["id"]
        self.run_action("decide", rejected, to="rejected", confirmed=True)
        self.moment += timedelta(days=9)
        result = self.run_action("pending", session="s1")
        self.assertEqual(result["source"], "opinion")
        self.assertEqual(result["counts"], {"pending": 2, "trial": 0, "formal": 0, "held": 0, "rejected": 1})
        self.assertEqual(result["session_unpresented"], [mine])
        self.assertEqual(result["oldest_pending_days"], 9.0)
        for item in result["items"]:
            self.assertEqual(set(item), {"id", "source", "kind", "title", "status", "created_at", "last_action_at",
                                          "age_days", "hint_command"})
        self.assertEqual(len(result["items"]), 2)

    def test_presented_candidates_leave_the_unpresented_list(self) -> None:
        identity = self.add(session="s1")["id"]
        self.assertEqual(self.run_action("list", unpresented=True, session="s1")["value"][0]["id"], identity)
        self.assertEqual(self.run_action("present", identity)["marked"], [identity])
        self.assertEqual(self.run_action("list", unpresented=True, session="s1")["value"], [])
        self.assertEqual(self.run_action("pending", session="s1")["session_unpresented"], [])

    def test_brief_injects_protocol_and_only_trial_rules(self) -> None:
        trial = self.add()["id"]
        self.add(title="用中文", text="使用中文回复")
        self.run_action("decide", trial, to="trial", confirmed=True)
        result = self.run_action("brief", session="s9")
        self.assertIn("--session s9", result["text"])
        self.assertIn("回答先给结论再展开", result["text"])
        self.assertNotIn("使用中文回复", result["text"])
        self.assertIn("待确认候选 1 条，尚未生效", result["text"])
        self.assertEqual((result["trial"], result["pending"], result["trial_shown"]), (1, 1, [trial]))

    def test_brief_respects_the_size_budget(self) -> None:
        for number in range(6):
            identity = self.add(title=f"规则{number}", text="详" * 200)["id"]
            self.run_action("decide", identity, to="trial", confirmed=True)
        result = self.run_action("brief", max_chars=len(inbox.PROTOCOL) + 700)
        self.assertLess(len(result["trial_shown"]), 6)
        self.assertIn("未展示", result["text"])

    def test_markdown_listing_is_numbered_for_the_user(self) -> None:
        self.add()
        text = self.run_action("list", output="markdown")
        self.assertTrue(text.startswith("1. **先给结论**"))
        self.assertIn("用户原话：以后都先说结论", text)

    def test_symlinked_store_is_refused(self) -> None:
        target = self.root / "elsewhere"
        target.mkdir()
        (self.root / ".opinion").symlink_to(target)
        with self.assertRaisesRegex(inbox.InboxError, "符号链接"):
            self.add()

    def test_command_line_round_trip(self) -> None:
        code, added = cli("add", "--project", str(self.root), "--title", "先给结论", "--text", "回答先给结论",
                          "--scope", "回答", "--quote", "以后都先说结论")
        self.assertEqual((code, added["status"]), (0, "added"))
        code, refused = cli("decide", added["id"], "--project", str(self.root), "--to", "trial")
        self.assertEqual((code, refused["ok"]), (2, False))
        code, done = cli("decide", added["id"], "--project", str(self.root), "--to", "trial", "--confirmed")
        self.assertEqual((code, done["to"]), (0, "trial"))
        code, brief = cli("brief", "--project", str(self.root))
        self.assertIn("opinion_manager.py inbox add", brief["text"])
        self.assertIn(added["id"], brief["text"])


if __name__ == "__main__":
    unittest.main()
