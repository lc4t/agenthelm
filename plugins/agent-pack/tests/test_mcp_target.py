"""MCP target judgement (#184, design §4): the fingerprint stays, the targets are compared in process, and a same-fingerprint
set whose targets differ or cannot be confirmed is a hint (``mcp_hints[]``), not a duplicate group."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
ROOT = TESTS.parents[2]
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(RUNTIME))

import agent_pack_dedupe as dedupe  # noqa: E402
import agent_pack_inventory as inv  # noqa: E402
import agent_pack_mcp_target as target  # noqa: E402
from test_inventory_observe import Fixture  # noqa: E402

REQUIRE_TAGS = os.environ.get("AGENTHELM_REQUIRE_TAGS") == "1"
LONG_A = "/opt/" + "a" * 70 + "/server.py"
LONG_B = "/opt/" + "b" * 70 + "/server.py"
ID_A, ID_B = "AbCdEfGhIjKlMnOpQrSt12345678", "ZyXwVuTsRqPoNmLkJiHg98765432"
FAKE_VALUES = ("sk-abcdefghijklmnopqrstuv", "hunter2-password-value", "Bearer qqqheadersecret", "qquerysecretvalue1")


def duplicates(report: dict) -> list[dict]:
    return [item for item in report["duplicates"]["mcps"] if item["kind"] == "same-fingerprint"]


def hints(report: dict) -> list[dict]:
    return report.get("mcp_hints", [])


class OldRuntime:
    """The v6.15.0 runtime, taken from the release tag (the baseline of the observation)."""

    path: Path | None = None
    problem: str | None = None

    @classmethod
    def get(cls, test: unittest.TestCase) -> Path:
        if cls.path is None and cls.problem is None:
            scratch = Path(tempfile.mkdtemp())
            atexit.register(shutil.rmtree, scratch, ignore_errors=True)
            archive = subprocess.run(["git", "-C", str(ROOT), "archive", "v6.15.0", "plugins/agent-pack/runtime"],
                                     capture_output=True, check=False)
            if archive.returncode:
                cls.problem = "release tag v6.15.0 is not in this clone; run `git fetch --tags origin`"
            else:
                subprocess.run(["tar", "-x", "-C", str(scratch)], input=archive.stdout, check=True)
                cls.path = scratch / "plugins" / "agent-pack" / "runtime"
        if cls.path is None:
            if REQUIRE_TAGS:
                raise AssertionError("AGENTHELM_REQUIRE_TAGS=1 but " + str(cls.problem))
            test.skipTest(str(cls.problem))
        return cls.path


class Base(Fixture):
    def cursor(self, servers: dict, scope: str = "user") -> None:
        base = self.home if scope == "user" else self.project
        self.write(base / ".cursor" / "mcp.json", {"mcpServers": servers})

    def run_cli(self, runtime: Path, *args: str, lang: str = "en") -> tuple[int, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("AGENTHELM_", "XDG_"))}
        env.update(HOME=str(self.home), AGENTHELM_LANG=lang)
        proc = subprocess.run([sys.executable, str(runtime / "agenthelm.py"), *args, "--home", str(self.home),
                               "--project", str(self.project)], capture_output=True, text=True, env=env, check=False)
        return proc.returncode, proc.stdout + proc.stderr

    def old_report(self) -> dict:
        code, out = self.run_cli(OldRuntime.get(self), "inventory", "--output", "json")
        self.assertEqual(code, 0, out)
        return json.loads(out)

    def assert_mechanism(self, names: list[str], differs_in: list[str], client: str = "cursor") -> dict:
        """Old runtime: the set is a duplicate group. New: it is a hint, with ``differs_in``, and no longer a group."""
        old = [item for item in self.old_report()["duplicates"]["mcps"]
               if item["kind"] == "same-fingerprint" and item["names"] == names]
        self.assertEqual(len(old), 1, "6.15.0 reports the set as a duplicate")
        report = self.observe()
        self.assertEqual([item for item in duplicates(report) if item["names"] == names], [])
        found = [hint for hint in hints(report) if hint["names"] == names]
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["kind"], found[0]["target_relation"], found[0]["differs_in"], found[0]["client"]),
                         ("same-fingerprint-different-target", "different", differs_in, client))
        self.assertEqual(found[0]["id"], dedupe.group_id("mcp", client, found[0]["fingerprint"]))
        return found[0]


class MechanismTests(Base):
    """Design §1.3: the four ways the same fingerprint hides different targets."""

    def test_m1_long_path_arguments_are_compared_and_not_redacted_into_equality(self) -> None:
        args = ["--port", "8001", "--mode", "stdio"]
        self.cursor({"a": {"command": "python3", "args": [LONG_A, *args]}, "b": {"command": "python3", "args": [LONG_B, *args]}})
        found = self.assert_mechanism(["a", "b"], ["target"])
        report = self.observe()
        classes = {item["name"]: item["target_class"] for item in report["mcps"]}
        self.assertNotEqual(classes["a"], classes["b"])
        self.assertEqual(report["mcps"][0]["target"], {"kind": "script", "label": "<redacted>", "basis": ["command", "args[0]"]})
        self.assertNotIn("aaaaaaaa", json.dumps(report))
        self.assertEqual(found["refs"], ["mcp:a@cursor:user", "mcp:b@cursor:user"])

    def test_m2_token_shaped_service_ids_are_compared(self) -> None:
        self.cursor({"a": {"command": "python3", "args": ["server.py", "--target", ID_A]},
                     "b": {"command": "python3", "args": ["server.py", "--target", ID_B]}})
        self.assert_mechanism(["a", "b"], ["args"])
        self.assertNotIn(ID_A, json.dumps(self.observe()))

    def test_m3_env_values_are_compared(self) -> None:
        self.cursor({"a": {"command": "uvx", "args": ["tool"], "env": {"TARGET": "alpha"}},
                     "b": {"command": "uvx", "args": ["tool"], "env": {"TARGET": "beta"}}})
        self.assert_mechanism(["a", "b"], ["env"])

    def test_m4_codex_cwd_is_compared(self) -> None:
        self.write(self.home / ".codex" / "config.toml",
                   '[mcp_servers.a]\ncommand = "uvx"\nargs = ["tool"]\ncwd = "/work/one"\n'
                   '[mcp_servers.b]\ncommand = "uvx"\nargs = ["tool"]\ncwd = "/work/two"\n')
        found = self.assert_mechanism(["a", "b"], ["cwd"], client="codex")
        self.assertNotIn("/work/", json.dumps(self.observe()))
        self.assertEqual(found["stores"], ["~/.codex/config.toml"])

    def test_remote_query_value_is_compared(self) -> None:
        self.cursor({"a": {"url": "https://mcp.example.com/mcp?workspace=one"},
                     "b": {"url": "https://mcp.example.com/mcp?workspace=two"}})
        self.assert_mechanism(["a", "b"], ["url"])


class StillDuplicateTests(Base):
    def assert_group(self, names: list[str]) -> dict:
        report = self.observe()
        group = next(item for item in duplicates(report) if item["names"] == names)
        self.assertEqual(group["target_relation"], "same")
        self.assertEqual(hints(report), [])
        self.assertEqual(group["relation"], "identical")
        return group

    def test_same_target_and_arguments_under_different_names(self) -> None:
        self.cursor({"a": {"command": "uvx", "args": ["tool", "--flag", "x"]}, "b": {"command": "uvx", "args": ["tool", "--flag", "x"]}})
        self.assert_group(["a", "b"])
        report = self.observe()
        self.assertEqual({item["target_class"] for item in report["mcps"]}, {report["mcps"][0]["target_class"]})
        self.assertRegex(report["mcps"][0]["target_class"], r"^t-[0-9a-f]{8}\.1$")

    def test_variable_references_and_their_defaults_are_the_same_target(self) -> None:
        self.cursor({"a": {"command": "uvx", "args": ["tool", "--port", "${PORT:-80}"], "env": {"HOME_DIR": "${HOME}"}},
                     "b": {"command": "uvx", "args": ["tool", "--port", "${PORT:-90}"], "env": {"HOME_DIR": "${HOME}"}}})
        self.assert_group(["a", "b"])

    def test_url_differing_in_scheme_and_host_case_only(self) -> None:
        self.cursor({"a": {"url": "https://example.com/mcp"}, "b": {"url": "HTTPS://Example.COM/mcp"}})
        self.assert_group(["a", "b"])

    def test_same_credential_positions_stay_a_duplicate(self) -> None:
        self.cursor({"a": {"command": "uvx", "args": ["tool", "--api-key", FAKE_VALUES[0]]},
                     "b": {"command": "uvx", "args": ["tool", "--api-key", FAKE_VALUES[0]]}})
        self.assert_group(["a", "b"])

    def test_entry_without_fingerprint_has_no_target(self) -> None:
        self.cursor({"none": {}})
        item = self.observe()["mcps"][0]
        self.assertEqual((item["fingerprint"], item["target"], item["target_class"]), (None, None, None))


class RelationTests(unittest.TestCase):
    """The comparison itself, on URLs and launchers (a URL that differs in port, path or trailing slash has another
    fingerprint, so these never form a same-fingerprint set in an inventory; the rules are still the contract)."""

    def remote(self, url: str, **extra) -> dict | None:
        return target.analyze({"url": url, **extra}, None, url, "remote", "url", lambda text: text)

    def stdio(self, command: str, args: list, **extra) -> dict | None:
        return target.analyze({"command": command, "args": args, **extra}, command, None, "stdio", "url", lambda text: text)

    def relation(self, *analyses: dict | None) -> dict:
        return target.relation(list(analyses))

    def test_url_equivalence_rules(self) -> None:
        base = self.remote("https://example.com/mcp")
        same = [self.remote("HTTPS://EXAMPLE.com/mcp"), self.remote("https://example.com:443/mcp"),
                self.remote("https://example.com/mcp/"), self.remote("http://example.com:80/mcp")]
        self.assertEqual(self.relation(base, same[0], same[1], same[2])["relation"], "same")
        self.assertEqual(self.remote("http://example.com:80/mcp")["parts"]["url"][:3], ("http", "example.com", None))
        self.assertEqual(same[3]["target"]["label"], "example.com/mcp")
        for other in ("https://example.com/other", "https://example.com:8443/mcp", "https://example.com/mcp?x=1",
                      "http://example.com/mcp"):
            verdict = self.relation(base, self.remote(other))
            self.assertEqual((verdict["relation"], verdict["differs_in"]), ("different", ["url"]), other)

    def test_user_info_and_credential_query_values_are_masked_not_compared(self) -> None:
        first = self.remote("https://one:pw1@example.com/mcp?access_token=aaa&workspace=w")
        second = self.remote("https://two:pw2@example.com/mcp?access_token=bbb&workspace=w")
        verdict = self.relation(first, second)
        self.assertEqual((verdict["relation"], verdict["reasons"]), ("unknown", ["credential-differs"]))
        self.assertNotIn("pw1", json.dumps(first["target"]))

    def test_launcher_tables(self) -> None:
        cases = {
            ("npx", ("-y", "@Scope/Pkg@1.2.3", "--flag")): ("package", "@scope/pkg@1.2.3"),
            ("npx", ("-y", "mcp-remote", "https://example.com/sse")): ("package", "mcp-remote"),
            ("uvx", ("--from", "git+https://example.com/x.git", "tool-name")): ("package", "tool-name"),
            ("uvx", ("My_Tool==1.0",)): ("package", "my-tool==1.0"),
            ("pnpm", ("dlx", "pkg")): ("package", "pkg"),
            ("python3", ("-u", "run.py", "--x")): ("script", "run.py"),
            ("python3.11", ("-m", "pkg.module")): ("script", "pkg.module"),
            ("node", ("dist/index.js",)): ("script", "dist/index.js"),
            ("python3", ("-c", "print(1)")): ("opaque", "python3"),
            ("docker", ("run", "-i", "--rm", "image")): ("opaque", "docker"),
            ("python3", ("--strange-option", "run.py")): ("opaque", "python3"),
            ("pnpm", ("start",)): ("binary", "pnpm"),
            ("/usr/local/bin/my-server", ("--stdio",)): ("binary", "/usr/local/bin/my-server"),
        }
        for (command, args), (kind, label) in cases.items():
            found = self.stdio(command, list(args))["target"]
            self.assertEqual((found["kind"], found["label"]), (kind, label), (command, args))

    def test_launcher_options_that_take_a_value_belong_to_the_target(self) -> None:
        first = self.stdio("uvx", ["--from", "pkg-a", "tool"])
        second = self.stdio("uvx", ["--from", "pkg-b", "tool"])
        self.assertEqual(self.relation(first, second)["differs_in"], ["target"])

    def test_opaque_launcher_is_unknown_even_when_identical(self) -> None:
        first = self.stdio("docker", ["run", "image"])
        self.assertEqual(self.relation(first, first), {"relation": "unknown", "differs_in": [], "reasons": ["opaque-launcher"]})
        env = self.stdio("docker", ["run", "image"], env={"K": "v"})
        self.assertEqual(self.relation(first, env)["differs_in"], ["env"])

    def test_unreadable_entry_is_unknown(self) -> None:
        self.assertEqual(self.relation(self.stdio("uvx", ["x"]), None)["reasons"], ["unreadable"])

    def test_target_label_never_carries_a_token_shape(self) -> None:
        for args in (["server.py", FAKE_VALUES[0]], [FAKE_VALUES[0]], [ID_A], [LONG_A]):
            label = self.stdio("python3", args)["target"]["label"]
            self.assertIn(label, ("server.py", "<redacted>", "python3"), args)  # a credential in the target slot: opaque


class SecretTests(Base):
    def test_no_value_reaches_any_output(self) -> None:
        self.cursor({
            "a": {"command": "python3", "args": ["server.py", "--api-key", FAKE_VALUES[0]], "env": {"DEMO_PASSWORD": FAKE_VALUES[1]},
                  "headers": {"Authorization": FAKE_VALUES[2]}},
            "b": {"command": "python3", "args": ["server.py", "--api-key", "sk-other-value-zzzzzzzzzzzz"],
                  "env": {"DEMO_PASSWORD": "other-password-value"}, "headers": {"Authorization": "Bearer other"}},
            "c": {"url": f"https://example.com/mcp?token={FAKE_VALUES[3]}"},
            "d": {"url": "https://example.com/mcp?token=otherquerysecret"}})
        report = self.observe()
        self.assertEqual({hint["names"][0] for hint in hints(report)}, {"a", "c"})
        self.assertTrue(all(hint["target_relation"] == "unknown" and hint["reasons"] == ["credential-differs"] for hint in hints(report)))
        text = json.dumps(report) + inv.render_text(report)
        for value in (*FAKE_VALUES, "sk-other-value-zzzzzzzzzzzz", "other-password-value", "otherquerysecret"):
            self.assertNotIn(value, text)
        for item in report["mcps"]:
            self.assertIn(item["target"]["label"], ("server.py", "mcp.example.com/mcp", "example.com/mcp"))
        code, out, _err = self.cli("--output", "text")
        self.assertEqual(code, 0)
        for value in FAKE_VALUES:
            self.assertNotIn(value, out)

    def test_credential_only_difference_is_unknown_and_listed_as_a_hint(self) -> None:
        self.cursor({"a": {"command": "uvx", "args": ["tool", "--api-key", FAKE_VALUES[0]]},
                     "b": {"command": "uvx", "args": ["tool", "--api-key", "sk-other-value-zzzzzzzzzzzz"]}})
        report = self.observe()
        self.assertEqual(duplicates(report), [])
        self.assertEqual([(hint["kind"], hint["target_relation"], hint["differs_in"], hint["reasons"]) for hint in hints(report)],
                         [("same-fingerprint-target-unconfirmed", "unknown", [], ["credential-differs"])])


class TextAndDedupeTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.cursor({"a": {"command": "uvx", "args": ["tool"], "env": {"TARGET": "alpha"}},
                     "b": {"command": "uvx", "args": ["tool"], "env": {"TARGET": "beta"}},
                     "c": {"command": "uvx", "args": ["other"]}})

    def test_text_output_has_a_hint_section_in_both_languages(self) -> None:
        code, out, _ = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("MCP hints (not duplicates)", out)
        self.assertIn("a and b: same fingerprint, different targets (differs in: environment)", out)
        self.assertNotIn("duplicate [cursor] a, b", out)
        code, out = self.run_cli(RUNTIME, "inventory", lang="zh")
        self.assertIn("MCP 提示（不算重复）", out)
        self.assertIn("a 与 b：指纹相同，目标不同（差异：环境变量）", out)

    def test_old_group_id_is_not_a_duplicate_group(self) -> None:
        old = [item for item in self.old_group_ids() if item["names"] == ["a", "b"]]
        gid = old[0]["id"]
        code, out = self.run_cli(RUNTIME, "dedupe", gid, "--keep-all", "--note", "x", "--output", "json")
        self.assertEqual(code, 1, out)
        blocked = json.loads(out)["plan"]["blocked"]
        self.assertEqual([item["code"] for item in blocked], ["not-a-duplicate-group"])
        self.assertEqual((blocked[0]["target_relation"], blocked[0]["differs_in"]), ("different", ["env"]))
        code, out = self.run_cli(RUNTIME, "dedupe", "--list", "--output", "json")
        self.assertEqual([item for item in json.loads(out)["groups"] if item["id"] == gid], [])

    def old_group_ids(self) -> list[dict]:
        old = OldRuntime.get(self)
        code, out = self.run_cli(old, "dedupe", "--list", "--output", "json")
        self.assertEqual(code, 0, out)
        return [{"id": item["id"], "names": item["key"].split("|")} for item in json.loads(out)["groups"] if item["kind"] == "mcp"]

    def test_hint_id_is_the_id_the_group_had_in_6_15_0(self) -> None:
        self.assertEqual({hint["id"] for hint in hints(self.observe())}, {item["id"] for item in self.old_group_ids()})


class CrossClientTests(Base):
    """D-CB4: the same target in several clients stays advice; a different or unconfirmed target does not."""

    def plan_warnings(self, group_client: str = "cursor") -> list[str]:
        report = inv.observe(self.home, self.project)
        group = next(item for item in dedupe.list_groups(report) if item["client"] == group_client and item["kind"] == "mcp")
        plan, _ctx = dedupe.build_plan(self.home, self.project, group["id"], None, False, True, False, False, "n")
        return [item["code"] for item in plan["warnings"]]

    def setUp(self) -> None:
        super().setUp()
        self.cursor({"a": {"command": "uvx", "args": ["tool"]}, "b": {"command": "uvx", "args": ["tool"]}})

    def codex(self, extra: str = "") -> None:
        self.write(self.home / ".codex" / "config.toml", '[mcp_servers.a]\ncommand = "uvx"\nargs = ["tool"]\n' + extra)

    def test_same_target_in_another_client_keeps_the_advice(self) -> None:
        self.codex()
        codes = self.plan_warnings()
        self.assertIn("cross-client-keep-recommended", codes)
        self.assertNotIn("cross-client-target-unconfirmed", codes)

    def test_different_target_in_another_client_says_nothing(self) -> None:
        self.codex('cwd = "/work/elsewhere"\n')
        codes = self.plan_warnings()
        self.assertNotIn("cross-client-keep-recommended", codes)
        self.assertNotIn("cross-client-target-unconfirmed", codes)

    def test_unconfirmed_target_in_another_client(self) -> None:
        docker = {"a": {"command": "docker", "args": ["run", "image"]}}
        self.cursor(docker)
        self.cursor(docker, scope="project")   # a same-name group (a same-fingerprint set of opaque entries is only a hint)
        self.write(self.home / ".codex" / "config.toml",
                   '[mcp_servers.a]\ncommand = "docker"\nargs = ["run", "image"]\nenv = { K = "v" }\n')
        codes = self.plan_warnings()
        self.assertIn("cross-client-target-unconfirmed", codes)
        self.assertNotIn("cross-client-keep-recommended", codes)


class SameNameTests(Base):
    def test_same_name_group_gains_target_relation_and_a_collision_warning(self) -> None:
        self.cursor({"s": {"command": "uvx", "args": ["tool"], "env": {"TARGET": "user"}}})
        self.cursor({"s": {"command": "uvx", "args": ["tool"], "env": {"TARGET": "project"}}}, scope="project")
        report = self.observe()
        group = next(item for item in report["duplicates"]["mcps"] if item["kind"] == "same-name")
        self.assertEqual((group["relation"], group["target_relation"]), ("identical", "different"))
        self.assertEqual(hints(report), [])
        found = next(item for item in dedupe.list_groups(report) if item["kind"] == "mcp")
        plan, _ctx = dedupe.build_plan(self.home, self.project, found["id"], None, False, True, False, False, "n")
        self.assertIn("fingerprint-collision", [item["code"] for item in plan["warnings"]])

    def test_same_name_same_target_has_no_collision_warning(self) -> None:
        self.cursor({"s": {"command": "uvx", "args": ["tool"]}})
        self.cursor({"s": {"command": "uvx", "args": ["tool"]}}, scope="project")
        report = self.observe()
        self.assertEqual(next(item for item in report["duplicates"]["mcps"] if item["kind"] == "same-name")["target_relation"], "same")
        found = next(item for item in dedupe.list_groups(report) if item["kind"] == "mcp")
        plan, _ctx = dedupe.build_plan(self.home, self.project, found["id"], None, False, True, False, False, "n")
        self.assertNotIn("fingerprint-collision", [item["code"] for item in plan["warnings"]])


class BaselineTests(Base):
    """A fixture without a collision: the inventory equals 6.15.0's once the two new keys are gone, byte for byte."""

    def fixture(self) -> None:
        self.cursor({"x": {"command": "uvx", "args": ["tool"]}, "y": {"url": "https://example.com/mcp"},
                     "z": {"command": "node", "args": ["a.js"], "env": {"K": "v"}}})
        self.write(self.home / ".codex" / "config.toml", '[mcp_servers.x]\ncommand = "uvx"\nargs = ["tool"]\n')
        self.write(self.home / ".claude.json", {"mcpServers": {"w": {"command": "npx", "args": ["-y", "pkg"]}}})
        self.skill(self.home / ".claude" / "skills", "one")

    @staticmethod
    def strip(text: str) -> str:
        data = json.loads(text)
        for item in data["mcps"]:
            item.pop("target")
            item.pop("target_class")
        for group in data["duplicates"]["mcps"]:
            group.pop("target_relation")
        return json.dumps(data, indent=2, sort_keys=True)

    def test_inventory_and_dedupe_list_equal_the_6_15_0_output(self) -> None:
        self.fixture()
        old = OldRuntime.get(self)
        for args in (("inventory", "--output", "json"), ("dedupe", "--list", "--output", "json")):
            code_old, old_out = self.run_cli(old, *args)
            code_new, new_out = self.run_cli(RUNTIME, *args)
            self.assertEqual((code_old, code_new), (0, 0), (old_out, new_out))
            if args[0] == "inventory":
                self.assertEqual(self.strip(new_out), json.dumps(json.loads(old_out), indent=2, sort_keys=True))
            else:
                self.assertEqual(new_out, old_out)
        code_old, old_text = self.run_cli(old, "inventory")
        code_new, new_text = self.run_cli(RUNTIME, "inventory")
        self.assertEqual(new_text, old_text)


if __name__ == "__main__":
    unittest.main()
