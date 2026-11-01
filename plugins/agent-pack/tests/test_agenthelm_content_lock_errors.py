"""内容锁无法解析时，check / update 不能说“已是最新”或“没有需要更新的内容”，也不能静默吞掉用户点名的文件。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
REPO = Path(__file__).resolve().parents[3]
INIT = REPO / "plugins/agents-init/skills/agents-init/scripts/init_project.py"
CLI = RUNTIME / "agenthelm.py"
LATEST = REPO / "latest.json"
LOCK = ".agents/moe.sakanano.agent-pack/content.lock.json"
CJK = re.compile(r"[一-鿿]")


class ContentLockErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.root = self.base / "proj"
        self.root.mkdir()
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", ""), "AGENTHELM_LANG": "zh"}
        done = subprocess.run([sys.executable, str(INIT), "--project", str(self.root), "--name", "T", "--project-type", "notes",
                               "--vcs", "none", "--stack", "markdown", "--runtime", "local", "--agent-cli", "claude",
                               "--output", "json", "--apply"], capture_output=True, text=True, env=self.env, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def run_cli(self, *args: str, lang: str = "zh") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(CLI), *args, "--project", str(self.root), "--latest", str(LATEST)],
                              capture_output=True, text=True, check=False, env={**self.env, "AGENTHELM_LANG": lang}, timeout=120)

    def test_a_broken_lock_is_not_up_to_date_and_strict_fails(self) -> None:
        (self.root / LOCK).write_text("{broken", encoding="utf-8")
        done = self.run_cli("check", "--output", "json", "--strict")
        self.assertEqual(done.returncode, 1, done.stdout)
        summary = json.loads(done.stdout)["summary"]
        self.assertEqual((summary["content_errors"], summary["up_to_date"]), (1, False))

    def test_update_with_a_broken_lock_says_why_exits_non_zero_and_changes_nothing(self) -> None:
        (self.root / LOCK).write_text("{broken", encoding="utf-8")
        mine = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        for lang in ("zh", "en"):
            done = self.run_cli("update", "--only", "content", "--apply", lang=lang)
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn("content.lock.json", done.stdout)
            if lang == "en":
                self.assertFalse(CJK.search(done.stdout), done.stdout)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, mine)

    def test_naming_a_file_when_nothing_is_pending_is_said_out_loud(self) -> None:
        done = self.run_cli("update", "--only", "content", "--apply", "--recovery-dir", str(self.base / "rec"))
        self.assertEqual(done.returncode, 0, done.stdout)
        (self.root / LOCK).unlink()  # 点名文件却没有内容锁：说清楚没处理；有内容锁时点名的文件照常交给 refresh（见 test_agenthelm_content_merge）
        for lang in ("zh", "en"):
            done = self.run_cli("update", "--only", "content", "--diff", "AGENTS.md", lang=lang)
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertIn("fix update-content", done.stdout)
            if lang == "en":
                self.assertFalse(CJK.search(done.stdout), done.stdout)


if __name__ == "__main__":
    unittest.main()
