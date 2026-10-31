"""#42：协议里被代入命令行的路径与会话 ID 经 shlex.quote；用真实 shell 执行生成的 inbox add / decide。"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
import inbox_text  # noqa: E402

NASTY = ["with space", "it's", 'say "hi"', "$HOME/x", "back`tick`", "中文 目录", "a b'c\"d$e`f中"]
SESSION = "sess $1 'x' \"y\""
SHELLS = [shell for shell in ("bash", "zsh") if shutil.which(shell)]


def _extract(protocol: str, verb: str) -> str:
    line = next(line for line in protocol.splitlines() if f"inbox {verb}" in line and "python3" in line)
    return line[line.index("python3"):].strip()


class ShellQuotingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name).resolve()

    def render(self, language: str, runtime: Path, project: Path, session: str = SESSION) -> str:
        return inbox_text.text("protocol", language, runtime=runtime, project=project, lang=language,
                               session=inbox_text.session_flag(session))

    def test_split_restores_paths_in_both_languages(self) -> None:
        for name in NASTY:
            runtime, project = Path("/r/") / name / "opinion_manager.py", Path("/p") / name
            for language in inbox_text.LANGUAGES:
                protocol = self.render(language, runtime, project)
                for verb in ("add", "decide"):
                    argv = shlex.split(_extract(protocol, verb))
                    self.assertEqual(argv[1], str(runtime), (name, language, verb))
                    self.assertEqual(argv[argv.index("--project") + 1], str(project), (name, language, verb))
                argv = shlex.split(_extract(protocol, "add"))
                self.assertEqual(argv[argv.index("--session") + 1], SESSION)

    def test_no_session_leaves_no_flag(self) -> None:
        self.assertEqual(inbox_text.session_flag(""), "")

    def test_plain_paths_stay_readable(self) -> None:
        protocol = self.render("en", Path("/opt/om/opinion_manager.py"), Path("/work/proj"), "abc-123")
        self.assertIn("python3 /opt/om/opinion_manager.py inbox add --project /work/proj ", protocol)
        self.assertIn("--session abc-123 ", protocol)

    def test_real_shell_writes_candidate(self) -> None:
        for shell in SHELLS:
            for language in inbox_text.LANGUAGES:
                for index, name in enumerate(NASTY):
                    with self.subTest(shell=shell, language=language, name=name):
                        runtime_dir = self.base / f"{shell}-{language}-{index}" / name
                        shutil.copytree(RUNTIME, runtime_dir, ignore=shutil.ignore_patterns("__pycache__"))
                        project = self.base / f"proj-{shell}-{language}-{index}" / name
                        project.mkdir(parents=True)
                        protocol = self.render(language, runtime_dir / "opinion_manager.py", project)
                        add = _extract(protocol, "add")
                        add = (add.replace('"<标题>"', "'T'").replace('"<完整规则>"', "'R'").replace('"<适用范围>"', "'S'")
                                  .replace('"<用户原话>"', "'Q'").replace('"<title>"', "'T'").replace('"<the full rule>"', "'R'")
                                  .replace('"<where it applies>"', "'S'").replace('"<the user\'s own words>"', "'Q'"))
                        done = subprocess.run([shell, "-c", add], capture_output=True, text=True,
                                              env={**os.environ, "HOME": str(self.base / "home")}, cwd=self.base)
                        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
                        stored = json.loads((project / ".opinion" / "inbox.json").read_text(encoding="utf-8"))
                        self.assertEqual(len(stored["candidates"]), 1)
                        self.assertEqual(next(iter(stored["candidates"].values()))["session"], SESSION)


if __name__ == "__main__":
    unittest.main()
