from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
AGENT_PACK = PLUGIN_ROOT / "plugins" / "agent-pack" / "runtime" / "agent_pack_config.py"
UNITS = (
    ("agents-init", PLUGIN_ROOT / "plugins" / "agents-init"),
    ("project-orchestrator", PLUGIN_ROOT / "plugins" / "project-orchestrator"),
    ("agent-pack", PLUGIN_ROOT / "plugins" / "agent-pack"),
    ("opinion-manager", PLUGIN_ROOT / "plugins" / "opinion-manager"),
)


class PluginEndToEndTests(unittest.TestCase):
    def run_command(self, *command: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(item) for item in command],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_manifests_distribute_four_chinese_skills(self) -> None:
        manifest = json.loads((PLUGIN_ROOT / "plugins/agents-init/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "agents-init")
        self.assertEqual(manifest["version"], "6.5.0")
        for name, root in UNITS:
            skill_file = root / "skills" / name / "SKILL.md"
            self.assertTrue(skill_file.is_file(), name)
            self.assertRegex(skill_file.read_text(encoding="utf-8"), r"[一-鿿]")

    def test_install_four_plugins_then_initialize_and_run_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as project_dir:
            home = Path(home_dir)
            project = Path(project_dir)
            for name, root in UNITS:
                install = self.run_command(
                    sys.executable,
                    AGENT_PACK,
                    "--output", "json",
                    "bootstrap",
                    "--plugin", root,
                    "--client", "codex",
                    "--home", home,
                    "--apply",
                )
                self.assertEqual(install.returncode, 0, f"{name}: {install.stderr or install.stdout}")
            installed = home / ".agents" / "plugins" / "plugins"

            initialize = self.run_command(
                sys.executable,
                installed / "agents-init/skills/agents-init/scripts/init_project.py",
                "--project", project,
                "--name", "示例项目",
                "--slug", "example-project",
                "--project-type", "code,docs",
                "--vcs", "github",
                "--stack", "python,markdown",
                "--runtime", "local",
                "--agent-cli", "codex",
                "--output", "json",
                "--apply",
            )
            self.assertEqual(initialize.returncode, 0, initialize.stderr or initialize.stdout)
            init_payload = json.loads(initialize.stdout)
            self.assertEqual(init_payload["runtime"]["source"], "bundled")
            self.assertIn("Agent 执行入口", (project / "AGENTS.md").read_text(encoding="utf-8"))

            doctor = self.run_command(
                sys.executable,
                installed / "agent-pack/runtime/agent_pack_config.py",
                "--output", "json",
                "doctor",
                "--project", project,
            )
            self.assertEqual(doctor.returncode, 0, doctor.stderr or doctor.stdout)
            self.assertTrue(json.loads(doctor.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
