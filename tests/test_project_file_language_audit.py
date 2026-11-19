"""Audit and guard: with ``language: en`` no command writes Chinese into a project file.

Each generator below runs one project-file-writing command against a fresh English project (ASCII fixtures, temporary HOME,
English UI) and requires that every file it created or changed has no Chinese character. Set ``AGENTHELM_AUDIT_TABLE=1`` to
print the audit table (generator, files written, Chinese characters found).

Not covered, on purpose:
- opinion-manager (OPINION.md framework, public catalog): its own language work.
- user-level files (``~/.claude``, ``~/.codex``, the runtime data directory): not project files.
- ``--mode migrate``/``decide``/``region`` and ``agenthelm update``: they rewrite or record existing content, and write no
  template text of their own beyond what ``refresh`` and ``adopt`` already cover; ``update`` also needs the network.
- ``project --demo``: writes a throw-away demo project outside the user's project.
- text the user typed (project name, titles, logs): only ASCII fixtures are used, so user input cannot hide a leak.
Command output (stdout) is out of scope: the UI language controls it.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "plugins/agents-init/skills/agents-init/scripts/init_project.py"
AGENTHELM = ROOT / "plugins/agent-pack/runtime/agenthelm.py"
SOP = ROOT / "plugins/project-orchestrator/runtime/sop.py"
CASE = ROOT / "plugins/project-orchestrator/runtime/case.py"
MEMORY = ROOT / "plugins/project-orchestrator/runtime/memory.py"
CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")
BASE = ("--name", "Demo", "--language", "en", "--project-type", "software", "--vcs", "git", "--stack", "python",
        "--runtime", "python3", "--agent-cli", "claude")


class Workspace:
    """One English project plus a way to run commands in it and see which files they wrote."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.root = base / "project"
        self.root.mkdir()
        (base / "home").mkdir()
        self.env = {**os.environ, "HOME": str(base / "home"), "AGENTHELM_LANG": "en", "AGENTHELM_NO_UPDATE_CHECK": "1"}
        self.written: dict[str, str] = {}

    def snapshot(self) -> dict[str, bytes]:
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*")
                if p.is_file() and ".git" not in p.relative_to(self.root).parts}

    def run(self, script: Path, *args: object, ok: bool = True) -> subprocess.CompletedProcess[str]:
        before = self.snapshot()
        done = subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True, env=self.env,
                              cwd=self.root, check=False, timeout=120)
        if ok and done.returncode != 0:
            raise AssertionError(f"{script.name} {' '.join(map(str, args))}: rc={done.returncode}\n{done.stdout}\n{done.stderr}")
        for path, data in self.snapshot().items():
            if before.get(path) != data:
                self.written[path] = data.decode("utf-8", "replace")
        return done

    def init(self, *extra: str) -> None:
        self.run(INIT, "--project", self.root, "--mode", "init", *BASE, *extra, "--apply")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)

    def project_json(self) -> Path:
        return self.root / ".agents/moe.sakanano.agent-pack/project.json"


# --- generators: each takes a Workspace, runs commands, and returns nothing; the Workspace records what was written.

def init_default(w: Workspace) -> None:
    w.init("--readme-entry")


def init_no_entry_then_entry_mode(w: Workspace) -> None:
    w.init("--no-entry")
    w.written.clear()
    w.run(INIT, "--project", w.root, "--mode", "entry", "--entry-name", "go", "--readme-entry", "--apply")


def add_skeleton_after_deletion(w: Workspace) -> None:
    w.init()
    for name in ("AGENTS.md", "private/.gitignore", "docs/refs/README.md"):
        (w.root / name).unlink()
    w.written.clear()
    w.run(INIT, "--project", w.root, "--mode", "add-skeleton", "--apply")
    w.run(INIT, "--project", w.root, "--mode", "refresh", "--apply")
    w.run(INIT, "--project", w.root, "--mode", "profile", "--apply")


def adopt_non_empty_directory(w: Workspace) -> None:
    (w.root / "README.md").write_text("# mine\n", encoding="utf-8")
    (w.root / "AGENTS.md").write_text("# mine\n", encoding="utf-8")
    w.run(INIT, "--project", w.root, "--mode", "adopt", "--agents-policy", "append", "--recovery-dir", w.base / "backup",
          *BASE, "--apply")


def sop_templates_and_lifecycle(w: Workspace) -> None:
    w.init()
    for template in ("blank", "video-to-transcript", "weekly-review"):
        w.run(SOP, "new", "--project", w.root, "--apply", "--slug", template.replace("-", "")[:12], "--template", template)
    w.run(SOP, "decide", "--project", w.root, "--apply", "--confirm", "trigger", "blank")
    w.run(SOP, "revise", "--project", w.root, "--apply", "--bump", "patch", "--note", "note", "blank")
    w.run(SOP, "retire", "--project", w.root, "--apply", "--note", "note", "blank")


def sop_trial_run_and_steps(w: Workspace) -> None:
    w.init()
    w.run(SOP, "new", "--project", w.root, "--apply", "--slug", "wk", "--template", "weekly-review")
    w.run(SOP, "run", "--project", w.root, "--apply", "--trial", "--title", "T", "wk")
    case = next(p.name for p in (w.root / "cases").iterdir() if p.name.startswith("CASE"))
    for flag in ("--done", "--skip", "--fail"):
        w.run(SOP, "step", "--project", w.root, "--apply", flag, "--note", "n", "--artifact", "out.txt:d", case, "1", ok=True)
    w.run(SOP, "confirm-read", "--project", w.root, "--apply", "--note", "n", case, "private/x.txt")


def case_new_and_update(w: Workspace) -> None:
    w.init()
    for slug, extra in (("shared", ()), ("hidden", ("--sensitivity", "private"))):
        w.run(CASE, "new", "--project", w.root, "--title", "T", "--slug", slug, "--goal", "G", "--next-action", "n", *extra)
    for case in sorted(p.name for p in (w.root / "cases").iterdir() if p.name.startswith("CASE")):
        w.run(CASE, "update", "--project", w.root, case, "--status", "done", "--log", "x", "--decision", "y",
              "--result", "r", "--add-deliverable", "a.txt", "--add-evidence", "e")


def memory_create(w: Workspace) -> None:
    w.init()
    (w.root / "body.txt").write_text("hello\n", encoding="utf-8")
    w.run(MEMORY, "create", "--project", w.root, "--apply", "--id", "m1", "--type", "project", "--slug", "s",
          "--description", "d", "--body-file", "body.txt")


def project_record_new(w: Workspace) -> None:
    w.init()
    w.run(AGENTHELM, "project", "--project", w.root, "record", "new", "--title", "X", "--goal", "Y", "--apply", "--no-input")


def private_gitignore_from_sop_private(w: Workspace) -> None:
    w.init()
    (w.root / "private/.gitignore").unlink()
    w.written.clear()
    w.run(SOP, "private", "--project", w.root, "--apply")


def project_fix_recreates_missing_files(w: Workspace) -> None:
    w.init()
    (w.root / "menu").unlink()
    (w.root / "private/.gitignore").unlink()
    w.written.clear()
    w.run(AGENTHELM, "project", "--project", w.root, "fix", "--apply", "--no-input")
    w.run(AGENTHELM, "fix", "--project", w.root, "--apply")


# (id, generator, files that must appear among the written ones: guards against a vacuous pass)
GENERATORS = [
    ("init", init_default, {"menu", "private/.gitignore", "AGENTS.md", "README.md"}),
    ("init --no-entry, then --mode entry", init_no_entry_then_entry_mode, {"go"}),
    ("add-skeleton / refresh / profile", add_skeleton_after_deletion, {"private/.gitignore", "AGENTS.md"}),
    ("adopt", adopt_non_empty_directory, {"menu", "AGENTS.md"}),
    ("sop new / decide / revise / retire", sop_templates_and_lifecycle, {"sops"}),
    ("sop run --trial / step / confirm-read", sop_trial_run_and_steps, {"cases"}),
    ("case new / update", case_new_and_update, {"cases"}),
    ("memory create", memory_create, {"memory"}),
    ("project record new", project_record_new, {"cases"}),
    ("sop private --apply", private_gitignore_from_sop_private, {"private/.gitignore"}),
    ("project fix / agenthelm fix", project_fix_recreates_missing_files, {"menu", "private/.gitignore"}),
]


class EnglishProjectFilesHaveNoChineseTests(unittest.TestCase):
    def test_every_generator_writes_english_only_files(self) -> None:
        table = []
        for name, generator, expected in GENERATORS:
            with self.subTest(generator=name), tempfile.TemporaryDirectory() as temporary:
                workspace = Workspace(Path(temporary))
                generator(workspace)
                counts = {path: len(CJK.findall(text)) for path, text in workspace.written.items()}
                table.append((name, len(counts), sum(counts.values())))
                for wanted in expected:
                    self.assertTrue(any(path == wanted or path.startswith(wanted + "/") for path in counts),
                                    f"{name}: expected a write under {wanted}; wrote {sorted(counts)}")
                self.assertEqual({path: n for path, n in counts.items() if n}, {}, name)
        if os.environ.get("AGENTHELM_AUDIT_TABLE"):
            print("\n{:<42}{:>8}{:>10}".format("generator", "files", "CJK chars"))
            for row in table:
                print("{:<42}{:>8}{:>10}".format(*row))

    def test_zh_and_default_language_keep_the_chinese_launcher_and_ignore_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace(Path(temporary))
            workspace.run(INIT, "--project", workspace.root, "--mode", "init", "--name", "Demo", "--language", "zh",
                          "--project-type", "software", "--vcs", "git", "--stack", "python", "--runtime", "python3",
                          "--agent-cli", "claude", "--apply")
            shipped = ROOT / "plugins/agents-init/skills/agents-init"
            self.assertEqual((workspace.root / "menu").read_text(encoding="utf-8"), (shipped / "launcher.sh").read_text(encoding="utf-8"))
            self.assertRegex((workspace.root / "private/.gitignore").read_text(encoding="utf-8"), CJK)

    def test_sop_private_follows_the_profile_language_and_defaults_to_zh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Workspace(Path(temporary))
            workspace.init()
            profile = workspace.project_json()
            text = profile.read_text(encoding="utf-8")
            profile.write_text(text.replace('"language": "en"', '"language": "zh"'), encoding="utf-8")  # same as no field: zh
            (workspace.root / "private/.gitignore").unlink()
            workspace.run(SOP, "private", "--project", workspace.root, "--apply")
            self.assertRegex((workspace.root / "private/.gitignore").read_text(encoding="utf-8"), CJK)


if __name__ == "__main__":
    unittest.main()
