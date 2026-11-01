"""``agenthelm check`` 的脚手架部分：只读、决定文件生效、旧 agents-init 时降级。"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "plugins/agent-pack/runtime"
INIT = ROOT / "plugins/agents-init/skills/agents-init/scripts/init_project.py"
sys.path.insert(0, str(RUNTIME))
import agenthelm  # noqa: E402
import agenthelm_runtime  # noqa: E402

LATEST = {"schema": 1, "marketplace": "agenthelm", "released_at": "2026-10-27", "plugins": {}, "migrations": []}


class ScaffoldCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.env = agenthelm.Env(home=base / "home", variables={"XDG_STATE_HOME": str(base / "state")})
        self.latest = base / "latest.json"
        self.latest.write_text(json.dumps(LATEST), encoding="utf-8")
        self.project = base / "sample-notes"
        self.project.mkdir()
        done = subprocess.run([sys.executable, str(INIT), "--project", str(self.project), "--name", "Sample Notes",
                               "--slug", "sample-notes", "--project-type", "notes", "--vcs", "none", "--stack", "md",
                               "--runtime", "local", "--agent-cli", "none", "--no-entry", "--apply", "--output", "json"],
                              capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stdout)
        (self.project / ".agents/mcp.json").unlink()

    def report(self, **kwargs) -> dict:
        return agenthelm.run_check(self.env, self.project, latest_source=str(self.latest), **kwargs)

    def test_missing_file_is_optional_and_does_not_count_against_up_to_date_or_one_line(self) -> None:
        report = self.report()
        self.assertEqual([item["id"] for item in report["scaffold"]["findings"]], ["skel:.agents/mcp.json"])
        self.assertEqual(report["scaffold"]["findings"][0]["level"], "suggest")
        self.assertEqual(report["summary"]["scaffold_findings"], 1)
        self.assertEqual(report["summary"]["scaffold_optional"], 1)
        self.assertEqual(report["summary"]["scaffold_required"], 0)
        self.assertTrue(report["summary"]["up_to_date"])
        self.assertIsNone(agenthelm.one_line(report))
        text = agenthelm.render_text(report)
        self.assertIn("1 个缺失文件", text)
        self.assertNotIn("skel:.agents/mcp.json", text)
        self.assertIn("skel:.agents/mcp.json", agenthelm.render_text(report, details=True))

    def test_a_required_finding_counts_against_up_to_date_and_one_line(self) -> None:
        report = self.report()
        report["scaffold"]["findings"][0]["level"] = "must"
        report["summary"]["scaffold_required"] = 1
        report["summary"]["up_to_date"] = False
        self.assertIn("1 项需要处理的差异", agenthelm.one_line(report))

    def test_a_subdirectory_of_the_project_finds_the_profile(self) -> None:
        inner = self.project / "docs"
        report = agenthelm.run_check(self.env, inner, latest_source=str(self.latest))
        self.assertEqual(Path(report["scaffold"]["root"]), self.project)

    def write_profile(self, mutate) -> None:
        path = self.project / ".agents/moe.sakanano.agent-pack/project.json"
        profile = json.loads(path.read_text(encoding="utf-8"))
        mutate(profile)
        path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    def test_undeclared_fields_are_one_info_line_by_default_and_listed_with_show_extras(self) -> None:
        before = self.report()["summary"]
        self.write_profile(lambda p: (p["privacy"].update(context_requires_confirmation=True, forbidden_default_read=["x/"]),
                                      p.update(team_notes={"a": 1})))
        report = self.report()
        self.assertEqual({key: report["summary"][key] for key in ("scaffold_findings", "up_to_date")},
                         {key: before[key] for key in ("scaffold_findings", "up_to_date")}, "info never counts")
        plain = agenthelm.render_text(report)
        self.assertIn("Profile 中有 3 个 schema 未声明的字段", plain)
        self.assertIn("项目自有 2，疑似拼写错误 1", plain)
        self.assertIn("check --show-extras 展开", plain)
        self.assertNotIn("privacy.context_requires_confirmation", plain)
        self.assertEqual(sum("schema 未声明" in line for line in plain.splitlines()), 1)
        full = agenthelm.render_text(report, show_extras=True)
        for path in ("privacy.context_requires_confirmation", "privacy.forbidden_default_read", "team_notes"):
            self.assertIn(path, full)
        self.assertIn("疑似拼写错误，是不是想写 privacy.forbidden_default_reads", full)
        self.assertNotIn("check --show-extras 展开", full)
        self.assertEqual(report["scaffold"]["profile_extras"]["total"], 3)
        self.assertEqual(report["summary"]["profile_notes"], 3)  # extras only here; scaffold_info counts info findings plus these notes
        self.assertEqual(report["summary"]["scaffold_info"], before["scaffold_info"] + 3)  # = the 3 [脚手架·信息] items

    def test_check_cli_accepts_show_extras_and_strict_ignores_info(self) -> None:
        self.write_profile(lambda p: p.update(team_notes=1))
        parsed = agenthelm.parser().parse_args(["check", "--show-extras"])
        self.assertTrue(parsed.show_extras)
        text = agenthelm.render_text(self.report(), show_extras=True)
        self.assertIn("team_notes（项目自有）", text)

    def test_conflicting_value_is_info_with_options_and_no_finding(self) -> None:
        self.write_profile(lambda p: p["work"]["case"].update(directory_naming="自己的命名规则"))
        report = self.report()
        self.assertEqual([item["id"] for item in report["scaffold"]["findings"]], ["skel:.agents/mcp.json"])
        text = agenthelm.render_text(report)
        self.assertEqual(report["summary"]["profile_notes"],
                         len(report["scaffold"]["profile_conflicts"]) + report["scaffold"]["profile_extras"]["total"])
        self.assertGreaterEqual(report["summary"]["profile_notes"], 1)
        self.assertIn("[脚手架·信息] Profile 的 work.case.directory_naming", text)
        self.assertIn("保留为项目自有字段", text)
        self.assertNotIn("无法使用", text)

    def test_outside_a_project_there_is_no_scaffold_section(self) -> None:
        report = agenthelm.run_check(self.env, self.project.parent, latest_source=str(self.latest))
        self.assertIsNone(report["scaffold"])
        self.assertTrue(report["summary"]["up_to_date"])

    def test_check_is_read_only(self) -> None:
        before = {p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        self.report()
        agenthelm.render_text(self.report(), show_skipped=True)
        self.assertEqual({p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()}, before)

    def test_skipped_items_vanish_and_show_with_their_reason(self) -> None:
        module = agenthelm.scaffold_module(self.env)
        module.decide(self.project, "skel:.agents/mcp.json", "skip", reason="no MCP in this project", apply=True,
                      state_dir=self.env.state_dir)
        report = self.report()
        self.assertEqual(report["scaffold"]["findings"], [])
        self.assertTrue(report["summary"]["up_to_date"])
        self.assertNotIn("no MCP", agenthelm.render_text(report))
        self.assertIn("no MCP in this project", agenthelm.render_text(report, show_skipped=True))
        self.assertEqual(len(self.report(reprompt=True)["scaffold"]["findings"]), 1)

    def test_an_agents_init_without_scaffold_support_degrades_instead_of_failing(self) -> None:
        with mock.patch.object(agenthelm, "scaffold_module", return_value=None):
            report = self.report()
        self.assertFalse(report["scaffold"]["available"])
        self.assertIn("agents-init", report["scaffold"]["error"])
        self.assertIn("无法检查", agenthelm.render_text(report))

    def test_machine_finding_explains_which_commands_work(self) -> None:
        data = {"running_from": "客户端插件", "current": None}
        text = agenthelm_runtime.missing_text(data, {"agenthelm": None, "wf": None})
        self.assertIn("wf", text["reason"])
        self.assertIn("--link-bin", text["label"])
        text = agenthelm_runtime.missing_text(data, {"agenthelm": "/opt/bin/agenthelm", "wf": None})
        self.assertIn("/opt/bin/agenthelm", text["reason"])

    def test_fix_is_forwarded_to_the_project_entry(self) -> None:
        with mock.patch.object(agenthelm, "run_project", return_value=0) as forwarded:
            self.assertEqual(agenthelm.main(["fix", "add-skeleton", "--apply"]), 0)
        forwarded.assert_called_once_with(["fix", "add-skeleton", "--apply"])


class SyncFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def project_in(self, *parts: str) -> Path:
        root = self.base.joinpath(*parts)
        root.mkdir(parents=True)
        return root

    def test_known_sync_services_are_recognised_by_path(self) -> None:
        cases = {
            "iCloud Drive": ("Library", "Mobile Documents", "com~apple~CloudDocs", "notes"),
            "Dropbox": ("Dropbox (Personal)", "notes"),
            "OneDrive": ("OneDrive - Example Org", "notes"),
            "Google Drive": ("GoogleDrive-someone", "My Drive", "notes"),
        }
        for service, parts in cases.items():
            with self.subTest(service=service):
                found = agenthelm.sync_folder(self.project_in(service.replace(" ", "-"), *parts))
                self.assertEqual(found["service"], service)

    def test_an_ordinary_folder_is_not_flagged(self) -> None:
        self.assertIsNone(agenthelm.sync_folder(self.project_in("work", "dropbox-tools", "notes")))

    def test_rebuildable_directories_are_listed_and_nothing_is_touched(self) -> None:
        root = self.project_in("Dropbox", "notes")
        (root / "node_modules/pkg").mkdir(parents=True)
        (root / "app/__pycache__").mkdir(parents=True)
        (root / "docs").mkdir()
        found = agenthelm.sync_folder(root)
        self.assertEqual(found["rebuildable"], ["app/__pycache__", "node_modules"])
        self.assertTrue((root / "node_modules/pkg").is_dir())


class FingerprintCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "sample-notes"
        self.project.mkdir()
        done = subprocess.run([sys.executable, str(INIT), "--project", str(self.project), "--name", "Sample Notes",
                               "--slug", "sample-notes", "--project-type", "notes", "--vcs", "none", "--stack", "md",
                               "--runtime", "local", "--agent-cli", "none", "--no-entry", "--apply", "--output", "json"],
                              capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stdout)
        latest = self.base / "latest.json"
        latest.write_text(json.dumps(LATEST), encoding="utf-8")
        self.env = {**__import__("os").environ, "HOME": str(self.base / "home"), "XDG_STATE_HOME": str(self.base / "st"),
                    "XDG_CACHE_HOME": str(self.base / "cache"), "AGENTHELM_LATEST_URL": str(latest), "NO_COLOR": "1"}

    def run_check(self, *args: str, project: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(RUNTIME / "agenthelm.py"), "check", "--project",
                               str(project or self.project), "--fingerprint", *args],
                              capture_output=True, text=True, check=False, env=self.env)

    def test_equal_scaffolds_print_equal_fingerprints_and_explain_finds_no_difference(self) -> None:
        mine = self.run_check("--output", "json")
        self.assertEqual(mine.returncode, 0, mine.stderr)
        saved = self.base / "other.json"
        saved.write_text(mine.stdout, encoding="utf-8")
        same = self.run_check("--explain", str(saved))
        self.assertEqual(same.returncode, 0, same.stdout)
        self.assertIn("一致", same.stdout)

    def test_explain_points_at_the_file_that_differs(self) -> None:
        saved = self.base / "other.json"
        saved.write_text(self.run_check("--output", "json").stdout, encoding="utf-8")
        (self.project / ".agents/mcp.json").unlink()
        mine = json.loads(self.run_check("--output", "json").stdout)
        self.assertNotEqual(mine["fingerprint"], json.loads(saved.read_text())["fingerprint"])
        different = self.run_check("--explain", str(saved))
        self.assertEqual(different.returncode, 1)
        self.assertIn("skeleton:.agents/mcp.json", different.stdout)

    def test_outside_a_project_there_is_nothing_to_fingerprint(self) -> None:
        result = self.run_check(project=self.base)
        self.assertEqual(result.returncode, 4)


if __name__ == "__main__":
    unittest.main()
