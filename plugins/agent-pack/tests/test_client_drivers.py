"""Client-driver registry (#134 Phase 1): derived enumerations, `agenthelm clients`, user declarations (validate only)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
sys.path.insert(0, str(RUNTIME))

import agent_pack_clients as registry  # noqa: E402
import agent_pack_inventory as inv  # noqa: E402
import agent_pack_nudge as nudge  # noqa: E402
import agent_pack_relocate as relocate  # noqa: E402
import agenthelm  # noqa: E402

AGENTHELM = RUNTIME / "agenthelm.py"
SNAPSHOT = TESTS / "fixtures" / "builtin_drivers_v6_14_0.json"
ROOT = TESTS.parents[2]


def valid(**changes) -> dict:
    data = {"schema_version": 1, "id": "example-ide", "display": "Example IDE",
            "detect": {"executables": ["example-ide"], "dirs": ["~/.example-ide"]},
            "distribution": "directory-dropin",
            "stores": {"skills": [{"scope": "user", "path": "~/.example-ide/skills", "format": "dir"}]},
            "capabilities": {"project_mcp_write": False}}
    data.update(changes)
    return data


class RegistryTests(unittest.TestCase):
    def test_enumerations_keep_their_content_and_order(self) -> None:
        self.assertEqual(registry.default_ids(), ("claude", "codex", "cursor"))
        self.assertEqual(agenthelm.CLIENTS, ("claude", "codex", "cursor"))
        self.assertEqual(agenthelm.CLIENT_KEYS, ("claude", "codex", "cursor"))
        self.assertEqual(agenthelm.CLIENT_NAMES, {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"})
        self.assertEqual(nudge.CLIENTS, ("claude", "codex", "cursor"))
        self.assertEqual(inv.CLIENTS, ("claude", "codex", "cursor"))
        self.assertEqual(inv.ALL_CLIENTS, ("claude", "codex", "cursor", "agy", "claude-desktop", "gemini"))
        self.assertEqual(registry.installable_ids(), ("claude", "codex", "cursor", "agy", "gemini"))

    def test_desktop_is_read_only_and_outside_the_default_set(self) -> None:
        desktop = registry.get("claude-desktop")
        self.assertEqual(registry.distribution_type(desktop), "none")
        self.assertTrue(desktop["capabilities"]["read_only"])
        self.assertNotIn("claude-desktop", registry.default_ids())

    def test_capability_lookup(self) -> None:
        self.assertTrue(registry.supports("claude", "project_mcp_write"))
        self.assertFalse(registry.supports("codex", "project_mcp_write"))
        self.assertFalse(registry.supports("nope", "project_mcp_write"))
        self.assertFalse(registry.supports("claude", "nope"))

    def test_project_write_support_comes_from_the_driver_capability(self) -> None:
        import agent_pack_config as config
        self.assertEqual({client: config.project_write_supported(client) for client in registry.all_ids()},
                         {"claude": True, "codex": False, "cursor": False, "agy": False, "claude-desktop": False, "gemini": False})
        for client in registry.all_ids():
            self.assertEqual(config.project_write_supported(client), registry.supports(client, "project_mcp_write"))
        self.assertFalse(config.project_write_supported("unknown-client"))

    def test_data_files_equal_the_v6_14_0_builtin_snapshot(self) -> None:
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in snapshot], ["claude", "codex", "cursor", "claude-desktop"])

        def comparable(driver: dict) -> dict:
            driver = json.loads(json.dumps(driver))
            driver["schema_version"] = 1  # the files declare 2 because they use write_target
            driver.pop("lifecycle", None)
            driver.pop("runtime_cache", None)  # added after 6.14.0 (#134 Phase 3)
            driver.pop("hooks", None)  # hook declarations added after 6.14.0 (#134 batch D1): see test_hook_declarations
            driver.pop("unsupported", None)
            driver.pop("precedence", None)  # read-side data added after the snapshot (see test_registry_read_side)
            driver.pop("sync", None)  # write-side data added after the snapshot (see test_sync_write_declarations)
            driver.pop("mcp", None)
            driver["distribution"] = registry.distribution_type(driver)  # the step declarations (batch C) are tested apart
            for flag in registry.EXTENDED_CAPABILITIES:  # schema v2 flags: tested in test_distribution_declarations
                driver["capabilities"].pop(flag, None)
            for kind in driver["stores"].values():
                for store in kind:
                    for key in ("write_target", "status", "root_key", "managed_by", "trust_required"):
                        store.pop(key, None)
            return driver

        # clients added after 6.14.0 have no snapshot entry: the four that did must still equal it, apart from what was added since
        self.assertEqual([comparable(item) for item in registry.BUILTIN if item["id"] in {s["id"] for s in snapshot}], snapshot)
        self.assertEqual([item["id"] for item in registry.BUILTIN if item["id"] not in {s["id"] for s in snapshot}], ["agy", "gemini"])  # test_agy_client, test_gemini_client
        self.assertEqual(sorted(path.stem for path in registry.DRIVERS_DIR.glob("*.json")), sorted(registry.BUILTIN_IDS))

    def test_write_targets_are_the_ones_relocate_wrote_before(self) -> None:
        self.assertEqual(registry.skill_write_targets(), {
            ("claude", "user"): "~/.claude/skills", ("claude", "project"): "<project>/.claude/skills",
            ("codex", "user"): "~/.agents/skills", ("codex", "project"): "<project>/.agents/skills",
            ("cursor", "user"): "~/.cursor/skills", ("cursor", "project"): "<project>/.agents/skills",
            ("gemini", "user"): "~/.gemini/skills", ("gemini", "project"): "<project>/.gemini/skills"})

    def test_every_consumer_reads_the_same_stores(self) -> None:
        import agent_pack_config as config
        for kind, table in (("skills", inv.SKILL_STORES), ("mcp", inv.MCP_STORES)):
            expected = {d["id"]: tuple((s["scope"], s["path"]) for s in d["stores"][kind])
                        for d in registry.BUILTIN if d["id"] in registry.default_ids() and d["stores"][kind]}
            self.assertEqual({k: v for k, v in table.items() if k in expected}, expected, kind)
            self.assertEqual(table, {k: v for k, v in registry.store_table(kind).items() if k in table}, kind)
        self.assertEqual(inv.PROJECT, "<project>")
        self.assertEqual(inv.PRIMARY_SKILL_STORE, registry.primary_skill_stores())
        self.assertEqual(relocate.TARGET_STORE, registry.skill_write_targets())
        for (client, scope), path in relocate.TARGET_STORE.items():  # a write target is always a store the client declares
            self.assertIn((scope, path), inv.SKILL_STORES[client])
        home = Path("/home/example")
        for client in registry.default_ids():
            first_user = next(path for scope, path in inv.SKILL_STORES[client] if scope == "user")
            self.assertEqual(config._client_skill_roots(client, home), [home / first_user[2:]])

    def test_client_choices_come_from_the_registry(self) -> None:
        import agent_pack_config as config
        import mcp_server
        parser = config.build_parser()
        choices = {}
        for command in ("doctor", "sync", "bootstrap", "reconcile"):
            sub = next(a for a in parser._actions if isinstance(a, __import__("argparse")._SubParsersAction))
            action = next(a for a in sub.choices[command]._actions if a.dest == "client")
            choices[command] = tuple(action.choices)
        self.assertEqual(choices, {"doctor": registry.all_ids(), "sync": registry.selectable_ids(),
                                   "bootstrap": registry.default_ids(), "reconcile": registry.selectable_ids()})
        enums = []

        def walk(node) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("client", "clients"):
                        item = value["items"] if key == "clients" else value
                        enums.append(item["enum"])
                    else:
                        walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(mcp_server.TOOLS)
        self.assertEqual(len(enums), 4)
        self.assertTrue(all(enum == list(registry.default_ids()) for enum in enums))

    def test_driver_files_ship_with_the_plugin(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import release_meta
        finally:
            sys.path.pop(0)
        plugin = ROOT / "plugins" / "agent-pack"
        for driver_id in registry.BUILTIN_IDS:
            relative = (plugin / "runtime" / "drivers" / f"{driver_id}.json").relative_to(plugin)
            self.assertTrue((plugin / relative).is_file(), relative)
            self.assertFalse(release_meta.is_unshipped(relative.parts), f"{relative} would be left out of a release")
        shipped = release_meta.pack_files(plugin, ["runtime"])
        self.assertTrue({f"runtime/drivers/{i}.json" for i in registry.BUILTIN_IDS} <= set(shipped))
        self.assertNotIn("tests", {part for path in shipped for part in Path(path).parts})

    def test_builtin_values_are_in_the_documented_vocabulary(self) -> None:
        for driver in registry.BUILTIN:
            self.assertIn(registry.distribution_type(driver), registry.DISTRIBUTIONS)
            self.assertTrue(set(registry.CAPABILITIES) <= set(driver["capabilities"])
                            <= {*registry.CAPABILITIES, *registry.EXTENDED_CAPABILITIES})
            self.assertIn(driver.get("lifecycle", "active"), registry.LIFECYCLES)


class BuiltinLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "drivers"
        self.folder.mkdir()

    def write(self, name: str, data) -> None:
        text = data if isinstance(data, str) else json.dumps(data)
        (self.folder / name).write_text(text, encoding="utf-8")

    def driver(self, **changes) -> dict:
        data = json.loads((registry.DRIVERS_DIR / "cursor.json").read_text(encoding="utf-8"))
        data.update({"id": "synthetic", "display": "Synthetic"})
        data.update(changes)
        return data

    def assertRejected(self, name: str, data, fragment: str) -> None:
        self.write(name, data)
        with self.assertRaises(registry.DriverDataError) as caught:
            registry.load_builtin(self.folder)
        self.assertIn(name, str(caught.exception))
        self.assertIn(fragment, str(caught.exception))

    def test_a_directory_argument_loads_synthetic_drivers(self) -> None:
        self.write("synthetic.json", self.driver(lifecycle="deprecated", schema_version=2))
        self.write("alpha.json", self.driver(id="alpha", display="Alpha", capabilities={
            "default_set": False, "read_only": True, "project_mcp_write": False}))
        loaded = registry.load_builtin(self.folder)
        self.assertEqual([item["id"] for item in loaded], ["synthetic", "alpha"])  # default set first, then the rest
        self.assertEqual(loaded[0]["lifecycle"], "deprecated")
        self.assertEqual(registry.BUILTIN_IDS, ("claude", "codex", "cursor", "agy", "claude-desktop", "gemini"))  # module state untouched

    def test_schema_version_one_and_two_are_accepted_and_nothing_else(self) -> None:
        for version in (1, 2):
            self.write("synthetic.json", self.driver(schema_version=version))
            self.assertEqual(len(registry.load_builtin(self.folder)), 1)
        for version in (0, 3, "2", True):
            self.assertRejected("synthetic.json", self.driver(schema_version=version), "schema_version")

    def test_store_status_vocabulary(self) -> None:
        for status in registry.STORE_STATUSES:
            data = self.driver()
            data["stores"]["skills"][0]["status"] = status
            self.write("synthetic.json", data)
            self.assertEqual(registry.load_builtin(self.folder)[0]["stores"]["skills"][0]["status"], status)
        data = self.driver()
        data["stores"]["skills"][0]["status"] = "guessed"
        self.assertRejected("synthetic.json", data, "status")

    def test_failures_are_loud_not_silent(self) -> None:
        with self.assertRaisesRegex(registry.DriverDataError, "missing"):
            registry.load_builtin(self.folder / "nowhere")
        with self.assertRaisesRegex(registry.DriverDataError, "no driver files"):
            registry.load_builtin(self.folder)
        self.assertRejected("broken.json", "{", "cannot read")
        (self.folder / "broken.json").unlink()
        self.assertRejected("synthetic.json", [], "top level")
        self.assertRejected("synthetic.json", self.driver(distribution="shell"), "distribution")
        self.assertRejected("synthetic.json", self.driver(lifecycle="retired"), "lifecycle")
        self.assertRejected("synthetic.json", self.driver(command="touch x"), "keys")
        self.assertRejected("synthetic.json", self.driver(id="all"), "id")
        self.assertRejected("synthetic.json", self.driver(capabilities={"default_set": True}), "capabilities")
        data = self.driver()
        data["stores"]["mcp"][0]["format"] = "yaml"
        self.assertRejected("synthetic.json", data, "format")
        data = self.driver()
        data["stores"]["skills"] = [dict(data["stores"]["skills"][0], write_target=True)] * 2
        self.assertRejected("synthetic.json", data, "write_target")
        (self.folder / "synthetic.json").unlink()
        self.write("other-name.json", self.driver())
        with self.assertRaisesRegex(registry.DriverDataError, "file name must be synthetic.json"):
            registry.load_builtin(self.folder)

    def test_a_broken_builtin_file_stops_the_import(self) -> None:
        import shutil
        copy = Path(self.folder).parent / "runtime"
        shutil.copytree(RUNTIME, copy, ignore=shutil.ignore_patterns("__pycache__"))
        (copy / "drivers" / "codex.json").write_text("{", encoding="utf-8")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(copy.parent)}
        result = subprocess.run([sys.executable, str(copy / "agenthelm.py"), "clients"], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DriverDataError: driver data codex.json: cannot read", result.stderr)


class DeclarationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.folder = self.home / ".config" / "agenthelm" / "clients.d"
        self.folder.mkdir(parents=True)

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "AGENTHELM_LANG": "en"}
        return subprocess.run([sys.executable, str(AGENTHELM), *args, "--home", str(self.home)], env=env,
                              capture_output=True, text=True)

    def clients(self) -> dict:
        result = self.run_cli("clients", "--output", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_valid_declaration_is_listed_but_not_installable(self) -> None:
        (self.folder / "example.json").write_text(json.dumps(valid()), encoding="utf-8")
        data = self.clients()
        self.assertEqual(data["warnings"], [])
        item = next(entry for entry in data["clients"] if entry["id"] == "example-ide")
        self.assertEqual(item["source"], "declared")
        self.assertFalse(item["install_supported"])
        text = self.run_cli("clients").stdout
        self.assertIn("declared; install is not supported in this version", text)

    def test_declared_id_is_not_a_usable_client_choice(self) -> None:
        (self.folder / "example.json").write_text(json.dumps(valid()), encoding="utf-8")
        result = self.run_cli("check", "--client", "example-ide", "--offline")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice: 'example-ide'", result.stderr)
        message = result.stderr.rsplit("invalid choice", 1)[1]  # argparse quotes the choices differently per Python
        for client in registry.default_ids():
            self.assertIn(client, message)
        self.assertNotIn("claude-desktop", message)

    def test_invalid_declarations_warn_and_are_ignored(self) -> None:
        bad = {
            "not-object.json": "[]",
            "broken.json": "{",
            "extra-field.json": json.dumps(valid(command="touch /tmp/should-not-exist")),
            "builtin-id.json": json.dumps(valid(id="claude")),
            "bad-id.json": json.dumps(valid(id="Bad Id")),
            "bad-distribution.json": json.dumps(valid(distribution="shell")),
            "path-escape.json": json.dumps(valid(detect={"dirs": ["~/../etc"]})),
            "exe-with-args.json": json.dumps(valid(detect={"executables": ["tool --flag"]})),
            "bad-flag.json": json.dumps(valid(capabilities={"run_hook": True})),
        }
        for name, content in bad.items():
            (self.folder / name).write_text(content, encoding="utf-8")
        data = self.clients()
        self.assertEqual({entry["file"] for entry in data["warnings"]}, set(bad))
        self.assertEqual([entry["id"] for entry in data["clients"]], list(registry.all_ids()))
        self.assertIn("Warning: ignoring declaration", self.run_cli("clients").stdout)

    def test_duplicate_declared_ids_keep_the_first(self) -> None:
        (self.folder / "a.json").write_text(json.dumps(valid()), encoding="utf-8")
        (self.folder / "b.json").write_text(json.dumps(valid()), encoding="utf-8")
        data = self.clients()
        self.assertEqual([entry["file"] for entry in data["warnings"]], ["b.json"])

    def test_declaration_content_is_never_executed(self) -> None:
        marker = self.home / "executed"
        script = self.home / "bin" / "example-ide"
        script.parent.mkdir()
        script.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        script.chmod(0o755)
        (self.folder / "cmd.json").write_text(json.dumps(valid(command=f"touch {marker}")), encoding="utf-8")
        (self.folder / "ok.json").write_text(json.dumps(valid(id="other-ide", detect={"executables": ["example-ide"]})),
                                              encoding="utf-8")
        env = {"PATH": f"{script.parent}:/usr/bin:/bin", "HOME": str(self.home)}
        result = subprocess.run([sys.executable, str(AGENTHELM), "clients", "--output", "json", "--home", str(self.home)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        data = json.loads(result.stdout)
        other = next(entry for entry in data["clients"] if entry["id"] == "other-ide")
        self.assertTrue(other["detected"])  # found on PATH, still never run

    def test_clients_command_writes_nothing(self) -> None:
        before = sorted(path.relative_to(self.home).as_posix() for path in self.home.rglob("*"))
        self.clients()
        self.assertEqual(sorted(path.relative_to(self.home).as_posix() for path in self.home.rglob("*")), before)

    def test_detection_reports_the_evidence(self) -> None:
        (self.home / ".cursor").mkdir()
        data = self.clients()
        cursor = next(entry for entry in data["clients"] if entry["id"] == "cursor")
        self.assertEqual(cursor["evidence"], {"kind": "directory", "name": "~/.cursor"})
        self.assertTrue(cursor["detected"])


if __name__ == "__main__":
    unittest.main()
