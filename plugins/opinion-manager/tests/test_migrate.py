"""opinion_manager.py migrate：设计文档第 7 节迁移契约的逐条测试（不变量 1–6、失败与回滚、重复执行）。

夹具位于 tests/fixtures/migrate-v6.13.1/，由 v6.13.1 标签的真实运行时生成（不是手写的合成数据）：
    git fetch --tags origin && git archive v6.13.1 | tar -x -C <临时目录>
    用 <临时目录>/plugins/opinion-manager/runtime/opinion_manager.py（AGENTHELM_LANG=zh）的 profile / compose / inbox add / inbox adopt 命令
    各自生成一个项目，再用脚本模拟用户的手工修改（edited-20 只追加内容；bundles-21 用 --loading-file 声明规则束）：
      reviewed-20   scenario/reviewed-project@1.0.0，Profile 2.0，未改动
      bundles-21    scenario/concise-project + development-standard/project-language + work-method/proportional（均 1.0.0）+ 规则束声明，Profile 2.1
      edited-20     reviewed-20 + 手工修改 OPINION.md：规则前后各追加一节、一行带条件的结构化行、一行无格式备注
      adopted-20    reviewed-20 + inbox add 三条（旧运行时无 trigger / purpose）、adopt 其中两条，第三条留在收件箱
      atomic-20     只选三条原子规则（om.conclusion-first / om.chinese-response / om.calculations），没有模板
      blank         初始化出来的空白 OPINION.md，没有锁
每个项目另带 .agents/moe.sakanano.agent-pack/project.json（迁移不得触碰）。测试不依赖网络与 tag，只读取仓库内的 catalog。
"""

from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"

import contextlib
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parents[2]
RUNTIME = ROOT / "plugins/opinion-manager/runtime"
SCRIPT = RUNTIME / "opinion_manager.py"
FIXTURES = TESTS / "fixtures" / "migrate-v6.13.1"
TEST_ROOT = ROOT / "__pycache__" / "opinion-test-work"
CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")
sys.path.insert(0, str(RUNTIME))

import migrate  # noqa: E402
import opinion_manager  # noqa: E402
import versioned  # noqa: E402

MIGRATING = ("reviewed-20", "edited-20", "adopted-20")


def tree_hash(root: Path, exclude: tuple[str, ...] = ()) -> str:
    """文件树（目录与文件的相对路径、文件字节）的 SHA-256。"""
    digest = hashlib.sha256()
    for base, directories, files in os.walk(root):
        directories.sort()
        relative = Path(base).relative_to(root).as_posix()
        if any(relative == item or relative.startswith(item + "/") for item in exclude):
            directories[:] = []
            continue
        digest.update(b"d:" + relative.encode() + b"\n")
        for name in sorted(files):
            digest.update(b"f:" + (Path(relative) / name).as_posix().encode() + b":" + hashlib.sha256((Path(base) / name).read_bytes()).digest())
    return digest.hexdigest()


class MigrateBase(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def project(self, name: str, *, base: Path | None = None) -> Path:
        target = (base or self.root) / f"project-{name}"
        shutil.copytree(FIXTURES / name, target)
        return target

    def run_main(self, *argv: object, lang: str = "zh") -> tuple[int, str]:
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENTHELM_LANG": lang}), contextlib.redirect_stdout(buffer):
            code = opinion_manager.main([str(item) for item in argv])
        return code, buffer.getvalue()

    def migrate(self, project: Path, *extra: object, expect: int = 0, lang: str = "zh") -> dict:
        code, output = self.run_main("migrate", "--project", project, "--output", "json", *extra, lang=lang)
        self.assertEqual(code, expect, output)
        return json.loads(output)

    def migrate_ok(self, project: Path) -> dict:
        preview = self.migrate(project)
        self.assertEqual(preview["status"], "preview", preview)
        return self.migrate(project, "--apply", "--confirm", preview["confirmation_sha256"])

    def context(self, project: Path, *argv: object) -> str:
        code, output = self.run_main("context", *argv, "--project", project)
        self.assertEqual(code, 0, output)
        return output

    def reading(self, project: Path) -> str:
        """core 与全部标签 select 的并集文本。"""
        code, index = self.run_main("context", "index", "--project", project)
        self.assertEqual(code, 0, index)
        names = [item["id"] for item in json.loads(index)["tags"] if item["state"] == "ready"]
        parts = [self.context(project, "core")]
        for name in names:
            parts.append(self.context(project, "select", "--tag", name))
        return "\n".join(parts)

    def lock(self, project: Path) -> dict:
        return json.loads((project / "opinion.lock.json").read_text(encoding="utf-8"))

    def user_lines(self, project: Path) -> list[str]:
        """用户自己的内容：当前 OPINION.md 里不属于锁渲染结果的行，加上锁里 custom 段的每一行。"""
        lock = self.lock(project)
        generated = set(versioned.render(lock["profile"], lock.get("language", "zh")).splitlines())
        current = (project / "OPINION.md").read_text(encoding="utf-8").splitlines()
        custom = (lock["profile"]["custom"] or "").split("\n")
        return [line for line in dict.fromkeys([*[item for item in current if item not in generated], *custom]) if line.strip()]

    def backups(self, project: Path) -> list[Path]:
        folder = project / ".opinion" / "backups"
        return sorted(folder.iterdir()) if folder.exists() else []


class PreviewTests(MigrateBase):
    def test_preview_writes_nothing_for_every_fixture(self) -> None:
        """不变量 1：预览零写入（文件树哈希相同），各形态的状态符合契约。"""
        expected = {"reviewed-20": "preview", "edited-20": "preview", "adopted-20": "preview", "bundles-21": "unchanged", "atomic-20": "nothing-to-migrate", "blank": "nothing-to-migrate"}
        for name, status in expected.items():
            with self.subTest(name):
                project = self.project(name)
                before = tree_hash(project)
                result = self.migrate(project)
                self.assertEqual(result["status"], status)
                self.assertEqual(tree_hash(project), before)
                code, text = self.run_main("migrate", "--project", project)
                self.assertEqual(code, 0)
                self.assertEqual(tree_hash(project), before)
                self.assertIn("# Opinion 迁移", text)

    def test_the_preview_classifies_a_hand_edited_project(self) -> None:
        project = self.project("edited-20")
        result = self.migrate(project)
        self.assertTrue(result["opinion_md"]["modified"])
        self.assertGreater(result["opinion_md"]["manual"]["appended_lines"], 0)
        self.assertEqual((result["loading"]["before"], result["loading"]["after"]["kind"]), (None, "tags"))
        self.assertEqual(result["profile"]["schema_version"], "2.0")
        self.assertEqual(result["target"]["schema_version"], "2.2")
        self.assertEqual([(item["current"], item["target"], item["action"]) for item in result["updates"]],
                         [("scenario/reviewed-project@1.0.0", "scenario/reviewed-project@1.1.0", "upgrade")])
        self.assertTrue(result["changes"]["loading_changed"])
        self.assertIsNone(result["changes"]["loading"]["before"])
        self.assertEqual(result["changes"]["loading"]["after"]["tags"][0]["id"], "decisions")
        categories = {item["text"]: item["category"] for item in result["custom"]["lines"]}
        self.assertEqual(categories["- **汇报先给摘要**（适用：汇报材料；条件：制作或审查汇报材料时）：开头先写三行摘要。"], "suggest")
        self.assertEqual(categories["这一行没有任何格式，是纯手写的备注。"], "resident")
        self.assertEqual(result["custom"]["suggested"], 1)
        self.assertIn("decisions", result["custom"]["available_tags"])
        self.assertTrue(result["content"].startswith("<!--"))
        self.assertTrue(any("--no-tags" in note for note in result["notes"]))

    def test_the_preview_lists_old_candidates_with_missing_fields_without_blocking(self) -> None:
        project = self.project("adopted-20")
        result = self.migrate(project)
        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["inbox"]["incomplete"], [{"id": "cand-20261120-003", "title": "长回答先给目录", "status": "pending", "missing": ["trigger", "purpose"]}])
        self.assertEqual(result["custom"]["resident"], 2)
        self.assertEqual(result["custom"]["suggested"], 0)

    def test_a_previewed_bundle_project_is_kept_unless_tags_are_chosen(self) -> None:
        project = self.project("bundles-21")
        result = self.migrate(project)
        self.assertEqual((result["status"], result["reason"]), ("unchanged", "bundles-kept"))
        self.assertEqual(result["loading"]["before"]["kind"], "bundles")
        self.assertTrue(any("--tags" in step for step in result["next"]))
        self.assertEqual(self.migrate(project, "--tags", "--skip", "scenario/concise-project", "--skip", "development-standard/project-language", "--skip", "work-method/proportional")["reason"], "no-tag-declaration")  # 没有声明可物化的模板组合不强加分层
        partial = self.migrate(project, "--tags", "--skip", "work-method/proportional", expect=3)  # 只升级一部分模板会让共享规则落在两个版本上：明确报错，不写入
        self.assertEqual(partial["reason"], "draft-failed")
        forced = self.migrate(project, "--tags")
        self.assertEqual(forced["status"], "preview")
        self.assertEqual((forced["loading"]["before"]["kind"], forced["loading"]["after"]["kind"]), ("bundles", "tags"))
        self.assertTrue(any("bundles" in note for note in forced["notes"]))

    def test_a_no_tags_project_gets_a_materialized_preview_with_an_explanation(self) -> None:
        """2.0 且曾用 --no-tags：模板已是 1.1.0 时，迁移预览重新物化声明并说明。"""
        project = self.project("reviewed-20")
        lock = self.lock(project)["profile"]
        args = ["profile", "--project", project, "--id", lock["id"], "--version", "1.0.1", "--from-profile", project / ".opinion/profiles/private.my-opinion/versions/1.0.0.json",
                "--template", "scenario/reviewed-project@1.1.0", "--no-tags", "--output", "json"]
        code, output = self.run_main(*args)
        self.assertEqual(code, 0, output)
        self.run_main(*args, "--apply", "--confirm", json.loads(output)["confirmation_sha256"])
        profile = project / ".opinion/profiles/private.my-opinion/versions/1.0.1.json"
        code, output = self.run_main("compose", "--project", project, "--profile", profile, "--output", "json")
        self.run_main("compose", "--project", project, "--profile", profile, "--apply", "--confirm", json.loads(output)["confirmation_sha256"])
        self.assertEqual(self.lock(project)["profile"]["schema_version"], "2.0")
        result = self.migrate(project)
        self.assertEqual((result["status"], result["updates"]), ("preview", []))
        self.assertEqual(result["loading"]["after"]["kind"], "tags")
        self.assertTrue(any("--no-tags" in note for note in result["notes"]))

    def test_skip_keeps_a_template_and_unknown_skip_is_an_error(self) -> None:
        project = self.project("reviewed-20")
        skipped = self.migrate(project, "--skip", "scenario/reviewed-project")
        self.assertEqual((skipped["status"], skipped["reason"]), ("nothing-to-migrate", "no-tag-declaration"))
        self.assertEqual(skipped["updates"][0]["action"], "available")
        bad = self.migrate(project, "--skip", "scenario/nope", expect=2)
        self.assertFalse(bad["ok"])


class ApplyTests(MigrateBase):
    def test_apply_verify_read_everything_rollback_and_repeat(self) -> None:
        """不变量 2–5 与重复执行：每个会迁移的夹具。"""
        for name in MIGRATING:
            with self.subTest(name):
                project = self.project(name)
                original = tree_hash(project)
                wanted = self.user_lines(project)
                self.assertEqual(bool(wanted), name != "reviewed-20")
                old_version = self.lock(project)["profile"]["version"]
                result = self.migrate_ok(project)
                self.assertEqual((result["status"], result["applied"]), ("migrated", True))
                self.assertEqual(result["verified"]["status"], "verified")
                lock = self.lock(project)
                self.assertEqual(lock["profile"]["schema_version"], "2.2")
                major, minor, patch = old_version.split(".")
                self.assertEqual(lock["profile"]["version"], f"{major}.{minor}.{int(patch) + 1}")
                self.assertEqual(versioned.verify_project(project)["status"], "verified")
                # 不变量 2：core ∪ 全部 select 里包含用户的每一行
                reading = self.reading(project)
                for line in wanted:
                    self.assertIn(line, reading)
                # 不变量 3：没有标记的行常驻（只在 core 里，不在任何按需标签里），且往返字节不变
                core = self.context(project, "core")
                for line in (lock["profile"]["custom"] or "").split("\n"):
                    if line.strip():
                        self.assertIn(line, core)
                self.assertEqual(self.migrate(project)["status"], "unchanged")  # 重复执行
                self.assertEqual(len(self.backups(project)), 1)
                # 不变量 5：回滚后逐字节相同
                code, output = self.run_main("migrate", "--project", project, "--rollback", result["backup"], "--output", "json")
                self.assertEqual(code, 0, output)
                self.assertEqual(json.loads(output)["status"], "rolled-back")
                self.assertEqual(tree_hash(project, exclude=(".opinion/backups",)), original)
                self.assertEqual(self.migrate(project, "--rollback", result["backup"])["status"], "unchanged")  # 再次回滚无事可做
                self.assertEqual(tree_hash(project, exclude=(".opinion/backups",)), original)

    def test_the_backup_manifest_records_paths_and_hashes(self) -> None:
        project = self.project("adopted-20")
        result = self.migrate_ok(project)
        backup = Path(result["backup"])
        self.assertTrue(re.fullmatch(r"\d{8}T\d{6}Z-migrate", backup.name), backup.name)
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        files = {item["path"]: item for item in manifest["files"]}
        self.assertEqual(set(files), {"OPINION.md", "opinion.lock.json", ".opinion/profiles/private.my-opinion/versions/1.0.1.json", ".opinion/inbox.json"})
        for path, item in files.items():
            self.assertEqual(hashlib.sha256((backup / "files" / path).read_bytes()).hexdigest(), item["sha256"])
            self.assertEqual(item["restore"], path != ".opinion/inbox.json")
        self.assertEqual(manifest["after"]["lock_sha256"], hashlib.sha256((project / "opinion.lock.json").read_bytes()).hexdigest())
        self.assertEqual(manifest["after"]["profile"], "private.my-opinion@1.0.2")
        self.assertEqual(manifest["before"], "private.my-opinion@1.0.1")

    def test_migration_does_not_touch_agents_config_or_inbox(self) -> None:
        """不变量 6。"""
        project = self.project("adopted-20")
        before = {part: tree_hash(project / part) if (project / part).is_dir() else hashlib.sha256((project / part).read_bytes()).hexdigest()
                  for part in (".agents", ".opinion/inbox.json")}
        self.migrate_ok(project)
        after = {part: tree_hash(project / part) if (project / part).is_dir() else hashlib.sha256((project / part).read_bytes()).hexdigest() for part in before}
        self.assertEqual(before, after)
        self.assertEqual(json.loads((project / ".opinion/inbox.json").read_text(encoding="utf-8"))["candidates"]["cand-20261120-003"]["status"], "pending")

    def test_a_bundle_project_chooses_tags_and_can_roll_back(self) -> None:
        project = self.project("bundles-21")
        original = tree_hash(project)
        preview = self.migrate(project, "--tags")
        result = self.migrate(project, "--tags", "--apply", "--confirm", preview["confirmation_sha256"])
        self.assertEqual(result["status"], "migrated")
        loading = self.lock(project)["profile"]["loading"]
        self.assertEqual(set(loading), {"always", "tags", "members"})  # 新旧声明互斥：不再有 bundles
        code, output = self.run_main("context", "bundle", "work-method", "--project", project)
        self.assertEqual(code, 2, output)
        self.assertEqual(self.migrate(project, "--rollback", result["backup"])["status"], "rolled-back")
        self.assertEqual(tree_hash(project, exclude=(".opinion/backups",)), original)

    def test_projects_that_stay_put_are_byte_identical(self) -> None:
        """不变量 4：未选择迁移的 2.1，以及没有可迁移内容的项目，--apply 之后字节不变且不产生备份。"""
        for name, extra in (("bundles-21", ()), ("atomic-20", ()), ("blank", ())):
            with self.subTest(name):
                project = self.project(name)
                before = tree_hash(project)
                result = self.migrate(project, "--apply", "--confirm", "anything", *extra)
                self.assertIn(result["status"], ("unchanged", "nothing-to-migrate"))
                self.assertEqual(tree_hash(project), before)
                self.assertEqual(self.backups(project), [])

    def test_the_fingerprint_covers_every_input(self) -> None:
        project = self.project("edited-20")
        preview = self.migrate(project)
        for label, change in (("opinion", lambda: (project / "OPINION.md").write_text((project / "OPINION.md").read_text(encoding="utf-8") + "\n再手写一行\n", encoding="utf-8")),
                              ("inbox", lambda: (project / ".opinion").mkdir(exist_ok=True) or (project / ".opinion/inbox.json").write_text('{"schema_version": 1, "candidates": {}}\n', encoding="utf-8"))):
            with self.subTest(label):
                change()
                stale = self.migrate(project, "--apply", "--confirm", preview["confirmation_sha256"], expect=4)
                self.assertEqual((stale["status"], stale["reason"]), ("refused", "stale-confirmation"))
                self.assertEqual(self.backups(project), [])
                preview = self.migrate(project)

    def test_apply_without_a_confirmation_is_refused_and_confirm_needs_apply(self) -> None:
        project = self.project("reviewed-20")
        before = tree_hash(project)
        self.assertEqual(self.migrate(project, "--apply", expect=4)["reason"], "confirm-required")
        self.assertEqual(self.migrate(project, "--apply", "--confirm", "0" * 64, expect=4)["reason"], "stale-confirmation")
        self.assertFalse(self.migrate(project, "--confirm", "0" * 64, expect=2)["ok"])
        self.assertEqual(tree_hash(project), before)

    def test_unresolved_upgrade_conflicts_block_the_write_until_resolved(self) -> None:
        project = self.project("reviewed-20")
        profile = project / ".opinion/profiles/private.my-opinion/versions/1.0.0.json"
        override = self.root / "override.json"
        override.write_text(json.dumps({"om.conclusion-first": {"title": "结论先行（我的版本）", "level": "required", "scopes": ["全部场景"], "text": "我自己改写的结论先行规则。"}}, ensure_ascii=False), encoding="utf-8")
        args = ["profile", "--project", project, "--id", "private.my-opinion", "--version", "1.0.1", "--from-profile", profile, "--overrides-file", override, "--output", "json"]
        code, output = self.run_main(*args)
        self.assertEqual(code, 0, output)
        self.run_main(*args, "--apply", "--confirm", json.loads(output)["confirmation_sha256"])
        new = project / ".opinion/profiles/private.my-opinion/versions/1.0.1.json"
        code, output = self.run_main("compose", "--project", project, "--profile", new, "--output", "json")
        self.run_main("compose", "--project", project, "--profile", new, "--apply", "--confirm", json.loads(output)["confirmation_sha256"])
        before = tree_hash(project)
        preview = self.migrate(project)
        self.assertEqual(preview["unresolved"], ["om.conclusion-first"])
        self.assertEqual(self.migrate(project, "--apply", "--confirm", preview["confirmation_sha256"], expect=4)["reason"], "unresolved-conflicts")
        self.assertEqual(tree_hash(project), before)
        resolutions = self.root / "resolutions.json"
        resolutions.write_text(json.dumps({"om.conclusion-first": "keep-local"}), encoding="utf-8")
        resolved = self.migrate(project, "--resolutions-file", resolutions)
        self.assertEqual(resolved["unresolved"], [])
        result = self.migrate(project, "--resolutions-file", resolutions, "--apply", "--confirm", resolved["confirmation_sha256"])
        self.assertEqual(result["status"], "migrated")
        self.assertIn("我自己改写的结论先行规则。", self.reading(project))

    def test_a_long_project_path_works_end_to_end(self) -> None:
        base = self.root.joinpath(*("long-directory-segment-" + str(index) for index in range(6)))
        base.mkdir(parents=True)
        project = self.project("adopted-20", base=base)
        self.assertGreaterEqual(len(str(project)), 138)
        before = tree_hash(project)
        preview = self.migrate(project)
        self.assertEqual(tree_hash(project), before)
        result = self.migrate(project, "--apply", "--confirm", preview["confirmation_sha256"])
        self.assertEqual(result["status"], "migrated")
        self.assertIn("引用来源写明版本", self.reading(project))
        self.assertEqual(self.migrate(project, "--rollback", result["backup"])["status"], "rolled-back")
        self.assertEqual(tree_hash(project, exclude=(".opinion/backups",)), before)


class FailureTests(MigrateBase):
    def assert_restored(self, project: Path, before: str, injected: dict, phase: str) -> None:
        self.assertEqual(injected["status"], "failed")
        self.assertEqual((injected["reason"], injected["phase"], injected["restored"]), ("apply-failed", phase, True))
        self.assertEqual(tree_hash(project), before)  # 不变量 1：失败的应用零写入（含备份目录与新目录）

    def prepared(self) -> tuple[Path, str, str]:
        project = self.project("edited-20")
        return project, tree_hash(project), self.migrate(project)["confirmation_sha256"]

    def test_a_backup_failure_changes_nothing(self) -> None:
        project, before, fingerprint = self.prepared()

        def broken(root: Path, plan: dict, directory: Path) -> list:
            directory.mkdir(parents=True)
            (directory / "partial").write_text("x", encoding="utf-8")
            raise OSError("disk full")

        with mock.patch.object(migrate, "write_backup", broken):
            self.assert_restored(project, before, self.migrate(project, "--apply", "--confirm", fingerprint, expect=5), "backup")

    def test_a_write_failure_restores_from_the_backup(self) -> None:
        project, before, fingerprint = self.prepared()
        real, calls = versioned.atomic_write, []

        def flaky(path: Path, content: str) -> None:
            calls.append(path.name)
            if path.name == "opinion.lock.json":
                raise OSError("simulated write failure")
            real(path, content)

        with mock.patch.object(versioned, "atomic_write", flaky):
            self.assert_restored(project, before, self.migrate(project, "--apply", "--confirm", fingerprint, expect=5), "write")
        self.assertIn("OPINION.md", calls)  # OPINION.md 已经被改写过，再被还原

    def test_a_read_back_failure_restores_from_the_backup(self) -> None:
        project, before, fingerprint = self.prepared()
        with mock.patch.object(migrate, "read_back", side_effect=versioned.OpinionVersionError("simulated read-back mismatch")):
            self.assert_restored(project, before, self.migrate(project, "--apply", "--confirm", fingerprint, expect=5), "read-back")
        self.assertEqual(self.migrate(project, "--apply", "--confirm", fingerprint)["status"], "migrated")  # 还原后可以重试

    def test_a_failed_restore_keeps_the_backup_and_names_the_manual_command(self) -> None:
        project, before, fingerprint = self.prepared()
        with mock.patch.object(migrate, "read_back", side_effect=versioned.OpinionVersionError("boom")), mock.patch.object(migrate, "restore_all", side_effect=OSError("restore failed")):
            result = self.migrate(project, "--apply", "--confirm", fingerprint, expect=5)
        self.assertEqual((result["reason"], result["restored"]), ("restore-failed", False))
        self.assertTrue(Path(result["backup"]).is_dir())
        self.assertIn("--rollback", result["next"][0])

    def test_rollback_is_refused_once_the_state_changed_afterwards(self) -> None:
        project = self.project("adopted-20")
        result = self.migrate_ok(project)
        (project / "OPINION.md").write_text((project / "OPINION.md").read_text(encoding="utf-8") + "\n迁移之后又手写的一行\n", encoding="utf-8")
        before = tree_hash(project)
        refused = self.migrate(project, "--rollback", result["backup"], expect=4)
        self.assertEqual((refused["status"], refused["reason"], refused["changed"]), ("refused", "state-changed", ["OPINION.md"]))
        self.assertEqual(tree_hash(project), before)

    def test_rollback_rejects_a_directory_outside_the_backups(self) -> None:
        project = self.project("reviewed-20")
        outside = self.root / "elsewhere-migrate"
        outside.mkdir()
        before = tree_hash(project)
        self.assertEqual(self.migrate(project, "--rollback", outside, expect=4)["reason"], "not-a-backup")
        self.assertEqual(tree_hash(project), before)


class UnmigratableTests(MigrateBase):
    """无法迁移的形态：非零退出码、明确原因与下一步、零写入。"""

    def refuse(self, project: Path, reason: str, *, status: str = "cannot-migrate", expect: int = 3) -> dict:
        before = tree_hash(project)
        result = self.migrate(project, expect=expect)
        self.assertEqual((result["status"], result["reason"]), (status, reason))
        self.assertTrue(result["message"])
        self.assertEqual(tree_hash(project), before)
        self.assertEqual(self.migrate(project, "--apply", "--confirm", "x" * 64, expect=expect)["reason"], reason)
        self.assertEqual(tree_hash(project), before)
        return result

    def test_missing_lock_with_real_content(self) -> None:
        project = self.project("reviewed-20")
        (project / "opinion.lock.json").unlink()
        result = self.refuse(project, "no-lock")
        self.assertIn("compose --profile", result["next"][0])

    def test_damaged_lock_and_damaged_profile(self) -> None:
        project = self.project("reviewed-20")
        (project / "opinion.lock.json").write_text("{not json", encoding="utf-8")
        self.refuse(project, "lock-invalid")
        project = self.project("adopted-20", base=self.root / "second") if (self.root / "second").mkdir() is None else None
        lock = self.lock(project)
        lock["profile"]["schema_version"] = "9.9"
        (project / "opinion.lock.json").write_text(json.dumps(lock), encoding="utf-8")
        self.refuse(project, "lock-invalid")

    def test_lock_and_text_disagree_in_generated_content(self) -> None:
        project = self.project("reviewed-20")
        text = (project / "OPINION.md").read_text(encoding="utf-8")
        (project / "OPINION.md").write_text(text.replace("结论", "结 论", 1), encoding="utf-8")
        result = self.refuse(project, "manual-edit-conflict")
        self.assertTrue(result["conflicts"])
        self.assertIn("无法并入自定义规则", result["message"])

    def test_empty_text_with_a_lock_and_missing_text(self) -> None:
        project = self.project("reviewed-20")
        (project / "OPINION.md").write_text("", encoding="utf-8")
        self.refuse(project, "manual-edit-conflict")
        (project / "OPINION.md").unlink()
        self.refuse(project, "opinion-missing")

    def test_a_symlinked_text_and_a_missing_project(self) -> None:
        project = self.project("reviewed-20")
        target = self.root / "real-opinion.md"
        shutil.move(project / "OPINION.md", target)
        (project / "OPINION.md").symlink_to(target)
        self.refuse(project, "opinion-unreadable")
        gone = self.migrate(self.root / "nope", expect=3)
        self.assertEqual(gone["reason"], "no-project")

    def test_deleting_all_custom_lines_is_not_silently_undone(self) -> None:
        project = self.project("adopted-20")
        text = (project / "OPINION.md").read_text(encoding="utf-8")
        (project / "OPINION.md").write_text(text[:text.index("## 用户自定义规则")], encoding="utf-8")
        self.refuse(project, "manual-edit-conflict")

    def test_blank_and_empty_files_are_not_errors(self) -> None:
        project = self.project("blank")
        (project / "OPINION.md").write_text("", encoding="utf-8")
        self.assertEqual(self.migrate(project)["status"], "nothing-to-migrate")
        (project / "OPINION.md").unlink()
        self.assertEqual(self.migrate(project)["status"], "nothing-to-migrate")


class RobustnessTests(MigrateBase):
    def test_crlf_text_migrates_and_rolls_back_byte_for_byte(self) -> None:
        project = self.project("adopted-20")
        raw = (project / "OPINION.md").read_bytes().replace(b"\n", b"\r\n")
        (project / "OPINION.md").write_bytes(raw)
        before = tree_hash(project)
        preview = self.migrate(project)
        self.assertEqual(preview["status"], "preview")
        self.assertTrue(preview["opinion_md"]["crlf"])
        self.assertFalse(preview["opinion_md"]["modified"])
        self.assertTrue(any("CRLF" in note for note in preview["notes"]))
        result = self.migrate(project, "--apply", "--confirm", preview["confirmation_sha256"])
        self.assertIn("引用来源写明版本", self.reading(project))
        self.assertEqual(self.migrate(project, "--rollback", result["backup"])["status"], "rolled-back")
        self.assertEqual(tree_hash(project, exclude=(".opinion/backups",)), before)
        self.assertEqual((project / "OPINION.md").read_bytes(), raw)

    def test_sections_added_before_and_after_the_custom_section_and_unknown_lines(self) -> None:
        project = self.project("adopted-20")
        text = (project / "OPINION.md").read_text(encoding="utf-8")
        text = text.replace("\n## ", "\n## 开头小节\n\n- 开头手写的一行\n\n## ", 1)
        text = text.replace("- **引用来源写明版本**", "完全认不出的一行\n- **引用来源写明版本**")
        text += "\n## 结尾小节\n\n结尾手写的一段，没有任何结构。\n"
        (project / "OPINION.md").write_text(text, encoding="utf-8")
        wanted = self.user_lines(project)
        self.assertIn("完全认不出的一行", wanted)
        result = self.migrate_ok(project)
        self.assertEqual(result["status"], "migrated")
        reading = self.reading(project)
        for line in wanted:
            self.assertIn(line, reading)
        core = self.context(project, "core")
        for line in ("完全认不出的一行", "结尾手写的一段，没有任何结构。", "- 开头手写的一行"):
            self.assertIn(line, core)  # 认不出的行与没有标记的行常驻
        custom = self.lock(project)["profile"]["custom"].split("\n")
        self.assertLess(custom.index("- **术语首次出现标注中英对照**（适用：技术文档）：专业术语首次出现时给出英文全称与缩写。"), custom.index("完全认不出的一行"))
        self.assertEqual(self.migrate(project, "--rollback", result["backup"])["status"], "rolled-back")

    def test_lines_with_valid_tags_stay_as_they_are_and_load_on_demand(self) -> None:
        """已带有效标签的行保持原样（在预览里归为 tagged），迁移后按需加载；损坏的标记保持常驻。"""
        project = self.project("adopted-20")
        text = (project / "OPINION.md").read_text(encoding="utf-8")
        tagged = "- **汇报先给摘要**（适用：汇报材料；条件：制作汇报时）：开头先写三行摘要。 [tags reports]"
        damaged = "- **文件命名**（适用：文件；条件：命名文件时）：带版本号。 [tags NoSuchTag]"
        (project / "OPINION.md").write_text(text + tagged + "\n" + damaged + "\n", encoding="utf-8")
        preview = self.migrate(project)
        categories = {item["text"]: item for item in preview["custom"]["lines"]}
        self.assertEqual(categories[tagged]["category"], "tagged")
        self.assertEqual(categories[tagged]["tags"], ["reports"])
        self.assertEqual(categories[damaged]["category"], "resident")
        self.assertEqual(categories[damaged]["note"], "invalid-tags")
        self.migrate(project, "--apply", "--confirm", preview["confirmation_sha256"])
        self.assertNotIn(tagged, self.context(project, "core"))
        self.assertIn(tagged, self.context(project, "select", "--tag", "reports"))
        self.assertIn(damaged, self.context(project, "core"))

    def test_a_hand_edit_inside_the_custom_section_is_taken_as_written(self) -> None:
        project = self.project("adopted-20")
        text = (project / "OPINION.md").read_text(encoding="utf-8").replace("写明版本号与日期", "写明版本号、日期与链接")
        (project / "OPINION.md").write_text(text, encoding="utf-8")
        self.migrate_ok(project)
        self.assertIn("写明版本号、日期与链接", self.context(project, "core"))
        self.assertNotIn("写明版本号与日期", self.reading(project))


class EnglishTests(MigrateBase):
    def test_an_english_ui_and_project_speak_no_chinese(self) -> None:
        project = self.project("reviewed-20")
        config = project / ".agents/moe.sakanano.agent-pack/project.json"
        config.write_text(json.dumps({"language": "en"}), encoding="utf-8")
        for name, check in (("preview", ()), ("kept", ("--tags",))):
            code, text = self.run_main("migrate", "--project", project, *check, lang="en")
            self.assertEqual(code, 0, text)
            self.assertFalse(CJK.search(text), text)
            self.assertIn("# Opinion migration", text)
        preview = self.migrate(project, lang="en")
        for key in ("message", "next", "notes", "warnings", "reason", "status"):
            self.assertFalse(CJK.search(json.dumps(preview.get(key, ""), ensure_ascii=False)), key)
        result = self.migrate(project, "--apply", "--confirm", preview["confirmation_sha256"], lang="en")
        self.assertEqual(result["status"], "migrated")
        self.assertFalse(CJK.search(result["message"]))
        self.assertFalse(CJK.search((project / "OPINION.md").read_text(encoding="utf-8")))
        refused = self.migrate(project, "--rollback", self.root / "x-migrate", expect=4, lang="en")
        self.assertFalse(CJK.search(refused["message"]))


class CommandLineTests(MigrateBase):
    def cli(self, *argv: object) -> subprocess.CompletedProcess:
        environment = {**os.environ, "AGENTHELM_LANG": "zh", "HOME": str(self.root / "home")}
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, argv)], capture_output=True, text=True, env=environment, check=False)

    def test_exit_codes_and_the_contract_fields(self) -> None:
        """退出码：0 成功类状态；2 用法与 I/O 错误；3 无法迁移；4 拒绝；5 失败。输出字段供 agenthelm check 的只读预览使用。"""
        project = self.project("reviewed-20")
        preview = self.cli("migrate", "--project", project, "--output", "json")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        data = json.loads(preview.stdout)
        self.assertEqual({"ok", "status", "applied", "project", "profile", "target", "updates", "loading", "opinion_md", "custom", "inbox", "changes", "confirmation_sha256", "next", "content"} - set(data), set())
        self.assertEqual(self.cli("migrate", "--project", project, "--apply", "--output", "json").returncode, 4)
        (project / "opinion.lock.json").unlink()
        broken = self.cli("migrate", "--project", project, "--output", "json")
        self.assertEqual(broken.returncode, 3)
        self.assertEqual(json.loads(broken.stdout)["reason"], "no-lock")
        self.assertEqual(self.cli("migrate", "--project", project, "--confirm", "x").returncode, 2)
        self.assertEqual(self.cli("migrate", "--project", self.project("blank")).returncode, 0)


if __name__ == "__main__":
    unittest.main()
