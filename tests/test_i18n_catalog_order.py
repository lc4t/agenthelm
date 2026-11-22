"""English catalogs stay in canonical order so parallel pull requests that add keys merge without conflicts (#175)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_catalog  # noqa: E402
from i18n_catalog import CATALOGS, ROOT, canonical, parse  # noqa: E402

FIX = "run: python3 tests/i18n_catalog.py --write"


class CatalogFormTests(unittest.TestCase):
    def test_every_catalog_is_canonical(self) -> None:
        for relative in CATALOGS:
            with self.subTest(catalog=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertEqual(text, canonical(text), f"{relative} is not in canonical order; {FIX}")

    def test_catalogs_are_string_maps_without_duplicate_keys(self) -> None:
        for relative in CATALOGS:
            with self.subTest(catalog=relative):
                self.assertGreater(len(parse((ROOT / relative).read_text(encoding="utf-8"))), 0)

    def test_duplicate_keys_and_non_string_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse('{"a": "1", "a": "2"}')
        with self.assertRaises(ValueError):
            parse('{"a": 1}')
        with self.assertRaises(ValueError):
            parse('["a"]')

    def test_canonical_form_sorts_by_code_point_one_entry_per_line(self) -> None:
        text = canonical('{"b": "2", "\\u4e2d": "3", "a\\nline": "1", "B": "0"}')
        self.assertEqual(text, '{\n "B": "0",\n "a\\nline": "1",\n "b": "2",\n "中": "3"\n}\n')
        self.assertEqual(canonical(text), text)

    def test_the_command_checks_and_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "en.json"
            copy.write_text('{"b": "2", "a": "1"}', encoding="utf-8")
            original = i18n_catalog.CATALOGS, i18n_catalog.ROOT
            i18n_catalog.CATALOGS, i18n_catalog.ROOT = ("en.json",), Path(temporary)
            try:
                self.assertEqual(i18n_catalog.main([]), 1)
                self.assertEqual(copy.read_text(encoding="utf-8"), '{"b": "2", "a": "1"}')
                self.assertEqual(i18n_catalog.main(["--write"]), 0)
                self.assertEqual(copy.read_text(encoding="utf-8"), '{\n "a": "1",\n "b": "2"\n}\n')
                self.assertEqual(i18n_catalog.main([]), 0)
            finally:
                i18n_catalog.CATALOGS, i18n_catalog.ROOT = original


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class MergeWithoutConflictTests(unittest.TestCase):
    """Two branches from one base each add different keys; the merge must be clean (reproduces the #175 problem)."""

    def run_git(self, directory: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
        return subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args],
                              cwd=directory, env=env, capture_output=True, text=True, check=check)

    def add_keys(self, path: Path, entries: dict[str, str], *, canonicalize: bool) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(entries)  # a new key goes to the end of the file, as it did before this change
        text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
        path.write_text(canonical(text) if canonicalize else text, encoding="utf-8")

    def merge_two_branches(self, *, canonicalize: bool) -> tuple[subprocess.CompletedProcess, Path]:
        source = (ROOT / CATALOGS[0]).read_text(encoding="utf-8")
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        catalog = directory / "locale/en.json"
        catalog.parent.mkdir()
        # The base is the catalog as it is in this repository; for the control it is the legacy append-only layout.
        catalog.write_text(source if canonicalize else json.dumps(parse(source), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        self.run_git(directory, "init", "-q", "-b", "base")
        self.run_git(directory, "add", "-A")
        self.run_git(directory, "commit", "-q", "-m", "base")
        for branch, entries in (("one", {"第一个分支新增的文案": "Message added on the first branch"}),
                                ("two", {"另一个分支新增的文案 {0}": "Message added on the second branch {0}"})):
            self.run_git(directory, "checkout", "-q", "-b", branch, "base")
            self.add_keys(catalog, entries, canonicalize=canonicalize)
            self.run_git(directory, "commit", "-q", "-am", branch)
        self.run_git(directory, "checkout", "-q", "one")
        return self.run_git(directory, "merge", "--no-edit", "two", check=False), catalog

    def test_two_branches_adding_different_keys_merge_cleanly(self) -> None:
        result, catalog = self.merge_two_branches(canonicalize=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        merged = catalog.read_text(encoding="utf-8")
        self.assertEqual(merged, canonical(merged))
        keys = parse(merged)
        self.assertIn("第一个分支新增的文案", keys)
        self.assertIn("另一个分支新增的文案 {0}", keys)

    def test_the_old_append_at_the_end_layout_conflicted(self) -> None:
        result, _catalog = self.merge_two_branches(canonicalize=False)
        self.assertNotEqual(result.returncode, 0, "the control scenario no longer conflicts, so the demonstration proves nothing")


if __name__ == "__main__":
    unittest.main()
