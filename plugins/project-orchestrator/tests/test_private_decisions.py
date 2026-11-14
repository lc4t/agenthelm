"""#102：health 的 private-tracked / private-ignore 发现可以 fix --skip 登记，被跟踪的文件变了会重新提示。

所有名称与路径都是虚构的通用示例。
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # 默认断言中文；英文用例单独给子进程设 AGENTHELM_LANG=en

import json
import subprocess
import sys
import unittest
from pathlib import Path

from test_project_real import AGENTHELM
from test_project_scaffold import DECISIONS, ScaffoldCase

TRACKED = "private-tracked:private"
IGNORE = "private-ignore:private"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


class PrivateDecisionCase(ScaffoldCase):
    def private_project(self) -> Path:
        root = self.make_project("sample-notes", "notes", "--no-entry")
        private = root / "private"
        private.mkdir(exist_ok=True)
        (private / ".gitignore").write_text("*\n!.gitignore\n!README.md\n", encoding="utf-8")
        (private / "README.md").write_text("说明这个目录的用途\n", encoding="utf-8")
        git(root, "init", "-q")
        git(root, "add", "-f", "private/.gitignore", "private/README.md")
        git(root, "commit", "-q", "-m", "init")
        return root

    def finding(self, payload: dict, identifier: str) -> dict | None:
        return next((f for f in payload["data"]["findings"] if f["id"] == identifier), None)

    def test_tracked_finding_can_be_skipped_reprompted_and_unskipped(self) -> None:
        root = self.private_project()
        _, payload = self.health(root)
        found = self.finding(payload, TRACKED)
        self.assertIsNotNone(found)
        self.assertEqual(found["decision"]["id"], TRACKED)
        self.assertEqual(found["decision"]["scope"], "project")
        code, preview = self.fix(root, "--skip", TRACKED, "--reason", "README 有意入库")
        self.assertEqual((code, preview["data"]["applied"]), (0, False))
        self.assertFalse((root / DECISIONS).exists())
        code, applied = self.fix(root, "--skip", TRACKED, "--reason", "README 有意入库", "--apply")
        self.assertEqual((code, applied["data"]["applied"]), (0, True))
        saved = json.loads((root / DECISIONS).read_text(encoding="utf-8"))["decisions"][TRACKED]
        self.assertEqual(saved["reason"], "README 有意入库")
        _, payload = self.health(root)
        self.assertNotIn(TRACKED, self.ids(payload))
        _, payload = self.health(root, "--show-skipped")
        self.assertEqual([item["id"] for item in payload["data"]["skipped"]], [TRACKED])
        _, payload = self.health(root, "--reprompt")
        self.assertIn(TRACKED, self.ids(payload))
        # 再跟踪一个新文件：文件列表变了，重新提示，并标注期望内容已变化
        (root / "private/extra.md").write_text("x\n", encoding="utf-8")
        git(root, "add", "-f", "private/extra.md")
        _, payload = self.health(root)
        again = self.finding(payload, TRACKED)
        self.assertIsNotNone(again)
        self.assertIn("期望内容已变化", again["title"])
        self.assertIn("2 个文件", again["title"])
        # 另一台机器上同样生效（决定在项目里）；--unskip 恢复
        git(root, "rm", "-q", "--cached", "private/extra.md")
        _, payload = self.health(root, env=self.other_machine())
        self.assertNotIn(TRACKED, self.ids(payload))
        code, _unused = self.fix(root, "--unskip", TRACKED, "--apply")
        self.assertEqual(code, 0)
        _, payload = self.health(root)
        self.assertIn(TRACKED, self.ids(payload))
        self.assertEqual(payload["data"]["skipped"], [])

    def test_ignore_finding_can_be_skipped(self) -> None:
        root = self.make_project("sample-notes", "notes", "--no-entry")
        (root / "private").mkdir(exist_ok=True)
        (root / "private/.gitignore").unlink(missing_ok=True)
        git(root, "init", "-q")
        _, payload = self.health(root)
        found = self.finding(payload, IGNORE)
        self.assertIsNotNone(found)
        self.assertEqual(found["decision"]["scope"], "project")
        code, _unused = self.fix(root, "--skip", IGNORE, "--reason", "有意不忽略", "--apply")
        self.assertEqual(code, 0)
        _, payload = self.health(root)
        self.assertNotIn(IGNORE, self.ids(payload))
        code, _unused = self.fix(root, "--unskip", IGNORE, "--apply")
        self.assertEqual(code, 0)
        _, payload = self.health(root)
        self.assertIn(IGNORE, self.ids(payload))

    def test_finding_without_decision_says_it_cannot_be_skipped(self) -> None:
        root = self.make_project("sample-notes", "notes", "--no-entry")
        self.edit_profile(root, lambda p: p.pop("kind", None))
        _, payload = self.health(root)
        plain = next((f for f in payload["data"]["findings"] if not f.get("decision")), None)
        if plain is None:
            self.skipTest("fixture has no finding without a decision")
        code, result = self.fix(root, "--skip", plain["id"])
        self.assertEqual(code, 2)
        text = json.dumps(result, ensure_ascii=False)
        self.assertIn("不支持跳过", text)
        self.assertIn("可跳过", text)
        code, result = self.fix(root, "--skip", "no-such-finding")
        self.assertEqual(code, 2)
        self.assertIn("现在没有这个发现", json.dumps(result, ensure_ascii=False))

    def test_english_messages(self) -> None:
        root = self.private_project()
        env = {**self.env, "AGENTHELM_LANG": "en"}
        _, payload = self.health(root, env=env)
        self.assertIn(TRACKED, self.ids(payload))
        self.fix(root, "--skip", TRACKED, "--reason", "intended", "--apply", env=env)
        (root / "private/extra.md").write_text("x\n", encoding="utf-8")
        git(root, "add", "-f", "private/extra.md")
        _, payload = self.health(root, env=env)
        self.assertIn("expected content changed", self.finding(payload, TRACKED)["title"])
        code, result = self.fix(root, "--skip", "no-such-finding", env=env)
        self.assertEqual(code, 2)
        self.assertIn("No such finding", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
