from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / 'plugins/opinion-manager/runtime/opinion_manager.py'
CATALOG = SCRIPT.parents[1] / 'catalog'
TEST_ROOT = ROOT / '__pycache__/opinion-test-work'
sys.path.insert(0, str(SCRIPT.parent))
from constants import KNOWN_PLACEHOLDERS


class OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / 'project'
        self.project.mkdir()

    def command(self, *args: object, success: bool = True) -> dict:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *(str(a) for a in args), '--output', 'json'],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0 if success else 2, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def previews(self, *selection: object) -> tuple[tuple, dict, dict]:
        args = ('profile', '--project', self.project, '--catalog', CATALOG,
                '--id', 'private.fixture', '--version', '1.0.0', *selection)
        profile = self.command(*args)
        candidate = self.root / 'candidate.json'
        candidate.write_text(json.dumps(profile['profile']), encoding='utf-8')
        compose = self.command('compose', '--project', self.project, '--profile', candidate)
        self.assertEqual(profile['content'], compose['content'])
        self.assertEqual(profile['unresolved'], [])
        self.assertIsNone(compose['blocked'])
        self.assertFalse(Path(profile['path']).exists())
        self.assertFalse((self.project / 'opinion.lock.json').exists())
        return args, profile, compose

    def save_confirmed_bundle(self, args: tuple, profile: dict, compose: dict) -> None:
        # One reviewed content bundle; two actual fingerprints retain their scope.
        rechecked = self.command(*args)
        current = self.command('compose', '--project', self.project,
                               '--profile', self.root / 'candidate.json')
        self.assertEqual(rechecked['confirmation_sha256'], profile['confirmation_sha256'])
        self.assertEqual(current['confirmation_sha256'], compose['confirmation_sha256'])
        self.command(*args, '--apply', '--confirm', profile['confirmation_sha256'])
        actual = self.command('compose', '--project', self.project, '--profile', profile['path'],
                              '--apply', '--confirm', compose['confirmation_sha256'])
        self.assertEqual(actual['content'], compose['content'])
        self.assertEqual(json.loads(Path(profile['path']).read_text()), profile['profile'])
        self.assertEqual((self.project / 'OPINION.md').read_text(), compose['content'])
        self.assertEqual(json.loads((self.project / 'opinion.lock.json').read_text()), compose['lock'])
        self.command('verify', '--project', self.project)

    def test_direct_scene_templates_preview_before_profile_save(self) -> None:
        for variant in ('reviewed-project', 'concise-project'):
            with self.subTest(variant=variant):
                # Each subcase is an independent project, not an overwrite.
                self.project = self.root / variant
                self.project.mkdir()
                ref = f'scenario/{variant}@1.0.0'
                args, profile, compose = self.previews('--template', ref)
                self.assertEqual(profile['profile']['templates'], [ref])
                self.assertEqual(len(profile['effective_rules']), 27)
                self.save_confirmed_bundle(args, profile, compose)

    def test_partial_selection_retains_template_and_exact_exclusion(self) -> None:
        edits = self.root / 'overrides.json'
        edits.write_text(json.dumps({'om.response-dense': None}), encoding='utf-8')
        args, profile, compose = self.previews(
            '--template', 'scenario/reviewed-project@1.0.0', '--overrides-file', edits)
        self.assertEqual(profile['profile']['templates'], ['scenario/reviewed-project@1.0.0'])
        self.assertEqual(len(profile['effective_rules']), 26)
        self.assertNotIn('om.response-dense', {r['id'] for r in profile['effective_rules']})
        self.save_confirmed_bundle(args, profile, compose)

    def test_custom_candidate_stays_unpublished_until_confirmed(self) -> None:
        custom = self.root / 'candidate.md'
        custom.write_text('CANDIDATE_ALPHA', encoding='utf-8')
        args, profile, compose = self.previews('--custom-file', custom)
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertIn('CANDIDATE_ALPHA', compose['content'])
        self.save_confirmed_bundle(args, profile, compose)

    def test_changed_candidate_rejects_old_profile_fingerprint(self) -> None:
        custom = self.root / 'candidate.md'
        custom.write_text('CANDIDATE_ALPHA', encoding='utf-8')
        args, profile, _ = self.previews('--custom-file', custom)
        custom.write_text('CANDIDATE_BETA', encoding='utf-8')
        self.command(*args, '--apply', '--confirm', profile['confirmation_sha256'], success=False)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_target_drift_is_visible_before_any_formal_save(self) -> None:
        args, profile, compose = self.previews('--rule', 'om.conclusion-first@1.0.0')
        opinion = self.project / 'OPINION.md'
        opinion.write_text('MANUAL_ALPHA', encoding='utf-8')
        current = self.command('compose', '--project', self.project,
                               '--profile', self.root / 'candidate.json')
        self.assertIsNotNone(current['blocked'])
        self.assertNotEqual(current['confirmation_sha256'], compose['confirmation_sha256'])
        self.assertFalse(Path(profile['path']).exists())
        self.command('compose', '--project', self.project, '--profile', self.root / 'candidate.json',
                     '--apply', '--confirm', compose['confirmation_sha256'], success=False)
        self.assertEqual(opinion.read_text(), 'MANUAL_ALPHA')

    def test_two_fingerprints_cannot_be_interchanged(self) -> None:
        args, profile, compose = self.previews('--rule', 'om.conclusion-first@1.0.0')
        self.assertNotEqual(profile['confirmation_sha256'], compose['confirmation_sha256'])
        self.command(*args, '--apply', '--confirm', compose['confirmation_sha256'], success=False)
        self.command('compose', '--project', self.project, '--profile', self.root / 'candidate.json',
                     '--apply', '--confirm', profile['confirmation_sha256'], success=False)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_new_and_legacy_placeholders_remain_exactly_protected(self) -> None:
        for index, placeholder in enumerate(KNOWN_PLACEHOLDERS):
            with self.subTest(index=index):
                self.project = self.root / f'placeholder-{index}'
                self.project.mkdir()
                opinion = self.project / 'OPINION.md'
                opinion.write_text(placeholder, encoding='utf-8')
                args, profile, compose = self.previews('--rule', 'om.conclusion-first@1.0.0')
                opinion.write_text(placeholder + '\nMANUAL_ALPHA\n', encoding='utf-8')
                changed = self.command('compose', '--project', self.project,
                                       '--profile', self.root / 'candidate.json')
                self.assertIsNotNone(changed['blocked'])
                self.assertFalse(Path(profile['path']).exists())
                self.assertEqual(opinion.read_text(), placeholder + '\nMANUAL_ALPHA\n')


if __name__ == '__main__':
    unittest.main()
