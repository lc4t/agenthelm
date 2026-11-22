"""Distribution declarations (#134 batch C): validation, the installed-state readers, and a client that exists only as data.

Covers ``distribution.steps`` / ``interactive`` / ``source`` / ``installed_state`` and friends (design section 4.3), the three
installed-state readers (one per layout, named for the layout), the rule that a user declaration never carries ``steps``
(D-R1), and a synthetic ``marketplace-cli`` client with a made-up id, a stub executable and a fixture tree whose install and
update previews come out of the declaration without a line of code naming it (the seed of the section 7.3 guard).
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(TESTS))

import agent_pack_clients as registry  # noqa: E402
import agenthelm  # noqa: E402

PLUGINS = ("agent-pack", "agents-init")


def marketplace_driver(**changes) -> dict:
    data = {"schema_version": 2, "id": "synthetic", "display": "Synthetic",
            "detect": {"executables": ["synthetic-cli"], "dirs": [{"path": "~/.synthetic"}]},
            "distribution": {
                "type": "marketplace-cli", "source": "marketplace", "interactive": "none",
                "installed_state": "cache-dir-versioned", "config_dir": {"path": "~/.synthetic"},
                "steps": {"add_source": ["{cli}", "extension", "source", "add", "{slug}"],
                          "install": ["{cli}", "extension", "get", "{plugin}@{marketplace}"],
                          "update_source": ["{cli}", "extension", "source", "refresh", "{marketplace}"]}},
            "stores": {"skills": [], "mcp": []},
            "capabilities": {"default_set": False, "read_only": False, "project_mcp_write": False,
                             "marketplace_update": True}}
    data.update(changes)
    return data


def with_distribution(**changes) -> dict:
    data = marketplace_driver()
    data["distribution"].update(changes)
    return data


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


class Rejected(unittest.TestCase):
    def reject(self, data: dict, fragment: str) -> None:
        with self.assertRaises(registry.DriverDataError) as raised:
            registry.validate_builtin(data, "synthetic.json")
        self.assertIn(fragment, str(raised.exception))

    def test_the_reference_driver_is_valid(self) -> None:
        self.assertIs(registry.validate_builtin(marketplace_driver(), "synthetic.json")["id"], "synthetic")

    def test_a_step_that_is_not_needed_is_left_out_not_empty(self) -> None:
        data = marketplace_driver()
        data["distribution"]["steps"]["add_source"] = []
        self.reject(data, "distribution.steps.add_source")
        data["distribution"]["steps"]["add_source"] = [""]
        self.reject(data, "distribution.steps.add_source")
        data["distribution"]["steps"].pop("add_source")
        registry.validate_builtin(data, "synthetic.json")
        self.assertIsNone(registry.expand_step(data, "add_source"))
        self.assertEqual(registry.expand_step(data, "install", cli="x", plugin="p", marketplace="m"),
                         ["x", "extension", "get", "p@m"])

    def test_install_is_required_for_a_marketplace_cli(self) -> None:
        data = marketplace_driver()
        data["distribution"]["steps"].pop("install")
        self.reject(data, "needs install")
        data["distribution"].pop("steps")
        self.reject(data, "needs steps")

    def test_placeholders_are_limited_to_the_documented_ones(self) -> None:
        for bad in ("{path}", "{plugin", "{PLUGIN}", "x{}y", "{plugin}{unknown}", "$HOME"):
            data = marketplace_driver()
            data["distribution"]["steps"]["install"] = ["{cli}", "get", bad]
            self.reject(data, "distribution.steps.install")
        for good in ("{plugin}", "{plugin}@{marketplace}", "{source_dir}", "{version}", "{slug}", "{cli}"):
            data = marketplace_driver()
            data["distribution"]["steps"]["install"] = ["{cli}", "get", good]
            registry.validate_builtin(data, "synthetic.json")

    def test_a_step_starts_with_the_cli_placeholder_and_has_plain_arguments(self) -> None:
        for bad in (["extension", "get"], ["{cli}", "a b"], ["{cli}", "a;b"], ["{cli}", "$(x)"], ["{cli}"] + ["a"] * 16,
                    "extension get", [["{cli}"]], ["{cli}", 3]):
            data = marketplace_driver()
            data["distribution"]["steps"]["install"] = bad
            self.reject(data, "distribution.steps.install")

    def test_unknown_step_names_and_values_are_rejected(self) -> None:
        data = marketplace_driver()
        data["distribution"]["steps"]["run"] = ["{cli}", "x"]
        self.reject(data, "distribution.steps")
        self.reject(with_distribution(interactive="always"), "distribution.interactive")
        self.reject(with_distribution(interactive=True), "distribution.interactive")
        self.reject(with_distribution(source="url"), "distribution.source")
        self.reject(with_distribution(installed_state="registry"), "distribution.installed_state")
        self.reject(with_distribution(messages={"hello": "x"}), "distribution.messages")
        self.reject(with_distribution(scope_flag="--scope here"), "distribution.scope_flag")
        self.reject(with_distribution(unknown=1), "distribution must be")
        self.reject(with_distribution(type="shell"), "distribution.type")

    def test_the_vocabulary_values_the_design_names_are_accepted(self) -> None:
        for value in registry.INTERACTIVE:
            registry.validate_builtin(with_distribution(interactive=value), "synthetic.json")
        for value in registry.SOURCES:
            registry.validate_builtin(with_distribution(source=value), "synthetic.json")

    def test_steps_belong_to_a_marketplace_cli_and_need_an_executable(self) -> None:
        data = marketplace_driver()
        data["distribution"]["type"] = "directory-dropin"
        self.reject(data, "marketplace-cli only")
        data = marketplace_driver(detect={"dirs": [{"path": "~/.synthetic"}]})
        self.reject(data, "detect.executables")

    def test_the_installed_state_reader_needs_the_directory_it_reads(self) -> None:
        data = marketplace_driver()
        data["distribution"].pop("config_dir")
        self.reject(data, "config_dir")
        none = marketplace_driver(distribution="none")
        registry.validate_builtin(none, "synthetic.json")
        self.reject(marketplace_driver(distribution={"type": "none", "installed_state": "dropin-dir",
                                                     "config_dir": {"path": "~/.x"}}), "installed_state")

    def test_a_flag_is_a_boolean_and_only_the_documented_ones_exist(self) -> None:
        data = marketplace_driver()
        data["capabilities"]["market_pin"] = "yes"
        self.reject(data, "capabilities")
        data = marketplace_driver()
        data["capabilities"]["run_hook"] = True
        self.reject(data, "capabilities")


class UserDeclarations(unittest.TestCase):
    """D-R1: a file in the user's clients.d never decides which command runs."""

    def declaration(self, **changes) -> dict:
        data = {"schema_version": 1, "id": "example-ide", "display": "Example IDE",
                "detect": {"executables": ["example-ide"], "dirs": ["~/.example-ide"]},
                "distribution": "marketplace-cli", "stores": {}, "capabilities": {}}
        data.update(changes)
        return data

    def test_steps_anywhere_are_refused(self) -> None:
        steps = {"install": ["{cli}", "get", "{plugin}"]}
        for data in (self.declaration(steps=steps), self.declaration(distribution={"type": "marketplace-cli", "steps": steps}),
                     self.declaration(detect={"executables": ["x"], "steps": steps}),
                     self.declaration(stores={"skills": [{"steps": steps}]})):
            reason = registry.validate_declaration(data)
            self.assertIn("steps", reason or "")
        self.assertIsNone(registry.validate_declaration(self.declaration()))

    def test_the_object_form_of_distribution_is_not_a_user_field(self) -> None:
        self.assertIsNotNone(registry.validate_declaration(self.declaration(distribution={"type": "marketplace-cli"})))

    def test_the_warning_names_the_file_and_the_clients_report_does_not_list_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            write(home / ".config/agenthelm/clients.d/mine.json",
                  self.declaration(steps={"install": ["{cli}", "get"]}))
            write(home / ".config/agenthelm/clients.d/fine.json", self.declaration(id="fine-ide"))
            data = registry.report(home, lambda name: None, lambda name: None)
            self.assertEqual([w["file"] for w in data["warnings"]], ["mine.json"])
            self.assertIn("steps", data["warnings"][0]["reason"])
            self.assertEqual([c["id"] for c in data["clients"] if c["source"] == "declared"], ["fine-ide"])
            self.assertIn("警告：忽略声明 mine.json", registry.render_text(data) if os.environ.get("AGENTHELM_LANG") == "zh"
                          else "警告：忽略声明 mine.json")


class BuiltinDeclarations(unittest.TestCase):
    def test_the_commands_the_three_clients_run_are_the_ones_they_ran_before(self) -> None:
        values = {"cli": "X", "plugin": "P", "marketplace": "agenthelm", "slug": "o/r"}
        expected = {
            "claude": {"add_source": ["X", "plugin", "marketplace", "add", "o/r"],
                       "install": ["X", "plugin", "install", "P@agenthelm"],
                       "update_source": ["X", "plugin", "marketplace", "update", "agenthelm"],
                       "update": ["X", "plugin", "update", "P@agenthelm"]},
            "codex": {"add_source": ["X", "plugin", "marketplace", "add", "o/r"],
                      "install": ["X", "plugin", "add", "P@agenthelm"],
                      "update_source": ["X", "plugin", "marketplace", "upgrade", "agenthelm"],
                      "update": None},
            "cursor": {"add_source": None, "install": None, "update_source": None, "update": None}}
        for client, steps in expected.items():
            driver = registry.get(client)
            for name, argv in steps.items():
                self.assertEqual(registry.expand_step(driver, name, **values), argv, (client, name))

    def test_types_layouts_and_flags(self) -> None:
        by = {item["id"]: registry.distribution(item) for item in registry.BUILTIN}
        self.assertEqual({k: (v["type"], v.get("installed_state"), v.get("source"), v.get("interactive"))
                          for k, v in by.items() if k != "claude-desktop"},
                         {"claude": ("marketplace-cli", "records-json", "marketplace", "none"),
                          "codex": ("marketplace-cli", "cache-dir-versioned", "marketplace", "none"),
                          "cursor": ("directory-dropin", "dropin-dir", "bundle-dir", "none")})
        self.assertEqual(by["claude-desktop"], {"type": "none"})
        self.assertEqual(registry.ids_with("legacy_cleanup"), ("claude", "codex"))
        self.assertEqual(registry.ids_with("marketplace_update"), ("claude", "codex"))
        self.assertEqual(registry.ids_with("reinstall_rewrites_all"), ("cursor",))
        self.assertEqual(registry.ids_with("market_pin"), ("claude",))
        self.assertEqual(registry.ids_of_type("directory-dropin"), ("cursor",))
        self.assertEqual(registry.ids_of_type("marketplace-cli"), ("claude", "codex"))

    def test_the_clients_report_keeps_its_shape(self) -> None:
        data = registry.report(Path("/nonexistent-home"), lambda name: None, lambda name: None)
        for item in data["clients"]:
            self.assertEqual(set(item["capabilities"]), set(registry.CAPABILITIES))
            self.assertIn(item["distribution"], registry.DISTRIBUTIONS)

    def test_every_message_key_a_declaration_names_has_a_text(self) -> None:
        tables = {"recovery": agenthelm.RECOVERY_HINTS, "applied_recovery": agenthelm.RECOVERY_HINTS,
                  "unchanged": agenthelm.UNCHANGED_REASONS, "unchanged_next": agenthelm.UNCHANGED_ADVICE,
                  "describe_update_source": agenthelm.STEP_TEXTS, "describe_update": agenthelm.STEP_TEXTS,
                  "describe_dropin": agenthelm.STEP_TEXTS, "unavailable_next": agenthelm.STEP_TEXTS}
        self.assertEqual(set(tables), set(registry.MESSAGE_SLOTS))
        for driver in registry.BUILTIN:
            for slot, key in registry.distribution(driver).get("messages", {}).items():
                self.assertIn(key, tables[slot], (driver["id"], slot))

    def test_a_marketplace_driver_declares_the_slots_its_update_path_reads(self) -> None:
        for driver in registry.BUILTIN:
            kind = registry.distribution_type(driver)
            if kind == "marketplace-cli" and driver["capabilities"].get("marketplace_update"):
                self.assertIsNotNone(registry.message_key(driver, "describe_update_source"), driver["id"])
                if registry.expand_step(driver, "update") is not None:
                    self.assertIsNotNone(registry.message_key(driver, "describe_update"), driver["id"])
            if kind == "directory-dropin":
                self.assertIsNotNone(registry.message_key(driver, "describe_dropin"), driver["id"])


class InstalledStateReaders(unittest.TestCase):
    """One reader per layout. Each is checked against a tree of its own layout, with no client involved in the reader."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = agenthelm.Env(home=self.home, variables={})
        self.names = set(PLUGINS)

    def driver(self, state: str, directory: str = "~/.synthetic", env: str | None = None) -> dict:
        place = {"path": directory, **({"env": env} if env else {})}
        return marketplace_driver(id="synth", distribution={**marketplace_driver()["distribution"], "installed_state": state,
                                                            "config_dir": place})

    def test_records_json_reads_the_install_records_and_the_scope(self) -> None:
        base = self.home / ".synthetic"
        for plugin, version, scope in (("agent-pack", "1.2.0", "user"), ("agents-init", "abcdef1", "project")):
            path = base / f"plugins/cache/agenthelm/{plugin}/{version}"
            write(path / "plugin.json", {"name": plugin, "version": "9.9.9" if version == "abcdef1" else version})
        write(base / "plugins/installed_plugins.json", {"version": 2, "plugins": {
            "agent-pack@agenthelm": [{"scope": "user", "installPath": str(base / "plugins/cache/agenthelm/agent-pack/1.2.0"),
                                      "version": "1.2.0"}],
            "agents-init@agenthelm": [{"scope": "project", "installPath": str(base / "plugins/cache/agenthelm/agents-init/abcdef1"),
                                       "version": "abcdef1"}],
            "agent-pack@fork": [{"scope": "user", "installPath": str(self.root / "fork"), "version": "7.0.0"}],
            "unrelated@agenthelm": [{"scope": "user", "installPath": "x", "version": "1.0.0"}]}})
        driver = self.driver("records-json")
        found = agenthelm._installed_records_json(self.env, driver, self.names)
        self.assertEqual([(i["client"], i["plugin"], i["version"], i["scope"], "dev" in i) for i in found],
                         [("synth", "agent-pack", "1.2.0", "user", False), ("synth", "agents-init", "9.9.9", "project", False)])
        others = agenthelm._others_records_json(self.env, driver, self.names)
        self.assertEqual([(i["plugin"], i["marketplace"], i["dev"], i["version"]) for i in others],
                         [("agent-pack", "fork", "other-marketplace", "7.0.0")])

    def test_records_json_marks_a_symlink_and_a_local_directory_market_as_development(self) -> None:
        base = self.home / ".synthetic"
        work = self.root / "work"
        write(work / "plugin.json", {"name": "agent-pack", "version": "1.0.0"})
        (base / "plugins/cache/agenthelm/agent-pack").mkdir(parents=True)
        (base / "plugins/cache/agenthelm/agent-pack/1.0.0").symlink_to(work)
        record = {"version": 2, "plugins": {"agent-pack@agenthelm": [
            {"scope": "user", "installPath": str(base / "plugins/cache/agenthelm/agent-pack/1.0.0"), "version": "1.0.0"}]}}
        write(base / "plugins/installed_plugins.json", record)
        driver = self.driver("records-json")
        self.assertEqual([i["dev"] for i in agenthelm._installed_records_json(self.env, driver, self.names)], ["symlink"])
        (base / "plugins/cache/agenthelm/agent-pack/1.0.0").unlink()
        write(base / "plugins/cache/agenthelm/agent-pack/1.0.0/plugin.json", {"name": "agent-pack", "version": "1.0.0"})
        self.assertNotIn("dev", agenthelm._installed_records_json(self.env, driver, self.names)[0])
        write(base / "plugins/known_marketplaces.json", {"agenthelm": {"source": {"source": "directory", "path": "/x"}}})
        self.assertEqual([i["dev"] for i in agenthelm._installed_records_json(self.env, driver, self.names)],
                         ["local-directory-marketplace"])

    def test_records_json_survives_a_missing_or_corrupt_file(self) -> None:
        driver = self.driver("records-json")
        self.assertEqual(agenthelm._installed_records_json(self.env, driver, self.names), [])
        write(self.home / ".synthetic/plugins/installed_plugins.json", "{not json")
        self.assertEqual(agenthelm._installed_records_json(self.env, driver, self.names), [])
        write(self.home / ".synthetic/plugins/installed_plugins.json", {"plugins": ["not", "a", "mapping"]})
        self.assertEqual(agenthelm._others_records_json(self.env, driver, self.names), [])

    def test_cache_dir_versioned_picks_the_newest_version_and_skips_empty_directories(self) -> None:
        base = self.home / ".synthetic/plugins/cache"
        for version in ("1.0.0", "1.10.0", "1.9.0"):
            write(base / f"agenthelm/agent-pack/{version}/plugin.json", {"name": "agent-pack", "version": version})
        (base / "agenthelm/agent-pack/2.0.0").mkdir()  # no plugin.json: not an install
        write(base / "agenthelm/agents-init/0.1.0/plugin.json", {"name": "agents-init", "version": "0.1.0"})
        write(base / "fork/agent-pack/3.0.0/plugin.json", {"name": "agent-pack", "version": "3.0.0"})
        write(base / "personal/agent-pack/0.9.0/plugin.json", {"name": "agent-pack", "version": "0.9.0"})
        driver = self.driver("cache-dir-versioned")
        found = agenthelm._installed_cache_dir_versioned(self.env, driver, self.names)
        self.assertEqual([(i["plugin"], i["version"], Path(i["path"]).name, "scope" in i) for i in found],
                         [("agent-pack", "1.10.0", "1.10.0", False), ("agents-init", "0.1.0", "0.1.0", False)])
        others = agenthelm._others_cache_dir_versioned(self.env, driver, self.names)
        self.assertEqual([(i["plugin"], i["marketplace"], i["version"]) for i in others], [("agent-pack", "fork", "3.0.0")])

    def test_cache_dir_versioned_follows_the_directory_override_and_marks_symlinks(self) -> None:
        elsewhere = self.root / "elsewhere"
        work = self.root / "work"
        write(work / "plugin.json", {"name": "agent-pack", "version": "1.0.0"})
        (elsewhere / "plugins/cache/agenthelm/agent-pack").mkdir(parents=True)
        (elsewhere / "plugins/cache/agenthelm/agent-pack/1.0.0").symlink_to(work)
        env = agenthelm.Env(home=self.home, variables={"SYNTH_HOME": str(elsewhere)})
        driver = self.driver("cache-dir-versioned", env="SYNTH_HOME")
        found = agenthelm._installed_cache_dir_versioned(env, driver, self.names)
        self.assertEqual([(i["plugin"], i["dev"]) for i in found], [("agent-pack", "symlink")])
        self.assertEqual(agenthelm._installed_cache_dir_versioned(self.env, driver, self.names), [])

    def test_dropin_dir_reads_plugin_directories_and_marks_a_symlink(self) -> None:
        base = self.home / ".synthetic/plugins/local"
        write(base / "agent-pack/plugin.json", {"name": "agent-pack", "version": "1.0.0"})
        work = self.root / "work"
        write(work / "plugin.json", {"name": "agents-init", "version": "2.0.0"})
        (base / "agents-init").symlink_to(work)
        (base / "other").mkdir()
        driver = self.driver("dropin-dir")
        found = agenthelm._installed_dropin_dir(self.env, driver, self.names)
        self.assertEqual([(i["plugin"], i["version"], i.get("dev")) for i in found],
                         [("agent-pack", "1.0.0", None), ("agents-init", "2.0.0", "symlink")])
        self.assertEqual(agenthelm._no_others(self.env, driver, self.names), [])
        write(base / "agent-pack/plugin.json", "{bad")
        self.assertEqual(agenthelm._installed_dropin_dir(self.env, driver, {"agent-pack"})[0]["version"], None)

    def test_a_reader_exists_for_every_layout_the_registry_accepts(self) -> None:
        self.assertEqual(set(agenthelm.STATE_READERS), set(registry.INSTALLED_STATES))


class SyntheticClient(unittest.TestCase):
    """A client that exists only as a declaration and a fixture: nothing in the runtime names it."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls.log"
        self.client = "zq" + uuid.uuid4().hex[:8]
        self.executable = f"{self.client}-cli"
        self.assertNotIn(self.client, registry.all_ids())
        driver = marketplace_driver(
            id=self.client, display="Zed Quux",
            detect={"executables": [self.executable], "dirs": [{"path": f"~/.{self.client}", "env": "ZQ_HOME"}]})
        driver["distribution"].update(config_dir={"path": f"~/.{self.client}", "env": "ZQ_HOME"},
                                      scope_flag="--where",
                                      steps={**driver["distribution"]["steps"],
                                             "update": ["{cli}", "extension", "bump", "{plugin}@{marketplace}"]},
                                      messages={"recovery": "managed-by-client-no-backup"})
        folder = self.root / "drivers"
        folder.mkdir()
        (folder / f"{self.client}.json").write_text(json.dumps(driver), encoding="utf-8")
        loaded = registry.load_builtin(folder)  # the synthetic file is the whole registry for these tests
        for patcher in (mock.patch.object(registry, "BUILTIN", (*registry.BUILTIN, *loaded)),
                        mock.patch.object(registry, "BUILTIN_IDS", (*registry.BUILTIN_IDS, self.client)),
                        mock.patch.dict(os.environ, {"AGENTHELM_LANG": "en", "PATH": str(self.bin)}),
                        mock.patch.object(agenthelm, "known_cli_locations", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)
        stub = self.bin / self.executable
        stub.write_text(
            f"#!{sys.executable}\nimport json, pathlib, sys\nopen({str(self.calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:3] == ['extension', 'get']:  # what the client would do: put the plugin in its cache\n"
            "    name = sys.argv[3].split('@')[0]\n"
            f"    folder = pathlib.Path({str(self.home)!r}) / '.{self.client}/plugins/cache/agenthelm' / name / '2.0.0'\n"
            "    folder.mkdir(parents=True)\n"
            "    (folder / 'plugin.json').write_text(json.dumps({'name': name, 'version': '2.0.0'}))\n")
        stub.chmod(0o755)
        self.env = agenthelm.Env(home=self.home, variables={"PATH": str(self.bin)})
        self.latest = {"schema": 1, "repository": "https://github.com/example/agenthelm", "migrations": [],
                       "plugins": {name: {"version": "2.0.0"} for name in PLUGINS}}

    def install_cache(self, version: str, plugin: str = "agent-pack") -> None:
        write(self.home / f".{self.client}/plugins/cache/agenthelm/{plugin}/{version}/plugin.json",
              {"name": plugin, "version": version})

    def test_the_client_is_detected_from_its_declared_facts(self) -> None:
        driver = registry.get(self.client)
        self.assertFalse(registry.detect(driver, self.home, self.env.get, lambda name: None)["detected"])
        (self.home / f".{self.client}").mkdir()
        self.assertEqual(registry.detect(driver, self.home, self.env.get, lambda name: None)["evidence"]["kind"], "directory")
        self.assertIn(self.client, registry.report(self.home, self.env.get, lambda name: str(self.bin / name))["clients"][-1]["id"])

    def test_install_preview_comes_from_the_declared_steps(self) -> None:
        steps = agenthelm.install_steps(self.env, self.latest, [self.client], list(PLUGINS), None, self.home)
        results = agenthelm.execute_steps(steps, apply=False, recovery_root=None, env=self.env)
        executable = str(self.bin / self.executable)
        self.assertEqual([r["command"] for r in results], [
            f"{executable} extension source add example/agenthelm",
            f"{executable} extension get agent-pack@agenthelm",
            f"{executable} extension get agents-init@agenthelm"])
        self.assertEqual([r["description"] for r in results], [
            "Zed Quux: add the agenthelm marketplace (example/agenthelm)", "Zed Quux: install agent-pack",
            "Zed Quux: install agents-init"])
        self.assertTrue(all(r["status"] == "preview" for r in results), results)
        self.assertTrue(all(r["client"] == self.client for r in results))
        self.assertFalse(self.calls.exists())

    def test_install_apply_runs_exactly_those_commands_and_reads_the_version_back(self) -> None:
        steps = agenthelm.install_steps(self.env, self.latest, [self.client], ["agent-pack"], None, self.home)
        results = agenthelm.execute_steps(steps, apply=True, recovery_root=None, env=self.env, child_env=dict(os.environ))
        self.assertEqual(self.calls.read_text().splitlines(), ["extension source add example/agenthelm",
                                                              "extension get agent-pack@agenthelm"])
        self.assertEqual([r["status"] for r in results], ["applied", "applied"])
        self.assertEqual((results[1]["before"], results[1]["after"]), (None, "2.0.0"))

    def test_an_installed_plugin_is_skipped_and_a_missing_executable_is_blocked(self) -> None:
        self.install_cache("2.0.0")
        steps = agenthelm.install_steps(self.env, self.latest, [self.client], ["agent-pack"], None, self.home)
        self.assertIn("Already installed at 2.0.0, up to date", steps[1].skipped)
        (self.bin / self.executable).unlink()
        steps = agenthelm.install_steps(self.env, self.latest, [self.client], ["agent-pack"], None, self.home)
        self.assertIn(f"AGENTHELM_{self.executable.upper()}", steps[0].blocked)
        self.assertEqual(steps[0].command[0], self.executable)

    def report(self, behind_scope: str | None = "user") -> dict:
        self.install_cache("1.0.0")
        plugins = [{"client": self.client, "plugin": "agent-pack", "status": "behind", "version": "1.0.0",
                    "latest": "2.0.0", "scope": behind_scope}]
        return {"_latest": self.latest, "plugins": plugins, "content": [], "market_sync": None, "install_state": {}}

    def test_update_preview_comes_from_the_declared_steps(self) -> None:
        steps = agenthelm.update_steps(self.env, self.report(), None, None, self.home, [], [], None, "plugins")
        results = agenthelm.execute_steps(steps, apply=False, recovery_root=None, env=self.env)
        executable = str(self.bin / self.executable)
        self.assertEqual([(r["description"], r["command"]) for r in results], [
            ("Zed Quux: refresh the agenthelm marketplace", f"{executable} extension source refresh agenthelm"),
            ("Zed Quux: update agent-pack → 2.0.0", f"{executable} extension bump agent-pack@agenthelm")])
        self.assertEqual([r["status"] for r in results], ["preview", "preview"])

    def test_a_scope_other_than_the_default_is_passed_with_the_declared_flag(self) -> None:
        steps = agenthelm.update_steps(self.env, self.report("project"), None, None, self.home, [], [], None, "plugins")
        self.assertEqual(steps[-1].command[-2:], ["--where", "project"])
        self.assertEqual(steps[-1].scope, "project")

    def test_the_update_hint_and_the_legacy_flag_follow_the_declaration(self) -> None:
        self.assertEqual(agenthelm.update_hint(self.client),
                         f"{self.executable} extension source refresh agenthelm && "
                         f"{self.executable} extension bump {{plugin}}@agenthelm")
        self.assertTrue(registry.supports(self.client, "marketplace_update"))
        self.assertFalse(registry.supports(self.client, "legacy_cleanup"))

    def test_the_diagnosis_reads_the_installed_state_of_the_layout(self) -> None:
        self.install_cache("1.0.0")
        found = agenthelm.installed_plugins(self.env, set(PLUGINS))
        self.assertEqual([(i["client"], i["plugin"], i["version"]) for i in found], [(self.client, "agent-pack", "1.0.0")])
        self.assertEqual(agenthelm.installed_versions(self.env, self.client, list(PLUGINS)),
                         {"agent-pack": "1.0.0", "agents-init": None})


class UserLevelSteps(unittest.TestCase):
    def test_the_builtin_loader_never_reads_the_user_directory(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            write(home / ".config/agenthelm/clients.d/evil.json",
                  {"schema_version": 1, "id": "evil", "display": "Evil", "detect": {"executables": ["evil"]},
                   "distribution": "marketplace-cli", "steps": {"install": ["{cli}", "x"]}})
            declared, warnings = registry.load_declarations(home, lambda name: None)
            self.assertEqual(declared, [])
            self.assertEqual([w["file"] for w in warnings], ["evil.json"])
            self.assertTrue(copy.deepcopy(registry.BUILTIN_IDS))


if __name__ == "__main__":
    unittest.main()
