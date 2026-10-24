"""项目根启动器草稿（agents-init/launcher.sh）与 ``agenthelm project`` 的衔接。

每个用例都在空的 HOME 与最小 PATH 里运行，模拟没有 AI 客户端、没有运行时或没有 Python 的机器。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "plugins/agents-init/skills/agents-init/launcher.sh"
AGENTHELM = ROOT / "plugins/agent-pack/runtime/agenthelm.py"


@unittest.skipUnless(shutil.which("sh") and sys.platform != "win32", "the launcher is a POSIX shell script")
class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.menu = self.project / "menu"
        shutil.copy(LAUNCHER, self.menu)
        self.menu.chmod(0o755)
        self.home = self.base / "home"
        self.home.mkdir()
        # Minimal PATH: the launcher needs dirname (everything else is a shell builtin);
        # python3 only where a case wants it.
        self.bin_plain = self.base / "bin-plain"
        self.bin_python = self.base / "bin-python"
        for directory in (self.bin_plain, self.bin_python):
            directory.mkdir()
            (directory / "dirname").symlink_to(shutil.which("dirname"))
        (self.bin_python / "python3").symlink_to(sys.executable)

    def launch(self, *args: str, path: Path | None = None, **extra: str) -> subprocess.CompletedProcess:
        # No /bin on PATH: merged-/usr Linux systems keep python3 there.
        environment = {"HOME": str(self.home), "PATH": str(path or self.bin_python),
                       "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1", **extra}
        return subprocess.run(["/bin/sh", str(self.menu), *args], input="", capture_output=True, text=True,
                              env=environment, timeout=30, check=False)

    def wrapper(self) -> Path:
        script = self.base / "agenthelm"
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{AGENTHELM}" "$@"\n', encoding="utf-8")
        script.chmod(0o755)
        return script

    def test_without_python_explains_the_dependency(self) -> None:
        result = self.launch(path=self.bin_plain)
        self.assertEqual(result.returncode, 3)
        self.assertIn("Python 3.11", result.stderr)
        self.assertIn("不需要 AI 客户端", result.stderr)

    def test_too_old_python_is_named_under_a_utf8_locale(self) -> None:
        # bash 3.2 reads a multibyte character after "$var" as part of the name; the message must survive it.
        old = self.base / "bin-old"
        old.mkdir()
        (old / "dirname").symlink_to(shutil.which("dirname"))
        (old / "python3").write_text('#!/bin/sh\ncase "$*" in *version_info*) exit 1;; *) echo 3.9.6;; esac\n',
                                     encoding="utf-8")
        (old / "python3").chmod(0o755)
        result = self.launch(path=old, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("找到的 Python 是 3.9.6", result.stderr)

    def test_with_python_but_no_runtime_explains_how_to_install(self) -> None:
        result = self.launch()
        self.assertEqual(result.returncode, 3)
        self.assertIn("没有找到 agenthelm 运行时", result.stderr)
        self.assertIn("AGENTHELM_BIN", result.stderr)

    def test_wrong_agenthelm_bin_is_reported(self) -> None:
        result = self.launch(AGENTHELM_BIN=str(self.base / "missing"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("AGENTHELM_BIN 指向的命令不存在", result.stderr)

    def test_hands_over_arguments_and_the_project_root(self) -> None:
        result = self.launch("--demo", "study", "show", "T-0007", "--output", "json", AGENTHELM_BIN=str(self.wrapper()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["data"]["title"], "词汇第一轮")

    def test_no_arguments_under_set_u_does_not_wait(self) -> None:
        result = self.launch("--demo", "blank", AGENTHELM_BIN=str(self.wrapper()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("新项目", result.stdout)

    def test_exit_codes_pass_through(self) -> None:
        result = self.launch("--demo", "study", "--demo-fault", "runtime-old", AGENTHELM_BIN=str(self.wrapper()))
        self.assertEqual(result.returncode, 3)
        self.assertIn("接口版本 2", result.stderr)

    def test_falls_back_to_plugins_installed_by_claude_code(self) -> None:
        cache = self.home / ".claude/plugins/cache/agenthelm"
        record = {}
        for name in ("agent-pack", "project-orchestrator"):
            version = json.loads((ROOT / "plugins" / name / "plugin.json").read_text(encoding="utf-8"))["version"]
            target = cache / name / version
            shutil.copytree(ROOT / "plugins" / name, target, ignore=shutil.ignore_patterns("__pycache__"))
            record[f"{name}@agenthelm"] = [{"scope": "user", "installPath": str(target), "version": version}]
        (self.home / ".claude/plugins/installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": record}), encoding="utf-8")
        result = self.launch("--demo", "personal", "progress", "--output", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["data"]["items"][0]["title"], "搬家公司比价")


class ProjectDispatchTests(unittest.TestCase):
    def test_project_command_reaches_the_project_entry(self) -> None:
        result = subprocess.run([sys.executable, str(AGENTHELM), "project", "--demo", "blank", "guide"],
                                input="", capture_output=True, text=True, timeout=30, check=False,
                                env={**os.environ, "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("项目指南", result.stdout)

    def test_missing_project_entry_exits_3_with_next_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugins = Path(temporary) / "plugins"
            shutil.copytree(ROOT / "plugins/agent-pack", plugins / "agent-pack",
                            ignore=shutil.ignore_patterns("__pycache__"))
            result = subprocess.run([sys.executable, str(plugins / "agent-pack/runtime/agenthelm.py"), "project"],
                                    input="", capture_output=True, text=True, timeout=30, check=False,
                                    env={**{key: value for key, value in os.environ.items()
                                            if key not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")},
                                         "HOME": temporary, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 3)
        self.assertIn("project-orchestrator", result.stderr)


if __name__ == "__main__":
    unittest.main()
