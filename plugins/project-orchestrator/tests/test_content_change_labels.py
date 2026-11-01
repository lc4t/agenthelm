"""update-content 预览里每一种“状态 → 动作”都有英文译文（init_project 实际会给出的全部取值）。"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import i18n_orchestrator  # noqa: E402
import project_maintain  # noqa: E402

CJK = re.compile(r"[一-鿿]")
STATES = ("unchanged", "clean", "modified", "missing", "local-only", "kept-by-decision")
ACTIONS = ("keep", "replace", "create", "skip", "blocked", "append", "merge")


class ContentChangeLabelTests(unittest.TestCase):
    def test_every_state_and_action_is_translated_in_english(self) -> None:
        previous = os.environ.get("AGENTHELM_LANG")
        os.environ["AGENTHELM_LANG"] = "en"
        self.addCleanup(lambda: os.environ.__setitem__("AGENTHELM_LANG", previous) if previous is not None
                        else os.environ.pop("AGENTHELM_LANG", None))
        for language in ("en", "zh"):
            os.environ["AGENTHELM_LANG"] = language
            for state in STATES:
                for action in ACTIONS:
                    with self.subTest(language=language, state=state, action=action):
                        line = project_maintain._content_change({"path": "AGENTS.md", "state": state, "action": action}, {})
                        if language == "en":
                            self.assertFalse(CJK.search(line), line)
                        else:  # 中文界面里不能出现内部取值（kept-by-decision 这类英文标识）
                            self.assertNotIn(state, line.split("：", 1)[1])
                            self.assertNotIn(f" → {action}", line)
        self.assertEqual(i18n_orchestrator.__name__, "i18n_orchestrator")


if __name__ == "__main__":
    unittest.main()
