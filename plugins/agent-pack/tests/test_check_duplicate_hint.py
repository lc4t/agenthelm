"""`check` points at inventory / dedupe / relocate when there are duplicate groups to sort out, and only then."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import agenthelm  # noqa: E402


def skill(root: Path, name: str, text: str = "x") -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {text}\n---\nbody\n", encoding="utf-8")


class CheckDuplicateHintTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.home.mkdir()
        self.project.mkdir()
        (self.home / ".claude").mkdir()
        self.latest = self.root / "latest.json"
        self.latest.write_text(json.dumps({"schema": 1, "marketplace": "agenthelm", "migrations": [], "plugins": {}}),
                               encoding="utf-8")
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def check(self, lang: str, *flags: str) -> tuple[int, str]:
        os.environ["AGENTHELM_LANG"] = lang
        output = io.StringIO()
        with redirect_stdout(output):
            code = agenthelm.main(["check", "--home", str(self.home), "--latest", str(self.latest), "--client", "claude",
                                   "--project", str(self.project), *flags])
        return code, output.getvalue()

    def duplicate(self) -> None:
        skill(self.home / ".claude/skills", "tidy-me", "user copy")
        skill(self.project / ".claude/skills", "tidy-me", "project copy")

    def test_no_duplicates_no_line_and_no_key(self) -> None:
        skill(self.home / ".claude/skills", "only-one")
        for lang in ("zh", "en"):
            code, text = self.check(lang)
            self.assertNotIn("inventory", text)
        _code, payload = self.check("en", "--output", "json")
        self.assertNotIn("inventory_hint", json.loads(payload))

    def test_duplicate_group_adds_one_line_in_both_languages(self) -> None:
        self.duplicate()
        code, text = self.check("zh")
        self.assertIn("[整理] 有 1 组重复的 Skill / MCP", text)
        self.assertRegex(text, r"inventory 查看，.* dedupe 处置，.* relocate 迁移")
        code_en, text_en = self.check("en")
        self.assertIn("[Tidy] Duplicate Skill / MCP groups: 1. See", text_en)
        self.assertIn("inventory, resolve with", text_en)
        self.assertEqual(code, code_en)

    def test_exit_code_and_existing_json_keys_do_not_change(self) -> None:
        clean_code, clean = self.check("en", "--output", "json")
        self.duplicate()
        code, payload = self.check("en", "--output", "json")
        report = json.loads(payload)
        self.assertEqual(code, clean_code)
        self.assertEqual(report["inventory_hint"], {"duplicate_groups": 1})
        self.assertEqual(set(report) - {"inventory_hint"}, set(json.loads(clean)))
        self.assertEqual(report["summary"], json.loads(clean)["summary"])

    def test_a_recorded_decision_silences_the_line_until_the_group_changes(self) -> None:
        self.duplicate()
        import agent_pack_dedupe as dedupe
        import agent_pack_inventory as inventory
        group = dedupe.list_groups(inventory.observe(self.home, self.project, ["claude"], "all", "all"))[0]
        dedupe.decisions_path(self.home).parent.mkdir(parents=True, exist_ok=True)
        dedupe.decisions_path(self.home).write_text(json.dumps({"schema_version": dedupe.DECISIONS_SCHEMA, "decisions": {
            group["id"]: {"decision": "ignore", "members_digest": dedupe.members_digest(group)}}}), encoding="utf-8")
        self.assertNotIn("inventory", self.check("en")[1])
        # The group changed since the decision (its recorded member digest no longer matches): it is open again.
        dedupe.decisions_path(self.home).write_text(json.dumps({"schema_version": dedupe.DECISIONS_SCHEMA, "decisions": {
            group["id"]: {"decision": "ignore", "members_digest": "stale"}}}), encoding="utf-8")
        self.assertIn("inventory", self.check("en")[1])

    def test_the_session_hook_stays_silent_about_it(self) -> None:
        self.duplicate()
        report = agenthelm.run_check(agenthelm.current_env(self.home), self.project, latest_source=str(self.latest),
                                     hook_mode=True, migrate_hint=True, clients=["claude"])
        self.assertNotIn("inventory_hint", report)


if __name__ == "__main__":
    unittest.main()
