"""install / update / 版本来源的残留缺陷：跳过优先于阻塞、runtime 命令的版本来源、--client 下的旧方式安装归属。

所有客户端命令都是临时目录里的桩脚本，HOME 是临时目录；不会运行真实的 claude / codex / cursor。
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import importlib.util
import io
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import test_agenthelm_reporting as reporting
from test_agenthelm_reporting import agenthelm, write

RUNTIME_DIR = Path(agenthelm.__file__).resolve().parent


def load_runtime_module():
    spec = importlib.util.spec_from_file_location("agenthelm_runtime_under_test", RUNTIME_DIR / "agenthelm_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SkippedBeatsBlockedTests(reporting.Base):
    def test_an_installed_plugin_is_skipped_not_blocked_when_the_client_command_is_missing(self) -> None:
        self.claude_plugin("2.0.0")  # PATH holds no claude: the marketplace step is blocked, the install step is not
        code, preview = self.run_json("install", "--client", "claude")
        by_description = {step["description"]: step["status"] for step in preview["steps"]}
        self.assertEqual(by_description["Claude Code 安装 agent-pack"], "skipped")
        self.assertEqual(by_description[next(d for d in by_description if "添加 agenthelm 市场" in d)], "blocked")
        text_code, text = self.run_text("install", "--client", "claude")
        self.assertIn("[跳过] Claude Code 安装 agent-pack", text)
        self.assertNotIn("[阻塞] Claude Code 安装 agent-pack", text)

    def test_a_step_that_is_skipped_stays_skipped_even_if_it_also_carries_a_block(self) -> None:
        steps = [agenthelm.Step("install", "claude", "已装", ["claude", "x"], blocked="no claude", skipped="不会改动：已安装"),
                 agenthelm.Step("install", "claude", "dev", None, blocked="no claude", skipped="不会改动：开发版", untouched=True),
                 agenthelm.Step("install", "claude", "真正要运行", ["claude", "y"], blocked="no claude")]
        for apply in (False, True):
            results = agenthelm.execute_steps(steps, apply=apply, recovery_root=None)
            self.assertEqual([r["status"] for r in results], ["skipped", "skipped", "blocked"])
            self.assertNotIn("blocked", results[0])
            self.assertNotIn("blocked", results[1])


class RuntimeCommandSourceTests(reporting.Base):
    A, B = "https://a.example/latest.json", "https://b.example/latest.json"

    def run_runtime(self, *args: str) -> str:
        module = load_runtime_module()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            module.main([*args, "--home", str(self.home)], agenthelm)
        return out.getvalue()

    def fetch(self):
        return mock.patch.object(agenthelm, "_fetch", side_effect=lambda url, timeout=0: dict(reporting.LATEST))

    def test_runtime_status_prints_the_source_and_never_reuses_another_sources_cache(self) -> None:
        env = agenthelm.current_env(self.home)
        with self.fetch() as fetch:
            agenthelm.load_latest(env, self.A)  # leaves a cache that belongs to A
            text = self.run_runtime("status", "--latest", self.B)
        self.assertEqual(fetch.call_count, 2)  # B was fetched, A's cache was not returned for it
        self.assertIn(f"云端版本源：{self.B}", text)
        self.assertIn(f"已忽略来自 {self.A} 的缓存", text)

    def test_runtime_status_offline_with_only_another_sources_cache_has_no_source_line(self) -> None:
        env = agenthelm.current_env(self.home)
        with self.fetch():
            agenthelm.load_latest(env, self.A)
            text = self.run_runtime("status", "--latest", self.B, "--offline")
        self.assertNotIn("云端版本源", text)

    def test_runtime_update_prints_the_source_it_downloaded_from(self) -> None:
        module = load_runtime_module()
        args = mock.Mock(source=None, latest=self.B, from_release=False)
        with self.fetch(), mock.patch.object(agenthelm, "release_source", return_value=self.root):
            _root, origin, line = module.resolve_source(agenthelm, agenthelm.current_env(self.home), args,
                                                         self.root, prefer_release=True)
        self.assertIn("GitHub Release", origin)
        self.assertEqual(line, f"云端版本源：{self.B}")
        args.source = self.root
        self.assertIsNone(module.resolve_source(agenthelm, agenthelm.current_env(self.home), args, self.root,
                                                prefer_release=True)[2])


class LegacyUnderClientFilterTests(reporting.Base):
    """旧方式安装不属于任何客户端：--client 时仍列出；路径属于所选客户端才计入 up_to_date，归属不明则不计入。"""

    def legacy(self, where: str) -> None:
        write(self.home / where / "agent-pack/plugin.json", {"name": "agent-pack", "version": "1.0.0"})

    def check(self, *client: str) -> dict:
        args = ["check", "--no-project"] + [item for name in client for item in ("--client", name)]
        return self.run_json(*args)[1]

    def test_owner_is_judged_from_the_recorded_path(self) -> None:
        env = agenthelm.current_env(self.home)
        self.assertEqual(agenthelm.legacy_owner(env, {"path": str(self.home / ".claude/agent-pack/plugins/x")}), "claude")
        self.assertEqual(agenthelm.legacy_owner(env, {"path": str(self.home / ".agents/plugins/plugins/x")}), "codex")
        self.assertIsNone(agenthelm.legacy_owner(env, {"path": str(self.home / "elsewhere/x")}))
        self.assertIsNone(agenthelm.legacy_owner(env, {}))

    def test_a_legacy_entry_of_another_client_is_listed_but_does_not_block_up_to_date(self) -> None:
        self.claude_plugin("2.0.0")
        self.legacy(".agents/plugins/plugins")  # the Codex personal-marketplace location
        report = self.check("claude")
        legacy = [item for item in report["plugins"] if item["client"] == "legacy"]
        self.assertEqual([item["owner"] for item in legacy], ["codex"])  # still shown
        self.assertEqual(report["summary"]["legacy_installs"], 0)
        self.assertEqual(report["summary"]["legacy_other_clients"], 1)
        self.assertTrue(report["summary"]["up_to_date"])
        _code, text = self.run_text("check", "--no-project", "--client", "claude")
        self.assertIn("不属于所选客户端", text)

    def test_a_legacy_entry_of_the_selected_client_counts(self) -> None:
        self.claude_plugin("2.0.0")
        self.legacy(".claude/agent-pack/plugins")
        report = self.check("claude")
        self.assertEqual(report["summary"]["legacy_installs"], 1)
        self.assertFalse(report["summary"]["up_to_date"])
        self.assertEqual(self.check("claude", "codex")["summary"]["legacy_installs"], 1)

    def test_without_client_every_legacy_entry_counts_as_before(self) -> None:
        self.claude_plugin("2.0.0")
        self.legacy(".agents/plugins/plugins")
        report = self.check()
        self.assertEqual((report["summary"]["legacy_installs"], report["summary"]["legacy_other_clients"]), (1, 0))
        self.assertFalse(report["summary"]["up_to_date"])

    def test_an_entry_whose_owner_cannot_be_told_is_listed_and_not_counted_under_client(self) -> None:
        self.claude_plugin("2.0.0")
        self.legacy(".agents/plugins/plugins")
        with mock.patch.object(agenthelm, "legacy_owner", return_value=None):
            report = self.check("claude")
            unfiltered = self.check()
        self.assertEqual(len([i for i in report["plugins"] if i["client"] == "legacy"]), 1)
        self.assertEqual((report["summary"]["legacy_installs"], report["summary"]["legacy_other_clients"]), (0, 1))
        self.assertEqual(unfiltered["summary"]["legacy_installs"], 1)


if __name__ == "__main__":
    unittest.main()
