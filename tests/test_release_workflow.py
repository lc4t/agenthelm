"""Release integration runs in disposable repositories, never tags/pushes the real checkout."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import release_meta as meta
import release_model as model
import release_workflow as workflow

ROOT = Path(__file__).resolve().parents[1]


def change(scope='demo', level='patch', **kw):
    return {'scope': scope, 'level': level, 'actions': [], 'user_action_required': [], 'touches': [],
            'default_for_existing': 'on', 'template_changes': [], 'contracts': [], **kw}


def fragment(*changes, **kw):
    return {'schema': 1, 'summary': {'zh': '更新测试', 'en': 'Test update'},
            'changes': list(changes or [change()]), 'suite': None, **kw}


class PurePlanTests(unittest.TestCase):
    def setUp(self):
        self.bom = {'tag': 'v1.0.0', 'suite': '1.0.0', 'plugins': {'demo': {'source': './plugins/demo', 'version': '1.0.0',
                    'content': {'demo/templates': {'version': '1.0.0', 'kind': 'template', 'paths': ['template.md', 'AGENTS.md']}}}}}

    def test_parallel_fragments_are_order_independent_and_bump_once(self):
        fragments = [('c.json', fragment(change(level='patch'))), ('a.json', fragment(change(level='minor'))),
                     ('b.json', fragment(change(level='patch')))]
        before = copy.deepcopy((self.bom, fragments))
        first = model.plan(self.bom, fragments, self.bom, '2026-11-04')
        self.assertEqual(first, model.plan(self.bom, list(reversed(fragments)), self.bom, '2026-11-04'))
        self.assertEqual(first['suite'], '1.1.0')
        self.assertEqual(first['plugins']['demo']['version'], '1.1.0')
        self.assertEqual((self.bom, fragments), before)
        self.assertEqual(first['plugins']['demo']['content']['demo/templates']['version'], '1.0.0')

    def test_content_semantics_and_suite_override(self):
        f = fragment(change(template_changes=[{'path': 'AGENTS.md', 'class': 'semantic'}], touches=['AGENTS.md'],
                            user_action_required=[{'what': 'Review content', 'session': 'current'}]), suite='minor')
        result = model.plan(self.bom, [('a', f)], self.bom, '2026-11-04')
        self.assertEqual(result['plugins']['demo']['content']['demo/templates']['version'], '1.1.0')
        self.assertEqual(result['suite'], '1.1.0')

    def test_added_and_removed_plugins_set_suite_grade(self):
        current = copy.deepcopy(self.bom)
        current['plugins']['new'] = {'source': './plugins/new', 'version': '0.0.0'}
        self.assertEqual(model.plan(self.bom, [], current, '2026-11-04')['suite'], '1.1.0')
        self.assertEqual(model.plan(self.bom, [], {'plugins': {}}, '2026-11-04')['suite'], '2.0.0')

    def test_overrides_can_only_raise_and_require_a_reason(self):
        fs = [('a', fragment(change(level='minor')))]
        for override in [{'demo': {'level': 'patch', 'reason': 'No'}}, {'demo': {'level': 'major', 'reason': ''}}]:
            with self.assertRaises(ValueError):
                model.plan(self.bom, fs, self.bom, '2026-11-04', override)
        raised = model.plan(self.bom, fs, self.bom, '2026-11-04', {'demo': {'level': 'major', 'reason': 'API break'}})
        self.assertEqual(raised['suite'], '2.0.0')
        self.assertEqual(raised['overrides']['demo']['reason'], 'API break')

    def test_every_required_field_has_a_negative_case(self):
        good = fragment()
        for key in ('schema', 'summary', 'changes', 'suite'):
            bad = copy.deepcopy(good)
            del bad[key]
            self.assertTrue(model.fragment_problems('bad', bad, {'demo'}, meta.declaration_problems), key)
        for key in ('scope', 'level', *model.FIELDS):
            bad = copy.deepcopy(good)
            del bad['changes'][0][key]
            self.assertTrue(model.fragment_problems('bad', bad, {'demo'}, meta.declaration_problems), key)
        bad = fragment(change(level='major'))
        self.assertTrue(model.fragment_problems('bad', bad, {'demo'}, meta.declaration_problems))
        bad = fragment(change(version='2.0.0'))
        self.assertTrue(model.fragment_problems('bad', bad, {'demo'}, meta.declaration_problems))


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(meta, 'ROOT', self.root)
        self.patch.start()
        self.git('init', '-q')
        self.git('config', 'user.email', 'tests@example.invalid')
        self.git('config', 'user.name', 'Release tests')
        self.write('plugins/demo/plugin.json', {'name': 'demo', 'version': '1.0.0', 'description': 'Demo',
                                                'author': {'name': 'Test'}, 'repository': 'https://github.com/test/demo'})
        self.write('plugins/demo/.codex-plugin/plugin.json', {'name': 'demo', 'version': '1.0.0'})
        self.write('plugins/demo/template.md', 'Template one\n')
        self.write('plugins/demo/content.json', {'schema_version': 1, 'packs': [{'id': 'demo/templates', 'version': '1.0.0',
                    'kind': 'template', 'sources': ['template.md'], 'materializes': ['AGENTS.md'], 'files': {}}]})
        self.write('index.json', {'plugin': {'version': '1.0.0'}, 'site': {'updated_at': '2026-11-01'},
                                 'skills': [{'id': 'demo', 'version': '1.0.0'}]})
        self.write('packages/cli/package.json', {'version': '1.0.0'})
        self.write('index.html', '<span>Release 1.0.0 · 更新日期 2026-11-01</span>\n')
        self.write('CHANGELOG.md', '# Changes\n')
        self.write('release/history/legacy/migrations.json', [])
        self.assertEqual(meta.main(['--write']), 0)
        self.commit('baseline')
        self.git('tag', 'v1.0.0')

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def adapter(self):
        return types.SimpleNamespace(**vars(meta))

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data if isinstance(data, str) else meta.dump(data), encoding='utf-8')

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-qm', message)

    def add_change(self, name='fix.json', **kw):
        self.write('release/changes/' + name, fragment(change(**kw)))

    def plan(self):
        return workflow.make_plan(self.adapter(), 'v1.0.0', '2026-11-04')

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*')
                if p.is_file() and '.git' not in p.relative_to(self.root).parts}

    def test_feature_gate_requires_fresh_fragment_and_freezes_versions(self):
        self.write('plugins/demo/runtime.py', 'print("changed")\n')
        problems = workflow.check_changes(self.adapter(), 'v1.0.0')
        self.assertTrue(any('fragment' in p for p in problems))
        self.add_change()
        self.assertEqual(workflow.check_changes(self.adapter(), 'v1.0.0'), [])
        manifest = meta.read_json(self.root / 'plugins/demo/plugin.json')
        self.write('plugins/demo/plugin.json', {**manifest, 'version': '1.0.1'})
        self.assertTrue(any('release command' in p for p in workflow.check_changes(self.adapter(), 'v1.0.0')))

    def test_existing_fragments_and_history_are_immutable(self):
        self.add_change()
        self.commit('first fragment')
        before = self.git('rev-parse', 'HEAD')
        self.add_change(level='minor')
        self.assertTrue(any('immutable' in p for p in workflow.check_changes(self.adapter(), before)))
        self.write('release/history/legacy/migrations.json', [{'bad': True}])
        self.assertTrue(any('immutable' in p for p in workflow.check_changes(self.adapter(), before)))

    def test_template_fix_after_rc_passes_feature_gate_without_writing_digests(self):
        self.add_change()
        self.commit('first fix')
        workflow.apply_release(self.adapter(), self.plan(), candidate=True)
        base = self.git('rev-parse', 'HEAD')
        self.write('plugins/demo/template.md', 'New template after RC\n')
        self.add_change('template-fix.json', template_changes=[{'path': 'AGENTS.md', 'class': 'cosmetic'}], touches=['AGENTS.md'])
        self.assertEqual(workflow.check_changes(self.adapter(), base), [])
        planned = self.plan()
        saved = meta.read_json(self.root / workflow.CANDIDATE)
        self.assertNotEqual(planned, saved)
        self.assertEqual(planned['plugins']['demo']['content']['demo/templates']['version'], '1.0.1')
        # Invoke the actual shared CI metadata test against this feature fixture.
        import test_release_meta
        with mock.patch.object(test_release_meta, 'ROOT', self.root):
            test_release_meta.ReleaseMetaTests('test_derived_files_are_in_sync').test_derived_files_are_in_sync()

    def test_formal_tag_requires_formal_archive(self):
        self.add_change()
        self.commit('fix')
        workflow.apply_release(self.adapter(), self.plan(), candidate=True)
        self.assertTrue(any('archived' in p for p in workflow.check_release(self.adapter(), 'v1.0.0', 'v1.0.1')))

    def test_candidate_tag_requires_retained_fragments(self):
        self.add_change()
        self.commit('fix')
        workflow.apply_release(self.adapter(), self.plan(), candidate=False)
        self.assertTrue(any('retaining change fragments' in p
                            for p in workflow.check_release(self.adapter(), 'v1.0.0', 'v1.0.1-rc.1')))

    def test_binary_plugin_asset_requires_a_fragment_and_passes_with_one(self):
        (self.root / 'plugins/demo/icon.png').write_bytes(b'\x89PNG\x00\xff\xfe')
        self.assertTrue(workflow.check_changes(self.adapter(), 'v1.0.0'))
        self.add_change()
        self.assertEqual(workflow.check_changes(self.adapter(), 'v1.0.0'), [])

    def test_mode_change_does_not_make_an_old_fragment_fresh(self):
        self.add_change()
        self.commit('old fragment')
        base = self.git('rev-parse', 'HEAD')
        self.write('plugins/demo/runtime.py', 'print("new change")\n')
        (self.root / 'release/changes/fix.json').chmod(0o755)
        self.assertTrue(any('new demo fragment' in p for p in workflow.check_changes(self.adapter(), base)))

    def test_template_change_requires_a_matching_declaration(self):
        self.write('plugins/demo/template.md', 'Template two\n')
        self.add_change()
        self.assertTrue(any('template_changes' in p for p in workflow.check_changes(self.adapter(), 'v1.0.0')))
        self.add_change(template_changes=[{'path': 'unrelated.md', 'class': 'cosmetic'}], touches=['unrelated.md'])
        with self.assertRaisesRegex(ValueError, 'matching template_changes'):
            self.plan()
        self.add_change(template_changes=[{'path': 'AGENTS.md', 'class': 'cosmetic'}], touches=['AGENTS.md'])
        self.assertEqual(self.plan()['plugins']['demo']['content']['demo/templates']['version'], '1.0.1')

    def test_rc_repeat_uses_official_baseline_then_formal_archives_in_one_commit(self):
        self.add_change()
        self.write('plugins/demo/runtime.py', 'print("fixed")\n')
        self.commit('fix')
        initial = int(self.git('rev-list', '--count', 'HEAD'))
        workflow.apply_release(self.adapter(), self.plan(), candidate=True)
        self.assertEqual(int(self.git('rev-list', '--count', 'HEAD')), initial + 1)
        self.git('tag', 'v1.0.1-rc.1')
        self.assertEqual(workflow.official_base(self.adapter()), 'v1.0.0')
        first = self.plan()
        self.assertEqual(first['suite'], '1.0.1')
        workflow.apply_release(self.adapter(), first, candidate=True)
        self.assertEqual(self.plan(), first)
        self.add_change('next.json')
        self.commit('second fix')
        workflow.apply_release(self.adapter(), self.plan(), candidate=False)
        self.assertFalse((self.root / 'release/changes/fix.json').exists())
        self.assertFalse((self.root / 'release/candidate.json').exists())
        self.assertTrue((self.root / 'release/history/1.0.1/changes/fix.json').is_file())
        self.assertEqual(workflow.check_release(self.adapter(), 'v1.0.0', 'v1.0.1'), [])
        self.assertTrue(workflow.check_release(self.adapter(), 'v1.0.0', 'v9.0.0'))
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('tag', '--list'), 'v1.0.0\nv1.0.1-rc.1')  # apply never tags
        self.assertEqual((self.root / 'CHANGELOG.md').read_text().count('agenthelm 1.0.1'), 1)

    def test_new_fragment_after_candidate_requires_regeneration(self):
        self.add_change()
        self.commit('fix')
        workflow.apply_release(self.adapter(), self.plan(), candidate=True)
        self.add_change('later.json', level='minor')
        self.commit('later feature')
        self.assertTrue(any('source fragments' in p for p in workflow.check_release(self.adapter(), 'v1.0.0')))

    def test_prepared_candidate_and_formal_reject_unlogged_source_changes(self):
        for candidate in (True, False):
            with self.subTest(candidate=candidate):
                self.add_change()
                self.write('plugins/demo/runtime.py', 'print("prepared")\n')
                self.commit('fix')
                workflow.apply_release(self.adapter(), self.plan(), candidate=candidate)
                self.write('plugins/demo/runtime.py', 'print("unlogged")\n')
                self.commit('source changed without fragment')
                found = workflow.check_release(self.adapter(), 'v1.0.0')
                self.assertTrue(any('source changed' in p and 'runtime.py' in p for p in found), found)
                # Reset only the disposable fixture before the next independent case.
                self.git('reset', '--hard', 'v1.0.0')
                self.git('clean', '-fd')

    def test_recomputing_versions_does_not_rebind_prepared_source(self):
        self.add_change()
        self.write('plugins/demo/icon.png', 'initial asset')
        self.commit('fix')
        workflow.apply_release(self.adapter(), self.plan(), candidate=True)
        (self.root / 'plugins/demo/icon.png').write_bytes(b'\x89PNG\x00\xff')
        saved = meta.read_json(self.root / workflow.CANDIDATE)
        recomputed = self.plan()
        saved['plugins'] = recomputed['plugins']
        saved['suite'] = recomputed['suite']
        self.write(workflow.CANDIDATE, saved)
        found = workflow.check_release(self.adapter(), 'v1.0.0')
        self.assertTrue(any('source changed' in p and 'icon.png' in p for p in found), found)

    def test_failed_commit_restores_files_and_index(self):
        self.add_change()
        self.commit('fix')
        before = self.snapshot()
        hook = self.root / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        with self.assertRaises(subprocess.CalledProcessError):
            workflow.apply_release(self.adapter(), self.plan(), candidate=False)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_dirty_tree_and_readonly_preview_are_safe(self):
        self.add_change()
        before = self.snapshot()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(meta.main(['release', 'plan', '--base', 'v1.0.0']), 0)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(ValueError, 'clean work tree'):
            workflow.apply_release(self.adapter(), self.plan(), candidate=True)

    def test_same_tree_promotion_merge_is_an_official_baseline(self):
        # Reproduce the pre-transition repository: main tags a no-change merge of dev,
        # while dev still points to that merge's parent.
        self.git('checkout', '-qb', 'dev')
        self.add_change()
        self.commit('dev work')
        dev = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-qb', 'main', 'v1.0.0')
        self.git('merge', '--no-ff', 'dev', '-m', 'promote')
        self.git('tag', 'v1.0.1')
        self.git('checkout', 'dev')
        self.assertEqual(self.git('rev-parse', 'HEAD'), dev)
        self.assertEqual(workflow.official_base(self.adapter()), 'v1.0.1')

    def test_default_release_gate_excludes_current_official_tag(self):
        self.add_change()
        self.commit('fix')
        workflow.apply_release(self.adapter(), self.plan(), candidate=False)
        self.git('tag', 'v1.0.1')
        self.assertEqual(meta.main(['check-release', '--tag', 'v1.0.1']), 0)

    def test_rc_gate_uses_same_tree_promotion_tag_as_the_planner(self):
        self.git('checkout', '-qb', 'dev')
        self.add_change()
        self.commit('first fix')
        workflow.apply_release(self.adapter(), self.plan(), candidate=False)
        self.git('checkout', '-qb', 'main', 'v1.0.0')
        self.git('merge', '--no-ff', 'dev', '-m', 'promote')
        self.git('tag', 'v1.0.1')
        self.git('checkout', 'dev')
        self.add_change('next.json')
        self.commit('next fix')
        baseline = workflow.official_base(self.adapter())
        self.assertEqual(baseline, 'v1.0.1')
        planned = workflow.make_plan(self.adapter(), baseline, '2026-11-05')
        workflow.apply_release(self.adapter(), planned, candidate=True)
        self.git('tag', 'v1.0.2-rc.1')
        self.assertEqual(meta.main(['check-release', '--tag', 'v1.0.2-rc.1']), 0)

    def test_new_release_notes_do_not_delete_previous_generated_section(self):
        self.add_change()
        self.commit('first')
        workflow.apply_release(self.adapter(), self.plan(), candidate=False)
        self.git('tag', 'v1.0.1')
        self.add_change('another.json')
        self.commit('next')
        second = workflow.make_plan(self.adapter(), 'v1.0.1', '2026-11-05')
        workflow.apply_release(self.adapter(), second, candidate=False)
        changelog = (self.root / 'CHANGELOG.md').read_text()
        self.assertIn('agenthelm 1.0.1', changelog)
        self.assertIn('agenthelm 1.0.2', changelog)
