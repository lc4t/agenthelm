"""接管非空目录：受管区块、冲突扫描与 adopt。

原则：历史内容不需要处理，工具只新增；改动已有文件必须显式选择并先备份；任何路径下用户的原文都不丢。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("init_project.py")
sys.path.insert(0, str(SCRIPT.parent))
import adopt  # noqa: E402
import init_project  # noqa: E402
import regions  # noqa: E402
import scaffold  # noqa: E402

scaffold.bind(init_project)
adopt.bind(init_project)

USER_AGENTS = """# 我的项目

这是接管前就有的开头说明。

## 常用命令

- 构建：make build
- 测试：make test

## 编码规则

- 禁止直接提交到 main。
- 必须先跑测试。

## 回复偏好

- 用中文回复，语气简洁。

## 发布流程

1. 打 tag
2. 推送
"""
USER_CLAUDE = "# 我自己的 Claude 说明\n\n用 plan mode 改动 billing。\n"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): digest(p) for p in sorted(root.rglob("*")) if p.is_file()}


class RegionTests(unittest.TestCase):
    def test_render_and_find_round_trip(self) -> None:
        text = regions.render("claude-import", "@AGENTS.md")
        [region] = regions.find(text)
        self.assertEqual((region.id, region.body, region.intact), ("claude-import", "@AGENTS.md\n", True))

    def test_upsert_states_and_user_text_outside_is_untouched(self) -> None:
        original = "# 用户文件\n\n用户写的内容。\n"
        inserted, state = regions.upsert(original, "claude-import", "@AGENTS.md")
        self.assertEqual(state, "inserted")
        self.assertTrue(inserted.startswith(original))
        self.assertEqual(regions.upsert(inserted, "claude-import", "@AGENTS.md"), (inserted, "unchanged"))
        updated, state = regions.upsert(inserted, "claude-import", "@AGENTS.md\n@AGENT.RULES.md")
        self.assertEqual(state, "updated")
        self.assertTrue(updated.startswith(original))
        self.assertIn("@AGENT.RULES.md", updated)

    def test_edited_region_is_not_overwritten_without_force(self) -> None:
        text = regions.upsert("", "claude-import", "@AGENTS.md")[0].replace("@AGENTS.md", "@AGENTS.md\n我加的一行")
        self.assertFalse(regions.find(text)[0].intact)
        self.assertEqual(regions.upsert(text, "claude-import", "@AGENTS.md"), (text, "modified"))
        forced, state = regions.upsert(text, "claude-import", "@AGENTS.md", force=True)
        self.assertEqual(state, "updated")
        self.assertNotIn("我加的一行", forced)

    def test_remove_leaves_user_text(self) -> None:
        original = "前\n\n后\n"
        text = regions.upsert(original, "claude-import", "@AGENTS.md")[0]
        removed, existed = regions.remove(text, "claude-import")
        self.assertTrue(existed)
        self.assertEqual(removed.strip(), original.strip())

    def test_bad_ids_and_nested_markers_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            regions.render("Bad Id", "x")
        with self.assertRaises(ValueError):
            regions.render("ok", "<!-- agenthelm:region inner sha=000000000000 -->")


class SectionTests(unittest.TestCase):
    def test_sections_split_on_level_one_and_two_headings_outside_fences(self) -> None:
        text = "开头\n\n# 标题\n\n## 命令\n\n```\n## 不是标题\n```\n\n## 规则\n"
        found = adopt.sections(text)
        self.assertEqual([s.heading for s in found], ["（开头）", "标题", "命令", "规则"])
        self.assertEqual([s.index for s in found], [1, 2, 3, 4])

    def test_suggestions_come_from_keywords_and_say_when_they_are_defaults(self) -> None:
        found = {s.heading: s for s in adopt.sections(USER_AGENTS)}
        self.assertEqual(found["常用命令"].suggestion, "agents")
        self.assertEqual(found["编码规则"].suggestion, "rules")
        self.assertEqual(found["回复偏好"].suggestion, "opinion")
        self.assertEqual(found["发布流程"].suggestion, "sop")
        self.assertIsNone(found["我的项目"].matched)


class AdoptCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        base = Path(self._temporary.name).resolve()
        self.root = base / "existing"
        self.root.mkdir()
        self.recovery = base / "recovery"
        self.state = base / "state"

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def messy_project(self) -> None:
        """A directory that already has history: its own AGENTS.md / CLAUDE.md and folders with other meanings."""
        self.write("AGENTS.md", USER_AGENTS)
        self.write("CLAUDE.md", USER_CLAUDE)
        self.write("README.md", "# 原有 README\n")
        self.write("cases/2024-旧事项/notes.md", "历史\n")
        self.write("cases/另一个旧事项/data.csv", "a,b\n")
        self.write("memory/old-note.txt", "旧记忆\n")
        self.write("projects/alpha/readme.md", "alpha\n")
        self.write("docs/guide.md", "指南\n")
        self.write(".cursorrules", "别的客户端的规则\n")

    def cmd(self, mode: str, *extra: str, name: str = "Existing Project") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--project", str(self.root), "--mode", mode, "--name", name,
             "--project-type", "notes", "--vcs", "none", "--stack", "markdown", "--runtime", "local",
             "--agent-cli", "claude", "--output", "json", "--state-dir", str(self.state), *extra],
            capture_output=True, text=True, check=False)

    def adopt(self, *extra: str, apply: bool = True, recovery: bool = False) -> tuple[int, dict]:
        args = list(extra)
        if recovery:
            args += ["--recovery-dir", str(self.recovery)]
        if apply:
            args.append("--apply")
        done = self.cmd("adopt", *args)
        return done.returncode, json.loads(done.stdout)

    def check(self) -> dict:
        return scaffold.check(self.root, state_dir=self.state)


class ScanTests(AdoptCase):
    def test_scan_is_read_only_and_describes_every_conflict(self) -> None:
        self.messy_project()
        before = snapshot(self.root)
        done = self.cmd("scan")
        self.assertEqual(done.returncode, 0, done.stdout)
        report = json.loads(done.stdout)
        self.assertEqual(snapshot(self.root), before)
        found = {item["id"]: item for item in report["findings"]}
        agents = found["adopt:contract:AGENTS.md"]
        self.assertEqual((agents["state"], agents["recommended"]), ("user-content", "append"))
        self.assertEqual({o["id"] for o in agents["options"]}, {"keep", "append", "split", "side-by-side"})
        self.assertEqual([s["heading"] for s in agents["detail"]["sections"]][:2], ["我的项目", "常用命令"])
        claude = found["adopt:contract:CLAUDE.md"]
        self.assertEqual((claude["state"], claude["recommended"]), ("shadows-agents", "add-import"))
        self.assertIn("只读 CLAUDE.md", claude["summary"])
        self.assertEqual(found["adopt:semantic:cases"]["state"], "foreign")
        self.assertEqual(found["adopt:semantic:cases"]["recommended"], "coexist")
        self.assertEqual(found["adopt:semantic:memory"]["recommended"], "alt-memory-root")
        self.assertEqual(found["adopt:semantic:container:projects"]["recommended"], "register-root")
        self.assertIn("adopt:semantic:docs", found)
        self.assertEqual(found["adopt:other-agent-files"]["detail"]["paths"], [".cursorrules"])
        self.assertIn("--agents-policy append", report["recommended_flags"])
        self.assertIn("--claude-policy add-import", report["recommended_flags"])

    def test_empty_directory_has_nothing_to_decide(self) -> None:
        report = json.loads(self.cmd("scan").stdout)
        self.assertEqual({item["state"] for item in report["findings"] if item["kind"] == "contract"}, {"absent"})
        self.assertEqual(report["would_preserve"], [])

    def test_already_initialized_and_v5_projects_are_not_adoption_cases(self) -> None:
        done = self.cmd("adopt", "--apply")
        self.assertEqual(done.returncode, 0, done.stdout)
        again = self.cmd("adopt", "--apply")
        self.assertEqual(again.returncode, 2)
        self.assertEqual(json.loads(again.stdout)["code"], "already-initialized")
        legacy = self.root.parent / "legacy"
        (legacy / init_project.LEGACY_PROFILE_PATH.parent).mkdir(parents=True)
        (legacy / init_project.LEGACY_PROFILE_PATH).write_text("{}")
        done = subprocess.run([sys.executable, str(SCRIPT), "--project", str(legacy), "--mode", "scan", "--output", "json"],
                              capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)["blockers"][0]["code"], "migrate-required")


class AdoptDefaultTests(AdoptCase):
    def test_default_adoption_only_adds_and_keeps_every_existing_byte(self) -> None:
        self.messy_project()
        before = snapshot(self.root)
        code, result = self.adopt()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["modify"], [])
        self.assertFalse(result["needs_recovery_dir"])
        after = snapshot(self.root)
        for path, value in before.items():
            self.assertEqual(after[path], value, f"{path} 被改动了")
        for created in (".agents/moe.sakanano.agent-pack/project.json", "AGENT.RULES.md", "docs/refs/README.md", "menu"):
            self.assertIn(created, after)
        self.assertEqual((self.root / "CLAUDE.md").read_text(), USER_CLAUDE)

    def test_kept_files_are_recorded_as_decisions_so_check_stays_quiet(self) -> None:
        self.messy_project()
        code, result = self.adopt()
        self.assertEqual(code, 0, result)
        self.assertEqual(set(result["decisions_recorded"]), {"content:AGENTS.md", "content:CLAUDE.md"})
        self.assertTrue(result["check_after"]["ok"], result["check_after"])
        report = self.check()
        self.assertEqual(report["findings"], [])
        self.assertEqual({item["id"] for item in report["skipped"]}, {"content:AGENTS.md", "content:CLAUDE.md"})

    def test_history_folders_are_tolerated_by_check(self) -> None:
        self.messy_project()
        self.adopt()
        report = self.check()
        self.assertEqual(report["case_naming"]["mismatched_count"], 0)  # 没有 CASE.md 的旧目录不是 Case
        self.assertTrue(report["ok"])

    def test_launcher_name_taken_by_user_file_is_left_alone(self) -> None:
        self.write("menu", "#!/bin/sh\necho mine\n")
        code, result = self.adopt()
        self.assertEqual(code, 0, result)
        self.assertEqual((self.root / "menu").read_text(), "#!/bin/sh\necho mine\n")
        profile = json.loads((self.root / init_project.PROFILE_PATH).read_text())
        self.assertNotIn("entry", profile)
        self.assertTrue(any("menu" in note for note in result["notes"]))

    def test_readme_entry_appends_one_marked_block_and_keeps_the_rest(self) -> None:
        self.write("README.md", "# 原有 README\n\n正文\n")
        code, result = self.adopt("--readme-entry")
        self.assertEqual(code, 0, result)
        readme = (self.root / "README.md").read_text()
        self.assertTrue(readme.startswith("# 原有 README\n\n正文\n"))
        self.assertEqual(readme.count(init_project.README_START), 1)

    def test_text_output_lists_options_and_recommendations(self) -> None:
        self.messy_project()
        done = subprocess.run([sys.executable, str(SCRIPT), "--project", str(self.root), "--mode", "scan"],
                              capture_output=True, text=True, check=False)
        self.assertIn("（推荐）", done.stdout)
        self.assertIn("--agents-policy append", done.stdout)

    def test_new_claude_md_is_the_import_stub(self) -> None:
        self.adopt()
        self.assertTrue((self.root / "CLAUDE.md").read_text().startswith("@AGENTS.md"))


class AdoptRootsTests(AdoptCase):
    def test_alternative_roots_and_registered_containers_go_into_the_profile(self) -> None:
        self.messy_project()
        code, result = self.adopt("--memory-root", "agent-memory", "--case-root", "work-cases", "--register-root", "projects")
        self.assertEqual(code, 0, result)
        profile = json.loads((self.root / init_project.PROFILE_PATH).read_text())
        self.assertEqual(profile["memory"]["root"], "agent-memory")
        self.assertEqual(profile["work"]["case"]["root"], "work-cases")
        self.assertEqual(profile["work"]["assets"]["roots"], ["projects"])
        self.assertTrue((self.root / "agent-memory/.gitkeep").exists())
        self.assertTrue((self.root / "work-cases/.gitkeep").exists())
        self.assertEqual((self.root / "memory/old-note.txt").read_text(), "旧记忆\n")
        self.assertTrue(self.check()["ok"])

    def test_bad_roots_are_refused(self) -> None:
        for flag, value in (("--case-root", "../x"), ("--memory-root", "/abs"), ("--register-root", "missing-dir")):
            with self.subTest(flag=flag):
                done = self.cmd("adopt", flag, value)
                self.assertEqual(done.returncode, 2, done.stdout)


class AdoptAppendTests(AdoptCase):
    def test_changing_a_user_file_needs_an_outside_backup_directory(self) -> None:
        self.messy_project()
        code, preview = self.adopt("--agents-policy", "append", apply=False)
        self.assertEqual(code, 0)
        self.assertTrue(preview["needs_recovery_dir"])
        self.assertEqual((self.root / "AGENTS.md").read_text(), USER_AGENTS)
        done = self.cmd("adopt", "--agents-policy", "append", "--apply")
        self.assertEqual(done.returncode, 2)
        self.assertIn("--recovery-dir", json.loads(done.stdout)["error"])
        self.assertEqual((self.root / "AGENTS.md").read_text(), USER_AGENTS)
        self.assertFalse((self.root / init_project.PROFILE_PATH).exists())
        inside = self.cmd("adopt", "--agents-policy", "append", "--recovery-dir", str(self.root / "backup"), "--apply")
        self.assertEqual(inside.returncode, 2)

    def test_append_keeps_the_original_text_and_adds_a_managed_region(self) -> None:
        self.messy_project()
        code, result = self.adopt("--agents-policy", "append", "--claude-policy", "add-import", recovery=True)
        self.assertEqual(code, 0, result)
        agents = (self.root / "AGENTS.md").read_text()
        self.assertTrue(agents.startswith(USER_AGENTS))
        self.assertIsNotNone(regions.get(agents, "agents-contract"))
        claude = (self.root / "CLAUDE.md").read_text()
        self.assertTrue(claude.startswith(USER_CLAUDE))
        self.assertIn("@AGENTS.md", regions.get(claude, "claude-import").body)
        self.assertEqual((self.recovery / "AGENTS.md").read_text(), USER_AGENTS)
        self.assertEqual((self.recovery / "CLAUDE.md").read_text(), USER_CLAUDE)
        lock = json.loads((self.root / init_project.CONTENT_LOCK_PATH).read_text())
        entry = lock["packs"]["agents-init/project-contract"]
        self.assertEqual(set(entry["regions"]), {"AGENTS.md", "CLAUDE.md"})
        self.assertEqual(entry["render_inputs"]["work_mode"], "case-workspace")
        self.assertTrue(result["check_after"]["ok"], result["check_after"])

    def test_policy_that_has_nothing_to_do_says_so(self) -> None:
        code, result = self.adopt("--agents-policy", "append", recovery=True)
        self.assertEqual(code, 0)
        self.assertTrue(any("没有生效" in note for note in result["notes"]))


class AdoptSplitTests(AdoptCase):
    def test_split_routes_sections_and_loses_nothing(self) -> None:
        self.write("AGENTS.md", USER_AGENTS)
        code, result = self.adopt("--agents-policy", "split", "--route", "3=rules", "--route", "4=opinion",
                                  "--route", "5=sop", recovery=True)
        self.assertEqual(code, 0, result)
        agents = (self.root / "AGENTS.md").read_text()
        rules = (self.root / "AGENT.RULES.md").read_text()
        user = (self.root / "AGENT.USER.md").read_text()
        self.assertIn("## 常用命令", agents)
        self.assertNotIn("## 编码规则", agents)
        self.assertIn("禁止直接提交到 main", rules)
        self.assertIn("用中文回复，语气简洁", user)
        self.assertIn("待交给 opinion-manager", user)
        self.assertIn("待并入 SOP", user)
        self.assertEqual({item["destination"] for item in result["pending_handoff"]}, {"opinion", "sop"})
        combined = agents + rules + user
        for line in (line for line in USER_AGENTS.splitlines() if line.strip()):
            self.assertIn(line, combined, f"丢了：{line}")
        self.assertEqual((self.recovery / "AGENTS.md").read_text(), USER_AGENTS)
        self.assertTrue(result["check_after"]["ok"], result["check_after"])

    def test_split_refuses_unknown_sections_and_destinations(self) -> None:
        self.write("AGENTS.md", USER_AGENTS)
        for route in ("99=rules", "2=nowhere", "x"):
            with self.subTest(route=route):
                done = self.cmd("adopt", "--agents-policy", "split", "--route", route, "--recovery-dir", str(self.recovery), "--apply")
                self.assertEqual(done.returncode, 2, done.stdout)
        self.assertEqual((self.root / "AGENTS.md").read_text(), USER_AGENTS)

    def test_side_by_side_moves_the_original_and_points_at_it(self) -> None:
        self.write("AGENTS.md", USER_AGENTS)
        code, result = self.adopt("--agents-policy", "side-by-side", recovery=True)
        self.assertEqual(code, 0, result)
        moved = (self.root / "AGENT.USER.md").read_text()
        self.assertIn(USER_AGENTS, moved)  # 原文整段保留，前面只加一段来源说明
        agents = (self.root / "AGENTS.md").read_text()
        self.assertIn("AGENT.USER.md", agents)
        self.assertNotIn("make build", agents)
        self.assertEqual((self.recovery / "AGENTS.md").read_text(), USER_AGENTS)
        self.assertTrue(result["check_after"]["ok"], result["check_after"])

    def test_existing_user_file_is_never_overwritten_by_a_move(self) -> None:
        self.write("AGENTS.md", USER_AGENTS)
        self.write("AGENT.USER.md", "别人已经有的\n")
        done = self.cmd("adopt", "--agents-policy", "side-by-side", "--recovery-dir", str(self.recovery), "--apply")
        self.assertEqual(done.returncode, 2)
        self.assertEqual((self.root / "AGENT.USER.md").read_text(), "别人已经有的\n")
        self.assertEqual((self.root / "AGENTS.md").read_text(), USER_AGENTS)

    def test_a_failure_during_writing_restores_the_modified_files(self) -> None:
        self.write("AGENTS.md", USER_AGENTS)
        args = mock.Mock(agents_policy="append", claude_policy="keep", route=[], register_root=[], case_root=None,
                         memory_root=None, readme_entry=False, apply=True, recovery_dir=self.recovery, state_dir=self.state)
        inputs = init_project.Inputs(self.root, "Existing", "existing", ("notes",), "none", ("markdown",), "local",
                                     ("claude",), "case-workspace", "menu")
        with mock.patch.object(init_project, "apply_writes", side_effect=init_project.InitError("disk full")):
            with self.assertRaises(init_project.InitError):
                adopt.run(args, self.root, inputs)
        self.assertEqual((self.root / "AGENTS.md").read_text(), USER_AGENTS)
        self.assertFalse((self.root / init_project.PROFILE_PATH).exists())


class RegionLifecycleTests(AdoptCase):
    def adopted_with_regions(self) -> None:
        self.messy_project()
        code, result = self.adopt("--agents-policy", "append", "--claude-policy", "add-import", recovery=True)
        self.assertEqual(code, 0, result)

    def region_cmd(self, *extra: str) -> tuple[int, dict]:
        done = subprocess.run([sys.executable, str(SCRIPT), "--project", str(self.root), "--mode", "region",
                               "--output", "json", "--state-dir", str(self.state), *extra],
                              capture_output=True, text=True, check=False)
        return done.returncode, json.loads(done.stdout)

    def test_a_clean_adoption_has_no_region_findings(self) -> None:
        self.adopted_with_regions()
        report = self.check()
        self.assertEqual([row["state"] for row in report["regions"]], ["ok", "ok"])
        self.assertTrue(report["ok"])

    def test_outdated_region_is_updated_after_a_backup_and_user_text_stays(self) -> None:
        self.adopted_with_regions()
        newer = dict(init_project.load_templates())
        newer["region/claude-import"] = "@AGENTS.md\n@AGENT.RULES.md\n"
        with mock.patch.object(init_project, "load_templates", return_value=newer):
            report = self.check()
            self.assertIn("region:CLAUDE.md#claude-import", {f["id"] for f in report["findings"]})
            finding = next(f for f in report["findings"] if f["id"] == "region:CLAUDE.md#claude-import")
            self.assertEqual((finding["kind"], finding["fix"]), ("region-outdated", "update-region"))
            code, preview = scaffold.update_regions(self.root, accept=[], keep=[], apply=False)
            self.assertEqual((code, preview["regions"][0]["action"]), (0, "replace"))
            before = (self.root / "CLAUDE.md").read_text()
            code, applied = scaffold.update_regions(self.root, accept=[], keep=[], apply=True,
                                                    recovery_dir=self.recovery / "r2", state_dir=self.state)
            self.assertEqual(code, 0, applied)
            after = (self.root / "CLAUDE.md").read_text()
            self.assertTrue(after.startswith(USER_CLAUDE))
            self.assertIn("@AGENT.RULES.md", after)
            self.assertEqual((self.recovery / "r2" / "CLAUDE.md").read_text(), before)
            self.assertEqual([row["state"] for row in self.check()["regions"]], ["ok", "ok"])

    def test_region_edited_by_the_user_blocks_until_they_choose(self) -> None:
        self.adopted_with_regions()
        path = self.root / "CLAUDE.md"
        path.write_text(path.read_text().replace("@AGENTS.md\n", "@AGENTS.md\n我加的一行\n", 1))
        report = self.check()
        finding = next(f for f in report["findings"] if f["kind"] == "region-modified")
        self.assertEqual(finding["fix"], "update-region")
        code, blocked = self.region_cmd()
        self.assertEqual(code, 1)
        self.assertIn("--accept", blocked["blocked"])
        key = "CLAUDE.md#claude-import"
        code, kept = self.region_cmd("--keep", key, "--apply")
        self.assertEqual(code, 0, kept)
        self.assertIn("我加的一行", path.read_text())
        self.assertIn(f"region:{key}", {item["id"] for item in self.check()["skipped"]})
        code, taken = self.region_cmd("--accept", key, "--recovery-dir", str(self.recovery / "r3"), "--apply")
        self.assertEqual(code, 0, taken)
        self.assertNotIn("我加的一行", path.read_text())
        self.assertTrue((self.recovery / "r3" / "CLAUDE.md").is_file())

    def test_removed_region_is_reported_and_can_be_skipped(self) -> None:
        self.adopted_with_regions()
        path = self.root / "AGENTS.md"
        path.write_text(regions.remove(path.read_text(), "agents-contract")[0])
        report = self.check()
        finding = next(f for f in report["findings"] if f["kind"] == "region-missing")
        self.assertIsNone(finding["fix"])
        self.assertEqual(path.read_text().strip(), USER_AGENTS.strip())

    def test_region_state_is_part_of_the_cross_machine_fingerprint(self) -> None:
        self.adopted_with_regions()
        clean = scaffold.fingerprint(self.check())
        path = self.root / "CLAUDE.md"
        path.write_text(path.read_text().replace("@AGENTS.md\n", "@AGENTS.md\n改\n", 1))
        self.assertNotEqual(scaffold.fingerprint(self.check())["fingerprint"], clean["fingerprint"])


class UpgradePathTests(AdoptCase):
    OLD_ROUTE = ("# Agent 路由\n\n读取 `AGENTS.md` 并遵循其中的 Project Profile。加载 `project-orchestrator` 管理项目生命周期；"
                 "Plugin/Skill/MCP 能力管理交给 `agent-pack`；Opinion 的生成、读取与审查交给 `opinion-manager`。\n")

    def refresh(self, *extra: str) -> dict:
        done = self.cmd("refresh", *extra)
        return json.loads(done.stdout)

    def test_projects_with_the_old_claude_md_get_the_import_stub_through_refresh(self) -> None:
        self.cmd("init", "--apply")
        claude = self.root / "CLAUDE.md"
        claude.write_text(self.OLD_ROUTE)
        lock_path = self.root / init_project.CONTENT_LOCK_PATH
        lock = json.loads(lock_path.read_text())
        lock["packs"]["agents-init/project-contract"]["files"]["CLAUDE.md"] = hashlib.sha256(self.OLD_ROUTE.encode()).hexdigest()
        lock_path.write_text(json.dumps(lock))
        preview = self.refresh()
        item = next(i for i in preview["files"] if i["path"] == "CLAUDE.md")
        self.assertEqual((item["state"], item["action"]), ("clean", "replace"))
        applied = self.refresh("--recovery-dir", str(self.recovery), "--apply")
        self.assertTrue(applied["applied"], applied)
        self.assertTrue(claude.read_text().startswith("@AGENTS.md"))
        self.assertEqual((self.recovery / "CLAUDE.md").read_text(), self.OLD_ROUTE)

    def test_a_user_edited_claude_md_is_never_replaced_by_refresh(self) -> None:
        self.cmd("init", "--apply")
        claude = self.root / "CLAUDE.md"
        claude.write_text(self.OLD_ROUTE)
        lock_path = self.root / init_project.CONTENT_LOCK_PATH
        lock = json.loads(lock_path.read_text())
        lock["packs"]["agents-init/project-contract"]["files"]["CLAUDE.md"] = hashlib.sha256(self.OLD_ROUTE.encode()).hexdigest()
        lock_path.write_text(json.dumps(lock))
        claude.write_text(self.OLD_ROUTE + "\n我的补充\n")
        done = self.cmd("refresh", "--recovery-dir", str(self.recovery), "--apply")
        self.assertEqual(done.returncode, 1)
        self.assertIn("blocked", json.loads(done.stdout))
        self.assertIn("我的补充", claude.read_text())

    def test_refresh_keeps_regions_and_render_inputs_in_the_lock(self) -> None:
        self.messy_project()
        self.adopt("--claude-policy", "add-import", recovery=True)
        done = self.cmd("refresh", "--recovery-dir", str(self.recovery / "x"), "--apply")
        entry = json.loads((self.root / init_project.CONTENT_LOCK_PATH).read_text())["packs"]["agents-init/project-contract"]
        self.assertIn("CLAUDE.md", entry["regions"], done.stdout)
        self.assertEqual(entry["render_inputs"]["name"], "Existing Project")

    def test_rendering_is_deterministic_for_every_input_combination(self) -> None:
        digests = set()
        for work_mode in ("case-workspace", "legacy"):
            for launcher in ("menu", None):
                renders = []
                for where in ("/tmp/a", "/var/b/c"):
                    inputs = init_project.Inputs(Path(where), "Demo", "demo", ("code",), "github", ("python",), "local",
                                                 ("codex",), work_mode, launcher)
                    renders.append({str(k): v for k, v in init_project.files_for(inputs).items()})
                self.assertEqual(renders[0], renders[1], (work_mode, launcher))
                digests.add(hashlib.sha256(json.dumps(renders[0], sort_keys=True).encode()).hexdigest())
        self.assertEqual(len(digests), 4)


if __name__ == "__main__":
    unittest.main()
