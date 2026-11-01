"""English wording must not depend on a count being 1 or many: no "(s)" / "(ies)" / "(es)" guesses."""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOGS = [
    "plugins/agent-pack/runtime/locale/en.json",
    "plugins/agents-init/skills/agents-init/scripts/locale/en.json",
    "plugins/project-orchestrator/runtime/locale/en.json",
    "plugins/opinion-manager/runtime/locale/en.json",
]
# "entry(ies)", "file(s)", "match(es)" - a letter run directly followed by a bracketed plural suffix.
HEDGED_PLURAL = re.compile(r"[A-Za-z]\((?:s|ies|es)\)")
# Counted nouns that read wrong at 1 ("1 subdirectories") in templates whose count can be 1.
COUNT_THEN_PLURAL = re.compile(r"\{\d\} (?:subdirectories|entries|sections|skeleton paths)\b")


class PluralWordingTests(unittest.TestCase):
    def test_catalogs_avoid_hedged_plurals(self) -> None:
        for relative in CATALOGS:
            for source, text in json.loads((ROOT / relative).read_text(encoding="utf-8")).items():
                with self.subTest(catalog=relative, source=source):
                    self.assertIsNone(HEDGED_PLURAL.search(text), text)
                    self.assertIsNone(COUNT_THEN_PLURAL.search(text), text)

    def test_nudge_status_text_avoids_hedged_plurals(self) -> None:
        text = (ROOT / "plugins/agent-pack/runtime/nudge_text.py").read_text(encoding="utf-8")
        self.assertIsNone(HEDGED_PLURAL.search(text))


if __name__ == "__main__":
    unittest.main()
