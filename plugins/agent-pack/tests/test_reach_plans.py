"""Impact and reachability in the previews of ``dedupe`` and ``relocate`` (#184 step 1), on temporary-HOME fixtures:
the reproduction of design section 1.2, digest behaviour, language independence, secrets, MCP, and a synced folder."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(RUNTIME))

from test_dedupe import FAKE_TOKEN, Base  # noqa: E402
from test_dedupe_mcp import KEEP, DedupeBase  # noqa: E402
from test_relocate_mcp import NAME, McpBase, SHAREABLE, USER_REF  # noqa: E402

PDF = "pdf-notes"
KEEP_SHARED = f"skill:{PDF}@codex:user#agents"


class Reproduction(Base):
    """``~/.agents/skills/<name>`` kept, ``<project>/.agents/skills/<name>`` retired; that project directory is a link to
    ``../skills`` and Claude Code's ``<project>/.claude/skills/<name>`` links to ``../../.agents/skills/<name>``."""

    def build(self) -> None:
        self.skill(self.home / ".agents" / "skills", PDF)
        self.skill(self.project / "skills", PDF)
        (self.project / ".agents").mkdir()
        os.symlink("../skills", self.project / ".agents" / "skills")
        (self.project / ".claude" / "skills").mkdir(parents=True)
        os.symlink(f"../../.agents/skills/{PDF}", self.project / ".claude" / "skills" / PDF)

    def codex_group(self) -> str:
        return next(g["id"] for g in self.groups() if g["client"] == "codex")

    def retire(self, *extra: str, lang: str = "en") -> dict:
        code, out, err = self.cli(self.codex_group(), "--keep", KEEP_SHARED, "--retire-others", "--output", "json", *extra, lang=lang)
        self.assertEqual(code, 0, err)
        return json.loads(out)


class SkillPreviewTests(Reproduction):
    def test_the_dangling_claude_link_is_reported_before_anything_is_written(self) -> None:
        self.build()
        before = self.snapshot()
        plan = self.retire()["plan"]
        self.assertEqual(self.snapshot(), before, "a preview writes nothing")
        self.assertEqual(plan["status"], "ready", "D-R3: the loss is a warning, never a block")
        self.assertEqual(plan["blocked"], [])
        impact = plan["impact"]
        self.assertEqual(impact["lost_clients"], ["claude"])
        self.assertEqual(impact["clients"]["claude"]["verdict"], "lost")
        self.assertEqual(impact["clients"]["claude"]["reasons"], ["link-target-retired"])
        self.assertEqual(impact["dangling_after"], [f"<project>/.claude/skills/{PDF}"])
        self.assertEqual({c: row["verdict"] for c, row in impact["clients"].items() if c != "claude"},
                         {"codex": "unchanged", "cursor": "unchanged"})
        codes = [w["code"] for w in plan["warnings"]]
        self.assertIn("impact-lost-client", codes)
        self.assertIn("impact-dangling-link", codes)
        lost = next(w for w in plan["warnings"] if w["code"] == "impact-lost-client")
        self.assertEqual((lost["client"], lost["ref"]), ("claude", f"skill:{PDF}@claude:project"))
        self.assertEqual((impact["basis"], impact["schema"]), ("file", 1))

    def test_apply_does_what_6_15_0_did_and_the_preview_was_right(self) -> None:
        self.build()
        digest = self.retire()["plan"]["plan_digest"]
        code, out, err = self.cli(self.codex_group(), "--keep", KEEP_SHARED, "--retire-others", "--output", "json", "--apply",
                                  "--expect-plan", digest)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["applied"]["status"], "applied")
        link = self.project / ".claude" / "skills" / PDF
        self.assertTrue(link.is_symlink() and not link.exists(), "the Claude Code link dangles, as the preview said")
        self.assertFalse((self.project / "skills" / PDF).exists())
        self.assertTrue((self.home / ".agents" / "skills" / PDF / "SKILL.md").is_file())
        code, out, err = self.cli("--list", "--output", "json")
        report = self.inventory_issues()
        self.assertEqual(report, ["broken-symlink"])

    def inventory_issues(self) -> list[str]:
        sys.path.insert(0, str(RUNTIME))
        import agent_pack_inventory as inv
        report = inv.observe(self.home, self.project, ["claude"], "skills", "all")
        return [issue for item in report["skills"] for issue in item["issues"]]

    def test_the_text_preview_lists_the_loss_in_both_languages(self) -> None:
        self.build()
        for lang, expect in (("zh", "影响面：退役后 Claude Code 将失去 技能「pdf-notes」"),
                             ("en", 'Impact: after this change Claude Code will lose skill "pdf-notes"')):
            code, out, err = self.cli(self.codex_group(), "--keep", KEEP_SHARED, "--retire-others", lang=lang)
            self.assertEqual(code, 0, err)
            self.assertIn(expect, out, lang)
            self.assertEqual(out.count(expect), 1, "the impact lines are printed once, not again as warnings")
            self.assertNotIn("注意：影响面", out)
        self.assertNotRegex(out, "[一-鿿]", "the English text has no Chinese left")

    def test_the_digest_does_not_depend_on_the_language(self) -> None:
        self.build()
        self.assertEqual(self.retire(lang="zh")["plan"]["plan_digest"], self.retire(lang="en")["plan"]["plan_digest"])
        self.assertNotEqual(json.dumps(self.retire(lang="zh")["plan"]["warnings"]), json.dumps(self.retire(lang="en")["plan"]["warnings"]))

    def test_a_copy_added_after_the_preview_changes_the_digest_and_apply_refuses(self) -> None:
        self.build()
        digest = self.retire()["plan"]["plan_digest"]
        self.skill(self.home / ".claude" / "skills", PDF)   # Claude Code now has a user-level copy
        after = self.retire()["plan"]
        self.assertNotEqual(after["plan_digest"], digest)
        self.assertEqual(after["impact"]["lost_clients"], [])
        self.assertEqual(after["impact"]["clients"]["claude"]["verdict"], "unchanged")
        before = self.snapshot()
        code, out, err = self.cli(self.codex_group(), "--keep", KEEP_SHARED, "--retire-others", "--output", "json", "--apply",
                                  "--expect-plan", digest)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["applied"]["reason"], "plan-digest-mismatch")
        self.assertEqual(self.snapshot(), before, "zero writes")

    def test_a_token_in_an_unrelated_configuration_never_reaches_the_output(self) -> None:
        self.build()
        self.write(self.home / ".claude.json", {"mcpServers": {"x": {"command": "npx", "args": ["-y", "x"], "env": {"API_KEY": FAKE_TOKEN}}}})
        self.write(self.project / ".mcp.json", {"mcpServers": {"y": {"command": "npx", "args": ["--token", FAKE_TOKEN]}}})
        for lang in ("en", "zh"):
            code, out, err = self.cli(self.codex_group(), "--keep", KEEP_SHARED, "--retire-others", "--output", "json", lang=lang)
            self.assertNotIn(FAKE_TOKEN, out + err)
            code, out, err = self.cli(self.codex_group(), "--keep", KEEP_SHARED, "--retire-others", lang=lang)
            self.assertNotIn(FAKE_TOKEN, out + err)

    def test_two_copies_that_only_differ_in_the_client_are_not_flagged(self) -> None:
        self.skill_pair()   # claude user + project, identical: retiring the project copy loses nothing
        group = next(g["id"] for g in self.groups())
        plan = self.preview(group, "--keep", "skill:dup@claude:user", "--retire-others")["plan"]
        self.assertEqual((plan["impact"]["lost_clients"], plan["impact"]["clients"]["claude"]["verdict"]), ([], "unchanged"))
        self.assertEqual([w for w in plan["warnings"] if w["code"].startswith("impact-")], [])

    def test_a_user_level_retirement_says_it_affects_every_project(self) -> None:
        self.skill_pair()
        group = next(g["id"] for g in self.groups())
        plan = self.preview(group, "--keep", "skill:dup@claude:project", "--retire-others")["plan"]
        self.assertEqual(plan["impact"]["scope_notes"], ["affects-all-projects"])
        code, out, err = self.cli(group, "--keep", "skill:dup@claude:project", "--retire-others", lang="en")
        self.assertIn("A user-level entry affects every project", out)

    def test_other_decisions_carry_no_impact(self) -> None:
        self.skill_pair()
        group = next(g["id"] for g in self.groups())
        for flag, extra in (("--keep-all", ("--note", "intended")), ("--ignore", ())):
            plan = self.preview(group, flag, *extra)["plan"]
            self.assertNotIn("impact", plan, flag)


class RelocateCli(Base):
    def relocate(self, ref: str, to: str, *args: str, lang: str = "en") -> tuple[int, str, str]:
        import subprocess
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENTHELM_", "XDG_"))}
        env.update(HOME=str(self.home), AGENTHELM_LANG=lang)
        proc = subprocess.run([sys.executable, str(RUNTIME / "agenthelm.py"), "relocate", ref, "--to", to, "--home", str(self.home),
                               "--project", str(self.project), *args], capture_output=True, text=True, env=env, check=False)
        return proc.returncode, proc.stdout, proc.stderr

    def plan(self, ref: str, to: str, *args: str) -> dict:
        code, out, err = self.relocate(ref, to, "--output", "json", *args)
        self.assertIn(code, (0, 1), err)
        return json.loads(out)["plan"]


class SyncedFolderTests(RelocateCli):
    """The behaviour of relative symlinks inside synced folders is not confirmed: the impact describes this machine's files
    only, and ``relocate`` keeps its existing ``sync-folder-demoted`` warning."""

    def test_the_impact_stays_a_local_file_statement_under_a_sync_folder(self) -> None:
        sync = self.root / "Dropbox" / "agent-skills"
        self.skill(sync, "shared")
        (self.home / ".claude" / "skills").mkdir(parents=True)
        os.symlink(os.path.relpath(sync / "shared", self.home / ".claude" / "skills"), self.home / ".claude" / "skills" / "shared")
        plan = self.plan("skill:shared@claude:user", "codex:user", "--mode", "link")
        self.assertIn("sync-folder-demoted", [w["code"] for w in plan["warnings"]])
        self.assertEqual(plan["impact"]["basis"], "file")
        text = json.dumps(plan["impact"])
        for claim in ("sync", "other-machine", "dropbox", "Dropbox"):
            self.assertNotIn(claim, text, "no conclusion about the synced copy")


class RelocateSkillTests(RelocateCli):
    def test_a_cross_client_move_loses_at_the_source_and_gains_at_every_reader_of_the_target(self) -> None:
        self.skill(self.project / ".claude" / "skills", "demo")
        plan = self.plan("skill:demo@claude:project", "codex:project")
        impact = plan["impact"]
        self.assertEqual(impact["clients"]["claude"]["verdict"], "lost")
        self.assertEqual(impact["clients"]["codex"]["verdict"], "gained")
        self.assertEqual(impact["clients"]["cursor"]["verdict"], "gained", "Cursor reads the shared .agents/skills too")
        self.assertEqual(impact["clients"]["codex"]["after"], ["skill:demo@codex:project"])
        self.assertEqual(impact["clients"]["cursor"]["after"], ["skill:demo@cursor:project#agents"])
        self.assertEqual(impact["scope_notes"], [])
        self.assertEqual(plan["status"], "ready")

    def test_keeping_the_source_loses_nothing(self) -> None:
        self.skill(self.project / ".claude" / "skills", "demo")
        impact = self.plan("skill:demo@claude:project", "codex:project", "--keep-source")["impact"]
        self.assertEqual((impact["lost_clients"], impact["clients"]["claude"]["verdict"]), ([], "unchanged"))
        self.assertEqual(impact["clients"]["codex"]["verdict"], "gained")

    def test_project_to_user_adds_the_other_machines_note(self) -> None:
        self.skill(self.project / ".claude" / "skills", "demo")
        plan = self.plan("skill:demo@claude:project", "claude:user", "--allow-widen")
        self.assertEqual(plan["impact"]["scope_notes"], ["other-machines-lose-it"])
        self.assertIn("impact-other-machines", [w["code"] for w in plan["warnings"]])
        code, out, err = self.relocate("skill:demo@claude:project", "claude:user", "--allow-widen", lang="en")
        self.assertIn("projects on other machines will no longer carry it", out)
        keep = self.plan("skill:demo@claude:project", "claude:user", "--allow-widen", "--keep-source")
        self.assertEqual(keep["impact"]["scope_notes"], ["other-machines-lose-it"])
        user_to_project = self.plan("skill:demo@claude:project", "claude:project")   # same place: blocked, nothing computed
        self.assertNotIn("impact", user_to_project)

    def test_user_to_project_has_no_other_machines_note(self) -> None:
        self.skill(self.home / ".claude" / "skills", "demo")
        impact = self.plan("skill:demo@claude:user", "claude:project")["impact"]
        self.assertEqual(impact["scope_notes"], ["affects-all-projects"])

    def test_a_native_plan_and_a_blocked_plan_have_no_impact(self) -> None:
        self.skill(self.home / ".agents" / "skills", "demo")
        native = self.plan("skill:demo@codex:user#agents", "cursor:user")
        self.assertEqual(native["status"], "native")   # Cursor already reads ~/.agents/skills: zero writes, nothing to compute
        self.assertNotIn("impact", native)
        self.skill(self.home / ".claude" / "skills", "demo")
        blocked = self.plan("skill:demo@claude:user", "claude:user")
        self.assertNotIn("impact", blocked)


class McpPlanTests(DedupeBase):
    def impact(self, keep: str) -> dict:
        code, out = self.dedupe(self.group(), "--keep", keep, "--retire-others", "--output", "json")
        self.assertIn(code, (0, 1), out)
        return json.loads(out)["plan"]

    def test_retiring_the_user_copy_leaves_only_the_project_file_whose_approval_is_unread(self) -> None:
        self.setup_pair(True)
        plan = self.impact(KEEP)
        row = plan["impact"]["clients"]["claude"]
        self.assertEqual((row["verdict"], row["reasons"]), ("uncertain", ["project-approval-unknown"]))
        self.assertEqual(plan["impact"]["scope_notes"], ["affects-all-projects"])
        self.assertEqual(plan["status"], "ready")
        self.assertIn("impact-uncertain", [w["code"] for w in plan["warnings"]])
        self.assertNotIn(FAKE_TOKEN, json.dumps(plan))

    def test_retiring_the_project_copy_keeps_the_user_one(self) -> None:
        self.setup_pair(True)
        plan = self.impact(f"mcp:{NAME}@claude:user")
        self.assertEqual(plan["impact"]["clients"]["claude"]["verdict"], "unchanged")
        self.assertEqual(plan["impact"]["lost_clients"], [])


class McpRelocateTests(McpBase):
    def test_a_user_entry_moved_to_local_keeps_claude_whole_and_is_unchanged(self) -> None:
        self.write_config()
        self.own("user")
        plan = self.preview(USER_REF, "claude:local")["plan"]
        self.assertEqual(plan["impact"]["clients"]["claude"]["verdict"], "unchanged")
        self.assertEqual(plan["impact"]["scope_notes"], ["affects-all-projects"])

    def test_a_project_file_entry_moved_to_user_level_notes_the_other_machines(self) -> None:
        self.write_config({})
        (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": {NAME: SHAREABLE}}), encoding="utf-8")
        plan = self.preview(f"mcp:{NAME}@claude:project", "claude:user", "--allow-widen")["plan"]
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["impact"]["scope_notes"], ["other-machines-lose-it"])
        row = plan["impact"]["clients"]["claude"]
        self.assertEqual(row["verdict"], "unchanged", "the user-level copy needs no approval")
        self.assertIn("impact-other-machines", [w["code"] for w in plan["warnings"]])

    def test_the_digest_of_an_mcp_preview_does_not_depend_on_the_language(self) -> None:
        self.write_config()
        self.own("user")
        digests = {json.loads(self.reloc(USER_REF, "claude:local", "--output", "json", lang=lang)[1])["plan"]["plan_digest"]
                   for lang in ("en", "zh")}
        self.assertEqual(len(digests), 1)


if __name__ == "__main__":
    unittest.main()
