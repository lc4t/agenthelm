"""#78：接管项目（内容锁登记了受管区块）的 AGENTS.md 被删除后，update --only content 不能说「没有需要更新的内容」。

全程临时 HOME 与离线 latest.json；用 init_project 的 adopt 模式建出登记了区块的项目。
"""
from __future__ import annotations

import json
import os
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


class MissingRegionFileTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        (base / "home").mkdir()
        self.root = base / "proj"
        self.root.mkdir()
        self.env = {"HOME": str(base / "home"), "PATH": os.environ.get("PATH", ""), "AGENTHELM_LANG": "zh"}
        (self.root / "AGENTS.md").write_text("# 我的项目\n\n接管前的规则。\n", encoding="utf-8")
        done = subprocess.run([sys.executable, str(INIT), "--project", str(self.root), "--mode", "adopt", "--name", "T",
                               "--project-type", "notes", "--vcs", "none", "--stack", "markdown", "--runtime", "local",
                               "--agent-cli", "claude", "--agents-policy", "append", "--output", "json", "--apply",
                               "--recovery-dir", str(base / "rec")], capture_output=True, text=True, env=self.env, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def cli(self, *args: str, lang: str = "zh") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(CLI), *args, "--project", str(self.root), "--latest", str(LATEST)],
                              capture_output=True, text=True, check=False, env={**self.env, "AGENTHELM_LANG": lang}, timeout=120)

    def test_missing_region_owned_file_gets_a_hint_instead_of_nothing_to_update(self) -> None:
        untouched = self.cli("update", "--only", "content")
        self.assertIn("没有需要更新的内容", untouched.stdout)  # 文件在时行为不变
        (self.root / "AGENTS.md").unlink()
        for lang, needle in (("zh", "项目里缺少 AGENTS.md"), ("en", "AGENTS.md is missing")):
            done = self.cli("update", "--only", "content", "--output", "json", lang=lang)
            message = json.loads(done.stdout)["message"]
            self.assertIn(needle, message)
            self.assertIn("--accept AGENTS.md", message)
            self.assertNotIn("没有需要更新的内容", message)
            self.assertNotIn("Nothing to update", message)
        self.assertFalse((self.root / "AGENTS.md").exists())  # 只提示，不替用户重建

    def test_the_suggested_command_restores_the_regions(self) -> None:
        (self.root / "AGENTS.md").unlink()
        done = self.cli("update", "--only", "content", "--accept", "AGENTS.md", "--apply")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("agenthelm:region agents-contract", (self.root / "AGENTS.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
