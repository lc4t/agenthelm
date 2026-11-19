"""``agenthelm project extensions move / dedupe / scope``：真实的 agenthelm relocate / dedupe 端到端。

每个用例在自己的临时 HOME 与项目里跑。覆盖：预览零写入、--apply 缺摘要、摘要不一致拒绝（零写入）、blocked 不可执行、
应用后读回、dedupe 分组只认 --list、scope 写 Profile 声明（先备份、原子写、放宽需 --allow-widen）、假令牌不进任何输出。
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AGENTHELM = ROOT / "plugins/agent-pack/runtime/agenthelm.py"
sys.path.insert(0, str(ROOT / "plugins/project-orchestrator/runtime"))
import project_ext_actions as actions  # noqa: E402

FAKE_SECRET = "FAKE_SECRET_VALUE_0002"


def write_skill(base: Path, name: str, body: str = "body") -> None:
    directory = base / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Demo {name}\n---\n{body}\n", encoding="utf-8")


def snapshot(*roots: Path) -> dict[str, str]:
    seen = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                seen[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return seen


class ActionCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.home, self.project = base / "home", base / "project"
        write_skill(self.home / ".claude/skills", "dup-same")
        write_skill(self.project / ".claude/skills", "dup-same")
        write_skill(self.home / ".claude/skills", "solo")
        profile = self.project / ".agents/moe.sakanano.agent-pack/project.json"
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({"schema_version": "2.0", "language": "en", "capabilities": {}}, indent=2) + "\n", encoding="utf-8")
        self.profile = profile
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {
            "dupm": {"command": "srv", "args": ["--token", FAKE_SECRET], "env": {"API_KEY": FAKE_SECRET}}},
            "projects": {str(self.project): {"mcpServers": {"dupm": {"command": "srv", "args": ["--local"]}}}}}), encoding="utf-8")
        self.env = {**{key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "AGENTHELM_", "CLAUDE", "CODEX"))},
                    "HOME": str(self.home), "AGENTHELM_LANG": "zh", "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"}

    def run_ext(self, *args: str) -> tuple[int, dict, str]:
        done = subprocess.run([sys.executable, str(AGENTHELM), "project", "--project", str(self.project), "--no-input", "extensions", *args,
                               "--output", "json"], input="", capture_output=True, text=True, env=self.env, timeout=180, check=False)
        self.assertNotIn(FAKE_SECRET, done.stdout + done.stderr)
        try:
            return done.returncode, json.loads(done.stdout), done.stderr
        except ValueError:
            return done.returncode, {}, done.stderr

    def run_text(self, *args: str) -> subprocess.CompletedProcess:
        done = subprocess.run([sys.executable, str(AGENTHELM), "project", "--project", str(self.project), "--no-input", "extensions", *args],
                              input="", capture_output=True, text=True, env=self.env, timeout=180, check=False)
        self.assertNotIn(FAKE_SECRET, done.stdout + done.stderr)
        return done

    def groups(self) -> list[dict]:
        done = subprocess.run([sys.executable, str(AGENTHELM), "dedupe", "--list", "--project", str(self.project), "--output", "json"],
                              capture_output=True, text=True, env=self.env, timeout=120, check=False)
        return json.loads(done.stdout)["groups"]

    def tree(self) -> dict[str, str]:
        return snapshot(self.home, self.project)


class MoveTests(ActionCase):
    MOVE = ("move", "skill:solo@claude:user", "--to", "codex:user")

    def test_preview_writes_nothing_and_shows_the_plan_as_is(self) -> None:
        before = self.tree()
        code, data, _err = self.run_ext(*self.MOVE)
        self.assertEqual(code, 0)
        self.assertEqual(self.tree(), before)
        d = data["data"]
        self.assertEqual((d["mode"], d["applied"], d["blocked"]), ("preview", None, False))
        self.assertEqual(len(d["plan"]["plan_digest"]), 64)
        self.assertTrue(all("promise" in step for step in d["plan"]["steps"]))

    def test_apply_needs_the_previewed_digest(self) -> None:
        before = self.tree()
        code, data, _err = self.run_ext(*self.MOVE, "--apply")
        self.assertEqual(code, 2)
        self.assertEqual(data["error"]["code"], "usage")
        self.assertEqual(self.tree(), before)

    def test_a_digest_mismatch_refuses_and_writes_nothing(self) -> None:
        before = self.tree()
        code, data, _err = self.run_ext(*self.MOVE, "--apply", "--expect-plan", "0" * 64)
        self.assertEqual(code, 1)
        self.assertEqual(data["data"]["refused"], "plan-changed")
        self.assertEqual(data["data"]["applied"]["status"], "refused")
        self.assertEqual(self.tree(), before)
        self.assertIn("重新预览", self.run_text(*self.MOVE, "--apply", "--expect-plan", "0" * 64).stdout)

    def test_apply_with_the_same_digest_writes_and_reads_back(self) -> None:
        digest = self.run_ext(*self.MOVE)[1]["data"]["plan"]["plan_digest"]
        code, data, _err = self.run_ext(*self.MOVE, "--apply", "--expect-plan", digest)
        self.assertEqual(code, 0, data)
        d = data["data"]
        self.assertEqual(d["applied"]["status"], "applied")
        self.assertTrue(d["readback"]["ok"])
        self.assertTrue((self.home / ".agents/skills/solo/SKILL.md").is_file())
        self.assertFalse((self.home / ".claude/skills/solo").exists())  # 源只归档、不删除
        self.assertTrue(d["applied"]["recovery_dir"])

    def test_a_blocked_plan_is_not_executable(self) -> None:
        argv = ("move", "skill:dup-same@claude:user", "--to", "claude:project", "--confirm-project-content")
        code, data, _err = self.run_ext(*argv)
        self.assertEqual(code, 1)
        self.assertTrue(data["data"]["blocked"])
        self.assertTrue(data["data"]["plan"]["blocked"])
        before = self.tree()
        code, data, _err = self.run_ext(*argv, "--apply", "--expect-plan", data["data"]["plan"]["plan_digest"])
        self.assertEqual((code, data["data"]["refused"]), (1, "blocked"))
        self.assertEqual(self.tree(), before)

    def test_user_to_project_needs_the_content_confirmation(self) -> None:
        _c, data, _e = self.run_ext("move", "skill:solo@claude:user", "--to", "claude:project")
        self.assertTrue(data["data"]["plan"]["confirmations"])
        digest = data["data"]["plan"]["plan_digest"]
        before = self.tree()
        code, data, _e = self.run_ext("move", "skill:solo@claude:user", "--to", "claude:project", "--apply", "--expect-plan", digest)
        self.assertEqual(code, 1)
        self.assertEqual(self.tree(), before)

    def test_a_bad_ref_is_a_usage_error(self) -> None:
        code, data, _err = self.run_ext("move", "solo", "--to", "codex:user")
        self.assertEqual((code, data["error"]["code"]), (2, "usage"))


class DedupeTests(ActionCase):
    def test_only_groups_from_dedupe_list_have_write_actions(self) -> None:
        code, data, _err = self.run_ext("dedupe", "g-0000000000", "--ignore")
        self.assertEqual(code, 1)
        self.assertTrue(data["data"]["unsupported"])
        self.assertIn("本版没有写入动作", data["data"]["message"])

    def test_retire_others_previews_then_applies_and_reads_back(self) -> None:
        group = next(item for item in self.groups() if item["key"] == "dup-same")
        argv = ("dedupe", group["id"], "--keep", "skill:dup-same@claude:user", "--retire-others")
        before = self.tree()
        code, data, _err = self.run_ext(*argv)
        self.assertEqual(code, 0, data)
        self.assertEqual(self.tree(), before)
        digest = data["data"]["plan"]["plan_digest"]
        code, data, _err = self.run_ext(*argv, "--apply", "--expect-plan", digest)
        self.assertEqual(code, 0, data)
        self.assertEqual(data["data"]["applied"]["status"], "applied")
        self.assertTrue(data["data"]["readback"]["ok"])
        self.assertFalse((self.project / ".claude/skills/dup-same").exists())

    def test_keep_all_records_only_the_machine_decision(self) -> None:
        group = next(item for item in self.groups() if item["key"] == "dup-same")
        argv = ("dedupe", group["id"], "--keep-all", "--note", "intentional")
        digest = self.run_ext(*argv)[1]["data"]["plan"]["plan_digest"]
        code, data, _err = self.run_ext(*argv, "--apply", "--expect-plan", digest)
        self.assertEqual(code, 0, data)
        self.assertTrue(data["data"]["readback"]["ok"])
        self.assertTrue((self.project / ".claude/skills/dup-same").exists())

    def test_digest_mismatch_refuses(self) -> None:
        group = next(item for item in self.groups() if item["key"] == "dup-same")
        before = self.tree()
        code, data, _err = self.run_ext("dedupe", group["id"], "--ignore", "--apply", "--expect-plan", "f" * 64)
        self.assertEqual((code, data["data"]["refused"]), (1, "plan-changed"))
        self.assertEqual(self.tree(), before)

    def test_mcp_group_is_manual_only_and_leaks_no_values(self) -> None:
        group = next(item for item in self.groups() if item["kind"] == "mcp")
        before = self.tree()
        code, data, _err = self.run_ext("dedupe", group["id"], "--keep", group["members"][0]["ref"], "--retire-others")
        self.assertIn(code, (0, 1))
        self.assertEqual(self.tree(), before)
        self.assertNotIn(FAKE_SECRET, json.dumps(data))

    def test_one_disposition_exactly(self) -> None:
        code, data, _err = self.run_ext("dedupe", "g-1", "--ignore", "--unignore")
        self.assertEqual((code, data["error"]["code"]), (2, "usage"))


class ScopeTests(ActionCase):
    ARGV = ("scope", "skill:solo@claude:user", "--effective", "project")

    def test_preview_shows_the_diff_and_a_pasteable_snippet_without_writing(self) -> None:
        before = self.tree()
        code, data, _err = self.run_ext(*self.ARGV)
        self.assertEqual(code, 0, data)
        self.assertEqual(self.tree(), before)
        plan = data["data"]["plan"]
        self.assertEqual((plan["direction"], plan["status"]), ("narrow", "ready"))
        self.assertEqual(plan["snippet"], {"capabilities": {"skill_scope": {"solo": "project"}}})
        self.assertIn('+      "solo": "project"', plan["diff"])
        self.assertNotIn("_updated", plan)
        self.assertIn("可粘贴的片段", self.run_text(*self.ARGV).stdout)

    def test_apply_backs_up_then_writes_the_declaration(self) -> None:
        original = self.profile.read_text(encoding="utf-8")
        digest = self.run_ext(*self.ARGV)[1]["data"]["plan"]["plan_digest"]
        code, data, _err = self.run_ext(*self.ARGV, "--apply", "--expect-plan", digest)
        self.assertEqual(code, 0, data)
        applied = data["data"]["applied"]
        self.assertEqual(applied["status"], "applied")
        profile = json.loads(self.profile.read_text(encoding="utf-8"))
        self.assertEqual(profile["capabilities"]["skill_scope"], {"solo": "project"})
        backup = Path(applied["recovery_dir"]).expanduser() / "project.json"
        self.assertEqual(backup.read_text(encoding="utf-8"), original)
        self.assertEqual([item.name for item in self.profile.parent.iterdir()], ["project.json"])  # 临时文件已清理

    def test_a_changed_profile_after_the_preview_is_refused(self) -> None:
        digest = self.run_ext(*self.ARGV)[1]["data"]["plan"]["plan_digest"]
        profile = json.loads(self.profile.read_text(encoding="utf-8"))
        profile["language"] = "zh"
        self.profile.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
        before = self.tree()
        code, data, _err = self.run_ext(*self.ARGV, "--apply", "--expect-plan", digest)
        self.assertEqual((code, data["data"]["refused"]), (1, "plan-changed"))
        self.assertEqual(self.tree(), before)

    def test_widening_needs_allow_widen(self) -> None:
        profile = json.loads(self.profile.read_text(encoding="utf-8"))
        profile["capabilities"]["skill_scope"] = {"solo": "project"}
        self.profile.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
        argv = ("scope", "skill:solo@claude:user", "--effective", "user")
        code, data, _err = self.run_ext(*argv)
        self.assertEqual(code, 1)
        self.assertEqual([item["code"] for item in data["data"]["plan"]["blocked"]], ["widen-needs-flag"])
        code, data, _err = self.run_ext(*argv, "--allow-widen")
        self.assertEqual((code, data["data"]["plan"]["direction"]), (0, "widen"))

    def test_the_same_scope_is_unchanged_and_an_unknown_entry_is_blocked(self) -> None:
        code, data, _err = self.run_ext("scope", "skill:solo@claude:user", "--effective", "user")
        self.assertEqual((code, data["data"]["plan"]["status"]), (0, "unchanged"))
        code, data, _err = self.run_ext("scope", "skill:nope@claude:user", "--effective", "project")
        self.assertEqual(code, 1)
        self.assertEqual(data["data"]["plan"]["blocked"][0]["code"], "entry-not-found")

    def test_mcp_scope_is_a_declaration_and_leaks_nothing(self) -> None:
        code, data, _err = self.run_ext("scope", "mcp:dupm@claude:user", "--effective", "project")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["data"]["plan"]["snippet"], {"capabilities": {"mcp_scope": {"dupm": "project"}}})

    def test_a_profile_without_capabilities_is_not_invented(self) -> None:
        self.profile.write_text(json.dumps({"schema_version": "2.0"}) + "\n", encoding="utf-8")
        code, data, _err = self.run_ext(*self.ARGV)
        self.assertEqual(code, 1)
        self.assertEqual(data["data"]["plan"]["blocked"][0]["code"], "profile-no-capabilities")


class ScrubTests(unittest.TestCase):
    def test_values_never_survive_the_scrub(self) -> None:
        cleaned = actions.scrub({"plan": {"steps": [{"id": "s1", "env": {"K": FAKE_SECRET}}], "headers": {"A": FAKE_SECRET},
                                           "members": [{"ref": "mcp:x@claude:user", "args": [FAKE_SECRET]}]}})
        self.assertNotIn(FAKE_SECRET, json.dumps(cleaned))
        self.assertEqual(cleaned["plan"]["members"], [{"ref": "mcp:x@claude:user"}])


if __name__ == "__main__":
    unittest.main()
