"""install / update 的报告与版本来源：失败原因、读回验证、缓存来源、按客户端选择与忽略（#33 #39 #32 #34）。

所有客户端命令都是临时目录里的桩脚本，HOME 是临时目录；不会运行真实的 claude / codex / cursor。
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import agenthelm  # noqa: E402

LATEST = {
    "schema": 1, "marketplace": "agenthelm", "released_at": "2026-10-21",
    "repository": "https://github.com/example/agenthelm",
    "plugins": {"agent-pack": {"version": "2.0.0"}},
    "migrations": [],
}


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls.log"
        self.latest_file = self.root / "latest.json"
        write(self.latest_file, LATEST)
        patch = mock.patch.dict(os.environ, {"PATH": str(self.bin), "AGENTHELM_LANG": "zh"})
        patch.start()
        self.addCleanup(patch.stop)
        for stop in (mock.patch.object(agenthelm, "known_cli_locations", return_value=[]),
                     mock.patch.object(agenthelm, "github_release", return_value={})):
            stop.start()
            self.addCleanup(stop.stop)

    # -- fixtures
    def stub(self, name: str, body: str = "") -> None:
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\nimport sys\nopen({str(self.calls)!r}, 'a').write("
                        f"'{name} ' + ' '.join(sys.argv[1:]) + '\\n')\n{body}\n")
        path.chmod(0o755)

    def installing_codex(self) -> None:
        self.stub("codex", f"import json, pathlib\np = pathlib.Path({str(self.home)!r}) / '.codex/plugins/cache/agenthelm/agent-pack/2.0.0'\n"
                           "if 'add' in sys.argv and 'marketplace' not in sys.argv:\n    p.mkdir(parents=True)\n"
                           "    (p / 'plugin.json').write_text(json.dumps({'name': 'agent-pack', 'version': '2.0.0'}))")

    def called(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def claude_plugin(self, version: str, scope: str = "user") -> None:
        path = self.home / f".claude/plugins/cache/agenthelm/agent-pack/{version}"
        write(path / "plugin.json", {"name": "agent-pack", "version": version})
        write(self.home / ".claude/plugins/installed_plugins.json", {"version": 2, "plugins": {
            "agent-pack@agenthelm": [{"scope": scope, "installPath": str(path), "version": version}]}})

    def codex_plugin(self, version: str) -> None:
        write(self.home / f".codex/plugins/cache/agenthelm/agent-pack/{version}/plugin.json",
              {"name": "agent-pack", "version": version})

    def cursor_plugin(self, version: str) -> None:
        write(self.home / ".cursor/plugins/local/agent-pack/plugin.json", {"name": "agent-pack", "version": version})

    def config(self, data: dict) -> None:
        write(self.home / ".config/agenthelm/config.json", data)

    def run_cli(self, *args: str, fmt: str = "json") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = agenthelm.main([*args, "--home", str(self.home), "--latest", str(self.latest_file), "--output", fmt])
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *args: str) -> tuple[int, dict]:
        code, out, _err = self.run_cli(*args)
        return code, json.loads(out)

    def run_text(self, *args: str) -> tuple[int, str]:
        code, out, err = self.run_cli(*args, fmt="text")
        return code, out + err


class FailureReasonTests(Base):
    """#33：失败的步骤显示命令、退出码、stderr 与已知原因，后续步骤是否执行写清楚。"""

    DECLARED = ("Cannot add marketplace \"agenthelm\": its network source differs from the one declared for it "
                "in settings (kind, target, or a fetch-shaping field such as headers / ref / path / sparsePaths); "
                "the source must match the one declared for this name in settings (or change the declaration).")

    def failing_claude(self) -> None:
        noise = "".join(f"noise line {i}\\n" for i in range(30))
        self.stub("claude", f"sys.stderr.write('{noise}' + {self.DECLARED!r} + '\\n')\nraise SystemExit(1)")

    def test_the_text_output_shows_command_exit_code_stderr_and_the_known_reason(self) -> None:
        self.failing_claude()
        code, text = self.run_text("install", "--client", "claude", "--marketplace-source", "example/x#rc", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("[失败] Claude Code 添加 agenthelm 市场", text)
        self.assertIn("$ " + str(self.bin / "claude") + " plugin marketplace add example/x#rc", text)
        self.assertIn("退出码：1", text)
        self.assertIn("network source differs", text)
        self.assertIn("市场来源与 settings.json 里的声明不一致", text)
        self.assertIn("下一步：", text)
        self.assertIn("extraKnownMarketplaces.agenthelm", text)
        self.assertIn("前面省略", text)  # 31 lines of stderr are cut to the last 20
        self.assertNotIn("noise line 0\n", text)
        # the next step of the same client did not run, and the summary says so
        self.assertIn("[未执行] Claude Code 安装 agent-pack", text)
        self.assertIn("同一客户端前面的步骤失败", text)
        self.assertIn("整体：没有全部成功", text)
        self.assertEqual(self.called(), ["claude plugin marketplace add example/x#rc"])

    def test_json_has_error_exit_code_and_stderr(self) -> None:
        self.failing_claude()
        code, payload = self.run_json("install", "--client", "claude", "--marketplace-source", "example/x#rc", "--apply")
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        failed = payload["steps"][0]
        self.assertEqual((failed["status"], failed["exit_code"]), ("failed", 1))
        self.assertIn("network source differs", failed["stderr"])
        self.assertLessEqual(len(failed["stderr"].splitlines()), 21)
        self.assertIn("命令退出码 1", failed["error"])
        self.assertIn("settings.json", failed["reason"])
        self.assertEqual(payload["steps"][1]["status"], "not_run")

    def test_an_unknown_failure_still_shows_stderr_and_a_missing_executable_is_reported(self) -> None:
        self.stub("codex", "sys.stderr.write('boom\\n')\nraise SystemExit(7)")
        _code, payload = self.run_json("install", "--client", "codex", "--apply")
        self.assertEqual((payload["steps"][0]["exit_code"], payload["steps"][0]["stderr"]), (7, "boom"))
        self.assertNotIn("reason", payload["steps"][0])
        step = agenthelm.Step("install", "codex", "x", [str(self.root / "missing-binary")])
        record = agenthelm.execute_steps([step], apply=True, recovery_root=None)[0]
        self.assertEqual((record["status"], record["exit_code"]), ("failed", None))
        self.assertTrue(record["error"])

    def test_one_clients_failure_does_not_stop_the_others_and_the_summary_separates_them(self) -> None:
        self.failing_claude()
        self.installing_codex()
        code, text = self.run_text("install", "--client", "claude", "--client", "codex", "--apply")
        self.assertEqual(code, 1)
        self.assertEqual([c.split()[0] for c in self.called()], ["claude", "codex", "codex"])
        self.assertIn("Claude Code：失败", text)
        self.assertIn("其他已完成：Codex", text)


class ReadBackTests(Base):
    """#39：[done] 要读回版本；成功但没变化要说明原因，整体不显示成功。"""

    def test_a_command_that_succeeds_without_changing_the_version_is_reported_as_unchanged(self) -> None:
        self.codex_plugin("1.0.0")
        self.stub("codex")
        code, payload = self.run_json("update", "--apply", "--no-project", "--only", "plugins")
        step = payload["steps"][0]
        self.assertEqual((step["status"], step["before"], step["after"]), ("unchanged", "1.0.0", "1.0.0"))
        self.assertIn("没有固定到此版本", step["reason"])
        self.assertFalse(payload["ok"])
        self.assertEqual(code, 1)
        _code, text = self.run_text("update", "--apply", "--no-project", "--only", "plugins")
        self.assertIn("[完成，无变化] Codex 升级", text)
        self.assertIn("仍是 1.0.0（目标 2.0.0）", text)
        self.assertIn("原因：该客户端的市场没有固定到此版本", text)
        self.assertNotIn("整体：已完成", text)
        self.assertIn("整体：没有全部成功", text)

    def test_a_version_that_moved_is_shown_as_from_to(self) -> None:
        self.claude_plugin("1.0.0")
        bump = (f"import json, pathlib\nf = pathlib.Path({str(self.home / '.claude/plugins/installed_plugins.json')!r})\n"
                "d = json.loads(f.read_text())\nif 'update' in sys.argv and 'marketplace' not in sys.argv:\n"
                "    d['plugins']['agent-pack@agenthelm'][0]['version'] = '2.0.0'\n    f.write_text(json.dumps(d))")
        self.stub("claude", bump)
        code, payload = self.run_json("update", "--apply", "--no-project", "--only", "plugins")
        plugin_step = [s for s in payload["steps"] if s["description"].startswith("Claude Code 更新")][0]
        self.assertEqual((plugin_step["status"], plugin_step["before"], plugin_step["after"]),
                         ("applied", "1.0.0", "2.0.0"))
        self.assertTrue(payload["ok"])
        self.assertEqual(code, 0)
        self.claude_plugin("1.0.0")
        _code, text = self.run_text("update", "--apply", "--no-project", "--only", "plugins")
        self.assertIn("1.0.0 → 2.0.0", text)
        self.assertIn("整体：已完成", text)

    def test_install_on_an_installed_plugin_says_what_it_does_and_does_not_run_it(self) -> None:
        self.claude_plugin("1.0.0")
        self.stub("claude")
        _code, preview = self.run_text("install", "--client", "claude")
        self.assertIn("[跳过] Claude Code 安装 agent-pack", preview)
        self.assertIn("已安装 1.0.0，云端是 2.0.0：install 不会升级已安装的插件，请用 `npx @sakanano-moe/agenthelm update`", preview)
        _code, payload = self.run_json("install", "--client", "claude", "--apply")
        self.assertEqual([s["status"] for s in payload["steps"]], ["applied", "skipped"])
        self.assertEqual(self.called(), ["claude plugin marketplace add example/agenthelm"])  # no plugin install
        self.assertTrue(payload["ok"])

    def test_a_fresh_install_reads_back_the_new_version(self) -> None:
        self.installing_codex()
        _code, text = self.run_text("install", "--client", "codex")
        self.assertIn("[预览] Codex 安装 agent-pack", text)  # a preview reads nothing back
        _code, payload = self.run_json("install", "--client", "codex", "--apply")
        step = payload["steps"][1]
        self.assertEqual((step["status"], step["before"], step["after"]), ("applied", None, "2.0.0"))


class CacheSourceTests(Base):
    """#32：缓存只在来源相同时使用；--refresh 在四个命令上一致；输出显示实际来源。"""

    A, B = "https://a.example/latest.json", "https://b.example/latest.json"

    def env(self) -> agenthelm.Env:
        return agenthelm.Env(home=self.home, variables={})

    def fetch(self, label: str):
        return mock.patch.object(agenthelm, "_fetch", side_effect=lambda url, timeout=0: {**LATEST, "released_at": f"{label}:{url}"})

    def test_a_cache_from_another_source_is_never_returned(self) -> None:
        env = self.env()
        with self.fetch("fresh") as fetch:
            latest_a, _ = agenthelm.load_latest(env, self.A)
            latest_b, origin = agenthelm.load_latest(env, self.B)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(latest_b["released_at"], f"fresh:{self.B}")
        self.assertEqual(origin["source"], self.B)
        self.assertFalse(origin["cached"])
        self.assertEqual(origin["ignored_cache"], self.A)
        self.assertIn(f"已忽略来自 {self.A} 的缓存", agenthelm.origin_line(origin))
        self.assertEqual(latest_a["released_at"], f"fresh:{self.A}")

    def test_the_same_source_still_uses_a_fresh_cache(self) -> None:
        env = self.env()
        with self.fetch("fresh") as fetch:
            agenthelm.load_latest(env, self.A)
            _latest, origin = agenthelm.load_latest(env, self.A)
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(origin["cached"])
        line = agenthelm.origin_line(origin)
        self.assertIn(f"云端版本源：{self.A}（缓存，获取于 ", line)

    def test_an_explicit_source_does_not_fall_back_to_another_sources_cache_even_when_offline(self) -> None:
        env = self.env()
        with self.fetch("fresh"):
            agenthelm.load_latest(env, self.A)
        with mock.patch.object(agenthelm, "_fetch", side_effect=OSError("down")):
            with self.assertRaises(agenthelm.CheckError):
                agenthelm.load_latest(env, self.B)
            with self.assertRaisesRegex(agenthelm.CheckError, "缓存来自另一个来源"):
                agenthelm.load_latest(env, self.B, offline=True)
            # the same source still falls back to its own stale cache
            cache = env.cache_dir / "latest.json"
            stale = json.loads(cache.read_text())
            stale["fetched_at"] = time.time() - 3 * agenthelm.CACHE_TTL_SECONDS
            cache.write_text(json.dumps(stale))
            _latest, origin = agenthelm.load_latest(env, self.A)
        self.assertTrue(origin["stale"])
        self.assertIn("已过期", agenthelm.origin_line(origin))

    def test_the_environment_variable_is_an_explicit_source_too(self) -> None:
        env = agenthelm.Env(home=self.home, variables={agenthelm.ENV_LATEST: self.B})
        with self.fetch("fresh"):
            agenthelm.load_latest(self.env(), self.A)
            _latest, origin = agenthelm.load_latest(env)
        self.assertEqual((origin["source"], origin["cached"]), (self.B, False))

    def test_the_default_sources_ignore_a_cache_left_by_an_explicit_one(self) -> None:
        env = self.env()
        with self.fetch("fresh"):
            agenthelm.load_latest(env, self.A)
            _latest, origin = agenthelm.load_latest(env)
        self.assertEqual(origin["source"], agenthelm.DEFAULT_LATEST_URLS[0])
        self.assertEqual(origin["ignored_cache"], self.A)

    def test_all_four_commands_accept_refresh_with_the_same_help(self) -> None:
        parsers = agenthelm.parser()._subparsers._group_actions[0].choices
        helps = set()
        for name in ("check", "install", "update", "migrate"):
            action = next(a for a in parsers[name]._actions if "--refresh" in a.option_strings)
            helps.add(action.help)
            self.assertTrue(parsers[name].parse_args(["--refresh"]).refresh)
        self.assertEqual(len(helps), 1)

    def test_install_update_and_migrate_print_the_source_they_used(self) -> None:
        self.stub("claude")
        for command in (("install", "--client", "claude"), ("update", "--no-project"), ("migrate",)):
            _code, text = self.run_text(*command, "--refresh")
            self.assertIn(f"云端版本源：{self.latest_file}", text)
            _code, payload = self.run_json(*command)
            self.assertEqual(payload["latest"]["source"], str(self.latest_file))
            self.assertFalse(payload["latest"]["cached"])


class ClientSelectionTests(Base):
    """#34：--client、ignored_clients、按客户端的汇总、未使用客户端的提示、Cursor 没有发布包信息。"""

    def three_clients(self) -> None:
        self.claude_plugin("2.0.0")      # current
        self.codex_plugin("1.0.0")       # behind, and the update would not change it
        self.cursor_plugin("1.0.0")      # behind, and latest.json has no release info
        self.stub("claude")
        self.stub("codex")

    def test_client_claude_ignores_the_state_of_codex_and_cursor(self) -> None:
        self.three_clients()
        code, text = self.run_text("check", "--no-project", "--strict")
        self.assertEqual(code, 1)  # the others are behind
        code, text = self.run_text("check", "--no-project", "--strict", "--client", "claude")
        self.assertEqual(code, 0)
        self.assertNotIn("codex", text.replace("只处理所选客户端", ""))
        self.assertIn("只处理所选客户端：Claude Code", text)
        _code, report = self.run_json("check", "--no-project", "--client", "claude")
        self.assertEqual({item["client"] for item in report["plugins"]}, {"claude"})
        self.assertTrue(report["summary"]["up_to_date"])
        self.assertEqual(report["clients"]["excluded"], ["codex", "cursor"])

    def test_update_client_claude_runs_nothing_for_other_clients_and_exits_zero(self) -> None:
        self.three_clients()
        code, payload = self.run_json("update", "--no-project", "--apply", "--client", "claude")
        self.assertEqual(code, 0)
        self.assertEqual(payload["steps"], [])
        self.assertEqual(self.called(), [])

    def test_ignored_clients_are_shown_as_ignored_and_do_not_affect_the_exit_code(self) -> None:
        self.three_clients()
        self.config({"ignored_clients": ["codex", "cursor"], "language": "zh"})
        code, text = self.run_text("check", "--no-project", "--strict")
        self.assertEqual(code, 0)
        self.assertIn("[已忽略] Codex", text)
        self.assertIn("[已忽略] Cursor", text)
        self.assertNotIn("[codex]", text)
        self.assertNotIn("1.0.0", text)
        _code, listed = self.run_text("check", "--no-project", "--show-skipped")
        self.assertIn("agent-pack 1.0.0 → 2.0.0（未检查）", listed)
        _code, report = self.run_json("check", "--no-project")
        self.assertEqual({item["client"] for item in report["ignored_plugins"]}, {"codex", "cursor"})
        self.assertEqual(report["summary"]["plugins_behind"], 0)
        # nothing is updated for them, and no [failed] appears for them
        code, text = self.run_text("update", "--no-project", "--apply")
        self.assertEqual(code, 0)
        self.assertNotIn("失败", text)
        self.assertEqual(self.called(), [])
        # an explicit --client wins over the ignore setting
        _code, report = self.run_json("check", "--no-project", "--client", "codex")
        self.assertEqual([item["client"] for item in report["plugins"]], ["codex"])

    def test_the_session_start_notice_counts_only_clients_that_are_not_ignored(self) -> None:
        self.three_clients()
        env = agenthelm.Env(home=self.home, variables={})
        report = agenthelm.run_check(env, None, latest_source=str(self.latest_file))
        notice = agenthelm.one_line(report)
        self.assertIn("2 个插件可更新", notice)
        self.assertIn("Claude Code：已是最新", notice)
        self.assertIn("Codex：1 个可更新", notice)
        self.config({"ignored_clients": ["codex", "cursor"]})
        report = agenthelm.run_check(env, None, latest_source=str(self.latest_file))
        self.assertIsNone(agenthelm.one_line(report))
        self.assertEqual(report["summary"]["by_client"], {"claude": {"behind": 0, "total": 1}})

    def test_cursor_without_release_info_is_unavailable_not_failed_and_does_not_block_others(self) -> None:
        self.claude_plugin("1.0.0")
        self.cursor_plugin("1.0.0")
        bump = (f"import json, pathlib\nf = pathlib.Path({str(self.home / '.claude/plugins/installed_plugins.json')!r})\n"
                "d = json.loads(f.read_text())\nif 'marketplace' not in sys.argv:\n"
                "    d['plugins']['agent-pack@agenthelm'][0]['version'] = '2.0.0'\n    f.write_text(json.dumps(d))")
        self.stub("claude", bump)
        code, payload = self.run_json("update", "--no-project", "--apply")
        statuses = {(s["client"], s["status"]) for s in payload["steps"]}
        self.assertIn(("cursor", "unavailable"), statuses)
        self.assertNotIn("failed", {s["status"] for s in payload["steps"]})
        self.assertIn(("claude", "applied"), statuses)
        self.assertEqual(code, 0)
        cursor = [s for s in payload["steps"] if s["client"] == "cursor"][0]
        self.assertIn("没有发布 tag 与摘要", cursor["error"])
        self.assertIn("--source", cursor["next"])
        self.assertIn("ignore-client cursor", cursor["next"])
        _code, text = self.run_text("update", "--no-project", "--apply")
        self.assertIn("[不可用] Cursor", text)
        self.assertIn("Cursor：不可用", text)

    def test_a_client_that_looks_unused_gets_a_one_time_hint_with_the_ignore_command(self) -> None:
        self.claude_plugin("2.0.0")
        self.cursor_plugin("1.0.0")
        self.stub("claude")
        _code, first = self.run_text("check", "--no-project")
        self.assertIn("检测到 Cursor", first)
        self.assertIn("config ignore-client cursor", first)
        self.assertIn("只显示一次", first)
        _code, second = self.run_text("check", "--no-project")
        self.assertNotIn("检测到 Cursor", second)
        # JSON never consumes it, and a single detected client never gets one
        self.assertIn("cursor", agenthelm.run_check(agenthelm.Env(home=self.home, variables={}), None,
                                                    latest_source=str(self.latest_file))["clients"]["unused"])
        (self.home / ".cursor").rename(self.root / "moved")
        self.assertEqual(agenthelm.run_check(agenthelm.Env(home=self.home, variables={}), None,
                                             latest_source=str(self.latest_file))["clients"]["unused"], [])

    def test_install_leaves_out_ignored_clients_unless_asked_for_explicitly(self) -> None:
        self.stub("claude")
        self.stub("codex")
        self.config({"ignored_clients": ["codex"]})
        _code, payload = self.run_json("install")
        self.assertEqual(payload["clients"], ["claude"])
        _code, payload = self.run_json("install", "--client", "codex")
        self.assertEqual(payload["clients"], ["codex"])


class IgnoreClientCommandTests(Base):
    def config_cli(self, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = agenthelm.main(["config", "--home", str(self.home), *args, "--output", "json"])
        return code, json.loads(out.getvalue())

    def data(self) -> dict:
        return json.loads((self.home / ".config/agenthelm/config.json").read_text())

    def test_preview_then_apply_changes_only_that_key_and_backs_up_the_file(self) -> None:
        self.config({"language": "zh", "custom": 1})
        code, preview = self.config_cli("ignore-client", "cursor")
        self.assertEqual((code, preview["applied"]), (0, False))
        self.assertEqual(preview["changes"], [{"key": "ignored_clients", "from": None, "to": ["cursor"]}])
        self.assertNotIn("ignored_clients", self.data())
        _code, applied = self.config_cli("ignore-client", "cursor", "--apply")
        self.assertTrue(applied["applied"])
        self.assertEqual(self.data(), {"language": "zh", "custom": 1, "ignored_clients": ["cursor"]})
        backup = Path(applied["recovery"]) / "config.json"
        self.assertEqual(json.loads(backup.read_text()), {"language": "zh", "custom": 1})
        self.config_cli("ignore-client", "codex", "--apply")
        self.assertEqual(self.data()["ignored_clients"], ["codex", "cursor"])
        _code, again = self.config_cli("ignore-client", "codex")
        self.assertEqual(again["changes"], [])

    def test_unignore_removes_the_key_when_nothing_is_left_and_show_lists_the_setting(self) -> None:
        self.config({"ignored_clients": ["cursor"], "language_hint": False})
        _code, shown = self.config_cli("show")
        self.assertEqual(shown["ignored_clients"], ["cursor"])
        self.config_cli("unignore-client", "cursor", "--apply")
        self.assertEqual(self.data(), {"language_hint": False})

    def test_a_file_that_cannot_be_parsed_is_not_overwritten(self) -> None:
        write(self.home / ".config/agenthelm/config.json", "{broken")
        code, payload = self.config_cli("ignore-client", "cursor", "--apply")
        self.assertEqual(code, 2)
        self.assertEqual((self.home / ".config/agenthelm/config.json").read_text(), "{broken")


if __name__ == "__main__":
    unittest.main()
