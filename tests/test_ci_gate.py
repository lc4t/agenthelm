"""Exercise event routing against actual commit graphs and the real deterministic gates."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ci_gate
import release_workflow as workflow
import test_release_workflow as release_tests


class EventGateTests(unittest.TestCase):
    setUp = release_tests.RepositoryTests.setUp
    tearDown = release_tests.RepositoryTests.tearDown
    git = release_tests.RepositoryTests.git
    write = release_tests.RepositoryTests.write
    commit = release_tests.RepositoryTests.commit
    add_change = release_tests.RepositoryTests.add_change
    adapter = release_tests.RepositoryTests.adapter
    plan = release_tests.RepositoryTests.plan

    def invoke(self, name, event):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = ci_gate.run(self.root, name, event)
        return code, output.getvalue()

    def push(self, before, branch='dev'):
        return {'before': before, 'after': self.git('rev-parse', 'HEAD'), 'ref': 'refs/heads/' + branch}

    def pull(self, base, head, source='feature/fix', target='dev'):
        return {'pull_request': {'base': {'sha': base, 'ref': target}, 'head': {'sha': head, 'ref': source}}}

    def test_dev_push_checks_all_new_commits_and_rejects_missing_fragment(self):
        before = self.git('rev-parse', 'HEAD')
        self.write('plugins/demo/runtime.py', 'print("new")\\n')
        self.commit('unlogged source change')
        code, output = self.invoke('push', self.push(before))
        self.assertEqual(code, 1, output)
        self.assertIn('new demo fragment', output)
        self.add_change()
        self.commit('declare the change')
        code, output = self.invoke('push', self.push(before))
        self.assertEqual(code, 0, output)
        self.assertIn('feature gate', output)

    def test_dev_push_of_generated_candidate_runs_release_gate(self):
        self.add_change()
        self.commit('feature')
        before = self.git('rev-parse', 'HEAD')
        workflow.apply_release(self.adapter(), self.plan(), candidate=True)
        self.assertTrue(workflow.check_changes(self.adapter(), before))  # The feature gate would reject generated versions.
        code, output = self.invoke('push', self.push(before))
        self.assertEqual(code, 0, output)
        self.assertIn('release gate', output)

    def test_release_pr_to_dev_uses_release_gate_and_feature_branch_cannot_write_versions(self):
        self.add_change()
        self.commit('feature')
        before = self.git('rev-parse', 'HEAD')
        self.git('branch', 'release/v1.0.1')
        workflow.apply_release(self.adapter(), self.plan(), candidate=False)
        head = self.git('rev-parse', 'HEAD')
        event = self.pull(before, head, source='release/v1.0.1')
        code, output = self.invoke('pull_request', event)
        self.assertEqual(code, 0, output)
        self.assertIn('release gate', output)
        event['pull_request']['head']['ref'] = 'feature/fix'
        code, output = self.invoke('pull_request', event)
        self.assertEqual(code, 1, output)
        self.assertIn('feature gate', output)

    def test_pr_uses_merge_base_of_real_head_not_newer_base_or_synthetic_merge(self):
        fork = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-qb', 'feature/fix')
        self.write('plugins/demo/feature.py', 'print("feature")\\n')
        self.add_change('feature.json')
        self.commit('feature based on the old dev')
        head = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-qb', 'dev', fork)
        self.add_change('upstream.json')
        self.commit('upstream feature')
        workflow.apply_release(self.adapter(), self.plan(), candidate=True)
        base = self.git('rev-parse', 'HEAD')
        self.git('merge', '--no-ff', 'feature/fix', '-m', 'synthetic GitHub PR merge')
        merge = self.git('rev-parse', 'HEAD')
        event = self.pull(base, head)
        selection = ci_gate.select_gate(self.root, 'pull_request', event)
        self.assertEqual(selection['head'], head)
        self.assertEqual(selection['base'], fork)
        self.assertNotEqual(selection['base'], base)
        self.assertNotEqual(selection['head'], merge)
        code, output = self.invoke('pull_request', event)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.git('rev-parse', 'HEAD'), merge)  # Inspection never checks out or rewrites the main worktree.
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_main_rejects_feature_pr_and_checks_release_head(self):
        self.add_change()
        self.commit('feature')
        before = self.git('rev-parse', 'HEAD')
        workflow.apply_release(self.adapter(), self.plan(), candidate=False)
        head = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'PRs to main'):
            self.invoke('pull_request', self.pull(before, head, target='main'))
        code, output = self.invoke('pull_request', self.pull(before, head, source='release/v1.0.1', target='main'))
        self.assertEqual(code, 0, output)
        self.write('plugins/demo/unlogged.py', 'print("unlogged")\\n')
        self.commit('unlogged after preparation')
        code, output = self.invoke('pull_request', self.pull(before, self.git('rev-parse', 'HEAD'), source='release/v1.0.1', target='main'))
        self.assertEqual(code, 1, output)
        self.assertIn('source changed', output)

    def test_main_promotion_rejects_a_release_head_that_omits_new_main_commits(self):
        baseline = self.git('rev-parse', 'HEAD')
        self.add_change()
        self.commit('feature')
        workflow.apply_release(self.adapter(), self.plan(), candidate=False)
        head = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-qb', 'main', baseline)
        self.write('README.md', 'Main advanced independently\n')
        self.commit('main advanced')
        base = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'fast-forward'):
            self.invoke('pull_request', self.pull(base, head, source='release/v1.0.1', target='main'))

    def test_new_release_branch_accepts_zero_before_and_runs_strict_release_gate(self):
        self.add_change()
        self.commit('feature')
        workflow.apply_release(self.adapter(), self.plan(), candidate=False)
        code, output = self.invoke('push', self.push('0' * 40, branch='release/v1.0.1'))
        self.assertEqual(code, 0, output)
        self.assertIn('release gate', output)
        self.write('plugins/demo/unlogged.py', 'print("changed")\n')
        self.commit('changed after preparation')
        code, output = self.invoke('push', self.push('0' * 40, branch='release/v1.0.1'))
        self.assertEqual(code, 1, output)
        self.assertIn('source changed', output)

    def test_release_branch_creation_still_rejects_missing_or_unknown_commits(self):
        for before in (None, 'f' * 40):
            with self.subTest(before=before), self.assertRaises((ValueError, subprocess.CalledProcessError)):
                self.invoke('push', self.push(before, branch='release/v1.0.1'))
        event = self.push('0' * 40, branch='release/v1.0.1')
        event['after'] = 'f' * 40
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke('push', event)

    def test_missing_or_zero_push_baseline_fails_explicitly(self):
        event = self.push('0' * 40)
        with self.assertRaisesRegex(ValueError, 'push.before'):
            self.invoke('push', event)


class WorkflowCollectionTests(unittest.TestCase):
    def test_suites_keep_running_after_a_failed_gate_or_suite_without_masking_failure(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / '.github/workflows/tests.yml').read_text()
        self.assertIn('- id: checkout', text)
        self.assertIn('- id: setup_python', text)
        self.assertEqual(text.count("if: ${{ !cancelled() && steps.setup_python.outcome == 'success' }}"), 11)
        self.assertNotIn('continue-on-error', text)
        self.assertIn('python scripts/ci_gate.py', text)
        self.assertNotIn('check-changes --base HEAD', text)
