"""update-content 预览里“默认”标记与动作之间的拼接：英文界面不能出现 defaultkeep 这类紧贴的词。"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import project_cli  # noqa: E402

GLUED = re.compile(r"default[a-z]")


def step(*items: dict) -> dict:
    return {"title": "t", "breaking": False, "changes": [], "writes": [], "custom": list(items), "network": False,
            "recovery": "r", "blocked": None}


# One item per branch of render_plan: generic file (keep / append / accept with lost lines), README, and `options` files.
ITEMS = (
    {"path": "AGENTS.md", "state": "modified", "default": "keep"},
    {"path": "AGENTS.md", "state": "modified", "default": "append"},
    {"path": "AGENTS.md", "state": "modified", "default": "accept"},
    {"path": "AGENTS.md", "state": "modified", "default": "accept", "lost": 4},
    {"path": "README.md", "state": "modified", "default": "keep"},
    {"path": "README.md", "state": "modified", "default": "append"},
    {"path": "x.md", "state": "modified", "default": "accept", "options": ["accept", "skip", "later"]},
    {"path": "x.md", "state": "modified", "default": "skip", "options": ["accept", "skip", "later"]},
    {"path": "x.md", "state": "modified", "default": "later", "options": ["accept", "skip", "later"]},
)


class DefaultLabelTests(unittest.TestCase):
    def render(self, language: str, item: dict, chosen: dict | None = None) -> str:
        previous = os.environ.get("AGENTHELM_LANG")
        os.environ["AGENTHELM_LANG"] = language
        self.addCleanup(lambda: os.environ.__setitem__("AGENTHELM_LANG", previous) if previous is not None
                        else os.environ.pop("AGENTHELM_LANG", None))
        lines = project_cli.render_plan({"steps": [step(item)]}, chosen)
        return next(line for line in lines if item["path"] in line and "→" in line)

    def test_english_default_label_is_separated_from_every_action(self) -> None:
        for item in ITEMS:
            with self.subTest(item=item):
                line = self.render("en", item)
                self.assertIn("→ default: ", line)
                self.assertIsNone(GLUED.search(line), line)
                self.assertFalse(re.search(r"[一-鿿]", line), line)

    def test_chosen_decision_carries_no_default_marker(self) -> None:
        for language in ("en", "zh"):
            for item in ITEMS:
                with self.subTest(language=language, item=item):
                    line = self.render(language, item, {item["path"]: item["default"]})
                    self.assertNotIn("default", line.lower())
                    self.assertNotIn("默认", line)

    def test_chinese_wording_is_unchanged(self) -> None:
        self.assertTrue(self.render("zh", ITEMS[0]).endswith("→ 默认保留你的版本"))
        self.assertTrue(self.render("zh", ITEMS[3]).endswith("→ 默认采用新模板（丢弃 4 行本地内容）"))
        self.assertTrue(self.render("zh", ITEMS[4]).endswith("→ 默认不改"))
        self.assertTrue(self.render("zh", ITEMS[6]).endswith("→ 默认采用（按模板处理）"))


if __name__ == "__main__":
    unittest.main()
