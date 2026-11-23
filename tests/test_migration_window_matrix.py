"""Regression matrix for the ``latest.json`` migration window (#28, docs/design/release-hardening.md section 2.7-1).

Every historical release's own ``agenthelm.py`` (``git archive <tag>``, run in its own directory and process) reads a
windowed ``latest.json`` and the full one; for every plugin and every version that has a migration, the ``breaking``
verdict, the change kinds and the (version, breaking) of each step must be identical. The windowed file is built by
``release_workflow.window_migrations`` from the real ``release/migrations.json``, with two floors: the middle of the
history and "everything is outside the window" (the most aggressive one).

Tags missing from the clone skip the test; ``AGENTHELM_REQUIRE_TAGS=1`` (CI) makes that a failure.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_workflow as workflow  # noqa: E402

TAGS = ("v6.8.0", "v6.11.0", "v6.12.0", "v6.13.0", "v6.13.1", "v6.14.0")
RUNTIME = "plugins/agent-pack/runtime"
REQUIRE_TAGS = os.environ.get("AGENTHELM_REQUIRE_TAGS") == "1"

PROBE = r'''
import json, os, sys
os.environ["AGENTHELM_LANG"] = "zh"
sys.path.insert(0, sys.argv[1])
import agenthelm
full, windowed = (json.load(open(path)) for path in sys.argv[2:4])
takes_loader = "load_full" in agenthelm.plugin_findings.__code__.co_varnames
versions = {}
for entry in full["migrations"]:
    versions.setdefault(entry["plugin"], set()).update([entry["from"], entry["to"]])
checked, breaking, mismatches = 0, 0, []
for plugin, found in sorted(versions.items()):
    for current in sorted(found):
        item = [{"client": "claude", "plugin": plugin, "version": current}]
        a = agenthelm.plugin_findings(full, item)[0]
        b = (agenthelm.plugin_findings(windowed, item, lambda: full["migrations"]) if takes_loader
             else agenthelm.plugin_findings(windowed, item))[0]
        shape = lambda f: (f["status"], f["breaking"], f["change_kinds"], [(s["to"], bool(s["breaking"])) for s in f["steps"]])
        checked += 1
        breaking += bool(a["breaking"])
        if shape(a) != shape(b):
            mismatches.append([plugin, current, shape(a), shape(b)])
print(json.dumps({"checked": checked, "breaking": breaking, "mismatches": mismatches}))
'''


def git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)


def tag_problem(tag: str) -> str | None:
    if git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}").returncode != 0:
        return f"release tag {tag} is not in this clone; run `git fetch --tags origin`"
    return None


class WindowMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        missing = {tag: tag_problem(tag) for tag in TAGS}
        missing = {tag: reason for tag, reason in missing.items() if reason}
        cls.available = [tag for tag in TAGS if tag not in missing]
        if missing and REQUIRE_TAGS:
            raise AssertionError("AGENTHELM_REQUIRE_TAGS=1 but release tags are missing: " + "; ".join(missing.values()))
        if not cls.available:
            raise unittest.SkipTest("no historical release tag is available. " + next(iter(missing.values())))
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.base = Path(cls.tmp.name).resolve()
        (cls.base / "probe.py").write_text(PROBE, encoding="utf-8")
        for tag in cls.available:
            archive = git("archive", "--format=tar", tag, RUNTIME)
            if archive.returncode != 0:
                raise RuntimeError(f"git archive {tag} failed: {archive.stderr.decode(errors='replace')}")
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as handle:
                handle.extractall(cls.base / tag, filter="data")
        cls.latest = json.loads((ROOT / "latest.json").read_text(encoding="utf-8"))
        full = cls.latest["migrations"]
        middle = {}
        for plugin in {entry["plugin"] for entry in full}:
            tos = sorted({tuple(map(int, e["to"].split("."))) for e in full if e["plugin"] == plugin})
            middle[plugin] = ".".join(map(str, tos[len(tos) // 2]))
        everything = {name: item["version"] for name, item in cls.latest["plugins"].items()}
        cls.floors = {"middle": middle, "everything": everything}

    def windowed(self, name: str) -> dict:
        migrations, window = workflow.window_migrations(self.latest["migrations"], self.floors[name])
        return {**self.latest, "migrations": migrations, "migrations_window": window}

    def test_every_released_client_reaches_the_same_breaking_verdicts(self) -> None:
        for name in self.floors:
            files = {}
            for label, data in (("full", self.latest), ("windowed", self.windowed(name))):
                files[label] = self.base / f"{name}-{label}.json"
                files[label].write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            for tag in self.available:
                with self.subTest(floor=name, tag=tag):
                    done = subprocess.run([sys.executable, "-I", str(self.base / "probe.py"), str(self.base / tag / RUNTIME),
                                           str(files["full"]), str(files["windowed"])], capture_output=True, text=True, check=False)
                    self.assertEqual(done.returncode, 0, done.stderr[-2000:])
                    result = json.loads(done.stdout.strip().splitlines()[-1])
                    self.assertGreater(result["checked"], 40)
                    self.assertGreater(result["breaking"], 10)
                    self.assertEqual(result["mismatches"], [])

    def test_the_current_client_with_the_full_file_matches_and_without_it_reports_a_gap(self) -> None:
        sys.path.insert(0, str(ROOT / RUNTIME))
        import agenthelm  # noqa: PLC0415
        windowed = self.windowed("everything")
        item = [{"client": "claude", "plugin": "agents-init", "version": "6.4.0"}]
        with_full = agenthelm.plugin_findings(windowed, item, lambda: self.latest["migrations"])[0]
        self.assertEqual(with_full["steps"], agenthelm.plugin_findings(self.latest, item)[0]["steps"])
        without = agenthelm.plugin_findings(windowed, item)[0]
        self.assertTrue(without["breaking"])
        self.assertEqual(sum(1 for step in without["steps"] if step.get("gap")), 1)


if __name__ == "__main__":
    unittest.main()
