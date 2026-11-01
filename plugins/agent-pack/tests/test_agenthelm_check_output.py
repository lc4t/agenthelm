"""check 的文本输出：同一插件的不兼容说明只打印一次；Claude 市场被固定在某个 ref 时的提示。"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agenthelm  # noqa: E402
import test_agenthelm_reporting as base  # noqa: E402

LATEST = {
    "schema": 1, "marketplace": "agenthelm", "released_at": "2026-10-21",
    "repository": "https://github.com/example/agenthelm",
    "plugins": {"agent-pack": {"version": "2.0.0"}},
    "migrations": [{"plugin": "agent-pack", "from": "1.0.0", "to": "2.0.0", "change_kind": "code", "breaking": True,
                    "date": "2026-10-21", "summary": "BREAKING-SUMMARY-X", "actions": ["ACTION-STEP-Y"]}],
}


class Fixture(base.Base):
    lang = "zh"

    def setUp(self) -> None:
        super().setUp()
        base.write(self.latest_file, LATEST)
        os.environ["AGENTHELM_LANG"] = self.lang

    def three_clients_behind(self) -> None:
        self.claude_plugin("1.0.0")
        self.codex_plugin("1.0.0")
        self.cursor_plugin("1.0.0")


class DedupeTests(Fixture):
    def test_breaking_notes_print_once_for_three_clients(self) -> None:
        self.three_clients_behind()
        _code, text = self.run_text("check", "--no-project")
        self.assertEqual(text.count("BREAKING-SUMMARY-X"), 1)
        self.assertEqual(text.count("ACTION-STEP-Y"), 1)
        self.assertEqual(text.count("不兼容：同上"), 2)
        first = [line for line in text.splitlines() if "BREAKING-SUMMARY-X" in line][0]
        self.assertIn("不兼容：BREAKING-SUMMARY-X", first)

    def test_json_keeps_the_per_client_detail(self) -> None:
        self.three_clients_behind()
        _code, payload = self.run_json("check", "--no-project")
        behind = [item for item in payload["plugins"] if item["status"] == "behind"]
        self.assertEqual(len(behind), 3)
        for item in behind:
            self.assertEqual(item["steps"][0]["summary"], "BREAKING-SUMMARY-X")
            self.assertEqual(item["steps"][0]["actions"], ["ACTION-STEP-Y"])

    def test_fewer_lines_than_repeating_everything(self) -> None:
        self.three_clients_behind()
        _code, text = self.run_text("check", "--no-project")
        self.claude_plugin("1.0.0")
        _code, single = self.run_text("check", "--no-project", "--client", "claude")
        self.assertLess(len(text.splitlines()), 3 * len(single.splitlines()))


class DedupeDetailsTests(Fixture):
    def test_details_prints_the_notes_once_and_every_client_row_points_at_them(self) -> None:
        self.three_clients_behind()
        for flags in ((), ("--details",)):
            _code, text = self.run_text("check", "--no-project", *flags)
            self.assertEqual(text.count("BREAKING-SUMMARY-X"), 1, flags)
            self.assertEqual(text.count("ACTION-STEP-Y"), 1, flags)
            rows = text.split("  [")
            for client in ("claude", "codex", "cursor"):
                row = next(r for r in rows if r.startswith(client + "]"))
                self.assertIn("含不兼容变更", row)
                self.assertTrue("BREAKING-SUMMARY-X" in row or "同上（见 claude）" in row, (client, flags))

    def test_update_and_install_previews_do_not_repeat_the_notes(self) -> None:
        self.three_clients_behind()
        for args in (("update", "--no-project", "--only", "plugins"), ("install",)):
            _code, text = self.run_text(*args)
            self.assertLessEqual(text.count("BREAKING-SUMMARY-X"), 1, args)


class ScaffoldCountTests(Fixture):
    """A3: the JSON counters and the numbers in the text come from the same place."""

    def synthetic(self) -> dict:
        scaffold = agenthelm.scaffold_module(agenthelm.Env(home=self.home, variables={}))

        def finding(identifier, kind):
            return scaffold._finding(identifier, kind, "T-" + identifier, "R", "fp", None)

        findings = [finding("profile:a", "profile-invalid"), finding("skel:a", "skeleton-missing"),
                    finding("skel:b", "skeleton-missing"), finding("skel:c", "skeleton-missing"),
                    finding("region:x#1", "region-outdated"), finding("region:y#1", "region-outdated")]
        return {"ok": False, "required_ok": False, "root": str(self.home), "findings": findings, "skipped": [],
                "levels": scaffold.level_counts(findings), "categories": scaffold.summarize(findings),
                "profile_conflicts": [{"message": "CONFLICT-ONE"}],
                "profile_extras": {"total": 3, "own": 2, "suspected_typo": 1, "read_by_plugin": 0}, "extras": [],
                "decisions": {"errors": [], "conflict_copies": []}}

    def report(self) -> dict:
        project = self.root / "proj"
        project.mkdir()
        with mock.patch.object(agenthelm, "scaffold_report", return_value=self.synthetic()):
            return agenthelm.run_check(agenthelm.Env(home=self.home, variables={}), project,
                                       latest_source=str(self.latest_file))

    def text_numbers(self, report: dict) -> dict:
        import re
        text = agenthelm.render_text(report)
        info_lines = [line for line in text.splitlines() if "[脚手架·信息]" in line]
        info = 0
        for line in info_lines:
            if "Profile 中有" in line:
                info += int(re.search(r"Profile 中有 (\d+) 个", line).group(1))
            elif "个受管区块有差异" in line:
                info += int(re.search(r"\] (\d+) 个受管区块", line).group(1))
            else:
                info += 1
        must = sum(1 for line in text.splitlines() if line.startswith("  [脚手架] "))
        return {"info": info, "must": must}

    def test_info_counter_equals_the_info_items_in_the_text(self) -> None:
        report = self.report()
        numbers = self.text_numbers(report)
        self.assertEqual(report["summary"]["scaffold_info"], numbers["info"])
        self.assertEqual(report["summary"]["scaffold_info"], 2 + 1 + 3)
        self.assertEqual(report["summary"]["scaffold_required"], numbers["must"])

    def test_counters_come_from_one_function(self) -> None:
        report = self.report()
        counts = agenthelm.scaffold_counts(report["scaffold"])
        for key, summary_key in (("required", "scaffold_required"), ("optional", "scaffold_optional"),
                                 ("info", "scaffold_info"), ("profile_notes", "profile_notes")):
            self.assertEqual(report["summary"][summary_key], counts[key], key)
        self.assertEqual(report["summary"]["scaffold_findings"], 6)
        self.assertEqual(report["summary"]["scaffold_optional"], 3)

    def test_info_does_not_change_up_to_date_or_exit_semantics(self) -> None:
        report = self.report()
        self.assertEqual(report["summary"]["scaffold_required"], 1)
        self.assertFalse(report["summary"]["up_to_date"])


class DedupeEnglishTests(Fixture):
    lang = "en"

    def test_english_text_has_each_step_once(self) -> None:
        self.three_clients_behind()
        _code, text = self.run_text("check", "--no-project")
        self.assertEqual(text.count("BREAKING-SUMMARY-X"), 1)
        self.assertEqual(text.count("same as above"), 2)
        self.assertNotIn("不兼容", text)


class PinnedTests(Fixture):
    def pin(self) -> None:
        base.write(self.home / ".claude/settings.json", {"extraKnownMarketplaces": {"agenthelm": {
            "source": {"source": "github", "repo": "example/agenthelm", "ref": "v1.0.0"}}}})
        self.claude_plugin("1.0.0")
        self.stub("claude")

    def test_preview_warns_about_the_pinned_ref_and_does_not_touch_settings(self) -> None:
        self.pin()
        settings = self.home / ".claude/settings.json"
        before = settings.read_text()
        _code, text = self.run_text("update", "--no-project", "--only", "plugins")
        self.assertIn("固定在 v1.0.0", text)
        self.assertIn("最新版本 2.0.0", text)
        self.assertIn("去掉 ref 跟随 main", text)
        self.assertEqual(settings.read_text(), before)

    def test_apply_without_change_explains_the_pin(self) -> None:
        self.pin()
        code, payload = self.run_json("update", "--apply", "--no-project", "--only", "plugins")
        step = [s for s in payload["steps"] if s["description"].startswith("Claude Code 更新")][0]
        self.assertEqual(step["status"], "unchanged")
        self.assertIn("固定在 v1.0.0", step["reason"])
        self.assertEqual(code, 1)
        _code, text = self.run_text("update", "--apply", "--no-project", "--only", "plugins")
        self.assertIn("[完成，无变化]", text)
        self.assertIn("原因：Claude 的 settings 把 agenthelm 市场固定在 v1.0.0", text)

    def test_unpinned_claude_has_no_pin_note(self) -> None:
        self.claude_plugin("1.0.0")
        self.stub("claude")
        _code, text = self.run_text("update", "--no-project", "--only", "plugins")
        self.assertNotIn("固定在", text)


class PinnedEnglishTests(PinnedTests):
    lang = "en"

    def test_preview_warns_about_the_pinned_ref_and_does_not_touch_settings(self) -> None:
        self.pin()
        _code, text = self.run_text("update", "--no-project", "--only", "plugins")
        self.assertIn("pin the agenthelm marketplace to v1.0.0", text)
        self.assertIn("latest version: 2.0.0", text)
        self.assertIn("remove the ref to follow main", text)

    def test_apply_without_change_explains_the_pin(self) -> None:
        self.pin()
        _code, payload = self.run_json("update", "--apply", "--no-project", "--only", "plugins")
        step = [s for s in payload["steps"] if s["description"].startswith("Claude Code: update")][0]
        self.assertIn("pin the agenthelm marketplace to v1.0.0", step["reason"])

    def test_unpinned_claude_has_no_pin_note(self) -> None:
        self.claude_plugin("1.0.0")
        self.stub("claude")
        _code, text = self.run_text("update", "--no-project", "--only", "plugins")
        self.assertNotIn("pin the agenthelm", text)


if __name__ == "__main__":
    unittest.main()
