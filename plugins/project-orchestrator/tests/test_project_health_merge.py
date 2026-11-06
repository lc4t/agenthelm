"""project health：同类建议默认合并为一行，--details 逐项展开；JSON findings、summary 数字与退出码不变（A10）。

同步盘夹具在临时目录里用当前初始化器生成；插件版本不一致用桩 host。所有名称与路径都是虚构的通用示例。
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # tests set the language per case through the child environment or AGENTHELM_LANG

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_project_real import AGENTHELM, INIT, RealProjectCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import project_cli  # noqa: E402
import project_maintain  # noqa: E402


class SyncFolderMergeCase(RealProjectCase):
    lang = "zh"

    def synced_project(self) -> Path:
        base = self.base / "Dropbox"
        base.mkdir()
        root = base / "sample-notes"
        root.mkdir()
        done = subprocess.run([sys.executable, str(INIT), "--project", str(root), "--name", "Sample Notes",
                               "--slug", "sample-notes", "--project-type", "notes", "--vcs", "none", "--stack", "none",
                               "--runtime", "local", "--agent-cli", "none", "--no-entry", "--apply", "--output", "json"],
                              capture_output=True, text=True, check=False, env=self.env)
        self.assertEqual(done.returncode, 0, done.stdout)
        (root / "node_modules").mkdir()
        (root / "node_modules/placeholder.txt").write_text("x\n", encoding="utf-8")
        (root / ".venv/bin").mkdir(parents=True)
        (root / ".venv/pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        return root

    def health(self, root: Path, *args: str, fmt: str = "text") -> tuple[int, str]:
        env = {**self.env, "AGENTHELM_LANG": self.lang}
        result = subprocess.run([sys.executable, str(AGENTHELM), "project", "--project", str(root), "health",
                                 "--offline", "--output", fmt, *args], input="", capture_output=True, text=True,
                                check=False, env=env, timeout=120)
        return result.returncode, result.stdout

    def test_sync_folder_and_local_only_directories_are_one_entry_by_default(self) -> None:
        root = self.synced_project()
        _, text = self.health(root)
        self.assertEqual(text.count("项目位于同步盘（Dropbox）"), 1)
        self.assertNotIn("同步目录里有本机专属目录", text)
        self.assertIn("2 个本机专属目录", text)
        self.assertIn(".venv", text)
        self.assertIn("node_modules", text)
        self.assertIn("health --details", text)

    def test_details_expands_every_item(self) -> None:
        root = self.synced_project()
        _, text = self.health(root, "--details")
        self.assertEqual(text.count("项目位于同步盘（Dropbox）"), 1)
        self.assertIn("同步目录里有本机专属目录 .venv", text)
        self.assertIn("同步目录里有本机专属目录 node_modules", text)

    def test_json_and_exit_code_do_not_change(self) -> None:
        root = self.synced_project()
        code_plain, plain = self.health(root, fmt="json")
        code_details, details = self.health(root, "--details", fmt="json")
        self.assertEqual(code_plain, code_details)
        ids = [f["id"] for f in json.loads(plain)["data"]["findings"]]
        for expected in ("sync-folder", "local:real:.venv", "local:real:node_modules"):
            self.assertIn(expected, ids)
        self.assertEqual(ids, [f["id"] for f in json.loads(details)["data"]["findings"]])

    def test_the_section_count_still_counts_every_finding(self) -> None:
        root = self.synced_project()
        _, text = self.health(root)
        _, details = self.health(root, "--details")
        visible, total = re.search(r"建议处理（(\d+) 项，展开 (\d+) 项）", text).groups()
        self.assertLess(int(visible), int(total))
        self.assertEqual(total, re.search(r"建议处理（(\d+)）", details).group(1))


class SyncFolderMergeEnglishCase(SyncFolderMergeCase):
    lang = "en"

    def test_sync_folder_and_local_only_directories_are_one_entry_by_default(self) -> None:
        root = self.synced_project()
        _, text = self.health(root)
        self.assertEqual(text.count("The project is in a synced folder (Dropbox)"), 1)
        self.assertNotIn("The sync folder contains the machine-local directory", text)
        self.assertIn("machine-local directories: 2", text)
        self.assertNotIn("(s)", text)

    def test_details_expands_every_item(self) -> None:
        root = self.synced_project()
        _, text = self.health(root, "--details")
        self.assertIn("The sync folder contains the machine-local directory .venv", text)
        self.assertIn("The sync folder contains the machine-local directory node_modules", text)

    def test_the_section_count_still_counts_every_finding(self) -> None:
        root = self.synced_project()
        _, text = self.health(root)
        _, details = self.health(root, "--details")
        visible, total = re.search(r"Suggested \((\d+) shown, (\d+) expanded\)", text).groups()
        self.assertLess(int(visible), int(total))
        self.assertEqual(total, re.search(r"Suggested \((\d+)\)", details).group(1))


class UpdatePlanDedupeCase(unittest.TestCase):
    def test_the_same_migration_note_is_listed_once_for_three_clients(self) -> None:
        step = {"to": "2.0.0", "summary": "BREAKING-SUMMARY-X", "actions": ["ACTION-STEP-Y"]}
        behind = [{"plugin": "agent-pack", "client": client, "status": "behind", "breaking": True, "steps": [step]}
                  for client in ("claude", "codex", "cursor")]
        host = SimpleNamespace(current_env=lambda: SimpleNamespace(home=Path("/nonexistent")),
                               update_steps=lambda *args: [])
        provider = SimpleNamespace(host=host, _report={"plugins": behind}, offline=False)
        plan = project_maintain._plan_step(provider, "update-plugins", {})
        text = "\n".join(plan["changes"])
        self.assertEqual(text.count("BREAKING-SUMMARY-X"), 1)
        self.assertEqual(text.count("ACTION-STEP-Y"), 1)


class VersionSkewMergeCase(unittest.TestCase):
    lang = "zh"

    def setUp(self) -> None:
        patch = mock.patch.dict(os.environ, {"AGENTHELM_LANG": self.lang, "NO_COLOR": "1"})
        patch.start()
        self.addCleanup(patch.stop)

    def findings(self, plugins: list[str]) -> list[dict]:
        issues = [{"plugin": name, "issue": "version-skew", "detail": f"claude=1.0.0、codex=1.1.0、cursor=1.2.0"} for name in plugins]
        host = SimpleNamespace(doctor=lambda env, report: {"issues": issues})
        report = {"plugins": [{"client": "claude", "status": "current"}], "summary": {"legacy_installs": 0}}
        found: list[dict] = []
        project_maintain._plugin_findings(SimpleNamespace(host=host), object(), report, found, [])
        return found

    def render(self, found: list[dict], details: bool) -> str:
        health = {"update_source": {"status": "online"}, "read_at": "2026-11-01T10:00:00+00:00", "findings": found,
                  "ok": [], "skipped": []}
        lines, _numbered = project_cli.render_health(health, details=details)
        return "\n".join(lines)

    def test_three_skewed_plugins_are_one_entry_by_default(self) -> None:
        found = self.findings(["agent-pack", "agents-init", "project-orchestrator"])
        self.assertEqual(len(found), 3)
        self.assertEqual(len({f["id"] for f in found}), 3, "ids stay unique so decisions and JSON consumers can address each")
        text = self.render(found, details=False)
        self.assertEqual(text.count("版本不一致"), 1)
        self.assertIn("3 个插件在各客户端的版本不一致", text)
        for name in ("agent-pack", "agents-init", "project-orchestrator"):
            self.assertIn(name, text)
        self.assertIn("建议处理（1 项，展开 3 项）", text)

    def test_details_lists_each_plugin(self) -> None:
        found = self.findings(["agent-pack", "agents-init"])
        text = self.render(found, details=True)
        self.assertEqual(text.count("各客户端插件版本不一致"), 2)
        self.assertEqual(text.count("claude=1.0.0"), 2)

    def test_a_single_skewed_plugin_keeps_the_old_shape(self) -> None:
        (item,) = self.findings(["agent-pack"])
        self.assertEqual(item["id"], "doctor-version-skew")
        self.assertNotIn("group", item)
        text = self.render([item], details=False)
        self.assertIn("原因：claude=1.0.0", text)


class VersionSkewMergeEnglishCase(VersionSkewMergeCase):
    lang = "en"

    def test_three_skewed_plugins_are_one_entry_by_default(self) -> None:
        found = self.findings(["agent-pack", "agents-init", "project-orchestrator"])
        text = self.render(found, details=False)
        self.assertIn("3 plugins have different versions across clients", text)
        self.assertEqual(text.count("Plugin versions differ between clients"), 0)
        self.assertNotIn("(s)", text)

    def test_details_lists_each_plugin(self) -> None:
        found = self.findings(["agent-pack", "agents-init"])
        text = self.render(found, details=True)
        self.assertEqual(text.count("Plugin versions differ between clients"), 2)

    def test_a_single_skewed_plugin_keeps_the_old_shape(self) -> None:
        (item,) = self.findings(["agent-pack"])
        self.assertIn("Plugin versions differ between clients", self.render([item], details=False))


if __name__ == "__main__":
    unittest.main()
