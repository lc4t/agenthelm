import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import promote_check as pc  # noqa: E402

SHA = 'a' * 40
OTHER = 'b' * 40
OLD_MAIN = 'c' * 40


def good(**changes):
    facts = dict(tag='v6.14.0', suite_version='6.14.0', tag_commit=SHA, branch_tip=SHA,
                 main_commit=OLD_MAIN, main_is_ancestor=True, tests_conclusion='success',
                 release_commit=SHA, npm_version='6.14.0')
    facts.update(changes)
    return pc.Facts(**facts)


def failed(facts):
    return [r.key for r in pc.evaluate(facts) if not r.ok]


class CheckTests(unittest.TestCase):
    def test_all_pass(self):
        self.assertEqual(failed(good()), [])

    def test_a_tag_format_and_version(self):
        self.assertEqual(failed(good(tag='v6.14.0-rc.1'))[0], 'a')
        self.assertEqual(failed(good(tag='6.14.0'))[0], 'a')
        self.assertEqual(failed(good(suite_version='6.15.0'))[0], 'a')
        self.assertEqual(failed(good(suite_version=None))[0], 'a')

    def test_a_failure_skips_the_rest(self):
        results = pc.evaluate(good(tag='v6.14.0-rc.1'))
        self.assertEqual([r.key for r in results], list('abcdef'))
        self.assertTrue(all('skipped' in r.detail for r in results[1:]))

    def test_b_branch_tip(self):
        self.assertEqual(failed(good(branch_tip=OTHER)), ['b'])
        self.assertEqual(failed(good(branch_tip=None)), ['b'])

    def test_c_fast_forward(self):
        self.assertEqual(failed(good(main_is_ancestor=False)), ['c'])
        self.assertEqual(failed(good(main_commit=None, main_is_ancestor=None)), ['c'])

    def test_c_noop_when_main_equals_tag(self):
        facts = good(main_commit=SHA, main_is_ancestor=True)
        results = pc.evaluate(facts)
        self.assertEqual(failed(facts), [])
        self.assertTrue(results[2].noop)
        text = pc.render(facts, results)
        self.assertIn('no-op', text)
        self.assertNotIn('git push', text)

    def test_d_tests(self):
        self.assertEqual(failed(good(tests_conclusion='failure')), ['d'])
        self.assertEqual(failed(good(tests_conclusion=None)), ['d'])

    def test_e_release(self):
        self.assertEqual(failed(good(release_commit=OTHER)), ['e'])
        self.assertEqual(failed(good(release_commit=None)), ['e'])

    def test_f_npm_not_published(self):
        self.assertEqual(failed(good(npm_version=None)), ['f'])
        self.assertEqual(failed(good(npm_version='6.13.1')), ['f'])

    def test_stale_tag(self):
        # A tag behind the suite version, and a tag no longer at the release branch tip.
        self.assertEqual(failed(good(tag='v6.13.1'))[0], 'a')
        self.assertEqual(failed(good(branch_tip=OTHER, tag_commit=SHA)), ['b'])

    def test_failure_reports_actual_and_expected(self):
        text = pc.render(good(npm_version=None), pc.evaluate(good(npm_version=None)))
        self.assertIn('[FAIL] f.', text)
        self.assertIn('actual=unavailable expected=6.14.0', text)
        self.assertIn('RESULT: FAIL; failed checks: f', text)
        self.assertNotIn('git push', text)


class OutputTests(unittest.TestCase):
    def test_push_command_text_is_fixed_and_has_no_token(self):
        facts = good()
        text = pc.render(facts, pc.evaluate(facts))
        self.assertEqual(pc.push_command('v6.14.0'), 'git push origin v6.14.0:main')
        self.assertIn('  git push origin v6.14.0:main\n', text)
        self.assertIsNone(re.search(r'https?://|token|x-access|ghp_', text, re.I))


class WorkflowTests(unittest.TestCase):
    def read(self, name):
        return (ROOT / '.github' / 'workflows' / name).read_text()

    def test_yaml_parses_when_pyyaml_is_available(self):
        try:
            import yaml
        except ImportError:
            return
        for name in ('promote.yml', 'release.yml'):
            self.assertIsInstance(yaml.safe_load(self.read(name)), dict)

    def test_promote_is_read_only_and_manual(self):
        text = self.read('promote.yml')
        self.assertNotIn('contents: write', text)
        self.assertNotIn('id-token: write', text)
        self.assertNotIn('actions: write', text)
        self.assertIsNone(re.search(r'secrets\.|git push|gh workflow run|create-github-app', text))
        self.assertIn('contents: read', text)
        self.assertIn('actions: read', text)
        triggers = re.search(r'^on:\n((?:[ \t]+.*\n|\n)+)', text, re.M).group(1)
        self.assertEqual(re.findall(r'^  (\w+):', triggers, re.M), ['workflow_dispatch'])

    def test_release_drops_unused_actions_write(self):
        self.assertNotIn('actions: write', self.read('release.yml'))
        self.assertIn('contents: write', self.read('release.yml'))


if __name__ == '__main__':
    unittest.main()
