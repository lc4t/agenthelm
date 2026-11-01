"""6.11.0-rc.3 W1-B：升级主路径。

* A11：会话开始提示里的 npx 命令在预发布运行时带 ``@<当前版本>``，稳定版不变；
* #26：Cursor 在 update 预览里“不可用”时，不计入 check 的 plugins_behind / 会话开始提示的“N 个可更新”，
  单列为 plugins_unavailable；有 --source 本地源时仍按落后计；
* A5：``runtime install`` 预发布版默认不切换 current（``--use`` 显式切换）。

HOME 是临时目录；客户端命令是桩脚本；不运行真实的 claude / codex / cursor。
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import agenthelm  # noqa: E402
import agenthelm_runtime  # noqa: E402
from test_agenthelm_reporting import Base, write  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
AGENTHELM = ROOT / "plugins/agent-pack/runtime/agenthelm.py"
PLUGINS = ("agent-pack", "agents-init", "opinion-manager", "project-orchestrator")

TWO = {
    "schema": 1, "marketplace": "agenthelm", "released_at": "2026-10-21",
    "repository": "https://github.com/example/agenthelm",
    "plugins": {"agent-pack": {"version": "2.0.0"}, "agents-init": {"version": "3.0.0"}},
    "migrations": [],
}


class CommandTagTests(unittest.TestCase):
    """A11：会话开始提示里的命令，预发布运行时带 @<当前版本>，稳定版保持原样。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def release_root(self, path: Path, version: str = "6.11.0") -> Path:
        write(path / "index.json", {"plugin": {"version": version}})
        return path

    def test_stable_and_unlabelled_content_keep_the_plain_command(self) -> None:
        root = self.release_root(self.base / "checkout")
        self.assertEqual(agenthelm.cli_command(root), "npx @sakanano-moe/agenthelm")
        self.assertEqual(agenthelm.cli_command(self.base / "nowhere"), "npx @sakanano-moe/agenthelm")
        stable = self.release_root(self.base / "runtimes/6.11.0")
        write(stable / "runtime.json", {"version": "6.11.0"})
        self.assertEqual(agenthelm.cli_command(stable), "npx @sakanano-moe/agenthelm")

    def test_user_level_runtime_candidate_pins_its_own_version(self) -> None:
        root = self.release_root(self.base / "runtimes/6.11.0-rc.2")
        write(root / "runtime.json", {"version": "6.11.0-rc.2"})
        self.assertEqual(agenthelm.cli_command(root), "npx @sakanano-moe/agenthelm@6.11.0-rc.2")

    def test_npm_package_candidate_pins_the_package_version(self) -> None:
        root = self.release_root(self.base / "pkg/runtime")
        write(self.base / "pkg/package.json", {"name": "@sakanano-moe/agenthelm", "version": "6.11.0-rc.2"})
        self.assertEqual(agenthelm.cli_command(root), "npx @sakanano-moe/agenthelm@6.11.0-rc.2")

    def test_release_tarball_candidate_pins_the_tag_version(self) -> None:
        root = self.release_root(self.base / "agenthelm-v6.11.0-rc.2")
        self.assertEqual(agenthelm.cli_command(root), "npx @sakanano-moe/agenthelm@6.11.0-rc.2")

    def test_unrelated_labels_are_not_trusted(self) -> None:
        root = self.release_root(self.base / "pkg/runtime")
        write(self.base / "pkg/package.json", {"name": "other", "version": "6.11.0-rc.2"})
        self.assertEqual(agenthelm.cli_command(root), "npx @sakanano-moe/agenthelm")
        root = self.release_root(self.base / "rt")
        write(root / "runtime.json", {"version": "7.0.0-rc.1"})  # does not extend this release version
        self.assertEqual(agenthelm.cli_command(root), "npx @sakanano-moe/agenthelm")


class HookCommandTests(Base):
    """会话开始 hook 输出的命令：模拟当前运行时=预发布/稳定。"""

    def hook_text(self, cli: str) -> str:
        self.claude_plugin("1.0.0")
        self.stub("claude")
        out = io.StringIO()
        with mock.patch.object(agenthelm, "CLI", cli), redirect_stdout(out):
            code = agenthelm.main(["check", "--hook", "--no-project", "--home", str(self.home),
                                   "--latest", str(self.latest_file)])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_candidate_runtime_hint_names_its_own_version(self) -> None:
        text = self.hook_text("npx @sakanano-moe/agenthelm@6.11.0-rc.2")
        self.assertIn("`npx @sakanano-moe/agenthelm@6.11.0-rc.2 check`", text)

    def test_stable_runtime_hint_is_unchanged(self) -> None:
        text = self.hook_text("npx @sakanano-moe/agenthelm")
        self.assertIn("`npx @sakanano-moe/agenthelm check`", text)
        self.assertNotIn("@6", text)

    def test_no_hint_hard_codes_the_plain_command(self) -> None:
        source = AGENTHELM.read_text(encoding="utf-8")
        literals = [line for line in source.splitlines() if "npx @sakanano-moe/agenthelm" in line]
        # only the docstring mention and the one definition; every printed hint goes through CLI / cli_command()
        self.assertEqual(len(literals), 2, literals)
        self.assertTrue(any(line.startswith('NPX = "npx @sakanano-moe/agenthelm"') for line in literals), literals)


class CursorUnavailableTests(Base):
    """#26：Cursor 在 update 预览里不可用 → check / 会话开始提示不把它算作“可更新”。"""

    def setUp(self) -> None:
        super().setUp()
        write(self.latest_file, TWO)

    def installed(self, cursor: str = "1.0.0") -> None:
        self.claude_plugin("2.0.0")
        self.stub("claude")
        for name in ("agent-pack", "agents-init"):
            write(self.home / f".cursor/plugins/local/{name}/plugin.json", {"name": name, "version": cursor})

    def check(self, *extra: str) -> dict:
        code, payload = self.run_json("check", "--no-project", *extra)
        self.assertEqual(code, 0)
        return payload

    def test_cursor_without_release_info_is_not_counted_as_behind(self) -> None:
        self.installed()
        summary = self.check()["summary"]
        self.assertEqual(summary["plugins_behind"], 0)
        self.assertEqual(summary["plugins_unavailable"], 2)
        self.assertTrue(summary["up_to_date"])
        self.assertEqual(summary["by_client"]["cursor"]["behind"], 0)
        self.assertEqual(summary["by_client"]["cursor"]["unavailable"], 2)
        report = agenthelm.run_check(agenthelm.Env(home=self.home, variables={}), None, latest_source=str(self.latest_file))
        self.assertIsNone(agenthelm.one_line(report), "没有别的待办时，会话开始不提示")

    def test_update_preview_agrees_with_check(self) -> None:
        self.installed()
        _code, payload = self.run_json("update", "--no-project")
        cursor = [step for step in payload["steps"] if step["client"] == "cursor"]
        self.assertEqual([step["status"] for step in cursor], ["unavailable"])
        self.assertEqual(self.check()["summary"]["plugins_behind"], 0)

    def test_session_hint_leaves_cursor_out_but_keeps_other_clients(self) -> None:
        self.installed()
        self.claude_plugin("1.0.0")
        report = agenthelm.run_check(agenthelm.Env(home=self.home, variables={}), None, latest_source=str(self.latest_file))
        line = agenthelm.one_line(report)
        self.assertIn("1 个插件可更新（Claude Code：1 个可更新；Cursor：2 个不可用", line)
        self.assertNotIn("Cursor：2 个可更新", line)
        self.assertIn("Cursor：", line)
        self.assertIn("不可用", line)

    def test_text_output_lists_cursor_as_unavailable_with_the_source_hint(self) -> None:
        self.installed()
        _code, text = self.run_text("check", "--no-project")
        self.assertIn("[cursor] agent-pack 1.0.0 → 2.0.0", text)
        self.assertIn("不可用", text)
        self.assertIn("--source", text)
        self.assertIn("全部为最新", text)

    def test_local_source_makes_cursor_updatable_again(self) -> None:
        self.installed()
        source = self.root / "checkout"
        (source / "plugins").mkdir(parents=True)
        summary = self.check("--source", str(source))["summary"]
        self.assertEqual((summary["plugins_behind"], summary["plugins_unavailable"]), (2, 0))
        self.assertFalse(summary["up_to_date"])

    def test_release_info_in_latest_json_makes_cursor_updatable(self) -> None:
        self.installed()
        write(self.latest_file, {**TWO, "release": {"tag": "v2.0.0", "tarball_sha256": "0" * 64}})
        summary = self.check()["summary"]
        self.assertEqual((summary["plugins_behind"], summary["plugins_unavailable"]), (2, 0))

    def test_a_cursor_that_is_current_stays_out_of_both_counts(self) -> None:
        self.installed("9.9.9")
        summary = self.check()["summary"]
        self.assertEqual((summary["plugins_behind"], summary["plugins_unavailable"]), (0, 0))


class RuntimeInstallUseTests(unittest.TestCase):
    """A5：预发布版 runtime install 默认不切换 current。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.data = self.home / ".local/share/agenthelm"
        self.env = {**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1",
                    "AGENTHELM_LATEST_URL": str(ROOT / "latest.json"), "AGENTHELM_LANG": "zh"}
        self.env.pop("XDG_DATA_HOME", None)

    def runtime(self, *args: str) -> tuple[int, dict]:
        result = subprocess.run([sys.executable, str(AGENTHELM), "runtime", *args, "--output", "json"],
                                capture_output=True, text=True, check=False, env=self.env, timeout=120)
        try:
            return result.returncode, json.loads(result.stdout)
        except ValueError:
            self.fail(f"exit {result.returncode}: {result.stdout}\n{result.stderr}")

    def runtime_text(self, *args: str) -> str:
        result = subprocess.run([sys.executable, str(AGENTHELM), "runtime", *args], capture_output=True, text=True,
                                check=False, env=self.env, timeout=120)
        return result.stdout + result.stderr

    def tarball(self, version: str, tag: str | None = None) -> Path:
        name = f"agenthelm-v{tag or version}"
        top = self.base / "stage" / name
        for plugin in PLUGINS:
            shutil.copytree(ROOT / "plugins" / plugin, top / "plugins" / plugin,
                            ignore=shutil.ignore_patterns("__pycache__", "tests"))
        index = json.loads((ROOT / "index.json").read_text(encoding="utf-8"))
        index["plugin"]["version"] = version
        (top / "index.json").write_text(json.dumps(index), encoding="utf-8")
        archive = self.base / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(top, arcname=name)
        shutil.rmtree(top)
        return archive

    def current(self) -> str | None:
        link = self.data / "current"
        return os.readlink(link) if link.is_symlink() else None

    def test_candidate_install_keeps_current_and_says_so(self) -> None:
        self.assertEqual(self.runtime("install", "--apply", "--source", str(self.tarball("9.3.0")))[0], 0)
        code, payload = self.runtime("install", "--apply", "--source", str(self.tarball("9.4.0", "9.4.0-rc.1")))
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.current(), "runtimes/9.3.0")
        self.assertTrue((self.data / "runtimes/9.4.0-rc.1/runtime.json").is_file(), "候选版仍然装好了")
        plan = payload["plan"]
        self.assertEqual((plan["version"], plan["switch"], plan["prerelease"], plan["switch_withheld"]),
                         ("9.4.0-rc.1", False, True, True))
        self.assertEqual(plan["use_command"], "agenthelm runtime use 9.4.0-rc.1 --apply")
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["rollback"], "current 没动，无需回滚")

    def test_candidate_install_preview_and_text_explain_the_unswitched_current(self) -> None:
        self.runtime("install", "--apply", "--source", str(self.tarball("9.3.0")))
        archive = self.tarball("9.4.0", "9.4.0-rc.1")
        preview = self.runtime_text("install", "--source", str(archive))
        self.assertIn("current 不会切换（当前 current=9.3.0）", preview)
        self.assertIn("agenthelm runtime use 9.4.0-rc.1 --apply", preview)
        self.assertIn("--use", preview)
        applied = self.runtime_text("install", "--apply", "--source", str(archive))
        self.assertIn("current 未切换（当前 current=9.3.0）", applied)
        self.assertIn("agenthelm runtime use 9.4.0-rc.1 --apply", applied)
        again = self.runtime_text("install", "--apply", "--source", str(archive))
        self.assertIn("已安装", again)
        self.assertIn("current 未切换（当前 current=9.3.0）", again)
        self.assertNotIn("已是当前版本，无需改动", again)
        self.assertEqual(self.current(), "runtimes/9.3.0")

    def test_use_flag_switches_to_the_candidate(self) -> None:
        self.runtime("install", "--apply", "--source", str(self.tarball("9.3.0")))
        archive = self.tarball("9.4.0", "9.4.0-rc.1")
        code, payload = self.runtime("install", "--apply", "--use", "--source", str(archive))
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.current(), "runtimes/9.4.0-rc.1")
        self.assertEqual(payload["rollback"], "agenthelm runtime use 9.3.0 --apply")
        self.assertFalse(payload["plan"]["switch_withheld"])

    def test_rerun_with_use_switches_an_already_installed_candidate(self) -> None:
        self.runtime("install", "--apply", "--source", str(self.tarball("9.3.0")))
        archive = self.tarball("9.4.0", "9.4.0-rc.1")
        self.runtime("install", "--apply", "--source", str(archive))
        code, payload = self.runtime("install", "--apply", "--use", "--source", str(archive))
        self.assertEqual((code, payload["plan"]["action"]), (0, "switch"))
        self.assertEqual(self.current(), "runtimes/9.4.0-rc.1")

    def test_stable_install_still_switches_without_use(self) -> None:
        self.runtime("install", "--apply", "--source", str(self.tarball("9.3.0")))
        code, payload = self.runtime("install", "--apply", "--source", str(self.tarball("9.4.0")))
        self.assertEqual((code, payload["plan"]["switch"], payload["plan"]["prerelease"]), (0, True, False))
        self.assertEqual(self.current(), "runtimes/9.4.0")

    def test_first_install_has_no_current_to_protect_so_it_is_set(self) -> None:
        code, payload = self.runtime("install", "--apply", "--source", str(self.tarball("9.4.0", "9.4.0-rc.1")))
        self.assertEqual((code, payload["ok"]), (0, True), payload)
        self.assertEqual(self.current(), "runtimes/9.4.0-rc.1")

    def test_candidate_that_is_already_current_is_a_quiet_noop(self) -> None:
        archive = self.tarball("9.4.0", "9.4.0-rc.1")
        self.runtime("install", "--apply", "--source", str(archive))
        code, payload = self.runtime("install", "--apply", "--source", str(archive))
        self.assertEqual((code, payload["plan"]["action"], payload["plan"]["switch_withheld"]), (0, "none", False))

    def test_status_points_at_an_installed_but_inactive_candidate(self) -> None:
        self.runtime("install", "--apply", "--source", str(self.tarball("9.3.0")))
        self.runtime("install", "--apply", "--source", str(self.tarball("9.4.0", "9.4.0-rc.1")))
        code, payload = self.runtime("status", "--offline")
        self.assertEqual(payload["inactive_newer"], ["9.4.0-rc.1"])
        text = self.runtime_text("status", "--offline")
        self.assertIn("9.4.0-rc.1", text)
        self.assertIn("runtime use 9.4.0-rc.1 --apply", text)

    def test_installed_candidate_runtime_pins_its_own_hint(self) -> None:
        """端到端：装进用户数据目录并切换的候选版，会话开始提示带 @<版本>；稳定版不带。"""
        for version, tag, expect in (("9.4.0", "9.4.0-rc.1", "agenthelm@9.4.0-rc.1 check"), ("9.5.0", None, "agenthelm check")):
            self.runtime("install", "--apply", "--use", "--source", str(self.tarball(version, tag)))
            latest = self.base / "latest.json"
            write(latest, {"schema": 1, "marketplace": "agenthelm", "repository": "https://github.com/example/agenthelm",
                           "plugins": {"agent-pack": {"version": "99.0.0"}}, "migrations": []})
            write(self.home / ".claude/plugins/cache/agenthelm/agent-pack/1.0.0/plugin.json",
                  {"name": "agent-pack", "version": "1.0.0"})
            write(self.home / ".claude/plugins/installed_plugins.json", {"version": 2, "plugins": {
                "agent-pack@agenthelm": [{"scope": "user", "version": "1.0.0", "installPath": str(
                    self.home / ".claude/plugins/cache/agenthelm/agent-pack/1.0.0")}]}})
            result = subprocess.run([str(self.data / "current/bin/agenthelm"), "check", "--hook", "--no-project",
                                     "--home", str(self.home), "--latest", str(latest)],
                                    capture_output=True, text=True, check=False, env={**self.env, "PATH": "/usr/bin:/bin", "AGENTHELM_PYTHON": sys.executable},
                                    timeout=60)
            self.assertIn(expect, result.stdout, result.stdout + result.stderr)
            shutil.rmtree(self.home / ".cache", ignore_errors=True)
            shutil.rmtree(self.home / ".local/state", ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
