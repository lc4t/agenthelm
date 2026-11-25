"""Per-tag regression matrix: does every released client reach the same verdicts from a candidate ``latest.json``?

usage: python3 release/verify/tag_matrix.py --latest FILE --baseline FILE [--since vX.Y.Z] [--tags vA,vB] [--report FILE]
       python3 release/verify/tag_matrix.py --self-test

For every formal release tag (``vX.Y.Z``, taken from the local ``git tag`` list; nothing is fetched) the agent-pack runtime of
that tag is extracted with ``git archive`` and run in its own process on both files. ``--baseline`` is the reference file (the
full ``latest.json``); ``--latest`` is the one under test (for example a windowed one). For every plugin and every version that
appears in the baseline migrations the tag's own ``plugin_findings`` must give the same status, breaking verdict, change kinds
and (version, breaking) of each step, must not raise and must not put the text ``None`` into a finding that the baseline does not have (prose may contain the word).
A tag without the agent-pack runtime or without ``plugin_findings`` is reported as SKIP (it predates the check; not a failure). The tag's ``check``
command is also run offline on both files (it must not crash or print ``None``; the summary verdict must match).

One line per tag, then a summary. Exit codes: 0 every tag passes, 1 a tag fails (or, with AGENTHELM_REQUIRE_TAGS=1, a tag is
missing), 2 bad usage, 3 nothing was verified (no tag available, or the baseline has no breaking entry so the comparison would be
vacuous). A missing tag prints the hint ``git fetch --tags origin``.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402

RUNTIME = "plugins/agent-pack/runtime"
FORMAL = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
FETCH_HINT = "run `git fetch --tags origin` and retry"

PROBE = r'''
import json, os, re, sys
os.environ["AGENTHELM_LANG"] = "zh"
sys.path.insert(0, sys.argv[1])
import agenthelm
if not hasattr(agenthelm, "plugin_findings"):
    print(json.dumps({"skipped": "this release has no plugin_findings"})); sys.exit(0)
baseline, latest = (json.load(open(path)) for path in sys.argv[2:4])
takes_loader = "load_full" in agenthelm.plugin_findings.__code__.co_varnames
versions = {}
for source in (baseline, latest):
    for entry in source.get("migrations", []):
        versions.setdefault(entry["plugin"], set()).update([entry["from"], entry["to"]])
for plugin, item in (baseline.get("plugins") or {}).items():
    versions.setdefault(plugin, set()).add(item["version"])
shape = lambda f: [f["status"], bool(f["breaking"]), f["change_kinds"], [[s["to"], bool(s["breaking"])] for s in f["steps"]]]
def has_none(value):
    if isinstance(value, dict):
        return any(has_none(k) or has_none(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_none(v) for v in value)
    return isinstance(value, str) and re.search(r"\bNone\b", value) is not None
checked, breaking, mismatches, crashes, nones = 0, 0, [], [], []
for plugin, found in sorted(versions.items()):
    for current in sorted(found):
        item = [{"client": "claude", "plugin": plugin, "version": current}]
        try:
            a = agenthelm.plugin_findings(baseline, item)[0]
            b = (agenthelm.plugin_findings(latest, item, lambda: baseline["migrations"]) if takes_loader
                 else agenthelm.plugin_findings(latest, item))[0]
        except Exception as exc:
            crashes.append([plugin, current, type(exc).__name__ + ": " + str(exc)[:200]])
            continue
        checked += 1
        breaking += bool(a["breaking"])
        if shape(a) != shape(b):
            mismatches.append([plugin, current, shape(a), shape(b)])
        if has_none(b) and not has_none(a):  # prose may contain the word; a "None" that only the file under test produces is the bug
            nones.append([plugin, current])
print(json.dumps({"checked": checked, "breaking": breaking, "mismatches": mismatches, "crashes": crashes, "nones": nones}))
'''


def formal_tags(repo: Path) -> list[str]:
    done = common.git("tag", "--list", cwd=repo)
    tags = [line.strip() for line in done.stdout.splitlines() if FORMAL.match(line.strip())]
    return sorted(tags, key=lambda tag: tuple(int(part) for part in FORMAL.match(tag).groups()))


def tag_key(tag: str) -> tuple[int, ...]:
    match = FORMAL.match(tag)
    if not match:
        raise SystemExit(f"{tag!r} is not a formal release tag (vX.Y.Z)")
    return tuple(int(part) for part in match.groups())


def select_tags(repo: Path, since: str | None, explicit: list[str] | None) -> tuple[list[str], list[str]]:
    """(tags to run, tags asked for or implied but missing)."""
    present = formal_tags(repo)
    if explicit:
        return [t for t in explicit if t in present], [t for t in explicit if t not in present]
    if since:
        floor = tag_key(since)
        if since not in present:
            return [t for t in present if tag_key(t) >= floor], [since]
        return [t for t in present if tag_key(t) >= floor], []
    return present, ([] if present else ["(no formal release tag in this clone)"])


def extract_runtime(repo: Path, tag: str, dest: Path) -> None:
    packed = subprocess.run(["git", "archive", "--format=tar", f"refs/tags/{tag}", RUNTIME], cwd=repo, capture_output=True, check=False)
    if packed.returncode != 0:
        raise RuntimeError(f"git archive {tag} failed: {packed.stderr.decode(errors='replace').strip()}")
    with tarfile.open(fileobj=io.BytesIO(packed.stdout)) as handle:
        handle.extractall(dest, filter="data")


def run_check_cli(runtime: Path, latest_file: Path, scratch: Path, label: str) -> dict:
    home = scratch / f"home-{label}"
    home.mkdir(parents=True, exist_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "AGENTHELM_LANG": "en", "LC_ALL": "C.UTF-8",
           "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "PYTHONDONTWRITEBYTECODE": "1"}
    out = {}
    for output in ("json", "text"):
        done = subprocess.run([sys.executable, str(runtime / "agenthelm.py"), "check", "--offline", "--no-project", "--latest", str(latest_file),
                               "--output", output], cwd=scratch, env=env, capture_output=True, text=True, check=False, timeout=120)
        out[output] = done
    result = {"exit": out["json"].returncode, "crash": None, "none": False, "breaking": None}
    for done in out.values():
        if "Traceback" in done.stderr or done.returncode not in (0, 1):
            result["crash"] = (done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1][:200]
    result["none"] = any(re.search(r"\bNone\b", done.stdout) for done in out.values())
    try:
        result["breaking"] = json.loads(out["json"].stdout)["summary"]["breaking"]
    except (ValueError, KeyError, TypeError):
        pass
    return result


def verify_tag(repo: Path, tag: str, baseline: Path, latest: Path, scratch: Path, min_breaking: int) -> dict:
    target = scratch / tag
    extract_runtime(repo, tag, target)
    if not (target / RUNTIME / "agenthelm.py").is_file():
        return {"tag": tag, "status": "skip", "reason": "this release has no agent-pack runtime/agenthelm.py"}
    probe = scratch / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    done = subprocess.run([sys.executable, "-I", str(probe), str(target / RUNTIME), str(baseline), str(latest)], capture_output=True, text=True,
                          check=False, timeout=300)
    if done.returncode != 0 or not done.stdout.strip():
        return {"tag": tag, "status": "fail", "reason": "probe crashed: " + (done.stderr.strip().splitlines() or ["no output"])[-1][:300]}
    result = json.loads(done.stdout.strip().splitlines()[-1])
    if "skipped" in result:
        return {"tag": tag, "status": "skip", "reason": result["skipped"]}
    cli_base = run_check_cli(target / RUNTIME, baseline, scratch, f"{tag}-base")
    cli_test = run_check_cli(target / RUNTIME, latest, scratch, f"{tag}-latest")
    problems = []
    if result["mismatches"]:
        first = result["mismatches"][0]
        problems.append(f"{len(result['mismatches'])} verdict mismatch(es), first {first[0]}@{first[1]}: baseline {first[2][:2]} vs latest {first[3][:2]}")
    if result["crashes"]:
        problems.append(f"{len(result['crashes'])} crash(es): {result['crashes'][0][2]}")
    if result["nones"]:
        problems.append(f"{len(result['nones'])} finding(s) contain 'None'")
    if cli_test["crash"]:
        problems.append("check crashed on latest: " + cli_test["crash"])
    if cli_test["none"] and not cli_base["none"]:
        problems.append("check output contains 'None' only with the latest file")
    if cli_base["breaking"] != cli_test["breaking"]:
        problems.append(f"check summary.breaking differs: baseline {cli_base['breaking']} vs latest {cli_test['breaking']}")
    if problems:
        status = "fail"
    elif result["checked"] == 0 or result["breaking"] < min_breaking:
        status, problems = "empty", [f"vacuous: checked={result['checked']} breaking={result['breaking']} (min_breaking={min_breaking})"]
    else:
        status = "ok"
    return {"tag": tag, "status": status, "checked": result["checked"], "breaking": result["breaking"], "problems": problems,
            "mismatches": result["mismatches"][:10], "crashes": result["crashes"][:10], "nones": result["nones"][:10],
            "check": {"baseline": cli_base, "latest": cli_test}}


def run_matrix(repo: Path, baseline: Path, latest: Path, since: str | None = None, tags: list[str] | None = None, min_breaking: int = 1,
               require: bool | None = None) -> dict:
    require = os.environ.get(common.ENV_REQUIRE_TAGS) == "1" if require is None else require
    chosen, missing = select_tags(repo, since, tags)
    scratch = common.new_tmp("tag-matrix-")
    rows = []
    try:
        for tag in chosen:
            try:
                rows.append(verify_tag(repo, tag, baseline, latest, scratch, min_breaking))
            except Exception as exc:  # noqa: BLE001 - one broken tag must not hide the rest
                rows.append({"tag": tag, "status": "fail", "reason": f"{type(exc).__name__}: {exc}"})
    finally:
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)
    skipped = [r for r in rows if r["status"] == "skip"]
    rows_run = [r for r in rows if r["status"] != "skip"]
    failed = [r for r in rows if r["status"] == "fail"]
    empty = [r for r in rows if r["status"] == "empty"]
    if failed or (missing and require):
        code = 1
    elif not rows_run or empty or missing:
        code = 3
    else:
        code = 0
    return {"schema": 1, "tool": "tag_matrix", "tags": rows, "missing": missing, "require_tags": require, "exit_code": code,
            "summary": {"total": len(rows), "skip": len(skipped), "ok": sum(r["status"] == "ok" for r in rows), "fail": len(failed), "empty": len(empty),
                        "missing": len(missing)}}


def render(report: dict) -> str:
    lines = []
    for row in report["tags"]:
        detail = f"checked={row['checked']} breaking={row['breaking']}" if "checked" in row else row.get("reason", "")
        if row.get("problems"):
            detail += "  " + "; ".join(row["problems"])
        lines.append(f"{row['tag']:10} {row['status'].upper():5} {detail}")
    for tag in report["missing"]:
        lines.append(f"{tag}  MISSING - {FETCH_HINT}" + ("  (AGENTHELM_REQUIRE_TAGS=1: failure)" if report["require_tags"] else ""))
    s = report["summary"]
    verdict = {0: "PASS", 1: "FAIL", 3: "NOTHING VERIFIED"}[report["exit_code"]]
    lines.append(f"tag_matrix: {verdict} - {s['total']} tags, {s['ok']} ok, {s['skip']} skipped, {s['fail']} fail, {s['empty']} vacuous, {s['missing']} missing")
    return "\n".join(lines)


# ---------------------------------------------------------------- self test

FAKE_RUNTIME = r'''
import json, sys
def _key(v):
    return tuple(int(p) for p in str(v).split("."))
def plugin_findings(latest, installed, load_full=None):
    out = []
    for item in installed:
        steps = [e for e in latest.get("migrations", []) if e["plugin"] == item["plugin"] and _key(e["to"]) > _key(item["version"])]
        out.append({"status": "behind" if steps else "current", "breaking": any(e.get("breaking") for e in steps),
                    "change_kinds": sorted({e.get("kind", "") for e in steps}),
                    "steps": [{"to": e["to"], "breaking": bool(e.get("breaking"))} for e in steps]})
    return out
if __name__ == "__main__":
    args = sys.argv[1:]
    path = args[args.index("--latest") + 1]
    data = json.load(open(path))
    breaking = any(e.get("breaking") for e in data.get("migrations", []))
    print(json.dumps({"ok": True, "summary": {"breaking": breaking}}) if "json" in args else "breaking: " + str(breaking))
'''


def _migrations() -> list[dict]:
    rows = []
    for i, (frm, to, breaking) in enumerate([("1.0.0", "1.1.0", False), ("1.1.0", "2.0.0", True), ("2.0.0", "2.1.0", False), ("2.1.0", "3.0.0", True)]):
        rows.append({"plugin": "demo", "from": frm, "to": to, "breaking": breaking, "kind": "layout" if breaking else "doc",
                     "summary": "s" * 10, "actions": ["do it"] if breaking else []})
    return rows


def self_test() -> list[str]:
    failures: list[str] = []
    base = common.new_tmp("tag-matrix-self-")
    import shutil
    try:
        repo = base / "repo"
        common.write_tree(repo, {RUNTIME + "/agenthelm.py": FAKE_RUNTIME})
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        for command in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "fake runtime"], ["tag", "v1.0.0"], ["tag", "v1.1.0"], ["tag", "v1.1.0-rc.1"]):
            subprocess.run(["git", *command], cwd=repo, env=env, check=True, capture_output=True)
        full = {"plugins": {"demo": {"version": "3.0.0"}}, "migrations": _migrations()}
        windowed = {**full, "migrations": [dict(e, summary="", actions=[]) if not e["breaking"] else e for e in full["migrations"]]}
        lost = {**full, "migrations": [dict(e, breaking=False) if e["to"] == "2.0.0" else e for e in full["migrations"]]}
        no_breaking = {"plugins": {"demo": {"version": "3.0.0"}}, "migrations": [dict(e, breaking=False) for e in full["migrations"]]}
        files = {}
        for name, data in (("full", full), ("windowed", windowed), ("lost", lost), ("nobreak", no_breaking)):
            files[name] = base / f"{name}.json"
            files[name].write_text(json.dumps(data), encoding="utf-8")

        def check(label, latest_file, baseline_file, expect, **kwargs):
            report = run_matrix(repo, baseline_file, latest_file, **kwargs)
            if report["exit_code"] != expect:
                failures.append(f"{label}: expected exit {expect}, got {report['exit_code']}\n{render(report)}")
            return report

        report = check("windowed-passes", files["windowed"], files["full"], 0, require=False)
        if [r["tag"] for r in report["tags"]] != ["v1.0.0", "v1.1.0"]:
            failures.append(f"windowed-passes: only formal tags expected, got {[r['tag'] for r in report['tags']]}")
        report = check("lost-breaking-marker", files["lost"], files["full"], 1, require=False)
        if not any("mismatch" in p for r in report["tags"] for p in r.get("problems", [])):
            failures.append("lost-breaking-marker: the missing marker was not reported as a verdict mismatch")
        check("vacuous-baseline", files["nobreak"], files["nobreak"], 3, require=False)
        check("since-filter", files["windowed"], files["full"], 0, since="v1.1.0", require=False)
        report = check("missing-tag-soft", files["windowed"], files["full"], 3, tags=["v9.9.9"], require=False)
        if not report["missing"] or FETCH_HINT.split(" and")[0] not in render(report):
            failures.append("missing-tag-soft: the fetch hint is not printed")
        check("missing-tag-required", files["windowed"], files["full"], 1, tags=["v1.0.0", "v9.9.9"], require=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--latest", type=Path, help="latest.json under test (for example a windowed one)")
    parser.add_argument("--baseline", type=Path, help="reference latest.json (the full one)")
    parser.add_argument("--since", help="only tags >= this one (vX.Y.Z)")
    parser.add_argument("--tags", help="comma separated list of tags instead of all formal tags")
    parser.add_argument("--min-breaking", type=int, default=1, help="a tag whose baseline has fewer breaking verdicts is a vacuous comparison (default 1)")
    parser.add_argument("--repo", type=Path, default=common.REPO, help="git repository holding the tags (default: this one)")
    parser.add_argument("--report", type=Path, help="also write the JSON report here")
    parser.add_argument("--self-test", action="store_true", help="check the tool against a synthetic repository, including a latest.json that lost a breaking marker")
    args = parser.parse_args(argv)
    if args.self_test:
        failures = self_test()
        for line in failures:
            print("self-test FAIL:", line, file=sys.stderr)
        print("tag_matrix self-test: " + ("FAILED" if failures else "ok"), file=sys.stderr)
        return 1 if failures else 0
    if not args.latest or not args.baseline:
        parser.error("--latest and --baseline are required")
    for path in (args.latest, args.baseline):
        if not path.is_file():
            print(f"tag_matrix: {path} is not a file", file=sys.stderr)
            return 2
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
    report = run_matrix(args.repo.resolve(), args.baseline.resolve(), args.latest.resolve(), args.since, tags, args.min_breaking)
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(render(report))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
