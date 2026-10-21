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
        self.assertEqual(report["summary"]["opinion_updates"], 1)
        self.assertIn("a/b@1.1.0", agenthelm.render_text(report))

    def test_cli_json_output_and_strict_exit_code(self) -> None:
        self.install()
        with mock.patch("builtins.print") as printed:
            code = agenthelm.main(["check", "--home", str(self.home), "--no-project",
                                   "--latest", str(self.latest_file), "--output", "json", "--strict"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(printed.call_args.args[0])["summary"]["plugins_behind"], 1)


if __name__ == "__main__":
    unittest.main()
