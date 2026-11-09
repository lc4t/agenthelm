"""English smoke over every command, discovered from ``--help`` instead of kept in a fixed list (issue #24, item 2).

The walker follows the ``{a,b,c} ...`` sub-command group of every parser, so a new sub-command shows up in the
discovered set at once. It must then be classified below, otherwise the test fails and says where to add it:
* READ_ONLY: runs a safe read-only case;
* PREVIEW:   a write command - runs its preview (no ``--apply``) and the test proves nothing on disk changed;
* HELP_ONLY: only ``--help`` is exercised, with the reason why no case runs here;
* DIRECT_WRITE: a command that writes at once and offers neither ``--apply`` nor ``--dry-run`` (#46); only ``--help`` runs.
  Every one needs its reason and is listed in docs/write-commands.md. The reconciliation tests keep this table honest:
  a command that gains a preview flag must leave it, and a command with a preview flag cannot hide in READ_ONLY or here.
Every discovered command also gets its ``--help`` checked for Chinese left in English output.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_i18n import AGENTHELM, CJK, PLUGINS, ROOT  # noqa: E402

ORCHESTRATOR = PLUGINS / "project-orchestrator/runtime"
SUBCOMMANDS = re.compile(r"\{([A-Za-z0-9_,-]+)\}\s+\.\.\.")  # the sub-command group, not an option's choices

# name -> (argv prefix, sub-command path before the walk starts). {project} is the temporary project.
ROOTS: dict[str, tuple[list[str], list[str]]] = {
    "agenthelm": ([str(AGENTHELM)], []),
    "agent-pack": ([str(PLUGINS / "agent-pack/runtime/agent_pack_config.py")], []),
    "nudge": ([str(AGENTHELM)], ["nudge"]),  # dispatched before the main parser, so it is not in `agenthelm --help`
    "sop": ([str(ORCHESTRATOR / "sop.py"), "--project", "{project}"], []),  # `agenthelm project sop` passes everything on to it
    "case": ([str(ORCHESTRATOR / "case.py")], []),
    "assets": ([str(ORCHESTRATOR / "assets.py")], []),
    "memory": ([str(ORCHESTRATOR / "memory.py")], []),
    "wf": ([str(ORCHESTRATOR / "wf.py")], []),
    "case_workspace": ([str(ORCHESTRATOR / "case_workspace.py")], []),
    "opinion": ([str(PLUGINS / "opinion-manager/runtime/opinion_manager.py")], []),
    "init": ([str(PLUGINS / "agents-init/skills/agents-init/scripts/init_project.py")], []),
}

P = ["--project", "{project}"]
READ_ONLY: dict[str, list[str]] = {
    "agent-pack inventory": ["--source", "project:{project}"],
    "agent-pack doctor": P,
    "agenthelm check": ["--offline", "--no-project"],
    "agenthelm doctor": ["--no-project"],
    "agenthelm locate": ["agents-init"],
    "agenthelm local-only check": P,
    "agenthelm local-only config": P,
    "agenthelm runtime status": [],
    "agenthelm config show": [],
    "agenthelm project find": [*P, "--no-input", "nothing"],
    "agenthelm project open": [*P, "--no-input", "nothing"],
    "agenthelm project refs": [*P, "--no-input", "nothing/here.md"],
    "agenthelm project progress": [*P, "--no-input"],
    "agenthelm project show": [*P, "--no-input", "missing"],
    "agenthelm project guide": [*P, "--no-input"],
    "agenthelm project health": [*P, "--no-input", "--offline"],
    "agenthelm project capabilities": [*P, "--no-input"],
    "agenthelm project scenarios": [*P, "--no-input"],
    "agenthelm project sop": [*P, "--no-input", "list"],
    "nudge status": [],
    "sop list": [], "sop index": [], "sop show": ["demo"], "sop validate": ["demo"], "sop interview": ["demo"],
    "sop events": ["case.created", "--case", "quarterly"],
    "sop preflight": ["demo"], "sop match": ["hello"], "sop due": [], "sop status": [],
    "case list": P, "case show": [*P, "quarterly"], "case validate": P,
    "assets find": [*P, "report"], "assets recent": P, "assets cases": P, "assets refs": [*P, "nothing/here.md"], "assets check": P,
    "assets status": P,
    "opinion verify": P,
}
PREVIEW: dict[str, list[str]] = {
    "agent-pack transfer": ["--from", "project:{project}", "--to-project", "{project}", "--skill", "missing"],
    "agent-pack sync": [*P, "--client", "codex"],
    "agent-pack bootstrap": ["--plugin", str(PLUGINS / "agent-pack"), "--client", "cursor"],
    "agent-pack reconcile": [*P, "--client", "claude"],
    "agenthelm install": ["--latest", "{latest}"],
    "agenthelm update": ["--no-project", "--latest", "{latest}"],
    "agenthelm migrate": ["--latest", "{latest}"],
    "agenthelm fix": [*P, "--no-input"],
    "agenthelm local-only fix": P,
    "agenthelm runtime install": [],
    "agenthelm runtime update": ["--source", str(ROOT)],
    "agenthelm runtime use": ["9.9.9"],
    "agenthelm config language": ["zh"],
    "agenthelm config language-hint": ["off"],
    "agenthelm config ignore-client": ["claude"],
    "agenthelm config unignore-client": ["claude"],
    "agenthelm config project-language": ["en", *P],
    "agenthelm project fix": [*P, "--no-input"],
    "agenthelm project tools": [*P, "--no-input"],
    "agenthelm project record": [*P, "--no-input", "new", "--title", "Second", "--goal", "Goal"],
    "nudge install-hooks": ["--client", "cursor"],
    "sop new": ["--slug", "fresh", "--template", "blank"],
    "sop defaults": ["--set", "max_retries=2"],
    "sop activate": ["demo"], "sop retire": ["demo"], "sop restore": ["demo"], "sop decide": ["demo", "--set", "max_retries=2"],
    "sop revise": ["demo", "--bump", "patch", "--note", "why"], "sop run": ["demo"], "sop init": [], "sop private": ["--create"],
    "case new": [*P, "--title", "Preview", "--slug", "preview", "--goal", "Goal", "--next-action", "Review", "--dry-run"],
    "case update": [*P, "quarterly", "--log", "Preview log", "--dry-run"],
    "sop step": ["missing", "1", "--done"],
    "sop confirm-read": ["missing", "private/input.txt"],
    "opinion inbox": ["adopt", "cand-none", "--project", "{project}"],  # adopt previews by default; add / decide / present / expire write .opinion/inbox.json at once (covered by the opinion-manager inbox tests)
}
HELP_ONLY: dict[str, str] = {
    "memory": "a single command with flags only; its English output is in the project-orchestrator tests",
    "wf": "interactive file finder; its English menu is covered by test_i18n.EnglishOutputTests",
    "case_workspace": "a single command with flags only",
    "opinion catalog": "lists the Chinese template titles by design (rule text is language independent); the English frame is checked in test_opinion_manager_and_agents_init_speak_english",
    "opinion compose": "needs a Profile file; English output is covered by test_opinion_manager_and_agents_init_speak_english",
    "opinion profile": "needs a catalog and writes a Profile preview; covered by test_the_opinion_files_do_not_depend_on_the_language",
    "opinion publish": "writes a published store; covered by the opinion-manager tests",
    "opinion updates": "needs a published store",
    "opinion compare": "needs two Profiles",
    "opinion context": "needs a Profile",
    "opinion lifecycle": "needs a store with an Occurrence log",
    "init": "a single command with many flags; its English flows are covered by the recovery and no-baseline tests",
}

DIRECT_WRITE: dict[str, str] = {
    "nudge hook": "client event consumer: reads a hook JSON on stdin and writes the local de-duplication state; covered by the nudge tests in plugins/agent-pack/tests",
    "assets rebuild": "rebuilds the file index at once; it writes only the machine-local cache $XDG_CACHE_HOME/agenthelm/assets (default ~/.cache), never the project",
}
PREVIEW_FLAG = re.compile(r"--(?:apply|dry-run)\b")
# Write commands that back up the files they replace and let the caller choose where (#80). Reconciled against ``--help`` below,
# so a command in this list cannot silently lose the flag, and every name must still be a registered PREVIEW command.
RECOVERY_DIR_FLAG = (
    "agenthelm update",
    "agenthelm config language", "agenthelm config language-hint", "agenthelm config ignore-client",
    "agenthelm config unignore-client", "agenthelm config project-language",
)


def tree(env: dict, prefix: list[str], start: list[str], path: list[str], found: dict, label: str) -> None:
    """Record ``label + path`` -> how to run it, for every leaf command below ``start``."""
    completed = subprocess.run([sys.executable, *prefix, *start, *path, "--help"], capture_output=True, text=True, env=env, timeout=120, check=False)
    assert completed.returncode == 0, (label, path, completed.stderr)
    match = SUBCOMMANDS.search(completed.stdout)
    if not match:
        found[" ".join([label, *path]).strip()] = (prefix, start, path)
        return
    for name in match.group(1).split(","):
        tree(env, prefix, start, [*path, name], found, label)


class EveryCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        base = Path(cls.directory.name).resolve()  # macOS temp dirs sit behind a symlink
        cls.home, cls.project, cls.empty_bin = base / "home", base / "project", base / "bin"
        for path in (cls.home, cls.empty_bin):
            path.mkdir()
        profile = cls.project / ".agents/moe.sakanano.agent-pack/project.json"
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({"schema_version": "2.0", "language": "en", "work": {"mode": "case-workspace", "case": {"root": "cases"}}}),
                           encoding="utf-8")
        cls.latest = base / "latest.json"  # a local stand-in for the cloud version file: nothing here touches the network
        cls.latest.write_text(json.dumps({"schema": 1, "marketplace": "agenthelm", "released_at": "2026-10-31", "migrations": [], "plugins": {},
                                           "repository": "https://github.com/lc4t/agenthelm"}),
                              encoding="utf-8")
        cls.env = {**{key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "AGENTHELM_", "LC_", "LANG", "CLAUDE", "CODEX"))},
                   "HOME": str(cls.home), "AGENTHELM_LANG": "en", "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1",
                   "PATH": str(cls.empty_bin)}  # no real claude / codex / cursor / git can be reached
        cls.commands: dict[str, tuple[list[str], list[str], list[str]]] = {}
        for root, (prefix, start) in ROOTS.items():
            tree(cls.env, cls.fill(prefix), start, [], cls.commands, root)

    @classmethod
    def fill(cls, arguments: list[str]) -> list[str]:
        return [item.replace("{project}", str(cls.project)).replace("{latest}", str(getattr(cls, "latest", ""))) for item in arguments]

    def run_case(self, key: str, arguments: list[str], expect: tuple[int, ...] = (0, 1, 2, 3, 4, 5)) -> subprocess.CompletedProcess:
        prefix, start, words = self.commands[key]
        completed = subprocess.run([sys.executable, *prefix, *start, *words, *self.fill(arguments)],
                                   capture_output=True, text=True, env=self.env, timeout=120, check=False)
        self.assertIn(completed.returncode, expect, f"{key}: {completed.stderr[-300:]}")
        self.assertEnglish(completed, key)
        return completed

    def assertEnglish(self, completed: subprocess.CompletedProcess, label: str) -> None:
        for line in (completed.stdout + "\n" + completed.stderr).splitlines():
            self.assertFalse(CJK.search(line), f"{label}: 英文输出里有漏翻的中文：{line[:160]}")

    def snapshot(self) -> dict[str, str]:
        state = {}
        for base in (self.home, self.project):
            for path in sorted(base.rglob("*")):
                if path.is_file() and ".cache" not in path.parts:  # the cache of the version check is not a result of the command
                    state[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return state

    def test_every_discovered_command_is_classified(self) -> None:
        known = {*READ_ONLY, *PREVIEW, *HELP_ONLY, *DIRECT_WRITE}
        self.assertGreater(len(self.commands), 60, "the walker found too few commands - did the help format change?")
        missing = [name for name in self.commands if name not in known]
        self.assertEqual(missing, [], "新增了子命令：把它登记到 READ_ONLY（只读用例）/ PREVIEW（写操作的预览用例）/ HELP_ONLY（写明原因）")
        stale = [name for name in known if name not in self.commands]
        self.assertEqual(stale, [], "登记表里有已经不存在的命令")

    def help_text(self, command: str) -> str:
        prefix, start, words = self.commands[command]
        completed = subprocess.run([sys.executable, *prefix, *start, *words, "--help"], capture_output=True, text=True,
                                   env=self.env, timeout=120, check=False)
        self.assertEqual(completed.returncode, 0, command)
        return completed.stdout

    def test_write_command_registry_matches_the_real_help(self) -> None:
        """Reconcile the tables with every discovered command's own ``--help`` (#46)."""
        groups = {"READ_ONLY": READ_ONLY, "PREVIEW": PREVIEW, "HELP_ONLY": HELP_ONLY, "DIRECT_WRITE": DIRECT_WRITE}
        seen: dict[str, str] = {}
        for label, table in groups.items():
            for name in table:
                self.assertNotIn(name, seen, f"{name} is registered in both {seen.get(name)} and {label}")
                seen[name] = label
        for name in self.commands:
            has_flag = bool(PREVIEW_FLAG.search(self.help_text(name)))
            label = seen[name]
            if label == "PREVIEW":
                self.assertTrue(has_flag, f"{name}: registered as PREVIEW but its --help offers no --apply/--dry-run")
            elif label in ("READ_ONLY", "DIRECT_WRITE"):
                self.assertFalse(has_flag, f"{name}: has --apply/--dry-run, so it is not {label}; move it to PREVIEW (or HELP_ONLY with a reason)")
        for name, reason in {**HELP_ONLY, **DIRECT_WRITE}.items():
            self.assertTrue(reason.strip(), name)

    def test_commands_that_back_up_replaced_files_accept_a_recovery_dir(self) -> None:
        for name in RECOVERY_DIR_FLAG:
            self.assertIn(name, PREVIEW, f"{name} must be a registered write command")
            self.assertRegex(self.help_text(name), r"--recovery-dir\b", f"{name} replaces files but offers no --recovery-dir")

    def test_read_only_cases_change_nothing(self) -> None:
        self.setup_sop()
        for key, arguments in READ_ONLY.items():
            with self.subTest(command=key):
                before = self.snapshot()
                self.run_case(key, arguments)
                self.assertEqual(self.snapshot(), before, f"{key}: registered as read-only but changed the disk")

    def test_every_help_screen_speaks_english(self) -> None:
        for command, (prefix, start, words) in self.commands.items():
            completed = subprocess.run([sys.executable, *prefix, *start, *words, "--help"], capture_output=True, text=True,
                                       env=self.env, timeout=120, check=False)
            self.assertEqual(completed.returncode, 0, command)
            self.assertEnglish(completed, f"{command} --help")

    def test_read_only_cases_speak_english(self) -> None:
        self.setup_sop()
        for key, arguments in READ_ONLY.items():
            with self.subTest(command=key):
                self.run_case(key, arguments)

    def test_write_previews_speak_english_and_change_nothing(self) -> None:
        self.setup_sop()
        for key, arguments in PREVIEW.items():
            with self.subTest(command=key):
                before = self.snapshot()
                self.run_case(key, arguments)
                self.assertEqual(self.snapshot(), before, f"{key}: 预览（没有 --apply）改动了磁盘")

    def setup_sop(self) -> None:
        if (self.project / "sops").exists() or any(self.project.glob("**/demo/SOP.md")):
            return
        subprocess.run([sys.executable, str(ORCHESTRATOR / "sop.py"), "--project", str(self.project), "new", "--slug", "demo",
                        "--template", "blank", "--apply"], capture_output=True, text=True, env=self.env, check=False, timeout=120)
        subprocess.run([sys.executable, str(ORCHESTRATOR / "case.py"), "--project", str(self.project), "new", "--title", "Quarterly report",
                        "--slug", "quarterly", "--goal", "Ship it", "--next-action", "Collect data"], capture_output=True, text=True,
                       env=self.env, check=False, timeout=120)


if __name__ == "__main__":
    unittest.main()
