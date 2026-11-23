"""Gemini CLI as a declared, deprecated client (#134 batch E): declaration, extensions-dir reader, trust-prompt hand-over.

Everything runs against ``fixtures/clients/gemini``: a stub ``gemini`` that records how it was started, an installed extension and
a plugin directory with the manifest. The real client is not run (verified in the release-candidate stage instead).
"""
from __future__ import annotations

import json
import os
import pty
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = HERE.parent / "runtime"
FIXTURE = HERE / "fixtures" / "clients" / "gemini"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(HERE))

import agent_pack_clients as registry  # noqa: E402
import agenthelm  # noqa: E402
import test_agenthelm_reporting as base  # noqa: E402

LATEST = {**base.LATEST, "plugins": {"agent-pack": {"version": "2.0.0"}, "agents-init": {"version": "2.0.0"}},
          "release": {"tag": "v2.0.0", "tarball_sha256": "x"}}


class GeminiFixture(base.Base):
    def setUp(self) -> None:
        super().setUp()
        base.write(self.latest_file, LATEST)
        os.environ["AGENTHELM_LANG"] = "en"
        self.package = self.root / "package"
        shutil.copytree(FIXTURE / "package", self.package)
        self.log = self.root / "gemini.log"
        stub = self.bin / "gemini"
        stub.write_text((FIXTURE / "bin" / "gemini").read_text().replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
        stub.chmod(0o755)

    def installed(self, version: str = "1.0.0") -> None:
        shutil.copytree(FIXTURE / "home" / ".gemini", self.home / ".gemini", dirs_exist_ok=True)
        base.write(self.home / ".gemini/extensions/agent-pack/gemini-extension.json", {"name": "agent-pack", "version": version})

    def stub_calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def run_process(self, *args: str, terminal: bool, extra_env: dict | None = None, timeout: int = 60) -> tuple[int, dict, str]:
        """Run the CLI as a real process. ``terminal``: stdin is a pseudo-terminal nobody writes to; otherwise /dev/null."""
        env = {**os.environ, "HOME": str(self.home), "PATH": f"{self.bin}{os.pathsep}{os.path.dirname(sys.executable)}",
               "GEMINI_STUB_LOG": str(self.log), "AGENTHELM_LANG": "en", **(extra_env or {})}
        command = [sys.executable, str(RUNTIME / "agenthelm.py"), *args, "--home", str(self.home), "--latest", str(self.latest_file),
                   "--output", "json"]
        if terminal:
            master, slave = pty.openpty()
            try:
                done = subprocess.run(command, stdin=slave, capture_output=True, text=True, env=env, timeout=timeout)
            finally:
                os.close(slave)
                os.close(master)
        else:
            done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=timeout)
        return done.returncode, json.loads(done.stdout), done.stderr


class DeclarationTests(unittest.TestCase):
    def test_gemini_is_declared_deprecated_and_outside_the_default_set(self) -> None:
        driver = registry.get("gemini")
        self.assertEqual(registry.lifecycle(driver), "deprecated")
        self.assertFalse(driver["capabilities"]["default_set"])
        self.assertNotIn("gemini", registry.default_ids())
        self.assertIn("gemini", registry.installable_ids())
        self.assertEqual(registry.deprecated_ids(), ("gemini",))
        self.assertEqual(agenthelm.CLIENT_KEYS, ("claude", "codex", "cursor"))

    def test_the_distribution_declares_a_trust_prompt_and_a_bundle_directory(self) -> None:
        driver = registry.get("gemini")
        self.assertEqual((registry.interactive(driver), registry.source_kind(driver)), ("trust-prompt", "bundle-dir"))
        self.assertEqual(registry.distribution(driver)["installed_state"], "extensions-dir")
        self.assertEqual(registry.expand_step(driver, "install", cli="gemini", source_dir="/x"),
                         ["gemini", "extensions", "install", "/x"])
        self.assertIsNone(registry.expand_step(driver, "update"))
        for client in ("claude", "codex", "cursor"):
            self.assertEqual(registry.interactive(registry.get(client)), "none")

    def test_clients_reports_the_lifecycle_of_gemini_only(self) -> None:
        data = registry.report(Path("/nonexistent"), lambda name: None, lambda name: None)
        found = {item["id"]: item for item in data["clients"]}
        self.assertEqual(found["gemini"]["lifecycle"], "deprecated")
        self.assertTrue(all("lifecycle" not in item for name, item in found.items() if name != "gemini"))

    def test_the_manifest_is_a_required_companion_this_release_does_not_ship(self) -> None:
        driver = registry.get("gemini")
        self.assertEqual(registry.manifest_file(driver), "gemini-extension.json")
        self.assertEqual(registry.required_files(driver), ("gemini-extension.json",))
        self.assertEqual(registry.companions(driver), ())  # none shipped with the release
        for plugin in ("agent-pack", "agents-init"):  # the repository really has no such file: the flag is true
            self.assertFalse((HERE.parents[1] / plugin / "gemini-extension.json").exists())


class ReaderTests(GeminiFixture):
    def test_the_extensions_dir_reader_reads_name_and_version(self) -> None:
        self.installed("1.0.0")
        found = agenthelm.installed_plugins(agenthelm.Env(self.home) if hasattr(agenthelm, "Env") else None, {"agent-pack", "agents-init"})
        mine = [item for item in found if item["client"] == "gemini"]
        self.assertEqual([(item["plugin"], item["version"]) for item in mine], [("agent-pack", "1.0.0")])
        self.assertEqual(mine[0]["path"], str(self.home / ".gemini/extensions/agent-pack"))

    def test_a_linked_extension_is_a_development_install(self) -> None:
        work = self.root / "work" / "agent-pack"
        base.write(work / "gemini-extension.json", {"name": "agent-pack", "version": "1.0.0"})
        (self.home / ".gemini/extensions").mkdir(parents=True)
        (self.home / ".gemini/extensions/agent-pack").symlink_to(work)
        code, payload = self.run_json("check", "--no-project", "--client", "gemini")
        item = payload["plugins"][0]
        self.assertEqual((item["client"], item.get("dev")), ("gemini", "symlink"))

    def test_the_reader_never_writes(self) -> None:
        self.installed()
        before = sorted(str(p) for p in self.home.rglob("*"))
        self.run_json("check", "--no-project", "--client", "gemini")
        self.assertEqual(sorted(str(p) for p in self.home.rglob("*")), before)

    def test_without_the_manifest_the_directory_is_not_an_install(self) -> None:
        (self.home / ".gemini/extensions/agent-pack").mkdir(parents=True)
        _c, payload = self.run_json("check", "--no-project", "--client", "gemini")
        self.assertEqual(payload["plugins"], [])


class SelectionTests(GeminiFixture):
    def test_gemini_is_not_touched_without_naming_it(self) -> None:
        self.installed()
        (self.home / ".claude").mkdir()
        _c, payload = self.run_json("check", "--no-project")
        self.assertEqual([item for item in payload["plugins"] if item["client"] == "gemini"], [])
        _c, payload = self.run_json("install")
        self.assertEqual([step for step in payload["steps"] if step["client"] == "gemini"], [])
        self.assertNotIn("gemini", payload["clients"])

    def test_naming_it_selects_it_and_says_why_it_is_not_the_default(self) -> None:
        self.installed()
        _c, payload = self.run_json("install", "--client", "gemini", "--source", str(self.package))
        self.assertEqual(payload["clients"], ["gemini"])
        self.assertTrue(all("deprecated" in step["note"] for step in payload["steps"]))

    def test_all_covers_every_installable_client_deprecated_included(self) -> None:
        _c, payload = self.run_json("install", "--client", "all", "--source", str(self.package))
        self.assertEqual(payload["clients"], ["claude", "codex", "cursor", "agy", "gemini"])

    def test_the_client_option_accepts_gemini_and_all(self) -> None:
        parsed = agenthelm.parser().parse_args(["install", "--client", "gemini", "--client", "all"])
        self.assertEqual(parsed.client, ["gemini", "all"])


class InstallTests(GeminiFixture):
    def test_the_preview_prints_the_command_asks_nothing_and_says_it_will_ask_the_user(self) -> None:
        code, payload = self.run_json("install", "--client", "gemini", "--source", str(self.package))
        self.assertEqual(code, 0, payload)
        self.assertEqual([step["status"] for step in payload["steps"]], ["preview", "preview"])
        step = payload["steps"][0]
        self.assertEqual(step["command"], f"{self.bin}/gemini extensions install {self.package}/plugins/agent-pack")
        self.assertEqual(step["interactive"], "trust-prompt")
        self.assertIn("asks you to confirm", step["note"])
        self.assertIn("does not answer, forward or bypass", step["note"])
        self.assertEqual(self.stub_calls(), [])
        self.assertFalse((self.home / ".gemini").exists())

    def test_a_release_without_the_manifest_is_blocked_before_anything_runs(self) -> None:
        code, payload = self.run_json("install", "--client", "gemini")
        step = payload["steps"][0]
        self.assertEqual((step["status"], step["blocker"]), ("blocked", "companion-not-shipped"))
        self.assertIn("gemini-extension.json", step["blocked"])
        self.assertEqual(self.stub_calls(), [])

    def test_a_checkout_without_the_manifest_fails_and_names_the_file(self) -> None:
        (self.package / "plugins/agent-pack/gemini-extension.json").unlink()
        code, payload = self.run_json("install", "--client", "gemini", "--source", str(self.package), "--plugin", "agent-pack")
        self.assertEqual(code, 1)
        self.assertEqual(payload["steps"][0]["status"], "failed")
        self.assertIn("gemini-extension.json", payload["steps"][0]["error"])

    def test_apply_gives_the_command_the_users_terminal_and_writes_nothing_to_it(self) -> None:
        code, payload, _stderr = self.run_process("install", "--client", "gemini", "--source", str(self.package),
                                                  "--plugin", "agent-pack", "--apply", terminal=True)
        self.assertEqual(code, 0, payload)
        (call,) = self.stub_calls()
        self.assertEqual(call["argv"], ["extensions", "install", f"{self.package}/plugins/agent-pack"])
        self.assertTrue(call["stdin_tty"], "the command must get the user's terminal as its standard input")
        self.assertFalse(call["stdin_has_data"], "the tool must not write an answer to the command's input")
        self.assertNotIn("--consent", call["argv"])  # the flag that skips the question is never passed
        step = payload["steps"][0]
        self.assertEqual((step["status"], step["result"]["terminal"]), ("applied", "inherited"))
        self.assertEqual([(c["before"], c["after"]) for c in step["changes"]], [(None, "2.0.0")])  # read back

    def test_apply_without_a_terminal_does_not_start_the_command(self) -> None:
        code, payload, _stderr = self.run_process("install", "--client", "gemini", "--source", str(self.package),
                                                  "--plugin", "agent-pack", "--apply", terminal=False)
        self.assertEqual(code, 1)
        self.assertEqual(payload["steps"][0]["status"], "failed")
        self.assertIn("interactive terminal", payload["steps"][0]["error"])
        self.assertEqual(self.stub_calls(), [])

    def test_a_command_that_exits_non_zero_is_a_failure_and_the_exit_code_is_reported(self) -> None:
        code, payload, _stderr = self.run_process("install", "--client", "gemini", "--source", str(self.package),
                                                  "--plugin", "agent-pack", "--apply", terminal=True,
                                                  extra_env={"GEMINI_STUB_EXIT": "3"})
        self.assertEqual(code, 1)
        step = payload["steps"][0]
        self.assertEqual(step["status"], "failed")
        self.assertIn("exit code 3", step["error"])
        self.assertIn("unconfirmed", step["error"])
        self.assertFalse((self.home / ".gemini/extensions/agent-pack").exists())

    def test_an_installed_extension_is_left_alone(self) -> None:
        self.installed("1.0.0")
        _c, payload = self.run_json("install", "--client", "gemini", "--source", str(self.package), "--plugin", "agent-pack")
        self.assertEqual(payload["steps"][0]["status"], "skipped")
        self.assertEqual(self.stub_calls(), [])


class UpdateTests(GeminiFixture):
    def test_the_preview_shows_uninstall_then_install_and_the_risk(self) -> None:
        self.installed("1.0.0")
        _c, payload = self.run_json("update", "--no-project", "--only", "plugins", "--client", "gemini", "--source", str(self.package))
        steps = [step for step in payload["steps"] if step["client"] == "gemini"]
        self.assertEqual(len(steps), 1)
        step = steps[0]
        self.assertEqual(step["status"], "preview")
        self.assertIn("extensions uninstall agent-pack && ", step["command"])
        self.assertIn(f"extensions install {self.package}/plugins/agent-pack", step["command"])
        self.assertIn("not installed", step["note"])
        self.assertEqual(self.stub_calls(), [])

    def test_apply_uninstalls_then_installs_on_the_terminal_and_reads_the_version_back(self) -> None:
        self.installed("1.0.0")
        code, payload, _stderr = self.run_process("update", "--no-project", "--only", "plugins", "--client", "gemini",
                                                  "--source", str(self.package), "--apply", terminal=True)
        self.assertEqual(code, 0, payload)
        calls = self.stub_calls()
        self.assertEqual([call["argv"][:2] for call in calls], [["extensions", "uninstall"], ["extensions", "install"]])
        self.assertTrue(calls[1]["stdin_tty"])
        self.assertFalse(any(call["stdin_has_data"] for call in calls))
        step = [s for s in payload["steps"] if s["client"] == "gemini"][0]
        self.assertEqual((step["status"], step["before"], step["after"]), ("applied", "1.0.0", "2.0.0"))
        self.assertEqual(json.loads((self.home / ".gemini/extensions/agent-pack/gemini-extension.json").read_text())["version"], "2.0.0")

    def test_update_without_naming_gemini_leaves_it_alone(self) -> None:
        self.installed("1.0.0")
        (self.home / ".claude").mkdir()
        _c, payload = self.run_json("update", "--no-project", "--only", "plugins", "--source", str(self.package))
        self.assertEqual([step for step in payload["steps"] if step["client"] == "gemini"], [])


class InventoryTests(GeminiFixture):
    def test_the_user_skill_directory_is_read_and_the_extension_directory_is_not_a_skill_store(self) -> None:
        self.installed()
        import io
        from contextlib import redirect_stdout
        import agent_pack_inventory
        out = io.StringIO()
        with redirect_stdout(out):
            agenthelm.main(["inventory", "--client", "gemini", "--type", "skills", "--home", str(self.home), "--output", "json",
                            "--project", str(self.root)])
        data = json.loads(out.getvalue())
        names = [entry["name"] for entry in data["skills"]]
        self.assertEqual(names, ["linked-demo"])
        self.assertEqual({entry["client"] for entry in data["skills"]}, {"gemini"})


if __name__ == "__main__":
    unittest.main()
