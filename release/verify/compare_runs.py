"""Run the same fixture and the same commands against an old and a new tree and report every observable difference.

usage: python3 release/verify/compare_runs.py [--old origin/dev] [--new HEAD] [--manifest FILE] [--report FILE]
       python3 release/verify/compare_runs.py --self-test

Each side gets its own private HOME per command. Compared: standard output, standard error, exit code, the argv every stub
client (a fake script on PATH) received, and the content hash of the files left in HOME and in the project afterwards.
Timestamps and temporary paths are normalised (see ``normalize`` in the manifest). Exit codes: 0 identical, 1 different,
2 bad usage, 3 empty comparison (a command did not really run on either side, for example both sides rejected its
arguments). A JSON report goes to standard output (or ``--report FILE``) and one human summary line to standard error.
"""
from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402

DEFAULT_MANIFEST = Path(__file__).resolve().parent / "manifests" / "compare_runs.json"
ACCEPTANCE = common.REPO / "release" / "acceptance" / "mkfixture.py"


def load_manifest(path: Path) -> dict:
    data = runpy.run_path(str(path))["MANIFEST"] if path.suffix == ".py" else json.loads(path.read_text(encoding="utf-8"))
    commands = data.get("commands")
    if not isinstance(commands, list) or not commands:
        raise SystemExit(f"{path}: 'commands' must be a non-empty list")
    names = set()
    for item in commands:
        if not isinstance(item.get("name"), str) or not isinstance(item.get("argv"), list) or not item["argv"]:
            raise SystemExit(f"{path}: every command needs a 'name' and a non-empty 'argv': {item!r}")
        if item["name"] in names:
            raise SystemExit(f"{path}: duplicate command name {item['name']!r}")
        names.add(item["name"])
    return data


def mkfixture_factory(cache: Path, package: Path):
    """Fixtures come from release/acceptance/mkfixture.py, built once per kind from ``package`` and copied per side."""
    built: dict[str, Path] = {}

    def factory(spec: dict) -> Path | None:
        kind = spec["fixture"]
        if kind not in built:
            import subprocess
            done = subprocess.run([sys.executable, "-I", str(ACCEPTANCE), kind, str(cache / kind), str(package)], capture_output=True, text=True,
                                  check=False)
            if done.returncode != 0:
                raise SystemExit(f"mkfixture {kind} failed: {done.stderr.strip()}")
            built[kind] = Path(done.stdout.strip().splitlines()[-1])
        return built[kind]
    return factory


def compare_trees(old_tree: Path, new_tree: Path, commands: list[dict], *, normalize: dict | None = None, latest: Path | None = None,
                  fixture_factory=None, fixture_cache: Path | None = None, new_stub_overrides: dict[str, str] | None = None,
                  extra_env: dict[str, str] | None = None) -> dict:
    names, regex = common.config_normalizer(normalize)
    norm = common.Normalizer(regex=regex)
    if fixture_cache is not None:
        norm.add_path(fixture_cache, names["fixture"])
    scratch = common.new_tmp("compare-runs-")
    import shutil
    try:
        # Both sides run in the very same directory (the old one is removed first): a program that hashes or derives names from
        # the project path then behaves identically, so path-dependent output is not reported as a difference.
        work = scratch / "run"
        old = common.run_side("old", old_tree, work, commands, norm, fixture_factory=fixture_factory, latest=latest, extra_env=extra_env)
        shutil.rmtree(work)
        new = common.run_side("new", new_tree, work, commands, norm, fixture_factory=fixture_factory, latest=latest,
                              stub_overrides=new_stub_overrides, extra_env=extra_env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    entries = common.compare_side_results(old, new)
    counts = common.summarize(entries)
    return {"schema": 1, "tool": "compare_runs", "commands": entries, "summary": counts, "exit_code": common.verdict_code(counts)}


# ---------------------------------------------------------------- self test

SYNTHETIC = r'''
import argparse, os, sys, subprocess, time
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("mode")
parser.add_argument("--flag", action="store_true")
args = parser.parse_args()
home = Path(os.environ["HOME"])
if args.mode == "hello":
    print("hello from", __file__, time.strftime("%Y-%m-%dT%H:%M:%S"))
    print("home is", home)
elif args.mode == "write":
    (home / "state.txt").write_text("state " + str(home) + "\n")
    print("wrote")
elif args.mode == "stub":
    subprocess.run(["fakeclient", "--do", "thing", str(home)], check=True)
    print("stub done")
elif args.mode == "fail":
    print("failing", file=sys.stderr)
    sys.exit(1)
'''


def _synthetic_tree(root: Path, *, stdout_char: bool = False, extra_file: bool = False, exit_code: int | None = None, stub_extra: bool = False) -> None:
    script = SYNTHETIC
    if stdout_char:
        script = script.replace('print("wrote")', 'print("wrote!")')
    if extra_file:
        script = script.replace('print("wrote")', '(home / "extra.txt").write_text("x"); print("wrote")')
    if exit_code is not None:
        script = script.replace("sys.exit(1)", f"sys.exit({exit_code})")
    if stub_extra:
        script = script.replace('"thing"', '"thing", "more"')
    common.write_tree(root, {"plugins/agent-pack/runtime/tool.py": script})


SELF_COMMANDS = [
    {"name": "hello", "argv": ["python3", "{runtime}/tool.py", "hello"]},
    {"name": "write", "argv": ["python3", "{runtime}/tool.py", "write"]},
    {"name": "stub", "argv": ["python3", "{runtime}/tool.py", "stub"], "stubs": ["fakeclient"]},
    {"name": "fail", "argv": ["python3", "{runtime}/tool.py", "fail"], "expect_nonzero": True},
]


def self_test() -> list[str]:
    """Return the list of failed expectations (empty when the tool does what it promises)."""
    failures: list[str] = []
    base = common.new_tmp("compare-runs-self-")
    import shutil

    def case(label, expect_code, expect_status=None, commands=SELF_COMMANDS, **new_kwargs):
        old_tree, new_tree = base / f"{label}-old", base / f"{label}-new"
        _synthetic_tree(old_tree)
        _synthetic_tree(new_tree, **{k: v for k, v in new_kwargs.items() if k != "stub_overrides"})
        report = compare_trees(old_tree, new_tree, commands, new_stub_overrides=new_kwargs.get("stub_overrides"))
        statuses = {entry["name"]: entry["status"] for entry in report["commands"]}
        if report["exit_code"] != expect_code:
            failures.append(f"{label}: expected exit {expect_code}, got {report['exit_code']} ({statuses})")
        for name, status in (expect_status or {}).items():
            if statuses.get(name) != status:
                failures.append(f"{label}: {name} expected {status}, got {statuses.get(name)}")
        return report

    try:
        # "hello" prints its own temporary path and the current time: identical only because of the normalisation
        case("identical", 0, {"hello": "same", "write": "same", "stub": "same", "fail": "same"})
        report = case("stdout-char", 1, {"write": "different", "hello": "same"}, stdout_char=True)
        if "stdout" not in next(e for e in report["commands"] if e["name"] == "write")["differences"]:
            failures.append("stdout-char: the injected character was not reported under stdout")
        report = case("extra-file", 1, {"write": "different"}, extra_file=True)
        if not next(e for e in report["commands"] if e["name"] == "write")["differences"].get("tree", {}).get("added"):
            failures.append("extra-file: the extra file was not reported under tree.added")
        report = case("exit-code", 1, {"fail": "different"}, exit_code=3)
        report = case("stub-argv", 1, {"stub": "different"}, stub_extra=True)
        one = [{"name": "stub", "argv": ["python3", "{runtime}/tool.py", "stub"], "stubs": ["fakeclient"]}]
        noisy = STUB_NOISY
        report = case("stub-output", 1, {"stub": "different"}, commands=one, stub_overrides={"fakeclient": noisy})
        # a command both sides reject (argparse error, exit 2) is an empty comparison, never "same"
        bad = [{"name": "bad-flag", "argv": ["python3", "{runtime}/tool.py", "hello", "--latest"]}]
        report = case("argparse-both", 3, {"bad-flag": "empty"}, commands=bad)
        missing = [{"name": "missing", "argv": ["no-such-command-for-compare-runs"]}]
        report = case("missing-command", 3, {"missing": "empty"}, commands=missing)
        explicit = [{"name": "bad-flag", "argv": ["python3", "{runtime}/tool.py", "hello", "--latest"], "expect_exit": [2]}]
        report = case("explicit-exit", 0, {"bad-flag": "same"}, commands=explicit)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return failures


STUB_NOISY = '#!' + sys.executable + '\nimport sys\nsys.stdout.write("x")\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--old", default="origin/dev", help="old ref (default origin/dev)")
    parser.add_argument("--new", default="HEAD", help="new ref (default HEAD)")
    parser.add_argument("--old-dir", type=Path, help="use this directory as the old tree instead of a ref")
    parser.add_argument("--new-dir", type=Path, help="use this directory as the new tree instead of a ref (for example '.' for uncommitted work)")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="JSON (or .py defining MANIFEST) list of commands")
    parser.add_argument("--latest", type=Path, help="latest.json copied for the {latest} token (default: the one of the new tree)")
    parser.add_argument("--report", type=Path, help="write the JSON report here instead of standard output")
    parser.add_argument("--self-test", action="store_true", help="inject differences and empty comparisons and check they are reported")
    args = parser.parse_args(argv)
    if args.self_test:
        failures = self_test()
        for line in failures:
            print("self-test FAIL:", line, file=sys.stderr)
        print("compare_runs self-test: " + ("FAILED" if failures else "ok"), file=sys.stderr)
        return 1 if failures else 0
    manifest = load_manifest(args.manifest)
    scratch = common.new_tmp("compare-runs-trees-")
    import shutil
    try:
        refs = {}
        for label, ref, directory in (("old", args.old, args.old_dir), ("new", args.new, args.new_dir)):
            if directory is not None:
                refs[label] = (directory.resolve(), str(directory), None)
            else:
                tree = scratch / label
                refs[label] = (tree, ref, common.materialize(ref, tree))
        latest = args.latest or (refs["new"][0] / "latest.json")
        cache = scratch / "fixtures"
        report = compare_trees(refs["old"][0], refs["new"][0], manifest["commands"], normalize=manifest.get("normalize"), latest=latest,
                               fixture_factory=mkfixture_factory(cache, refs["new"][0]), fixture_cache=cache)
        report["old"], report["new"] = ({"ref": refs[s][1], "commit": refs[s][2]} for s in ("old", "new"))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    print(common.short_status(report["commands"]), file=sys.stderr)
    print(common.summary_line("compare_runs", report["summary"], report["exit_code"], f"[{args.old} -> {args.new}]"), file=sys.stderr)
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
