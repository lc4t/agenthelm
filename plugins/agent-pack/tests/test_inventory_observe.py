"""`agenthelm inventory` (#67 #69): the read-only observation layer and its JSON contract."""
from __future__ import annotations

import hashlib
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

import agent_pack_inventory as inv  # noqa: E402

SECRETS = ("sk-abcdefghijklmnopqrstuv", "supersecretvalue", "hunter2token", "Bearer zzzsecret", "qqquerysecret")


def skill_md(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\nbody\n"


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()  # macOS: /var is a symlink to /private/var
        self.home = self.root / "home"
        self.project = self.root / "proj"
        self.home.mkdir()
        self.project.mkdir()

    def skill(self, base: Path, name: str, description: str = "A tool for testing things in a sandbox safely", text: str | None = None) -> Path:
        directory = base / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(text if text is not None else skill_md(name, description), encoding="utf-8")
        return directory

    def write(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def observe(self, **kwargs) -> dict:
        kwargs.setdefault("project", self.project)
        return inv.observe(self.home, **kwargs)

    def cli(self, *args: str) -> tuple[int, str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("AGENTHELM_", "XDG_"))}
        env.update(HOME=str(self.home), AGENTHELM_LANG="en")
        proc = subprocess.run([sys.executable, str(RUNTIME / "agenthelm.py"), "inventory", "--home", str(self.home),
                               "--project", str(self.project), *args], capture_output=True, text=True, env=env, check=False)
        return proc.returncode, proc.stdout, proc.stderr


class EmptyMachineTests(Fixture):
    def test_no_configuration_at_all(self) -> None:
        report = self.observe()
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual((report["skills"], report["mcps"], report["unknown"]), ([], [], []))
        self.assertEqual(report["duplicates"], {"skills": [], "mcps": []})
        self.assertEqual(report["overlap_hints"], [])
        for client in ("claude", "codex", "cursor"):
            self.assertEqual(report["summary"]["per_client"][client],
                             {"skill_count": 0, "description_bytes_total": 0, "description_bytes_distinct_files": 0, "mcp_count": 0})

    def test_cli_is_read_only_json_and_exit_zero(self) -> None:
        before = sorted(path for path in self.root.rglob("*"))
        code, out, err = self.cli("--output", "json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["schema_version"], 1)
        code, text, _err = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("No Skill or MCP configuration found.", text)
        self.assertEqual(sorted(self.root.rglob("*")), before)

    def test_cli_text_follows_the_language(self) -> None:
        code, text, _err = self.cli("--lang", "zh")
        self.assertIn("没有发现任何", text)
        code, text, _err = self.cli("--lang", "en")
        self.assertIn("No Skill or MCP", text)


class SkillTests(Fixture):
    def test_description_bytes_and_hash(self) -> None:
        description = "说明 with 中文 and english words"
        self.skill(self.home / ".claude" / "skills", "alpha", description)
        item = self.observe()["skills"][0]
        raw = (self.home / ".claude" / "skills" / "alpha" / "SKILL.md").read_bytes()
        self.assertEqual(item["description_bytes"], len(description.encode()))
        self.assertEqual(item["skill_md_bytes"], len(raw))
        self.assertEqual(item["content_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual((item["client"], item["scope"], item["store"], item["path"]),
                         ("claude", "user", "~/.claude/skills", "~/.claude/skills/alpha"))
        self.assertEqual(item["frontmatter"], {"name": "alpha", "description": description})
        self.assertEqual((item["evidence"], item["origin"]["kind"], item["stale"]), ("file", "unknown", "unknown"))

    def test_block_scalar_description(self) -> None:
        text = "---\nname: folded\ndescription: >\n  first line\n  second line\n---\n"
        self.skill(self.home / ".claude" / "skills", "folded", text=text)
        self.assertEqual(self.observe()["skills"][0]["frontmatter"]["description"], "first line second line")

    def test_summary_counts_description_bytes_per_client(self) -> None:
        self.skill(self.home / ".claude" / "skills", "one", "x" * 10)
        self.skill(self.project / ".claude" / "skills", "two", "y" * 20)
        self.skill(self.home / ".codex" / "skills", "three", "z" * 7)
        summary = self.observe()["summary"]["per_client"]
        self.assertEqual(summary["claude"]["description_bytes_total"], 30)
        self.assertEqual(summary["claude"]["skill_count"], 2)
        self.assertEqual(summary["codex"]["description_bytes_total"], 7)

    def test_missing_stores_are_read(self) -> None:
        self.skill(self.home / ".agents" / "skills", "shared")
        self.skill(self.project / ".claude" / "skills", "proj")
        self.skill(self.project / ".agents" / "skills", "native")
        seen = {(item["client"], item["scope"], item["store"], item["name"]) for item in self.observe()["skills"]}
        self.assertIn(("codex", "user", "~/.agents/skills", "shared"), seen)
        self.assertIn(("cursor", "user", "~/.agents/skills", "shared"), seen)
        self.assertIn(("claude", "project", "<project>/.claude/skills", "proj"), seen)
        self.assertIn(("codex", "project", "<project>/.agents/skills", "native"), seen)
        self.assertNotIn("claude", {client for client, _s, store, name in seen if name == "shared"})

    def test_scope_and_type_and_client_filters(self) -> None:
        self.skill(self.home / ".claude" / "skills", "u")
        self.skill(self.project / ".claude" / "skills", "p")
        self.write(self.home / ".cursor" / "mcp.json", {"mcpServers": {"m": {"command": "node"}}})
        self.assertEqual([item["name"] for item in self.observe(scope="project")["skills"]], ["p"])
        self.assertEqual([item["name"] for item in self.observe(scope="user")["skills"]], ["u"])
        only_mcp = self.observe(types="mcp")
        self.assertEqual((only_mcp["skills"], len(only_mcp["mcps"])), ([], 1))
        self.assertEqual(self.observe(clients=["codex"])["clients"], ["codex"])
        self.assertEqual(self.observe(clients=["codex"])["skills"], [])

    def test_same_name_identical_and_different(self) -> None:
        self.skill(self.home / ".claude" / "skills", "same")
        self.skill(self.project / ".claude" / "skills", "same")
        self.skill(self.home / ".claude" / "skills", "diff", "one description for the first copy here")
        self.skill(self.project / ".claude" / "skills", "diff", "other words entirely in the second copy")
        groups = {item["name"]: item for item in self.observe()["duplicates"]["skills"]}
        self.assertEqual(groups["same"]["relation"], "identical")
        self.assertEqual(groups["diff"]["relation"], "different")
        self.assertEqual(groups["diff"]["effective"]["store"], "~/.claude/skills")  # Claude: personal over project
        self.assertEqual(groups["diff"]["effective"]["source_url"], inv.DOC_CLAUDE_SKILLS)

    def test_codex_and_cursor_same_name_make_no_precedence_claim(self) -> None:
        self.skill(self.home / ".agents" / "skills", "dup")
        self.skill(self.project / ".agents" / "skills", "dup", "something else with different words here ok")
        groups = {(item["client"], item["name"]): item for item in self.observe()["duplicates"]["skills"]}
        self.assertEqual(groups[("codex", "dup")]["effective"]["rule"], "both-load")
        cursor = groups[("cursor", "dup")]["effective"]
        self.assertEqual((cursor["store"], cursor["evidence"]), (None, "unknown"))

    def test_symlink_recorded_and_same_file_detected(self) -> None:
        real = self.skill(self.root / "elsewhere", "linked")
        (self.home / ".claude" / "skills").mkdir(parents=True)
        (self.home / ".claude" / "skills" / "linked").symlink_to(real)
        (self.project / ".claude").mkdir()
        (self.project / ".claude" / "skills").symlink_to(self.home / ".claude" / "skills")
        report = self.observe()
        item = next(entry for entry in report["skills"] if entry["scope"] == "user")
        self.assertTrue(item["symlink"]["is_symlink"])
        self.assertEqual(item["symlink"]["target"], str(real))
        self.assertEqual(report["duplicates"]["skills"][0]["relation"], "same-file")
        summary = report["summary"]["per_client"]["claude"]
        self.assertEqual(summary["description_bytes_total"], 2 * summary["description_bytes_distinct_files"])

    def test_broken_symlink_does_not_crash(self) -> None:
        (self.home / ".claude" / "skills").mkdir(parents=True)
        (self.home / ".claude" / "skills" / "gone").symlink_to(self.root / "missing")
        item = self.observe()["skills"][0]
        self.assertEqual((item["issues"], item["evidence"]), (["broken-symlink"], "unknown"))

    def test_broken_frontmatter_degrades(self) -> None:
        self.skill(self.home / ".claude" / "skills", "nofm", text="no frontmatter at all\n")
        self.skill(self.home / ".claude" / "skills", "nodesc", text="---\nname: nodesc\n---\n")
        self.skill(self.home / ".claude" / "skills", "renamed", text="---\nname: other\ndescription: d\n---\n")
        issues = {item["name"]: item["issues"] for item in self.observe()["skills"]}
        self.assertEqual(issues["nofm"], ["frontmatter-missing"])
        self.assertEqual(issues["nodesc"], ["description-missing"])
        self.assertEqual(issues["renamed"], ["name-mismatch"])

    def test_directories_without_skill_md_and_hidden_entries_are_not_skills(self) -> None:
        (self.home / ".claude" / "skills" / "empty").mkdir(parents=True)
        self.skill(self.home / ".claude" / "skills", ".system")
        self.assertEqual(self.observe()["skills"], [])

    def test_origin_and_staleness_only_for_recognised_agenthelm_plugins(self) -> None:
        def plugin(base: Path, version: str) -> None:
            self.write(base / "plugin.json", {"name": "agent-pack", "version": version, "repository": "https://github.com/lc4t/agenthelm"})
            self.skill(base / "skills", "agent-pack")
        plugin(self.root / "old", "1.0.0")
        plugin(self.home / ".agents" / "plugins" / "plugins" / "agent-pack", "9.9.9")
        (self.home / ".claude" / "skills").mkdir(parents=True)
        (self.home / ".claude" / "skills" / "agent-pack").symlink_to(self.root / "old" / "skills" / "agent-pack")
        self.skill(self.home / ".claude" / "skills", "stranger")
        items = {item["name"]: item for item in self.observe(clients=["claude"])["skills"]}
        self.assertEqual(items["agent-pack"]["origin"], {"kind": "agenthelm-plugin", "detail": "agent-pack@1.0.0"})
        self.assertEqual((items["agent-pack"]["version"], items["agent-pack"]["stale"]), ("1.0.0", True))
        self.assertEqual((items["stranger"]["origin"]["kind"], items["stranger"]["stale"]), ("unknown", "unknown"))

    def test_state_record_origin(self) -> None:
        state = self.home / ".claude" / "agent-pack" / "state" / "mypkg.json"
        self.write(state, {"schema_version": 1, "skills": ["linked"], "mcps": {"srv": {}}})
        real = self.skill(self.root / "pkg" / "skills", "linked")
        (self.home / ".claude" / "skills").mkdir(parents=True)
        (self.home / ".claude" / "skills" / "linked").symlink_to(real)
        self.write(self.home / ".claude.json", {"mcpServers": {"srv": {"command": "node"}}})
        report = self.observe(clients=["claude"])
        self.assertEqual(report["skills"][0]["origin"], {"kind": "state-record", "detail": "mypkg"})
        self.assertEqual(report["skills"][0]["stale"], "unknown")
        self.assertEqual(report["mcps"][0]["origin"], {"kind": "state-record", "detail": "mypkg"})

    def test_overlap_hint_fixed_samples(self) -> None:
        base = "Generate polished slide decks from an outline with themes charts and speaker notes"
        self.skill(self.home / ".claude" / "skills", "deck-a", base)
        self.skill(self.home / ".claude" / "skills", "deck-b", base + " quickly")
        self.skill(self.home / ".claude" / "skills", "unrelated", "Query a relational database and summarize slow queries for review")
        self.skill(self.home / ".claude" / "skills", "short-a", "make slides")
        self.skill(self.home / ".claude" / "skills", "short-b", "make slides")
        hints = self.observe()["overlap_hints"]
        self.assertEqual([hint["names"] for hint in hints], [["deck-a", "deck-b"]])
        self.assertTrue(hints[0]["hint_only"])
        self.assertGreaterEqual(hints[0]["similarity"], inv.OVERLAP_THRESHOLD)


class McpTests(Fixture):
    def claude_fixture(self) -> None:
        self.write(self.home / ".claude.json", {
            "mcpServers": {"srv": {"command": "npx", "args": ["-y", "pkg", "--api-key", SECRETS[0], "--token=" + SECRETS[2]],
                                   "env": {"API_TOKEN": SECRETS[1], "PLAIN": "value"}}},
            "projects": {str(self.project): {"mcpServers": {"srv": {"command": "npx", "args": ["local"]}}},
                         str(self.root / "other"): {"mcpServers": {"hidden": {"command": "x"}}}}})
        self.write(self.project / ".mcp.json", {"mcpServers": {
            "srv": {"type": "http", "url": "https://h.example/x?token=" + SECRETS[4] + "&v=${V:-dflt}",
                    "headers": {"Authorization": SECRETS[3]}}}})

    def test_secrets_never_appear_in_any_output(self) -> None:
        self.claude_fixture()
        self.write(self.home / ".cursor" / "mcp.json", {"mcpServers": {"c": {"url": "https://u:" + SECRETS[1] + "@h.example/p", "headers": {"X-Key": SECRETS[2]}}}})
        self.write(self.home / ".codex" / "config.toml",
                   f'[mcp_servers.x]\ncommand = "node"\nenv = {{ K = "{SECRETS[1]}" }}\nbearer_token_env_var = "TOK"\nhttp_headers = {{ A = "{SECRETS[3]}" }}\n')
        for text in (json.dumps(self.observe()), self.cli("--output", "json")[1], self.cli()[1], self.cli("--lang", "zh")[1]):
            for secret in SECRETS:
                self.assertNotIn(secret, text)
            self.assertNotIn("dflt", text)

    def test_names_only_for_env_and_headers(self) -> None:
        self.claude_fixture()
        items = {(item["scope"], item["name"]): item for item in self.observe()["mcps"]}
        user = items[("user", "srv")]
        self.assertEqual(user["env_names"], ["API_TOKEN", "PLAIN"])
        self.assertEqual(user["args"], ["-y", "pkg", "--api-key", "<redacted>", "--token=<redacted>"])
        self.assertEqual(items[("project", "srv")]["header_names"], ["Authorization"])
        self.assertEqual(items[("project", "srv")]["url"], "https://h.example/x?token=<redacted>&v=${V}")
        self.assertEqual(items[("project", "srv")]["transport"], "http")
        self.assertEqual(user["transport"], "stdio")

    def test_local_scope_reads_only_this_projects_entries(self) -> None:
        self.claude_fixture()
        names = {(item["scope"], item["name"]) for item in self.observe()["mcps"]}
        self.assertIn(("local", "srv"), names)
        self.assertNotIn(("local", "hidden"), names)
        self.assertNotIn(("local", "srv"), {(item["scope"], item["name"]) for item in self.observe(scope="user")["mcps"]})

    def test_claude_precedence_only_where_documented(self) -> None:
        self.claude_fixture()
        group = next(item for item in self.observe()["duplicates"]["mcps"] if item["kind"] == "same-name")
        self.assertEqual(group["effective"]["store"], "~/.claude.json#projects")
        self.assertEqual(group["effective"]["source_url"], inv.DOC_CLAUDE_MCP)
        self.assertEqual(group["relation"], "different")
        # project vs user: the winner depends on the .mcp.json approval, which this version does not read
        only = self.observe(scope="user")
        self.write(self.home / ".claude.json", {"mcpServers": {"srv": {"command": "npx"}}})
        group = next(item for item in self.observe()["duplicates"]["mcps"] if item["kind"] == "same-name")
        self.assertEqual((group["effective"]["store"], group["effective"]["evidence"]), (None, "unknown"))
        self.assertNotIn("removable", json.dumps(self.observe()))
        self.assertEqual(only["duplicates"]["mcps"], [])

    def test_cursor_and_codex_make_no_unknown_precedence_claim(self) -> None:
        self.write(self.home / ".cursor" / "mcp.json", {"mcpServers": {"s": {"command": "node", "args": ["a"]}}})
        self.write(self.project / ".cursor" / "mcp.json", {"mcpServers": {"s": {"command": "node", "args": ["a"]}}})
        self.write(self.home / ".codex" / "config.toml", '[mcp_servers.s]\ncommand = "node"\nargs = ["a"]\n')
        self.write(self.project / ".codex" / "config.toml", '[mcp_servers.s]\ncommand = "node"\nargs = ["b"]\n')
        groups = {item["client"]: item for item in self.observe()["duplicates"]["mcps"] if item["kind"] == "same-name"}
        self.assertEqual(groups["cursor"]["relation"], "identical")
        self.assertEqual(groups["cursor"]["effective"]["rule"], "precedence-undocumented")
        self.assertEqual(groups["codex"]["relation"], "different")
        self.assertIsNone(groups["codex"]["effective"]["store"])
        codex_project = next(item for item in self.observe()["mcps"] if item["client"] == "codex" and item["scope"] == "project")
        self.assertEqual(codex_project["notes"], ["codex-project-requires-trust"])

    def test_same_fingerprint_under_different_names(self) -> None:
        self.write(self.home / ".cursor" / "mcp.json", {"mcpServers": {
            "a": {"command": "uvx", "args": ["tool"]}, "b": {"command": "uvx", "args": ["tool"], "env": {"K": "v"}},
            "c": {"command": "uvx", "args": ["other"]}}})
        groups = [item for item in self.observe()["duplicates"]["mcps"] if item["kind"] == "same-fingerprint"]
        self.assertEqual([group["names"] for group in groups], [["a", "b"]])

    def test_transport_inference(self) -> None:
        self.write(self.home / ".cursor" / "mcp.json", {"mcpServers": {
            "sse": {"type": "sse", "url": "https://x/sse"}, "http": {"type": "streamable-http", "url": "https://x/m"},
            "bare": {"url": "https://x/b"}, "none": {}}})
        transports = {item["name"]: item["transport"] for item in self.observe()["mcps"]}
        self.assertEqual(transports, {"sse": "sse", "http": "http", "bare": "http", "none": "unknown"})

    def test_corrupt_files_become_unknown_not_a_crash(self) -> None:
        self.write(self.home / ".claude.json", "{ not json")
        self.write(self.project / ".mcp.json", "[1, 2]")
        self.write(self.home / ".codex" / "config.toml", "broken = [")
        self.write(self.project / ".codex" / "config.toml", "[mcp_servers]\nx = 1\n")
        self.write(self.home / ".cursor" / "mcp.json", {"mcpServers": []})
        report = self.observe()
        self.assertEqual(report["mcps"][:0], [])
        reasons = {(item["client"], item["store"]): item["reason"] for item in report["unknown"]}
        self.assertEqual(reasons[("claude", "~/.claude.json")], "invalid-json")
        self.assertEqual(reasons[("claude", "<project>/.mcp.json")], "unexpected-structure")
        self.assertEqual(reasons[("codex", "~/.codex/config.toml")], "invalid-toml")
        self.assertEqual(reasons[("cursor", "~/.cursor/mcp.json")], "unexpected-structure")
        code, out, err = self.cli("--output", "json")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(json.loads(out)["unknown"])
        self.assertIn("cannot be parsed", self.cli()[1])

    def test_claude_local_structure_that_cannot_be_read_is_unknown(self) -> None:
        self.write(self.home / ".claude.json", {"projects": {str(self.project): ["not", "an", "object"]}})
        report = self.observe(clients=["claude"])
        self.assertEqual([item["reason"] for item in report["unknown"]], ["claude-local-structure-unrecognized"])

    def test_json_keys_are_stable(self) -> None:
        self.claude_fixture()
        self.skill(self.home / ".claude" / "skills", "a")
        report = self.observe()
        self.assertEqual(list(report), ["schema_version", "generated_by", "project", "clients", "filters", "skills", "mcps",
                                        "summary", "duplicates", "overlap_hints", "unknown"])
        self.assertEqual(sorted(report["skills"][0]), sorted([
            "name", "client", "scope", "store", "path", "frontmatter", "description_bytes", "skill_md_bytes", "content_sha256",
            "origin", "symlink", "version", "stale", "evidence", "issues"]))
        self.assertEqual(sorted(report["mcps"][0]), sorted([
            "name", "client", "scope", "store", "transport", "command", "args", "url", "env_names", "header_names",
            "fingerprint", "origin", "evidence", "notes"]))


class ExistingInventoryUnchangedTests(Fixture):
    def test_inventory_client_and_doctor_keys_do_not_change(self) -> None:
        import agent_pack_config as core
        self.skill(self.home / ".agents" / "skills", "invisible")
        result = core.inventory_client("codex", self.home).to_dict()
        self.assertEqual(sorted(result), ["issues", "mcps", "plugins", "skills", "source"])
        self.assertEqual(result["skills"], [])  # the new reader does not leak into the old inventory
        code = subprocess.run([sys.executable, str(RUNTIME / "agent_pack_config.py"), "--output", "json", "inventory",
                               "--source", "client:codex", "--home", str(self.home)], capture_output=True, text=True, check=False)
        self.assertEqual(code.returncode, 0)
        self.assertEqual(sorted(json.loads(code.stdout)), ["issues", "mcps", "ok", "plugins", "skills", "source"])


if __name__ == "__main__":
    unittest.main()
