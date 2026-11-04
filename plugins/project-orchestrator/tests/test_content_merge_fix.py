"""rc.3 W2：fix update-content 的 --diff 只输出差异、--merge 取不到基线时说明原因、建议命令带文件名、状态词随语言。

夹具：临时 HOME + 当前版本 init_project.py 建项目，再把内容锁改成“旧版本、AGENTS.md 没有基线”。离线运行，
不碰真实的 ~/.agent-pack。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
INIT = REPO / "plugins/agents-init/skills/agents-init/scripts/init_project.py"
CLI = REPO / "plugins/agent-pack/runtime/agenthelm.py"
LOCK = ".agents/moe.sakanano.agent-pack/content.lock.json"
PACK = "agents-init/project-contract"
CJK = re.compile(r"[一-鿿]")


class FixUpdateContentTests(unittest.TestCase):
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
        lock = json.loads((self.root / LOCK).read_text(encoding="utf-8"))
        entry = lock["packs"][PACK]
        entry["version"] = "1.0.0"
        entry["files"].pop("AGENTS.md")
        (self.root / LOCK).write_text(json.dumps(lock), encoding="utf-8")
        (self.root / "AGENTS.md").write_text((self.root / "AGENTS.md").read_text(encoding="utf-8") + "\n我的补充\n", encoding="utf-8")

    def fix(self, *args: str, lang: str = "zh") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(CLI), "fix", "update-content", *args, "--project", str(self.root), "--offline"],
                              capture_output=True, text=True, check=False, env={**self.env, "AGENTHELM_LANG": lang}, timeout=120)

    def tree(self) -> dict[Path, bytes]:
        return {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def test_diff_prints_only_the_difference(self) -> None:
        before = self.tree()
        for lang in ("zh", "en"):
            done = self.fix("--diff", "AGENTS.md", lang=lang)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("--- a/AGENTS.md", done.stdout)
            self.assertIn("-我的补充", done.stdout)
            for other in ("AGENT.RULES.md", "content.lock.json", "--apply"):
                self.assertNotIn(other, done.stdout)
        self.assertEqual(self.tree(), before)

    def test_merge_without_a_baseline_says_why_in_the_preview_and_keeps_the_file(self) -> None:
        before = self.tree()
        for lang, words in (("zh", "内容锁里没有它的基线摘要，无法合并"), ("en", "no baseline digest for it, so it cannot be merged")):
            done = self.fix("--merge", "AGENTS.md", lang=lang)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn(words, done.stdout)
        self.assertEqual(self.tree(), before)

    def test_keep_then_merge_gives_the_same_reason_instead_of_nothing_to_update(self) -> None:
        kept = self.fix("--keep", "AGENTS.md", "--apply")
        self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
        before = self.tree()
        for lang, reason, state in (("zh", "无法合并", "你已决定保留"),
                                    ("en", "no baseline digest for it, so it cannot be merged", "you decided to keep it")):
            with self.subTest(lang=lang):
                done = self.fix("--merge", "AGENTS.md", lang=lang)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn(reason, done.stdout)
                self.assertIn(state, done.stdout)
                self.assertNotIn("没有需要更新", done.stdout)
                self.assertNotIn("No template needs updating", done.stdout)
                self.assertNotIn("定制文件：AGENTS.md", done.stdout)
                self.assertNotIn("Customized file: AGENTS.md", done.stdout)
                if lang == "en":
                    self.assertFalse(CJK.search(done.stdout), done.stdout)
                raw = self.fix("--merge", "AGENTS.md", "--output", "json", lang=lang)
                self.assertEqual(raw.returncode, 0, raw.stdout + raw.stderr)
                plan = json.loads(raw.stdout)["data"]["plan"]["steps"][0]
                self.assertFalse(plan["blocked"])
                item = next(row for row in plan["payload"]["files"] if row["path"] == "AGENTS.md")
                self.assertEqual((item["state"], item["action"], item["reason"]),
                                 ("kept-by-decision", "blocked", "no-baseline"))
                self.assertTrue(item["merge_requested"])
                self.assertNotIn("AGENTS.md", [row["path"] for row in plan["custom"]])
                self.assertEqual(self.tree(), before)

    def test_kept_file_still_has_a_diff_when_named_explicitly(self) -> None:
        kept = self.fix("--keep", "AGENTS.md", "--apply")
        self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
        before = self.tree()
        for lang in ("zh", "en"):
            done = self.fix("--diff", "AGENTS.md", lang=lang)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("--- a/AGENTS.md", done.stdout)
            self.assertIn("-我的补充", done.stdout)
            self.assertNotIn("No template needs updating", done.stdout)
        self.assertEqual(self.tree(), before)

    def test_plain_preview_still_suppresses_files_kept_by_decision(self) -> None:
        kept = self.fix("--keep", "AGENTS.md", "--apply")
        self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
        before = self.tree()
        for lang, words in (("zh", "没有需要更新的模板"), ("en", "No template needs updating")):
            done = self.fix(lang=lang)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn(words, done.stdout)
        self.assertEqual(self.tree(), before)

    def test_suggestions_name_the_file_and_states_follow_the_language(self) -> None:
        (self.root / "CLAUDE.md").unlink()
        (self.root / "CLAUDE.md").symlink_to("AGENTS.md")
        zh = self.fix()
        self.assertEqual(zh.returncode, 0, zh.stdout + zh.stderr)
        self.assertIn("--keep AGENTS.md", zh.stdout)
        self.assertNotIn("<文件>", zh.stdout)
        self.assertNotRegex(zh.stdout, r"--(keep|accept|merge|diff|append) (?![\w./]*\.md)")
        self.assertIn("CLAUDE.md：符号链接", zh.stdout)
        for raw in ("symlink", "blocked", "modified"):
            self.assertNotIn(raw, zh.stdout)
        en = self.fix(lang="en")
        self.assertIn("CLAUDE.md: symlink", en.stdout)


if __name__ == "__main__":
    unittest.main()
