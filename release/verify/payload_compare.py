"""Feed the same hook payloads to the hook entry of an old and a new tree and compare what comes back.

usage: python3 release/verify/payload_compare.py [--old origin/dev] [--new HEAD] [--cases FILE_OR_DIR] [--client NAME ...] [--report FILE]
       python3 release/verify/payload_compare.py --self-test

The entry is ``python3 <tree>/plugins/agent-pack/runtime/agenthelm.py nudge hook --client <client> --event <event>`` with the
payload JSON on standard input (see ``agent_pack_nudge.py``); it always exits 0 and prints one JSON object only when it has
something to say. Compared per case: the standard output parsed as JSON (field by field), the exit code, standard error and the
files left in HOME and in the project. Every side runs with a private HOME in the same directory layout.

Without ``--cases`` the cases are generated from the ``hooks`` declaration of every driver in ``runtime/drivers/*.json`` of the
new tree: for each declared event a payload that uses exactly the project-directory and session keys the driver declares, in a
project that has a pending opinion candidate, so that a working hook must answer. A cases file is a JSON list (or a directory of
``*.json`` files, each one case or a list) of::

  {"name": "...", "client": "claude", "event": "session-start", "payload": {...}, "expect_output": true, "env": {...}}

``{project}`` inside a payload is replaced by the project directory; ``expect_output`` false means silence is a valid answer.

A hook that finds no project, or is given an unknown event, prints nothing and succeeds, which makes "both sides silent" look like
"both sides identical". That is why every case must either produce output on a side or say ``expect_output: false``; otherwise the
comparison is reported as EMPTY. Exit codes: 0 identical, 1 different, 2 bad usage, 3 empty comparison.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402

RUNTIME_ENTRY = "{runtime}/agenthelm.py"
EVENT_PAYLOADS = {
    "session-start": {"source": "startup"},
    "prompt-submit": {"prompt": "from now on, answer first"},
    "stop": {"last_assistant_message": "The change is done.", "status": "completed"},
}
OPINION_MANAGER = common.REPO / "plugins" / "opinion-manager" / "runtime" / "opinion_manager.py"


def hook_command(case: dict) -> dict:
    payload = json.loads(json.dumps(case["payload"]))
    return {"name": case["name"], "argv": ["python3", RUNTIME_ENTRY, "nudge", "hook", "--client", case["client"], "--event", case["event"]],
            "stdin": payload, "fixture": case.get("fixture", case["client"]), "stdout_json": True, "env": case.get("env") or {},
            "expect_output": case.get("expect_output", True), "expect_exit": [0], "session": case.get("session", "")}


def default_cases(tree: Path, only: list[str] | None = None) -> list[dict]:
    cases = []
    for path in sorted((tree / "plugins/agent-pack/runtime/drivers").glob("*.json")):
        driver = json.loads(path.read_text(encoding="utf-8"))
        hooks = driver.get("hooks") or {}
        client = path.stem
        if not hooks.get("events") or (only and client not in only):
            continue
        declared = hooks.get("payload") or {}
        for event in hooks["events"]:
            body = dict(EVENT_PAYLOADS.get(event, {}))
            session = f"verify-{client}"
            project_keys, session_keys = declared.get("project_dir") or [], declared.get("session") or []
            if project_keys:
                body[project_keys[0]] = "{project}"  # the hook accepts a string or a list for the keys a driver declares
            else:
                body["cwd"] = "{project}"
            body[session_keys[0] if session_keys else "session_id"] = session
            if not hooks.get("stop_status_gated"):
                body.pop("status", None)
            silent = event == "stop" and hooks.get("stop_prompt") == "off" or event not in EVENT_PAYLOADS
            cases.append({"name": f"{client}-{event}", "client": client, "event": event, "payload": body, "session": session,
                          "expect_output": not silent})
    return cases


def load_cases(source: Path) -> list[dict]:
    files = sorted(source.glob("*.json")) if source.is_dir() else [source]
    cases: list[dict] = []
    for file in files:
        data = json.loads(file.read_text(encoding="utf-8"))
        for item in data if isinstance(data, list) else [data]:
            for key in ("name", "client", "event", "payload"):
                if key not in item:
                    raise SystemExit(f"{file}: a case needs {key!r}: {item!r}")
            cases.append(item)
    if not cases:
        raise SystemExit(f"{source}: no cases")
    return cases


def project_fixture_factory(cache: Path, seed: bool, cases: dict[str, dict]):
    """One opinion project per case session: ``.opinion/``, a git repository, and (when ``seed``) one pending candidate added with
    the repository's opinion-manager, so session-start / stop have something to report. The same files are copied to both sides."""
    built: dict[str, Path] = {}

    def factory(spec: dict) -> Path | None:
        key = spec["name"]
        if key not in built:
            root = cache / key / "project"
            (root / ".opinion").mkdir(parents=True)
            (root / ".opinion" / "keep").write_text("", encoding="utf-8")
            (root / ".gitignore").write_text(".opinion/\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            session = cases[key].get("session") or "s1"
            if seed:
                done = subprocess.run([sys.executable, str(OPINION_MANAGER), "inbox", "add", "--project", str(root), "--title", "Answer first",
                                       "--text", "Answer first, then explain.", "--scope", "answers", "--quote", "answer first", "--session", session],
                                      capture_output=True, text=True, check=False)
                if done.returncode != 0:
                    raise SystemExit(f"cannot seed the fixture project: {done.stderr.strip()[:300]}")
            built[key] = root
        return built[key]
    return factory


def compare_hooks(old_tree: Path, new_tree: Path, cases: list[dict], *, seed: bool = True, new_extra_env: dict | None = None,
                  factory=None) -> dict:
    import shutil
    commands = [hook_command(case) for case in cases]
    by_name = {case["name"]: case for case in cases}
    scratch = common.new_tmp("payload-compare-")
    try:
        cache = scratch / "fixtures"
        norm = common.Normalizer()
        norm.add_path(cache, "<FIXTURE>")
        factory = factory or project_fixture_factory(cache, seed, by_name)
        work = scratch / "run"
        old = common.run_side("old", old_tree, work, commands, norm, fixture_factory=factory)
        shutil.rmtree(work)
        new = common.run_side("new", new_tree, work, commands, norm, fixture_factory=factory, extra_env=new_extra_env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    entries = common.compare_side_results(old, new)
    counts = common.summarize(entries)
    return {"schema": 1, "tool": "payload_compare", "commands": entries, "summary": counts, "exit_code": common.verdict_code(counts)}


# ---------------------------------------------------------------- self test

FAKE_ENTRY = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] != ["nudge", "hook"] or "--event" not in args or "--client" not in args:
    sys.exit(2)
event = args[args.index("--event") + 1]
if event not in ("session-start", "prompt-submit", "stop"):
    sys.exit(2)
payload = json.loads(sys.stdin.read() or "{}")
project = payload.get("cwd") or (payload.get("workspacePaths") or [None])[0]
if not project or not (Path(project) / ".opinion").is_dir():
    sys.exit(0)  # silent, exactly like the real hook outside an opinion project
state = Path(os.environ["HOME"]) / ".local" / "state" / "fake"
state.mkdir(parents=True, exist_ok=True)
(state / "seen").write_text(payload.get("session_id", "") + "\n")
out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": "VARIANT"}}
print(json.dumps(out))
'''


def _fake_tree(root: Path, variant: str = "") -> None:
    script = FAKE_ENTRY.replace("VARIANT", "context")
    if variant == "field":
        script = script.replace('"additionalContext": "context"', '"additionalContext": "contex"')
    elif variant == "extra-key":
        script = script.replace('out = {', 'out = {"systemMessage": "x", ')
    elif variant == "file":
        script = script.replace('print(json.dumps(out))', '(state / "extra").write_text("x"); print(json.dumps(out))')
    elif variant == "exit":
        script = script.replace('print(json.dumps(out))', 'print(json.dumps(out)); sys.exit(1)')
    common.write_tree(root, {"plugins/agent-pack/runtime/agenthelm.py": script})


def self_test() -> list[str]:
    failures: list[str] = []
    base = common.new_tmp("payload-compare-self-")
    import shutil

    def factory_for(root: Path):
        def make(spec: dict) -> Path:
            target = root / spec["name"] / "project"
            (target / ".opinion").mkdir(parents=True, exist_ok=True)
            return target
        return make

    cases = [{"name": "claude-start", "client": "claude", "event": "session-start", "payload": {"cwd": "{project}", "session_id": "s"}},
             {"name": "agy-start", "client": "agy", "event": "session-start", "payload": {"workspacePaths": ["{project}"], "conversationId": "c"}}]

    def case(label, expect, variant="", cases=cases, expect_status=None):
        old, new = base / f"{label}-old", base / f"{label}-new"
        _fake_tree(old)
        _fake_tree(new, variant)
        report = compare_hooks(old, new, cases, factory=factory_for(base / f"{label}-fx"))
        if report["exit_code"] != expect:
            failures.append(f"{label}: expected exit {expect}, got {report['exit_code']}: {[(e['name'], e['status']) for e in report['commands']]}")
        for entry in report["commands"]:
            if expect_status and entry["status"] != expect_status:
                failures.append(f"{label}: {entry['name']} expected {expect_status}, got {entry['status']}")
        return report

    try:
        case("identical", 0, expect_status="same")
        report = case("field", 1, "field", expect_status="different")
        if not any(f["path"].endswith("additionalContext") for e in report["commands"] for f in e["differences"].get("stdout", {}).get("fields", [])):
            failures.append("field: the changed JSON field was not named in the report")
        report = case("extra-key", 1, "extra-key", expect_status="different")
        case("extra-file", 1, "file", expect_status="different")
        case("exit-code", 1, "exit", expect_status="different")
        # silence on both sides (the payload names no project) is an empty comparison, not "identical"
        silent = [{"name": "no-project", "client": "claude", "event": "session-start", "payload": {"session_id": "s"}}]
        case("silent-both", 3, cases=silent, expect_status="empty")
        # an event the hook does not know is an argument error on both sides
        unknown = [{"name": "bad-event", "client": "claude", "event": "no-such-event", "payload": {"cwd": "{project}"}}]
        case("unknown-event", 3, cases=unknown, expect_status="empty")
        # silence that is declared expected is a valid comparison
        declared = [{"name": "no-project", "client": "claude", "event": "session-start", "payload": {"session_id": "s"}, "expect_output": False}]
        case("silent-declared", 0, cases=declared, expect_status="same")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--old", default="origin/dev")
    parser.add_argument("--new", default="HEAD")
    parser.add_argument("--old-dir", type=Path)
    parser.add_argument("--new-dir", type=Path)
    parser.add_argument("--cases", type=Path, help="cases file or directory (default: generated from the driver declarations of the new tree)")
    parser.add_argument("--client", action="append", help="only the generated cases of this client (repeatable)")
    parser.add_argument("--no-seed", action="store_true", help="do not add a pending candidate to the fixture project")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--self-test", action="store_true", help="inject differences and silent/failed hooks and check they are reported")
    args = parser.parse_args(argv)
    if args.self_test:
        failures = self_test()
        for line in failures:
            print("self-test FAIL:", line, file=sys.stderr)
        print("payload_compare self-test: " + ("FAILED" if failures else "ok"), file=sys.stderr)
        return 1 if failures else 0
    import shutil
    scratch = common.new_tmp("payload-compare-trees-")
    try:
        trees = {}
        for label, ref, directory in (("old", args.old, args.old_dir), ("new", args.new, args.new_dir)):
            if directory is not None:
                trees[label] = (directory.resolve(), str(directory), None)
            else:
                trees[label] = (scratch / label, ref, common.materialize(ref, scratch / label))
        cases = load_cases(args.cases) if args.cases else default_cases(trees["new"][0], args.client)
        if not cases:
            print("payload_compare: no hook cases (the drivers declare no hooks for the selected clients)", file=sys.stderr)
            return 2
        report = compare_hooks(trees["old"][0], trees["new"][0], cases, seed=not args.no_seed)
        report["old"], report["new"] = ({"ref": trees[s][1], "commit": trees[s][2]} for s in ("old", "new"))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    print(common.short_status(report["commands"]), file=sys.stderr)
    print(common.summary_line("payload_compare", report["summary"], report["exit_code"], f"[{args.old} -> {args.new}]"), file=sys.stderr)
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
