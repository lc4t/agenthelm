"""`agenthelm dedupe`: duplicate-group handling, decision file, MCP manual-only output, Desktop snippet."""
from __future__ import annotations

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

import agent_pack_dedupe as dedupe  # noqa: E402
import agent_pack_desktop as desktop  # noqa: E402
import agent_pack_ext_common as ext  # noqa: E402

FAKE_TOKEN = "sk-abcdefghijklmnopqrstuv"
ENV_SECRET = "hunter2-fake-env-secret"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.project = self.root / "proj"
        self.home.mkdir()
        self.project.mkdir()

    def skill(self, base: Path, name: str, text: str = "same") -> Path:
        directory = base / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: A duplicate skill used by the dedupe tests\n---\n{text}\n", encoding="utf-8")
        return directory

    def write(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def cli(self, *args: str, lang: str = "en") -> tuple[int, str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("AGENTHELM_", "XDG_"))}
        env.update(HOME=str(self.home), AGENTHELM_LANG=lang)
        proc = subprocess.run([sys.executable, str(RUNTIME / "agenthelm.py"), "dedupe", "--home", str(self.home),
                               "--project", str(self.project), *args], capture_output=True, text=True, env=env, check=False)
        return proc.returncode, proc.stdout, proc.stderr

    def groups(self) -> list[dict]:
        code, out, err = self.cli("--list", "--output", "json")
        self.assertEqual(0, code, err)
        return json.loads(out)["groups"]

    def preview(self, group: str, *args: str) -> dict:
        code, out, err = self.cli(group, "--output", "json", *args)
        self.assertIn(code, (0, 1), err)
        return json.loads(out)

    def apply(self, group: str, *args: str, digest: str | None = None) -> tuple[int, dict]:
        digest = digest or self.preview(group, *args)["plan"]["plan_digest"]
        code, out, err = self.cli(group, "--output", "json", "--apply", "--expect-plan", digest, *args)
        self.assertIn(code, (0, 1), err)
        return code, json.loads(out)

    def snapshot(self) -> dict[str, str]:
        found = {}
        for base in (self.home, self.project):
            for path in sorted(base.rglob("*")):
                found[str(path)] = "L:" + os.readlink(path) if path.is_symlink() else (
                    "D" if path.is_dir() else path.read_text(encoding="utf-8", errors="replace"))
        return found

    def skill_pair(self, text_user: str = "same", text_project: str = "same") -> tuple[Path, Path]:
        return (self.skill(self.home / ".claude" / "skills", "dup", text_user),
                self.skill(self.project / ".claude" / "skills", "dup", text_project))

    def mcp_pair(self) -> None:
        self.write(self.home / ".claude.json", {"mcpServers": {"srv": {
            "command": "npx", "args": ["-y", "server-x"], "env": {"API_KEY": ENV_SECRET}}}})
        self.write(self.project / ".mcp.json", {"mcpServers": {"srv": {"command": "npx", "args": ["-y", "server-x"]}}})

    def gid(self, kind: str) -> str:
        return next(item["id"] for item in self.groups() if item["kind"] == kind)


class SkillRetireTests(Base):
    def test_list_and_preview_write_nothing(self) -> None:
        self.skill_pair()
        before = self.snapshot()
        group = self.groups()[0]
        self.assertEqual("skill", group["kind"])
        self.assertTrue(group["id"].startswith("g-"))
        plan = self.preview(group["id"], "--keep", "skill:dup@claude:user", "--retire-others")["plan"]
        self.assertEqual("ready", plan["status"])
        self.assertEqual(["skill:dup@claude:project"], [item["ref"] for item in plan["retire"]])
        self.assertTrue(all(step["promise"] in ("certain", "best-effort") for step in plan["steps"]))
        self.assertEqual(before, self.snapshot())

    def test_group_id_is_stable_when_other_entries_appear(self) -> None:
        self.skill_pair()
        first = self.groups()[0]["id"]
        self.skill(self.home / ".codex" / "skills", "other")
        self.assertEqual(first, [g for g in self.groups() if g["key"] == "dup"][0]["id"])

    def test_retire_project_copy_archives_and_keeps_user_copy(self) -> None:
        user, project = self.skill_pair()
        group = self.groups()[0]["id"]
        code, document = self.apply(group, "--keep", "skill:dup@claude:user", "--retire-others")
        self.assertEqual(0, code, document)
        self.assertTrue((user / "SKILL.md").is_file())
        self.assertFalse(project.exists())
        archived = next((self.home / ".agent-pack" / "recovery").glob("*-dedupe-*/skills/dup/SKILL.md"))
        self.assertTrue(archived.is_file())
        self.assertIn("mv ", document["applied"]["restore"][0])

    def test_retire_user_copy_uses_the_same_archive_layout_as_reconcile(self) -> None:
        user, project = self.skill_pair()
        group = self.groups()[0]["id"]
        self.apply(group, "--keep", "skill:dup@claude:project", "--retire-others")
        self.assertTrue(project.exists())
        self.assertFalse(user.exists())
        self.assertTrue(next((self.home / ".agent-pack" / "recovery").glob("*-dedupe-*/skills/dup")).is_dir())

    def test_digest_mismatch_and_missing_digest_refuse(self) -> None:
        user, project = self.skill_pair()
        group = self.groups()[0]["id"]
        before = self.snapshot()
        code, document = self.apply(group, "--keep", "skill:dup@claude:user", "--retire-others", digest="f" * 64)
        self.assertEqual("plan-digest-mismatch", document["applied"]["reason"])
        self.assertEqual(before, self.snapshot())
        rc, _out, err = self.cli(group, "--keep", "skill:dup@claude:user", "--retire-others", "--apply")
        self.assertEqual(2, rc)

    def test_unknown_evidence_copy_cannot_be_retired(self) -> None:
        user, project = self.skill_pair()
        (project / "SKILL.md").write_bytes(b"x" * (5 * 1024 * 1024))  # too large: evidence becomes unknown
        group = self.groups()[0]["id"]
        plan = self.preview(group, "--keep", "skill:dup@claude:user", "--retire-others")["plan"]
        self.assertEqual("evidence-unknown", plan["blocked"][0]["code"])
        before = self.snapshot()
        code, document = self.apply(group, "--keep", "skill:dup@claude:user", "--retire-others")
        self.assertEqual(1, code)
        self.assertEqual(before, self.snapshot())

    def test_keep_must_be_in_group_and_flags_are_exclusive(self) -> None:
        self.skill_pair()
        group = self.groups()[0]["id"]
        self.assertEqual("keep-not-in-group", self.preview(group, "--keep", "skill:zzz@claude:user", "--retire-others")["plan"]["blocked"][0]["code"])
        self.assertEqual("choose-one-disposition", self.preview(group, "--keep-all", "--ignore")["plan"]["blocked"][0]["code"])
        self.assertEqual("group-not-found", self.preview("g-nope", "--ignore")["plan"]["blocked"][0]["code"])

    def test_failure_rolls_back_archived_copies(self) -> None:
        user, project = self.skill_pair()
        group_id = self.groups()[0]["id"]
        plan, ctx = dedupe.build_plan(self.home, self.project, group_id, "skill:dup@claude:user", True, False, False, False, None)
        with mock.patch.object(dedupe, "_kept_still_intact", lambda _ctx: False):
            applied = dedupe.apply_plan(plan, ctx)
        self.assertEqual("rolled-back", applied["status"])
        self.assertTrue((project / "SKILL.md").is_file(), "archived copy moved back")
        self.assertTrue((user / "SKILL.md").is_file())

    def test_keep_that_points_into_a_retired_directory_is_blocked(self) -> None:
        real = self.skill(self.home / ".agents" / "skills", "dup")
        link_dir = self.home / ".codex" / "skills"
        link_dir.mkdir(parents=True)
        os.symlink(real, link_dir / "dup")
        plan = None
        for group in self.groups():
            if group["key"] == "dup":
                plan = self.preview(group["id"], "--keep", "skill:dup@codex:user", "--retire-others")["plan"]
        self.assertIsNotNone(plan)
        self.assertEqual("keep-depends-on-retired", plan["blocked"][0]["code"])


class DecisionTests(Base):
    def test_keep_all_writes_only_the_private_decision_file(self) -> None:
        self.skill_pair()
        group = self.groups()[0]["id"]
        user_skill = self.home / ".claude" / "skills" / "dup" / "SKILL.md"
        content = user_skill.read_text()
        code, document = self.apply(group, "--keep-all", "--note", "intentional override")
        self.assertEqual(0, code, document)
        path = self.home / ".config" / "agenthelm" / "ext-decisions.json"
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        data = json.loads(path.read_text())
        entry = data["decisions"][group]
        self.assertEqual({"decision", "note", "kind", "client", "key", "members_digest", "at"}, set(entry))
        self.assertEqual("keep-all", entry["decision"])
        self.assertEqual(content, user_skill.read_text())
        self.assertFalse((self.home / ".agent-pack").exists())
        listed = self.groups()[0]["decision"]
        self.assertEqual("keep-all", listed["decision"])
        self.assertFalse(listed["stale"])

    def test_ignore_then_unignore(self) -> None:
        self.skill_pair()
        group = self.groups()[0]["id"]
        self.assertEqual("not-ignored", self.preview(group, "--unignore")["plan"]["blocked"][0]["code"])
        self.apply(group, "--ignore")
        self.assertEqual("ignore", self.groups()[0]["decision"]["decision"])
        code, _document = self.apply(group, "--unignore")
        self.assertEqual(0, code)
        self.assertIsNone(self.groups()[0]["decision"])

    def test_keep_all_needs_a_note_and_rejects_secret_like_notes(self) -> None:
        self.skill_pair()
        group = self.groups()[0]["id"]
        self.assertEqual("note-required", self.preview(group, "--keep-all")["plan"]["blocked"][0]["code"])
        self.assertEqual("note-looks-like-secret", self.preview(group, "--keep-all", "--note", f"token {FAKE_TOKEN}")["plan"]["blocked"][0]["code"])
        self.assertEqual("note-too-long", self.preview(group, "--keep-all", "--note", "x " * 150)["plan"]["blocked"][0]["code"])
        self.assertFalse((self.home / ".config").exists())

    def test_decision_goes_stale_when_membership_changes(self) -> None:
        self.skill(self.home / ".codex" / "skills", "dup")
        self.skill(self.project / ".agents" / "skills", "dup")
        group = self.groups()[0]["id"]
        self.apply(group, "--ignore")
        self.skill(self.home / ".agents" / "skills", "dup")  # a third copy appears for the same client
        listed = [g for g in self.groups() if g["id"] == group]
        self.assertTrue(listed and listed[0]["decision"]["stale"])

    def test_decision_preview_is_zero_write(self) -> None:
        self.skill_pair()
        before = self.snapshot()
        self.preview(self.groups()[0]["id"], "--keep-all", "--note", "ok")
        self.assertEqual(before, self.snapshot())


class McpTests(Base):
    def test_mcp_retire_is_manual_only_and_never_touches_config(self) -> None:
        self.mcp_pair()
        group = self.gid("mcp")
        before = self.snapshot()
        document = self.preview(group, "--keep", "mcp:srv@claude:user", "--retire-others")
        plan = document["plan"]
        self.assertEqual("manual-only", plan["status"])
        self.assertEqual([], plan["retire"])
        manual = plan["manual"][0]
        self.assertTrue(manual["not_archivable_by_tool"])
        self.assertEqual("claude mcp remove --scope project srv", manual["command"])
        self.assertIn("cp ~/.claude.json", manual["backup"])
        self.assertTrue(all(step["promise"] == "cannot-promise" for step in plan["steps"]))
        code, applied = self.apply(group, "--keep", "mcp:srv@claude:user", "--retire-others")
        self.assertEqual(0, code)
        self.assertEqual("manual-only", applied["applied"]["status"])
        self.assertEqual(before, self.snapshot())

    def test_unknown_evidence_mcp_is_read_only(self) -> None:
        self.mcp_pair()
        self.write(self.project / ".mcp.json", '{"mcpServers": {"srv": ["bad"]}}')
        groups = [g for g in self.groups() if g["kind"] == "mcp"]
        if groups:  # an unusable entry may drop out of the group; if it stays it must be read-only
            plan = self.preview(groups[0]["id"], "--keep", groups[0]["members"][0]["ref"], "--retire-others")["plan"]
            self.assertEqual([], plan["retire"])

    def test_fake_secrets_never_reach_any_output_or_file(self) -> None:
        self.write(self.home / ".claude.json", {"mcpServers": {"srv": {
            "command": "npx", "args": ["-y", "server-x", FAKE_TOKEN], "env": {"API_KEY": ENV_SECRET},
            "headers": {"Authorization": f"Bearer {ENV_SECRET}"}}}})
        self.write(self.project / ".mcp.json", {"mcpServers": {"srv": {"command": "npx", "args": ["-y", "server-x", FAKE_TOKEN]}}})
        group = self.gid("mcp")
        blob = ""
        for args in (("--list",), ("--list", "--output", "json"), (group, "--keep", "mcp:srv@claude:user", "--retire-others"),
                     (group, "--keep", "mcp:srv@claude:user", "--retire-others", "--output", "json"),
                     (group, "--keep", "mcp:srv@claude:user", "--desktop-snippet"),
                     (group, "--keep", "mcp:srv@claude:project", "--desktop-snippet", "--output", "json")):
            _code, out, err = self.cli(*args)
            blob += out + err
        digest = self.preview(group, "--keep-all", "--note", "intentional")["plan"]["plan_digest"]
        self.cli(group, "--keep-all", "--note", "intentional", "--apply", "--expect-plan", digest)
        for path in (self.home / ".config").rglob("*"):
            if path.is_file():
                blob += path.read_text()
        for secret in (FAKE_TOKEN, ENV_SECRET):
            self.assertNotIn(secret, blob)

    def test_same_fingerprint_in_other_client_is_advisory_keep(self) -> None:
        self.mcp_pair()
        self.write(self.home / ".cursor" / "mcp.json", {"mcpServers": {"srv": {"command": "npx", "args": ["-y", "server-x"]}}})
        plan = self.preview(self.gid("mcp"), "--keep", "mcp:srv@claude:user", "--retire-others")["plan"]
        codes = [item["code"] for item in plan["warnings"]]
        self.assertIn("cross-client-keep-recommended", codes)


class DesktopSnippetTests(Base):
    def test_snippet_has_path_placeholders_and_restart_hint_and_writes_nothing(self) -> None:
        self.mcp_pair()
        self.write(self.home / ".claude.json", {"mcpServers": {"srv": {
            "command": "npx", "args": ["-y", "server-x"], "env": {"API_KEY": "${MY_KEY}"}}}})
        before = self.snapshot()
        document = json.loads(self.cli(self.gid("mcp"), "--keep", "mcp:srv@claude:user", "--desktop-snippet", "--output", "json")[1])
        desk = document["desktop"]
        self.assertEqual({"mcpServers": {"srv": {"command": "npx", "args": ["-y", "server-x"], "env": {"API_KEY": "<fill in your API_KEY>"}}}},
                         desk["snippet"])
        self.assertEqual("cannot-promise", desk["promise"])
        self.assertTrue(desk["path"].endswith("claude_desktop_config.json"))
        _code, text, _err = self.cli(self.gid("mcp"), "--keep", "mcp:srv@claude:user", "--desktop-snippet")
        self.assertIn("quit and restart Claude Desktop", text)
        self.assertEqual(before, self.snapshot())

    def test_inline_secret_entry_is_blocked_without_snippet(self) -> None:
        self.write(self.home / ".claude.json", {"mcpServers": {"srv": {
            "command": "npx", "args": ["x", "--token", FAKE_TOKEN], "env": {"API_KEY": ENV_SECRET}}}})
        self.write(self.project / ".mcp.json", {"mcpServers": {"srv": {"command": "npx"}}})
        document = json.loads(self.cli(self.gid("mcp"), "--keep", "mcp:srv@claude:user", "--desktop-snippet", "--output", "json")[1])
        desk = document["desktop"]
        self.assertIsNone(desk["snippet"])
        self.assertEqual("inline-secret", desk["blocked"][0]["code"])

    def test_paths_per_platform_and_remote_is_unconfirmed(self) -> None:
        self.assertEqual("~/Library/Application Support/Claude/claude_desktop_config.json", desktop.config_path("darwin"))
        self.assertEqual("~/.config/Claude/claude_desktop_config.json", desktop.config_path("linux"))
        self.assertIsNone(desktop.config_path("win32"))
        entry = {"ref": "mcp:r@claude:user", "name": "r", "evidence": "file", "has_inline_secret": False, "transport": "http",
                 "command": None, "args": [], "url": "https://example.test/mcp", "env_names": [], "header_names": ["X-Key"]}
        built = desktop.build(entry, "linux")
        self.assertIn("remote-unconfirmed", [item["code"] for item in built["notes"]])
        self.assertIn("headers-not-included", [item["code"] for item in built["notes"]])
        self.assertEqual({"mcpServers": {"r": {"url": "https://example.test/mcp"}}}, built["snippet"])


class LanguageTests(Base):
    def test_english_output_has_no_chinese_and_digest_is_language_independent(self) -> None:
        self.skill_pair()
        group = self.groups()[0]["id"]
        _c, english, _e = self.cli(group, "--keep", "skill:dup@claude:user", "--retire-others", lang="en")
        _c, chinese, _e = self.cli(group, "--keep", "skill:dup@claude:user", "--retire-others", lang="zh")
        self.assertNotRegex(english, r"[一-鿿]")
        self.assertIn("计划指纹", chinese)
        digests = [self.preview(group, "--keep", "skill:dup@claude:user", "--retire-others", "--lang", lang)["plan"]["plan_digest"]
                   for lang in ("zh", "en")]
        self.assertEqual(digests[0], digests[1])
        _c, listing, _e = self.cli("--list", lang="en")
        self.assertNotRegex(listing, r"[一-鿿]")


if __name__ == "__main__":
    unittest.main()
