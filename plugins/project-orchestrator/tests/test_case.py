from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import case  # noqa: E402


class CaseToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile({"mode": "case-workspace", "case": {"root": "cases", "artifact_mode": "case-local"}})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def profile(self, work: dict) -> None:
        path = self.root / case.PROFILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": "1.0", "work": work}), encoding="utf-8")

    def run_case(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = case.main(["--project", str(self.root), "--output", "json", *args])
        return code, json.loads(output.getvalue())

    def new(self, slug: str = "first", *extra: str) -> dict:
        code, result = self.run_case("new", "--title", "第一项工作", "--slug", slug, "--goal", "交付报告",
                                     "--next-action", "整理材料", *extra)
        self.assertEqual(code, 0, result)
        return result

    def test_new_creates_a_numbered_case_that_validates(self) -> None:
        first, second = self.new(), self.new("second")
        self.assertRegex(first["id"], r"^CASE-\d{8}-001-first$")
        self.assertRegex(second["id"], r"^CASE-\d{8}-002-second$")
        text = (self.root / first["path"]).read_text(encoding="utf-8")
        self.assertIn('title: "第一项工作"', text)
        self.assertIn("## 工作记录", text)
        self.assertNotIn("{{", text)
        code, checked = self.run_case("validate")
        self.assertEqual(code, 0, checked)
        self.assertTrue(all(item["valid"] for item in checked["results"]))

    def test_list_orders_by_status_and_hides_closed_cases(self) -> None:
        active = self.new("active-one")
        captured = self.new("captured-one", "--status", "captured")
        done = self.new("done-one")
        self.run_case("update", done["id"], "--status", "done", "--result", "已交付")
        _, listed = self.run_case("list")
        self.assertEqual([item["id"] for item in listed["cases"]], [active["id"], captured["id"]])
        _, everything = self.run_case("list", "--all")
        self.assertEqual(everything["cases"][-1]["id"], done["id"])

    def test_update_changes_fields_and_appends_records_without_losing_the_body(self) -> None:
        created = self.new()
        code, result = self.run_case("update", "first", "--next-action", "提交评审", "--log", "完成初稿",
                                     "--decision", "采用方案 B", "--add-deliverable", "docs/report.md",
                                     "--add-deliverable", "docs/report.md")
        self.assertEqual(code, 0, result)
        data, body = case.read_case(self.root / created["path"])
        self.assertEqual(data["next_action"], "提交评审")
        self.assertEqual(data["deliverables"], ["docs/report.md"])
        self.assertIn("完成初稿", body)
        self.assertIn("采用方案 B", body)
        self.assertNotIn("\n暂无。\n", body.split("## 决策记录")[1].split("##")[0])
        self.assertIn("## 经验候选", body)
        self.assertIn("提交评审", body.split("## 下一动作")[1].split("##")[0])

    def test_update_enforces_status_rules_and_detects_concurrent_edits(self) -> None:
        created = self.new()
        code, result = self.run_case("update", created["id"], "--status", "waiting")
        self.assertEqual((code, result["ok"]), (2, False))
        self.assertIn("waiting_for", result["error"])
        code, result = self.run_case("update", created["id"], "--log", "x", "--expect-updated-at", "2000-01-01T00:00:00Z")
        self.assertEqual(result["code"], "conflict")

    def test_project_tool_takes_over_writes_but_reads_still_work(self) -> None:
        created = self.new()
        self.profile({"mode": "case-workspace", "case": {"root": "cases", "command": "make case"}})
        code, result = self.run_case("new", "--title", "x", "--slug", "x", "--goal", "y", "--next-action", "z")
        self.assertEqual(result["code"], "project-tool")
        code, result = self.run_case("update", created["id"], "--log", "x")
        self.assertEqual(result["code"], "project-tool")
        code, listed = self.run_case("list")
        self.assertEqual(code, 0)
        self.assertEqual(len(listed["cases"]), 1)

    def test_refuses_projects_without_case_workspace_and_unsafe_roots(self) -> None:
        self.profile({"mode": "legacy"})
        self.assertEqual(self.run_case("list")[1]["code"], "mode")
        self.profile({"mode": "case-workspace", "case": {"root": "../outside"}})
        self.assertEqual(self.run_case("list")[1]["code"], "profile-invalid")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (self.root / "linked").symlink_to(outside.name, target_is_directory=True)
        self.profile({"mode": "case-workspace", "case": {"root": "linked"}})
        self.assertEqual(self.run_case("list")[1]["code"], "symlink")

    def test_validate_reports_schema_and_identity_problems(self) -> None:
        created = self.new()
        path = self.root / created["path"]
        path.write_text(path.read_text(encoding="utf-8").replace('status: "active"', 'status: "maybe"'),
                        encoding="utf-8")
        code, result = self.run_case("validate")
        self.assertEqual(code, 1)
        self.assertTrue(any("status" in error for error in result["results"][0]["errors"]))

    def test_slug_rejects_credential_words(self) -> None:
        code, result = self.run_case("new", "--title", "x", "--slug", "api-token", "--goal", "y", "--next-action", "z")
        self.assertEqual(code, 2)

    def test_options_work_before_or_after_the_subcommand(self) -> None:
        self.new()
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = case.main(["list", "--project", str(self.root), "--output", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output.getvalue())["cases"]), 1)

    def test_frontmatter_round_trip(self) -> None:
        data = {"schema_version": "1.0", "id": "CASE-20260101-001-x", "title": "引号\"与冒号: 测试",
                "status": "active", "tags": [], "evidence": ["a", "b"], "created_at": "2026-01-01T00:00:00+08:00"}
        text = case.render_frontmatter(data)
        parsed = case.parse_frontmatter(text[4:-5])
        self.assertEqual(parsed, data)


if __name__ == "__main__":
    unittest.main()
