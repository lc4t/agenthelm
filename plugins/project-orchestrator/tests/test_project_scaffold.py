"""脚手架一致性经项目入口（health / fix）与 ``agenthelm check`` 的行为。

“旧版项目”在临时目录里用当前初始化器生成后删文件、改内容锁来模拟；“另一台机器”是另一套 HOME 与状态目录。
所有名称、路径与内容都是虚构的通用示例。
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from test_project_real import AGENTHELM, INIT, RealProjectCase, tree_digest

LOCK = ".agents/moe.sakanano.agent-pack/content.lock.json"
DECISIONS = ".agents/moe.sakanano.agent-pack/check-decisions.json"
PROFILE = ".agents/moe.sakanano.agent-pack/project.json"


class ScaffoldCase(RealProjectCase):
    def setUp(self) -> None:
        super().setUp()
        self.env["XDG_STATE_HOME"] = str(self.home / ".local/state")

    def other_machine(self) -> dict:
        """The same project seen from a second machine: its own HOME, caches and state directory."""
        home = self.base / "second-home"
        home.mkdir(exist_ok=True)
        return {**self.env, "HOME": str(home), "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_STATE_HOME": str(home / ".local/state")}

    def old_project(self, name: str = "sample-notes") -> Path:
        root = self.make_project(name, "notes", "--no-entry")
        for relative in (".agents/plugin.json", ".agents/mcp.json", "docs/refs/README.md"):
            (root / relative).unlink()
        (root / "docs/drafts/.gitkeep").unlink()
        (root / "docs/drafts").rmdir()
        lock = root / LOCK
        if lock.exists():
            lock.unlink()
        return root

    def health(self, root: Path, *args: str, env: dict | None = None) -> tuple[int, dict]:
        result = subprocess.run([sys.executable, str(AGENTHELM), "project", "--project", str(root), "health",
                                 "--offline", "--output", "json", *args], input="", capture_output=True, text=True,
                                check=False, env=env or self.env, timeout=120)
        return result.returncode, json.loads(result.stdout)

    def fix(self, root: Path, *args: str, env: dict | None = None) -> tuple[int, dict]:
        result = subprocess.run([sys.executable, str(AGENTHELM), "fix", "--project", str(root), "--offline",
                                 "--output", "json", *args], input="", capture_output=True, text=True,
                                check=False, env=env or self.env, timeout=120)
        try:
            return result.returncode, json.loads(result.stdout)
        except ValueError:
            self.fail(f"not JSON (exit {result.returncode}): {result.stdout}\n{result.stderr}")

    def check(self, root: Path, *args: str, env: dict | None = None) -> dict:
        result = subprocess.run([sys.executable, str(AGENTHELM), "check", "--project", str(root), "--output", "json",
                                 *args], capture_output=True, text=True, check=False, env=env or self.env, timeout=120)
        return json.loads(result.stdout)

    @staticmethod
    def ids(payload: dict) -> set[str]:
        return {finding["id"] for finding in payload["data"]["findings"]}


class MissingSkeletonTests(ScaffoldCase):
    def test_health_and_check_list_missing_files_and_fix_adds_them_one_by_one(self) -> None:
        root = self.old_project()
        code, payload = self.health(root)
        self.assertEqual(code, 0, "脚手架差异只是建议，不改变 health 的退出码")
        wanted = {"skel:.agents/plugin.json", "skel:.agents/mcp.json", "skel:docs/refs/README.md",
                  "skel:docs/drafts/.gitkeep"}
        self.assertTrue(wanted <= self.ids(payload), self.ids(payload))
        report = self.check(root)
        self.assertEqual({item["id"] for item in report["scaffold"]["findings"]}, wanted)
        self.assertEqual(report["summary"]["scaffold_findings"], 4)
        before = tree_digest(root)
        code, preview = self.fix(root, "add-skeleton", ".agents/mcp.json")
        self.assertEqual((code, preview["data"]["applied"]), (0, False))
        self.assertEqual(tree_digest(root), before, "没有 --apply 就不写")
        code, applied = self.fix(root, "add-skeleton", ".agents/mcp.json", "--apply")
        self.assertEqual(code, 0, applied)
        self.assertTrue((root / ".agents/mcp.json").is_file())
        self.assertFalse((root / ".agents/plugin.json").exists(), "只补了点名的文件")
        self.fix(root, "add-skeleton", "--apply")
        self.assertEqual(self.check(root)["scaffold"]["findings"], [])
        _, payload = self.health(root)
        self.assertFalse({i for i in self.ids(payload) if i.startswith("skel:")})

    def test_health_is_read_only(self) -> None:
        root = self.old_project()
        before = tree_digest(root)
        self.health(root)
        self.check(root)
        self.assertEqual(tree_digest(root), before)
        self.assertFalse((self.home / ".local/state").exists())

    def test_existing_files_survive_add_skeleton(self) -> None:
        root = self.old_project()
        (root / "AGENTS.md").write_text("my own contract\n", encoding="utf-8")
        self.fix(root, "add-skeleton", "--apply")
        self.assertEqual((root / "AGENTS.md").read_text(encoding="utf-8"), "my own contract\n")


class SkipTests(ScaffoldCase):
    def test_a_permanent_skip_holds_on_another_machine_and_is_listed(self) -> None:
        root = self.old_project()
        identifier = "skel:docs/drafts/.gitkeep"
        code, preview = self.fix(root, "--skip", identifier, "--reason", "we keep drafts elsewhere")
        self.assertEqual((code, preview["data"]["applied"]), (0, False))
        self.assertFalse((root / DECISIONS).exists())
        code, applied = self.fix(root, "--skip", identifier, "--reason", "we keep drafts elsewhere", "--apply")
        self.assertEqual((code, applied["data"]["applied"]), (0, True))
        saved = json.loads((root / DECISIONS).read_text(encoding="utf-8"))["decisions"][identifier]
        self.assertEqual(saved["reason"], "we keep drafts elsewhere")
        second = self.other_machine()
        _, payload = self.health(root, env=second)
        self.assertNotIn(identifier, self.ids(payload))
        _, payload = self.health(root, "--show-skipped", env=second)
        self.assertEqual([item["id"] for item in payload["data"]["skipped"]], [identifier])
        result = subprocess.run([sys.executable, str(AGENTHELM), "check", "--project", str(root), "--show-skipped"],
                                capture_output=True, text=True, check=False, env=second)
        self.assertIn("we keep drafts elsewhere", result.stdout)
        _, payload = self.health(root, "--reprompt", env=second)
        self.assertIn(identifier, self.ids(payload))

    def test_unskip_brings_it_back_and_unknown_ids_are_usage_errors(self) -> None:
        root = self.old_project()
        identifier = "skel:.agents/mcp.json"
        self.fix(root, "--skip", identifier, "--apply")
        code, _ = self.fix(root, "--unskip", identifier, "--apply")
        self.assertEqual(code, 0)
        _, payload = self.health(root)
        self.assertIn(identifier, self.ids(payload))
        code, payload = self.fix(root, "--skip", "skel:not-a-thing")
        self.assertEqual(code, 2)
        code, _ = self.fix(root, "--unskip", "skel:never-skipped", "--apply")
        self.assertEqual(code, 2)

    def test_a_changed_decision_file_is_not_clobbered_exit_5_or_reported_broken_exit_4(self) -> None:
        root = self.old_project()
        (root / DECISIONS).write_text("{broken", encoding="utf-8")
        code, _ = self.fix(root, "--skip", "skel:.agents/mcp.json", "--apply")
        self.assertEqual(code, 4)
        self.assertEqual((root / DECISIONS).read_text(encoding="utf-8"), "{broken")
        _, payload = self.health(root)
        self.assertIn("scaffold-decisions", self.ids(payload))

    def test_machine_findings_are_remembered_in_the_user_directory_only(self) -> None:
        root = self.old_project()
        _, payload = self.health(root)
        runtime = next(f for f in payload["data"]["findings"] if f["id"] == "install-runtime")
        self.assertEqual(runtime["decision"]["scope"], "machine")
        self.assertIn("wf", runtime["reason"] + runtime["impact"], "要说明 wf / agenthelm 命令是否可用")
        self.assertIn("--link-bin", runtime["next"][0]["label"])
        before = tree_digest(root)
        code, _ = self.fix(root, "--skip", "local:runtime-missing", "--reason", "dev box", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(tree_digest(root), before, "本机决定不进项目")
        _, payload = self.health(root)
        self.assertNotIn("install-runtime", self.ids(payload))
        _, payload = self.health(root, env=self.other_machine())
        self.assertIn("install-runtime", self.ids(payload), "另一台机器仍然提示")


class ContentLockTests(ScaffoldCase):
    def lock_version(self, root: Path) -> str:
        return json.loads((root / LOCK).read_text(encoding="utf-8"))["packs"]["agents-init/project-contract"]["version"]

    def test_entry_apply_then_health_has_no_zero_version(self) -> None:
        root = self.old_project()
        result = subprocess.run([sys.executable, str(INIT), "--project", str(root), "--mode", "entry",
                                 "--entry-name", "menu", "--apply", "--output", "json"],
                                capture_output=True, text=True, check=False, env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotEqual(self.lock_version(root), "0.0.0")
        report = self.check(root)
        self.assertEqual([item["status"] for item in report["content"]], ["current"])
        _, payload = self.health(root)
        self.assertNotIn("update-content", self.ids(payload) - {i for i in self.ids(payload) if i.startswith("content:")})
        self.assertNotIn("0.0.0", json.dumps(payload["data"]["findings"], ensure_ascii=False))

    def test_an_existing_zero_version_lock_is_repaired(self) -> None:
        root = self.old_project()
        subprocess.run([sys.executable, str(INIT), "--project", str(root), "--mode", "entry", "--entry-name", "menu",
                        "--apply", "--output", "json"], capture_output=True, text=True, check=True, env=self.env)
        lock = json.loads((root / LOCK).read_text(encoding="utf-8"))
        lock["packs"]["agents-init/project-contract"]["version"] = "0.0.0"
        (root / LOCK).write_text(json.dumps(lock), encoding="utf-8")
        self.assertEqual([i["status"] for i in self.check(root)["content"]], ["update-available"])
        _, payload = self.health(root)
        self.assertIn("lock:agents-init/project-contract", self.ids(payload))
        code, preview = self.fix(root, "repair-lock")
        self.assertEqual((code, self.lock_version(root)), (0, "0.0.0"), "预览不写")
        code, applied = self.fix(root, "repair-lock", "--apply")
        self.assertEqual(code, 0, applied)
        self.assertNotEqual(self.lock_version(root), "0.0.0")
        self.assertEqual([i["status"] for i in self.check(root)["content"]], ["current"])
        _, payload = self.health(root)
        self.assertNotIn("lock:agents-init/project-contract", self.ids(payload))

    def test_keep_through_update_content_is_remembered(self) -> None:
        root = self.old_project()
        (root / "AGENTS.md").write_text("a contract written long ago\n", encoding="utf-8")
        _, payload = self.health(root)
        self.assertIn("content:AGENTS.md", self.ids(payload))
        code, preview = self.fix(root, "update-content", "--keep", "AGENTS.md")
        self.assertEqual(code, 0, preview)
        self.assertFalse((root / DECISIONS).exists())
        code, applied = self.fix(root, "update-content", "--keep", "AGENTS.md", "--apply")
        self.assertEqual(code, 0, applied)
        self.assertEqual((root / "AGENTS.md").read_text(encoding="utf-8"), "a contract written long ago\n")
        decisions = json.loads((root / DECISIONS).read_text(encoding="utf-8"))["decisions"]
        self.assertEqual(decisions["content:AGENTS.md"]["decision"], "keep")
        _, payload = self.health(root, env=self.other_machine())
        self.assertNotIn("content:AGENTS.md", self.ids(payload))


class ProfileFixTests(ScaffoldCase):
    def test_profile_fix_changes_only_the_selected_field_and_backs_up(self) -> None:
        root = self.make_project("sample-notes", "notes", "--no-entry")
        self.edit_profile(root, lambda p: (p["runtime"].pop("commit_policy"), p.pop("privacy"),
                                           p.setdefault("custom", {}).update(mine=1)))
        _, payload = self.health(root)
        self.assertIn("profile:runtime.commit_policy", self.ids(payload))
        code, preview = self.fix(root, "profile", "runtime.commit_policy")
        step = preview["data"]["plan"]["steps"][0]
        self.assertTrue(any("runtime.commit_policy" in change for change in step["changes"]))
        self.assertTrue(step["payload"]["diffs"]["runtime.commit_policy"].startswith("--- a/"))
        before = (root / PROFILE).read_text(encoding="utf-8")
        self.assertEqual(before, (root / PROFILE).read_text(encoding="utf-8"))
        code, applied = self.fix(root, "profile", "runtime.commit_policy", "--apply")
        self.assertEqual(code, 0, applied)
        profile = json.loads((root / PROFILE).read_text(encoding="utf-8"))
        self.assertEqual(profile["runtime"]["commit_policy"], "explicit")
        self.assertNotIn("privacy", profile)
        self.assertEqual(profile["custom"], {"mine": 1})
        backups = list((self.home / ".agent-pack/recovery").rglob("project.json"))
        self.assertEqual(len(backups), 1)
        self.assertNotIn("commit_policy", backups[0].read_text(encoding="utf-8"))


class SyncFolderHealthTests(ScaffoldCase):
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
        return root

    def test_health_notes_the_sync_folder_once_and_changes_nothing(self) -> None:
        root = self.synced_project()
        before = tree_digest(root)
        _, payload = self.health(root)
        hits = [f for f in payload["data"]["findings"] if f["id"] == "sync-folder"]
        self.assertEqual(len(hits), 1)
        self.assertIn("Dropbox", hits[0]["title"])
        self.assertIn("node_modules", hits[0]["reason"])
        self.assertEqual(hits[0]["decision"]["scope"], "machine")
        self.assertEqual(tree_digest(root), before)

    def test_the_guidance_explains_what_syncs_and_deletes_nothing(self) -> None:
        root = self.synced_project()
        code, preview = self.fix(root, "sync-folder", "--apply")
        step = preview["data"]["plan"]["steps"][0]
        text = "\n".join(step["changes"])
        for expected in ("每台机器各一份", "wf", ".gitignore", "node_modules"):
            self.assertIn(expected, text)
        self.assertEqual(step["writes"], [])
        self.assertTrue((root / "node_modules/placeholder.txt").is_file())
        self.assertEqual(preview["data"]["results"][0]["status"], "skipped")

    def test_it_can_be_dismissed_on_this_machine_only(self) -> None:
        root = self.synced_project()
        self.fix(root, "--skip", "local:sync-folder", "--apply")
        _, payload = self.health(root)
        self.assertNotIn("sync-folder", self.ids(payload))
        _, payload = self.health(root, env=self.other_machine())
        self.assertIn("sync-folder", self.ids(payload))

    def test_a_project_outside_sync_folders_gets_no_finding(self) -> None:
        root = self.old_project("plain-notes")
        _, payload = self.health(root)
        self.assertNotIn("sync-folder", self.ids(payload))


class CaseNamingFixTests(ScaffoldCase):
    def test_naming_mismatch_is_found_and_the_fix_only_plans(self) -> None:
        root = self.make_project("sample-notes", "notes", "--no-entry")
        for name in ("20260902-grammar", "20260903-listening"):
            (root / "cases" / name).mkdir()
            (root / "cases" / name / "CASE.md").write_text("# case\n", encoding="utf-8")
        (root / "docs/refs/index.md").write_text("see cases/20260902-grammar\n", encoding="utf-8")
        _, payload = self.health(root)
        self.assertIn("case-naming", self.ids(payload))
        before = tree_digest(root)
        code, applied = self.fix(root, "case-naming", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(tree_digest(root), before, "不会移动目录或改文件")
        step = applied["data"]["plan"]["steps"][0]
        self.assertTrue(any("cases/20260902-grammar → cases/CASE-20260902-001-grammar" in c and "docs/refs/index.md" in c
                            for c in step["changes"]))
        self.assertTrue((root / "cases/20260902-grammar").is_dir())

    def test_declaring_the_convention_ends_the_finding(self) -> None:
        root = self.make_project("sample-notes", "notes", "--no-entry")
        (root / "cases/20260902-grammar").mkdir()
        (root / "cases/20260902-grammar/CASE.md").write_text("# case\n", encoding="utf-8")
        self.edit_profile(root, lambda p: p["work"]["case"].update(directory_naming={"style": "date-topic"}))
        _, payload = self.health(root)
        self.assertNotIn("case-naming", self.ids(payload))
        code, listed = self.run_json(root, "progress")
        self.assertEqual(code, 0, "wf / 进展照常列出这些目录，命名只是声明与检查")


class MenuChoiceTests(unittest.TestCase):
    def test_each_item_offers_adopt_skip_later_and_diff(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
        import project_menu
        item = {"path": "docs/refs/README.md", "state": "缺少", "options": True}
        with mock.patch.object(project_menu, "choose", side_effect=["d", "s"]) as asked, \
                mock.patch("builtins.print") as printed:
            self.assertEqual(project_menu._decide_item(item, "+hello"), "skip")
        self.assertEqual(asked.call_count, 2)
        self.assertTrue(any("+hello" in str(call) for call in printed.call_args_list))
        for answer, expected in (("a", "accept"), ("l", "later")):
            with mock.patch.object(project_menu, "choose", return_value=answer):
                self.assertEqual(project_menu._decide_item(item, ""), expected)


if __name__ == "__main__":
    unittest.main()
