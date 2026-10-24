"""项目入口（阶段 1 Demo）的命令契约与交互菜单。

演示数据不读写真实文件；这里验证的是界面契约：退出码、结构化输出、非交互不等待输入、
不可用能力如实说明，以及菜单能从任务分组走到结果与后续操作。
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
CLI = RUNTIME / "project_cli.py"
ENV = {**os.environ, "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def run(*args: str) -> subprocess.CompletedProcess:
    # stdin is an open pipe that never delivers input: a command that waits would hit the timeout.
    return subprocess.run([sys.executable, str(CLI), *args], input="", capture_output=True, text=True,
                          env=ENV, timeout=20, check=False)


def run_json(*args: str) -> tuple[int, dict]:
    result = run(*args, "--output", "json")
    return result.returncode, json.loads(result.stdout)


class NonInteractiveContractTests(unittest.TestCase):
    def test_home_without_a_terminal_prints_the_overview_and_exits(self) -> None:
        result = run("--demo", "study")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("日语 N2 备考", result.stdout)
        self.assertIn("【演示数据", result.stdout)

    def test_every_result_is_labelled_as_demo(self) -> None:
        for command in (["progress"], ["guide"], ["health"], ["capabilities"], ["find", "语法"]):
            code, payload = run_json("--demo", "study", *command)
            self.assertTrue(payload["demo"], command)
            self.assertIn(code, (0, 1), command)

    def test_global_options_work_in_any_position(self) -> None:
        first = run_json("find", "模拟题", "--demo", "study")
        second = run_json("--demo", "study", "find", "模拟题")
        self.assertEqual(first, second)
        self.assertEqual(first[1]["data"]["total"], 2)

    def test_find_with_no_result_is_attention_not_failure(self) -> None:
        code, payload = run_json("--demo", "blank", "find", "任何")
        self.assertEqual(code, 1)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["attention"])

    def test_items_are_found_by_old_ids_and_aliases(self) -> None:
        for token in ("T-0007", "单词", "CASE-20260901-001-vocab-round1"):
            code, payload = run_json("--demo", "study", "show", token)
            self.assertEqual(code, 0, token)
            self.assertEqual(payload["data"]["title"], "词汇第一轮")

    def test_project_status_names_map_to_common_groups(self) -> None:
        code, payload = run_json("--demo", "research", "progress", "--all")
        self.assertEqual(code, 0)
        groups = {item["status"]: item["group"] for item in payload["data"]["items"]}
        self.assertEqual(groups, {"进行中": "active", "待复核": "waiting", "归档": "done"})

    def test_ambiguous_item_lists_the_candidates(self) -> None:
        result = run("--demo", "personal", "show", "CASE-2026")
        self.assertEqual(result.returncode, 1)
        self.assertIn("个事项都匹配", result.stderr)

    def test_usage_errors_exit_2(self) -> None:
        self.assertEqual(run("--demo", "nope").returncode, 2)
        self.assertEqual(run("--demo", "study", "progress", "--status", "bogus").returncode, 2)
        self.assertEqual(run("--demo", "study", "record", "new").returncode, 2)

    def test_outside_a_project_explains_where_to_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, payload = run_json("--project", temporary, "health")
        self.assertEqual(code, 4)
        self.assertEqual(payload["error"]["code"], "not-a-project")
        self.assertTrue(any("--demo" in step for step in payload["error"]["next"]))


class FaultTests(unittest.TestCase):
    def test_incompatible_runtime_exits_3_before_doing_anything(self) -> None:
        code, payload = run_json("--demo", "study", "--demo-fault", "runtime-old", "progress")
        self.assertEqual(code, 3)
        self.assertEqual(payload["error"]["code"], "runtime-incompatible")
        self.assertTrue(payload["error"]["next"])

    def test_broken_index_blocks_find_and_is_a_must_fix(self) -> None:
        code, payload = run_json("--demo", "study", "--demo-fault", "index-broken", "find", "语法")
        self.assertEqual((code, payload["error"]["code"]), (4, "index-broken"))
        code, payload = run_json("--demo", "study", "--demo-fault", "index-broken", "health")
        self.assertEqual(code, 1)
        self.assertEqual(payload["data"]["findings"][0]["level"], "must")
        code, payload = run_json("--demo", "study", "--demo-fault", "index-broken", "progress")
        self.assertEqual(code, 0, "看进展不依赖资料索引")

    def test_offline_marks_network_steps_as_preview_only(self) -> None:
        code, payload = run_json("--demo", "study", "--demo-fault", "offline", "fix", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["results"][0]["status"], "skipped")
        self.assertEqual(payload["data"]["plan"]["update_source"]["status"], "offline")

    def test_concurrent_change_is_refused_with_exit_5(self) -> None:
        code, payload = run_json("--demo", "study", "--demo-fault", "conflict", "record", "语法",
                                 "--log", "完成第 12 课", "--apply")
        self.assertEqual((code, payload["error"]["code"]), (5, "conflict"))


class WriteFlowTests(unittest.TestCase):
    def test_writes_are_previews_until_apply(self) -> None:
        code, payload = run_json("--demo", "personal", "record", "宽带", "--log", "已预约")
        self.assertEqual(code, 0)
        self.assertFalse(payload["data"]["applied"])
        self.assertEqual(payload["data"]["preview"]["expect_updated_at"], "2026-10-24T10:30:00+08:00")

    def test_kept_custom_files_are_not_written_and_preview_matches(self) -> None:
        code, preview = run_json("--demo", "personal", "fix", "template-update")
        self.assertEqual(code, 0)
        planned = [write["path"] for write in preview["data"]["plan"]["steps"][0]["writes"]]
        code, applied = run_json("--demo", "personal", "fix", "template-update", "--apply")
        result = applied["data"]["results"][0]
        self.assertEqual(result["kept"], ["AGENTS.md"], "定制文件默认保留")
        self.assertEqual(set(result["written"]) | set(result["kept"]), set(planned))
        code, applied = run_json("--demo", "personal", "fix", "template-update", "--apply", "--accept", "AGENTS.md")
        self.assertIn("AGENTS.md", applied["data"]["results"][0]["written"])

    def test_project_tool_without_write_interface_is_not_bypassed(self) -> None:
        code, payload = run_json("--demo", "research", "record", "OBS-2026Q4-01", "--log", "x", "--apply")
        self.assertEqual((code, payload["error"]["code"]), (4, "unavailable"))
        self.assertIn("tools/obs.py", payload["error"]["next"][0])

    def test_unavailable_tool_is_not_run(self) -> None:
        code, payload = run_json("--demo", "research", "tools", "sync-db", "--run", "--apply")
        self.assertEqual(code, 4)
        self.assertIn("BIRDDB_TOKEN", payload["error"]["next"][0])
        code, payload = run_json("--demo", "research", "tools", "export-csv")
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["writes"], ["exports/"])


def drive(command: list[str], inputs: list[str], env: dict[str, str] | None = None,
          cwd: str | None = None) -> tuple[int, str]:
    """Run ``command`` in a pseudo-terminal, typing ``inputs`` one line at a time."""
    import pty

    pid, fd = pty.fork()
    if pid == 0:  # child
        if cwd:
            os.chdir(cwd)
        os.execve(command[0], command, env or ENV)
    output = b""

    def drain(seconds: float) -> None:
        nonlocal output
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                output += chunk

    drain(1.5)
    for line in inputs:
        os.write(fd, (line + "\r").encode("utf-8"))
        drain(0.5)
    deadline = time.monotonic() + 10
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done or time.monotonic() > deadline:
            break
        drain(0.1)
    os.close(fd)
    return os.waitstatus_to_exitcode(status) if done else -1, output.decode("utf-8", "replace")


@unittest.skipUnless(sys.platform != "win32", "pseudo-terminals need a POSIX system")
class MenuTests(unittest.TestCase):
    def drive(self, args: list[str], inputs: list[str]) -> tuple[int, str]:
        return drive([sys.executable, str(CLI), *args], inputs)

    def test_find_then_follow_up_actions_then_quit(self) -> None:
        code, text = self.drive(["--demo", "study"], ["1", "语法", "v1", "o1", "", "q"])
        self.assertEqual(code, 0, text)
        self.assertIn("同系列另有 2 个版本", text)
        self.assertIn("grammar-summary-v1.md", text)
        self.assertIn("【演示】将打开", text)

    def test_keyword_on_home_searches_and_broken_index_leads_to_repair(self) -> None:
        code, text = self.drive(["--demo", "study", "--demo-fault", "index-broken"], ["语法", "5", "yes", "", "q"])
        self.assertEqual(code, 0, text)
        self.assertIn("资料索引损坏", text)
        self.assertIn("1–2. 检查并选中：重建资料索引", text)
        self.assertIn("语法总结（第 3 版）", text, "修好后用同一个词再查一次")

    def test_record_needs_explicit_yes(self) -> None:
        code, text = self.drive(["--demo", "personal"], ["6", "宽带", "1", "已预约", "", "q"])
        self.assertEqual(code, 0, text)
        self.assertIn("已取消，没有写入", text)


if __name__ == "__main__":
    unittest.main()
