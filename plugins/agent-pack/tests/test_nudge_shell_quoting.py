"""#42：nudge 文案里被代入命令行的路径与会话 ID 经 shlex.quote；用真实 shell 执行生成的 inbox add。"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
RUNTIME = PLUGIN / "runtime"
MANAGER_RUNTIME = PLUGIN.parent / "opinion-manager" / "runtime"
sys.path.insert(0, str(RUNTIME))
import agent_pack_nudge as nudge  # noqa: E402
import nudge_text  # noqa: E402

NASTY = ["with space", "it's", 'say "hi"', "$HOME/x", "back`tick`", "中文 目录", "a b'c\"d$e`f中"]
SESSION = "sess $1 'x' \"y\""
SHELLS = [shell for shell in ("bash", "zsh") if shutil.which(shell)]


def add_command(language: str, manager: Path, root: Path, session: str = SESSION) -> str:
    return nudge_text.text("add_command", language, manager=manager, root=root, lang=language,
                           session=nudge_text.session_flag(session))


class NudgeShellQuotingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name).resolve()

    def test_add_command_split_restores_values(self) -> None:
        for name in NASTY:
            manager, root = Path("/m") / name / "opinion_manager.py", Path("/p") / name
            for language in nudge_text.LANGUAGES:
                argv = shlex.split(add_command(language, manager, root))
                self.assertEqual(argv[1], str(manager), (name, language))
                self.assertEqual(argv[argv.index("--project") + 1], str(root), (name, language))
                self.assertEqual(argv[argv.index("--session") + 1], SESSION, (name, language))

    def test_stop_request_decide_command_split_restores_values(self) -> None:
        for name in NASTY:
            manager, root = Path("/m") / name / "opinion_manager.py", Path("/p") / name
            for language in nudge_text.LANGUAGES:
                message = nudge_text.text("stop_request", language, count=1, manager=manager, root=root, lang=language, listing="1. x")
                line = next(part for part in message.replace("。", "。\n").replace(". ", ".\n").splitlines() if "inbox decide" in part)
                argv = shlex.split(line[line.index("python3"):].rstrip("。."))
                self.assertEqual(argv[1], str(manager), (name, language))
                self.assertEqual(argv[argv.index("--project") + 1], str(root), (name, language))

    def test_display_messages_are_not_quoted(self) -> None:
        self.assertEqual(nudge_text.text("status_project", "en", project="/a b/c"), "Project: /a b/c")

    def test_plain_paths_stay_readable(self) -> None:
        self.assertIn("python3 /opt/om/opinion_manager.py inbox add --project /work/proj ",
                      add_command("en", Path("/opt/om/opinion_manager.py"), Path("/work/proj"), "abc-123"))

    def test_no_session_leaves_no_flag(self) -> None:
        self.assertNotIn("--session", add_command("en", Path("/m/o.py"), Path("/p"), ""))

    def test_real_shell_writes_candidate(self) -> None:
        for shell in SHELLS:
            for language in nudge_text.LANGUAGES:
                for index, name in enumerate(NASTY):
                    with self.subTest(shell=shell, language=language, name=name):
                        runtime_dir = self.base / f"{shell}-{language}-{index}" / name
                        shutil.copytree(MANAGER_RUNTIME, runtime_dir, ignore=shutil.ignore_patterns("__pycache__"))
                        project = self.base / f"proj-{shell}-{language}-{index}" / name
                        project.mkdir(parents=True)
                        command = add_command(language, runtime_dir / "opinion_manager.py", project)
                        for placeholder in ("<标题>", "<title>", "<完整规则>", "<the full rule>", "<适用范围>", "<where it applies>",
                                            "<用户原话>", "<the user's own words>"):
                            command = command.replace(f'"{placeholder}"', "'x'")
                        done = subprocess.run([shell, "-c", command], capture_output=True, text=True,
                                              env={**os.environ, "HOME": str(self.base / "home")}, cwd=self.base)
                        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
                        stored = json.loads((project / ".opinion" / "inbox.json").read_text(encoding="utf-8"))
                        self.assertEqual(next(iter(stored["candidates"].values()))["session"], SESSION)

    def test_cursor_hook_command_quotes_runtime_path(self) -> None:
        for name in NASTY:
            runtime = Path("/a") / name / "agenthelm.py"
            command = nudge.cursor_commands(runtime)["sessionStart"]["command"]
            self.assertEqual(shlex.split(command)[1], str(runtime), name)
            self.assertTrue(nudge.OURS.search(command), name)


if __name__ == "__main__":
    unittest.main()
