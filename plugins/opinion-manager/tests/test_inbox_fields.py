"""PR1：候选的条件 / 目的 / 不适用 / 关系备注，软必填，以及项目文件语言。"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_inbox import InboxBase, RUNTIME  # noqa: E402

import inbox  # noqa: E402
import inbox_text  # noqa: E402
import versioned  # noqa: E402

GLOBAL_ZH, GLOBAL_EN = inbox.GLOBAL_VALUES["zh"], inbox.GLOBAL_VALUES["en"]
CJK = re.compile(r"[一-鿿　-〿＀-￯]")


def set_language(root: Path, language: str) -> None:
    path = root / ".agents" / "moe.sakanano.agent-pack" / "project.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"language": language}), encoding="utf-8")


class FieldTests(InboxBase):
    def full(self, **options):
        options.setdefault("trigger", "写对外文档时")
        options.setdefault("purpose", "读者不需要先读背景")
        return self.add(**options)

    def test_missing_trigger_and_purpose_only_warn(self) -> None:
        result = self.add()
        self.assertEqual((result["status"], result["complete"]), ("added", False))
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("触发条件", result["warnings"][0])
        (item,) = self.run_action("list")["value"]
        self.assertEqual((item["trigger"], item["purpose"], item["complete"]), ("", "", False))

    def test_complete_candidate_has_no_warnings_and_shows_everywhere(self) -> None:
        result = self.full(exclude="代码注释", relation="收紧 cand-x")
        self.assertEqual((result["complete"], result["warnings"]), (True, []))
        (item,) = self.run_action("list")["value"]
        self.assertEqual((item["trigger"], item["purpose"], item["exclude"], item["relation"]),
                         ("写对外文档时", "读者不需要先读背景", "代码注释", "收紧 cand-x"))
        text = self.run_action("list", output="markdown")
        for fragment in ("条件：写对外文档时", "目的：读者不需要先读背景", "不适用：代码注释", "关系备注：收紧 cand-x"):
            self.assertIn(fragment, text)
        self.assertNotIn("不完整", text)

    def test_incomplete_candidate_is_marked_in_markdown_and_pending(self) -> None:
        self.add()
        self.assertIn("不完整：缺少触发条件、目的", self.run_action("list", output="markdown"))
        (entry,) = self.run_action("pending")["items"]
        self.assertEqual((entry["complete"], entry["trigger"]), (False, ""))

    def test_invalid_values_are_rejected(self) -> None:
        for word in ("  ", "所有", "全部", "相关", "一切", "any", "ALL", "Everything", "relevant", "n/a", "N/A"):
            for name in ("trigger", "purpose", "exclude"):
                with self.subTest(word=word, name=name), self.assertRaises(inbox.InboxError):
                    self.add(**{name: word})
        with self.assertRaises(inbox.InboxError):
            self.add(relation="   ")
        self.assertEqual(self.run_action("list")["value"], [])

    def test_concrete_value_containing_a_placeholder_word_is_fine(self) -> None:
        self.assertTrue(self.add(trigger="所有对外报告", purpose="all readers skim")["complete"])

    def test_strict_switch_rejects_missing_fields(self) -> None:
        from unittest import mock
        with mock.patch.object(inbox, "STRICT_REQUIRED", True):
            with self.assertRaises(inbox.InboxError):
                self.add()
            self.assertTrue(self.full()["complete"])

    def test_global_needs_standard_value_and_a_quote(self) -> None:
        with self.assertRaises(inbox.InboxError):
            self.add(trigger=GLOBAL_ZH, purpose="p", quote=None)
        with self.assertRaises(inbox.InboxError):
            self.add(trigger=GLOBAL_ZH, purpose="p", quote="  ")
        self.add(trigger=GLOBAL_ZH, purpose="p")
        (item,) = self.run_action("list")["value"]
        self.assertTrue(item["global"])
        self.assertIn("全局（请用户确认）", self.run_action("list", output="markdown"))

    def test_old_records_without_new_keys_still_read(self) -> None:
        self.add()
        path = self.root / ".opinion/inbox.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for candidate in data["candidates"].values():
            for name in inbox.FIELDS:
                candidate.pop(name)
        path.write_text(json.dumps(data), encoding="utf-8")
        (item,) = self.run_action("list")["value"]
        self.assertEqual((item["trigger"], item["complete"], item["global"]), ("", False, False))
        self.run_action("list", output="markdown")
        self.run_action("pending")
        self.run_action("present", item["id"])
        self.run_action("brief")
        self.run_action("decide", item["id"], to="trial", confirmed=True)
        self.assertIn("适用", self.run_action("brief")["text"])

    def test_present_decide_and_adopt_preview_carry_the_fields(self) -> None:
        identity = self.full(exclude="注释")["id"]
        self.assertEqual(self.run_action("present", identity)["candidates"][0]["trigger"], "写对外文档时")
        self.assertEqual(self.run_action("decide", identity, to="trial", confirmed=True)["candidates"][0]["purpose"], "读者不需要先读背景")
        self.assertEqual(self.run_action("adopt", identity)["candidates"][0]["exclude"], "注释")

    def test_trial_entry_in_brief_includes_conditions(self) -> None:
        identity = self.full(exclude="注释")["id"]
        self.run_action("decide", identity, to="trial", confirmed=True)
        text = self.run_action("brief")["text"]
        self.assertIn("（适用：回答；条件：写对外文档时；目的：读者不需要先读背景；不适用：注释）", text)

    def test_digest_ignores_new_fields_so_duplicates_still_merge(self) -> None:
        self.add()
        self.assertEqual(self.full()["status"], "duplicate")


class RenderTests(InboxBase):
    def test_rule_line_without_new_fields_is_byte_identical_to_before(self) -> None:
        candidate = {"title": "先给结论", "text": "回答先给结论再展开", "scopes": ["回答", "报告"]}
        self.assertEqual(inbox.candidate_rule(candidate), "- **先给结论**（适用：回答、报告）：回答先给结论再展开")
        self.assertEqual(inbox.candidate_rule(candidate, "zh"), inbox.candidate_rule(candidate))

    def test_rule_line_with_fields_zh_and_en(self) -> None:
        candidate = {"title": "T", "text": "X", "scopes": ["a", "b"], "trigger": "when", "purpose": "why", "exclude": "never"}
        self.assertEqual(inbox.candidate_rule(candidate, "zh"), "- **T**（适用：a、b；条件：when；目的：why；不适用：never）：X")
        self.assertEqual(inbox.candidate_rule(candidate, "en"), "- **T** (Applies to: a, b; When: when; Purpose: why; Not for: never): X")

    def test_global_value_is_localized_into_the_project_language(self) -> None:
        candidate = {"title": "T", "text": "X", "scopes": ["a"], "trigger": GLOBAL_ZH, "purpose": "p"}
        self.assertIn(GLOBAL_EN, inbox.candidate_rule(candidate, "en"))
        self.assertNotRegex(inbox.candidate_rule(candidate, "en"), CJK)
        self.assertIn(GLOBAL_ZH, inbox.candidate_rule({**candidate, "trigger": GLOBAL_EN}, "zh"))

    def test_fingerprint_changes_with_new_fields_but_not_for_old_records(self) -> None:
        base = {"id": "c", "title": "T", "text": "X", "scopes": ["a"]}
        self.assertEqual(inbox.fingerprint_input(base)[1], versioned.digest(["T", "X", ["a"]]))
        self.assertNotEqual(inbox.fingerprint_input(base), inbox.fingerprint_input({**base, "trigger": "when"}))
        self.assertNotEqual(inbox.fingerprint_input({**base, "trigger": "a"}), inbox.fingerprint_input({**base, "trigger": "b"}))

    def adopt(self) -> dict:
        identity = self.add(title="Reply first", text="Give the conclusion first", scope=["replies"], quote="always",
                            trigger="writing answers", purpose="save reading time")["id"]
        preview = self.run_action("adopt", identity)
        self.run_action("adopt", identity, apply=True, confirm=preview["confirmation_sha256"])
        return preview

    def test_zh_project_adopt_keeps_the_chinese_frame_and_lock_without_language(self) -> None:
        preview = self.adopt()
        self.assertIn("# Agent Opinion\n\n本文件包含当前环境中用户确认的 Agent 行为、表达、判断与交付要求。\n\n## 用户自定义规则", preview["content"])
        lock = json.loads((self.root / "opinion.lock.json").read_text(encoding="utf-8"))
        self.assertNotIn("language", lock)
        self.assertEqual(versioned.verify_project(self.root)["status"], "verified")

    def test_en_project_has_no_chinese_and_verifies_with_language_in_lock(self) -> None:
        set_language(self.root, "en")
        preview = self.adopt()
        content = (self.root / "OPINION.md").read_text(encoding="utf-8")
        self.assertEqual(content, preview["content"])
        self.assertNotRegex(content, CJK)
        self.assertIn("## User-defined rules", content)
        self.assertIn("- **Reply first** (Applies to: replies; When: writing answers; Purpose: save reading time): Give the conclusion first", content)
        lock = json.loads((self.root / "opinion.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["language"], "en")
        self.assertEqual(versioned.verify_project(self.root)["status"], "verified")

    def test_en_lock_cannot_be_verified_as_zh_and_explicit_zh_is_invalid(self) -> None:
        set_language(self.root, "en")
        self.adopt()
        path = self.root / "opinion.lock.json"
        lock = json.loads(path.read_text(encoding="utf-8"))
        for language in ("zh", "fr"):
            path.write_text(json.dumps({**lock, "language": language}), encoding="utf-8")
            with self.assertRaises(versioned.OpinionVersionError):
                versioned.verify_project(self.root)
        path.write_text(json.dumps({key: value for key, value in lock.items() if key != "language"}), encoding="utf-8")
        with self.assertRaises(versioned.OpinionVersionError):
            versioned.verify_project(self.root)

    def test_project_language_defaults_to_zh(self) -> None:
        self.assertEqual(versioned.project_language(self.root), "zh")
        for content, expected in (('{"language":"en"}', "en"), ('{"language":"zh"}', "zh"), ('{"language":"fr"}', "zh"), ("[]", "zh"), ("nope", "zh"), ('{}', "zh")):
            path = self.root / ".agents/moe.sakanano.agent-pack/project.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content):
                self.assertEqual(versioned.project_language(self.root), expected)


class ProtocolSizeTests(unittest.TestCase):
    def test_protocol_stays_within_the_injection_budget(self) -> None:
        text = inbox_text.protocol("zh", runtime=RUNTIME / "opinion_manager.py", project="/tmp/project", lang="zh",
                               session="session-0123456789abcdef")
        self.assertLessEqual(len(text.encode("utf-8")), 3000)
        self.assertIn("--trigger", text)
        self.assertIn("--purpose", text)

    def test_skill_md_did_not_grow_beyond_budget(self) -> None:
        skill = RUNTIME.parent / "skills" / "opinion-manager" / "SKILL.md"
        self.assertLessEqual(len(skill.read_bytes()), 12554 + 400)


if __name__ == "__main__":
    unittest.main()
