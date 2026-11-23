"""Read side of the client registry (#134 batch B): precedence evaluator, format-named read adapters, ``detect.cli``
probes, the empty-file rule and shared stores. Synthetic drivers stand in for new clients: no code names them."""
from __future__ import annotations

import copy
import itertools
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
sys.path.insert(0, str(RUNTIME))

import agent_pack_clients as registry  # noqa: E402
import agent_pack_config as config  # noqa: E402
import agent_pack_inventory as inv  # noqa: E402
import agent_pack_probe as probe  # noqa: E402
import agent_pack_store_read as store_read  # noqa: E402

URL_MCP = "https://code.claude.com/docs/en/mcp"
URL_SKILLS = "https://code.claude.com/docs/en/skills"
URL_CODEX_SKILLS = "https://learn.chatgpt.com/docs/build-skills"
URL_CODEX_MCP = "https://learn.chatgpt.com/docs/extend/mcp?surface=cli"


def reference_skill_effective(client: str, group: list[dict]) -> dict:
    """The conclusions as the code before #134 batch B wrote them (kept verbatim as the reference)."""
    scopes = {item["scope"] for item in group}
    if client == "claude" and scopes == {"user", "project"}:
        return {"store": next(item["store"] for item in group if item["scope"] == "user"), "rule": "personal-over-project",
                "evidence": "file", "source_url": URL_SKILLS}
    if client == "codex":
        return {"store": None, "rule": "both-load", "evidence": "file", "source_url": URL_CODEX_SKILLS}
    return {"store": None, "rule": "precedence-undocumented" if client == "cursor" else "unknown",
            "evidence": "unknown", "source_url": None}


def reference_mcp_effective(client: str, group: list[dict]) -> dict:
    stores = {item["scope"]: item["store"] for item in group}
    if client == "claude":
        if "local" in stores:
            return {"store": stores["local"], "rule": "local-over-project-over-user", "evidence": "file",
                    "source_url": URL_MCP}
        return {"store": None, "rule": "project-approval-unknown", "evidence": "unknown", "source_url": URL_MCP}
    if client == "codex":
        return {"store": None, "rule": "project-trust-unknown", "evidence": "unknown", "source_url": URL_CODEX_MCP}
    return {"store": None, "rule": "precedence-undocumented", "evidence": "unknown", "source_url": None}


def groups() -> list[list[dict]]:
    """Every non-empty combination of one store per scope, plus a group of two stores in one scope."""
    result = []
    scopes = ("user", "local", "project")
    for size in (1, 2, 3):
        for chosen in itertools.combinations(scopes, size):
            result.append([{"scope": scope, "store": f"store-{scope}"} for scope in chosen])
    result.append([{"scope": "user", "store": "a"}, {"scope": "user", "store": "b"}])
    return result


class PrecedenceTests(unittest.TestCase):
    def test_declared_conclusions_equal_the_previous_functions_for_every_registered_client(self) -> None:
        for client in registry.all_ids():
            for group in groups():
                self.assertEqual(inv._skill_effective(client, group), reference_skill_effective(client, group), (client, group))
                self.assertEqual(inv._mcp_effective(client, group), reference_mcp_effective(client, group), (client, group))

    def test_every_conclusion_the_old_code_could_reach_is_reached(self) -> None:
        reached = {(client, kind, inv._effective(client, kind, group)["rule"])
                   for client in registry.all_ids() for kind in ("skills", "mcp") for group in groups()}
        self.assertTrue({("claude", "skills", "personal-over-project"), ("claude", "skills", "unknown"),
                         ("claude", "mcp", "local-over-project-over-user"), ("claude", "mcp", "project-approval-unknown"),
                         ("codex", "skills", "both-load"), ("codex", "mcp", "project-trust-unknown"),
                         ("cursor", "skills", "precedence-undocumented"), ("cursor", "mcp", "precedence-undocumented"),
                         ("claude-desktop", "skills", "unknown"), ("claude-desktop", "mcp", "precedence-undocumented")}
                        <= reached, reached)

    def test_an_undeclared_client_is_unknown(self) -> None:
        for kind in ("skills", "mcp"):
            self.assertEqual(inv._effective("not-registered", kind, groups()[0]),
                             {"store": None, "rule": "unknown", "evidence": "unknown", "source_url": None})

    def test_the_evaluator_runs_declared_conditions_in_order(self) -> None:
        entries = (
            {"kind": "mcp", "rule": "local-over-project-over-user", "evidence": "file", "source_url": "https://example.test/a",
             "when": {"scopes": ["local"], "match": "includes"}, "winner": "local"},
            {"kind": "mcp", "rule": "personal-over-project", "evidence": "cli", "source_url": None,
             "when": {"scopes": ["user", "project"], "match": "exact"}, "winner": "user"},
            {"kind": "mcp", "rule": "both-load", "evidence": "file", "source_url": None},
        )
        with mock.patch.object(inv.client_registry, "precedence", lambda client, kind: entries):
            both = lambda *scopes: [{"scope": s, "store": f"s-{s}"} for s in scopes]  # noqa: E731
            self.assertEqual(inv._effective("x", "mcp", both("user", "local", "project"))["store"], "s-local")
            exact = inv._effective("x", "mcp", both("user", "project"))
            self.assertEqual((exact["rule"], exact["store"], exact["evidence"]), ("personal-over-project", "s-user", "cli"))
            fallthrough = inv._effective("x", "mcp", both("project"))  # matches neither condition: the last entry applies
            self.assertEqual((fallthrough["rule"], fallthrough["store"]), ("both-load", None))

    def test_annotation_uses_the_declared_winner(self) -> None:
        group = {"client": "claude", "kind": "same-name", "names": ["x"], "stores": ["~/.claude.json", "~/.claude.json#projects"],
                 "relation": "different", "effective": inv._mcp_effective("claude", [
                     {"scope": "user", "store": "~/.claude.json"}, {"scope": "local", "store": "~/.claude.json#projects"}])}
        items = [{"client": "claude", "name": "x", "scope": "user", "store": "~/.claude.json", "precedence": "unique", "shadowed_by": None},
                 {"client": "claude", "name": "x", "scope": "local", "store": "~/.claude.json#projects", "precedence": "unique", "shadowed_by": None}]
        inv._annotate([], items, {"skills": [], "mcps": [group]})
        self.assertEqual([i["precedence"] for i in items], ["shadowed", "winner"])
        self.assertEqual(items[0]["shadowed_by"]["source_url"], URL_MCP)

    def test_documentation_links_live_in_the_declarations(self) -> None:
        self.assertFalse([name for name in dir(inv) if name.startswith(("DOC_CLAUDE", "DOC_CODEX"))])
        urls = {entry["source_url"] for client in registry.default_ids() for entry in registry.get(client).get("precedence", [])}
        self.assertTrue({URL_MCP, URL_SKILLS, URL_CODEX_SKILLS, URL_CODEX_MCP, None} <= urls)


class DeclarationValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "drivers"
        self.folder.mkdir()

    def driver(self, **changes) -> dict:
        data = json.loads((registry.DRIVERS_DIR / "claude.json").read_text(encoding="utf-8"))
        data.update({"id": "synthetic", "display": "Synthetic"})
        data.update(changes)
        return data

    def rejected(self, data: dict, fragment: str) -> None:
        (self.folder / "synthetic.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(registry.DriverDataError) as caught:
            registry.load_builtin(self.folder)
        self.assertIn(fragment, str(caught.exception))

    def test_precedence_is_validated(self) -> None:
        entry = {"kind": "mcp", "rule": "both-load", "evidence": "file", "source_url": None}
        for change, fragment in (
                ({"kind": "hooks"}, "kind"), ({"rule": "best"}, "rule"), ({"evidence": "guess"}, "evidence"),
                ({"source_url": "http://insecure.test"}, "source_url"), ({"winner": "user"}, "winner needs a when"),
                ({"when": {"scopes": ["user"], "match": "some"}}, "when"), ({"when": {"scopes": [], "match": "exact"}}, "when"),
                ({"when": {"scopes": ["user"], "match": "exact"}, "winner": "local"}, "winner must be one of"),
                ({"extra": 1}, "needs kind")):
            self.rejected(self.driver(precedence=[{**entry, **change}]), fragment)
        self.rejected(self.driver(precedence=[entry, dict(entry, rule="unknown")]), "after an unconditional mcp")
        self.rejected(self.driver(precedence={}), "must be a list")
        (self.folder / "synthetic.json").write_text(json.dumps(self.driver(precedence=[entry])), encoding="utf-8")
        self.assertEqual(registry.load_builtin(self.folder)[0]["precedence"], [entry])

    def test_store_attributes_are_validated(self) -> None:
        for mutate, fragment in (
                (lambda d: d["stores"]["mcp"][0].pop("root_key"), "root_key"),
                (lambda d: d["stores"]["skills"][0].update(root_key="x"), "root_key"),
                (lambda d: d["stores"]["mcp"][0].update(managed_by="somebody"), "managed_by"),
                (lambda d: d["stores"]["skills"][0].update(trust_required=True), "trust_required"),
                (lambda d: d["stores"]["mcp"][0].update(trust_required="yes"), "trust_required"),
                (lambda d: d["stores"]["skills"][0].update(path="~/x#y"), "#section")):
            data = self.driver()
            mutate(data)
            self.rejected(data, fragment)

    def test_cli_probe_is_validated(self) -> None:
        good = {"argv": ["plugin", "list", "--json"], "stream": "stdout", "parse": "json"}
        for bad, fragment in (
                ({**good, "argv": []}, "argv"), ({**good, "argv": ["a b"]}, "argv"), ({**good, "argv": ["$(x)"]}, "argv"),
                ({**good, "argv": ["a;b"]}, "argv"), ({**good, "argv": "plugin list"}, "argv"),
                ({**good, "stream": "stdin"}, "stream"), ({**good, "parse": "yaml"}, "parse"),
                ({"argv": ["x"], "stream": "stdout"}, "detect.cli")):
            data = self.driver()
            data["detect"]["cli"] = bad
            self.rejected(data, fragment)
        data = self.driver()
        data["detect"] = {"dirs": [{"path": "~/.x"}], "cli": good}
        self.rejected(data, "detect.executables")
        data = self.driver()
        data["detect"]["cli"] = good
        (self.folder / "synthetic.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(registry.load_builtin(self.folder)[0]["detect"]["cli"], good)

    def test_no_builtin_driver_declares_a_probe_in_this_version(self) -> None:
        self.assertTrue(all("cli" not in driver["detect"] for driver in registry.BUILTIN))

    def test_user_declarations_cannot_carry_a_probe_or_precedence(self) -> None:
        base = {"schema_version": 1, "id": "example-ide", "display": "Example", "detect": {"executables": ["example"]},
                "distribution": "none", "stores": {}, "capabilities": {}}
        self.assertIsNone(registry.validate_declaration(base))
        probing = copy.deepcopy(base)
        probing["detect"]["cli"] = {"argv": ["list"], "stream": "stdout", "parse": "text"}
        self.assertIsNotNone(registry.validate_declaration(probing))
        self.assertIsNotNone(registry.validate_declaration({**base, "precedence": []}))


STUB_HEADER = "#!/bin/sh\n"


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def stub(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(STUB_HEADER + body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    JSON_OUT = {"argv": ["plugin", "list", "--json"], "stream": "stdout", "parse": "json"}

    def test_stdout_json(self) -> None:
        exe = self.stub("c1", 'printf \'{"plugins":["a"],"args":"%s"}\' "$*"')
        result = probe.run_probe(self.JSON_OUT, exe)
        self.assertEqual(result, {"ok": True, "evidence": "cli", "stream": "stdout", "parse": "json",
                                  "data": {"plugins": ["a"], "args": "plugin list --json"}})

    def test_stderr_text(self) -> None:
        exe = self.stub("c2", 'echo "usage: tool [options]" >&2')
        result = probe.run_probe({"argv": ["--help"], "stream": "stderr", "parse": "text"}, exe)
        self.assertEqual((result["ok"], result["data"], result["stream"]), (True, "usage: tool [options]\n", "stderr"))
        wrong_stream = probe.run_probe({"argv": ["--help"], "stream": "stdout", "parse": "text"}, exe)
        self.assertEqual(wrong_stream, {"ok": False, "evidence": "unknown", "reason": "unexpected-format"})

    def test_stderr_json_and_stdout_text(self) -> None:
        self.assertEqual(probe.run_probe({"argv": ["x"], "stream": "stderr", "parse": "json"},
                                         self.stub("c3", "echo '[1,2]' >&2"))["data"], [1, 2])
        self.assertEqual(probe.run_probe({"argv": ["x"], "stream": "stdout", "parse": "text"},
                                         self.stub("c4", "echo v1.2.3"))["data"], "v1.2.3\n")

    def test_output_that_is_not_the_expected_format(self) -> None:
        for name, body, parse in (("bad-json", "echo not-json", "json"), ("empty-json", "true", "json"),
                                  ("empty-text", "true", "text"), ("binary", "printf '\\377\\376'", "text")):
            result = probe.run_probe({"argv": ["x"], "stream": "stdout", "parse": parse}, self.stub(name, body))
            self.assertEqual(result, {"ok": False, "evidence": "unknown", "reason": "unexpected-format"}, name)

    def test_timeout_missing_command_and_exit_status(self) -> None:
        slow = self.stub("slow", "sleep 5")
        self.assertEqual(probe.run_probe(self.JSON_OUT, slow, timeout=0.3)["reason"], "timeout")
        self.assertEqual(probe.run_probe(self.JSON_OUT, self.root / "nothing-here")["reason"], "not-found")
        self.assertEqual(probe.run_probe(self.JSON_OUT, self.stub("fails", "echo '{}'; exit 3"))["reason"], "nonzero-exit")
        not_executable = self.root / "plain"
        not_executable.write_text("x", encoding="utf-8")
        self.assertEqual(probe.run_probe(self.JSON_OUT, not_executable)["reason"], "unreadable")

    def test_too_much_output_is_not_parsed(self) -> None:
        exe = self.stub("big", f"head -c {probe.MAX_OUTPUT + 1} /dev/zero | tr '\\0' 'a'")
        self.assertEqual(probe.run_probe({"argv": ["x"], "stream": "stdout", "parse": "text"}, exe)["reason"], "too-large")

    def test_standard_input_is_empty_and_no_shell_is_involved(self) -> None:
        reader = self.stub("reader", 'printf "stdin=%s" "$(cat)"')
        self.assertEqual(probe.run_probe({"argv": ["x"], "stream": "stdout", "parse": "text"}, reader)["data"], "stdin=")
        marker = self.root / "injected"
        arg = {"argv": ["a", "b"], "stream": "stdout", "parse": "text"}
        seen = []

        def runner(argv, timeout):
            seen.append((argv, timeout))
            return 0, b"ok", b""

        probe.run_probe(arg, "/opt/tool dir/client", runner, timeout=2.5)
        self.assertEqual(seen, [(["/opt/tool dir/client", "a", "b"], 2.5)])  # a list, passed as is: nothing splits or expands it
        self.assertFalse(marker.exists())
        self.assertEqual(probe.run_probe({"argv": [f"x;touch{marker}"], "stream": "stdout", "parse": "text"}, reader)["reason"],
                         "invalid-probe")

    def test_runner_is_injected_and_errors_become_reasons(self) -> None:
        def raising(error):
            def runner(argv, timeout):
                raise error
            return runner

        for error, reason in ((FileNotFoundError(), "not-found"), (subprocess.TimeoutExpired("x", 1), "timeout"),
                              (PermissionError(), "unreadable"), (OSError(), "unreadable")):
            self.assertEqual(probe.run_probe(self.JSON_OUT, "x", raising(error))["reason"], reason)
        self.assertEqual(probe.run_probe(self.JSON_OUT, "x", lambda argv, timeout: (0, '{"a": 1}', ""))["data"], {"a": 1})
        for bad in (None, {}, {"argv": ["x"]}, {**self.JSON_OUT, "stream": "stdin"}):
            self.assertEqual(probe.run_probe(bad, "x", lambda argv, timeout: (0, b"{}", b""))["reason"], "invalid-probe")

    def test_detection_never_runs_the_probe(self) -> None:
        marker = self.root / "ran"
        exe = self.stub("synthetic-client", f"touch {marker}\necho '{{}}'")
        driver = registry.validate_builtin({
            "schema_version": 2, "id": "synthetic", "display": "Synthetic",
            "detect": {"executables": ["synthetic-client"], "cli": self.JSON_OUT}, "distribution": "none",
            "stores": {"skills": [], "mcp": []},
            "capabilities": {"default_set": False, "read_only": True, "project_mcp_write": False}}, "synthetic.json")
        found = registry.detect(driver, self.root, lambda name: None, lambda name: str(exe))
        self.assertTrue(found["detected"])
        self.assertEqual(found["evidence"], {"kind": "executable", "name": "synthetic-client"})
        self.assertFalse(marker.exists())
        self.assertTrue(probe.run_probe(driver["detect"]["cli"], exe)["ok"])  # only an explicit call runs it
        self.assertTrue(marker.exists())


def synthetic(**changes) -> dict:
    data = {"schema_version": 2, "id": "synth", "display": "Synth",
            "detect": {"dirs": [{"path": "~/.synth"}]}, "distribution": "none",
            "stores": {"skills": [{"scope": "user", "path": "~/.agents/skills", "format": "dir"}],
                       "mcp": [{"scope": "user", "path": "~/.synth/mcp.json", "format": "json", "root_key": "servers"},
                               {"scope": "user", "path": "~/.synth/config.toml", "format": "toml", "root_key": "tools"}]},
            "capabilities": {"default_set": False, "read_only": False, "project_mcp_write": False}}
    data.update(changes)
    return registry.validate_builtin(data, "synth.json")


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home, self.project = self.root / "home", self.root / "proj"
        self.home.mkdir()
        self.project.mkdir()

    def write(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def skill(self, base: Path, name: str) -> Path:
        self.write(base / name / "SKILL.md", f"---\nname: {name}\ndescription: A tool for testing things in a sandbox safely\n---\nbody\n")
        return base / name

    def observe_with(self, *drivers: dict, clients=None, **kwargs) -> dict:
        """Observe with synthetic drivers registered next to the built-in ones (module tables patched for the call)."""
        skills = {**inv.SKILL_DECLS, **{d["id"]: tuple(d["stores"]["skills"]) for d in drivers if d["stores"]["skills"]}}
        mcps = {**inv.MCP_DECLS, **{d["id"]: tuple(d["stores"]["mcp"]) for d in drivers if d["stores"]["mcp"]}}
        names = (*inv.ALL_CLIENTS, *[d["id"] for d in drivers])
        primary = dict(inv.PRIMARY_SKILL_STORE)
        for d in drivers:
            for store in d["stores"]["skills"]:
                primary.setdefault((d["id"], store["scope"]), store["path"])
        with mock.patch.multiple(inv, SKILL_DECLS=skills, MCP_DECLS=mcps, ALL_CLIENTS=names, PRIMARY_SKILL_STORE=primary):
            return inv.observe(self.home, project=self.project, clients=clients or [d["id"] for d in drivers], **kwargs)


class ReadAdapterTests(Fixture):
    def test_adapters_are_named_by_format(self) -> None:
        self.assertEqual(set(store_read.ADAPTERS), {"json", "toml"})
        self.assertEqual(set(store_read.ADAPTERS) | {"dir"}, set(registry.FORMATS))
        self.assertEqual(store_read.read_object("dir", self.root), (None, "unreadable"))  # directories are scanned, not parsed

    def test_the_declared_format_and_root_key_decide_how_a_store_is_read(self) -> None:
        self.write(self.home / ".synth/mcp.json", {"servers": {"from-json": {"command": "a"}}, "mcpServers": {"ignored": {"command": "z"}}})
        self.write(self.home / ".synth/config.toml", '[tools.from-toml]\ncommand = "b"\n[mcp_servers.ignored]\ncommand = "y"\n')
        report = self.observe_with(synthetic())
        self.assertEqual({(m["name"], m["store"]) for m in report["mcps"]},
                         {("from-json", "~/.synth/mcp.json"), ("from-toml", "~/.synth/config.toml")})
        self.assertEqual(report["unknown"], [])

    def test_a_wrong_format_is_reported_not_guessed(self) -> None:
        self.write(self.home / ".synth/mcp.json", '[tools.x]\ncommand = "b"\n')
        self.write(self.home / ".synth/config.toml", '{"tools": {}}')
        report = self.observe_with(synthetic())
        self.assertEqual({(u["store"], u["reason"]) for u in report["unknown"]},
                         {("~/.synth/mcp.json", "invalid-json"), ("~/.synth/config.toml", "invalid-toml")})

    def test_a_root_key_that_is_not_an_object_is_unexpected_structure(self) -> None:
        self.write(self.home / ".synth/mcp.json", {"servers": ["x"]})
        report = self.observe_with(synthetic())
        self.assertEqual(report["unknown"], [{"store": "~/.synth/mcp.json", "client": "synth", "reason": "unexpected-structure"}])

    def test_a_section_store_reads_the_entry_under_the_project_path(self) -> None:
        driver = synthetic(stores={"skills": [], "mcp": [
            {"scope": "local", "path": "~/.synth/all.json#workspaces", "format": "json", "root_key": "servers"}]})
        self.write(self.home / ".synth/all.json", {"workspaces": {str(self.project): {"servers": {"mine": {"command": "a"}}},
                                                                 "/elsewhere": {"servers": {"other": {"command": "b"}}}}})
        report = self.observe_with(driver)
        self.assertEqual([(m["name"], m["scope"]) for m in report["mcps"]], [("mine", "local")])
        self.write(self.home / ".synth/all.json", {"workspaces": [1]})
        self.assertEqual(self.observe_with(driver)["unknown"][0]["reason"], "claude-local-structure-unrecognized")

    def test_managed_and_trust_attributes_come_from_the_store_declaration(self) -> None:
        self.write(self.home / ".claude/agent-pack/state/pkg.json", {"schema_version": 1, "skills": ["linked"], "mcps": {"managed": {}}})
        elsewhere = self.skill(self.home / "src", "linked")
        for base in (self.home / ".agents/skills", self.home / "plain/skills"):
            base.mkdir(parents=True)
            (base / "linked").symlink_to(elsewhere)
        self.write(self.home / ".synth/mcp.json", {"servers": {"managed": {"command": "a"}}})
        self.write(self.project / ".synth/mcp.json", {"servers": {"managed": {"command": "a"}}})
        stores = {"skills": [{"scope": "user", "path": "~/.agents/skills", "format": "dir", "managed_by": "agent-pack-state"},
                             {"scope": "user", "path": "~/plain/skills", "format": "dir"}],
                  "mcp": [{"scope": "user", "path": "~/.synth/mcp.json", "format": "json", "root_key": "servers",
                           "managed_by": "agent-pack-state"},
                          {"scope": "project", "path": "<project>/.synth/mcp.json", "format": "json", "root_key": "servers",
                           "trust_required": True}]}
        report = self.observe_with(synthetic(stores=stores))
        origins = {s["store"]: s["origin"]["kind"] for s in report["skills"]}
        self.assertEqual(origins, {"~/.agents/skills": "state-record", "~/plain/skills": "unknown"})
        by_scope = {m["scope"]: m for m in report["mcps"]}
        self.assertEqual(by_scope["user"]["origin"], {"kind": "state-record", "detail": "pkg"})
        self.assertEqual((by_scope["user"]["notes"], by_scope["project"]["notes"], by_scope["project"]["origin"]["kind"]),
                         ([], ["codex-project-requires-trust"], "unknown"))

    def test_builtin_stores_carry_the_declarations_the_old_name_branches_held(self) -> None:
        managed = [(c, s["scope"], k) for c in registry.all_ids() for k in ("skills", "mcp")
                   for s in registry.stores(c, k) if s.get("managed_by")]
        self.assertEqual(managed, [("claude", "user", "skills"), ("claude", "user", "mcp")])
        trust = [(c, s["scope"]) for c in registry.all_ids() for s in registry.stores(c, "mcp") if s.get("trust_required")]
        self.assertEqual(trust, [("codex", "project")])
        self.assertEqual({c: [s["root_key"] for s in registry.stores(c, "mcp")] for c in registry.all_ids()},
                         {"claude": ["mcpServers"] * 3, "codex": ["mcp_servers"] * 2, "cursor": ["mcpServers"] * 2,
                          "claude-desktop": ["mcpServers"], "gemini": []})


class EmptyFileTests(Fixture):
    """Before #134 batch B a 0-byte configuration file was ``invalid-json`` (JSON stores) and therefore an undetermined
    store in inventory and a ``desktop-config`` warning in ``doctor --client claude-desktop``; an empty TOML file already
    read as an empty table. Now a 0-byte file is an empty object for both formats."""

    def test_adapters_read_zero_bytes_as_an_empty_object(self) -> None:
        for name, reader in (("a.json", store_read.read_json_object), ("a.toml", store_read.read_toml_object)):
            self.write(self.root / name, "")
            self.assertEqual(reader(self.root / name), ({}, None), name)
        self.write(self.root / "ws.json", "  \n")
        self.assertEqual(store_read.read_json_object(self.root / "ws.json"), (None, "invalid-json"))  # whitespace is not empty
        self.assertEqual(store_read.read_json_object(self.root / "missing.json"), (None, None))
        self.write(self.root / "list.json", "[]")
        self.assertEqual(store_read.read_json_object(self.root / "list.json"), (None, "unexpected-structure"))

    def test_inventory_reports_no_servers_and_nothing_undetermined(self) -> None:
        for path in (self.home / ".claude.json", self.home / ".cursor/mcp.json", self.project / ".mcp.json",
                     self.home / ".codex/config.toml"):
            self.write(path, "")
        report = inv.observe(self.home, project=self.project)
        self.assertEqual((report["mcps"], report["unknown"]), ([], []))

    def test_other_corrupt_files_are_still_undetermined(self) -> None:
        self.write(self.home / ".claude.json", "{")
        self.write(self.home / ".cursor/mcp.json", " ")
        reasons = {(u["store"], u["reason"]) for u in inv.observe(self.home, project=self.project)["unknown"]}
        self.assertEqual(reasons, {("~/.claude.json", "invalid-json"), ("~/.claude.json#projects", "invalid-json"),
                                   ("~/.cursor/mcp.json", "invalid-json")})

    def test_a_zero_byte_desktop_file_is_a_parsed_empty_configuration(self) -> None:
        self.write(self.home / ".config/Claude/claude_desktop_config.json", "")
        entries, unknown = inv.observe_desktop(self.home, "linux")
        self.assertEqual((entries, unknown), ([], []))
        self.assertEqual(inv.desktop_info(self.home, "linux")["parse_ok"], True)
        self.assertEqual(inv.desktop_report(self.home, "linux")["warnings"], [])

    def test_doctor_reads_native_servers_the_same_way(self) -> None:
        for path in (self.home / ".codex/config.toml", self.home / ".cursor/mcp.json", self.home / ".claude.json"):
            self.write(path, "")
        for client in registry.default_ids():
            self.assertEqual(config._native_user_mcps(client, self.home), {}, client)


class NativeMcpReadTests(Fixture):
    def test_the_driver_decides_file_format_and_key(self) -> None:
        self.write(self.home / ".codex/config.toml", '[mcp_servers.a]\ncommand = "x"\n')
        self.write(self.home / ".cursor/mcp.json", {"mcpServers": {"b": {"command": "y"}}})
        self.write(self.home / ".claude.json", {"mcpServers": {"c": {"command": "z"}}, "projects": {}})
        self.assertEqual({c: sorted(config._native_user_mcps(c, self.home)) for c in registry.default_ids()},
                         {"claude": ["c"], "codex": ["a"], "cursor": ["b"]})
        self.assertEqual(config._native_user_mcps("claude-desktop", self.home), {})  # no user-level file at a fixed path
        self.assertEqual(config._native_user_mcps("not-registered", self.home), {})

    def test_symlinked_unparsable_or_wrong_shaped_files_read_as_nothing(self) -> None:
        real = self.root / "real.toml"
        self.write(real, '[mcp_servers.a]\ncommand = "x"\n')
        (self.home / ".codex").mkdir()
        (self.home / ".codex/config.toml").symlink_to(real)
        self.write(self.home / ".cursor/mcp.json", "{")
        self.write(self.home / ".claude.json", {"mcpServers": []})
        self.assertEqual({c: config._native_user_mcps(c, self.home) for c in registry.default_ids()},
                         {"claude": {}, "codex": {}, "cursor": {}})


class DesktopTargetTests(Fixture):
    def test_desktop_stays_a_read_only_separate_target(self) -> None:
        self.assertEqual(inv.DESKTOP, "claude-desktop")
        self.assertEqual(registry.per_platform_ids(), ("claude-desktop",))
        self.assertTrue(registry.supports(inv.DESKTOP, "read_only"))
        self.assertNotIn(inv.DESKTOP, inv.CLIENTS)
        self.assertIn(inv.DESKTOP, inv.ALL_CLIENTS)
        self.assertNotIn(inv.DESKTOP, inv.MCP_STORES)  # resolved by the platform table, not from a path template
        self.assertNotIn(inv.DESKTOP, inv.SKILL_STORES)

    def test_desktop_is_observed_only_when_named(self) -> None:
        self.write(self.home / ".config/Claude/claude_desktop_config.json", {"mcpServers": {"d": {"command": "x"}}})
        self.write(self.home / ".cursor/mcp.json", {"mcpServers": {"c": {"command": "y"}}})
        default = inv.observe(self.home, project=self.project, platform="linux")
        self.assertEqual({m["client"] for m in default["mcps"]}, {"cursor"})
        self.assertNotIn("desktop", default)
        named = inv.observe(self.home, project=self.project, clients=["claude-desktop"], platform="linux")
        self.assertEqual([(m["client"], m["name"], m["store"]) for m in named["mcps"]], [("claude-desktop", "d", "desktop-config")])
        self.assertEqual(named["desktop"]["parse_ok"], True)
        every = inv.observe(self.home, project=self.project, clients=["all"], platform="linux")
        self.assertEqual({m["client"] for m in every["mcps"]}, {"cursor", "claude-desktop"})


class SharedStoreTests(Fixture):
    """``~/.agents/skills`` is declared by Codex and Cursor. Entries and duplicate groups are per client: a shared path is
    scanned once for each client that declares it, never merged across clients, and a path that two stores of one client reach
    (a symlink, or the shared directory itself) is ``same-file``."""

    def setUp(self) -> None:
        super().setUp()
        self.shared = self.home / ".agents/skills"
        self.third = synthetic(id="third", stores={"skills": [
            {"scope": "user", "path": "~/.third/skills", "format": "dir"},
            {"scope": "user", "path": "~/.agents/skills", "format": "dir"}], "mcp": []})

    def test_every_declaring_client_sees_the_shared_skill_once(self) -> None:
        self.skill(self.shared, "common")
        report = self.observe_with(self.third, clients=["codex", "cursor", "third"])
        self.assertEqual(sorted((s["client"], s["path"]) for s in report["skills"]),
                         [("codex", "~/.agents/skills/common"), ("cursor", "~/.agents/skills/common"),
                          ("third", "~/.agents/skills/common")])
        self.assertEqual(report["duplicates"]["skills"], [])
        self.assertEqual({s["precedence"] for s in report["skills"]}, {"unique"})
        self.assertEqual(len({s["content_sha256"] for s in report["skills"]}), 1)

    def test_a_skill_reached_through_two_stores_of_one_client_is_same_file(self) -> None:
        real = self.skill(self.shared, "common")
        (self.home / ".third").mkdir()
        (self.home / ".third/skills").symlink_to(self.shared)  # the client's own directory is the shared one
        (self.home / ".codex").mkdir()
        (self.home / ".codex/skills").symlink_to(self.shared)
        report = self.observe_with(self.third, clients=["codex", "cursor", "third"])
        groups = {g["client"]: g for g in report["duplicates"]["skills"]}
        self.assertEqual(set(groups), {"codex", "third"})  # cursor reads the shared path only: nothing to compare with
        for group in groups.values():
            self.assertEqual((group["relation"], group["effective"]), ("same-file", None))
        self.assertEqual({s["precedence"] for s in report["skills"]}, {"unique"})
        # the summary counts a shared real file once per client, however many stores reach it
        per_client = report["summary"]["per_client"]
        for client in ("codex", "third"):
            self.assertEqual(per_client[client]["skill_count"], 2)
            self.assertEqual(per_client[client]["description_bytes_distinct_files"], per_client[client]["description_bytes_total"] // 2)
        self.assertEqual(per_client["cursor"]["skill_count"], 1)
        self.assertTrue(real.is_dir())

    def test_a_copy_next_to_the_shared_directory_is_a_duplicate_of_that_client_only(self) -> None:
        self.skill(self.shared, "common")
        self.skill(self.home / ".third/skills", "common")
        report = self.observe_with(self.third, clients=["codex", "cursor", "third"])
        self.assertEqual([(g["client"], g["relation"], g["stores"]) for g in report["duplicates"]["skills"]],
                         [("third", "identical", ["~/.agents/skills/common", "~/.third/skills/common"])])
        verdict = report["duplicates"]["skills"][0]["effective"]
        self.assertEqual((verdict["rule"], verdict["evidence"]), ("unknown", "unknown"))  # nothing declared for the client

    def test_the_real_paths_of_a_shared_store_agree_across_clients(self) -> None:
        self.skill(self.shared, "common")
        report = self.observe_with(self.third, clients=["codex", "cursor", "third"])
        refs = sorted(s["ref"] for s in report["skills"])
        self.assertEqual(refs, ["skill:common@codex:user#agents", "skill:common@cursor:user#agents", "skill:common@third:user#agents"])


if __name__ == "__main__":
    unittest.main()
