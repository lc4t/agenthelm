"""升级主路径：A1 settings 声明的市场与 Claude 已登记市场不一致时 update 预览即阻塞；A2 固定 ref 等于目标版本；A9 Codex 未固定的下一步。

HOME 是临时目录，claude / codex 是桩脚本；不会运行真实客户端。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agenthelm  # noqa: E402
import test_agenthelm_reporting as base  # noqa: E402

TAG = "v6.11.0-rc.3"
OLD = "v6.11.0-rc.2"
LATEST = {**base.LATEST, "release": {"tag": TAG, "tarball_sha256": "x"}}


def declare(home: Path, ref: str | None, repo: str = "example/agenthelm") -> None:
    source = {"source": "github", "repo": repo, **({"ref": ref} if ref else {})}
    base.write(home / ".claude/settings.json", {"extraKnownMarketplaces": {"agenthelm": {"source": source}}})


def register(home: Path, ref: str | None, repo: str = "example/agenthelm") -> None:
    source = {"source": "github", "repo": repo, **({"ref": ref} if ref else {})}
    base.write(home / ".claude/plugins/known_marketplaces.json", {"agenthelm": {"source": source, "installLocation": "x"}})


class Fixture(base.Base):
    lang = "zh"

    def setUp(self) -> None:
        super().setUp()
        base.write(self.latest_file, LATEST)
        os.environ["AGENTHELM_LANG"] = self.lang
        self.claude_plugin("1.0.0")
        self.stub("claude")

    def claude_step(self, payload: dict) -> list[dict]:
        return [s for s in payload["steps"] if s["client"] == "claude"]


class MarketSyncTests(Fixture):
    def test_preview_blocks_when_settings_ref_differs_from_the_registered_market(self) -> None:
        declare(self.home, TAG)
        register(self.home, OLD)
        code, payload = self.run_json("update", "--no-project", "--only", "plugins")
        steps = self.claude_step(payload)
        self.assertTrue(steps and all(s["status"] == "blocked" for s in steps), steps)
        self.assertEqual(steps[0]["blocker"], "market-not-synced")
        self.assertIn(OLD, steps[0]["blocked"])
        self.assertIn(TAG, steps[0]["blocked"])
        self.assertIn(f"claude plugin marketplace add example/agenthelm#{TAG}", steps[0]["next"])
        self.assertIn("重启", steps[0]["next"])
        self.assertEqual(payload["market_sync"]["status"], "blocked")
        self.assertEqual(self.called(), [])

    def test_apply_runs_nothing_on_claude_and_exits_nonzero(self) -> None:
        declare(self.home, TAG)
        register(self.home, OLD)
        self.stub("codex")
        self.codex_plugin("1.0.0")
        code, payload = self.run_json("update", "--apply", "--no-project", "--only", "plugins")
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertFalse([c for c in self.called() if c.startswith("claude")])
        self.assertTrue([c for c in self.called() if c.startswith("codex")], "other clients still run")
        _c, text = self.run_text("update", "--apply", "--no-project", "--only", "plugins")
        self.assertIn("[阻塞]", text)
        self.assertIn("Claude Code：未执行（阻塞）", text)
        self.assertIn("Codex：命令成功但版本没有变化", text)  # reported apart from Claude

    def test_unregistered_market(self) -> None:
        declare(self.home, TAG)
        base.write(self.home / ".claude/plugins/known_marketplaces.json", {"other": {}})
        _c, payload = self.run_json("update", "--no-project", "--only", "plugins")
        self.assertEqual(self.claude_step(payload)[0]["blocker"], "market-not-synced")
        self.assertEqual(payload["market_sync"]["reason_code"], "not-registered")

    def test_missing_or_corrupt_known_marketplaces_gives_a_reason_not_an_exception(self) -> None:
        declare(self.home, TAG)
        _c, payload = self.run_json("update", "--no-project", "--only", "plugins")
        self.assertEqual(payload["market_sync"]["reason_code"], "not-registered")
        base.write(self.home / ".claude/plugins/known_marketplaces.json", "{not json")
        _c, payload = self.run_json("update", "--no-project", "--only", "plugins")
        self.assertEqual(payload["market_sync"]["reason_code"], "unreadable")

    def test_consistent_does_not_block(self) -> None:
        declare(self.home, TAG)
        register(self.home, TAG)
        _c, payload = self.run_json("update", "--no-project", "--only", "plugins")
        self.assertNotIn("blocker", self.claude_step(payload)[0])
        self.assertIsNone(payload["market_sync"])

    def test_no_declaration_is_unchanged(self) -> None:
        _c, payload = self.run_json("update", "--no-project", "--only", "plugins")
        self.assertEqual(self.claude_step(payload)[0]["status"], "preview")
        self.assertIsNone(payload["market_sync"])

    def test_text_preview_names_reason_and_next_steps(self) -> None:
        declare(self.home, TAG)
        register(self.home, OLD)
        code, text = self.run_text("update", "--no-project", "--only", "plugins")
        self.assertEqual(code, 0)
        self.assertIn("[阻塞]", text)
        self.assertIn("重启 Claude Code", text)
        self.assertIn(f"marketplace add example/agenthelm#{TAG}", text)


class PinTests(Fixture):
    def test_pin_equal_to_target_is_not_a_conflict(self) -> None:
        declare(self.home, TAG)
        _c, payload = self.run_json("check", "--no-project", "--client", "claude")
        diag = payload["install_state"]["clients"]["claude"]
        self.assertIsNone(diag["market_conflict"])
        self.assertNotEqual(diag["state"], "market-conflict")
        self.assertEqual(diag["market_pin"]["at_target"], True)
        self.assertNotIn("--marketplace-source", diag["next_command"] or "")

    def test_pin_on_old_ref_still_reported_and_update_follows_it(self) -> None:
        declare(self.home, OLD)
        register(self.home, OLD)
        _c, payload = self.run_json("check", "--no-project", "--client", "claude")
        diag = payload["install_state"]["clients"]["claude"]
        self.assertEqual(diag["market_conflict"]["reason"], "pinned")
        self.assertEqual(diag["market_pin"]["at_target"], False)
        _c, text = self.run_text("update", "--no-project", "--only", "plugins")
        self.assertIn(f"固定在 {OLD}", text)
        self.assertIn(TAG, text)


class CodexNextStepTests(Fixture):
    def test_unchanged_codex_gets_the_pin_command(self) -> None:
        self.codex_plugin("1.0.0")
        self.stub("codex")
        _c, payload = self.run_json("update", "--apply", "--no-project", "--only", "plugins", "--client", "codex")
        step = [s for s in payload["steps"] if s["client"] == "codex"][0]
        self.assertEqual(step["status"], "unchanged")
        self.assertIn(f"codex plugin marketplace add example/agenthelm --ref {TAG}", step["next"])
        _c, text = self.run_text("update", "--apply", "--no-project", "--only", "plugins", "--client", "codex")
        self.assertIn(f"--ref {TAG}", text)


class EnglishTests(Fixture):
    lang = "en"

    def test_preview_and_apply(self) -> None:
        declare(self.home, TAG)
        register(self.home, OLD)
        _c, text = self.run_text("update", "--no-project", "--only", "plugins")
        self.assertIn("[blocked]", text)
        self.assertIn("restart Claude Code", text)
        code, _t = self.run_text("update", "--apply", "--no-project", "--only", "plugins")
        self.assertEqual(code, 1)
        self.assertNotIn("claude plugin update", "\n".join(self.called()))

    def test_codex_next_step(self) -> None:
        self.codex_plugin("1.0.0")
        self.stub("codex")
        _c, text = self.run_text("update", "--apply", "--no-project", "--only", "plugins", "--client", "codex")
        self.assertIn(f"codex plugin marketplace add example/agenthelm --ref {TAG}", text)


if __name__ == "__main__":
    unittest.main()
