"""Batch D2 (#134): ``sync`` writes follow the client declarations.

Declaration validation (``sync``, ``mcp``, the new capability flags) with counter-examples, user-level declarations that
may not decide where anything is written, the ``json`` and ``toml`` MCP format adapters, and a synthetic client (an
unregistered random id) that ``sync`` writes for with nothing but a declaration and a fixture.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import tomllib
import unittest
import uuid
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "runtime"))

import agent_pack_clients as registry  # noqa: E402
import agent_pack_config as core  # noqa: E402
import agent_pack_mcp_json as jsonfmt  # noqa: E402
import agent_pack_mcp_toml as tomlfmt  # noqa: E402
from test_agent_pack_config import make_project, write_json  # noqa: E402

REMOTE = {"type": "streamable-http", "url": "https://mcp.example.com/mcp",
          "headers": {"Authorization": "Bearer ${EXAMPLE_MCP_TOKEN}", "X-Static": "1"}}
STDIO = {"type": "stdio", "command": "python3", "args": ["-m", "checker"], "cwd": "/work"}


def builtin(client: str) -> dict:
    return json.loads((registry.DRIVERS_DIR / f"{client}.json").read_text(encoding="utf-8"))


def load(data: dict) -> tuple:
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / f"{data['id']}.json").write_text(json.dumps(data), encoding="utf-8")
        return registry.load_builtin(Path(folder))


class DeclarationVocabulary(unittest.TestCase):
    def rejected(self, fragment: str, **edit) -> None:
        data = copy.deepcopy(builtin("codex"))
        for key, value in edit.items():
            target = data
            *path, last = key.split("__")
            for part in path:
                target = target[part]
            if value is None:
                target.pop(last)
            else:
                target[last] = value
        with self.assertRaises(registry.DriverDataError) as caught:
            registry.validate_builtin(data, "codex.json")
        self.assertIn(fragment, str(caught.exception))

    def test_the_three_clients_declare_how_sync_writes_for_them(self) -> None:
        self.assertEqual(registry.ids_with_sync("native-package"), ("claude",))
        self.assertEqual(registry.ids_with_sync("plugin-dir"), ("codex", "cursor"))
        self.assertEqual(registry.package_dir("cursor", Path("/h")), Path("/h/.cursor/plugins/local"))
        self.assertEqual(registry.package_dir("codex", Path("/h")), Path("/h/.agents/plugins/plugins"))
        self.assertEqual(registry.package_dir("claude", Path("/h")), Path("/h/.claude/agent-pack/plugins"))
        self.assertIsNone(registry.package_dir("claude-desktop", Path("/h")))
        self.assertEqual(registry.user_mcp_file("codex", Path("/h")), Path("/h/.codex/config.toml"))
        self.assertEqual(registry.user_mcp_file("claude", Path("/h")), Path("/h/.claude.json"))   # the #section is not a file
        self.assertEqual([registry.mcp_spec(c)["adapter"] for c in ("claude", "codex", "cursor")], ["json", "toml", "json"])
        self.assertEqual([registry.mcp_spec(c)["write"] for c in ("claude", "codex", "cursor")],
                         ["user-file", "package", "package"])

    def test_the_write_capabilities_are_declared_for_every_sync_client(self) -> None:
        flags = {c: {f: registry.supports(c, f) for f in ("user_mcp_write", "user_skill_write", "project_skill_write",
                                                          "project_mcp_write")} for c in ("claude", "codex", "cursor")}
        self.assertEqual(flags["claude"], dict.fromkeys(flags["claude"], True))
        for client in ("codex", "cursor"):
            self.assertEqual(flags[client], {"user_mcp_write": True, "user_skill_write": True,
                                             "project_skill_write": False, "project_mcp_write": False})
        for flag in ("user_mcp_write", "user_skill_write", "project_skill_write"):
            self.assertIn(flag, registry.EXTENDED_CAPABILITIES)

    def test_option_defaults_fill_in_what_a_driver_leaves_out(self) -> None:
        self.assertEqual(registry.mcp_options("cursor")["remote_url_key"], "url")
        self.assertEqual(registry.mcp_options("cursor")["env_interpolation"], "declared-variables")
        self.assertEqual(registry.mcp_options("claude")["type_names"], {"streamable-http": "http"})
        self.assertEqual(registry.mcp_options("not-registered"), registry.MCP_OPTION_DEFAULTS)

    def test_counter_examples_for_sync(self) -> None:
        self.rejected("sync.write must be one of", sync__write="shell")
        self.rejected("sync needs write and package_dir", sync__package_dir=None)
        self.rejected("sync.package_dir needs path", sync__package_dir="relative/dir")
        self.rejected("sync.package_dir needs path", sync__package_dir="~/../escape")
        self.rejected("sync needs write and package_dir", sync__command="touch x")
        self.rejected("sync.companions needs manifest", sync__companions={"mcp": ".mcp.json"})
        self.rejected("relative file path without ..", sync__companions={"manifest": "../x.json"})
        self.rejected("relative file path without ..", sync__companions={"manifest": "/etc/x.json"})
        self.rejected("remote_mcp needs mcp", sync__companions={"manifest": "m.json", "remote_mcp": "r.json"})
        self.rejected("placeholder_prefix", sync__companions={"manifest": "m.json", "placeholder_prefix": "lower"})
        self.rejected("sync.marketplace needs exactly path and source_dir", sync__marketplace={"path": "~/m.json"})
        self.rejected("source_dir must be ./relative", sync__marketplace={"path": "~/m.json", "source_dir": "/abs"})
        self.rejected("sync.cleanup holds", sync__cleanup={"anything": "~/x"})
        self.rejected("sync needs mcp", mcp=None)

    def test_plugin_dir_only_blocks_are_refused_on_a_native_package(self) -> None:
        data = builtin("claude")
        data["sync"]["marketplace"] = {"path": "~/m.json", "source_dir": "./x"}
        with self.assertRaises(registry.DriverDataError) as caught:
            registry.validate_builtin(data, "claude.json")
        self.assertIn("applies to plugin-dir writes only", str(caught.exception))

    def test_counter_examples_for_mcp(self) -> None:
        self.rejected("mcp.adapter must be one of", mcp__adapter="yaml")
        self.rejected("mcp.write must be one of", mcp__write="shell-out")
        self.rejected("mcp.native_remote must be a boolean", mcp__native_remote="yes")
        self.rejected("mcp needs adapter and write", mcp__command="x")
        self.rejected("must be the format of the first user-level mcp store", mcp__adapter="json")
        self.rejected("mcp.manual_remove is a list of plain arguments", mcp__manual_remove=["rm", "-rf", "$(x)"])
        self.rejected("mcp.manual_remove is a list of plain arguments", mcp__manual_remove=["tool", "{path}"])

    def test_counter_examples_for_mcp_options(self) -> None:
        for options, fragment in (
                ({"unknown": 1}, "mcp.options holds only"),
                ({"remote_url_key": "bad key"}, "remote_url_key"),
                ({"remote_headers": "magic"}, "remote_headers must be one of"),
                ({"env_interpolation": "everything"}, "env_interpolation must be one of"),
                ({"type_field_required": "yes"}, "type_field_required must be a boolean"),
                ({"type_names": {"a": 1}}, "type_names must map"),
                ({"remote_headers": "split-env"}, "header_fields is required"),
                ({"remote_headers": "inline", "header_fields": {"plain": "p", "env": "e", "bearer": "b"}}, "header_fields is required"),
                ({"remote_headers": "split-env", "header_fields": {"plain": "p"}}, "header_fields"),
                ({"remote_headers": "split-env", "header_fields": {"plain": "p", "env": "e", "bearer": "bad key"}}, "plain key names")):
            with self.subTest(options=options):
                self.rejected(fragment, mcp__options=options)

    def test_a_write_capability_needs_the_declaration_that_carries_it_out(self) -> None:
        self.rejected("capabilities.user_mcp_write needs a sync declaration", sync=None, mcp=None, capabilities__user_mcp_write=True)
        self.rejected("capabilities.project_skill_write needs sync.write native-package", capabilities__project_skill_write=True)
        self.rejected("capabilities.project_mcp_write needs sync.write native-package", capabilities__project_mcp_write=True)

    def test_new_flags_and_blocks_reach_a_synthetic_directory_load(self) -> None:
        data = builtin("cursor")
        data.update(id="zz-synthetic", display="Synthetic")
        loaded = load(data)
        self.assertEqual(loaded[0]["sync"]["write"], "plugin-dir")


class UserLevelDeclarations(unittest.TestCase):
    """D-R1: a file in ``clients.d`` never decides which command runs or where anything is written."""

    VALID = {"schema_version": 1, "id": "zz-user", "display": "User Declared", "detect": {"executables": ["zzuser"]},
             "distribution": "none", "stores": {"skills": [{"scope": "user", "path": "~/.zz/skills", "format": "dir"}]},
             "capabilities": {"default_set": False}}

    def test_a_plain_declaration_stays_valid(self) -> None:
        self.assertIsNone(registry.validate_declaration(self.VALID))

    def test_write_side_blocks_and_flags_are_refused(self) -> None:
        sync = {"write": "plugin-dir", "package_dir": "~/.zz/plugins"}
        mcp = {"adapter": "json", "write": "package"}
        for edit, why in (({"sync": sync}, "sync"), ({"mcp": mcp}, "mcp"),
                          ({"capabilities": {"user_mcp_write": True}}, "capabilities"),
                          ({"capabilities": {"project_skill_write": True}}, "capabilities"),
                          ({"capabilities": {"user_skill_write": True}}, "capabilities"),
                          ({"precedence": []}, "precedence"), ({"runtime_cache": []}, "runtime_cache")):
            with self.subTest(why=why, edit=edit):
                self.assertIsNotNone(registry.validate_declaration({**copy.deepcopy(self.VALID), **edit}))

    def test_write_target_and_every_other_store_attribute_are_refused(self) -> None:
        for extra in ({"write_target": True}, {"root_key": "mcpServers"}, {"managed_by": "agent-pack-state"},
                      {"trust_required": True}, {"status": "observed"}, {"command": "x"}):
            data = copy.deepcopy(self.VALID)
            data["stores"]["skills"][0].update(extra)
            with self.subTest(extra=extra):
                self.assertIsNotNone(registry.validate_declaration(data))

    def test_a_steps_or_manual_remove_command_is_refused_anywhere_it_is_nested(self) -> None:
        data = copy.deepcopy(self.VALID)
        data["distribution"] = {"type": "marketplace-cli", "steps": {"install": ["{cli}", "x"]}}
        self.assertIn("steps", registry.validate_declaration(data))

    def test_a_declared_client_never_enters_the_sync_set(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            target = home / ".config/agenthelm/clients.d"
            target.mkdir(parents=True)
            (target / "user.json").write_text(json.dumps({**self.VALID, "sync": {"write": "plugin-dir"}}), encoding="utf-8")
            declared, warnings = registry.load_declarations(home, {}.get)
            self.assertEqual((declared, [item["file"] for item in warnings]), ([], ["user.json"]))
            self.assertNotIn("zz-user", registry.default_ids())


class JsonAdapter(unittest.TestCase):
    OPTIONS = registry.MCP_OPTION_DEFAULTS

    def options(self, **changes) -> dict:
        return {**copy.deepcopy(self.OPTIONS), **changes}

    def test_env_interpolation_values(self) -> None:
        for value, expected in (("none", "Bearer ${T}"), ("plugin-root-only", "Bearer ${T}"), ("shell-env", "Bearer ${T}"),
                                ("declared-variables", "Bearer ${env:T}")):
            with self.subTest(env_interpolation=value):
                entry = jsonfmt.package_entry({"type": "streamable-http", "url": "u", "headers": {"Authorization": "Bearer ${T}"}},
                                              self.options(env_interpolation=value))
                self.assertEqual(entry["headers"]["Authorization"], expected)

    def test_a_stdio_entry_and_a_remote_entry_without_headers_pass_through_unchanged(self) -> None:
        options = self.options(env_interpolation="declared-variables")
        for config in ({"type": "stdio", "command": "x", "env": {"A": "${A}"}}, {"type": "sse", "url": "u"}):
            self.assertEqual(jsonfmt.package_entry(config, options), config)

    def test_headers_option_values(self) -> None:
        config = {"type": "sse", "url": "u", "headers": {"A": "${X}"}}
        self.assertEqual(jsonfmt.package_entry(config, self.options(remote_headers="split-env", env_interpolation="declared-variables")), config)
        with self.assertRaises(core.AgentPackError):
            jsonfmt.package_entry(config, self.options(remote_headers="none"))

    def test_package_entry_does_not_touch_its_input(self) -> None:
        config = {"type": "sse", "url": "u", "headers": {"A": "${X}"}}
        before = copy.deepcopy(config)
        jsonfmt.package_entry(config, self.options(env_interpolation="declared-variables"))
        self.assertEqual(config, before)

    def test_native_entry_projects_onto_the_clients_schema(self) -> None:
        stdio = {"type": "stdio", "command": "${PLUGIN_ROOT}/run", "args": ["--data", "${PLUGIN_DATA}"], "cwd": "/work"}
        entry = jsonfmt.native_entry(stdio, Path("/plugin"), Path("/data"), self.options(type_names={"streamable-http": "http"}))
        self.assertEqual(entry, {"type": "stdio", "command": "/plugin/run", "args": ["--data", "/data"]})
        remote = jsonfmt.native_entry({"type": "streamable-http", "url": "u"}, Path("/p"), Path("/d"),
                                      self.options(type_names={"streamable-http": "http"}))
        self.assertEqual(remote, {"type": "http", "url": "u"})
        self.assertEqual(jsonfmt.native_entry({"type": "sse", "url": "u"}, Path("/p"), Path("/d"),
                                              self.options(type_names={"streamable-http": "http"}))["type"], "sse")

    def test_native_entry_options_url_key_and_type_field(self) -> None:
        entry = jsonfmt.native_entry({"type": "streamable-http", "url": "u"}, Path("/p"), Path("/d"),
                                     self.options(remote_url_key="serverUrl", type_field_required=False))
        self.assertEqual(entry, {"serverUrl": "u"})

    def test_companion_entry_keeps_a_type_only_when_it_is_mapped(self) -> None:
        names = {"streamable-http": "http", "sse": "sse"}
        self.assertEqual(jsonfmt.companion_entry({"type": "stdio", "command": "${PLUGIN_ROOT}/x"}, "CLAUDE_", names),
                         {"command": "${CLAUDE_PLUGIN_ROOT}/x"})
        self.assertEqual(jsonfmt.companion_entry({"type": "streamable-http", "url": "u", "headers": {"A": "${PLUGIN_DATA}"}},
                                                 "CLAUDE_", names),
                         {"url": "u", "headers": {"A": "${CLAUDE_PLUGIN_DATA}"}, "type": "http"})
        self.assertEqual(jsonfmt.prefixed_placeholders(["${PLUGIN_ROOT}", {"k": "${OTHER}"}], "P_"), ["${P_PLUGIN_ROOT}", {"k": "${OTHER}"}])

    def test_servers_and_with_servers_keep_every_other_key(self) -> None:
        data = {"other": 1, "mcpServers": {"a": {"x": 1}}, "projects": {"/p": {}}}
        updated = jsonfmt.with_servers(data, "mcpServers", {"b": {"y": 2}, "a": {"x": 3}}, {"gone"})
        self.assertEqual(updated, {"other": 1, "mcpServers": {"a": {"x": 3}, "b": {"y": 2}}, "projects": {"/p": {}}})
        self.assertEqual(data["mcpServers"], {"a": {"x": 1}})
        self.assertEqual(jsonfmt.with_servers(data, "mcpServers", {}, {"a"})["mcpServers"], {})
        self.assertEqual(jsonfmt.with_servers({}, "servers", {"a": {}}), {"servers": {"a": {}}})   # another root key

    def test_a_root_key_that_is_not_an_object_is_an_error_and_a_missing_one_is_empty(self) -> None:
        self.assertEqual(jsonfmt.servers({}, "mcpServers"), {})
        with self.assertRaises(core.AgentPackError):
            jsonfmt.servers({"mcpServers": []}, "mcpServers")

    def test_an_empty_json_file_is_still_a_corrupt_config_on_the_write_side(self) -> None:
        """Unchanged on purpose (batch B tolerates 0 bytes on the read side only; the write side is a later change)."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mcp.json"
            path.write_text("", encoding="utf-8")
            with self.assertRaises(core.AgentPackError):
                core._read_json(path)


class TomlAdapter(unittest.TestCase):
    OPTIONS = registry.mcp_options("codex")

    def test_headers_split_into_the_declared_fields(self) -> None:
        config = {"url": "https://x", "headers": {"Authorization": "Bearer ${TOK}", "X-Key": "${KEY}", "X-Plain": "1"}}
        self.assertEqual(tomlfmt.native_entry(config, self.OPTIONS, "Label"),
                         {"url": "https://x", "bearer_token_env_var": "TOK", "http_headers": {"X-Plain": "1"},
                          "env_http_headers": {"X-Key": "KEY"}})

    def test_the_field_names_come_from_the_options(self) -> None:
        options = {**self.OPTIONS, "remote_url_key": "httpUrl", "header_fields": {"plain": "p", "env": "e", "bearer": "b"}}
        entry = tomlfmt.native_entry({"url": "u", "headers": {"Authorization": "bearer ${T}", "K": "${V}", "Z": "1"}}, options, "L")
        self.assertEqual(entry, {"httpUrl": "u", "b": "T", "p": {"Z": "1"}, "e": {"K": "V"}})

    def test_a_header_with_more_than_one_reference_is_refused_by_name(self) -> None:
        with self.assertRaises(core.AgentPackError) as caught:
            tomlfmt.native_entry({"url": "u", "headers": {"X": "a ${A} b ${B}"}}, self.OPTIONS, "Label")
        self.assertEqual(str(caught.exception), "Label header requires a single environment reference: X")

    def test_render_an_empty_file_gets_just_the_tables(self) -> None:
        text = tomlfmt.render("", {"a": {"url": "u", "http_headers": {"K": "v"}}}, set(), "mcp_servers")
        self.assertEqual(text, '\n[mcp_servers."a"]\nurl = "u"\nhttp_headers = { "K" = "v" }\n')
        self.assertEqual(tomllib.loads(text), {"mcp_servers": {"a": {"url": "u", "http_headers": {"K": "v"}}}})

    def test_render_keeps_other_keys_comments_and_tables_and_replaces_only_the_owned_one(self) -> None:
        original = ('# top\nmodel = "m"\n\n[mcp_servers.keep]\ncommand = "k"  # trailing\n\n'
                    '[mcp_servers.a]\nurl = "old"\n\n[other]\nx = 1\n')
        text = tomlfmt.render(original, {"a": {"url": "new"}}, set(), "mcp_servers")
        self.assertIn('# top\nmodel = "m"\n', text)
        self.assertIn('command = "k"  # trailing\n', text)
        self.assertIn("[other]\nx = 1\n", text)
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["mcp_servers"]["a"], {"url": "new"})
        self.assertEqual(parsed["mcp_servers"]["keep"], {"command": "k"})
        self.assertNotIn('url = "old"', text)

    def test_render_removes_a_named_table_and_leaves_the_rest(self) -> None:
        text = tomlfmt.render('[mcp_servers.a]\nurl = "u"\n\n[mcp_servers.b]\nurl = "v"\n', {}, {"a"}, "mcp_servers")
        self.assertEqual(tomllib.loads(text), {"mcp_servers": {"b": {"url": "v"}}})

    def test_render_follows_the_root_key_and_quotes_awkward_names(self) -> None:
        text = tomlfmt.render("", {'we"ird name': {"url": "u"}}, set(), "servers")
        self.assertEqual(tomllib.loads(text), {"servers": {'we"ird name': {"url": "u"}}})

    def test_only_named_tables_count_as_sections_and_a_sub_table_goes_with_its_server(self) -> None:
        original = 'mcp_servers = { a = { url = "x" } }\n'
        self.assertEqual(tomlfmt._section_name("[mcp_servers.a]", "mcp_servers"), "a")
        self.assertEqual(tomlfmt._section_name("[mcp_servers.a.env]", "mcp_servers"), "a")
        self.assertIsNone(tomlfmt._section_name("[other.a]", "mcp_servers"))
        self.assertIsNone(tomlfmt._section_name(original, "mcp_servers"))

    def test_the_toml_string_escapes_what_toml_forbids(self) -> None:
        self.assertEqual(tomlfmt._toml_string("a\x7fb"), '"a\\u007fb"')
        self.assertEqual(tomllib.loads("k = " + tomlfmt._toml_string("😀\n\"\\"))["k"], "😀\n\"\\")


class SyntheticClient(unittest.TestCase):
    """An unregistered, random id: a declaration and a fixture are all ``sync`` needs; no code changes."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.sid = "zq" + uuid.uuid4().hex[:8]
        self.assertNotIn(self.sid, registry.all_ids())
        self.driver = {
            "schema_version": 2, "id": self.sid, "display": "Synthetic Client",
            "detect": {"executables": [], "dirs": [{"path": f"~/.{self.sid}"}]},
            "distribution": {"type": "directory-dropin", "source": "bundle-dir", "interactive": "none",
                             "installed_state": "dropin-dir", "config_dir": {"path": f"~/.{self.sid}"}},
            "stores": {"skills": [{"scope": "user", "path": f"~/.{self.sid}/skills", "format": "dir", "write_target": True}],
                       "mcp": [{"scope": "user", "path": f"~/.{self.sid}/mcp.json", "format": "json", "root_key": "mcpServers"}]},
            "capabilities": {"default_set": True, "read_only": False, "project_mcp_write": False, "user_mcp_write": True,
                             "user_skill_write": True, "project_skill_write": False},
            "sync": {"write": "plugin-dir", "package_dir": f"~/.{self.sid}/plugins"},
            "mcp": {"adapter": "json", "write": "package", "options": {"env_interpolation": "declared-variables"}}}
        loaded = load(self.driver)
        patches = (mock.patch.object(registry, "BUILTIN", registry.BUILTIN + loaded),
                   mock.patch.object(registry, "BUILTIN_IDS", registry.BUILTIN_IDS + (self.sid,)),
                   mock.patch.dict(os.environ, {"EXAMPLE_MCP_TOKEN": "t-dummy"}))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.project = make_project(self.root / "proj", "proj")
        write_json(self.project / ".agents/mcp.json", {"$schema": core.AGENT_PLUGIN_MCP_SCHEMA, "mcpServers": {
            "example": REMOTE, "local-check": {"type": "stdio", "command": "python3", "args": ["-m", "checker"]}}})
        self.package = self.home / f".{self.sid}/plugins/proj"

    def sync(self, apply: bool = False, **kwargs):
        return core.sync_project(self.project, self.sid, home=self.home, apply=apply,
                                 recovery_root=self.root / "recovery", **kwargs)

    def test_the_driver_is_part_of_the_registry_the_code_reads(self) -> None:
        self.assertIn(self.sid, registry.default_ids())
        self.assertEqual(registry.package_dir(self.sid, self.home), self.package.parent)
        self.assertEqual(core.package_parent(self.sid, self.home), self.package.parent)

    def test_preview_writes_nothing_and_names_the_declared_destination(self) -> None:
        result = self.sync()
        self.assertEqual(result["operations"], [{"action": "install-plugin", "client": self.sid, "name": "proj",
                                                 "to": str(self.package)}])
        self.assertFalse((self.home / f".{self.sid}").exists())

    def test_apply_writes_the_skills_and_the_mcp_entries_and_stays_in_sync(self) -> None:
        self.assertTrue(self.sync(apply=True)["applied"])
        self.assertTrue((self.package / "skills/sample-skill/SKILL.md").is_file())
        servers = json.loads((self.package / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(sorted(servers), ["example", "local-check"])
        self.assertEqual(servers["example"]["headers"]["Authorization"], "Bearer ${env:EXAMPLE_MCP_TOKEN}")   # declared-variables
        self.assertEqual(servers["local-check"]["command"], "python3")
        self.assertEqual(json.loads((self.package / "plugin.json").read_text(encoding="utf-8"))["name"], "proj")
        self.assertEqual(self.sync(apply=True)["operations"][0]["action"], "keep-plugin")
        self.assertTrue(core.client_drift(self.project, self.sid, self.home)["in_sync"])
        self.assertTrue(core.doctor(self.project, [self.sid], self.home)["ok"])
        self.assertFalse(list(self.package.rglob(".codex-plugin")))   # no companion files: none declared

    def test_the_write_capabilities_limit_what_the_package_carries(self) -> None:
        for flag, kept, dropped in (("user_skill_write", "mcp", "skills"), ("user_mcp_write", "skills", "mcp")):
            with self.subTest(flag=flag):
                patched = copy.deepcopy(self.driver)
                patched["capabilities"][flag] = False
                with mock.patch.object(registry, "BUILTIN", registry.BUILTIN[:-1] + load(patched)):
                    self.sync(apply=True)
                    servers = json.loads((self.package / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
                    skills = [path.name for path in (self.package / "skills").iterdir()]
                    self.assertEqual((bool(skills), bool(servers)), (kept == "skills", kept == "mcp"))
                    shutil.rmtree(self.package)

    def test_a_changed_package_is_replaced_and_the_original_is_recoverable(self) -> None:
        self.sync(apply=True)
        (self.package / "junk.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.sync()["operations"][0]["action"], "replace-plugin")
        result = self.sync(apply=True)
        self.assertFalse((self.package / "junk.txt").exists())
        self.assertTrue((Path(result["recovery"]) / "replaced-plugin" / "proj" / "junk.txt").is_file())

    def test_the_client_inventory_reads_the_package_it_wrote(self) -> None:
        self.sync(apply=True)
        found = core.inventory_client(self.sid, self.home)
        self.assertIn("sample-skill", [item.name for item in found.skills])

    def test_the_command_line_takes_the_new_client_without_a_code_change(self) -> None:
        for apply in (False, True):
            argv = ["--output", "json", "sync", "--project", str(self.project), "--client", self.sid, "--home", str(self.home)]
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = core.main([*argv, *(["--apply"] if apply else [])])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buffer.getvalue())["applied"], apply)
        self.assertTrue((self.package / "mcp.json").is_file())

    def test_flags_that_need_a_native_package_are_refused_for_it(self) -> None:
        with self.assertRaises(core.AgentPackError) as caught:
            self.sync(scope="user")
        self.assertEqual(str(caught.exception), f"--scope user|project is supported for the claude client only: "
                                                f"{self.sid} has no project-level write yet")


class UndeclaredClients(unittest.TestCase):
    def test_a_client_without_a_sync_declaration_is_named_in_the_error(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            project = make_project(Path(folder) / "proj", "proj")
            for client in ("claude-desktop", "not-registered"):
                with self.subTest(client=client), self.assertRaises(core.AgentPackError) as caught:
                    core.sync_project(project, client, home=Path(folder) / "home")
                self.assertEqual(str(caught.exception), f"sync adapter not implemented for client: {client}")


if __name__ == "__main__":
    unittest.main()
