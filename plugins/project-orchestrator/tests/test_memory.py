from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "runtime" / "memory.py"


class MemoryToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name).resolve() / "project"
        (self.project / "docs").mkdir(parents=True)
        (self.project / "docs" / "source.md").write_text("SOURCE_FIXTURE", encoding="utf-8")
        (self.project / "body.md").write_text("BODY_FIXTURE", encoding="utf-8")

    def run_tool(self, *arguments: object, code: int = 0) -> str:
        result = subprocess.run([sys.executable, str(SCRIPT), *(str(item) for item in arguments), "--project", str(self.project),
                                 "--today", "2026-03-01"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        return result.stdout

    def create(self, *extra: object, kind: str = "reference", slug: str = "fixture", code: int = 0, apply: bool = True) -> dict:
        arguments = ["create", "--type", kind, "--slug", slug, "--description", "WHEN_TO_RECALL", "--body-file", "body.md",
                     "--date", "2026-01-01", *extra]
        return json.loads(self.run_tool(*arguments, *(["--apply"] if apply else []), code=code))

    def policy(self, value: dict) -> None:
        (self.project / "policy.json").write_text(json.dumps(value), encoding="utf-8")

    def check(self, *extra: object, code: int = 0) -> dict:
        return json.loads(self.run_tool("check", *extra, code=code))

    def test_preview_writes_nothing_and_create_numbers_entries_by_day(self) -> None:
        preview = self.create(apply=False)
        self.assertEqual(preview["status"], "preview")
        self.assertFalse((self.project / "memory").exists())
        first = self.create("--source", "docs/source.md")
        second = self.create(slug="another")
        self.assertEqual(first["path"], "memory/MEM-20260101-001-fixture.md")
        self.assertEqual(second["path"], "memory/MEM-20260101-002-another.md")
        text = (self.project / first["path"]).read_text(encoding="utf-8")
        self.assertIn('description: "WHEN_TO_RECALL"', text)
        self.assertIn('  - "docs/source.md"', text)
        self.assertIn("BODY_FIXTURE", text)
        self.assertEqual(self.run_tool("index"), "- `MEM-20260101-001-fixture`（reference）：WHEN_TO_RECALL\n"
                                                 "- `MEM-20260101-002-another`（reference）：WHEN_TO_RECALL\n")
        self.assertTrue(self.check()["ok"])

    def test_invalid_input_is_rejected_before_writing(self) -> None:
        self.create(kind="unknown", code=2)
        self.create(slug="Bad Slug", code=2)
        self.create("--source", "../outside.md", code=2)
        self.create("--source", "docs/missing.md", code=2)
        self.create("--review-after", "soon", code=2)
        self.create(kind="assumption", code=2)
        self.assertFalse((self.project / "memory").exists())
        self.assertEqual(self.create("--review-after", "2026-06-01", kind="assumption")["status"], "written")

    def test_check_reports_structural_problems(self) -> None:
        created = self.create("--source", "docs/source.md")
        (self.project / "docs" / "source.md").unlink()
        memory = self.project / "memory"
        (memory / "MEM-20260101-009-broken.md").write_text("no frontmatter", encoding="utf-8")
        text = (self.project / created["path"]).read_text(encoding="utf-8")
        (memory / "MEM-20260102-001-copy.md").write_text(text.replace('promoted_to: ""', 'promoted_to: "rules/x"'), encoding="utf-8")
        result = self.check(code=1)
        codes = sorted(item["code"] for item in result["problems"])
        self.assertEqual(codes, ["duplicate-id", "id-mismatch", "missing-source", "missing-source", "promoted-still-active", "unreadable"])

    def test_due_entries_are_marked_but_do_not_fail_the_check(self) -> None:
        self.create("--review-after", "2026-02-01")
        self.create(slug="later")
        self.assertIn("｜可能过期", self.run_tool("index").splitlines()[0])
        result = self.check()
        self.assertTrue(result["ok"])
        self.assertEqual(result["due"], ["MEM-20260101-001-fixture"])
        self.policy({"review_interval_days": {"reference": 30}})
        self.assertEqual(len(self.check("--policy", "policy.json")["due"]), 2)
        verified = json.loads(self.run_tool("verify", "--id", "MEM-20260101-002-later", "--apply"))
        self.assertIn('verified_at: "2026-03-01"', verified["content"])
        self.assertEqual(self.check("--policy", "policy.json")["due"], ["MEM-20260101-001-fixture"])

    def test_retired_entries_leave_the_index_and_keep_their_reason(self) -> None:
        created = self.create()
        self.run_tool("retire", "--id", "MEM-20260101-001-fixture", code=2)
        preview = json.loads(self.run_tool("retire", "--id", "MEM-20260101-001-fixture", "--promoted-to", "rules/example"))
        self.assertEqual(preview["status"], "preview")
        self.assertIn("WHEN_TO_RECALL", self.run_tool("index"))
        self.run_tool("retire", "--id", "MEM-20260101-001-fixture", "--promoted-to", "rules/example", "--apply")
        self.assertEqual(self.run_tool("index"), "")
        text = (self.project / created["path"]).read_text(encoding="utf-8")
        self.assertIn('status: "retired"', text)
        self.assertIn("**退役**：2026-03-01，已晋升到 rules/example", text)
        self.assertTrue(self.check()["ok"])
        self.run_tool("retire", "--id", "MEM-20260101-404-missing", "--reason", "x", code=2)

    def test_project_policy_sets_budgets_and_summary_sections(self) -> None:
        self.create()
        summary = self.project / ".agent-doc" / "chat-summary.md"
        summary.parent.mkdir()
        summary.write_text("# 会话摘要\n\n## 待确认假设\n\n无。\n\n## 未解决冲突与待裁定项\n\n无。\n", encoding="utf-8")
        result = self.check(code=1)
        self.assertEqual([item["message"] for item in result["problems"]], ["缺少小节：Opinion 演化候选"])
        summary.write_text(summary.read_text(encoding="utf-8") + "\n## Opinion 演化候选\n\n无。\n", encoding="utf-8")
        self.assertTrue(self.check()["ok"])
        self.policy({"index": {"max_lines": 0 + 1, "max_bytes": 10, "max_description_chars": 5},
                     "startup_bundle": {"max_bytes": 20, "files": [".agent-doc/chat-summary.md", "missing.md"]},
                     "chat_summary": {"max_bytes": 10}})
        codes = sorted(item["code"] for item in self.check("--policy", "policy.json", code=1)["problems"])
        self.assertEqual(codes, ["description-too-long", "index-too-large", "startup-too-large", "summary-too-large"])

    def test_root_and_policy_come_from_the_project_profile(self) -> None:
        profile = self.project / ".agents" / "moe.sakanano.agent-pack" / "project.json"
        profile.parent.mkdir(parents=True)
        self.policy({"types": ["note"]})
        profile.write_text(json.dumps({"memory": {"provider": "project-orchestrator", "root": "notes/memory", "policy": "policy.json"}}), encoding="utf-8")
        created = self.create(kind="note")
        self.assertEqual(created["path"], "notes/memory/MEM-20260101-001-fixture.md")
        self.create(kind="reference", code=2)
        self.assertEqual(self.check()["entries"], 1)
        self.assertEqual(json.loads(self.run_tool("list"))["entries"][0]["type"], "note")


if __name__ == "__main__":
    unittest.main()
