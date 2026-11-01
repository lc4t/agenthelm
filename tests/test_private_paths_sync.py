"""agents-init 与 project-orchestrator 各存一份私有目录的 .gitignore 文本，必须一致。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/project-orchestrator/runtime"))
sys.path.insert(0, str(ROOT / "plugins/agents-init/skills/agents-init/scripts"))
import init_project  # noqa: E402
import private_paths  # noqa: E402


class PrivateIgnoreTextTests(unittest.TestCase):
    def test_both_plugins_agree_on_the_gitignore_text(self) -> None:
        self.assertEqual(init_project.PRIVATE_IGNORE, private_paths.IGNORE_TEXT)

    def test_new_projects_get_it_at_the_declared_path(self) -> None:
        self.assertEqual(init_project.PRIVATE_IGNORE.splitlines()[1:], ["*", "!.gitignore"])


if __name__ == "__main__":
    unittest.main()
