"""#67 / #82 I-4 (step 3): project-level Skills (``.claude/skills`` relative links, copies in sync folders), scope
migration previews and advisory shadow detection."""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
RUNTIME = TESTS.parent / "runtime"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(RUNTIME))

import agent_pack_native_package as claude  # noqa: E402
import agent_pack_config as core  # noqa: E402
from test_agent_pack_config import make_project  # noqa: E402
from test_project_store_write import older_runtime_runs  # noqa: E402
from test_scope_declaration import MCP, SKILL, ScopeBase, declare  # noqa: E402
from test_sync_preview_parity import snapshot  # noqa: E402

REL = f"../../.agents/skills/{SKILL}"


def doctor_cli(test: ScopeBase):
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, str(RUNTIME / "agent_pack_config.py"), "--output", "json", "doctor",
                           "--project", str(test.project), "--client", "claude", "--home", str(test.home)],
                          capture_output=True, text=True, env=env, check=False)
    return proc.returncode, json.loads(proc.stdout), proc.stdout


class SkillBase(ScopeBase):
    def setUp(self) -> None:
        super().setUp()
        declare(self.project, skill_scope={SKILL: "project"})

    @property
    def link(self) -> Path:
        return self.project / ".claude" / "skills" / SKILL

    @property
    def user_skill(self) -> Path:
        return self.home / ".claude" / "skills" / SKILL


class LinkTests(SkillBase):
    def test_relative_symlink_into_the_project_sources(self) -> None:
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(os.readlink(self.link), REL)
        self.assertTrue((self.link / "SKILL.md").is_file())
        self.assertFalse(self.user_skill.exists() or self.user_skill.is_symlink(), "never the user level")
        op = next(item for item in payload["operations"] if item["action"] == "link-project-skill")
        self.assertEqual((op["scope"], op["store"], op["mode"], op["target"]), ("project", "project-skills", "link", REL))
        self.assertEqual(self.state()["scopes"]["skills"], {SKILL: "project:link"})

    def test_the_link_survives_moving_the_project(self) -> None:
        self.cli("--apply")
        moved = self.root / "elsewhere" / "renamed"
        moved.parent.mkdir()
        shutil.move(str(self.project), str(moved))
        self.assertTrue((moved / ".claude" / "skills" / SKILL / "SKILL.md").is_file())

    def test_second_apply_keeps_and_a_hand_made_link_to_the_same_source_is_accepted(self) -> None:
        self.cli("--apply")
        rc, payload, out = self.cli("--apply")
        self.assertEqual([item["action"] for item in payload["operations"] if item["name"] == SKILL],
                         ["keep-project-skill"])
        self.link.unlink()
        os.symlink(REL, self.link)
        rc, payload, out = self.cli()
        self.assertEqual([item["action"] for item in payload["operations"] if item["name"] == SKILL],
                         ["keep-project-skill"])

    def test_a_foreign_directory_blocks_until_replace_skills(self) -> None:
        self.link.mkdir(parents=True)
        (self.link / "SKILL.md").write_text("---\nname: sample-skill\ndescription: mine\n---\nmine\n")
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"project-skill {SKILL}", payload["error"])
        self.assertFalse(self.link.is_symlink())
        rc, payload, out = self.cli("--apply", "--replace-skills")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(payload["conflicts"], {"project_skills": 1})
        archived = Path(payload["recovery"]) / "replaced-project-skills" / SKILL / "SKILL.md"
        self.assertIn("mine", archived.read_text())

    def test_removing_the_skill_from_the_project_archives_only_the_managed_link(self) -> None:
        self.cli("--apply")
        shutil.rmtree(self.project / ".agents" / "skills" / SKILL)
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn(("archive-project-skill", SKILL), {(i["action"], i["name"]) for i in payload["operations"]})
        self.assertFalse(self.link.is_symlink() or self.link.exists())
        self.assertTrue((Path(payload["recovery"]) / "project-skills" / SKILL).is_symlink())
        self.assertEqual(self.state()["skills"], [])

    def test_symlinked_claude_directory_is_never_written_through(self) -> None:
        target = self.root / "shared-claude"
        target.mkdir()
        os.symlink(target, self.project / ".claude")
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(payload["skipped"][0]["reason"], "project-skills-symlink")
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse(self.user_skill.exists())

    def test_source_outside_the_project_is_skipped(self) -> None:
        with mock.patch.object(claude, "_inside", lambda path, root: False if "agents/skills" in str(path) else True):
            result = claude.sync_project(self.project, home=self.home, apply=False, recovery_root=self.recovery)
        self.assertTrue(any(item["reason"] == "skill-source-outside-project" for item in result["skipped"]))

    def test_failure_rolls_everything_back_and_removes_directories_it_made(self) -> None:
        self.cli("--scope", "user", "--apply")
        real = core._write_json_atomic

        def failing(path, value, *, mode=0o644):
            if Path(path).parent.name == "state":
                raise OSError("simulated failure")
            return real(path, value, mode=mode)

        with mock.patch.object(core, "_write_json_atomic", failing):
            with self.assertRaises(core.AgentPackError):
                claude.sync_project(self.project, home=self.home, apply=True, recovery_root=self.recovery)
        self.assertFalse((self.project / ".claude").exists(), "the directories created for the link are removed again")


class OlderRuntimeTests(SkillBase):
    def test_older_runtime_leaves_the_project_level_link_alone(self) -> None:
        rc, _, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        older_runtime_runs(self)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(self.state()["skills"], [], "nothing project-level in the pre-existing key")


class SyncFolderTests(ScopeBase):
    def test_copy_replaces_the_link_inside_a_sync_folder(self) -> None:
        project = make_project(self.root / "Dropbox" / "proj-s", "proj-s")
        declare(project, skill_scope={SKILL: "project"})
        rc, preview, out = self.cli(project=project)
        self.assertEqual(rc, 0, out)
        self.assertEqual(preview["degraded"], [{"kind": "skill", "mode": "copy", "reason": "sync-folder", "service": "dropbox"}])
        self.assertTrue(any("sync folder" in item or "同步盘" in item for item in preview["warnings"]))
        rc, applied, out = self.cli("--apply", project=project)
        self.assertEqual(preview["operations"], applied["operations"])
        copy = project / ".claude" / "skills" / SKILL
        self.assertTrue(copy.is_dir() and not copy.is_symlink())
        self.assertEqual((copy / "SKILL.md").read_bytes(), (project / ".agents" / "skills" / SKILL / "SKILL.md").read_bytes())
        self.assertEqual(json.loads((self.home / ".claude/agent-pack/state/proj-s.json").read_text())["scopes"]["skills"],
                         {SKILL: "project:copy"})
        rc, again, out = self.cli("--apply", project=project)
        self.assertEqual([i["action"] for i in again["operations"] if i["name"] == SKILL], ["keep-project-skill"])
        (project / ".agents" / "skills" / SKILL / "SKILL.md").write_text("---\nname: sample-skill\ndescription: v2\n---\n")
        rc, refreshed, out = self.cli("--apply", project=project)
        self.assertEqual([i["action"] for i in refreshed["operations"] if i["name"] == SKILL], ["refresh-project-skill"])
        self.assertIn("v2", (copy / "SKILL.md").read_text())


class MigrationTests(SkillBase):
    def setUp(self) -> None:
        super().setUp()
        declare(self.project, skill_scope={})   # start user-level

    def test_user_to_project_lists_both_steps_and_archives_the_old_link(self) -> None:
        self.cli("--apply")
        self.assertTrue(self.user_skill.is_symlink())
        declare(self.project, skill_scope={SKILL: "project"})
        rc, preview, out = self.cli()
        self.assertEqual(rc, 0, out)
        (migration,) = [item for item in preview["migrations"] if item["kind"] == "skill"]
        self.assertEqual((migration["direction"], migration["from_store"], migration["to_store"]),
                         ("narrow", "claude-user-skills", "project-skills"))
        self.assertEqual([step["step"] for step in migration["steps"]], ["write-new", "retire-old"])
        self.assertEqual(migration["steps"][1]["action"], "archive-skill")
        self.assertTrue(self.user_skill.is_symlink(), "preview writes nothing")
        rc, applied, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(preview["operations"], applied["operations"])
        self.assertFalse(self.user_skill.exists() or self.user_skill.is_symlink())
        self.assertTrue(self.link.is_symlink())
        self.assertTrue((Path(applied["recovery"]) / "skills" / SKILL).is_symlink(), "archived, not deleted")
        self.assertEqual(self.state()["scopes"]["skills"], {SKILL: "project:link"})

    def test_someone_elses_user_level_skill_is_not_archived_and_shadows_the_project_one(self) -> None:
        self.user_skill.mkdir(parents=True)
        (self.user_skill / "SKILL.md").write_text("---\nname: sample-skill\ndescription: personal\n---\n")
        declare(self.project, skill_scope={SKILL: "project"})
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.user_skill.is_dir() and (self.user_skill / "SKILL.md").is_file())
        self.assertFalse([item for item in payload["operations"] if item["action"] == "archive-skill"])
        self.assertEqual(payload["narrowing"], [], "nothing of ours leaves the user level")
        (advisory,) = payload["shadowed"]
        self.assertEqual((advisory["kind"], advisory["effective_store"], advisory["rule"], advisory["evidence"]),
                         ("skill", "~/.claude/skills", "personal-over-project", "file"))
        self.assertEqual(advisory["source_url"], "https://code.claude.com/docs/en/skills")
        self.assertTrue(payload["ok"])
        rc, doctor, out = doctor_cli(self)
        self.assertEqual(rc, 0, out)
        self.assertEqual(doctor["clients"]["claude"]["advisories"][0]["name"], SKILL)
        self.assertTrue(doctor["clients"]["claude"]["in_sync"], "advisory only: in_sync and the exit code are untouched")

    def test_project_to_user_archives_the_project_link_and_warns_about_widening(self) -> None:
        declare(self.project, skill_scope={SKILL: "project"})
        self.cli("--apply")
        declare(self.project, skill_scope={})
        rc, preview, out = self.cli()
        self.assertEqual(rc, 0, out)
        (migration,) = [item for item in preview["migrations"] if item["kind"] == "skill"]
        self.assertEqual(migration["direction"], "widen")
        self.assertTrue(any("will see skill" in item or "都会看到 skill" in item for item in preview["warnings"]))
        rc, applied, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(preview["operations"], applied["operations"])
        self.assertTrue(self.user_skill.is_symlink())
        self.assertFalse(self.link.is_symlink() or self.link.exists())
        self.assertTrue((Path(applied["recovery"]) / "project-skills" / SKILL).is_symlink())
        self.assertEqual(self.state()["scopes"]["skills"], {SKILL: "user"})

    def test_a_partial_scope_never_retires_the_other_side(self) -> None:
        self.cli("--apply")
        declare(self.project, skill_scope={SKILL: "project"})
        rc, payload, out = self.cli("--scope", "project", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.user_skill.is_symlink(), "retiring the user-level link needs the full scope")
        self.assertTrue(self.link.is_symlink())
        self.assertFalse([item for item in payload["operations"] if item["action"] == "archive-skill"])
        rc, payload, out = self.cli("--apply")   # the full sync finishes the migration
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.user_skill.is_symlink())

    def test_mcp_migration_names_the_manual_command_in_both_directions(self) -> None:
        self.cli("--apply")
        declare(self.project, mcp_scope={MCP: "project"})
        rc, payload, out = self.cli("--apply")
        (to_project,) = [item for item in payload["migrations"] if item["kind"] == "mcp"]
        self.assertEqual(to_project["steps"][1], {"step": "retire-old", "action": "manual",
                                                  "command": f"claude mcp remove --scope user {MCP}"})
        self.assertIn(MCP, self.user_config()["mcpServers"])
        declare(self.project, mcp_scope={})
        rc, payload, out = self.cli()
        (to_user,) = [item for item in payload["migrations"] if item["kind"] == "mcp"]
        self.assertEqual(to_user["direction"], "widen")
        self.assertEqual(to_user["steps"][1]["command"], f"claude mcp remove --scope local {MCP}")

    def test_text_preview_lists_the_two_steps_in_both_languages(self) -> None:
        self.cli("--apply")
        declare(self.project, skill_scope={SKILL: "project"})
        rc, _, text = self.cli(output="text", lang="en")
        self.assertIn("Scope migration (narrowing): skill sample-skill", text)
        self.assertIn("1. Write the new location: link-project-skill", text)
        self.assertIn("2. Retire the old location: archive-skill", text)
        rc, _, text = self.cli(output="text", lang="zh")
        self.assertIn("作用域迁移（收窄）", text)
        self.assertIn("2. 从旧位置退役：archive-skill", text)


class CompatibilityTests(ScopeBase):
    def test_no_declaration_means_no_project_level_anything(self) -> None:
        before = snapshot(self.project)
        rc, payload, out = self.cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(before, snapshot(self.project), "the project tree is untouched")
        for key in ("migrations", "shadowed", "degraded"):
            self.assertNotIn(key, payload)
        rc, doctor_payload, out = doctor_cli(self)
        self.assertNotIn("advisories", doctor_payload["clients"]["claude"])


if __name__ == "__main__":
    unittest.main()
