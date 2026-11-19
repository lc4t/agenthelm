"""PR2：inbox check（只读）、inbox revise、机械关系提示与自定义行解析。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_inbox import InboxBase  # noqa: E402
from test_inbox_fields import GLOBAL_ZH, set_language  # noqa: E402

import inbox  # noqa: E402
import inbox_relations as relations  # noqa: E402
import rule_lines  # noqa: E402


def tree(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): path.read_text(encoding="utf-8", errors="replace") for path in sorted(root.rglob("*")) if path.is_file()}


class ParseTests(unittest.TestCase):
    def test_both_render_formats_round_trip(self) -> None:
        candidate = {"title": "T", "text": "X: y", "scopes": ["a", "b"], "trigger": "when; now", "purpose": "why", "exclude": "never"}
        for language in ("zh", "en"):
            with self.subTest(language=language):
                parsed = rule_lines.parse_line(rule_lines.candidate_rule(candidate, language))
                self.assertEqual({key: parsed[key] for key in ("format", "title", "scopes", "trigger", "purpose", "exclude", "text")},
                                 {"format": language, **candidate})

    def test_line_without_new_fields_parses_with_empty_values(self) -> None:
        parsed = rule_lines.parse_line("- **先给结论**（适用：回答、报告）：先说结论")
        self.assertEqual((parsed["scopes"], parsed["trigger"], parsed["purpose"]), (["回答", "报告"], "", ""))

    def test_foreign_lines_are_unstructured(self) -> None:
        for line in ("- 回答先说结论", "- **T**（目的：x）：y", "普通段落", "- **T** (When: x): y"):
            with self.subTest(line=line):
                self.assertIsNone(rule_lines.parse_line(line))

    def test_custom_section_is_found_in_either_language_and_stops_at_next_heading(self) -> None:
        for heading in rule_lines.CUSTOM_HEADINGS:
            content = f"# Agent Opinion\n\n## Other\n\n- not mine\n\n{heading}\n\n- a\n\nplain\n\n## Later\n- b\n"
            self.assertEqual(rule_lines.custom_section_lines(content), [(9, "- a"), (11, "plain")])
        self.assertEqual(rule_lines.custom_section_lines("# nothing\n"), [])


class MechanicalRelationTests(unittest.TestCase):
    """固定样例守住阈值：保守，宁可漏报。"""

    def pool(self, *texts: str, trigger: str = "") -> list[dict[str, str]]:
        return [{"type": "candidate", "ref": f"c{index}", "title": "", "text": text, "trigger": trigger} for index, text in enumerate(texts, 1)]

    def kinds(self, text: str, *others: str, **kwargs) -> list[str]:
        candidate = {"id": "me", "title": "标题", "text": text, "trigger": kwargs.get("trigger", "")}
        return [item["kind"] for item in relations.relations_for(candidate, self.pool(*others, trigger=kwargs.get("other_trigger", "")))]

    def test_same_text_ignores_case_space_and_punctuation(self) -> None:
        self.assertEqual(self.kinds("回答先给结论，再展开细节", "回答先给结论 再展开细节。"), ["same_text"])
        self.assertEqual(self.kinds("Give the conclusion first", "give the  conclusion first."), ["same_text"])

    def test_paraphrases_above_threshold_are_reported(self) -> None:
        self.assertEqual(self.kinds("回答先给结论再展开细节", "回答要先给出结论，再展开细节"), ["text_overlap"])
        self.assertEqual(self.kinds("Always give the conclusion first", "Give the conclusion first, then details"), ["text_overlap"])

    def test_unrelated_and_borderline_texts_are_not_reported(self) -> None:
        for left, right in (("回答先给结论再展开细节", "代码注释只写为什么，不写做了什么"),
                            ("表格列数不超过三列", "表格最多三列"),
                            ("回答先给结论再展开", "先给结论，然后再展开说明"),
                            ("Reply in Chinese", "Reply in English to English users")):
            with self.subTest(left=left):
                self.assertEqual(self.kinds(left, right), [])

    def test_very_short_texts_are_not_compared_by_overlap(self) -> None:
        self.assertEqual(self.kinds("用中文", "用中文回复"), [])

    def test_condition_overlap_needs_both_triggers_and_ignores_global(self) -> None:
        self.assertEqual(self.kinds("正文甲乙丙", "完全不同的另一句话", trigger="写对外文档时", other_trigger="写对外文档或报告时"), ["condition_overlap"])
        self.assertEqual(self.kinds("正文甲乙丙", "完全不同的另一句话", trigger="写对外文档时", other_trigger="写代码注释时"), [])
        self.assertEqual(self.kinds("正文甲乙丙", "完全不同的另一句话", trigger="", other_trigger="写对外文档时"), [])
        self.assertEqual(self.kinds("正文甲乙丙", "完全不同的另一句话", trigger=GLOBAL_ZH, other_trigger=GLOBAL_ZH), [])

    def test_self_is_skipped_and_results_are_capped_and_ordered(self) -> None:
        candidate = {"id": "c1", "title": "标题", "text": "回答先给结论再展开细节", "trigger": ""}
        pool = self.pool(*["回答先给结论再展开细节"] * 8, "回答要先给出结论，再展开细节")
        found = relations.relations_for(candidate, pool)
        self.assertEqual(len(found), relations.MAX_RELATIONS)
        self.assertNotIn("c1", [item["with"]["ref"] for item in found])
        self.assertEqual({item["kind"] for item in found}, {"same_text"})


class RelationsInOutputTests(InboxBase):
    def test_list_and_present_carry_relations_with_evidence_and_note(self) -> None:
        first = self.add(title="先给结论", text="回答先给结论再展开细节")["id"]
        second = self.add(title="结论优先", text="回答要先给出结论，再展开细节")["id"]
        values = {item["id"]: item for item in self.run_action("list")["value"]}
        (entry,) = values[first]["relations"]
        self.assertEqual((entry["kind"], entry["with"], entry["note"]), ("text_overlap", {"type": "candidate", "ref": second}, "机械比对，仅供参考"))
        self.assertIn("回答要先给出结论", entry["evidence"])
        (marked,) = self.run_action("present", first)["candidates"]
        self.assertEqual(marked["relations"][0]["with"]["ref"], second)
        self.assertIn("机械比对，仅供参考", self.run_action("list", output="markdown"))

    def test_no_relations_is_an_empty_list_and_rejected_candidates_are_ignored(self) -> None:
        first = self.add()["id"]
        other = self.add(title="用中文", text="回答先给结论再展开细节，请用中文")["id"]
        self.run_action("decide", other, to="rejected", confirmed=True)
        (item,) = self.run_action("list", status="pending")["value"]
        self.assertEqual((item["id"], item["relations"]), (first, []))

    def test_custom_lines_and_public_rules_are_compared(self) -> None:
        (self.root / "OPINION.md").write_text("# Agent Opinion\n\n## 用户自定义规则\n\n- **旧规则**（适用：回答；条件：写报告时）：回答先给结论再展开细节\n- 随手一行\n", encoding="utf-8")
        self.add(text="回答先给结论再展开细节")
        (item,) = self.run_action("list")["value"]
        self.assertEqual([(entry["kind"], entry["with"]) for entry in item["relations"]], [("same_text", {"type": "custom_line", "ref": "OPINION.md:5"})])

    def test_selected_public_rule_text_is_compared(self) -> None:
        from unittest import mock
        rule = {"id": "demo.reply", "version": "1.0.0", "title": "先结论", "text": "回答先给结论再展开细节", "level": "required", "scopes": ["回答"]}
        with mock.patch.object(relations.versioned, "read_object", return_value={"profile": {}}), \
                mock.patch.object(relations.versioned, "effective_rules", return_value={"demo.reply": rule}):
            self.add(text="回答先给结论再展开细节")
            (item,) = self.run_action("list")["value"]
        self.assertEqual([(entry["kind"], entry["with"]["type"], entry["with"]["ref"]) for entry in item["relations"]],
                         [("same_text", "public_rule", "demo.reply@1.0.0")])

    def test_relation_note_is_stored_and_shown_verbatim(self) -> None:
        self.add(relation="放大 cand-x 的范围")
        (item,) = self.run_action("list")["value"]
        self.assertEqual(item["relation"], "放大 cand-x 的范围")
        self.assertIn("关系备注：放大 cand-x 的范围", self.run_action("list", output="markdown"))


class CheckTests(InboxBase):
    def test_check_reports_incomplete_candidates_and_custom_lines_and_writes_nothing(self) -> None:
        self.add(title="缺项", text="甲乙丙丁戊己庚")
        self.add(title="完整", text="另一条完全不同的规则", trigger="写对外文档时", purpose="读者省时间")
        held = self.add(title="暂缓的", text="暂缓项的正文内容")["id"]
        self.run_action("decide", held, to="held", confirmed=True)
        (self.root / "OPINION.md").write_text("# Agent Opinion\n\n## 用户自定义规则\n\n"
                                              "- **缺条件**（适用：回答）：正文一\n- **齐全**（适用：回答；条件：写报告时；目的：省时间）：正文二\n随手写的一行\n", encoding="utf-8")
        before = tree(self.root)
        result = self.run_action("check")
        self.assertEqual(tree(self.root), before)
        self.assertEqual([(item["title"], item["missing"]) for item in result["candidates"]], [("缺项", ["trigger", "purpose"])])
        self.assertIn("inbox revise", result["candidates"][0]["revise_command"])
        self.assertEqual([(item["line"], item["format"], item["missing"]) for item in result["custom_lines"]],
                         [(5, "zh", ["trigger", "purpose"]), (7, "unstructured", [])])
        self.assertEqual(result["summary"], {"candidates_incomplete": 1, "custom_lines_incomplete": 1, "custom_lines_unstructured": 1})
        self.assertFalse(result["writes_stable_rules"])

    def test_check_reads_english_format_and_reports_clean_state(self) -> None:
        (self.root / "OPINION.md").write_text("# Agent Opinion\n\n## User-defined rules\n\n- **T** (Applies to: a; When: x): body\n", encoding="utf-8")
        result = self.run_action("check")
        self.assertEqual([(item["format"], item["missing"]) for item in result["custom_lines"]], [("en", ["purpose"])])
        (self.root / "OPINION.md").unlink()
        self.assertEqual(self.run_action("check")["custom_lines"], [])
        self.assertIn("没有缺少", self.run_action("check")["next_step"])

    def test_check_without_inbox_creates_no_file(self) -> None:
        self.run_action("check")
        self.assertEqual(list(self.root.iterdir()), [])


class ReviseTests(InboxBase):
    def stash(self, **options):
        return self.add(title="先给结论", text="回答先给结论再展开细节", **options)["id"]

    def history(self, identity: str) -> list[dict]:
        return json.loads((self.root / ".opinion/inbox.json").read_text(encoding="utf-8"))["candidates"][identity]["history"]

    def test_unpresented_pending_revises_without_confirmation_and_records_previous(self) -> None:
        identity = self.stash()
        result = self.run_action("revise", identity, trigger="写报告时", purpose="省时间", title="结论优先")
        self.assertEqual((result["status"], result["changed"]), ("written", ["purpose", "title", "trigger"]))
        self.assertEqual(result["warnings"], [])
        (item,) = self.run_action("list")["value"]
        self.assertEqual((item["title"], item["trigger"], item["status"], item["complete"]), ("结论优先", "写报告时", "pending", True))
        last = self.history(identity)[-1]
        self.assertEqual((last["to"], last["note"], last["previous"]), ("pending", "revised", {"title": "先给结论", "trigger": "", "purpose": ""}))

    def test_presented_or_non_pending_needs_confirmation(self) -> None:
        identity = self.stash()
        self.run_action("present", identity)
        with self.assertRaises(inbox.InboxError):
            self.run_action("revise", identity, trigger="写报告时")
        self.assertEqual(self.run_action("revise", identity, trigger="写报告时", confirmed=True)["status"], "written")
        self.run_action("decide", identity, to="trial", confirmed=True)
        with self.assertRaises(inbox.InboxError):
            self.run_action("revise", identity, purpose="省时间")
        self.run_action("revise", identity, purpose="省时间", confirmed=True)
        self.assertEqual(self.run_action("list")["value"][0]["status"], "trial")
        self.assertEqual(self.history(identity)[-1]["to"], "trial")

    def test_digest_is_recomputed_and_collisions_are_rejected(self) -> None:
        first = self.stash()
        second = self.add(title="用中文", text="使用中文回复")["id"]
        with self.assertRaises(inbox.InboxError):
            self.run_action("revise", second, title="先给结论", text="回答先给结论再展开细节")
        self.run_action("revise", second, text="使用中文回复用户")
        self.assertEqual(self.add(title="用中文", text="使用中文回复用户")["status"], "duplicate")
        self.assertNotEqual(first, second)

    def test_invalid_values_and_empty_revisions_are_rejected(self) -> None:
        identity = self.stash()
        for options in ({}, {"trigger": "所有"}, {"purpose": "  "}, {"title": ""}, {"relation": " "}):
            with self.subTest(options=options), self.assertRaises(inbox.InboxError):
                self.run_action("revise", identity, **options)
        with self.assertRaises(inbox.InboxError):
            self.run_action("revise", self.add(title="无原话", text="没有原话的规则正文", quote="")["id"], trigger=GLOBAL_ZH)
        with self.assertRaises(inbox.InboxError):
            self.run_action("revise", "cand-none", trigger="写报告时")
        self.assertEqual(len(self.history(identity)), 1)

    def test_global_with_a_quote_is_accepted_and_quote_is_appended(self) -> None:
        identity = self.stash(quote="先说结论")
        result = self.run_action("revise", identity, trigger=GLOBAL_ZH, quote="所有场景都这样")
        self.assertEqual(result["changed"], ["quotes", "trigger"])
        (item,) = self.run_action("list")["value"]
        self.assertEqual((item["quotes"], item["global"]), (["先说结论", "所有场景都这样"], True))
        self.assertEqual(self.history(identity)[-1]["previous"]["quotes"], ["先说结论"])

    def test_same_values_are_unchanged_and_scope_is_replaced(self) -> None:
        identity = self.stash(trigger="写报告时")
        self.assertEqual(self.run_action("revise", identity, trigger="写报告时")["status"], "unchanged")
        self.run_action("revise", identity, scope=["报告", "回答"])
        self.assertEqual(self.run_action("list")["value"][0]["scopes"], ["报告", "回答"])

    def test_revised_fields_reach_the_adoption_fingerprint(self) -> None:
        identity = self.stash(trigger="写报告时", purpose="省时间")
        before = self.run_action("adopt", identity)["confirmation_sha256"]
        self.run_action("revise", identity, trigger="写周报时")
        self.assertNotEqual(self.run_action("adopt", identity)["confirmation_sha256"], before)

    def test_formal_revision_warns_that_opinion_md_is_unchanged(self) -> None:
        identity = self.stash(trigger="写报告时", purpose="省时间")
        self.run_action("decide", identity, to="formal", confirmed=True)
        result = self.run_action("revise", identity, purpose="读者省时间", confirmed=True)
        self.assertTrue(any("OPINION.md" in item for item in result["warnings"]))

    def test_english_project_check_after_adopt_sees_complete_lines(self) -> None:
        set_language(self.root, "en")
        identity = self.stash(trigger="writing answers", purpose="save time")
        preview = self.run_action("adopt", identity)
        self.run_action("adopt", identity, apply=True, confirm=preview["confirmation_sha256"])
        self.assertEqual(self.run_action("check")["custom_lines"], [])


if __name__ == "__main__":
    unittest.main()
