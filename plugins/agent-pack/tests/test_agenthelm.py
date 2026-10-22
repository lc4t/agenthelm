from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
import agenthelm  # noqa: E402

LATEST = {
    "schema": 1,
    "marketplace": "agenthelm",
    "released_at": "2026-10-21",
    "plugins": {
        "agents-init": {"version": "7.0.0", "content": {
            "agents-init/project-contract": {"version": "1.1.0", "kind": "template"}}},
        "agent-pack": {"version": "1.6.0"},
        "opinion-manager": {"version": "0.4.0"},
    },
    "migrations": [
        {"plugin": "agent-pack", "from": "1.5.1", "to": "1.5.2", "change_kind": "code",
         "breaking": False, "date": "2026-10-21", "summary": "fix"},
        {"plugin": "agent-pack", "from": "1.5.2", "to": "1.6.0", "change_kind": "content",
         "breaking": True, "date": "2026-10-21", "summary": "templates"},
        {"plugin": "agent-pack", "from": "1.6.0", "to": "1.7.0", "change_kind": "code",
         "breaking": False, "date": "2026-10-22", "summary": "future"},
    ],
}


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


class AgenthelmCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.env = agenthelm.Env(home=self.home, variables={})
        self.latest_file = self.root / "latest.json"
        write(self.latest_file, LATEST)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install(self) -> None:
        claude_path = self.home / ".claude/plugins/cache/agenthelm/agent-pack/1.5.1"
        write(claude_path / "plugin.json", {"name": "agent-pack", "version": "1.5.1"})
        write(self.home / ".claude/plugins/installed_plugins.json", {"version": 2, "plugins": {
            "agent-pack@agenthelm": [{"scope": "user", "installPath": str(claude_path), "version": "1.5.1"}],
            "other@elsewhere": [{"scope": "user", "installPath": "/nowhere", "version": "0.1.0"}],
        }})
        write(self.home / ".codex/plugins/cache/agenthelm/agents-init/7.0.0/plugin.json",
              {"name": "agents-init", "version": "7.0.0"})
        write(self.home / ".agents/plugins/plugins/opinion-manager/plugin.json",
              {"name": "opinion-manager", "version": "0.4.0"})

    def project_with_lock(self) -> Path:
        project = self.root / "project"
        write(project / "AGENTS.md", "rendered\n")
        write(project / "AGENT.md", "user edited\n")
        digest = hashlib.sha256(b"rendered\n").hexdigest()
        write(project / agenthelm.CONTENT_LOCK, {"schema_version": 1, "packs": {
            "agents-init/project-contract": {"plugin": "agents-init", "version": "1.0.0",
                                             "files": {"AGENTS.md": digest, "AGENT.md": digest}}}})
        return project

    def check(self, project: Path | None = None) -> dict:
        return agenthelm.run_check(self.env, project, latest_source=str(self.latest_file))

    def test_reports_each_client_install_against_the_cloud_version(self) -> None:
        self.install()
        report = self.check()
        by_client = {(item["client"], item["plugin"]): item for item in report["plugins"]}
        behind = by_client[("claude", "agent-pack")]
        self.assertEqual((behind["status"], behind["latest"]), ("behind", "1.6.0"))
        # Only the steps between the installed and the latest version, never future ones.
        self.assertEqual([step["to"] for step in behind["steps"]], ["1.5.2", "1.6.0"])
        self.assertEqual(behind["change_kinds"], ["code", "content"])
        self.assertTrue(behind["breaking"])
        self.assertEqual(by_client[("codex", "agents-init")]["status"], "current")
        self.assertEqual(by_client[("legacy", "opinion-manager")]["status"], "current")
        self.assertNotIn("other", {item["plugin"] for item in report["plugins"]})
        self.assertEqual(report["summary"]["legacy_installs"], 1)
        self.assertFalse(report["summary"]["up_to_date"])

    def test_content_pack_update_distinguishes_clean_and_locally_changed_files(self) -> None:
        project = self.project_with_lock()
        (finding,) = self.check(project)["content"]
        self.assertEqual(finding["status"], "conflict")
        self.assertEqual(finding["locally_changed"], ["AGENT.md"])
        (project / "AGENT.md").write_text("rendered\n", encoding="utf-8")
        (finding,) = self.check(project)["content"]
        self.assertEqual((finding["status"], finding["latest"]), ("update-available", "1.1.0"))

    def test_nothing_installed_points_to_install_instead_of_up_to_date(self) -> None:
        text = agenthelm.render_text(self.check())
        self.assertNotIn("全部为最新", text)
        self.assertIn("install", text)

    def test_content_only_release_is_reported_without_plugin_changes(self) -> None:
        project = self.project_with_lock()
        report = self.check(project)
        self.assertEqual(report["summary"]["plugins_behind"], 0)
        self.assertEqual(report["summary"]["content_updates"], 1)
        self.assertIn("模板/样例包可更新", agenthelm.one_line(report))

    def test_up_to_date_project_and_plugins_produce_no_notice(self) -> None:
        latest = json.loads(json.dumps(LATEST))
        latest["plugins"]["agents-init"]["content"]["agents-init/project-contract"]["version"] = "1.0.0"
        write(self.latest_file, latest)
        project = self.project_with_lock()
        (project / "AGENT.md").write_text("rendered\n", encoding="utf-8")
        report = self.check(project)
        self.assertTrue(report["summary"]["up_to_date"])
        self.assertIsNone(agenthelm.one_line(report))

    def test_hook_is_silent_when_disabled_and_notifies_once_per_day(self) -> None:
        self.install()
        self.env.variables[agenthelm.ENV_LATEST] = str(self.latest_file)
        with mock.patch("builtins.print") as printed:
            self.env.variables[agenthelm.ENV_DISABLE] = "1"
            agenthelm.hook(self.env, None, "text")
            printed.assert_not_called()
            del self.env.variables[agenthelm.ENV_DISABLE]
            write(self.env.config_file, {"update_check": False})
            agenthelm.hook(self.env, None, "text")
            printed.assert_not_called()
            self.env.config_file.unlink()
            agenthelm.hook(self.env, None, "claude-hook")
            agenthelm.hook(self.env, None, "claude-hook")
            self.assertEqual(printed.call_count, 1)
        payload = json.loads(printed.call_args.args[0])
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("插件可更新", payload["systemMessage"])

    def test_hook_never_fails_a_session_when_offline(self) -> None:
        self.env.variables[agenthelm.ENV_LATEST] = "https://agenthelm.invalid/latest.json"
        with mock.patch.object(agenthelm, "_fetch", side_effect=OSError("offline")), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(agenthelm.hook(self.env, None, "text"), 0)
        printed.assert_not_called()

    def test_remote_source_is_fetched_at_most_once_per_day_and_falls_back_to_cache(self) -> None:
        url = "https://example.invalid/latest.json"
        with mock.patch.object(agenthelm, "_fetch", return_value=LATEST) as fetch:
            agenthelm.load_latest(self.env, url)
            _, origin = agenthelm.load_latest(self.env, url)
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(origin["cached"])
        cache = self.env.cache_dir / "latest.json"
        stale = json.loads(cache.read_text())
        stale["fetched_at"] = time.time() - 2 * agenthelm.CACHE_TTL_SECONDS
        cache.write_text(json.dumps(stale))
        with mock.patch.object(agenthelm, "_fetch", side_effect=OSError("offline")):
            latest, origin = agenthelm.load_latest(self.env, url)
        self.assertEqual(latest["released_at"], "2026-10-21")
        self.assertTrue(origin["stale"])

    def test_opinion_updates_are_delegated_to_opinion_manager(self) -> None:
        self.install()
        project = self.root / "project"
        write(project / agenthelm.OPINION_LOCK, {"profile": {"id": "p"}})
        manager = self.home / ".agents/plugins/plugins/opinion-manager"
        reply = json.dumps({"ok": True, "updates": [{"kind": "templates", "current": "a/b@1.0.0",
                                                      "available": ["a/b@1.1.0"]}]})
        completed = mock.Mock(stdout=reply, stderr="", returncode=0)
        with mock.patch.object(agenthelm, "locate_companion", return_value=manager), \
                mock.patch.object(agenthelm.subprocess, "run", return_value=completed) as run:
            report = agenthelm.run_check(self.env, project, latest_source=str(self.latest_file))
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], [str(manager / "runtime/opinion_manager.py"), "updates"])
        profile_arg = Path(command[command.index("--profile") + 1])
        self.assertEqual(profile_arg, profile_arg.resolve())  # no /var -> /private/var symlink on macOS
        self.assertEqual(report["summary"]["opinion_updates"], 1)
        self.assertIn("a/b@1.1.0", agenthelm.render_text(report))

    def test_cli_json_output_and_strict_exit_code(self) -> None:
        self.install()
        with mock.patch("builtins.print") as printed:
            code = agenthelm.main(["check", "--home", str(self.home), "--no-project",
                                   "--latest", str(self.latest_file), "--output", "json", "--strict"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(printed.call_args.args[0])["summary"]["plugins_behind"], 1)



class AgenthelmOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.env = agenthelm.Env(home=self.home, variables={})
        latest = json.loads(json.dumps(LATEST))
        latest["repository"] = "https://github.com/lc4t/agenthelm"
        self.latest = latest
        self.latest_file = self.root / "latest.json"
        write(self.latest_file, latest)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *args: str) -> tuple[int, dict]:
        with mock.patch("builtins.print") as printed:
            code = agenthelm.main([*args, "--home", str(self.home), "--latest", str(self.latest_file),
                                   "--output", "json"])
        return code, json.loads(printed.call_args.args[0])

    def legacy_home(self) -> None:
        legacy = self.home / ".agents/plugins/plugins/agent-pack"
        write(legacy / "plugin.json", {"name": "agent-pack", "version": "1.5.1"})
        write(legacy / "skills/agent-pack/SKILL.md", "x")
        write(self.home / ".agents/plugins/marketplace.json", {"name": "personal", "plugins": [
            {"name": "agent-pack", "source": {"source": "local", "path": "./.agents/plugins/plugins/agent-pack"}},
            {"name": "someone-else", "source": {"source": "local", "path": "./x"}}]})
        write(self.home / ".codex/config.toml",
              'model = "x"\n\n[plugins."agent-pack@personal"]\nenabled = true\n\n'
              '[plugins."someone-else@personal"]\nenabled = true\n')
        (self.home / ".claude/skills").mkdir(parents=True)
        (self.home / ".claude/skills/agent-pack").symlink_to(legacy / "skills/agent-pack")
        (self.home / ".claude/skills/unrelated").symlink_to(self.root)
        record = self.home / ".claude/agent-pack/plugins"
        record.mkdir(parents=True)
        (record / "agent-pack").symlink_to(legacy)
        write(self.home / ".claude.json", {"mcpServers": {
            "agent-pack": {"command": "python3", "args": [str(record / "agent-pack/runtime/mcp_server.py")]},
            "mine": {"command": "node", "args": ["server.js"]}}})

    def test_migrate_previews_then_retires_only_agenthelm_legacy_installs(self) -> None:
        self.legacy_home()
        code, preview = self.run_cli("migrate")
        self.assertEqual(code, 0)
        kinds = sorted(item["kind"] for item in preview["legacy"])
        self.assertEqual(kinds, ["claude-skill-link", "claude-user-mcp", "codex-config-section",
                                 "codex-personal-entry", "plugin-copy", "plugin-record"])
        self.assertTrue((self.home / ".agents/plugins/plugins/agent-pack").exists())  # preview only
        code, applied = self.run_cli("migrate", "--apply")
        self.assertEqual(code, 0, applied)
        recovery = Path(applied["recovery"])
        self.assertFalse((self.home / ".agents/plugins/plugins/agent-pack").exists())
        self.assertFalse((self.home / ".claude/skills/agent-pack").is_symlink())
        self.assertTrue((self.home / ".claude/skills/unrelated").is_symlink())
        self.assertTrue((recovery / "moved/.agents/plugins/plugins/agent-pack/plugin.json").is_file())
        marketplace = json.loads((self.home / ".agents/plugins/marketplace.json").read_text())
        self.assertEqual([entry["name"] for entry in marketplace["plugins"]], ["someone-else"])
        config = (self.home / ".codex/config.toml").read_text()
        self.assertNotIn("agent-pack@personal", config)
        self.assertIn('[plugins."someone-else@personal"]', config)
        self.assertIn('model = "x"', config)
        servers = json.loads((self.home / ".claude.json").read_text())["mcpServers"]
        self.assertEqual(list(servers), ["mine"])
        self.assertIn("agent-pack", json.loads((recovery / "files/.claude.json").read_text())["mcpServers"])
        self.assertEqual(len(json.loads((recovery / "migration.json").read_text())["artifacts"]), 6)

    def test_skill_links_into_a_checkout_of_an_agenthelm_plugin_are_legacy(self) -> None:
        checkout = self.root / "checkout/plugins/agent-pack"
        write(checkout / "plugin.json", {"name": "agent-pack", "version": "1.5.1"})
        write(checkout / "skills/agent-pack/SKILL.md", "x")
        foreign = self.root / "elsewhere/skills/agent-pack"
        write(foreign / "SKILL.md", "y")
        (self.home / ".claude/skills").mkdir(parents=True)
        (self.home / ".claude/skills/agent-pack").symlink_to(checkout / "skills/agent-pack")
        artifacts = agenthelm.legacy_artifacts(self.env, {"agent-pack"})
        self.assertEqual([item["kind"] for item in artifacts], ["claude-skill-link"])
        (self.home / ".claude/skills/agent-pack").unlink()
        (self.home / ".claude/skills/agent-pack").symlink_to(foreign)
        self.assertEqual(agenthelm.legacy_artifacts(self.env, {"agent-pack"}), [])

    def test_copied_claude_skill_is_reported_but_left_for_the_user(self) -> None:
        write(self.home / ".claude/skills/agent-pack/SKILL.md", "copied by hand")
        code, applied = self.run_cli("migrate", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(applied["retired"][0]["status"], "manual")
        self.assertTrue((self.home / ".claude/skills/agent-pack/SKILL.md").is_file())

    def test_install_plans_native_marketplace_commands_per_client(self) -> None:
        steps = agenthelm.install_steps(self.env, self.latest, ["claude", "codex"], ["agent-pack"], None, self.home)
        commands = [step.command for step in steps]
        self.assertEqual(commands, [
            ["claude", "plugin", "marketplace", "add", "lc4t/agenthelm"],
            ["claude", "plugin", "install", "agent-pack@agenthelm"],
            ["codex", "plugin", "marketplace", "add", "lc4t/agenthelm"],
            ["codex", "plugin", "add", "agent-pack@agenthelm"],
        ])
        mirrored = agenthelm.install_steps(self.env, self.latest, ["claude"], ["agent-pack"], None, self.home,
                                           "https://mirror.example/skill.git")
        self.assertEqual(mirrored[0].command[-1], "https://mirror.example/skill.git")

    def test_update_plans_per_client_and_waits_for_the_new_template_plugin(self) -> None:
        report = {"_latest": self.latest, "summary": {}, "content": [
            {"pack": "agents-init/project-contract", "plugin": "agents-init", "version": "1.0.0",
             "latest": "1.1.0", "status": "update-available", "locally_changed": []}],
            "plugins": [
                {"client": "claude", "plugin": "agent-pack", "status": "behind", "latest": "1.6.0", "scope": "project"},
                {"client": "codex", "plugin": "agent-pack", "status": "behind", "latest": "1.6.0"},
                {"client": "codex", "plugin": "agents-init", "status": "behind", "latest": "7.0.0"},
                {"client": "claude", "plugin": "opinion-manager", "status": "current", "latest": "0.4.0"},
            ]}
        steps = agenthelm.update_steps(self.env, report, self.root, None, self.home, [], [], None, None)
        commands = [step.command for step in steps if step.command]
        self.assertEqual(commands, [
            ["claude", "plugin", "marketplace", "update", "agenthelm"],
            ["claude", "plugin", "update", "agent-pack@agenthelm", "--scope", "project"],
            ["codex", "plugin", "marketplace", "upgrade", "agenthelm"],
        ])
        content = [step for step in steps if step.kind == "content"]
        self.assertEqual(len(content), 1)
        self.assertIn("先更新 agents-init", content[0].blocked)
        only_content = agenthelm.update_steps(self.env, report, self.root, None, self.home, [], [], None, "content")
        self.assertEqual([step.kind for step in only_content], ["content"])

    def test_failed_step_stops_the_remaining_ones(self) -> None:
        steps = [agenthelm.Step("update", "claude", "one", [sys.executable, "-c", "raise SystemExit(3)"]),
                 agenthelm.Step("update", "claude", "two", [sys.executable, "-c", "pass"])]
        results = agenthelm.execute_steps(steps, apply=True, recovery_root=None)
        self.assertEqual([record["status"] for record in results], ["failed"])
        preview = agenthelm.execute_steps(steps, apply=False, recovery_root=None)
        self.assertEqual([record["status"] for record in preview], ["preview", "preview"])

    def test_doctor_flags_duplicate_and_skewed_installs(self) -> None:
        report = {"_latest": self.latest, "summary": {}, "plugins": [
            {"client": "claude", "plugin": "agent-pack", "version": "1.6.0"},
            {"client": "codex", "plugin": "agent-pack", "version": "1.5.1"},
            {"client": "legacy", "plugin": "agent-pack", "version": "1.5.1"}]}
        result = agenthelm.doctor(self.env, report)
        issues = {issue["issue"] for issue in result["issues"]}
        self.assertTrue({"duplicate", "version-skew", "incomplete"} <= issues)
        self.assertFalse(result["ok"])

    def test_release_archive_with_escaping_paths_is_rejected(self) -> None:
        import io
        import tarfile
        archive = self.root / "bad.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            info = tarfile.TarInfo("../escape.txt")
            info.size = 1
            bundle.addfile(info, io.BytesIO(b"x"))
        target = self.root / "out"
        target.mkdir()
        with self.assertRaises(agenthelm.CheckError):
            agenthelm._safe_extract(archive, target)
        self.assertFalse((self.root / "escape.txt").exists())

    def test_client_commands_run_against_the_inspected_home(self) -> None:
        with mock.patch.dict(agenthelm.os.environ, {"CLAUDE_CONFIG_DIR": "/real/.claude", "CODEX_HOME": "/real/.codex"}):
            env = agenthelm.current_env(self.home)
        child = env.child_environment()
        self.assertEqual(child["HOME"], str(self.home))
        self.assertNotIn("CLAUDE_CONFIG_DIR", child)
        self.assertNotIn("CODEX_HOME", child)
        step = agenthelm.Step("install", "claude", "home", [sys.executable, "-c",
                              "import os, sys; sys.exit(0 if os.environ['HOME'] == sys.argv[1] else 5)", str(self.home)])
        (record,) = agenthelm.execute_steps([step], apply=True, recovery_root=None, child_env=child)
        self.assertEqual(record["status"], "applied")

    def test_newest_companion_wins_over_the_copy_next_to_this_script(self) -> None:
        newer = self.home / ".claude/plugins/cache/agenthelm/agents-init/99.0.0"
        write(newer / "plugin.json", {"name": "agents-init", "version": "99.0.0"})
        installed = [{"client": "claude", "plugin": "agents-init", "version": "99.0.0", "path": str(newer)}]
        self.assertEqual(agenthelm.locate_companion("agents-init", installed), newer)

    def test_locate_returns_the_newest_or_the_requested_version(self) -> None:
        legacy = self.home / ".agents/plugins/plugins/opinion-manager"
        write(legacy / "plugin.json", {"name": "opinion-manager", "version": "0.4.0"})
        codex = self.home / ".codex/plugins/cache/agenthelm/opinion-manager/0.4.1"
        write(codex / "plugin.json", {"name": "opinion-manager", "version": "0.4.1"})
        self.assertEqual(agenthelm.locate(self.env, "opinion-manager")["path"], str(codex))
        # Legacy copies stay out by default so wrappers do not break once migrate retires them.
        self.assertFalse(agenthelm.locate(self.env, "opinion-manager", "0.4.0")["ok"])
        self.assertEqual(agenthelm.locate(self.env, "opinion-manager", "0.4.0", include_legacy=True)["path"],
                         str(legacy))
        self.assertFalse(agenthelm.locate(self.env, "opinion-manager", "9.9.9")["ok"])

    def test_release_download_requires_a_pinned_tag_and_digest(self) -> None:
        with mock.patch.object(agenthelm, "_fetch_json", side_effect=OSError("offline")), \
                self.assertRaises(agenthelm.CheckError):
            agenthelm.release_source(self.latest, self.root)

    def test_release_falls_back_to_github_when_the_tag_ships_the_listed_versions(self) -> None:
        release = {"tag": "v9", "tarball_sha256": "ab" * 32, "tarball_url": "https://example.invalid/a.tgz"}

        def fetch(url: str, timeout: float = 0):
            if url.endswith("/releases/latest/download/release.json"):
                return release
            if url == "https://raw.githubusercontent.com/lc4t/agenthelm/v9/latest.json":
                return tagged
            raise AssertionError(url)

        tagged = json.loads(json.dumps(self.latest))
        with mock.patch.object(agenthelm, "_fetch_json", side_effect=fetch):
            self.assertEqual(agenthelm.github_release(self.latest), release)
            tagged["plugins"]["agent-pack"]["version"] = "0.0.1"
            self.assertEqual(agenthelm.github_release(self.latest), {})


if __name__ == "__main__":
    unittest.main()
