"""#31：check 识别并说明插件安装状态（none / partial / marketplace / dev / legacy / mixed / market-conflict）。

HOME 是临时目录，客户端命令不存在（PATH 被清空）；不会运行真实的 claude / codex / cursor，也不写真实的配置目录。
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import agenthelm  # noqa: E402

NAMES = ("agent-pack", "agents-init", "opinion-manager", "project-orchestrator")
LATEST = {"schema": 1, "marketplace": "agenthelm", "released_at": "2026-10-21",
          "repository": "https://github.com/example/agenthelm",
          "plugins": {name: {"version": "2.0.0"} for name in NAMES}, "migrations": []}


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
        (self.root / "bin").mkdir()
        self.latest = self.root / "latest.json"
        write(self.latest, LATEST)
        patch = mock.patch.dict(os.environ, {"PATH": str(self.root / "bin"), "AGENTHELM_LANG": "zh"})
        patch.start()
        self.addCleanup(patch.stop)
        for stop in (mock.patch.object(agenthelm, "known_cli_locations", return_value=[]),
                     mock.patch.object(agenthelm, "github_release", return_value={})):
            stop.start()
            self.addCleanup(stop.stop)
        self.records: dict = {}

    # -- fixtures
    def claude_dir(self) -> None:
        (self.home / ".claude").mkdir(exist_ok=True)

    def codex_dir(self) -> None:
        (self.home / ".codex").mkdir(exist_ok=True)

    def claude(self, plugin: str, version: str = "2.0.0", market: str = "agenthelm", scope: str = "user",
               link_to: Path | None = None) -> Path:
        path = self.home / f".claude/plugins/cache/{market}/{plugin}/{version}"
        if link_to is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(link_to)
        else:
            write(path / "plugin.json", {"name": plugin, "version": version})
        self.records.setdefault(f"{plugin}@{market}", []).append(
            {"scope": scope, "installPath": str(path), "version": version})
        write(self.home / ".claude/plugins/installed_plugins.json", {"version": 2, "plugins": self.records})
        return path

    def codex(self, plugin: str, version: str = "2.0.0", market: str = "agenthelm") -> None:
        write(self.home / f".codex/plugins/cache/{market}/{plugin}/{version}/plugin.json", {"name": plugin, "version": version})

    def cursor(self, plugin: str, version: str = "2.0.0", link_to: Path | None = None) -> None:
        path = self.home / ".cursor/plugins/local" / plugin
        if link_to is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(link_to)
        else:
            write(path / "plugin.json", {"name": plugin, "version": version})

    def legacy(self, plugin: str, version: str = "1.0.0", where: str = ".claude/agent-pack/plugins") -> None:
        # Default: Claude Code's old location; the per-client diagnosis only counts a legacy entry for the client its path says.
        write(self.home / where / plugin / "plugin.json", {"name": plugin, "version": version})

    def working_copy(self, plugin: str, version: str = "9.9.9") -> Path:
        path = self.root / "work" / plugin
        write(path / "plugin.json", {"name": plugin, "version": version})
        return path

    # -- running
    def run_cli(self, *args: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = agenthelm.main([*args, "--home", str(self.home)])
        return code, out.getvalue()

    def check(self, *extra: str) -> dict:
        code, out = self.run_cli("check", "--latest", str(self.latest), "--output", "json", "--no-project", *extra)
        return json.loads(out)["install_state"]["clients"]

    def state(self, client: str = "claude", *extra: str) -> dict:
        return self.check("--client", client, *extra)[client]


class StateClassification(Base):
    def test_none_when_the_client_exists_without_any_plugin(self) -> None:
        self.claude_dir()
        diag = self.state()
        self.assertEqual((diag["state"], diag["missing"]), ("none", list(NAMES)))
        self.assertEqual(diag["next_command"], f"{agenthelm.CLI} install --client claude")
        self.assertFalse(diag["modifies_existing"])

    def test_partial_lists_what_is_missing_and_mentions_the_agents_init_dependency(self) -> None:
        self.claude("agent-pack")
        self.claude("agents-init")
        diag = self.state()
        self.assertEqual(diag["state"], "partial")
        self.assertEqual(diag["missing"], ["opinion-manager", "project-orchestrator"])
        self.assertIn("agents-init 依赖", diag["summary"])
        self.assertFalse(diag["modifies_existing"])  # install skips what is already there

    def test_cursor_partial_install_rewrites_so_it_names_the_recovery_place(self) -> None:
        self.cursor("agent-pack")
        diag = self.state("cursor")
        self.assertEqual(diag["state"], "partial")
        self.assertTrue(diag["modifies_existing"])
        self.assertIn("replaced-plugin", diag["recovery"])

    def test_marketplace_current_behind_and_ahead(self) -> None:
        for plugin in NAMES:
            self.claude(plugin)
        self.assertEqual((self.state()["state"], self.state()["sub_status"]), ("marketplace", "current"))
        self.assertIsNone(self.state()["next_command"])
        (self.home / ".claude/plugins/installed_plugins.json").unlink()
        self.records.clear()
        for plugin in NAMES:
            self.claude(plugin, "1.0.0")
        diag = self.state()
        self.assertEqual((diag["state"], diag["sub_status"]), ("marketplace", "behind"))
        self.assertEqual(diag["next_command"], f"{agenthelm.CLI} update --client claude")
        self.assertTrue(diag["modifies_existing"])
        self.assertIn("~/.claude/plugins/cache/agenthelm", diag["recovery"])
        self.records.clear()
        for plugin in NAMES:
            self.claude(plugin, "3.0.0")
        self.assertEqual(self.state()["sub_status"], "ahead")

    def test_dev_from_another_marketplace_a_symlink_and_a_local_directory_marketplace(self) -> None:
        for plugin in NAMES:
            self.claude(plugin, "9.0.0", market="agenthelm-dev")
        diag = self.state()
        self.assertEqual(diag["state"], "dev")
        self.assertEqual({item["origin"] for item in diag["untouched"]}, {"other-marketplace:agenthelm-dev"})
        self.assertEqual(diag["untouched"][0]["path"], str(self.home / ".claude/plugins/cache/agenthelm-dev/agent-pack/9.0.0"))
        self.assertIn("不会改动", diag["modifies_note"])

        self.records.clear()
        linked = self.working_copy("agent-pack")
        self.claude("agent-pack", "9.9.9", link_to=linked)
        diag = self.state()
        self.assertEqual(diag["state"], "dev")
        self.assertEqual(diag["untouched"], [{"plugin": "agent-pack", "path": str(self.home / ".claude/plugins/cache/agenthelm/agent-pack/9.9.9"),
                                              "origin": "symlink"}])
        self.assertEqual(diag["missing"], ["agents-init", "opinion-manager", "project-orchestrator"])

        self.records.clear()
        for plugin in NAMES:
            self.claude(plugin)
        write(self.home / ".claude/plugins/known_marketplaces.json",
              {"agenthelm": {"source": {"source": "directory", "path": "/work/agenthelm"}}})
        self.assertEqual({item["origin"] for item in self.state()["untouched"]}, {"local-directory-marketplace"})

    def test_dev_in_codex_and_cursor(self) -> None:
        for plugin in NAMES:
            self.codex(plugin, market="agenthelm-fork")
        diag = self.state("codex")
        self.assertEqual((diag["state"], diag["untouched"][0]["origin"]), ("dev", "other-marketplace:agenthelm-fork"))
        self.cursor("agent-pack", link_to=self.working_copy("agent-pack"))
        self.assertEqual(self.state("cursor")["untouched"][0]["origin"], "symlink")

    def test_legacy_only(self) -> None:
        self.claude_dir()
        self.legacy("agent-pack")
        diag = self.state()
        self.assertEqual(diag["state"], "legacy")
        self.assertEqual(diag["next_command"], f"{agenthelm.CLI} migrate --install")
        self.assertTrue(diag["modifies_existing"])
        self.assertIn("migrate", diag["recovery"])

    def test_mixed_marketplace_plus_another_marketplace_and_marketplace_plus_legacy(self) -> None:
        for plugin in NAMES:
            self.claude(plugin)
        self.claude("agent-pack", "9.0.0", market="agenthelm-dev")
        diag = self.state()
        self.assertEqual(diag["state"], "mixed")
        self.assertEqual(diag["conflicts"], [{"plugin": "agent-pack", "versions_differ": True, "effective": None}])
        self.assertEqual(diag["next_command"], f"{agenthelm.CLI} doctor")
        self.assertFalse(diag["modifies_existing"])
        self.assertIn("未确认", diag["summary"])
        self.records.clear()
        for plugin in NAMES:
            self.claude(plugin)
        self.legacy("agent-pack")
        diag = self.state()
        self.assertEqual(diag["state"], "mixed")
        self.assertEqual(diag["next_command"], f"{agenthelm.CLI} migrate")
        self.assertTrue(diag["modifies_existing"])

    def test_legacy_entries_are_attributed_by_path_in_the_per_client_diagnosis(self) -> None:
        self.claude_dir()
        for plugin in NAMES:
            self.claude(plugin)
        self.codex_dir()
        self.legacy("agent-pack", where=".agents/plugins/plugins")  # the Codex personal-marketplace location
        self.assertEqual(self.state("claude")["state"], "marketplace")  # not legacy/mixed because of Codex's entry
        self.assertEqual(self.state("claude")["installs"][0]["kind"], "marketplace")
        self.assertEqual(self.state("codex")["state"], "legacy")

    def test_a_legacy_entry_of_a_claude_path_is_not_counted_for_codex(self) -> None:
        self.codex_dir()
        self.legacy("agent-pack")  # ~/.claude/agent-pack/plugins
        self.assertEqual(self.state("claude")["state"], "legacy")
        self.assertNotIn("legacy", [item["kind"] for item in self.state("codex")["installs"]])

    def test_a_legacy_entry_with_an_unknown_owner_stays_in_every_diagnosis(self) -> None:
        self.claude_dir()
        self.codex_dir()
        env = agenthelm.current_env(self.home)
        item = {"client": "legacy", "plugin": "agent-pack", "path": str(self.home / "elsewhere/agent-pack"), "version": "1.0.0"}
        latest = {"plugins": {"agent-pack": {"version": "2.0.0"}}}
        state = agenthelm.install_state(env, latest, [item], [], [], ["claude", "codex"])["clients"]
        self.assertEqual({name: state[name]["state"] for name in ("claude", "codex")}, {"claude": "legacy", "codex": "legacy"})

    def test_two_scopes_of_the_same_marketplace_are_not_a_conflict(self) -> None:
        for plugin in NAMES:
            self.claude(plugin)
        self.claude("agent-pack", scope="project")
        self.assertEqual(self.state()["state"], "marketplace")


class MarketConflict(Base):
    def settings(self, source: dict, where: str = "user") -> Path:
        path = (self.home / ".claude" if where == "user" else self.root / "project/.claude") / "settings.json"
        write(path, {"extraKnownMarketplaces": {"agenthelm": {"source": source}}})
        return path

    def test_a_pinned_ref_is_reported_with_the_source_that_add_would_accept(self) -> None:
        path = self.settings({"source": "github", "repo": "example/agenthelm", "ref": "v6.11.0-rc.1"})
        before = path.read_text()
        self.claude_dir()
        diag = self.state()
        self.assertEqual(diag["state"], "market-conflict")
        self.assertEqual(diag["base_state"], "none")
        self.assertEqual(diag["market_conflict"]["reason"], "pinned")
        self.assertEqual(diag["market_conflict"]["ref"], "v6.11.0-rc.1")
        self.assertEqual(diag["next_command"], f'{agenthelm.CLI} install --client claude --marketplace-source "example/agenthelm#v6.11.0-rc.1"')
        self.assertEqual(path.read_text(), before, "settings.json is never edited")

    def test_a_declaration_pointing_elsewhere(self) -> None:
        self.settings({"source": "github", "repo": "someone/else"})
        self.claude_dir()
        diag = self.state()
        self.assertEqual((diag["state"], diag["market_conflict"]["reason"]), ("market-conflict", "points-elsewhere"))

    def test_the_same_repository_without_a_ref_is_no_conflict(self) -> None:
        self.settings({"source": "github", "repo": "example/agenthelm"})
        self.claude_dir()
        self.assertEqual(self.state()["state"], "none")

    def test_project_settings_are_read_too(self) -> None:
        self.settings({"source": "github", "repo": "someone/else"}, "project")
        self.claude_dir()
        code, out = self.run_cli("check", "--latest", str(self.latest), "--output", "json", "--project", str(self.root / "project"),
                                 "--client", "claude")
        diag = json.loads(out)["install_state"]["clients"]["claude"]
        self.assertEqual((diag["state"], diag["market_conflict"]["scope"]), ("market-conflict", "project"))

    def test_dev_state_wins_but_still_carries_the_conflict(self) -> None:
        self.settings({"source": "github", "repo": "example/agenthelm", "ref": "v1"})
        self.claude("agent-pack", "9.0.0", market="agenthelm-dev")
        diag = self.state()
        self.assertEqual(diag["state"], "dev")
        self.assertEqual(diag["market_conflict"]["reason"], "pinned")


class OutputAndPreviews(Base):
    def test_json_fields_are_stable(self) -> None:
        self.claude("agent-pack")
        diag = self.state()
        self.assertEqual(set(diag), {"client", "state", "base_state", "sub_status", "installed", "missing", "installs", "conflicts",
                                     "market_conflict", "untouched", "summary", "next_command", "modifies_existing",
                                     "modifies_note", "recovery", "market_pin"})
        self.assertEqual(set(diag["installs"][0]), {"plugin", "version", "path", "kind", "origin", "scope", "status"})
        code, out = self.run_cli("check", "--latest", str(self.latest), "--output", "json", "--no-project", "--client", "claude")
        self.assertIn(json.loads(out)["summary"]["install_states"]["claude"], agenthelm.INSTALL_STATES)

    def test_text_gives_the_conclusion_then_the_preview_command_and_its_reach(self) -> None:
        for plugin in NAMES:
            self.claude(plugin, "1.0.0")
        code, out = self.run_cli("check", "--latest", str(self.latest), "--no-project", "--client", "claude")
        self.assertIn("安装状态：", out)
        self.assertIn("[Claude Code·市场安装·落后]", out)
        self.assertIn(f"下一步（预览）：{agenthelm.CLI} update --client claude", out)
        self.assertIn("会改动已有的安装", out)
        self.assertIn("原件恢复位置：", out)

    def test_ignored_and_unselected_clients_get_no_diagnosis(self) -> None:
        self.claude_dir()
        (self.home / ".codex").mkdir()
        write(self.home / ".config/agenthelm/config.json", {"ignored_clients": ["codex"]})
        self.assertEqual(list(self.check()), ["claude"])

    def test_update_and_migrate_previews_mark_dev_installs_as_untouched(self) -> None:
        for plugin in NAMES:
            self.claude(plugin, "1.0.0")
        dev = self.claude("agent-pack", "9.0.0", market="agenthelm-dev")
        linked = self.working_copy("agents-init")
        self.records["agents-init@agenthelm"] = []
        self.claude("agents-init", "9.9.9", link_to=linked)
        for command in ("update", "migrate"):
            extra = ["--no-project"] if command == "update" else []
            code, out = self.run_cli(command, "--latest", str(self.latest), "--output", "json", *extra, "--client", "claude")
            steps = [step for step in json.loads(out)["steps"] if step.get("untouched")]
            self.assertEqual(len(steps), 2, command)
            self.assertTrue(all(step["skipped"].startswith("不会改动") for step in steps))
            self.assertTrue(any(str(dev) in step["description"] for step in steps))
        # update must not try to update the symlinked or foreign installs
        code, out = self.run_cli("update", "--latest", str(self.latest), "--output", "json", "--no-project", "--client", "claude")
        commands = [step.get("command", "") for step in json.loads(out)["steps"]]
        self.assertFalse(any("agents-init@agenthelm" in c or "agent-pack@agenthelm-dev" in c for c in commands))
        self.assertTrue(dev.exists() and linked.exists())

    def test_install_does_not_duplicate_a_plugin_that_has_a_dev_install(self) -> None:
        self.claude("agent-pack", "9.0.0", market="agenthelm-dev")
        code, out = self.run_cli("install", "--latest", str(self.latest), "--output", "json", "--client", "claude")
        steps = {step["description"]: step for step in json.loads(out)["steps"]}
        pack = steps["Claude Code 安装 agent-pack"]
        self.assertEqual(pack["status"], "skipped")
        self.assertTrue(pack["skipped"].startswith("不会改动"))
        self.assertNotIn("untouched", steps["Claude Code 安装 agents-init"])


if __name__ == "__main__":
    unittest.main()
