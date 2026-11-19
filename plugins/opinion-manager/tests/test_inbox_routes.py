"""T1：opinion 路由到 skill（V1）：参数校验、渲染逐字节、解析往返、展示快照与指纹、routes list 的机械检查、旧数据、en 项目无中文。"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_inbox import InboxBase, RUNTIME  # noqa: E402
from test_inbox_fields import set_language  # noqa: E402

import inbox  # noqa: E402
import inbox_routes  # noqa: E402
import inbox_text  # noqa: E402
import rule_lines  # noqa: E402
import versioned  # noqa: E402

CJK = re.compile(r"[一-鿿　-〿＀-￯]")
ROUTE = {"skill": "humanizer-zh", "when": "after", "final": True, "on": "text-deliverable"}
BASE = {"title": "T", "text": "X", "scopes": ["a"], "trigger": "when", "purpose": "why"}
ZH_LINE = ("- **T**（适用：a；条件：when；目的：why；不适用：skill humanizer-zh 自身的执行过程）：X 路由：主体完成后、交付前最后一步使用 skill humanizer-zh 处理成果，"
           "其后不再改动该成果的文字；该 skill 执行期间不再触发本规则；该 skill 不可用时告知用户并按目的自行完成。 [route skill=humanizer-zh when=after final=1 on=text-deliverable]")
EN_LINE = ("- **T** (Applies to: a; When: when; Purpose: why; Not for: the execution of skill humanizer-zh itself): X Routing: as the last step before delivery, use skill humanizer-zh "
           "on the result; do not change its wording afterwards; this rule does not trigger again while that skill runs; if it is unavailable, tell the user and "
           "finish the work yourself per the purpose. [route skill=humanizer-zh when=after final=1 on=text-deliverable]")


class RouteBase(InboxBase):
    def route(self, title: str = "路由", skill: str = "humanizer-zh", **options):
        options.setdefault("trigger", "产出面向人的文字成果")
        options.setdefault("purpose", "减少模板化表达")
        return self.add(title=title, text=f"{title}正文内容", route_skill=skill, **options)

    def candidate(self, identity: str) -> dict:
        return json.loads((self.root / ".opinion/inbox.json").read_text(encoding="utf-8"))["candidates"][identity]


class RouteArgumentTests(RouteBase):
    def test_defaults_and_stored_object(self) -> None:
        identity = self.route()["id"]
        self.assertEqual(self.candidate(identity)["route"], {"skill": "humanizer-zh", "when": "before", "final": False, "on": ""})

    def test_final_implies_after_and_rejects_other_times(self) -> None:
        self.assertEqual(self.candidate(self.route("a", route_final=True)["id"])["route"]["when"], "after")
        self.assertEqual(self.candidate(self.route("b", route_final=True, route_when="after")["id"])["route"]["final"], True)
        for when in ("before", "instead"):
            with self.subTest(when=when), self.assertRaises(inbox.InboxError):
                self.route("c", route_final=True, route_when=when)

    def test_skill_name_boundaries(self) -> None:
        for name in ("a", "a-b-1", "x" * 64):
            with self.subTest(name=name):
                self.route(f"ok-{len(name)}-{name[:3]}", skill=name)
        for name in ("x" * 65, "Upper", "under_score", "-lead", "trail-", "dou--ble", "plug:skill", "中文", " "):
            with self.subTest(name=name), self.assertRaises(inbox.InboxError):
                self.route("bad", skill=name)

    def test_tag_boundaries(self) -> None:
        self.route("t1", route_on="a" * 32)
        for tag in ("a" * 33, "Up", "a b", "a_b", "中"):
            with self.subTest(tag=tag), self.assertRaises(inbox.InboxError):
                self.route("t2", route_on=tag)

    def test_other_route_options_need_a_skill(self) -> None:
        for options in ({"route_when": "after"}, {"route_final": True}, {"route_on": "tag"}):
            with self.subTest(options=options), self.assertRaises(inbox.InboxError):
                self.add(**options)

    def test_revise_merges_into_the_existing_route(self) -> None:
        identity = self.route(route_on="tag-a")["id"]
        self.run_action("revise", identity, route_when="instead")
        self.assertEqual(self.candidate(identity)["route"], {"skill": "humanizer-zh", "when": "instead", "final": False, "on": "tag-a"})
        self.run_action("revise", identity, route_skill="other", route_final=True)
        self.assertEqual(self.candidate(identity)["route"], {"skill": "other", "when": "after", "final": True, "on": "tag-a"})
        self.run_action("revise", identity, route_when="before")  # 明确改时机时收尾标记随之取消
        self.assertEqual(self.candidate(identity)["route"], {"skill": "other", "when": "before", "final": False, "on": "tag-a"})
        self.assertEqual(self.candidate(identity)["history"][-1]["previous"]["route"]["when"], "after")

    def test_revise_can_add_a_route_to_a_plain_candidate_and_needs_a_skill_then(self) -> None:
        identity = self.add()["id"]
        with self.assertRaises(inbox.InboxError):
            self.run_action("revise", identity, route_on="tag")
        self.run_action("revise", identity, route_skill="s1")
        self.assertEqual(self.candidate(identity)["route"]["skill"], "s1")

    def test_route_clear_removes_the_route_and_keeps_history(self) -> None:
        identity = self.route(route_on="tag-a")["id"]
        old = self.candidate(identity)["route"]
        result = self.run_action("revise", identity, route_clear=True)
        self.assertEqual(result["changed"], ["route"])
        self.assertNotIn("route", result["candidate"])
        self.assertEqual(self.candidate(identity)["history"][-1]["previous"], {"route": old})
        self.assertNotIn("[route", inbox.candidate_rule(self.candidate(identity)))
        self.assertEqual(self.run_action("routes")["routes"], [])
        self.assertEqual(self.run_action("revise", identity, route_clear=True)["status"], "unchanged")

    def test_route_clear_is_exclusive_and_needs_confirmation_after_presenting(self) -> None:
        identity = self.route()["id"]
        for options in ({"route_skill": "x"}, {"route_when": "after"}, {"route_final": True}, {"route_on": "t"}):
            with self.subTest(options=options), self.assertRaises(inbox.InboxError):
                self.run_action("revise", identity, route_clear=True, **options)
        self.run_action("present", identity)
        with self.assertRaises(inbox.InboxError):
            self.run_action("revise", identity, route_clear=True)
        self.run_action("revise", identity, route_clear=True, confirmed=True)
        self.assertNotIn("route", self.run_action("list")["value"][0])

    def test_identical_revision_is_unchanged(self) -> None:
        identity = self.route()["id"]
        self.assertEqual(self.run_action("revise", identity, route_skill="humanizer-zh")["status"], "unchanged")


class RouteRenderTests(RouteBase):
    def test_rule_line_bytes_zh_and_en(self) -> None:
        candidate = {**BASE, "exclude": "", "route": ROUTE}
        self.assertEqual(rule_lines.candidate_rule(candidate, "zh"), ZH_LINE)
        self.assertEqual(rule_lines.candidate_rule(candidate, "en"), EN_LINE)
        self.assertNotRegex(rule_lines.candidate_rule(candidate, "en"), CJK)

    def test_each_timing_sentence_and_marker(self) -> None:
        for when, final, fragment, marker in (("before", False, "开始主体工作前先使用 skill s", "[route skill=s when=before]"),
                                              ("after", False, "主体完成后、交付前使用 skill s 处理成果", "[route skill=s when=after]"),
                                              ("instead", False, "用 skill s 取代默认做法完成本项工作", "[route skill=s when=instead]"),
                                              ("after", True, "最后一步使用 skill s", "[route skill=s when=after final=1]")):
            with self.subTest(when=when, final=final):
                line = rule_lines.candidate_rule({**BASE, "route": {"skill": "s", "when": when, "final": final, "on": ""}}, "zh")
                self.assertIn(fragment, line)
                self.assertTrue(line.endswith(" " + marker))

    def test_user_exclude_comes_first_and_automatic_constraints_always_present(self) -> None:
        line = rule_lines.candidate_rule({**BASE, "exclude": "代码", "route": ROUTE}, "zh")
        self.assertIn("不适用：代码、skill humanizer-zh 自身的执行过程）", line)
        self.assertIn("该 skill 执行期间不再触发本规则", line)
        self.assertIn("该 skill 不可用时告知用户并按目的自行完成", line)
        english = rule_lines.candidate_rule({**BASE, "exclude": "code", "route": ROUTE}, "en")
        self.assertIn("Not for: code, the execution of skill humanizer-zh itself)", english)
        self.assertIn("this rule does not trigger again while that skill runs", english)
        self.assertIn("if it is unavailable, tell the user", english)

    def test_rules_without_route_are_byte_identical(self) -> None:
        candidate = {"title": "先给结论", "text": "回答先给结论再展开", "scopes": ["回答"]}
        self.assertEqual(rule_lines.candidate_rule(candidate), "- **先给结论**（适用：回答）：回答先给结论再展开")
        self.assertEqual(rule_lines.candidate_rule({**candidate, "route": None}), rule_lines.candidate_rule(candidate))
        self.assertEqual(rule_lines.candidate_rule({**candidate, "route": {}}), rule_lines.candidate_rule(candidate))


class RouteParseTests(unittest.TestCase):
    def test_round_trip_both_languages_and_all_shapes(self) -> None:
        for language in ("zh", "en"):
            for route in (ROUTE, {"skill": "s", "when": "before", "final": False, "on": ""}, {"skill": "s", "when": "instead", "final": False, "on": "t"}):
                for exclude in ("", "代码；配置"):
                    with self.subTest(language=language, route=route, exclude=exclude):
                        candidate = {**BASE, "text": "正文；含分号", "exclude": exclude, "route": route}
                        parsed = rule_lines.parse_line(rule_lines.candidate_rule(candidate, language))
                        self.assertEqual((parsed["format"], parsed["text"], parsed["exclude"], parsed["route"]), (language, "正文；含分号", exclude, route))
                        self.assertEqual((parsed["trigger"], parsed["purpose"]), ("when", "why"))

    def test_plain_lines_have_no_route_keys(self) -> None:
        parsed = rule_lines.parse_line("- **T**（适用：a）：X")
        self.assertNotIn("route", parsed)
        self.assertNotIn("route_error", parsed)

    def test_damaged_markers_are_reported_not_guessed(self) -> None:
        good = rule_lines.candidate_rule({**BASE, "route": ROUTE}, "zh")
        for bad in (good.replace("skill=humanizer-zh", "skill=Bad_Name"), good.replace("when=after", "when=never"), good.replace("final=1", "final=2"),
                    good.replace("on=text-deliverable", "on=Up"), good.replace(" when=after", ""), good + " [route skill=x]",
                    good.replace("[route skill=humanizer-zh", "[route skill=humanizer-zh skill=dup"), good.replace("[route", "[route bogus=1")):
            with self.subTest(bad=bad[-70:]):
                parsed = rule_lines.parse_line(bad)
                self.assertNotIn("route", parsed)
                self.assertEqual(parsed.get("route_error"), "marker_damaged")
        self.assertEqual(rule_lines.parse_marker("hand-written line about a skill"), (None, None))

    def test_final_requires_after_in_the_marker(self) -> None:
        self.assertEqual(rule_lines.parse_marker("x [route skill=s when=before final=1]"), (None, "marker_damaged"))


class RouteViewsTests(RouteBase):
    def test_list_present_pending_brief_decide_adopt_carry_the_route(self) -> None:
        identity = self.route(route_on="tag-a")["id"]
        route = {"skill": "humanizer-zh", "when": "before", "final": False, "on": "tag-a"}
        (item,) = self.run_action("list")["value"]
        self.assertEqual(item["route"], route)
        self.assertIn("   - 路由：skill humanizer-zh（开始前，标签 tag-a）", self.run_action("list", output="markdown"))
        self.assertEqual(self.run_action("present", identity)["candidates"][0]["route"], route)
        self.assertEqual(self.run_action("pending")["items"][0]["route"], route)
        self.assertEqual(self.run_action("decide", identity, to="trial", confirmed=True)["candidates"][0]["route"], route)
        self.assertIn("路由：skill humanizer-zh（开始前，标签 tag-a）", self.run_action("brief")["text"])
        preview = self.run_action("adopt", identity)
        self.assertEqual(preview["candidates"][0]["route"], route)
        self.assertTrue(preview["added_lines"][0].endswith("[route skill=humanizer-zh when=before on=tag-a]"))
        self.assertIn(preview["added_lines"][0], preview["content"])

    def test_plain_candidates_show_no_route_keys_or_lines(self) -> None:
        identity = self.add()["id"]
        (item,) = self.run_action("list")["value"]
        self.assertNotIn("route", item)
        self.assertNotIn("路由", self.run_action("list", output="markdown"))
        self.assertNotIn("route", self.run_action("pending")["items"][0])

    def test_shown_snapshot_follows_the_rendered_line(self) -> None:
        identity = self.route()["id"]
        self.run_action("present", identity)
        self.assertTrue(self.run_action("adopt", identity)["shown_matches"])
        self.run_action("revise", identity, route_when="instead", confirmed=True)
        self.assertFalse(self.run_action("adopt", identity)["shown_matches"])

    def test_fingerprint_includes_the_route_and_old_records_are_unchanged(self) -> None:
        base = {"id": "c", "title": "T", "text": "X", "scopes": ["a"], "trigger": "when"}
        old = inbox.fingerprint_input(base)
        self.assertEqual(old[1], versioned.digest(["T", "X", ["a"], "when", "", ""]))
        self.assertNotEqual(old, inbox.fingerprint_input({**base, "route": ROUTE}))
        self.assertNotEqual(inbox.fingerprint_input({**base, "route": ROUTE}), inbox.fingerprint_input({**base, "route": {**ROUTE, "on": "other"}}))
        self.assertEqual(inbox.fingerprint_input({**base, "route": None}), old)

    def test_adopt_preview_fingerprint_changes_with_the_route(self) -> None:
        identity = self.route()["id"]
        first = self.run_action("adopt", identity)["confirmation_sha256"]
        self.run_action("revise", identity, route_on="tag-a")
        self.assertNotEqual(first, self.run_action("adopt", identity)["confirmation_sha256"])

    def test_old_records_without_a_route_key_still_read(self) -> None:
        self.add()
        path = self.root / ".opinion/inbox.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for candidate in data["candidates"].values():
            candidate.pop("route", None)
        path.write_text(json.dumps(data), encoding="utf-8")
        for action in ("list", "pending", "brief", "check", "routes"):
            self.run_action(action)
        self.run_action("list", output="markdown")
        self.assertEqual(self.run_action("routes")["routes"], [])
        self.assertEqual(self.run_action("routes", "list", output="markdown"), "没有路由规则。\n")


class AdoptRoundTripTests(RouteBase):
    def adopt_route(self, language: str) -> str:
        if language == "en":
            set_language(self.root, "en")
        identity = self.add(title="Final polish", text="Polish text", scope=["text"], quote="always", trigger="producing text", purpose="less boilerplate",
                            route_skill="humanizer-en", route_final=True, route_on="text-deliverable")["id"]
        preview = self.run_action("adopt", identity)
        self.run_action("adopt", identity, apply=True, confirm=preview["confirmation_sha256"])
        self.assertEqual(versioned.verify_project(self.root)["status"], "verified")
        return (self.root / "OPINION.md").read_text(encoding="utf-8")

    def test_en_project_has_no_chinese_and_routes_list_reads_the_written_line(self) -> None:
        content = self.adopt_route("en")
        self.assertNotRegex(content, CJK)
        self.assertIn("[route skill=humanizer-en when=after final=1 on=text-deliverable]", content)
        result = self.run_action("routes", lang="en")
        (entry,) = result["routes"]
        self.assertEqual((entry["source"], entry["skill"], entry["when"], entry["final"], entry["on"]), ("custom_line", "humanizer-en", "after", True, "text-deliverable"))
        self.assertEqual(result["checks"], [])

    def test_zh_project_writes_chinese_constraints_and_the_formal_candidate_is_not_counted_twice(self) -> None:
        content = self.adopt_route("zh")
        self.assertIn("该 skill 不可用时告知用户并按目的自行完成", content)
        result = self.run_action("routes")
        self.assertEqual([item["source"] for item in result["routes"]], ["custom_line"])
        self.assertEqual(result["summary"]["routes"], 1)

    def test_hand_written_and_damaged_lines_are_unstructured_or_damaged(self) -> None:
        self.adopt_route("zh")
        path = self.root / "OPINION.md"
        text = path.read_text(encoding="utf-8").rstrip("\n")
        path.write_text(text + "\n- 写文字时请使用 humanizer skill\n- 与 skill 无关的手写行不应出现\n- 普通手写行\n"
                        "- **损坏**（适用：a）：x [route skill=Bad_Name when=after]\n", encoding="utf-8")
        entries = self.run_action("routes")["routes"]
        self.assertEqual([item["structure"] for item in entries], ["route", "unstructured", "unstructured", "marker_damaged"])
        self.assertEqual(self.run_action("routes")["summary"], {"routes": 1, "unstructured": 3, "warnings": 0})
        self.assertIn("未结构化", self.run_action("routes", output="markdown"))


class MechanicalCheckTests(RouteBase):
    def codes(self) -> list[str]:
        return [item["code"] for item in self.run_action("routes")["checks"]]

    def test_same_tag_and_time_different_skill(self) -> None:
        self.route("a", "skill-a", route_on="t", route_when="after")
        self.route("b", "skill-b", route_on="t", route_when="after")
        self.assertEqual(self.codes(), ["same_tag_different_skill"])
        (check,) = self.run_action("routes")["checks"]
        self.assertEqual((check["severity"], check["note"]), ("warn", "机械检查，仅供参考"))

    def test_near_misses_do_not_trigger_the_same_tag_check(self) -> None:
        self.route("a", "skill-a", route_on="t", route_when="after")
        self.route("b", "skill-b", route_on="t", route_when="before")   # 时机不同
        self.route("c", "skill-c", route_on="u", route_when="after")    # 标签不同
        self.route("d", "skill-d", route_when="after")                  # 无标签
        self.route("e", "skill-e", route_when="after")
        self.assertEqual(self.codes(), [])

    def test_duplicate_route(self) -> None:
        self.route("a", "s", route_on="t")
        self.route("b", "s", route_on="t")
        self.assertEqual(self.codes(), ["duplicate_route"])
        self.route("c", "s", route_on="u")      # 标签不同
        self.route("d", "s", route_on="t", route_when="after")  # 时机不同
        self.assertEqual(self.codes(), ["duplicate_route"])

    def test_duplicate_without_tags_counts(self) -> None:
        self.route("a", "s")
        self.route("b", "s")
        self.assertEqual(self.codes(), ["duplicate_route"])

    def test_multiple_final_is_project_wide(self) -> None:
        self.route("a", "s1", route_final=True, route_on="x")
        self.assertEqual(self.codes(), [])
        self.route("b", "s2", route_final=True, route_on="y")
        self.assertEqual(self.codes(), ["multiple_final"])

    def test_route_count_budget_boundary(self) -> None:
        for number in range(inbox_routes.MAX_ROUTES):
            self.route(f"r{number}", f"s{number}")
        self.assertEqual(self.codes(), [])
        self.route("r-last", "s-last")
        self.assertEqual(self.codes(), ["too_many_routes"])

    def test_held_and_rejected_candidates_are_not_counted(self) -> None:
        a, b = self.route("a", "s", route_on="t")["id"], self.route("b", "s", route_on="t")["id"]
        self.run_action("decide", b, to="held", confirmed=True)
        self.assertEqual(self.codes(), [])
        self.run_action("decide", b, to="pending", confirmed=True)
        self.assertEqual(self.codes(), ["duplicate_route"])
        self.run_action("decide", a, to="rejected", confirmed=True)
        self.assertEqual(self.codes(), [])

    def test_line_size_budget_boundary_is_500_bytes(self) -> None:
        def sized(target: int) -> str:
            filler = ""
            probe = {"id": "c", "title": "T", "text": "x", "scopes": ["a"], "route": {"skill": "s", "when": "before", "final": False, "on": ""}}
            while len(rule_lines.candidate_rule({**probe, "text": "x" + filler}, "zh").encode()) < target:
                filler += "a"
            return "x" + filler
        exact = sized(500)
        probe = {"id": "c", "title": "T", "text": exact, "scopes": ["a"], "route": {"skill": "s", "when": "before", "final": False, "on": ""}}
        self.assertEqual(len(rule_lines.candidate_rule(probe, "zh").encode()), 500)
        identity = self.add(title="T", text=exact, scope=["a"], route_skill="s")
        self.assertFalse([w for w in identity["warnings"] if "预算" in w])
        self.assertEqual(self.codes(), [])
        over = self.add(title="T2", text="y" + exact[1:] + "z", scope=["a"], route_skill="s2")
        self.assertTrue([w for w in over["warnings"] if "预算" in w])   # 只警告，仍然暂存成功
        self.assertEqual(over["status"], "added")
        self.assertEqual(self.codes(), ["line_too_long"])

    def test_the_checks_never_claim_skill_existence_and_list_what_is_unchecked(self) -> None:
        self.route()
        result = self.run_action("routes")
        self.assertIn("skill_exists", result["unchecked"])
        self.assertNotIn("target", result["routes"][0])


class EnglishInterfaceTests(RouteBase):
    def test_english_messages_have_no_chinese(self) -> None:
        self.route("a", "s", route_on="t", route_when="after")
        self.route("b", "s2", route_on="t", route_when="after")
        self.route("c", "s2", route_on="t", route_when="after")
        output = self.run_action("routes", lang="en", output="markdown")
        self.assertIn("Mechanical check, for reference only", output)
        self.assertIn("same timing (after) routes to different skills", output)
        self.assertIn("Duplicate route: skill s2", output)
        self.assertIn("   - Route: skill s2 (after, tag t)", self.run_action("list", output="markdown", lang="en"))
        for text in (self.run_action("routes", lang="en", output="markdown"), json.dumps(self.run_action("routes", lang="en"), ensure_ascii=False)):
            self.assertNotRegex(text, CJK)

    def test_every_new_message_has_both_languages(self) -> None:
        for key, value in inbox_text.MESSAGES.items():
            if key.startswith(("route", "routes_", "err_route", "warn_route", "list_route", "brief_route")) or key == "brief_join_short":
                self.assertEqual(set(value), {"zh", "en"}, key)
                self.assertTrue(value["zh"] and value["en"], key)
                self.assertEqual(set(inbox_text.PLACEHOLDER.findall(value["zh"])), set(inbox_text.PLACEHOLDER.findall(value["en"])), key)

    def test_protocol_and_skill_budgets_still_hold(self) -> None:
        text = inbox_text.text("protocol", "zh", runtime=RUNTIME / "opinion_manager.py", project="/tmp/project", lang="zh",
                               session=inbox_text.session_flag("session-0123456789abcdef"))
        self.assertLessEqual(len(text.encode("utf-8")), 3000)
        self.assertLessEqual((RUNTIME.parent / "skills" / "opinion-manager" / "SKILL.md").stat().st_size, 12554 + 400)

    def test_routes_only_supports_list(self) -> None:
        with self.assertRaises(inbox.InboxError):
            self.run_action("routes", "check")


if __name__ == "__main__":
    unittest.main()
