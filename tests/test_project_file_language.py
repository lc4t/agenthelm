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


TOKENS = re.compile(r"\b(?:PROJECT_[A-Z_]+|ENTRY_COMMAND|PRIVACY_[A-Z_]+|AGENT_CLI|STACK|RUNTIME|VCS)\b")


def template_blocks(source: str) -> dict[str, str]:
    return {match['name']: match['body'] for match in BLOCKS.finditer(source)}


def line_kinds(text: str) -> list[str]:
    """Non-blank line kinds: heading level, ordered/bullet item with indent, code fence, plain paragraph line."""
    kinds = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped:
            continue
        indent = len(line) - len(stripped)
        if stripped.startswith('```'):
            kinds.append('fence')
        elif stripped.startswith('#'):
            kinds.append('h%d' % (len(stripped) - len(stripped.lstrip('#'))))
        elif re.match(r'\d+\. ', stripped):
            kinds.append('ol%d' % indent)
        elif re.match(r'[-*] ', stripped):
            kinds.append('ul%d' % indent)
        else:
            kinds.append('p')
    return kinds


def code_spans(text: str) -> list[str]:
    return sorted(re.sub(r'<[^>]*>', '<>', span) for span in re.findall(r'`([^`\n]+)`', text))


def link_targets(text: str) -> list[str]:
    return sorted(re.findall(r'\]\(([^)]+)\)', text))


def equivalence_problems(zh: str, en: str) -> list[str]:
    """Differences between a Chinese template block and its English counterpart (empty when equivalent)."""
    problems = []
    if CJK.search(en):
        problems.append('English block contains Chinese characters')
    for label, extract in (('line kinds', line_kinds), ('code spans', code_spans), ('link targets', link_targets),
                           ('placeholder tokens', lambda text: sorted(TOKENS.findall(text)))):
        if extract(zh) != extract(en):
            problems.append(label + ' differ')
    return problems


class ProjectFileLanguageTests(unittest.TestCase):
    def test_english_template_pairs_keep_structure_and_tokens(self):
        blocks = template_blocks((SCRIPT.parents[1] / "AGENT.template.md").read_text())
        neutral = {"region/claude-import"}  # one language-neutral import directive
        checked = 0
        for name, zh in blocks.items():
            if name.endswith(".en") or name in neutral:
                continue
            with self.subTest(template=name):
                self.assertIn(name + ".en", blocks)
                self.assertEqual(equivalence_problems(zh, blocks[name + ".en"]), [])
                checked += 1
        self.assertGreater(checked, 10)

    def test_equivalence_check_rejects_known_mutations(self):
        """Each edit below slipped past the earlier heading-level + token comparison; all must now be reported.

        Not detectable mechanically: renaming a heading or dropping one sentence inside a line (the sentence count per line
        legitimately differs in 7 Chinese/English template pairs). Those need the human review the i18n policy asks for.
        """
        source = (SCRIPT.parents[1] / "AGENT.template.md").read_text()
        blocks = template_blocks(source)
        zh, en = blocks["AGENTS.md"], blocks["AGENTS.md.en"]
        mutations = {
            "dropped bullet": ("- Preserve unrelated changes. Pushes, releases, merges, deletion and destructive migration require explicit authorization.\n", ""),
            "dropped numbered step 6": ("6. Read the memory index declared in the Project Profile's `memory` section. Verify a memory before acting on it, as specified in `AGENT.RULES.md`.\n", ""),
            "heading level changed": ("## Responsibilities", "### Responsibilities"),
            "wrong profile path": ("`.agents/moe.sakanano.agent-pack/project.json`", "`.agents/project.json`"),
            "placeholder removed": ("Run `ENTRY_COMMAND` without an Agent", "Run the command without an Agent"),
            "bullet became paragraph": ("- Tests: TODO", "Tests: TODO"),
            "Chinese left in English": ("- Build: TODO", "- Build: TODO 构建"),
        }
        self.assertEqual(equivalence_problems(zh, en), [])
        for label, (old, new) in mutations.items():
            with self.subTest(mutation=label):
                self.assertEqual(en.count(old), 1, "fixture drifted: " + label)
                self.assertTrue(equivalence_problems(zh, en.replace(old, new)), label)

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
