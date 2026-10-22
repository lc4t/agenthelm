from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import assets  # noqa: E402
import wf  # noqa: E402

CASE_ID = "CASE-20261001-001-alpha"


class WfTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / "project"
        self.cache = base / "cache"
        environment = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache), "NO_COLOR": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        docs = self.root / "cases" / CASE_ID / "docs"
        docs.mkdir(parents=True)
        (self.root / "cases" / CASE_ID / "CASE.md").write_text(
            '---\nid: "CASE-20261001-001-alpha"\ntitle: "阿尔法专项"\nstatus: "active"\n---\n', encoding="utf-8")
        for name in ("summary_v1.md", "summary_v2.md"):
            (docs / name).write_text("# 季度总结\n", encoding="utf-8")

    def run_wf(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = wf.main(list(args))
        return code, output.getvalue()

    def test_json_search_saves_last_results_in_the_cache_only(self) -> None:
        before = sorted(str(path) for path in self.root.rglob("*"))
        code, text = self.run_wf("季度总结", "--project", str(self.root), "--output", "json")
        self.assertEqual(code, 0, text)
        result = json.loads(text)
        self.assertEqual(result["results"][0]["name"], "summary_v2.md")
        self.assertEqual([item["name"] for item in result["results"][0]["versions"]], ["summary_v1.md"])
        self.assertEqual(before, sorted(str(path) for path in self.root.rglob("*")))
        last = assets.Project(self.root).state_dir() / assets.LAST_NAME
        self.assertEqual(json.loads(last.read_text(encoding="utf-8"))[0]["path"],
                         f"cases/{CASE_ID}/docs/summary_v2.md")

    def test_numbered_actions_use_the_last_results(self) -> None:
        self.run_wf("--project", str(self.root), "summary")
        code, text = self.run_wf("p", "1", "--project", str(self.root))
        self.assertEqual(code, 0)
        self.assertEqual(text.strip(), str(self.root.resolve() / "cases" / CASE_ID / "docs" / "summary_v2.md"))
        code, text = self.run_wf("v", "1", "--project", str(self.root))
        self.assertIn("summary_v1.md", text)
        code, text = self.run_wf("p", "9", "--project", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("没有第 9 项", text)

    def test_open_reveal_and_copy_print_the_path_outside_macos(self) -> None:
        project = assets.Project(self.root)
        relative = f"cases/{CASE_ID}/docs/summary_v2.md"
        for action in ("open", "reveal", "copy"):
            output = io.StringIO()
            with mock.patch("sys.stdout", output), mock.patch.object(wf.subprocess, "run") as run:
                self.assertEqual(wf.do_action(action, relative, project, platform="linux"), "printed")
            run.assert_not_called()
            self.assertEqual(output.getvalue().strip(), str(project.root / relative))

    def test_macos_without_the_commands_also_prints(self) -> None:
        project = assets.Project(self.root)
        with mock.patch.object(wf.shutil, "which", return_value=None), \
                mock.patch.object(wf.subprocess, "run") as run, mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(wf.do_action("open", "cases", project, platform="darwin"), "printed")
        run.assert_not_called()
        with mock.patch.object(wf.shutil, "which", return_value="/usr/bin/open"), \
                mock.patch.object(wf.subprocess, "run") as run, mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(wf.do_action("reveal", "cases", project, platform="darwin"), "reveal")
        self.assertEqual(run.call_args.args[0][:2], ["open", "-R"])

    def test_cases_check_and_filters(self) -> None:
        code, text = self.run_wf("cases", "--project", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("阿尔法专项", text)
        code, text = self.run_wf("check", "--project", str(self.root))
        self.assertIn("台账巡检", text)
        code, text = self.run_wf("summary", "ext:pdf", "--project", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("没有找到", text)
        self.assertEqual(wf.parse_filters(["a", "ext:xlsx", "in:alpha", "kind:draft"]),
                         (["a"], {"ext": "xlsx", "container": "alpha", "kind": "draft"}))

    def test_bad_global_option_reports_an_error(self) -> None:
        output = io.StringIO()
        with mock.patch("sys.stderr", output):
            code = wf.main(["--output", "yaml", "x"])
        self.assertEqual(code, 2)
        self.assertIn("--output", output.getvalue())


if __name__ == "__main__":
    unittest.main()
