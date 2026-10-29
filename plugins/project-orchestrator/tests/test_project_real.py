"""项目入口读写真实项目：在临时目录里初始化项目，经 ``agenthelm project`` 运行。

每个用例都有独立的 HOME 与 XDG 目录，版本源指向仓库里的 latest.json（不联网），
没有任何 AI 客户端插件；覆盖通用默认项目、非工作场景、声明了自有工具的项目与故障。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AGENTHELM = ROOT / "plugins/agent-pack/runtime/agenthelm.py"
PROJECT_CLI = ROOT / "plugins/project-orchestrator/runtime/project_cli.py"
INIT = ROOT / "plugins/agents-init/skills/agents-init/scripts/init_project.py"
sys.path.insert(0, str(PROJECT_CLI.parent))


def tree_digest(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


class RealProjectCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = {**{key: value for key, value in os.environ.items()
                       if key not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")},
                    "HOME": str(self.home), "XDG_CACHE_HOME": str(self.home / ".cache"),
                    "XDG_DATA_HOME": str(self.home / ".local/share"), "XDG_CONFIG_HOME": str(self.home / ".config"),
                    "AGENTHELM_LATEST_URL": str(ROOT / "latest.json"), "AGENTHELM_OPEN": "print",
                    "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"}

    def make_project(self, name: str, kind: str, *extra: str) -> Path:
        root = self.base / name
        root.mkdir()
        result = subprocess.run([sys.executable, str(INIT), "--project", str(root), "--name", name, "--slug", name,
                                 "--project-type", kind, "--vcs", "none", "--stack", "none", "--runtime", "local",
                                 "--agent-cli", "none", *extra, "--apply", "--output", "json"],
                                capture_output=True, text=True, check=False, env=self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return root

    def edit_profile(self, root: Path, change) -> None:
        path = root / ".agents/moe.sakanano.agent-pack/project.json"
        profile = json.loads(path.read_text(encoding="utf-8"))
        change(profile)
        path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_entry(self, root: Path, *args: str, via_host: bool = True) -> subprocess.CompletedProcess:
        command = [sys.executable, str(AGENTHELM), "project"] if via_host else [sys.executable, str(PROJECT_CLI)]
        return subprocess.run([*command, "--project", str(root), *args], input="", capture_output=True, text=True,
                              check=False, env=self.env, timeout=120)

    def run_json(self, root: Path, *args: str, via_host: bool = True) -> tuple[int, dict]:
        result = self.run_entry(root, *args, "--output", "json", via_host=via_host)
        try:
            return result.returncode, json.loads(result.stdout)
        except ValueError:
            self.fail(f"not JSON (exit {result.returncode}): {result.stdout}\n{result.stderr}")

    def new_item(self, root: Path, title: str, slug: str, next_action: str = "第一步") -> str:
        code, payload = self.run_json(root, "record", "new", "--title", title, "--goal", f"{title}的目标",
                                      "--slug", slug, "--next", next_action, "--apply")
        self.assertEqual(code, 0, payload)
        return payload["data"]["id"]


class DefaultProjectTests(RealProjectCase):
    def test_study_project_end_to_end(self) -> None:
        root = self.make_project("reading", "study")
        self.assertTrue(os.access(root / "menu", os.X_OK), "新项目默认生成可执行的 ./menu")
        code, payload = self.run_json(root, "progress")
        self.assertEqual((code, payload["data"]["items"]), (0, []))
        case_id = self.new_item(root, "读完《小王子》", "little-prince", "读第 1-5 章")
        notes = root / "cases" / case_id / "docs" / "notes.md"
        notes.parent.mkdir()
        notes.write_text("# 读后感草稿\n", encoding="utf-8")
        code, payload = self.run_json(root, "find", "读后感")
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["results"][0]["path"], f"cases/{case_id}/docs/notes.md")
        self.assertEqual(payload["data"]["index"]["status"], "fresh")
        code, payload = self.run_json(root, "open", f"cases/{case_id}/docs/notes.md")
        self.assertEqual((code, payload["data"]["executed"]), (0, False))
        self.assertEqual(payload["data"]["absolute"], str(notes))
        code, payload = self.run_json(root, "record", "小王子", "--log", "读完第 1-5 章", "--next", "读第 6-10 章",
                                      "--add-deliverable", f"cases/{case_id}/docs/notes.md", "--apply")
        self.assertEqual(code, 0, payload)
        code, item = self.run_json(root, "show", "little-prince")
        self.assertEqual(item["data"]["next_action"], "读第 6-10 章")
        self.assertEqual(item["data"]["last_record"]["text"], "读完第 1-5 章")
        self.assertEqual(item["data"]["deliverables"], [f"cases/{case_id}/docs/notes.md"])
        text = (root / "cases" / case_id / "CASE.md").read_text(encoding="utf-8")
        self.assertIn('  - "docs/notes.md"', text, "Case 内的文件按相对 Case 的路径登记")

    def test_preview_writes_nothing(self) -> None:
        root = self.make_project("plain", "general")
        before = tree_digest(root)
        code, payload = self.run_json(root, "record", "new", "--title", "Plan", "--goal", "Goal", "--next", "Step")
        self.assertEqual((code, payload["data"]["applied"]), (0, False))
        self.assertEqual(tree_digest(root), before)

    def test_unknown_fields_and_other_sections_survive_an_update(self) -> None:
        root = self.make_project("trip", "personal")
        # A project schema that allows its own fields (work.case.schema); the default one is strict.
        schema = json.loads((ROOT / "plugins/project-orchestrator/references/case.schema.json").read_text())
        schema["additionalProperties"] = True
        (root / "case.schema.json").write_text(json.dumps(schema), encoding="utf-8")
        self.edit_profile(root, lambda profile: profile["work"]["case"].__setitem__("schema", "case.schema.json"))
        case_id = self.new_item(root, "Packing", "packing")
        path = root / "cases" / case_id / "CASE.md"
        text = path.read_text(encoding="utf-8").replace('tags: []', 'tags: []\nmood: "calm"', 1)
        text += "\n## 自定义小节\n\n保留我。\n"
        path.write_text(text, encoding="utf-8")
        code, payload = self.run_json(root, "record", "Packing", "--log", "bought a bag", "--apply")
        self.assertEqual(code, 0, payload)
        updated = path.read_text(encoding="utf-8")
        self.assertIn('mood: "calm"', updated)
        self.assertIn("保留我。", updated)
        self.assertIn("bought a bag", updated)

    def test_concurrent_change_is_refused(self) -> None:
        root = self.make_project("garden", "personal")
        case_id = self.new_item(root, "Seeds", "seeds")
        code, preview = self.run_json(root, "record", "Seeds", "--log", "planted")
        expected = preview["data"]["preview"]
        path = root / "cases" / case_id / "CASE.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n另一个人刚写的内容\n", encoding="utf-8")
        code, payload = self.run_json(root, "record", "Seeds", "--log", "planted", "--apply",
                                      "--expect-sha256", expected["expect_sha256"])
        self.assertEqual((code, payload["error"]["code"]), (5, "conflict"))
        self.assertIn("另一个人刚写的内容", path.read_text(encoding="utf-8"))
        self.assertNotIn("planted", path.read_text(encoding="utf-8"))

    def test_concurrent_change_inside_one_session(self) -> None:
        import project_base
        import project_real

        root = self.make_project("music", "study")
        case_id = self.new_item(root, "Scales", "scales")
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old_home or ""))
        provider = project_real.RealProvider(root, True)
        preview = provider.record_preview("Scales", {"log": "C major"})
        path = root / "cases" / case_id / "CASE.md"
        path.write_text(path.read_text(encoding="utf-8").replace("第一步", "别人改了下一步"), encoding="utf-8")
        with self.assertRaises(project_base.EntryError) as raised:
            provider.record_apply(preview)
        self.assertEqual(raised.exception.code, "conflict")

    def test_close_and_reopen(self) -> None:
        root = self.make_project("course", "study")
        self.new_item(root, "Lesson", "lesson")
        self.assertEqual(self.run_json(root, "record", "Lesson", "--close", "--apply")[0], 0)
        code, payload = self.run_json(root, "progress", "--status", "done")
        self.assertEqual([item["title"] for item in payload["data"]["items"]], ["Lesson"])
        code, payload = self.run_json(root, "record", "Lesson", "--reopen", "--next", "Review", "--apply")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.run_json(root, "show", "Lesson")[1]["data"]["group"], "active")

    def test_guide_reads_readme_and_profile(self) -> None:
        root = self.make_project("notes", "research")
        readme = root / "README.md"
        readme.write_text("# notes\n\n整理阅读过的论文与摘录。\n\n" + readme.read_text(encoding="utf-8").split("\n", 2)[2],
                          encoding="utf-8")
        code, payload = self.run_json(root, "guide")
        self.assertEqual(payload["data"]["about"], "整理阅读过的论文与摘录。")
        self.assertEqual(payload["data"]["command"], "./menu")
        self.assertTrue(payload["data"]["readme_mentions_entry"])

    def test_legacy_project_reports_progress_as_unavailable(self) -> None:
        root = self.make_project("legacy", "general", "--work-mode", "legacy")
        code, payload = self.run_json(root, "progress")
        self.assertEqual((code, payload["error"]["code"]), (4, "unavailable"))
        code, payload = self.run_json(root, "capabilities")
        states = {item["id"]: item["state"] for item in payload["data"]}
        self.assertEqual((states["find"], states["progress"], states["record"]), ("available", "unavailable", "unavailable"))


class HealthAndRepairTests(RealProjectCase):
    def test_health_without_ai_clients_or_runtime(self) -> None:
        root = self.make_project("hobby", "personal")
        code, payload = self.run_json(root, "health")
        self.assertEqual(code, 0, payload)
        ids = {finding["id"] for finding in payload["data"]["findings"]}
        self.assertIn("install-runtime", ids)
        self.assertEqual(payload["data"]["update_source"]["status"], "online")
        self.assertFalse([f for f in payload["data"]["findings"] if f["level"] == "must"])

    def test_offline_without_cache_still_works(self) -> None:
        root = self.make_project("offline", "study")
        self.env["AGENTHELM_LATEST_URL"] = "https://agenthelm.invalid/latest.json"
        code, payload = self.run_json(root, "health", "--offline")
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["update_source"]["status"], "offline")
        self.assertIn("update-check", {finding["id"] for finding in payload["data"]["findings"]})
        self.assertEqual(self.run_json(root, "guide", "--offline")[0], 0)

    def test_broken_index_is_rebuilt_and_project_files_stay(self) -> None:
        root = self.make_project("broken", "study")
        self.new_item(root, "Item", "item")
        self.assertEqual(self.run_json(root, "find", "Item")[0], 0)
        database = next((self.home / ".cache/agenthelm/assets").rglob("assets.sqlite3"))
        database.write_bytes(b"not a database")
        before = tree_digest(root)
        code, payload = self.run_json(root, "health")
        self.assertEqual(code, 1, "必须处理的项让退出码为 1")
        self.assertEqual(payload["data"]["findings"][0]["id"], "rebuild-index")
        code, payload = self.run_json(root, "fix", "rebuild-index", "--apply")
        self.assertEqual(payload["data"]["results"][0]["status"], "applied", payload)
        sqlite3.connect(database).execute("SELECT count(*) FROM files").fetchone()
        self.assertEqual(self.run_json(root, "health")[0], 0)
        self.assertEqual(tree_digest(root), before)

    def test_runtime_install_through_the_menu_flow(self) -> None:
        root = self.make_project("runtime", "general")
        code, payload = self.run_json(root, "fix", "install-runtime")
        writes = [write["path"] for write in payload["data"]["plan"]["steps"][0]["writes"]]
        code, payload = self.run_json(root, "fix", "install-runtime", "--apply")
        result = payload["data"]["results"][0]
        self.assertEqual(result["status"], "applied", payload)
        self.assertEqual(result["written"], writes)
        # No agenthelm on PATH: the launcher must find the runtime just installed.
        path = f"{Path(sys.executable).parent}:/usr/bin:/bin"
        launcher = subprocess.run(["/bin/sh", str(root / "menu"), "guide", "--output", "json"], input="",
                                  capture_output=True, text=True, check=False,
                                  env={**self.env, "PATH": path}, timeout=60)
        self.assertEqual(launcher.returncode, 0, launcher.stderr)
        self.assertEqual(json.loads(launcher.stdout)["data"]["command"], "./menu")

    def test_template_update_keeps_customised_files(self) -> None:
        root = self.make_project("tmpl", "study")
        lock_path = root / ".agents/moe.sakanano.agent-pack/content.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["packs"]["agents-init/project-contract"]["version"] = "1.0.0"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        # As if rendered by an older template: CLAUDE.md untouched since, AGENTS.md edited by the user.
        agents = root / "AGENTS.md"
        agents.write_text("旧模板的 AGENTS\n我的补充。\n", encoding="utf-8")
        (root / "CLAUDE.md").write_text("旧模板渲染的内容\n", encoding="utf-8")
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        files = lock["packs"]["agents-init/project-contract"]["files"]
        files["AGENTS.md"] = hashlib.sha256("旧模板的 AGENTS\n".encode()).hexdigest()
        files["CLAUDE.md"] = hashlib.sha256("旧模板渲染的内容\n".encode()).hexdigest()
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        code, health = self.run_json(root, "health")
        self.assertIn("update-content", {finding["id"] for finding in health["data"]["findings"]})
        code, preview = self.run_json(root, "fix", "update-content")
        step = preview["data"]["plan"]["steps"][0]
        self.assertEqual([item["path"] for item in step["custom"]], ["AGENTS.md"])
        self.assertNotIn("AGENTS.md", [write["path"] for write in step["writes"]])
        code, applied = self.run_json(root, "fix", "update-content", "--apply")
        result = applied["data"]["results"][0]
        self.assertEqual(result["status"], "applied", applied)
        self.assertEqual(result["kept"], ["AGENTS.md"])
        self.assertIn("我的补充。", agents.read_text(encoding="utf-8"))
        self.assertNotEqual((root / "CLAUDE.md").read_text(encoding="utf-8"), "旧模板渲染的内容\n")
        backup = Path(result["recovery"]) / "CLAUDE.md"
        self.assertEqual(backup.read_text(encoding="utf-8"), "旧模板渲染的内容\n", "被替换的文件先备份")
        shipped = json.loads((Path(__file__).resolve().parents[2] / "agents-init/content.json").read_text(encoding="utf-8"))
        current = next(pack["version"] for pack in shipped["packs"] if pack["id"] == "agents-init/project-contract")
        self.assertEqual(json.loads(lock_path.read_text())["packs"]["agents-init/project-contract"]["version"], current)

    def test_add_entry_to_a_project_with_its_own_script(self) -> None:
        root = self.make_project("chores", "personal", "--no-entry")
        own = root / "work"
        own.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        own.chmod(0o755)
        code, health = self.run_json(root, "health")
        self.assertIn("add-entry", {finding["id"] for finding in health["data"]["findings"]})
        code, payload = self.run_json(root, "fix", "add-entry", "--entry-name", "work", "--apply")
        result = payload["data"]["results"][0]
        self.assertEqual(result["status"], "applied", payload)
        self.assertEqual(own.read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n", "同名文件默认保留")
        code, health = self.run_json(root, "health")
        self.assertIn("launcher-custom", {finding["id"] for finding in health["data"]["findings"]})
        code, payload = self.run_json(root, "fix", "add-entry", "--accept", "work", "--apply")
        result = payload["data"]["results"][0]
        self.assertEqual(result["status"], "applied", payload)
        self.assertIn("agenthelm", own.read_text(encoding="utf-8"))
        self.assertEqual((Path(result["recovery"]) / "work").read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")

    def test_without_agent_pack_update_checks_are_marked_unavailable(self) -> None:
        root = self.make_project("direct", "general")
        code, payload = self.run_json(root, "health", via_host=False)
        self.assertEqual(code, 0)
        self.assertIn("no-agent-pack", {finding["id"] for finding in payload["data"]["findings"]})
        self.assertEqual(payload["data"]["update_source"]["status"], "unavailable")


@unittest.skipIf(sys.platform == "win32", "pseudo-terminals need a POSIX system")
class RealMenuTests(RealProjectCase):
    def test_menu_records_then_shows_progress_and_fixes_with_choices(self) -> None:
        from test_project_cli import drive

        root = self.make_project("violin", "study", "--no-entry")
        (root / "work").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        command = [sys.executable, str(AGENTHELM), "project", "--project", str(root)]
        code, text = drive(command, ["6", "n", "练习曲", "拉完第一页", "慢练 4 小节", "yes",
                                     "2", "", "5", "1", "work", "k", "yes", "", "q"], self.env)
        self.assertEqual(code, 0, text)
        self.assertIn("写入后重新读取：已建立", text)
        self.assertIn("练习曲", text.split("选择 › 2", 1)[1])
        self.assertIn("按你的选择，最终将", text)
        self.assertIn("已处理 add-entry", text)
        self.assertEqual((root / "work").read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")
        profile = json.loads((root / ".agents/moe.sakanano.agent-pack/project.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["entry"]["launcher"], "work")


class ProjectAdapterTests(RealProjectCase):
    TOOL = '''import json, sys
from pathlib import Path
state = Path("items.json")
items = json.loads(state.read_text()) if state.exists() else [
    {"id": "OBS-01", "title": "东湖越冬观察", "status": "进行中", "next_action": "周日第 3 次",
     "updated_at": "2026-10-24T07:40:00+08:00", "path": "observations/OBS-01.yaml", "legacy_ids": ["Q4-1"]}]
args = sys.argv[1:]
def value(flag):
    return args[args.index(flag) + 1] if flag in args else None
if args[0] == "list":
    print(json.dumps({"items": items}, ensure_ascii=False))
elif args[0] == "update":
    item = next(entry for entry in items if entry["id"] == args[1])
    if value("--expect-updated-at") and value("--expect-updated-at") != item["updated_at"]:
        print(json.dumps({"code": "conflict"})); sys.exit(1)
    if "--dry-run" in args:
        print(json.dumps({"preview": ["+ 工作记录：" + (value("--log") or "")], "path": item["path"]}, ensure_ascii=False))
    else:
        item["last_record"] = {"at": "2026-10-25T10:00:00+08:00", "text": value("--log")}
        item["updated_at"] = "2026-10-25T10:00:00+08:00"
        state.write_text(json.dumps(items, ensure_ascii=False))
        print(json.dumps({"ok": True, "id": item["id"]}))
elif args[0] == "export":
    Path("exports").mkdir(exist_ok=True); Path("exports/all.csv").write_text("id\\n"); print("exported")
elif args[0] == "touch":
    Path("touched").write_text("ran")
'''

    def research_project(self, *, write: bool) -> Path:
        root = self.make_project("birds", "research", "--entry-name", "obs")
        (root / "tools").mkdir()
        (root / "tools/obs.py").write_text(self.TOOL, encoding="utf-8")

        def change(profile: dict) -> None:
            profile["entry"]["items"] = {"command": [sys.executable, "tools/obs.py"], "write": write,
                                         "status_map": {"进行中": "active", "待复核": "waiting"}}
        self.edit_profile(root, change)
        (root / ".agents/moe.sakanano.agent-pack/tools.json").write_text(json.dumps({"schema_version": 1, "tools": [
            {"id": "export-csv", "title": "导出", "command": [sys.executable, "tools/obs.py", "export"],
             "writes": ["exports/"]},
            {"id": "touch", "title": "会留下痕迹的工具", "command": [sys.executable, "tools/obs.py", "touch"]},
            {"id": "sync", "title": "同步", "command": [sys.executable, "tools/obs.py", "sync"], "network": True,
             "requires": {"env": ["AGENTHELM_TEST_TOKEN_UNSET"]}}]}), encoding="utf-8")
        return root

    def test_read_only_project_tool(self) -> None:
        root = self.research_project(write=False)
        code, payload = self.run_json(root, "progress")
        self.assertEqual(payload["data"]["items"][0]["group"], "active")
        self.assertEqual(self.run_json(root, "show", "Q4-1")[1]["data"]["id"], "OBS-01")
        code, payload = self.run_json(root, "record", "OBS-01", "--log", "x", "--apply")
        self.assertEqual((code, payload["error"]["code"]), (4, "unavailable"))
        self.assertFalse((root / "items.json").exists(), "没有写接口时绝不绕过项目工具")

    def test_project_tool_with_write_interface(self) -> None:
        root = self.research_project(write=True)
        code, preview = self.run_json(root, "record", "OBS-01", "--log", "记录 15 种")
        self.assertEqual(preview["data"]["preview"]["lines"], ["+ 工作记录：记录 15 种"])
        self.assertFalse((root / "items.json").exists())
        code, payload = self.run_json(root, "record", "OBS-01", "--log", "记录 15 种", "--apply")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.run_json(root, "show", "OBS-01")[1]["data"]["last_record"]["text"], "记录 15 种")
        code, payload = self.run_json(root, "record", "OBS-01", "--log", "again", "--apply",
                                      "--expect-updated-at", "2026-10-24T07:40:00+08:00")
        self.assertEqual((code, payload["error"]["code"]), (5, "conflict"))

    def test_tools_run_only_when_chosen(self) -> None:
        root = self.research_project(write=False)
        for args in (["tools"], ["health"], ["capabilities"], []):
            self.run_entry(root, *args)
        self.assertFalse((root / "touched").exists(), "打开菜单、列出与检查都不运行项目工具")
        code, payload = self.run_json(root, "tools", "touch")
        self.assertFalse((root / "touched").exists(), "不加 --run --apply 只显示说明")
        code, payload = self.run_json(root, "tools", "export-csv", "--run", "--apply")
        self.assertEqual(code, 0, payload)
        self.assertTrue((root / "exports/all.csv").is_file())
        code, payload = self.run_json(root, "tools", "sync", "--run", "--apply")
        self.assertEqual((code, payload["error"]["code"]), (4, "unavailable"))
        self.assertIn("AGENTHELM_TEST_TOKEN_UNSET", payload["error"]["next"][0])

    def test_invalid_tools_file_is_reported_not_run(self) -> None:
        root = self.make_project("badtools", "general")
        (root / ".agents/moe.sakanano.agent-pack/tools.json").write_text(
            json.dumps({"tools": [{"id": "x", "command": "rm -rf ."}]}), encoding="utf-8")
        code, payload = self.run_json(root, "health")
        self.assertIn("tools-invalid", {finding["id"] for finding in payload["data"]["findings"]})
        code, payload = self.run_json(root, "tools", "x", "--run", "--apply")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
