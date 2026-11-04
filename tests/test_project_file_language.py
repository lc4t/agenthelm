"""Project text templates follow the Profile language; UI locale does not change their structure."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/agents-init/skills/agents-init/scripts/init_project.py"
CJK = re.compile(r"[\u3400-\u9fff]")
BLOCKS = re.compile(r"<!-- agents-init:template (?P<name>[^ ]+) -->\n```[^\n]*\n(?P<body>.*?)```\n<!-- /agents-init:template -->", re.S)


class ProjectFileLanguageTests(unittest.TestCase):
    def test_english_template_pairs_keep_structure_and_tokens(self):
        source = (SCRIPT.parents[1] / "AGENT.template.md").read_text()
        blocks = {match['name']: match['body'] for match in BLOCKS.finditer(source)}
        neutral = {"region/claude-import"}  # one language-neutral import directive
        for name, zh in blocks.items():
            if name.endswith(".en") or name in neutral:
                continue
            with self.subTest(template=name):
                self.assertIn(name + ".en", blocks)
                en = blocks[name + ".en"]
                self.assertFalse(CJK.search(en))
                headings = lambda text: [len(line) - len(line.lstrip('#')) for line in text.splitlines() if line.startswith('#')]
                self.assertEqual(headings(zh), headings(en))
                tokens = lambda text: sorted(re.findall(r"\b(?:PROJECT_[A-Z_]+|ENTRY_COMMAND|PRIVACY_[A-Z_]+|AGENT_CLI|STACK|RUNTIME|VCS)\b", text))
                self.assertEqual(tokens(zh), tokens(en))

    def test_generated_english_markdown_is_english_and_ui_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            outputs = []
            for ui in ("zh", "en"):
                project = base / ui
                project.mkdir()
                home = base / (ui + "-home")
                home.mkdir()
                done = subprocess.run([sys.executable, str(SCRIPT), "--project", str(project), "--name", "Fixture", "--language", "en",
                                       "--project-type", "notes", "--vcs", "none", "--stack", "markdown", "--runtime", "local", "--agent-cli", "claude",
                                       "--no-entry", "--readme-entry", "--output", "json", "--apply"],
                                      capture_output=True, text=True, env={**os.environ, "HOME": str(home), "AGENTHELM_LANG": ui})
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                markdown = {p.relative_to(project).as_posix(): p.read_text() for p in project.rglob('*.md')}
                for path, text in markdown.items():
                    self.assertFalse(CJK.search(text), path)
                self.assertIn('## How to use', markdown['README.md'])
                outputs.append(markdown)
                self.assertEqual(json.loads((project / '.agents/plugin.json').read_text())['description'],
                                 'Portable Agent capabilities local to this project.')
            self.assertEqual(*outputs)

    def test_adoption_appends_english_regions_and_preserves_user_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project"
            project.mkdir()
            original = "# My existing project\n\nKeep my rules.\n"
            (project / "AGENTS.md").write_text(original)
            done = subprocess.run([sys.executable, str(SCRIPT), "--project", str(project), "--name", "Fixture", "--language", "en",
                                   "--mode", "adopt", "--agents-policy", "append", "--recovery-dir", str(base / "backup"),
                                   "--project-type", "notes", "--vcs", "none", "--stack", "markdown", "--runtime", "local", "--agent-cli", "claude",
                                   "--no-entry", "--output", "json", "--apply"], capture_output=True, text=True,
                                  env={**os.environ, "HOME": str(base / "home"), "AGENTHELM_LANG": "zh"})
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            text = (project / "AGENTS.md").read_text()
            self.assertTrue(text.startswith(original))
            self.assertIn("## agenthelm project contract", text)
            self.assertFalse(CJK.search(text))
            self.assertEqual((base / "backup/AGENTS.md").read_text(), original)

    def test_opinion_manager_recognizes_english_placeholder(self):
        path = ROOT / "plugins/opinion-manager/runtime/constants.py"
        spec = importlib.util.spec_from_file_location("opinion_placeholder_constants", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        blocks = {match['name']: match['body'] for match in BLOCKS.finditer((SCRIPT.parents[1] / "AGENT.template.md").read_text())}
        self.assertIn(blocks['OPINION.md.en'], module.KNOWN_PLACEHOLDERS)


if __name__ == '__main__':
    unittest.main()
