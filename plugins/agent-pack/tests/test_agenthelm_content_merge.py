"""rc.3 W2：update --only content 的无基线文件、先 --keep 再 --merge、状态词与 --diff 口径。

夹具：临时 HOME，用当前版本的 init_project.py 建项目，再把内容锁改成“旧版本、AGENTS.md 没有基线”，
模拟早期版本初始化且被用户改过的项目。全程离线（--latest 指向仓库的 latest.json），不碰真实的 ~/.agent-pack。
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

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
REPO = Path(__file__).resolve().parents[3]
INIT = REPO / "plugins/agents-init/skills/agents-init/scripts/init_project.py"
CLI = RUNTIME / "agenthelm.py"
LATEST = REPO / "latest.json"
LOCK = ".agents/moe.sakanano.agent-pack/content.lock.json"
PACK = "agents-init/project-contract"
CJK = re.compile(r"[一-鿿]")


class ContentMergeUpdateTests(unittest.TestCase):
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

    def old_lock_without_baseline(self) -> str:
        """锁停在旧版本且没有 AGENTS.md 的基线；用户在 AGENTS.md 里加了自己的内容。返回改后的 AGENTS.md。"""
        lock = json.loads((self.root / LOCK).read_text(encoding="utf-8"))
        entry = lock["packs"][PACK]
        entry["version"] = "1.0.0"
        entry["files"].pop("AGENTS.md")
        entry.pop("file_versions", None)
        (self.root / LOCK).write_text(json.dumps(lock), encoding="utf-8")
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8") + "\n我的补充\n"
        (self.root / "AGENTS.md").write_text(text, encoding="utf-8")
        return text

    def cli(self, *args: str, lang: str = "zh") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(CLI), *args, "--project", str(self.root), "--latest", str(LATEST)],
                              capture_output=True, text=True, check=False, env={**self.env, "AGENTHELM_LANG": lang}, timeout=120)

    def tree(self) -> dict[Path, bytes]:
        return {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    # ---- A7
    def test_merge_without_a_baseline_previews_a_reason_instead_of_nothing_to_update(self) -> None:
        mine = self.old_lock_without_baseline()
        before = self.tree()
        done = self.cli("update", "--only", "content", "--merge", "AGENTS.md")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)  # 预览不是失败
        self.assertNotIn("没有需要更新的内容", done.stdout)
        self.assertIn("基线摘要，无法三方合并", done.stdout)
        self.assertIn("--keep AGENTS.md", done.stdout)
        self.assertEqual(done.stdout.count("无法三方合并"), 1, done.stdout)
        self.assertEqual(self.tree(), before)
        self.assertEqual((self.root / "AGENTS.md").read_text(encoding="utf-8"), mine)

    def test_keep_then_merge_is_still_handed_to_refresh_after_the_lock_moved_to_the_new_version(self) -> None:
        mine = self.old_lock_without_baseline()
        kept = self.cli("update", "--only", "content", "--keep", "AGENTS.md", "--apply", "--recovery-dir", str(self.base / "rec"))
        self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
        self.assertNotIn("建议先保留", kept.stdout, "已经 --keep 了")
        lock = json.loads((self.root / LOCK).read_text(encoding="utf-8"))
        self.assertEqual(lock["packs"][PACK]["version"], json.loads(LATEST.read_text())["plugins"]["agents-init"]["content"][PACK]["version"])
        before = self.tree()
        for flags in (("--merge", "AGENTS.md"), ("--diff", "AGENTS.md")):
            done = self.cli("update", "--only", "content", *flags)
            self.assertNotIn("没有需要更新的内容", done.stdout, done.stdout)
            self.assertNotIn("没有被处理", done.stdout)
        merge = self.cli("update", "--only", "content", "--merge", "AGENTS.md")
        self.assertEqual(merge.returncode, 0, merge.stdout)
        self.assertIn("无法三方合并", merge.stdout)
        self.assertIn("阻塞", merge.stdout)
        self.assertEqual(self.tree(), before)
        self.assertEqual((self.root / "AGENTS.md").read_text(encoding="utf-8"), mine)

    def test_append_then_merge_is_also_handed_to_refresh(self) -> None:
        self.old_lock_without_baseline()
        done = self.cli("update", "--only", "content", "--append", "AGENTS.md", "--apply", "--recovery-dir", str(self.base / "rec"))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        merge = self.cli("update", "--only", "content", "--merge", "AGENTS.md")
        self.assertNotIn("没有需要更新的内容", merge.stdout)
        self.assertIn("AGENTS.md", merge.stdout)

    def test_naming_a_file_in_a_project_without_a_lock_still_explains_why_nothing_ran(self) -> None:
        (self.root / LOCK).unlink()
        before = self.tree()
        for lang in ("zh", "en"):
            done = self.cli("update", "--only", "content", "--merge", "AGENTS.md", lang=lang)
            self.assertEqual(done.returncode, 0, done.stdout)
            self.assertIn("fix update-content", done.stdout)
            if lang == "en":
                self.assertFalse(CJK.search(done.stdout), done.stdout)
        self.assertEqual(self.tree(), before)

    # ---- A6
    def test_status_words_follow_the_interface_language(self) -> None:
        self.old_lock_without_baseline()
        zh = self.cli("update", "--only", "content", "--merge", "AGENTS.md")
        for raw in ("blocked", "[modified]", "[unchanged]"):
            self.assertNotIn(raw, zh.stdout)
        self.assertRegex(zh.stdout, r"阻塞\s+AGENTS\.md \[你改过\]")
        self.assertRegex(zh.stdout, r"保持原样\s+CLAUDE\.md \[与新模板相同\]")
        en = self.cli("update", "--only", "content", "--merge", "AGENTS.md", lang="en")
        self.assertRegex(en.stdout, r"blocked\s+AGENTS\.md \[edited by you\]")
        self.assertRegex(en.stdout, r"keep\s+CLAUDE\.md \[same as template\]")
        self.assertFalse(CJK.search(en.stdout), en.stdout)
        raw = self.cli("update", "--only", "content", "--merge", "AGENTS.md", "--output", "json")
        item = next(file for step in json.loads(raw.stdout)["steps"] for file in (step.get("result") or {}).get("files", [])
                    if file["path"] == "AGENTS.md")
        self.assertEqual((item["state"], item["action"], item["reason"]), ("modified", "blocked", "no-baseline"))

    def test_diff_prints_only_the_difference(self) -> None:
        self.old_lock_without_baseline()
        for lang in ("zh", "en"):
            done = self.cli("update", "--only", "content", "--diff", "AGENTS.md", lang=lang)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("--- a/AGENTS.md", done.stdout)
            self.assertIn("-我的补充", done.stdout)
            for other in ("AGENT.RULES.md", "docs/refs/README.md", "[与新模板相同]", "[same as template]", "--keep AGENTS.md"):
                self.assertNotIn(other, done.stdout)

    def test_suggestions_name_the_file(self) -> None:
        self.old_lock_without_baseline()
        done = self.cli("update", "--only", "content")
        joined = done.stdout
        self.assertIn("--keep AGENTS.md", joined)
        self.assertNotIn("<文件>", joined)
        self.assertNotRegex(joined, r"--(keep|append|accept|diff|merge) (?![\w./]*\.md)")


if __name__ == "__main__":
    unittest.main()
