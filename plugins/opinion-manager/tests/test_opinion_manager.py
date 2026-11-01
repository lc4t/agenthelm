from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import json
import subprocess
import sys
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "runtime" / "opinion_manager.py"
TEST_ROOT = PLUGIN_ROOT.parents[1] / "__pycache__" / "opinion-test-work"
sys.path.insert(0, str(SCRIPT.parent))
import opinion_manager  # noqa: E402


class OpinionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
    def run_command(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *(str(argument) for argument in arguments)],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_explicit_empty_catalog_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            result = self.run_command("catalog", "--catalog", directory, "--output", "json")
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["templates"], [])
            self.assertEqual(payload["rules"], [])

    def test_unapproved_template_cannot_be_selected(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            project = Path(directory)
            result = self.run_command(
                "compose",
                "--project", project,
                "--template", "unapproved-template@1.0.0",
                "--output", "json",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("未知模板", json.loads(result.stdout)["error"])
            self.assertFalse((project / "OPINION.md").exists())

    def test_template_rules_with_identical_ids_are_deduplicated(self) -> None:
        shared_rule = {
            "id": "shared.rule-alpha",
            "title": "RULE_ALPHA",
            "level": "required",
            "scopes": ["SCOPE_ALPHA"],
            "text": "TEMPLATE_RULE_ALPHA",
        }
        catalog = OrderedDict({
            "template-alpha": {
                "id": "template-alpha",
                "version": "1.0.0",
                "name": "TEMPLATE_ALPHA",
                "description": "DESCRIPTION_ALPHA",
                "scopes": ["SCOPE_ALPHA"],
                "rules": [shared_rule],
                "questions": [],
            },
            "template-beta": {
                "id": "template-beta",
                "version": "1.0.0",
                "name": "TEMPLATE_BETA",
                "description": "DESCRIPTION_BETA",
                "scopes": ["SCOPE_BETA"],
                "rules": [dict(shared_rule)],
                "questions": [],
            },
        })
        selected, sources = opinion_manager.select_rules(
            catalog,
            ["template-alpha@1.0.0", "template-beta@1.0.0"],
            [],
        )
        self.assertEqual(sources, ["template-alpha@1.0.0", "template-beta@1.0.0"])
        self.assertEqual(sum(len(rules) for rules in selected.values()), 1)

    def test_custom_generation_replaces_only_known_placeholder(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            project = Path(directory)
            opinion = project / "OPINION.md"
            opinion.write_text("# 项目 Opinion 覆盖层\n\n尚未配置 Opinion。\n", encoding="utf-8")
            custom = project / "custom.md"
            custom.write_text("CUSTOM_RULE_ALPHA\n", encoding="utf-8")
            result = self.run_command(
                "compose",
                "--project", project,
                "--custom-file", custom,
                "--output", "json",
                "--apply",
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            content = opinion.read_text(encoding="utf-8")
            self.assertIn("## 用户自定义规则", content)
            self.assertIn("CUSTOM_RULE_ALPHA", content)

    def test_existing_opinion_requires_explicit_replace(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            project = Path(directory)
            opinion = project / "OPINION.md"
            opinion.write_text("EXISTING_CONTENT\n", encoding="utf-8")
            custom = project / "custom.md"
            custom.write_text("CUSTOM_RULE_BETA\n", encoding="utf-8")
            result = self.run_command(
                "compose",
                "--project", project,
                "--custom-file", custom,
                "--output", "json",
                "--apply",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("--replace", json.loads(result.stdout)["error"])
            self.assertEqual(opinion.read_text(encoding="utf-8"), "EXISTING_CONTENT\n")

    def test_edited_placeholder_requires_explicit_replace(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            project = Path(directory)
            opinion = project / "OPINION.md"
            existing = "# 项目 Opinion 覆盖层\n\n尚未配置 Opinion。\n\n用户补写的要求。\n"
            opinion.write_text(existing, encoding="utf-8")
            custom = project / "custom.md"
            custom.write_text("CUSTOM_RULE_GAMMA\n", encoding="utf-8")
            result = self.run_command(
                "compose", "--project", project, "--custom-file", custom,
                "--output", "json", "--apply",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("--replace", json.loads(result.stdout)["error"])
            self.assertEqual(opinion.read_text(encoding="utf-8"), existing)


if __name__ == "__main__":
    unittest.main()
