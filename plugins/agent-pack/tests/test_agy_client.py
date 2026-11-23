"""Antigravity (``agy``) as a declared client (#134 batch E): what the commands do with it, against a stub ``agy``.

The stub (``fixtures/clients/agy/bin/agy``) records the command lines it receives and keeps its own state under
``$HOME/.stub-agy``; no test runs a real client. What a real ``agy`` does is for the release-candidate acceptance.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "runtime"))

import agent_pack_clients as registry  # noqa: E402
import agent_pack_config as core  # noqa: E402
import agenthelm  # noqa: E402
import test_agenthelm_reporting as base  # noqa: E402
from test_agent_pack_config import PROJECT_PROFILE_NAMESPACE, make_project  # noqa: E402
from test_agenthelm_reporting import write  # noqa: E402

FIXTURE = TESTS / "fixtures" / "clients" / "agy"
SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
LATEST = {**base.LATEST, "plugins": {"agent-pack": {"version": "2.0.0"}}, "release": {"tag": "v2.0.0", "tarball_sha256": "x"}}


def install_agy_stub(directory: Path) -> Path:
    """Copy the stub ``agy`` into ``directory`` with a shebang that names this interpreter (the tests' PATH holds no python)."""
    target = directory / "agy"
    lines = (FIXTURE / "bin" / "agy").read_text(encoding="utf-8").splitlines(keepends=True)
    target.write_text(f"#!{sys.executable}\n" + "".join(lines[1:]), encoding="utf-8")
    target.chmod(0o755)
    return target


class Fixture(base.Base):
    def setUp(self) -> None:
        super().setUp()
        write(self.latest_file, LATEST)
        install_agy_stub(self.bin)
        env = mock.patch.dict(os.environ, {"AGY_STUB_LOG": str(self.calls)})
        env.start()
        self.addCleanup(env.stop)
        self.source = self.root / "source"
        write(self.source / "plugins" / "agent-pack" / "plugin.json", {"$schema": SCHEMA, "name": "agent-pack", "version": "2.0.0"})
        write(self.source / "plugins" / "agent-pack" / "skills" / "s" / "SKILL.md", "new\n")

    def agy_state(self) -> dict:
        path = self.home / ".stub-agy" / "state.json"
        return json.loads(path.read_text()) if path.is_file() else {}

    def install_old(self, version: str = "1.0.0", enabled: bool = True) -> Path:
        """Put an installed ``agent-pack`` into the stub's store (what ``agy plugin install`` would have left)."""
        path = self.home / ".gemini/config/plugins/agent-pack"
        write(path / "plugin.json", {"name": "agent-pack", "version": version})
        write(self.home / ".stub-agy" / "state.json", {"agent-pack": {"enabled": enabled}})
        return path

    def commands(self) -> list[str]:
        return [line for line in self.called() if not line.startswith("plugin list")]


class DeclarationTests(unittest.TestCase):
    def test_the_driver_is_declared_data_outside_the_default_set(self) -> None:
        driver = registry.get("agy")
        self.assertEqual((driver["display"], driver["lifecycle"]), ("Antigravity", "active"))
        self.assertNotIn("agy", registry.default_ids())
        self.assertIn("agy", registry.selectable_ids())
        self.assertEqual(driver["detect"]["executables"], ["agy"])
        self.assertEqual(driver["detect"]["cli"], {"argv": ["plugin", "list", "--json"], "stream": "stdout", "parse": "json"})
        self.assertEqual(registry.expand_step(driver, "install", cli="agy", source_dir="/x"), ["agy", "plugin", "install", "/x"])
        self.assertEqual(registry.expand_step(driver, "uninstall", cli="agy", plugin="p"), ["agy", "plugin", "uninstall", "p"])
        self.assertIsNone(registry.expand_step(driver, "update", cli="agy"))   # there is no update command
        self.assertTrue(registry.supports("agy", "update_by_reinstall"))
        self.assertFalse(registry.supports("agy", "reinstall_rewrites_all"))

    def test_store_statuses_follow_what_was_observed(self) -> None:
        driver = registry.get("agy")
        statuses = {(kind, store["scope"], store["path"]): store["status"]
                    for kind in ("skills", "mcp") for store in driver["stores"][kind]}
        self.assertEqual(statuses, {("skills", "user", "~/.gemini/skills"): "undetermined",
                                    ("skills", "user", "~/.gemini/antigravity-cli/skills"): "undetermined",
                                    ("skills", "project", "<project>/.agents/skills"): "documented",
                                    ("mcp", "user", "~/.gemini/config/mcp_config.json"): "observed"})
        # an undetermined store is shown nowhere that reads or writes
        self.assertEqual(registry.store_table("skills")["agy"], (("project", "<project>/.agents/skills"),))
        self.assertNotIn(("agy", "user"), registry.primary_skill_stores())
        self.assertNotIn(("agy", "user"), registry.skill_write_targets())

    def test_hook_and_mcp_declarations(self) -> None:
        self.assertEqual(registry.hook_event_name("agy", "session-start"), "SessionStart")
        self.assertEqual(registry.hook_event_name("agy", "stop"), "Stop")
        self.assertIsNone(registry.hook_event_name("agy", "prompt-submit"))
        self.assertEqual(registry.hook_output("agy", "stop"), "injectSteps")
        options = registry.mcp_options("agy")
        self.assertEqual((options["remote_url_key"], options["env_interpolation"], options["type_field_required"]),
                         ("serverUrl", "declared-variables", False))
        self.assertEqual(registry.mcp_spec("agy")["write"], "package")
        self.assertEqual(registry.sync_spec("agy")["write"], "plugin-install")

    def test_unsupported_features_carry_a_reason(self) -> None:
        for feature in ("hook-event:prompt-submit", "relocate", "dedupe", "sync-skills-project", "sync-mcp-project",
                        "agents-init-agent-cli", "nudge-install-hooks"):
            self.assertTrue(registry.unsupported("agy", feature), feature)
        self.assertIsNone(registry.unsupported("claude", "relocate"))
        self.assertIsNone(registry.unsupported("nobody", "relocate"))

    def test_the_clients_report_lists_it_and_runs_nothing(self) -> None:
        with mock.patch.dict(os.environ, {"AGY_STUB_LOG": "/nonexistent/should-not-be-written"}):
            data = registry.report(Path("/nonexistent-home"), lambda name: None, lambda name: str(FIXTURE / "bin" / "agy"))
        row = next(item for item in data["clients"] if item["id"] == "agy")
        self.assertEqual((row["detected"], row["distribution"], row["install_supported"]), (True, "marketplace-cli", True))
        self.assertFalse(row["capabilities"]["default_set"])
        self.assertFalse(Path("/nonexistent/should-not-be-written").exists())


class InstallAndUpdateTests(Fixture):
    def test_install_preview_names_the_command_and_writes_nothing(self) -> None:
        code, payload = self.run_json("install", "--client", "agy", "--source", str(self.source))
        self.assertEqual(code, 0)
        step = payload["steps"][0]
        self.assertEqual((step["client"], step["status"]), ("agy", "preview"))
        self.assertEqual(step["command"], f"{self.bin / 'agy'} plugin install {self.source / 'plugins/agent-pack'}")
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.agy_state(), {})

    def test_install_apply_runs_the_declared_command_and_reads_the_version_back(self) -> None:
        code, payload = self.run_json("install", "--client", "agy", "--source", str(self.source), "--apply")
        self.assertEqual(code, 0, payload)
        step = payload["steps"][0]
        self.assertEqual(step["status"], "applied")
        self.assertEqual([(c["plugin"], c["before"], c["after"]) for c in step["changes"]], [("agent-pack", None, "2.0.0")])
        self.assertEqual(self.commands(), [f"plugin install {self.source / 'plugins/agent-pack'}"])
        self.assertTrue((self.home / ".gemini/config/plugins/agent-pack/plugin.json").is_file())

    def test_an_installed_plugin_is_left_alone_by_install(self) -> None:
        self.install_old()
        code, payload = self.run_json("install", "--client", "agy", "--source", str(self.source), "--apply")
        step = payload["steps"][0]
        self.assertEqual(step["status"], "skipped")
        self.assertIn("update", step["skipped"])
        self.assertEqual(self.commands(), [])

    def test_update_is_an_uninstall_then_an_install_and_says_so_in_the_preview(self) -> None:
        self.install_old()
        code, payload = self.run_json("update", "--no-project", "--client", "agy", "--source", str(self.source))
        steps = [s for s in payload["steps"] if s["client"] == "agy"]
        self.assertEqual([s["command"].split(" ", 1)[1] for s in steps],
                         ["plugin uninstall agent-pack", f"plugin install {self.source / 'plugins/agent-pack'}"])
        self.assertIn("启用状态", steps[0]["note"])   # the preview says the enabled state is lost and restored
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.agy_state(), {"agent-pack": {"enabled": True}})

    def test_update_apply_reinstalls_and_keeps_an_enabled_plugin_enabled(self) -> None:
        self.install_old()
        code, payload = self.run_json("update", "--no-project", "--client", "agy", "--source", str(self.source), "--apply")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.commands(), ["plugin uninstall agent-pack", f"plugin install {self.source / 'plugins/agent-pack'}"])
        reinstalled = [s for s in payload["steps"] if s["client"] == "agy"][-1]
        self.assertEqual((reinstalled["status"], reinstalled["before"], reinstalled["after"]), ("applied", None, "2.0.0"))
        self.assertEqual(self.agy_state(), {"agent-pack": {"enabled": True}})

    def test_a_disabled_plugin_is_disabled_again_after_the_reinstall(self) -> None:
        self.install_old(enabled=False)
        _code, payload = self.run_json("update", "--no-project", "--client", "agy", "--source", str(self.source), "--apply")
        self.assertEqual(self.commands(), ["plugin uninstall agent-pack", f"plugin install {self.source / 'plugins/agent-pack'}",
                                           "plugin disable agent-pack"])
        self.assertEqual(self.agy_state(), {"agent-pack": {"enabled": False}})
        self.assertTrue(all(s["status"] == "applied" for s in payload["steps"] if s["client"] == "agy"))

    def test_a_failed_uninstall_stops_the_rest_of_the_update(self) -> None:
        self.install_old()
        with mock.patch.dict(os.environ, {"AGY_STUB_UNINSTALL_FAIL": "1"}):
            _code, payload = self.run_json("update", "--no-project", "--client", "agy", "--source", str(self.source), "--apply")
        statuses = [s["status"] for s in payload["steps"] if s["client"] == "agy"]
        self.assertEqual(statuses[0], "failed")
        self.assertEqual(set(statuses[1:]), {"not_run"})
        self.assertFalse(payload["ok"])

    def test_an_unreachable_release_blocks_with_the_reason_and_runs_nothing(self) -> None:
        write(self.latest_file, {**LATEST, "release": {"tag": "v2.0.0"}})
        _code, payload = self.run_json("install", "--client", "agy", "--apply")
        step = payload["steps"][0]
        self.assertEqual(step["status"], "blocked")
        self.assertIn("--source", step["blocked"])
        self.assertEqual(self.commands(), [])

    def test_check_reads_the_install_state_through_the_plugin_list(self) -> None:
        self.install_old()
        _code, report = self.run_json("check", "--no-project", "--client", "agy")
        self.assertEqual(report["clients"]["selected"], ["agy"])
        item = report["plugins"][0]
        self.assertEqual((item["client"], item["plugin"], item["version"], item["status"]), ("agy", "agent-pack", "1.0.0", "behind"))
        self.assertEqual(report["install_state"]["clients"]["agy"]["state"], "marketplace")
        self.assertEqual(report["summary"]["by_client"], {"agy": {"behind": 1, "unavailable": 0, "total": 1}})
        self.assertIn("update --client agy", report["install_state"]["clients"]["agy"]["next_command"])

    def test_without_naming_it_the_client_program_is_never_started(self) -> None:
        self.install_old()
        for args in (("check", "--no-project"), ("update", "--no-project", "--source", str(self.source)),
                     ("install", "--source", str(self.source)), ("doctor",)):
            self.run_cli(*args)
        import io
        from contextlib import redirect_stderr, redirect_stdout
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            agenthelm.main(["inventory", "--home", str(self.home)])
            agenthelm.main(["clients", "--home", str(self.home)])
        self.assertEqual(self.called(), [])

    def test_the_text_output_names_the_client_and_the_command_to_run(self) -> None:
        self.install_old()
        _code, text = self.run_text("check", "--no-project", "--client", "agy")
        self.assertIn("Antigravity", text)
        self.assertIn("update --client agy", text)


class CliEvidenceTests(Fixture):
    def inventory(self, *args: str) -> dict:
        os.environ["AGENTHELM_LANG"] = "en"
        code, out, _err = self.run_cli("inventory", "--home", str(self.home), *args, fmt="json")
        self.assertEqual(code, 0)
        return json.loads(out)

    def run_cli(self, *args: str, fmt: str = "json") -> tuple[int, str, str]:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = agenthelm.main([*args, "--output", fmt]) if args[0] == "inventory" else agenthelm.main(
                [*args, "--home", str(self.home), "--latest", str(self.latest_file), "--output", fmt])
        return code, out.getvalue(), err.getvalue()

    def test_inventory_adds_cli_evidence_for_a_named_client(self) -> None:
        shutil.copytree(FIXTURE / "plugin", self.home / ".gemini/config/plugins/sample-plugin")
        write(self.home / ".stub-agy" / "state.json", {"sample-plugin": {"enabled": False}})
        report = self.inventory("--client", "agy")
        section = report["cli"]["agy"]
        self.assertEqual((section["evidence"], section["ok"]), ("cli", True))
        self.assertEqual(section["plugins"], [{
            "name": "sample-plugin", "enabled": False, "version": "1.0.0", "path": "~/.gemini/config/plugins/sample-plugin",
            "skills": ["sample-skill"], "mcp_servers": ["sample"], "hooks": ["sample"]}])
        self.assertEqual(self.commands(), [])   # only the read-only list ran
        self.assertEqual(self.called(), ["plugin list --json"])

    def test_the_file_evidence_and_the_cli_evidence_stay_apart(self) -> None:
        shutil.copytree(FIXTURE / "home/.gemini", self.home / ".gemini", dirs_exist_ok=True)
        report = self.inventory("--client", "agy", "--type", "mcp")
        self.assertEqual([(m["name"], m["transport"], m["loaded"], m["evidence"]) for m in report["mcps"]],
                         [("alpha", "stdio", "unknown", "file"), ("paused", "stdio", "no", "file"),
                          ("remote", "http", "unknown", "file")])
        self.assertEqual(next(m for m in report["mcps"] if m["name"] == "remote")["url_host"], "mcp.example.com")
        self.assertEqual(report["cli"]["agy"]["plugins"], [])
        self.assertTrue(all(m["store"] == "~/.gemini/config/mcp_config.json" for m in report["mcps"]))

    def test_an_undetermined_skills_directory_is_not_read(self) -> None:
        write(self.home / ".gemini/skills/guess/SKILL.md", "---\nname: guess\ndescription: x\n---\n")
        report = self.inventory("--client", "agy", "--type", "skills")
        self.assertEqual(report["skills"], [])

    def test_a_failed_list_is_no_evidence(self) -> None:
        with mock.patch.dict(os.environ, {"AGY_STUB_LIST_FAIL": "1"}):
            report = self.inventory("--client", "agy")
        self.assertEqual(report["cli"]["agy"], {"evidence": "unknown", "ok": False, "reason": "nonzero-exit", "plugins": []})

    def test_a_client_that_is_not_installed_is_skipped_without_noise(self) -> None:
        (self.bin / "agy").unlink()
        report = self.inventory("--client", "agy")
        self.assertNotIn("cli", report)

    def test_the_text_output_lists_the_plugins_the_client_reports(self) -> None:
        shutil.copytree(FIXTURE / "plugin", self.home / ".gemini/config/plugins/sample-plugin")
        write(self.home / ".stub-agy" / "state.json", {"sample-plugin": {"enabled": True}})
        out, err = __import__("io").StringIO(), __import__("io").StringIO()
        from contextlib import redirect_stderr, redirect_stdout
        with redirect_stdout(out), redirect_stderr(err):
            agenthelm.main(["inventory", "--home", str(self.home), "--client", "agy", "--output", "text", "--lang", "en"])
        self.assertIn("plugin agy sample-plugin", out.getvalue())
        self.assertIn("cli", out.getvalue())


class SyncTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.project = make_project(self.root / "proj", "proj")
        self.recovery = self.root / "recovery"

    def sync(self, apply: bool = False, **kwargs):
        return core.sync_project(self.project, "agy", home=self.home, apply=apply, recovery_root=self.recovery, **kwargs)

    def staged(self) -> Path:
        return self.home / ".agent-pack/packages/agy/proj"

    def test_a_preview_builds_and_checks_the_package_but_runs_no_install(self) -> None:
        result = self.sync()
        self.assertFalse(result["applied"])
        self.assertEqual([op["action"] for op in result["operations"]], ["install-plugin", "run-client-command"])
        self.assertEqual(result["operations"][1]["to"], f"agy plugin install {self.staged()}")
        self.assertFalse(self.staged().exists())
        self.assertEqual(self.commands(), [])

    def test_apply_stages_the_package_and_hands_it_to_the_client(self) -> None:
        self.sync(apply=True)
        self.assertEqual(self.commands(), [f"plugin install {self.staged()}"])
        installed = self.home / ".gemini/config/plugins/proj"
        self.assertEqual(sorted(p.name for p in installed.iterdir()), ["mcp.json", "mcp_config.json", "plugin.json", "skills"])
        self.assertTrue((installed / "skills/sample-skill/SKILL.md").is_file())
        manifest = json.loads((installed / "plugin.json").read_text())
        self.assertEqual(manifest["$schema"], core.AGENT_PLUGIN_SCHEMA)   # the portable manifest, not a client-specific one

    def test_mcp_config_is_generated_from_the_same_entries_as_mcp_json(self) -> None:
        write(self.project / ".agents" / "mcp.json", {"$schema": core.AGENT_PLUGIN_MCP_SCHEMA, "mcpServers": {
            "local-check": {"type": "stdio", "command": "python3", "args": ["-m", "checker"]},
            "remote": {"type": "streamable-http", "url": "https://mcp.example.com/mcp",
                       "headers": {"Authorization": "Bearer ${EXAMPLE_TOKEN}", "X-Static": "1"}}}})
        with mock.patch.dict(os.environ, {"EXAMPLE_TOKEN": "t-dummy"}):
            self.sync(apply=True)
        package = self.staged()
        portable = json.loads((package / "mcp.json").read_text())["mcpServers"]
        projected = json.loads((package / "mcp_config.json").read_text())["mcpServers"]
        self.assertEqual(sorted(portable), sorted(projected))
        self.assertTrue(all("type" not in entry for entry in projected.values()))
        self.assertEqual(projected["remote"]["serverUrl"], "https://mcp.example.com/mcp")
        self.assertNotIn("url", projected["remote"])
        self.assertEqual(projected["remote"]["headers"]["Authorization"], "Bearer ${env:EXAMPLE_TOKEN}")  # declared-variables
        self.assertEqual(projected["local-check"], {"command": "python3", "args": ["-m", "checker"]})

    def test_an_unchanged_package_that_is_installed_does_nothing(self) -> None:
        self.sync(apply=True)
        self.calls.write_text("")
        again = self.sync(apply=True)
        self.assertEqual(again["operations"], [{"action": "keep-plugin", "client": "agy", "name": "proj", "to": str(self.staged())}])
        self.assertEqual(self.commands(), [])

    def test_a_changed_package_is_uninstalled_and_installed_again(self) -> None:
        self.sync(apply=True)
        self.calls.write_text("")
        write(self.project / ".agents/skills/sample-skill/SKILL.md",
              "---\nname: sample-skill\ndescription: Changed.\n---\n\n# Changed\n")
        result = self.sync(apply=True)
        self.assertEqual(result["operations"][0]["action"], "replace-plugin")
        self.assertEqual(self.commands(), ["plugin uninstall proj", f"plugin install {self.staged()}"])
        archived = list(self.recovery.glob("*-agy-proj/replaced-plugin/proj/skills/sample-skill/SKILL.md"))
        self.assertEqual(len(archived), 1)
        self.assertIn("Test skill", archived[0].read_text())   # the previous staged copy went to the recovery directory

    def test_a_disabled_package_is_disabled_again(self) -> None:
        self.sync(apply=True)
        write(self.home / ".stub-agy" / "state.json", {"proj": {"enabled": False}})
        self.calls.write_text("")
        write(self.project / ".agents/skills/sample-skill/SKILL.md", "---\nname: sample-skill\ndescription: Changed.\n---\n")
        self.sync(apply=True)
        self.assertEqual(self.commands(), ["plugin uninstall proj", f"plugin install {self.staged()}", "plugin disable proj"])

    def test_a_failed_install_restores_the_previous_package(self) -> None:
        self.sync(apply=True)
        before = (self.staged() / "skills/sample-skill/SKILL.md").read_text()
        write(self.project / ".agents/skills/sample-skill/SKILL.md", "---\nname: sample-skill\ndescription: Changed.\n---\n")
        with mock.patch.dict(os.environ, {"AGY_STUB_INSTALL_FAIL": "1"}):
            with self.assertRaisesRegex(core.AgentPackError, "rolled back.*install failed"):
                self.sync(apply=True)
        self.assertEqual((self.staged() / "skills/sample-skill/SKILL.md").read_text(), before)

    def test_project_level_entries_are_skipped_not_written(self) -> None:
        profile = self.project / ".agents" / PROJECT_PROFILE_NAMESPACE / "project.json"
        data = json.loads(profile.read_text())
        data["capabilities"]["mcp_scope"] = {"local-check": "project"}
        write(profile, data)
        result = self.sync()
        self.assertIn({"kind": "mcp", "name": "local-check", "client": "agy", "reason": "client-project-unsupported"},
                      result.get("skipped", []))

    def test_options_that_only_one_client_supports_are_refused(self) -> None:
        for kwargs in ({"scope": "user"}, {"project_store": "file"}, {"only": ["mcp"]}, {"replace_skills": True}):
            with self.assertRaisesRegex(core.AgentPackError, "supported for the claude client only"):
                self.sync(**kwargs)

    def test_a_missing_client_program_is_a_clear_error_on_apply_only(self) -> None:
        (self.bin / "agy").unlink()
        self.sync()   # the preview still plans (it names the command it would run)
        with self.assertRaisesRegex(core.AgentPackError, "agy was not found"):
            self.sync(apply=True)
        self.assertFalse(self.staged().exists())

    def test_drift_compares_the_staged_package_and_doctor_reports_cli_evidence(self) -> None:
        self.assertFalse(core.client_drift(self.project, "agy", self.home)["in_sync"])
        self.sync(apply=True)
        self.assertTrue(core.client_drift(self.project, "agy", self.home)["in_sync"])
        result = core.doctor(self.project, ["agy"], self.home)
        section = result["clients"]["agy"]["cli"]
        self.assertEqual((section["evidence"], section["ok"], [p["name"] for p in section["plugins"]]), ("cli", True, ["proj"]))
        self.assertTrue(any("agent-plugins-spec" in note for note in result["warnings"]))   # the switch state is reported unknown
        self.assertTrue(result["ok"])   # advisory only: it never changes ok

    def test_doctor_reports_a_disabled_plugin_and_a_missing_companion(self) -> None:
        write(self.home / ".gemini/config/plugins/agent-pack/plugin.json", {"name": "agent-pack", "version": "1.0.0"})
        write(self.home / ".stub-agy" / "state.json", {"agent-pack": {"enabled": False}})
        result = core.doctor(self.project, ["agy"], self.home)
        warnings = result["warnings"]
        self.assertIn("agy: plugin agent-pack is disabled", warnings)
        self.assertIn("agy: plugin agent-pack has no hooks.json (hooks)", warnings)
        self.assertIn("agy: plugin agent-pack has no mcp_config.json (mcp)", warnings)

    def test_doctor_without_a_program_concludes_nothing(self) -> None:
        (self.bin / "agy").unlink()
        result = core.doctor(self.project, ["agy"], self.home)
        self.assertEqual(result["clients"]["agy"]["cli"]["reason"], "not-found")
        self.assertTrue(any("could not be read" in note for note in result["warnings"]))


class OtherCommandTests(Fixture):
    def test_relocate_and_dedupe_say_why_they_do_not_apply(self) -> None:
        import io
        from contextlib import redirect_stderr, redirect_stdout
        for ref, to in (("skill:x@claude:user", "agy:user"), ("skill:x@agy:project", "claude:user"),
                        ("mcp:x@agy:user", "codex:user"), ("mcp:x@codex:user", "agy:user")):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = agenthelm.main(["relocate", ref, "--to", to, "--home", str(self.home), "--output", "json"])
            plan = json.loads(out.getvalue())["plan"]
            self.assertEqual(code, 1, ref)
            self.assertEqual([item["code"] for item in plan["blocked"]], ["unsupported-client"], ref)
            self.assertIn("agy", plan["blocked"][0]["message"])
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = agenthelm.main(["dedupe", "g1", "--keep", "skill:x@agy:project", "--home", str(self.home)])
        self.assertEqual(code, 1)
        self.assertIn("agy", err.getvalue())

    def test_the_nudge_hook_speaks_the_declared_output_for_the_declared_events(self) -> None:
        import agent_pack_nudge as nudge
        self.assertEqual(nudge.render("agy", "session-start", context="hello"),
                         {"injectSteps": [{"systemMessage": {"systemMessage": "hello"}}]})
        self.assertEqual(nudge.render("agy", "stop", context="bye"),
                         {"injectSteps": [{"systemMessage": {"systemMessage": "bye"}}]})
        self.assertIsNone(nudge.render("agy", "prompt-submit", context="x"))   # unsupported: no such hook
        self.assertIsNone(nudge.render("agy", "session-start"))
        self.assertIn("agy", nudge.HOOK_CLIENTS)
        self.assertNotIn("agy", nudge.CLIENTS)

    def test_the_nudge_hook_accepts_the_client_and_a_user_can_set_its_stop_mode(self) -> None:
        import agent_pack_nudge as nudge
        args = nudge.parser().parse_args(["hook", "--event", "stop", "--client", "agy"])
        self.assertEqual(args.client, "agy")
        config, warnings = nudge.merge_config(None, {"stop_prompt": {"agy": "always"}})
        self.assertEqual((config["stop_prompt"]["agy"], warnings), ("always", []))
        default, _w = nudge.merge_config(None, None)
        self.assertNotIn("agy", default["stop_prompt"])   # the default modes stay those of the default clients


if __name__ == "__main__":
    unittest.main()
