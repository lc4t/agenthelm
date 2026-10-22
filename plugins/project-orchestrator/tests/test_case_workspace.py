from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import case_workspace as cw


class CaseWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / 'tasks/20260101-example'
        (source / 'docs').mkdir(parents=True)
        (source / 'docs/report.md').write_text('unchanged')
        self.mapping = {'schema_version': '1.0', 'moves': [
            {'source': 'tasks/20260101-example', 'target': 'cases/CASE-20260101-001-example'}]}

    def test_preview_has_zero_writes(self):
        before = sorted(str(p) for p in self.root.rglob('*'))
        result = cw.migrate(self.root, self.mapping)
        self.assertFalse(result['applied'])
        self.assertEqual(result['file_count'], 1)
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob('*')))

    def test_project_declared_case_root(self):
        profile = self.root / '.agents/moe.sakanano.agent-pack/project.json'
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({'work': {'case': {'root': 'work/cases'}}}))
        self.mapping['moves'][0]['target'] = 'work/cases/example'
        self.assertTrue(cw.migrate(self.root, self.mapping, apply=True)['applied'])
        profile.unlink()
        profile.symlink_to(self.root / 'work/cases/example/docs/report.md')
        with self.assertRaises(cw.WorkspaceError):
            cw.inspect_cases(self.root)

    def test_apply_verifies_content_and_preserves_empty_directories(self):
        (self.root / 'tasks/20260101-example/empty').mkdir()
        self.mapping['records'] = [{'path': 'cases/CASE-20260101-001-example/CASE.md', 'content': 'record'}]
        before = cw.inventory(self.root / self.mapping['moves'][0]['source'])
        result = cw.migrate(self.root, self.mapping, apply=True)
        target = self.root / self.mapping['moves'][0]['target']
        self.assertTrue(result['applied'])
        self.assertFalse((self.root / self.mapping['moves'][0]['source']).exists())
        self.assertTrue((target / 'empty').is_dir())
        self.assertEqual(before, result['moves'][0]['manifest'])
        self.assertEqual((target / 'docs/report.md').read_text(), 'unchanged')

    def test_all_conflicts_checked_before_moves(self):
        self.mapping['moves'].append({'source': 'tasks/other', 'target': 'cases/taken'})
        (self.root / 'tasks/other').mkdir()
        (self.root / 'cases/taken').mkdir(parents=True)
        with self.assertRaises(cw.WorkspaceError):
            cw.migrate(self.root, self.mapping, apply=True)
        self.assertTrue((self.root / 'tasks/20260101-example').exists())

    def test_symlink_and_traversal_rejected(self):
        (self.root / 'tasks/20260101-example/link').symlink_to('/tmp')
        with self.assertRaises(cw.WorkspaceError):
            cw.migrate(self.root, self.mapping, apply=True)
        with self.assertRaises(cw.WorkspaceError):
            cw.safe_path(self.root, '../outside')

    def test_opaque_files_are_moved_without_reading(self):
        source = self.root / 'tasks/20260101-example'
        (source / '.env').write_text('opaque-test')
        (source / 'confidential').mkdir()
        (source / 'confidential/note.md').write_text('opaque-test')
        original = cw.digest_file
        def digest(path):
            self.assertNotIn(path.name, ('.env', 'note.md'))
            return original(path)
        with patch.object(cw, 'digest_file', side_effect=digest):
            result = cw.migrate(self.root, self.mapping, apply=True)
        self.assertEqual(result['opaque_files'], 2)

    def test_failure_after_move_rolls_back(self):
        original = cw.inventory
        def check(path):
            if path.parent.name == 'cases':
                raise cw.WorkspaceError('simulated verification failure')
            return original(path)
        with patch.object(cw, 'inventory', side_effect=check):
            with self.assertRaises(cw.WorkspaceError):
                cw.migrate(self.root, self.mapping, apply=True)
        self.assertTrue((self.root / 'tasks/20260101-example/docs/report.md').exists())
        self.assertFalse((self.root / 'cases').exists())

    def test_record_write_failure_rolls_back(self):
        self.mapping['records'] = [{'path': 'cases/CASE-20260101-001-example/CASE.md', 'content': 'record'}]
        with patch.object(cw.os, 'fsync', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                cw.migrate(self.root, self.mapping, apply=True)
        self.assertTrue((self.root / 'tasks/20260101-example').exists())
        self.assertFalse((self.root / 'tasks/20260101-example/CASE.md').exists())

    def test_stale_fingerprint_rejected(self):
        preview = cw.migrate(self.root, self.mapping)
        self.mapping['moves'][0]['fingerprint'] = preview['moves'][0]['manifest']['fingerprint']
        (self.root / 'tasks/20260101-example/docs/report.md').write_text('changed')
        with self.assertRaises(cw.WorkspaceError):
            cw.migrate(self.root, self.mapping, apply=True)

    def test_file_move_and_atomic_target_conflict(self):
        mapping = {'schema_version': '1.0', 'moves': [{'source': 'tasks/20260101-example/docs/report.md', 'target': 'cases/example/docs/report.md'}]}
        self.assertTrue(cw.migrate(self.root, mapping, apply=True)['applied'])
        source = self.root / 'tasks/20260101-example'
        target = self.root / 'cases/example'
        with self.assertRaises(cw.WorkspaceError):
            cw.rename_exclusive(source, target)
        self.assertTrue(source.exists())

    def test_search_all_statuses_and_old_id_without_artifact_read(self):
        for status in ('active', 'done', 'abandoned'):
            directory = self.root / 'cases' / status
            directory.mkdir(parents=True)
            (directory / 'CASE.md').write_text('---\nid: "' + status + '"\ntitle: "Example"\nstatus: "' + status + '"\nlegacy_ids: ["old-id"]\n---\n')
            (directory / 'attachment.pdf').write_bytes(b'not parsed')
        with patch.object(cw, 'digest_file', side_effect=AssertionError('artifact read')):
            result = cw.inspect_cases(self.root, ['old-id', 'attachment'])
        self.assertEqual(len(result['cases']), 3)
        self.assertFalse(result['invalid'])
