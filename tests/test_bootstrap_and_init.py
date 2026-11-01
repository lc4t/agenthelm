from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "scripts" / "bootstrap_and_init.py"


class BootstrapAndInitTests(unittest.TestCase):
    def run_script(
        self, project: Path, home: Path, *extra: str, client: str = "codex"
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--client", client,
                "--project", str(project),
                "--name", "示例项目",
                "--slug", "example-project",
                "--project-type", "code,docs",
                "--vcs", "github",
                "--stack", "python,markdown",
                "--runtime", "local",
                "--agent-cli", "codex",
                "--home", str(home),
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_dry_run_keeps_client_and_project_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir, tempfile.TemporaryDirectory() as home_dir:
            project, home = Path(project_dir), Path(home_dir)
            result = self.run_script(project, home)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["stage"], "preview")
            self.assertFalse(payload["applied"])
            self.assertEqual(list(project.iterdir()), [])
            self.assertEqual(list(home.iterdir()), [])

    def test_apply_installs_initializes_and_runs_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir, tempfile.TemporaryDirectory() as home_dir:
            project, home = Path(project_dir), Path(home_dir)
            result = self.run_script(project, home, "--apply")
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["stage"], "complete")
            self.assertTrue(payload["applied"])
            self.assertTrue(payload["initializer"]["applied"])
            self.assertTrue(payload["doctor"]["ok"])
            self.assertTrue((project / "AGENTS.md").is_file())
            self.assertTrue((project / ".agents/moe.sakanano.agent-pack/project.json").is_file())
            plugins = home / ".agents/plugins/plugins"
            self.assertEqual(
                set(payload["plugins"]),
                {"agents-init", "project-orchestrator", "agent-pack", "opinion-manager"},
            )
            self.assertTrue((plugins / "agents-init/skills/agents-init/SKILL.md").is_file())
            self.assertTrue((plugins / "project-orchestrator/skills/project-orchestrator/SKILL.md").is_file())
            self.assertTrue((plugins / "agent-pack/runtime/agent_pack_config.py").is_file())
            self.assertTrue((plugins / "opinion-manager/runtime/opinion_manager.py").is_file())
            self.assertFalse((plugins / "agents-init/skills/project-runtime").exists())

    def test_apply_for_claude_projects_skills_and_agent_pack_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir, tempfile.TemporaryDirectory() as home_dir:
            project, home = Path(project_dir), Path(home_dir)
            result = self.run_script(project, home, "--apply", client="claude")
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["stage"], "complete")
            skills = home / ".claude/skills"
            for skill in ("agents-init", "project-orchestrator", "agent-pack", "opinion-manager"):
                self.assertTrue((skills / skill / "SKILL.md").is_file(), skill)
            self.assertFalse((skills / "project-runtime").exists())
            claude_config = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
            self.assertIn("agent-pack", claude_config["mcpServers"])
            self.assertNotIn("project-runtime", claude_config["mcpServers"])

    def test_project_collision_blocks_before_plugin_install(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir, tempfile.TemporaryDirectory() as home_dir:
            project, home = Path(project_dir), Path(home_dir)
            (project / "AGENTS.md").write_text("existing\n", encoding="utf-8")
            result = self.run_script(project, home, "--apply")
            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["stage"], "initializer-preview")
            self.assertEqual((project / "AGENTS.md").read_text(encoding="utf-8"), "existing\n")
            self.assertEqual(list(home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
