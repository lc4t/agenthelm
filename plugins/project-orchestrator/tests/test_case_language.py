"""Case 正文的结构按项目文件语言（Profile 的 language）生成；读取与更新两种语言的小节名都认。"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # the UI language is deliberately different from the project language in some tests

import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import case  # noqa: E402
import case_structure as structure  # noqa: E402
import project_real  # noqa: E402

CJK = re.compile(r"[　-〿一-鿿＀-￯]")


class CaseLanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def project(self, language: str | None) -> None:
        path = self.root / case.PROFILE
        path.parent.mkdir(parents=True, exist_ok=True)
        profile = {"schema_version": "1.0", "work": {"mode": "case-workspace", "case": {"root": "cases", "artifact_mode": "case-local"}}}
        if language:
            profile["language"] = language
        path.write_text(json.dumps(profile), encoding="utf-8")

    def run_case(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = case.main(["--project", str(self.root), "--output", "json", *args])
        return code, json.loads(output.getvalue())

    def new(self, slug: str = "first", title: str = "Quarterly report", *extra: str) -> dict:
        code, result = self.run_case("new", "--title", title, "--slug", slug, "--goal", "Ship it", "--next-action", "Collect data", *extra)
        self.assertEqual(code, 0, result)
        return result

    def text(self, result: dict) -> str:
        return (self.root / result["path"]).read_text(encoding="utf-8")

    def test_an_english_project_gets_an_english_case(self) -> None:
        self.project("en")
        text = self.text(self.new())
        body = text.split("---\n", 2)[2]
        for heading in ("# Summary", "## Current trusted state", "## Next action", "## Decision log", "## Work log", "## Result"):
            self.assertIn(heading, body)
        self.assertIn("Case created.", body)
        self.assertFalse(CJK.search(body), body)
        self.assertNotIn("{{", text)
        code, checked = self.run_case("validate")
        self.assertEqual(code, 0, checked)

    def test_the_default_and_a_chinese_project_keep_the_chinese_case(self) -> None:
        for language in (None, "zh", "fr"):
            with self.subTest(language=language):
                self.setUp()
                self.project(language)
                text = self.text(self.new())
                self.assertIn("## 工作记录", text)
                self.assertIn("## 结果", text)
                self.assertNotIn("## Work log", text)

    def test_the_ui_language_does_not_change_the_files(self) -> None:
        self.project("en")
        with mock.patch.dict(os.environ, {"AGENTHELM_LANG": "zh"}):
            self.assertIn("## Work log", self.text(self.new("one")))
        self.project("zh")
        with mock.patch.dict(os.environ, {"AGENTHELM_LANG": "en"}):
            self.assertIn("## 工作记录", self.text(self.new("two")))

    def test_updates_write_under_the_english_sections(self) -> None:
        self.project("en")
        created = self.new()
        code, updated = self.run_case("update", "first", "--log", "Drafted the outline", "--decision", "Use the Q3 template",
                                      "--result", "Delivered", "--current-state", "In review", "--next-action", "Send it")
        self.assertEqual(code, 0, updated)
        self.assertEqual(sorted(updated["changed"]), sorted(["Work log", "Decision log", "Result", "current_state", "next_action"]))
        body = self.text(created).split("---\n", 2)[2]
        self.assertRegex(body, r"## Work log\n\n- [^\n]*Case created\.\n- [^\n]*Drafted the outline")
        self.assertRegex(body, r"## Decision log\n\n- [^\n]*Use the Q3 template")
        self.assertNotIn("None yet.\n\n- ", body.split("## Decision log")[1].split("## Work log")[0])
        self.assertIn("## Result\n\nDelivered", body)
        self.assertIn("## Current trusted state\n\nIn review", body)
        self.assertFalse(CJK.search(body), body)

    def test_the_files_own_section_names_win_in_either_direction(self) -> None:
        self.project("zh")
        chinese = self.new("zh-case", "报告")
        english_project_text = self.text(chinese)
        self.project("en")  # the project changed language later; the old Case keeps its spelling
        code, updated = self.run_case("update", "zh-case", "--log", "继续", "--decision", "采用模板")
        self.assertEqual(code, 0, updated)
        self.assertEqual(sorted(updated["changed"]), ["决策记录", "工作记录"])
        after = self.text(chinese)
        self.assertNotIn("## Work log", after)
        self.assertNotIn("## Decision log", after)
        self.assertEqual(after.count("## 工作记录"), 1)
        self.assertGreater(len(after), len(english_project_text))
        self.new("en-case", "Report")
        self.project("zh")
        code, updated = self.run_case("update", "en-case", "--log", "continued")
        self.assertEqual(code, 0, updated)
        self.assertEqual(updated["changed"], ["Work log"])
        latest = (self.root / updated["path"]).read_text(encoding="utf-8")
        self.assertNotIn("## 工作记录", latest)
        self.assertIn("continued", latest)

    def test_a_missing_section_is_created_in_the_project_language(self) -> None:
        self.project("en")
        body = "# Notes\n\nSome text.\n"
        self.assertIn("## Work log\n\n- first", structure_append(body, "work_log", "first", "en"))
        self.assertIn("## 工作记录\n\n- first", structure_append(body, "work_log", "first", "zh"))
        self.assertEqual(case.append_section(body, "工作记录", "legacy call", "zh").count("## 工作记录"), 1)

    def test_the_last_record_is_read_from_either_language(self) -> None:
        english = "## Work log\n\n- 2026-10-29T09:00 First\n- 2026-10-29T10:00 Second\n"
        chinese = "## 工作记录\n\n- 2026-10-29T09:00 第一\n- 2026-10-29T10:00 第二\n"
        self.assertEqual(project_real.last_record(english), {"at": "2026-10-29T10:00", "text": "Second"})
        self.assertEqual(project_real.last_record(chinese)["text"], "第二")
        self.assertIsNone(project_real.last_record("## Result\n\nx\n"))

    def test_the_vocabulary_is_consistent(self) -> None:
        for key, (chinese, english) in structure.SECTIONS.items():
            self.assertEqual(structure.key_of(chinese), key)
            self.assertEqual(structure.key_of(english), key)
            self.assertEqual(structure.heading(key, "en"), english)
        self.assertEqual(structure.language_of({}), "zh")
        self.assertEqual(structure.language_of({"language": "en"}), "en")
        self.assertEqual(structure.language_of({"language": "fr"}), "zh")
        template = (Path(case.__file__).resolve().parents[1] / "templates" / "CASE.en.md").read_text(encoding="utf-8")
        chinese_template = (Path(case.__file__).resolve().parents[1] / "templates" / "CASE.md").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"\{\{(\w+)\}\}", template), re.findall(r"\{\{(\w+)\}\}", chinese_template))
        self.assertEqual(len(re.findall(r"^## ", template, re.M)), len(re.findall(r"^## ", chinese_template, re.M)))
        for placeholder in re.findall(r"^(None yet\.|None\.|Pending\.)$", template, re.M):
            self.assertIn(placeholder, structure.PLACEHOLDERS)


def structure_append(body: str, key: str, line: str, language: str) -> str:
    return case.append_section(body, key, line, language)


if __name__ == "__main__":
    unittest.main()
