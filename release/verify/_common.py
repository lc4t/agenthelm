"""Shared engine of the release verification tools (standard library only; nothing here touches the network).

``run_side`` runs a list of command specs inside one directory tree with a private HOME per command and returns a plain
dict per command; ``compare_side_results`` compares two such lists. ``compare_runs.py`` and ``payload_compare.py`` are thin
front ends over these two functions, so the guard against an "empty comparison" (both sides failing the same way and being
reported as identical) lives in exactly one place.

A command spec is a dict:

  name            unique label
  argv            list of strings; ``python3`` as first item means the interpreter running this tool; the tokens
                  ``{tree} {runtime} {home} {project} {work} {bin} {latest}`` are replaced per side and per command
  fixture         optional fixture kind; the fixture factory copies it to ``{project}`` (the working directory)
  stubs           names of fake client scripts put first on PATH; each records its argv
  stdin           optional text (or a JSON-able value) fed to standard input
  env             extra environment variables (tokens are replaced)
  expect_exit     optional list of accepted exit codes; the default rejects argparse errors (2) and "no such command"
  expect_nonzero  true when the command must fail (exit 0 is then "not executed")
  expect_output   true: an empty standard output on both sides is an empty comparison; false: silence is fine
  stdout_json     compare standard output as parsed JSON (field by field) when it parses
  timeout         seconds, default 60
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
ENV_REQUIRE_TAGS = "AGENTHELM_REQUIRE_TAGS"
BAD_DEFAULT_EXITS = (2, 126, 127)  # argparse usage error, "not executable", "command not found"
TOKENS = ("tree", "runtime", "home", "project", "work", "bin", "latest")
TREE_EXCLUDE = (".git",)
TEXT_KEEP = 40_000

DEFAULT_REGEX = (
    (r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?", "<TS>"),
    (r"\b1[5-9]\d{8}(?:\.\d+)?\b", "<EPOCH>"),
)

STUB = r'''#!{python}
import json, os, sys
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\n")
sys.stdout.write(os.environ.get("STUB_STDOUT", ""))
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
'''


# ---------------------------------------------------------------- normalisation

class Normalizer:
    """Replace volatile text: timestamps by regex, absolute directories by stable tokens (longest path first, so a nested
    directory is never half-replaced). Both are configurable; the token for a directory is ``<NAME>``."""

    def __init__(self, tokens: dict[str, str] | None = None, regex: list[tuple[str, str]] | None = None) -> None:
        self.regex = [(re.compile(p), r) for p, r in (list(DEFAULT_REGEX) + [tuple(x) for x in (regex or [])])]
        self.paths: list[tuple[str, str]] = []
        for path, token in (tokens or {}).items():
            self.add_path(path, token)

    def add_path(self, path: str | Path, token: str) -> None:
        for variant in {str(path), os.path.realpath(path)}:
            if variant and variant not in ("/", "."):
                self.paths.append((variant, token))
        self.paths.sort(key=lambda item: -len(item[0]))

    def text(self, value: str) -> str:
        for path, token in self.paths:
            value = value.replace(path, token)
        for pattern, replacement in self.regex:
            value = pattern.sub(replacement, value)
        return value

    def child(self) -> "Normalizer":
        clone = Normalizer()
        clone.regex = list(self.regex)
        clone.paths = list(self.paths)
        return clone


def config_normalizer(config: dict | None) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Fixed tokens for the per-side directories can be renamed under ``normalize.names``; extra regex under ``normalize.regex``."""
    config = config or {}
    names = {name: f"<{name.upper()}>" for name in ("tree", "home", "project", "work", "fixture")}
    names.update(config.get("names") or {})
    return names, [tuple(item) for item in config.get("regex") or []]


# ---------------------------------------------------------------- trees and git

def git(*args: str, cwd: Path | None = None, binary: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd or REPO, capture_output=True, text=not binary, check=False)


def resolve_commit(ref: str, repo: Path = REPO) -> str | None:
    done = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=repo)
    return done.stdout.strip() if done.returncode == 0 else None


def materialize(ref: str, dest: Path, repo: Path = REPO, paths: tuple[str, ...] = ()) -> str:
    """Extract ``ref`` (a commit-ish of the local repository; nothing is fetched) into ``dest`` and return its commit id."""
    commit = resolve_commit(ref, repo)
    if commit is None:
        hint = "; run `git fetch --tags origin`" if re.match(r"^v\d", ref) else ""
        raise SystemExit(f"cannot resolve ref {ref!r} in {repo}{hint}")
    dest.mkdir(parents=True, exist_ok=True)
    packed = subprocess.run(["git", "archive", "--format=tar", commit, *paths], cwd=repo, capture_output=True, check=False)
    if packed.returncode != 0:
        raise SystemExit(f"git archive {ref} failed: {packed.stderr.decode(errors='replace').strip()}")
    unpacked = subprocess.run(["tar", "-x", "-C", str(dest)], input=packed.stdout, capture_output=True, check=False)
    if unpacked.returncode != 0:
        raise SystemExit(f"tar failed: {unpacked.stderr.decode(errors='replace').strip()}")
    return commit


# ---------------------------------------------------------------- one side

def _substitute(value: Any, tokens: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, replacement in tokens.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if isinstance(value, list):
        return [_substitute(item, tokens) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, tokens) for key, item in value.items()}
    return value


def _copy(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _hash_tree(roots: dict[str, Path], norm: Normalizer) -> tuple[dict[str, dict], dict[str, str]]:
    entries: dict[str, dict] = {}
    texts: dict[str, str] = {}
    for label, root in roots.items():
        if not root.exists():
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in TREE_EXCLUDE)
            here = Path(current)
            if not dirs and not files and here != root:
                entries[f"{label}/{here.relative_to(root)}/"] = {"kind": "dir", "sha": ""}
            for name in sorted(files):
                path = here / name
                key = f"{label}/{path.relative_to(root)}"
                if path.is_symlink():
                    entries[key] = {"kind": "link", "sha": hashlib.sha256(norm.text(os.readlink(path)).encode()).hexdigest()}
                    continue
                raw = path.read_bytes()
                try:
                    text = norm.text(raw.decode("utf-8")) if b"\0" not in raw else None
                except UnicodeDecodeError:
                    text = None
                digest = hashlib.sha256(text.encode() if text is not None else raw).hexdigest()
                entries[key] = {"kind": "file", "sha": digest, "exec": bool(path.stat().st_mode & 0o111)}
                if text is not None:
                    texts[key] = text[:TEXT_KEEP]
    return entries, texts


def executed_verdict(spec: dict, code: int | None, spawn_error: str | None, timed_out: bool) -> str | None:
    """None when the command really ran the way the manifest expects, otherwise the reason it did not."""
    if spawn_error:
        return f"could not start: {spawn_error}"
    if timed_out:
        return "timed out"
    accepted = spec.get("expect_exit")
    if accepted is not None:
        return None if code in accepted else f"exit {code} is not in expect_exit {accepted}"
    if code in BAD_DEFAULT_EXITS:
        return f"exit {code} (argument error or command not found); list it in expect_exit if that is the point"
    if spec.get("expect_nonzero") and code == 0:
        return "expected a non-zero exit, got 0"
    return None


def run_side(side: str, tree: Path, work: Path, commands: list[dict], norm: Normalizer, *, fixture_factory: Callable[[dict], Path | None] | None = None,
             latest: Path | None = None, stub_overrides: dict[str, str] | None = None, extra_env: dict[str, str] | None = None) -> list[dict]:
    """Run every command in its own sandbox ``work/<n>-<name>/{home,project,bin}`` (a private HOME each) and collect results."""
    results = []
    norm = norm.child()
    norm.add_path(tree, "<TREE>")
    for number, spec in enumerate(commands):
        base = work / f"{number:02d}-{re.sub(r'[^A-Za-z0-9_.-]', '_', spec['name'])}"
        home, project, bin_dir = base / "home", base / "project", base / "bin"
        for directory in (home, project, bin_dir):
            directory.mkdir(parents=True, exist_ok=True)
        log = base / "stub.log"
        for stub in spec.get("stubs") or []:
            script = (stub_overrides or {}).get(stub) or STUB.format(python=sys.executable)
            target = bin_dir / stub
            target.write_text(script, encoding="utf-8")
            target.chmod(0o755)
        if spec.get("fixture") is not None and fixture_factory:
            source = fixture_factory(spec)
            if source is not None:
                _copy(source, project)
        local = norm.child()
        local.add_path(base, "<WORK>")  # after the nested ones, so home/project keep their own tokens
        local.add_path(home, "<HOME>")
        local.add_path(project, "<PROJECT>")
        if latest is not None:  # a copy per command, so the path is the per-command work directory on both sides
            shutil.copy2(latest, base / "latest.json")
        tokens = {"tree": str(tree), "runtime": str(tree / "plugins/agent-pack/runtime"), "home": str(home), "project": str(project),
                  "work": str(base), "bin": str(bin_dir), "latest": str(base / "latest.json") if latest is not None else ""}
        argv = _substitute(spec["argv"], tokens)
        if argv and argv[0] == "python3":
            argv[0] = sys.executable
        env = {"PATH": os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"]),
               "HOME": str(home), "TZ": "UTC", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "AGENTHELM_LANG": "en", "PYTHONHASHSEED": "0",
               "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1", "STUB_LOG": str(log),
               "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}
        env.update(_substitute(extra_env or {}, tokens))
        env.update(_substitute(spec.get("env") or {}, tokens))
        stdin = spec.get("stdin")
        if stdin is not None and not isinstance(stdin, str):
            stdin = json.dumps(_substitute(stdin, tokens), ensure_ascii=False)
        elif isinstance(stdin, str):
            stdin = _substitute(stdin, tokens)
        cwd = project if spec.get("fixture") is not None else base
        code: int | None = None
        out = err = ""
        spawn_error = None
        timed_out = False
        try:
            done = subprocess.run(argv, cwd=cwd, env=env, input=stdin if stdin is not None else "", capture_output=True, text=True,
                                  timeout=spec.get("timeout", 60), check=False)
            code, out, err = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            out = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        except OSError as exc:
            spawn_error = f"{type(exc).__name__}: {exc}"
        calls = []
        if log.is_file():
            for line in log.read_text(encoding="utf-8").splitlines():
                item = json.loads(line)
                calls.append({"argv": [local.text(arg) for arg in item["argv"]], "cwd": local.text(item["cwd"])})
        entries, texts = _hash_tree({"home": home, "project": project}, local)
        problem = executed_verdict(spec, code, spawn_error, timed_out)
        results.append({"name": spec["name"], "exit": code, "stdout": local.text(out), "stderr": local.text(err), "stubs": calls,
                        "tree": entries, "texts": texts, "problem": problem, "spec": spec})
    return results


# ---------------------------------------------------------------- comparison

def _first_line_diff(old: str, new: str) -> dict:
    a, b = old.splitlines(), new.splitlines()
    for index in range(max(len(a), len(b))):
        left = a[index] if index < len(a) else None
        right = b[index] if index < len(b) else None
        if left != right:
            clip = lambda s: None if s is None else (s if len(s) <= 240 else s[:239] + "…")  # noqa: E731
            return {"line": index + 1, "old": clip(left), "new": clip(right), "old_chars": len(old), "new_chars": len(new)}
    return {"line": None, "old_chars": len(old), "new_chars": len(new), "note": "differs only in line endings or trailing text"}


def json_paths(old: Any, new: Any, path: str = "$") -> list[dict]:
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for key in sorted(set(old) | set(new)):
            if key not in old:
                out.append({"path": f"{path}.{key}", "old": "<missing>", "new": new[key]})
            elif key not in new:
                out.append({"path": f"{path}.{key}", "old": old[key], "new": "<missing>"})
            else:
                out.extend(json_paths(old[key], new[key], f"{path}.{key}"))
        return out
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [d for i, (a, b) in enumerate(zip(old, new)) for d in json_paths(a, b, f"{path}[{i}]")]
    return [] if old == new else [{"path": path, "old": old, "new": new}]


def compare_command(old: dict, new: dict) -> dict:
    spec = new["spec"]
    diffs: dict[str, Any] = {}
    for stream in ("stdout", "stderr"):
        if old[stream] == new[stream]:
            continue
        parsed = None
        if stream == "stdout" and spec.get("stdout_json"):
            try:
                parsed = (json.loads(old[stream] or "null"), json.loads(new[stream] or "null"))
            except ValueError:
                parsed = None
        diffs[stream] = {"fields": json_paths(*parsed)[:40]} if parsed and json_paths(*parsed) else \
            ({} if parsed else _first_line_diff(old[stream], new[stream]))
        if diffs[stream] == {}:
            del diffs[stream]
    if old["exit"] != new["exit"]:
        diffs["exit"] = {"old": old["exit"], "new": new["exit"]}
    if old["stubs"] != new["stubs"]:
        index = next((i for i, (a, b) in enumerate(zip(old["stubs"], new["stubs"])) if a != b), min(len(old["stubs"]), len(new["stubs"])))
        diffs["stubs"] = {"old_calls": len(old["stubs"]), "new_calls": len(new["stubs"]), "first_difference": index,
                          "old": old["stubs"][index] if index < len(old["stubs"]) else None,
                          "new": new["stubs"][index] if index < len(new["stubs"]) else None}
    tree = {"added": sorted(set(new["tree"]) - set(old["tree"])), "removed": sorted(set(old["tree"]) - set(new["tree"])), "changed": []}
    for key in sorted(set(old["tree"]) & set(new["tree"])):
        if old["tree"][key] != new["tree"][key]:
            item = {"path": key}
            if key in old["texts"] and key in new["texts"]:
                item["first_difference"] = _first_line_diff(old["texts"][key], new["texts"][key])
            tree["changed"].append(item)
    if tree["added"] or tree["removed"] or tree["changed"]:
        diffs["tree"] = tree
    problems = {side: result["problem"] for side, result in (("old", old), ("new", new)) if result["problem"]}
    if spec.get("expect_output") and not problems and not old["stdout"].strip() and not new["stdout"].strip():
        problems = {"old": "empty standard output", "new": "empty standard output"}
    if len(problems) == 2:
        status = "empty"
    elif problems or diffs:
        status = "different"
    else:
        status = "same"
    return {"name": new["name"], "status": status, "exit": {"old": old["exit"], "new": new["exit"]}, "not_executed": problems,
            "stderr_head": {side: result["stderr"][:400] for side, result in (("old", old), ("new", new)) if side in problems},
            "differences": diffs, "tree_files": {"old": len(old["tree"]), "new": len(new["tree"])}}


def compare_side_results(old: list[dict], new: list[dict]) -> list[dict]:
    return [compare_command(a, b) for a, b in zip(old, new)]


def summarize(entries: list[dict]) -> dict:
    counts = {"total": len(entries), "same": 0, "different": 0, "empty": 0}
    for entry in entries:
        counts[entry["status"]] += 1
    return counts


def verdict_code(counts: dict) -> int:
    """1 when anything differs, 3 when nothing differs but some comparison was empty or ran nothing, 0 otherwise."""
    if counts["different"]:
        return 1
    if counts["empty"] or not counts["total"]:
        return 3
    return 0


def summary_line(tool: str, counts: dict, code: int, extra: str = "") -> str:
    word = {0: "IDENTICAL", 1: "DIFFERENT", 3: "EMPTY COMPARISON"}.get(code, str(code))
    return (f"{tool}: {word} - {counts['total']} compared, {counts['same']} same, {counts['different']} different, "
            f"{counts['empty']} empty (not executed on either side){(' ' + extra) if extra else ''}")


def short_status(entries: list[dict]) -> str:
    lines = []
    for entry in entries:
        lines.append(f"  {entry['status'].upper():9} {entry['name']}  exit {entry['exit']['old']}/{entry['exit']['new']}")
        for side, why in entry["not_executed"].items():
            lines.append(f"            {side}: not executed - {why}")
            head = entry["stderr_head"].get(side, "").strip().splitlines()
            if head:
                lines.append(f"            {side} stderr: {head[-1][:160]}")
        if entry["differences"]:
            lines.append(f"            differs in: {', '.join(entry['differences'])}")
    return "\n".join(lines)


def new_tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix)).resolve()


def write_tree(root: Path, files: dict[str, str], executable: tuple[str, ...] = ()) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if rel in executable:
            path.chmod(0o755)
