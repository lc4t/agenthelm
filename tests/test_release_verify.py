"""Guards the release verification tools in release/verify/ (compare_runs, tag_matrix, payload_compare).

Each tool carries its own ``--self-test`` (injected differences must be reported, a command that never ran must be an empty
comparison); this file runs them, runs each tool once on the real repository, and pins that ``release/verify/`` is not shipped
in the npm package nor in the release tarball. Tags missing from the clone skip the tag tests;
``AGENTHELM_REQUIRE_TAGS=1`` (CI) makes that a failure.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "release" / "verify"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(VERIFY))
import _common as common  # noqa: E402
import release_workflow as workflow  # noqa: E402
import tag_matrix  # noqa: E402

REQUIRE_TAGS = os.environ.get("AGENTHELM_REQUIRE_TAGS") == "1"
TAGS = ("v6.14.0", "v6.15.0")


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(VERIFY / script), *args], capture_output=True, text=True, check=False, cwd=ROOT)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)


class SelfTests(unittest.TestCase):
    def test_every_tool_passes_its_own_self_test(self) -> None:
        for script in ("compare_runs.py", "tag_matrix.py", "payload_compare.py"):
            with self.subTest(tool=script):
                done = run(script, "--self-test")
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("self-test: ok", done.stderr)

    def test_a_broken_guard_is_caught_by_the_self_test(self) -> None:
        # The 6.15.0 lesson: if an argument error stopped counting as "not executed", the self-test must fail.
        import compare_runs
        saved = common.BAD_DEFAULT_EXITS
        try:
            common.BAD_DEFAULT_EXITS = ()
            self.assertTrue(compare_runs.self_test())
        finally:
            common.BAD_DEFAULT_EXITS = saved
        self.assertEqual(compare_runs.self_test(), [])


class EngineTests(unittest.TestCase):
    def test_executed_verdict(self) -> None:
        verdict = common.executed_verdict
        self.assertIsNone(verdict({}, 0, None, False))
        self.assertIsNone(verdict({}, 1, None, False))
        for code in (2, 126, 127):
            self.assertIsNotNone(verdict({}, code, None, False))
        self.assertIsNone(verdict({"expect_exit": [2]}, 2, None, False))
        self.assertIsNotNone(verdict({"expect_exit": [0]}, 1, None, False))
        self.assertIsNotNone(verdict({"expect_nonzero": True}, 0, None, False))
        self.assertIsNotNone(verdict({}, None, "FileNotFoundError", False))
        self.assertIsNotNone(verdict({}, None, None, True))

    def test_normalizer_replaces_the_longest_path_first_and_timestamps(self) -> None:
        norm = common.Normalizer()
        norm.add_path("/work/a", "<WORK>")
        norm.add_path("/work/a/home", "<HOME>")
        self.assertEqual(norm.text("/work/a/home/x at 2026-01-02T03:04:05+00:00 and /work/a/y"), "<HOME>/x at <TS> and <WORK>/y")

    def test_json_paths_names_the_field(self) -> None:
        self.assertEqual(common.json_paths({"a": {"b": 1}, "c": [1, 2]}, {"a": {"b": 2}, "c": [1, 2]}), [{"path": "$.a.b", "old": 1, "new": 2}])

    def test_verdict_codes(self) -> None:
        self.assertEqual(common.verdict_code({"total": 2, "same": 2, "different": 0, "empty": 0}), 0)
        self.assertEqual(common.verdict_code({"total": 2, "same": 1, "different": 1, "empty": 0}), 1)
        self.assertEqual(common.verdict_code({"total": 2, "same": 1, "different": 0, "empty": 1}), 3)
        self.assertEqual(common.verdict_code({"total": 0, "same": 0, "different": 0, "empty": 0}), 3)

    def test_tag_selection_uses_only_formal_tags(self) -> None:
        self.assertIsNotNone(tag_matrix.FORMAL.match("v6.15.0"))
        self.assertIsNone(tag_matrix.FORMAL.match("v6.15.0-rc.1"))
        with self.assertRaises(SystemExit):
            tag_matrix.tag_key("v6.15.0-rc.1")


class RealRepositoryTests(unittest.TestCase):
    def test_compare_runs_on_the_same_ref_is_identical_and_every_command_ran(self) -> None:
        done = run("compare_runs.py", "--old", "HEAD", "--new", "HEAD")
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertGreaterEqual(report["summary"]["same"], 5)
        self.assertEqual(report["summary"]["empty"], 0)
        self.assertTrue(all(entry["exit"]["old"] not in (2, 126, 127) for entry in report["commands"]))

    def test_payload_compare_on_the_same_ref_is_identical_with_output(self) -> None:
        done = run("payload_compare.py", "--old", "HEAD", "--new", "HEAD")
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertGreaterEqual(report["summary"]["same"], 8)
        self.assertEqual(report["summary"]["empty"], 0)


class TagMatrixRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        missing = [tag for tag in TAGS if git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}").returncode != 0]
        if missing and REQUIRE_TAGS:
            raise AssertionError(f"AGENTHELM_REQUIRE_TAGS=1 but tags are missing: {missing}; run `git fetch --tags origin`")
        if missing:
            raise unittest.SkipTest(f"tags {missing} are not in this clone; run `git fetch --tags origin`")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        base = Path(cls.tmp.name)
        latest = json.loads((ROOT / "latest.json").read_text(encoding="utf-8"))
        floor = {name: item["version"] for name, item in latest["plugins"].items()}
        migrations, window = workflow.window_migrations(latest["migrations"], floor)
        cls.windowed = base / "windowed.json"
        cls.windowed.write_text(json.dumps({**latest, "migrations": migrations, "migrations_window": window}), encoding="utf-8")
        lost = [dict(entry, breaking=False) if entry.get("breaking") else entry for entry in migrations]
        cls.lost = base / "lost.json"
        cls.lost.write_text(json.dumps({**latest, "migrations": lost, "migrations_window": window}), encoding="utf-8")

    def matrix(self, latest: Path, *tags: str) -> subprocess.CompletedProcess:
        return run("tag_matrix.py", "--latest", str(latest), "--baseline", str(ROOT / "latest.json"), "--tags", ",".join(tags or TAGS[:1]))

    def test_the_full_file_and_a_windowed_file_pass(self) -> None:
        for latest in (ROOT / "latest.json", self.windowed):
            with self.subTest(latest=latest.name):
                done = self.matrix(latest)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn(" OK ", done.stdout)

    def test_a_file_that_lost_its_breaking_markers_is_reported(self) -> None:
        done = self.matrix(self.lost)  # v6.14.0 does not know the window, so the lost marker changes its verdict
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("verdict mismatch", done.stdout)


class PackagingTests(unittest.TestCase):
    def test_the_tools_stay_out_of_the_npm_package(self) -> None:
        package = json.loads((ROOT / "packages/cli/package.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(package["files"]), ["bin", "runtime"])
        sync = (ROOT / "packages/cli/scripts/sync-runtime.mjs").read_text(encoding="utf-8")
        self.assertNotIn("release", sync.replace("release content", ""))  # the runtime is copied from index.json and plugins/* only

    def test_the_release_tarball_is_a_plain_git_archive_that_excludes_the_tools(self) -> None:
        workflow_text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn('git archive --format=tar.gz --prefix="agenthelm-${tag}/"', workflow_text)
        self.assertNotIn("--worktree-attributes", workflow_text)
        attribute = git("check-attr", "export-ignore", "--", "release/verify/README.md", "release/verify/compare_runs.py")
        self.assertEqual(attribute.stdout.count("export-ignore: set"), 2, attribute.stdout)
        if git("ls-tree", "HEAD", "release/verify").stdout.strip():  # committed: the archive itself must not list it
            listing = subprocess.run("git archive HEAD | tar -t", shell=True, cwd=ROOT, capture_output=True, text=True, check=False).stdout
            self.assertNotIn("release/verify", listing)
            self.assertIn("release/acceptance/mkfixture.py", listing)


if __name__ == "__main__":
    unittest.main()
