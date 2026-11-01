"""SOP 的项目文件语言：英文项目的 SOP 与执行 Case 用英文，两套词表都能解析，界面语言不改文件。"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["AGENTHELM_LANG"] = "zh"  # the UI language; these tests are about the language of the FILES
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import case  # noqa: E402
import sop  # noqa: E402
import sop_structure as vocab  # noqa: E402

CJK = re.compile(r"[　-〿一-鿿＀-￯]")
TEMPLATES = ("blank", "video-to-transcript", "weekly-review", "knowledge-digest")


class SopLanguageTests(unittest.TestCase):
    language = "en"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.write_profile(self.language)
        patcher = mock.patch.dict(os.environ, {sop.SOP_HOME_ENV: str(Path(self.temporary.name) / "user-sops")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_profile(self, language: str | None) -> None:
        path = self.root / sop.PROFILE
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"schema_version": "1.0", "work": {"mode": "case-workspace", "case": {"root": "cases", "artifact_mode": "case-local"}}}
        if language:
            data["language"] = language
        path.write_text(json.dumps(data), encoding="utf-8")

    def run_sop(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch("sys.stdout", output), mock.patch("sys.stderr", io.StringIO()):
            code = sop.main(["--project", str(self.root), "--output", "json", *args])
        return code, json.loads(output.getvalue())

    def run_case(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = case.main(["--project", str(self.root), "--output", "json", *args])
        return code, json.loads(output.getvalue())

    def file(self, slug: str) -> Path:
        return self.root / "sops" / slug / "SOP.md"

    def create(self, slug: str, template: str, *extra: str) -> None:
        code, result = self.run_sop("new", "--slug", slug, "--template", template, *extra, "--apply")
        self.assertEqual(code, 0, result)

    def activate(self, slug: str, template: str, *extra: str) -> None:
        self.create(slug, template, *extra)
        path = self.file(slug)
        text = path.read_text(encoding="utf-8")
        for marker in vocab.PLACEHOLDERS:
            text = text.replace(marker, "filled in")
        path.write_text(text, encoding="utf-8")
        self.run_sop("defaults", "--set", "on_missing_tool=ask", "--set", "artifact_keep=on-error", "--set", "resume=from-failed-step",
                     "--set", "confirm_costly=ask", "--set", "on_error=ask", "--apply")
        code, result = self.run_sop("decide", slug, "--confirm", "trigger", "--confirm", "requires", "--confirm", "scope", "--apply")
        self.assertEqual(code, 0, result)
        code, result = self.run_sop("activate", slug, "--apply")
        self.assertEqual(code, 0, result)

    def case_file(self, case_id: str) -> Path:
        return next((self.root / "cases").glob(f"{case_id}*/CASE.md"))


class EnglishProjectTests(SopLanguageTests):
    def test_every_sample_creates_an_english_draft(self) -> None:
        for template in TEMPLATES:
            with self.subTest(template=template):
                self.create(f"demo-{template}", template)
                text = self.file(f"demo-{template}").read_text(encoding="utf-8")
                if template in ("blank", "weekly-review", "knowledge-digest", "video-to-transcript"):
                    # only the sample's own content may differ; the structure is English
                    self.assertIn("## Steps", text)
                    self.assertIn("## Revision log", text)
                    self.assertFalse(CJK.search(text), [line for line in text.splitlines() if CJK.search(line)][:3])
                code, checked = self.run_sop("validate", f"demo-{template}")
                self.assertEqual(code, 0, checked)
        review = (self.root / "sops/demo-weekly-review/templates/review.md").read_text(encoding="utf-8")
        self.assertIn("Next week's plan", review)
        self.assertFalse(CJK.search(review))

    def test_a_chinese_project_still_gets_the_chinese_samples(self) -> None:
        self.write_profile(None)
        self.create("zh-demo", "blank")
        text = self.file("zh-demo").read_text(encoding="utf-8")
        self.assertIn("## 步骤", text)
        self.assertIn("【待补充】", text)
        self.assertNotIn("## Steps", text)

    def test_language_option_overrides_the_project_language(self) -> None:
        self.create("override", "blank", "--language", "zh")
        self.assertIn("## 步骤", self.file("override").read_text(encoding="utf-8"))

    def test_the_ui_language_does_not_change_the_files(self) -> None:
        with mock.patch.dict(os.environ, {"AGENTHELM_LANG": "zh"}):
            self.create("ui-zh", "blank")
        with mock.patch.dict(os.environ, {"AGENTHELM_LANG": "en"}):
            self.create("ui-en", "blank")
        self.assertEqual(self.file("ui-zh").read_text(encoding="utf-8").replace("ui-zh", "x"),
                         self.file("ui-en").read_text(encoding="utf-8").replace("ui-en", "x"))

    def test_both_vocabularies_parse_to_the_same_structure(self) -> None:
        self.create("english", "video-to-transcript")
        self.create("chinese", "video-to-transcript", "--language", "zh")
        english = sop.parsed(sop.load_dir(self.root / "sops/english", "project"))
        chinese = sop.parsed(sop.load_dir(self.root / "sops/chinese", "project"))
        self.assertEqual(english["language"], "en")
        self.assertEqual(chinese["language"], "zh")
        self.assertEqual(english["step_problems"], [])
        self.assertEqual(english["require_problems"], [])
        self.assertEqual([step["n"] for step in english["steps"]], [step["n"] for step in chinese["steps"]])
        self.assertEqual([step["tools"] for step in english["steps"]], [step["tools"] for step in chinese["steps"]])
        self.assertEqual([step["confirm"] for step in english["steps"]], [step["confirm"] for step in chinese["steps"]])
        self.assertEqual([(req["kind"], req["name"], req["if_missing"], req["alternative"], bool(req["when"])) for req in english["requires"]],
                         [(req["kind"], req["name"], req["if_missing"], req["alternative"], bool(req["when"])) for req in chinese["requires"]])

    def test_mixed_vocabularies_in_one_file_are_understood(self) -> None:
        self.create("mixed", "blank")
        path = self.file("mixed")
        text = path.read_text(encoding="utf-8").replace("Tools: [TO FILL IN]", "工具：ffmpeg").replace(
            "- Command: [TO FILL IN] | Purpose: [TO FILL IN] | Install: [TO FILL IN] | If missing: Ask the user",
            "- 命令：ffmpeg｜Purpose: convert｜缺失时：Stop")
        path.write_text(text, encoding="utf-8")
        info = sop.parsed(sop.load_dir(path.parent, "project"))
        self.assertEqual(info["steps"][0]["tools"], ["ffmpeg"])
        self.assertEqual((info["requires"][0]["kind"], info["requires"][0]["if_missing"], info["requires"][0]["purpose"]), ("command", "stop", "convert"))
        self.assertEqual(info["require_problems"], [])

    def test_unknown_fields_are_reported_with_the_files_vocabulary(self) -> None:
        self.create("typo", "blank")
        path = self.file("typo")
        path.write_text(path.read_text(encoding="utf-8").replace("   - Tools: [TO FILL IN]", "   - Gadgets: x"), encoding="utf-8")
        info = sop.parsed(sop.load_dir(path.parent, "project"))
        self.assertTrue(any("Tools" in problem and "Gadgets" in problem for problem in info["step_problems"]), info["step_problems"])

    def test_a_full_run_is_written_in_english(self) -> None:
        self.activate("transcribe", "video-to-transcript")
        code, started = self.run_sop("run", "transcribe", "--param", "url=https://example.com/v", "--apply")
        self.assertEqual(code, 0, started)
        case_id = started["case_id"]
        text = self.case_file(case_id).read_text(encoding="utf-8")
        self.assertIn("## SOP checklist", text)
        self.assertIn("- On error: ask", text)
        self.assertIn("## Intermediate artifacts", text)
        self.assertIn("Started running transcribe@1.0.0", text)
        self.assertIn("## Blockers and resume conditions", text)
        self.assertLess(text.index("## SOP checklist"), text.index("## Blockers and resume conditions"))
        code, stepped = self.run_sop("step", case_id, "1", "--done", "--apply")
        self.assertEqual(code, 0, stepped)
        self.assertEqual(stepped["next_action"], "Step 2: Get the video file")
        code, stepped = self.run_sop("step", case_id, "2", "--fail", "--note", "link is dead", "--artifact", "data/video.mp4:half downloaded", "--apply")
        self.assertEqual(code, 0, stepped)
        self.assertIn("Handle the failure of step 2 (convention: ask): link is dead", stepped["next_action"])
        code, stepped = self.run_sop("step", case_id, "2", "--skip", "--note", "use a local file", "--apply")
        self.assertEqual(code, 0, stepped)
        code, status = self.run_sop("status", case_id)
        self.assertEqual(status["artifacts"], ["Step 2: `data/video.mp4` (half downloaded)"])
        self.assertEqual(status["keep_policy"], "kept in " + "this Case's data/ directory" + "; afterwards: on-error")
        self.assertEqual(status["skipped"], [2])
        text = self.case_file(case_id).read_text(encoding="utf-8")
        self.assertIn('SOP step 2 "Get the video file" failed: link is dead', text)
        self.assertIn("SOP progress 2/6", text)
        self.assertIn("## Current trusted state", text)
        self.assertFalse(CJK.search(text), [line for line in text.splitlines() if CJK.search(line)][:3])
        (self.root / "private").mkdir()
        code, confirmed = self.run_sop("confirm-read", case_id, "private/notes.md", "--note", "needed for step 4", "--apply")
        self.assertEqual(code, 0, confirmed)
        text = self.case_file(case_id).read_text(encoding="utf-8")
        self.assertIn("The user confirmed reading private paths: private/notes.md; needed for step 4", text)
        self.assertFalse(CJK.search(text))

    def test_versions_and_status_changes_write_an_english_revision_log(self) -> None:
        self.activate("review", "weekly-review")
        code, result = self.run_sop("revise", "review", "--bump", "patch", "--note", "tightened step 3", "--apply")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.run_sop("retire", "review", "--apply")[0], 0)
        self.assertEqual(self.run_sop("restore", "review", "--apply")[0], 0)
        text = self.file("review").read_text(encoding="utf-8")
        self.assertIn("## Revision log", text)
        self.assertRegex(text, r"v1\.0\.0: Activated \(content and run conventions confirmed with the user\)")
        self.assertRegex(text, r"v1\.0\.1: tightened step 3")
        self.assertRegex(text, r"v1\.0\.1: Retired")
        self.assertRegex(text, r"v1\.0\.1: Restored")
        self.assertNotIn("None yet.", text)
        self.assertFalse(CJK.search(text))

    def test_a_chinese_sop_runs_in_an_english_project_with_an_english_case(self) -> None:
        self.activate("chinese", "weekly-review", "--language", "zh")
        code, started = self.run_sop("run", "chinese", "--apply")
        self.assertEqual(code, 0, started)
        text = self.case_file(started["case_id"]).read_text(encoding="utf-8")
        self.assertIn("## SOP checklist", text)  # the Case follows the project, the SOP keeps its own language
        code, stepped = self.run_sop("step", started["case_id"], "1", "--done", "--apply")
        self.assertEqual(code, 0, stepped)
        self.assertTrue(stepped["next_action"].startswith("Step 2: "))

    def test_a_chinese_project_still_writes_a_chinese_case(self) -> None:
        self.write_profile(None)
        self.activate("weekly", "weekly-review")
        code, started = self.run_sop("run", "weekly", "--apply")
        self.assertEqual(code, 0, started)
        case_id = started["case_id"]
        self.assertEqual(self.run_sop("step", case_id, "1", "--done", "--apply")[0], 0)
        text = self.case_file(case_id).read_text(encoding="utf-8")
        for expected in ("## SOP 执行清单", "- 出错时：ask", "## 中间产物", "SOP 步骤 1", "SOP 进度 1/6"):
            self.assertIn(expected, text)

    def test_a_case_keeps_its_own_language_when_the_project_switches(self) -> None:
        self.write_profile(None)
        self.activate("weekly", "weekly-review")
        code, started = self.run_sop("run", "weekly", "--apply")
        case_id = started["case_id"]
        self.write_profile("en")  # the project switches language after the run started
        self.assertEqual(self.run_sop("step", case_id, "1", "--done", "--apply")[0], 0)
        text = self.case_file(case_id).read_text(encoding="utf-8")
        self.assertIn("SOP 步骤 1", text)
        self.assertNotIn("SOP step 1", text)
        self.assertEqual(self.run_sop("status", case_id)[0], 1)

    def test_from_case_distils_an_english_draft(self) -> None:
        code, made = self.run_case("new", "--title", "Trial run", "--slug", "trial", "--goal", "Try it", "--next-action", "Start")
        self.assertEqual(code, 0, made)
        self.run_case("update", made["id"], "--log", "Downloaded the material")
        code, result = self.run_sop("new", "--slug", "distilled", "--from-case", made["id"], "--apply")
        self.assertEqual(code, 0, result)
        text = self.file("distilled").read_text(encoding="utf-8")
        self.assertIn("1. Downloaded the material", text)
        self.assertNotIn("Case created", text)
        self.assertIn("## Steps", text)
        self.assertFalse(CJK.search(text))

    def test_the_validation_messages_name_the_files_own_sections(self) -> None:
        self.create("empty", "blank")
        path = self.file("empty")
        text = path.read_text(encoding="utf-8")
        start, end = text.index("## Prerequisites"), text.index("## Steps")
        path.write_text(text[:start] + "## Prerequisites\n\n" + text[end:], encoding="utf-8")
        code, checked = self.run_sop("validate", "empty")
        messages = [item["message"] for result in checked["results"] for item in result["issues"]]
        self.assertIn("## Prerequisites", " ".join(messages))
        self.assertTrue(any("[TO FILL IN]" in message for message in messages), messages)
        self.assertFalse(any("## 前置条件" in message for message in messages))


class VocabularyTests(unittest.TestCase):
    def test_every_entry_has_a_chinese_and_an_english_form(self) -> None:
        for table in (vocab.SECTIONS, vocab.STEP_KEYS, vocab.REQ_KINDS, vocab.REQ_KEYS, vocab.MISSING, vocab.CASE_SECTIONS, vocab.TEXTS):
            for key, variants in table.items():
                self.assertEqual(len(variants), 2, key)
                self.assertFalse(CJK.search(variants[1]), (key, variants[1]))

    def test_both_languages_parse_back_to_the_same_keys(self) -> None:
        for key, variants in vocab.STEP_KEYS.items():
            for variant in variants:
                self.assertEqual(vocab.step_key(variant), key)
        for key, variants in vocab.REQ_KINDS.items():
            for variant in variants:
                self.assertEqual(vocab.kind_key(variant), key)
        for key, variants in vocab.REQ_KEYS.items():
            for variant in variants:
                self.assertEqual(vocab.req_key(variant), key)
        for key, variants in vocab.MISSING.items():
            for variant in variants:
                self.assertEqual(vocab.missing_value(variant), key)
        for key, variants in vocab.SECTIONS.items():
            for variant in variants:
                self.assertEqual(vocab.section_key(variant), key)

    def test_the_samples_use_exactly_the_vocabulary(self) -> None:
        base = Path(__file__).resolve().parents[1] / "sop-templates"
        for name in TEMPLATES:
            for file, index in (("SOP.md", 0), ("SOP.en.md", 1)):
                body = (base / name / file).read_text(encoding="utf-8").split("\n---\n", 1)[1]
                headings = re.findall(r"^## (.+)$", body, re.M)
                self.assertEqual(headings, [variants[index] for variants in vocab.SECTIONS.values()], (name, file))
                self.assertEqual(vocab.detect(body), "zh" if index == 0 else "en")

    def test_detect_and_helpers(self) -> None:
        self.assertEqual(vocab.detect("## Steps\n## Purpose\n", "zh"), "en")
        self.assertEqual(vocab.detect("## 步骤\n", "en"), "zh")
        self.assertEqual(vocab.detect("nothing here", "en"), "en")
        self.assertEqual(vocab.strip_tools("Download it (tools: yutto, yt-dlp)"), "Download it")
        self.assertEqual(vocab.strip_tools("下载（工具：yutto）"), "下载")
        self.assertEqual(vocab.count_placeholders("[TO FILL IN] a 【待补充】 b [TO FILL IN]"), 3)


if __name__ == "__main__":
    unittest.main()
