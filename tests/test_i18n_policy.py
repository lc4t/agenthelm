"""Explicit documentation pairing and shared UI terminology (#24)."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / 'tests/i18n-policy.json').read_text(encoding='utf-8'))


def links_to(source: Path, destination: Path) -> bool:
    targets = re.findall(r'\[[^\]]*\]\(([^)]+)\)', source.read_text(encoding='utf-8'))
    return any((source.parent / target.split('#', 1)[0]).resolve() == destination.resolve()
               for target in targets if not re.match(r'\w+://', target))


class DocumentationPolicyTests(unittest.TestCase):
    def test_pairs_exist_and_link_both_ways(self):
        registered = set()
        for pair in POLICY['pairs']:
            self.assertEqual(len(pair), 2)
            for source, destination in (pair, pair[::-1]):
                self.assertNotIn(source, registered, source)
                registered.add(source)
                self.assertTrue((ROOT / source).is_file(), source)
                self.assertTrue(links_to(ROOT / source, ROOT / destination), source)

    def test_public_docs_are_paired_or_explicitly_deferred(self):
        paths = {ROOT / 'README.md', ROOT / 'INSTALL.md',
                 *(ROOT / 'plugins').glob('*/references/**/*.md'),
                 *(ROOT / 'plugins').glob('*/skills/**/SKILL.md')}
        pairs = {ROOT / p for pair in POLICY['pairs'] for p in pair}
        deferred = {ROOT / p for p in POLICY['deferred']}
        self.assertFalse(pairs & deferred)
        self.assertEqual(paths - pairs, deferred, 'Register new public docs and remove stale deferrals')
        self.assertTrue(all(reason.strip() for reason in POLICY['deferred'].values()))

    def test_glossary_labels_and_quoted_references_are_consistent(self):
        for term in POLICY['glossary']:
            for path in term['catalogs']:
                catalog = json.loads((ROOT / path).read_text(encoding='utf-8'))
                self.assertEqual(catalog[term['source']], term['english'], path)
                for source, translation in catalog.items():
                    if f'「{term["source"]}」' in source:
                        self.assertIn(term['english'], translation, f'{path}: {source}')


if __name__ == '__main__':
    unittest.main()
