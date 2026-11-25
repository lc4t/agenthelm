"""Regression matrix for the ``latest.json`` migration window (#28, docs/design/release-hardening.md section 2.7-1).

Every historical release's own ``agenthelm.py`` (``git archive <tag>``, run in its own directory and process) reads a
windowed ``latest.json`` and the full one; for every plugin and every version that has a migration, the ``breaking``
verdict, the change kinds and the (version, breaking) of each step must be identical. The full history is the real
``release/migrations.json``. The windowed file is the one ``build_latest`` generates today (the real floors) plus two
synthetic floors: the middle of the history and "everything is outside the window" (the most aggressive one).

A second probe runs the real ``check`` command of each tag against the full file, the windowed file next to a
``release/migrations.json`` and the windowed file alone: no crash, no ``None`` in the text, the same exit code, and an
incompatibility mark is never lost (clients before 6.15.0 may only lose the text of entries outside the window;
6.15.0 and later fetch the full text when it is next to ``latest.json`` and say so when it is not).

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
import release_meta  # noqa: E402
import release_workflow as workflow  # noqa: E402

TAGS = ("v6.8.0", "v6.11.0", "v6.12.0", "v6.13.0", "v6.13.1", "v6.14.0", "v6.15.0")
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

E2E = r'''
import contextlib, io, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import agenthelm
full = json.load(open(sys.argv[2]))["migrations"]
variants = dict(zip(("full", "with_full", "alone"), sys.argv[3:6]))
os.environ["PATH"] = tempfile.mkdtemp()
os.environ["AGENTHELM_LANG"] = "zh"
for name, value in (("known_cli_locations", lambda *a, **k: []), ("github_release", lambda *a, **k: {})):
    if hasattr(agenthelm, name):
        setattr(agenthelm, name, value)
MARK = "含不兼容变更"
newer = hasattr(agenthelm, "migrations_window")
cases = sorted({(e["plugin"], e["from"]) for e in full})
mismatches, checked, marked = [], 0, 0
for plugin, version in cases:
    texts = {}
    for label, latest in variants.items():
        home = Path(tempfile.mkdtemp())
        path = home / f".claude/plugins/cache/agenthelm/{plugin}/{version}"
        path.mkdir(parents=True)
        (path / "plugin.json").write_text(json.dumps({"name": plugin, "version": version}))
        (home / ".claude/plugins/installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
            f"{plugin}@agenthelm": [{"scope": "user", "installPath": str(path), "version": version}]}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = agenthelm.main(["check", "--no-project", "--home", str(home), "--latest", latest, "--output", "text"])
        texts[label] = (code, out.getvalue().replace(latest, "<latest>"))
    checked += 1
    base_code, base = texts["full"]
    marked += MARK in base
    for label in ("with_full", "alone"):
        code, text = texts[label]
        problems = []
        if code != base_code:
            problems.append(f"exit code {code} != {base_code}")
        if "None" in text or "Traceback" in text:
            problems.append("None or Traceback in the output")
        if MARK in base and MARK not in text:
            problems.append("lost the incompatibility mark")
        if (MARK in text) != (MARK in base) and not (newer and label == "alone"):
            problems.append("gained an incompatibility mark an unwindowed reader does not show")
        if newer and label == "with_full" and text != base:
            problems.append("did not restore the full text from release/migrations.json")
        if newer and label == "alone" and MARK in text and base_code == 0 and "窗口" not in text and "CHANGELOG" not in text:
            problems.append("no note that the notes outside the window are unavailable")
        if problems:
            mismatches.append([plugin, version, label, problems])
print(json.dumps({"checked": checked, "marked": marked, "mismatches": mismatches}))
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
        built = release_meta.build_latest(release_meta.load_plugins())
        full = json.loads((ROOT / "release/migrations.json").read_text(encoding="utf-8"))
        cls.latest = {**{k: v for k, v in built.items() if k != "migrations_window"}, "migrations": full}
        cls.generated = built
        middle = {}
        for plugin in {entry["plugin"] for entry in full}:
            tos = sorted({tuple(map(int, e["to"].split("."))) for e in full if e["plugin"] == plugin})
            middle[plugin] = ".".join(map(str, tos[len(tos) // 2]))
        everything = {name: item["version"] for name, item in cls.latest["plugins"].items()}
        cls.floors = {"middle": middle, "everything": everything, "generated": built["migrations_window"]["floor"]}

    def windowed(self, name: str) -> dict:
        if name == "generated":
            return self.generated
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

    def test_the_real_check_command_of_every_release_agrees_on_the_generated_window(self) -> None:
        sources = self.base / "e2e"
        (sources / "with_full/release").mkdir(parents=True)
        (sources / "alone").mkdir()
        (sources / "full").mkdir()
        (sources / "full/latest.json").write_text(json.dumps(self.latest, ensure_ascii=False), encoding="utf-8")
        windowed = json.dumps(self.windowed("generated"), ensure_ascii=False)
        (sources / "with_full/latest.json").write_text(windowed, encoding="utf-8")
        (sources / "with_full/release/migrations.json").write_text(json.dumps(self.latest["migrations"], ensure_ascii=False), encoding="utf-8")
        (sources / "alone/latest.json").write_text(windowed, encoding="utf-8")
        (self.base / "e2e.py").write_text(E2E, encoding="utf-8")
        for tag in self.available:
            with self.subTest(tag=tag):
                done = subprocess.run([sys.executable, "-I", str(self.base / "e2e.py"), str(self.base / tag / RUNTIME),
                                       str(sources / "full/latest.json"), *(str(sources / name / "latest.json") for name in ("full", "with_full", "alone"))],
                                      capture_output=True, text=True, check=False)
                self.assertEqual(done.returncode, 0, done.stderr[-2000:])
                result = json.loads(done.stdout.strip().splitlines()[-1])
                self.assertGreater(result["checked"], 30)
                self.assertGreater(result["marked"], 8)
                self.assertEqual(result["mismatches"], [])

    def test_the_generated_window_loses_no_entry_or_breaking_mark_and_keeps_its_text_inside(self) -> None:
        full, built = self.latest["migrations"], self.generated
        self.assertEqual([(e["plugin"], e["from"], e["to"], e["breaking"]) for e in built["migrations"]],
                         [(e["plugin"], e["from"], e["to"], e["breaking"]) for e in full])
        floor = built["migrations_window"]["floor"]
        for entry, original in zip(built["migrations"], full):
            inside = entry["plugin"] not in floor or tuple(map(int, entry["to"].split("."))) > tuple(map(int, floor[entry["plugin"]].split(".")))
            if inside:
                self.assertEqual(entry, original)
            if not inside and entry["breaking"]:
                self.assertTrue(entry["summary"] and len(entry["summary"]) <= 80 and entry["actions"] == [])


if __name__ == "__main__":
    unittest.main()
