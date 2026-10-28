from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import case  # noqa: E402
import project_cli  # noqa: E402
import sop  # noqa: E402


class SopTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.user_home = Path(self.temporary.name) / "user-sops"
        self.root.mkdir()
        self.write_profile({"mode": "case-workspace", "case": {"root": "cases", "artifact_mode": "case-local"}})
        patcher = mock.patch.dict(os.environ, {sop.SOP_HOME_ENV: str(self.user_home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temporary.cleanup)

    def write_profile(self, work: dict, extra: dict | None = None) -> None:
        path = self.root / sop.PROFILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": "1.0", "work": work, **(extra or {})}), encoding="utf-8")

    def run_sop(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch("sys.stdout", output), mock.patch("sys.stderr", io.StringIO()):
            code = sop.main(["--project", str(self.root), "--output", "json", *args])
        return code, json.loads(output.getvalue())

    def file(self, slug: str) -> Path:
        return self.root / "sops" / slug / "SOP.md"

    def create(self, slug: str = "video-to-transcript", template: str = "video-to-transcript") -> None:
        code, result = self.run_sop("new", "--slug", slug, "--template", template, "--apply")
        self.assertEqual(code, 0, result)

    def fill_placeholders(self, slug: str) -> None:
        path = self.file(slug)
        path.write_text(path.read_text(encoding="utf-8").replace(sop.PLACEHOLDER, "已填写"), encoding="utf-8")

    def make_active(self, slug: str = "video-to-transcript", template: str = "video-to-transcript") -> None:
        self.create(slug, template)
        self.fill_placeholders(slug)
        self.run_sop("defaults", "--set", "on_missing_tool=ask", "--set", "artifact_keep=on-error",
                     "--set", "resume=from-failed-step", "--set", "confirm_costly=ask", "--set", "on_error=ask", "--apply")
        code, result = self.run_sop("decide", slug, "--confirm", "trigger", "--confirm", "requires", "--apply")
        self.assertEqual(code, 0, result)
        code, result = self.run_sop("activate", slug, "--apply")
        self.assertEqual(code, 0, result)


class CreationTests(SopTestCase):
    def test_every_bundled_template_creates_a_valid_draft(self) -> None:
        for template in ("blank", "video-to-transcript", "weekly-review"):
            with self.subTest(template=template):
                self.create(f"demo-{template}", template)
                code, checked = self.run_sop("validate", f"demo-{template}")
                self.assertEqual(code, 0, checked)
                text = self.file(f"demo-{template}").read_text(encoding="utf-8")
                self.assertIn('status: "draft"', text)
                self.assertNotIn("{{", text)
        self.assertTrue((self.root / "sops" / "demo-weekly-review" / "templates" / "review.md").is_file())

    def test_new_previews_without_writing_and_refuses_to_overwrite(self) -> None:
        code, preview = self.run_sop("new", "--slug", "alpha", "--template", "blank")
        self.assertEqual(code, 0)
        self.assertFalse(preview["applied"])
        self.assertFalse(self.file("alpha").exists())
        self.create("alpha", "blank")
        code, again = self.run_sop("new", "--slug", "alpha", "--template", "blank", "--apply")
        self.assertEqual(code, 5, again)

    def test_bad_slug_and_unknown_template_are_rejected(self) -> None:
        self.assertEqual(self.run_sop("new", "--slug", "Bad Slug")[0], 2)
        code, result = self.run_sop("new", "--slug", "ok", "--template", "nope")
        self.assertEqual(code, 1)
        self.assertIn("没有这个样例", result["error"])

    def test_from_case_makes_a_draft_with_steps_from_the_work_log(self) -> None:
        code, made = self.run_case("new", "--title", "一次尝试", "--slug", "attempt", "--goal", "试试", "--next-action", "开始")
        self.assertEqual(code, 0, made)
        self.run_case("update", made["id"], "--log", "下载了素材")
        code, result = self.run_sop("new", "--slug", "distilled", "--from-case", made["id"], "--apply")
        self.assertEqual(code, 0, result)
        text = self.file("distilled").read_text(encoding="utf-8")
        self.assertIn("下载了素材", text)
        self.assertIn(f"case:{made['id']}", text)

    def run_case(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = case.main(["--project", str(self.root), "--output", "json", *args])
        return code, json.loads(output.getvalue())

    def test_profile_root_must_stay_inside_the_project(self) -> None:
        self.write_profile({"mode": "case-workspace"}, {"sop": {"root": "../outside"}})
        code, result = self.run_sop("list")
        self.assertEqual(code, 2, result)


class NegotiationTests(SopTestCase):
    def test_interview_lists_every_question_with_numbers(self) -> None:
        code, result = self.run_sop("interview")
        self.assertEqual(code, 0)
        self.assertEqual([item["no"] for item in result["questions"]], list(range(1, 8)))
        self.assertEqual({item["key"] for item in result["questions"]},
                         {"trigger", "requires", "on_error", "on_missing_tool", "artifacts", "resume", "confirm_costly"})

    def test_activation_is_blocked_until_the_user_has_decided(self) -> None:
        self.create()
        self.fill_placeholders("video-to-transcript")
        code, result = self.run_sop("activate", "video-to-transcript", "--apply")
        self.assertEqual(code, 1)
        keys = {item["message"] for item in result["blockers"]}
        self.assertTrue(any("on_error" in message for message in keys))
        self.assertIn('status: "draft"', self.file("video-to-transcript").read_text(encoding="utf-8"))

    def test_project_defaults_answer_habit_questions_once(self) -> None:
        self.create()
        code, before = self.run_sop("interview", "video-to-transcript")
        self.assertEqual(code, 1)
        self.assertEqual(len(before["questions"]), 7)
        self.run_sop("defaults", "--set", "on_error=retry", "--set", "max_retries=3", "--set", "on_missing_tool=ask",
                     "--set", "artifact_keep=final-only", "--set", "resume=restart", "--set", "confirm_costly=ask", "--apply")
        _, after = self.run_sop("interview", "video-to-transcript")
        self.assertEqual([item["key"] for item in after["questions"]], ["trigger", "requires"])
        _, shown = self.run_sop("show", "video-to-transcript")
        self.assertEqual(shown["conventions"]["on_error"], {"value": "retry", "source": "project-default"})

    def test_defaults_reject_unknown_keys_and_values(self) -> None:
        self.assertEqual(self.run_sop("defaults", "--set", "on_error=explode")[0], 2)
        self.assertEqual(self.run_sop("defaults", "--set", "nonsense=1")[0], 2)
        self.assertEqual(self.run_sop("defaults", "--set", "max_retries=0")[0], 2)

    def test_decide_records_confirmed_items_and_sop_values_win_over_defaults(self) -> None:
        self.create()
        self.run_sop("defaults", "--set", "on_error=ask", "--apply")
        code, result = self.run_sop("decide", "video-to-transcript", "--set", "on_error=stop", "--confirm", "trigger", "--apply")
        self.assertEqual(code, 0, result)
        self.assertIn("on_error", result["decided"])
        _, shown = self.run_sop("show", "video-to-transcript")
        self.assertEqual(shown["conventions"]["on_error"], {"value": "stop", "source": "sop"})

    def test_decide_rejects_unknown_confirm_key(self) -> None:
        self.create()
        self.assertEqual(self.run_sop("decide", "video-to-transcript", "--confirm", "everything")[0], 2)

    def test_activation_snapshots_the_version_and_lists_it_in_the_index(self) -> None:
        self.make_active()
        snapshot = self.root / "sops" / "video-to-transcript" / ".versions" / "1.0.0.md"
        self.assertEqual(snapshot.read_text(encoding="utf-8"), self.file("video-to-transcript").read_text(encoding="utf-8"))
        _, index = self.run_sop("index")
        self.assertEqual(len(index["entries"]), 1)
        self.assertIn("触发：", index["entries"][0]["line"])

    def test_drafts_stay_out_of_the_index_and_matching(self) -> None:
        self.create()
        _, index = self.run_sop("index")
        self.assertEqual(index["entries"], [])
        self.assertEqual(index["drafts"], 1)
        code, found = self.run_sop("match", "视频转录")
        self.assertEqual(code, 1)
        self.assertEqual(found["matches"], [])


class ValidationTests(SopTestCase):
    def test_active_sop_with_undeclared_tool_is_an_error(self) -> None:
        self.make_active()
        path = self.file("video-to-transcript")
        path.write_text(path.read_text(encoding="utf-8").replace("   - 工具：ffmpeg", "   - 工具：ffmpeg、handbrake"), encoding="utf-8")
        _, checked = self.run_sop("validate", "video-to-transcript")
        codes = {item["code"] for item in checked["results"][0]["issues"]}
        self.assertIn("undeclared-tool", codes)
        self.assertFalse(checked["ok"])

    def test_embedded_secrets_are_rejected_without_echoing_them(self) -> None:
        self.create()
        path = self.file("video-to-transcript")
        secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
        path.write_text(path.read_text(encoding="utf-8") + f"\n密钥 {secret}\n", encoding="utf-8")
        code, checked = self.run_sop("validate", "video-to-transcript")
        self.assertEqual(code, 1)
        self.assertNotIn(secret, json.dumps(checked, ensure_ascii=False))

    def test_editing_without_bumping_the_version_warns_and_revise_fixes_it(self) -> None:
        self.make_active()
        path = self.file("video-to-transcript")
        path.write_text(path.read_text(encoding="utf-8") + "\n- 补充一条例外\n", encoding="utf-8")
        _, checked = self.run_sop("validate", "video-to-transcript")
        self.assertIn("version-not-bumped", {item["code"] for item in checked["results"][0]["issues"]})
        code, result = self.run_sop("revise", "video-to-transcript", "--bump", "minor", "--note", "补充例外", "--apply")
        self.assertEqual(code, 0, result)
        self.assertEqual((result["from"], result["to"]), ("1.0.0", "1.1.0"))
        _, checked = self.run_sop("validate", "video-to-transcript")
        self.assertEqual(checked["results"][0]["issues"], [])
        self.assertTrue((self.root / "sops" / "video-to-transcript" / ".versions" / "1.1.0.md").is_file())

    def test_revise_requires_a_note(self) -> None:
        self.make_active()
        self.assertEqual(self.run_sop("revise", "video-to-transcript", "--bump", "patch")[0], 2)

    def test_unknown_frontmatter_field_and_bad_schedule_are_errors(self) -> None:
        self.create()
        path = self.file("video-to-transcript")
        text = path.read_text(encoding="utf-8").replace('confirm: "ask"', 'confirm: "ask"\nmystery: "x"\nschedule: "sometimes"')
        path.write_text(text, encoding="utf-8")
        _, checked = self.run_sop("validate", "video-to-transcript")
        codes = {item["code"] for item in checked["results"][0]["issues"]}
        self.assertTrue({"unknown-field", "invalid-schedule"} <= codes)

    def test_alias_collision_between_active_sops_warns(self) -> None:
        self.make_active("first-one", "weekly-review")
        self.make_active("second-one", "weekly-review")
        _, checked = self.run_sop("validate")
        self.assertIn("trigger-overlap", {item["code"] for result in checked["results"] for item in result["issues"]})

    def test_conflict_copy_in_the_sop_directory_is_reported(self) -> None:
        self.make_active()
        (self.root / "sops" / "video-to-transcript" / "SOP 2.md").write_text("x", encoding="utf-8")
        _, checked = self.run_sop("validate", "video-to-transcript")
        self.assertIn("conflict-copy", {item["code"] for item in checked["results"][0]["issues"]})

    def test_stale_preview_cannot_overwrite_a_newer_edit(self) -> None:
        self.create()
        path = self.file("video-to-transcript")
        preview = sop.Context(sop.parser().parse_args(["--project", str(self.root), "decide", "video-to-transcript", "--confirm", "trigger"]))
        item = sop.find_sop(preview.sops(), "video-to-transcript")
        writes = [{"path": path, "content": "x", "expect": sop.sha(item["text"])}]
        path.write_text(item["text"] + "\n人工改动\n", encoding="utf-8")
        with self.assertRaises(sop.SopError) as raised:
            sop.apply_writes(writes)
        self.assertEqual(raised.exception.code, "conflict")


class PreflightTests(SopTestCase):
    def setup_requirements(self, lines: str) -> None:
        self.create("tooling", "blank")
        path = self.file("tooling")
        text = path.read_text(encoding="utf-8")
        start = text.index("- 命令：【待补充】")
        end = text.index("\n", start)
        path.write_text(text[:start] + lines + text[end:], encoding="utf-8")

    def preflight(self) -> tuple[int, dict]:
        return self.run_sop("preflight", "tooling")

    def test_present_and_missing_commands_are_distinguished(self) -> None:
        self.setup_requirements("- 命令：python3｜用途：运行脚本\n- 命令：definitely-not-installed-xyz｜用途：演示｜安装：不要安装｜缺失时：停止")
        code, report = self.preflight()
        self.assertEqual(code, 1)
        states = {row["name"]: row["status"] for row in report["requires"]}
        self.assertEqual(states, {"python3": "ok", "definitely-not-installed-xyz": "missing"})
        missing = next(row for row in report["requires"] if row["status"] == "missing")
        self.assertEqual((missing["if_missing"], missing["install"]), ("stop", "不要安装"))

    def test_environment_variables_report_presence_but_never_the_value(self) -> None:
        self.setup_requirements("- 环境变量：DEMO_TRANSCRIBE_KEY｜用途：云端转录")
        with mock.patch.dict(os.environ, {"DEMO_TRANSCRIBE_KEY": "super-secret-value-123"}):
            code, report = self.preflight()
        self.assertEqual(code, 0, report)
        self.assertNotIn("super-secret-value-123", json.dumps(report, ensure_ascii=False))
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("DEMO_TRANSCRIBE_KEY", None)
            code, report = self.preflight()
        self.assertEqual(code, 1)

    def test_alternative_resolves_only_when_the_user_agreed_to_use_it(self) -> None:
        lines = ("- 命令：missing-local-tool-xyz｜用途：本地转录｜替代：CLOUD_KEY_XYZ｜缺失时：使用替代\n"
                 "- 环境变量：CLOUD_KEY_XYZ｜用途：云端转录")
        self.setup_requirements(lines)
        with mock.patch.dict(os.environ, {"CLOUD_KEY_XYZ": "x"}):
            code, report = self.preflight()
        self.assertEqual(code, 0, report)
        self.assertEqual(next(row for row in report["requires"] if row["kind"] == "command")["resolved_by"], "CLOUD_KEY_XYZ")
        path = self.file("tooling")
        path.write_text(path.read_text(encoding="utf-8").replace("缺失时：使用替代", "缺失时：询问用户"), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CLOUD_KEY_XYZ": "x"}):
            code, report = self.preflight()
        self.assertEqual(code, 1, report)

    def test_unused_alternative_is_optional_when_the_primary_is_available(self) -> None:
        self.setup_requirements("- 命令：python3｜替代：NO_SUCH_KEY_XYZ｜缺失时：使用替代\n- 环境变量：NO_SUCH_KEY_XYZ｜用途：备用")
        os.environ.pop("NO_SUCH_KEY_XYZ", None)
        code, report = self.preflight()
        self.assertEqual(code, 0, report)

    def test_modules_skills_mcp_and_manual_items_need_confirmation_when_not_found(self) -> None:
        self.setup_requirements("- 模块：json\n- 模块：no_such_module_xyz\n- 技能：no-such-skill\n- MCP：no-such-server\n- 手动确认：本地模型已下载")
        code, report = self.preflight()
        states = {row["name"]: row["status"] for row in report["requires"]}
        self.assertEqual(states["json"], "ok")
        self.assertEqual({states[name] for name in ("no_such_module_xyz", "no-such-skill", "no-such-server", "本地模型已下载")}, {"unknown"})
        self.assertEqual(code, 1)

    def test_project_skill_and_mcp_are_found(self) -> None:
        skill = self.root / ".agents" / "skills" / "my-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: my-skill\n---\n", encoding="utf-8")
        (self.root / ".agents" / "mcp.json").write_text(json.dumps({"mcpServers": {"my-server": {}}}), encoding="utf-8")
        self.setup_requirements("- 技能：my-skill\n- MCP：my-server")
        code, report = self.preflight()
        self.assertEqual(code, 0, report)

    def test_path_requirement_resolves_relative_to_the_project(self) -> None:
        (self.root / "models").mkdir()
        (self.root / "models" / "m.bin").write_text("x", encoding="utf-8")
        self.setup_requirements("- 路径：models/m.bin\n- 路径：models/absent.bin")
        _, report = self.preflight()
        self.assertEqual([row["status"] for row in report["requires"]], ["ok", "missing"])

    def test_malformed_requirement_lines_are_reported_by_validation(self) -> None:
        self.setup_requirements("- 随便写点什么")
        _, checked = self.run_sop("validate", "tooling")
        self.assertIn("requirement-format", {item["code"] for item in checked["results"][0]["issues"]})


class ScheduleTests(SopTestCase):
    def test_next_occurrence_rules(self) -> None:
        monday = date(2026, 10, 26)
        self.assertEqual(sop.next_occurrence("daily", monday), date(2026, 10, 27))
        self.assertEqual(sop.next_occurrence("weekly:sun", monday), date(2026, 11, 1))
        self.assertEqual(sop.next_occurrence("weekly:mon", monday), date(2026, 11, 2))
        self.assertEqual(sop.next_occurrence("monthly:1", monday), date(2026, 11, 1))
        self.assertEqual(sop.next_occurrence("monthly:28", date(2026, 2, 28)), date(2026, 3, 28))
        self.assertEqual(sop.next_occurrence("every:14d", monday), date(2026, 11, 9))

    def test_due_uses_the_latest_execution_case(self) -> None:
        self.make_active("weekly-review", "weekly-review")
        later = date.today() + timedelta(days=8)
        with mock.patch.object(sop, "today", return_value=later):
            _, due = self.run_sop("due")
        self.assertEqual([row["id"] for row in due["due"]], ["weekly-review"])
        code, run = self.run_sop("run", "weekly-review", "--apply")
        self.assertEqual(code, 0, run)
        with mock.patch.object(sop, "today", return_value=date.today() + timedelta(days=1)):
            _, due = self.run_sop("due")
        self.assertEqual(due["due"], [])
        self.assertEqual(due["schedules"][0]["last_run"], date.today().isoformat())

    def test_events_lists_only_active_listeners(self) -> None:
        self.make_active("closer", "weekly-review")
        path = self.file("closer")
        path.write_text(path.read_text(encoding="utf-8").replace("on_events: []", 'on_events:\n  - "case.closed"'), encoding="utf-8")
        _, listening = self.run_sop("events", "case.closed")
        self.assertEqual([item["id"] for item in listening["sops"]], ["closer"])
        self.assertEqual(self.run_sop("events", "case.exploded")[0], 2)


class MatchTests(SopTestCase):
    def test_alias_and_example_hits_come_with_the_exclusion_hint(self) -> None:
        self.make_active()
        code, found = self.run_sop("match", "帮我做个视频转录")
        self.assertEqual(code, 0)
        self.assertEqual(found["matches"][0]["id"], "video-to-transcript")
        self.assertTrue(found["matches"][0]["maybe_excluded"])
        self.assertEqual(self.run_sop("match", "今天天气怎么样")[0], 1)


class ExecutionTests(SopTestCase):
    def test_run_creates_a_case_with_checklist_conventions_and_snapshot(self) -> None:
        self.make_active()
        code, preview = self.run_sop("run", "video-to-transcript", "--param", "视频链接=https://example.test/v")
        self.assertEqual(code, 0, preview)
        self.assertFalse(preview["applied"])
        self.assertFalse((self.root / "cases").exists())
        code, ran = self.run_sop("run", "video-to-transcript", "--param", "视频链接=https://example.test/v", "--apply")
        self.assertEqual(code, 0, ran)
        text = (self.root / ran["path"]).read_text(encoding="utf-8")
        self.assertIn('sop: "video-to-transcript@1.0.0"', text)
        self.assertIn("- [ ] 2. 取得视频文件（工具：yt-dlp）", text)
        self.assertIn("出错时：ask", text)
        self.assertIn("## 中间产物", text)
        self.assertEqual(self.run_case("validate")[0], 0)

    def run_case(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = case.main(["--project", str(self.root), "--output", "json", *args])
        return code, json.loads(output.getvalue())

    def start(self) -> str:
        self.make_active()
        _, ran = self.run_sop("run", "video-to-transcript", "--apply")
        return ran["case_id"]

    def test_step_updates_checklist_log_artifacts_and_next_action(self) -> None:
        case_id = self.start()
        code, result = self.run_sop("step", case_id, "1", "--done", "--note", "输入确认", "--apply")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["progress"], "1/6")
        self.run_sop("step", case_id, "2", "--done", "--artifact", "data/video.mp4:原始视频", "--apply")
        code, status = self.run_sop("status", case_id)
        self.assertEqual(code, 1)
        self.assertEqual(status["artifacts"], ["步骤 2：`data/video.mp4`（原始视频）"])
        shown = self.run_case("show", case_id)[1]["content"]
        self.assertIn("下一动作\n\n步骤 3：提取音频", shown)
        self.assertIn("SOP 步骤 2「取得视频文件」完成", shown)

    def test_failure_needs_a_reason_and_is_surfaced_in_the_next_action(self) -> None:
        case_id = self.start()
        self.assertEqual(self.run_sop("step", case_id, "3", "--fail", "--apply")[0], 2)
        code, result = self.run_sop("step", case_id, "3", "--fail", "--note", "无音轨", "--apply")
        self.assertEqual(code, 0, result)
        self.assertIn("处理步骤 3 的失败", result["next_action"])
        _, status = self.run_sop("status", case_id)
        self.assertEqual(status["failed"], [3])
        self.assertFalse(status["complete"])

    def test_step_preview_does_not_write_and_stale_hash_conflicts(self) -> None:
        case_id = self.start()
        path = next((self.root / "cases").glob("*/CASE.md"))
        before = path.read_text(encoding="utf-8")
        code, preview = self.run_sop("step", case_id, "1", "--done")
        self.assertEqual((code, preview["applied"]), (0, False))
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.run_sop("step", case_id, "1", "--done", "--apply")
        code, stale = self.run_sop("step", case_id, "2", "--done", "--expect-sha256", preview["sha256_before"], "--apply")
        self.assertEqual(code, 5, stale)

    def test_complete_when_every_step_is_done_or_skipped_with_reason(self) -> None:
        case_id = self.start()
        for number in range(1, 7):
            flag = ("--skip", "--note", "不需要") if number == 6 else ("--done",)
            self.assertEqual(self.run_sop("step", case_id, str(number), *flag, "--apply")[0], 0)
        code, status = self.run_sop("status", case_id)
        self.assertEqual((code, status["complete"], status["skipped"]), (0, True, [6]))

    def test_draft_needs_trial_flag_and_active_with_unbumped_edit_is_refused(self) -> None:
        self.create()
        self.assertEqual(self.run_sop("run", "video-to-transcript", "--apply")[0], 5)
        code, trial = self.run_sop("run", "video-to-transcript", "--trial", "--apply")
        self.assertEqual(code, 0, trial)
        self.assertIn("sop-trial", (self.root / trial["path"]).read_text(encoding="utf-8"))
        self.make_active("second", "weekly-review")
        path = self.file("second")
        path.write_text(path.read_text(encoding="utf-8") + "\n- 未登记的改动\n", encoding="utf-8")
        code, refused = self.run_sop("run", "second", "--apply")
        self.assertEqual(code, 5, refused)

    def test_run_requires_case_workspace_and_a_case_creating_sop(self) -> None:
        self.make_active()
        self.write_profile({"mode": "legacy"})
        code, result = self.run_sop("run", "video-to-transcript", "--apply")
        self.assertEqual(code, 4, result)
        self.write_profile({"mode": "case-workspace", "case": {"root": "cases"}})
        path = self.file("video-to-transcript")
        path.write_text(path.read_text(encoding="utf-8").replace('creates_case: "always"', 'creates_case: "never"'), encoding="utf-8")
        self.assertEqual(self.run_sop("run", "video-to-transcript", "--trial", "--apply")[0], 5)

    def test_case_tool_accepts_and_rejects_sop_references(self) -> None:
        code, made = self.run_case("new", "--title", "t", "--slug", "t", "--goal", "g", "--next-action", "n", "--sop", "weekly-review@1.2.3")
        self.assertEqual(code, 0, made)
        self.assertEqual(self.run_case("validate")[0], 0)
        code, bad = self.run_case("new", "--title", "t", "--slug", "u", "--goal", "g", "--next-action", "n", "--sop", "Weekly Review")
        self.assertEqual(code, 2, bad)


class ScopeTests(SopTestCase):
    def test_user_level_sops_are_listed_and_project_level_shadows_them(self) -> None:
        code, made = self.run_sop("new", "--slug", "shared-one", "--template", "blank", "--scope", "user", "--apply")
        self.assertEqual(code, 0, made)
        self.assertTrue((self.user_home / "shared-one" / "SOP.md").is_file())
        _, listed = self.run_sop("list")
        self.assertEqual([(row["id"], row["scope"]) for row in listed["sops"]], [("shared-one", "user")])
        self.create("shared-one", "blank")
        _, listed = self.run_sop("list")
        scopes = {(row["scope"], row["shadowed"]) for row in listed["sops"]}
        self.assertEqual(scopes, {("project", False), ("user", True)})

    def test_retire_and_restore_round_trip(self) -> None:
        self.make_active()
        self.assertEqual(self.run_sop("retire", "video-to-transcript", "--note", "不再使用", "--apply")[0], 0)
        _, index = self.run_sop("index")
        self.assertEqual(index["entries"], [])
        self.assertEqual(self.run_sop("list")[1]["sops"], [])
        self.assertEqual(len(self.run_sop("list", "--all")[1]["sops"]), 1)
        self.assertEqual(self.run_sop("restore", "video-to-transcript", "--apply")[0], 0)
        self.assertEqual(len(self.run_sop("index")[1]["entries"]), 1)

    def test_init_creates_the_directory_once(self) -> None:
        code, preview = self.run_sop("init")
        self.assertFalse(preview["applied"])
        self.run_sop("init", "--apply")
        self.assertTrue((self.root / "sops" / ".gitkeep").is_file())
        self.assertEqual(self.run_sop("init", "--apply")[1]["writes"], [])


class ProjectEntryTests(SopTestCase):
    def cli(self, *args: str, output: bool = True) -> tuple[int, dict]:
        out = io.StringIO()
        prefix = ["--project", str(self.root), "--offline"] + (["--output", "json"] if output else [])
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            code = project_cli.main([*prefix, *args])
        return code, json.loads(out.getvalue())

    def test_sop_subcommand_creates_and_lists_through_the_entry(self) -> None:
        code, payload = self.cli("sop", "new", "--slug", "weekly-review", "--template", "weekly-review", "--apply")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["data"]["applied"])
        code, payload = self.cli("sop", "list")
        self.assertEqual([row["id"] for row in payload["data"]["sops"]], ["weekly-review"])

    def test_output_flag_after_the_subcommand_is_honoured(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = project_cli.main(["--project", str(self.root), "sop", "list", "--output", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["command"], "sop")

    def test_errors_use_the_entry_exit_codes(self) -> None:
        self.assertEqual(self.cli("sop", "show", "nope")[0], 1)
        self.assertEqual(self.cli("sop", "new", "--slug", "Bad Slug")[0], 2)
        self.assertEqual(self.cli("sop", "nonsense")[0], 2)
        self.create("dup", "blank")
        self.assertEqual(self.cli("sop", "new", "--slug", "dup", "--apply")[0], 5)

    def test_demo_projects_have_no_sop(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            code = project_cli.main(["--demo", "study", "--output", "json", "sop", "list"])
        self.assertEqual(code, 4)

    def test_health_stays_quiet_without_sops_then_reports_problems_and_due_items(self) -> None:
        _, quiet = self.cli("health")
        self.assertFalse([item for item in quiet["data"]["findings"] if item["area"] == "流程"])
        self.make_active("weekly-review", "weekly-review")
        with mock.patch.object(sop, "today", return_value=date.today() + timedelta(days=8)):
            _, due = self.cli("health")
        self.assertIn("sop-due", {item["id"] for item in due["data"]["findings"]})
        path = self.file("weekly-review")
        path.write_text(path.read_text(encoding="utf-8").replace("   - 工具：agenthelm", "   - 工具：mystery-tool"), encoding="utf-8")
        code, broken = self.cli("health")
        finding = next(item for item in broken["data"]["findings"] if item["id"] == "sop")
        self.assertEqual(finding["level"], "suggest")
        self.assertEqual(code, 0)  # 流程问题只是建议处理，不改变退出码

    def test_capabilities_list_the_sop_entry(self) -> None:
        _, payload = self.cli("capabilities")
        self.assertIn("sop", {item["id"] for item in payload["data"]})


if __name__ == "__main__":
    unittest.main()
