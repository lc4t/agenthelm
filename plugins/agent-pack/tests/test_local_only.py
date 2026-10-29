"""local-only：同步盘里的本机专属目录、外置链接、HOME 约束、体积阈值、跨设备对比。"""
from __future__ import annotations

import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "plugins/agent-pack/runtime"
INIT = ROOT / "plugins/agents-init/skills/agents-init/scripts/init_project.py"
os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English
sys.path.insert(0, str(RUNTIME))
import agenthelm  # noqa: E402
import local_only  # noqa: E402
import mcp_server  # noqa: E402

LATEST = {"schema": 1, "marketplace": "agenthelm", "released_at": "2026-10-29", "plugins": {}, "migrations": []}


class Fixture(unittest.TestCase):
    """base/home 是这台“设备”的 HOME；项目放在名为 iCloud Drive 的目录下，路径识别就会认出同步盘。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.root = self.base / "iCloud Drive" / "proj"
        self.root.mkdir(parents=True)

    def venv(self, relative: str = ".venv") -> Path:
        path = self.root / relative
        (path / "bin").mkdir(parents=True)
        (path / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        (path / "bin/pip").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        return path

    def node_modules(self, relative: str = "node_modules", manager: str | None = "yarn") -> Path:
        path = self.root / relative
        (path / "pkg").mkdir(parents=True)
        (path / "pkg/index.js").write_text("1\n", encoding="utf-8")
        parent = path.parent
        locks = {"yarn": "yarn.lock", "npm": "package-lock.json", "pnpm": "pnpm-lock.yaml", "bun": "bun.lock"}
        (parent / "package.json").write_text("{}\n", encoding="utf-8")
        if manager:
            (parent / locks[manager]).write_text("", encoding="utf-8")
        return path

    def check(self, **kwargs) -> dict:
        kwargs.setdefault("mode", "standard")
        return local_only.check(self.root, home=self.home, **kwargs)

    def plan(self, **kwargs) -> dict:
        return local_only.plan_fix(self.root, home=self.home, **kwargs)

    def fix(self, **kwargs) -> dict:
        plan = self.plan(**kwargs)
        local_only.apply_fix(plan, home=self.home)
        return plan

    def ids(self, report: dict) -> list[str]:
        return [finding["id"] for finding in report["findings"]]

    def store(self, project_id: str) -> Path:
        return Path(local_only.store_root(self.home)) / project_id

    def config(self) -> dict:
        return json.loads((self.root / local_only.CONFIG_PATH).read_text(encoding="utf-8"))


class ServiceAndConfigTests(Fixture):
    def test_services_are_recognised_by_path_including_file_provider_names(self) -> None:
        cases = {"icloud": ("Library", "Mobile Documents", "com~apple~CloudDocs", "n"),
                 "dropbox": ("Library", "CloudStorage", "Dropbox-Personal", "n"),
                 "onedrive": ("OneDrive - Example Org", "n"), "google-drive": ("GoogleDrive-someone", "My Drive", "n")}
        for identifier, parts in cases.items():
            with self.subTest(service=identifier):
                path = self.base.joinpath("svc-" + identifier, *parts)
                path.mkdir(parents=True)
                self.assertEqual(local_only.detect_service(path, None)["id"], identifier)

    def test_an_ordinary_folder_and_a_declared_none_have_no_service(self) -> None:
        plain = self.base / "work" / "dropbox-tools"
        plain.mkdir(parents=True)
        self.assertIsNone(local_only.detect_service(plain, None))
        self.assertIsNone(local_only.detect_service(self.root, "none"))

    def test_a_declared_service_wins_over_the_path(self) -> None:
        found = local_only.detect_service(self.root, "dropbox")
        self.assertEqual((found["id"], found["source"]), ("dropbox", "declared"))

    def test_a_project_outside_sync_folders_with_no_config_is_not_scanned(self) -> None:
        plain = self.base / "plain"
        (plain / ".venv").mkdir(parents=True)
        (plain / ".venv/pyvenv.cfg").write_text("x\n", encoding="utf-8")
        with mock.patch.object(local_only, "scan_tree", side_effect=AssertionError("scanned")):
            report = local_only.check(plain, home=self.home)
        self.assertEqual(report["findings"], [])

    def test_defaults_user_config_and_project_config_stack_in_that_order(self) -> None:
        user = self.base / "user.json"
        user.write_text(json.dumps({"local_only": {"limits": {"dir_mb": 500, "warn_ratio": 0.5},
                                                   "keep_synced": ["dist"]}}), encoding="utf-8")
        config = local_only.load_config(self.root, user)
        self.assertEqual((config.limits["dir_mb"], config.limits["warn_ratio"], config.limits["dir_files"]), (500, 0.5, 20000))
        self.assertEqual(config.keep_synced, ["dist"])
        path = self.root / local_only.CONFIG_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1, "limits": {"dir_mb": 50, "overrides": {"docs": {"dir_mb": 900}}}}),
                        encoding="utf-8")
        config = local_only.load_config(self.root, user)
        self.assertEqual((config.limits["dir_mb"], config.limits["warn_ratio"]), (50, 0.5))
        self.assertEqual(config.limit_for("docs")[0], 900)
        self.assertEqual(config.limit_for("src")[0], 50)

    def test_bad_configuration_is_reported_and_does_not_break_the_check(self) -> None:
        path = self.root / local_only.CONFIG_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1, "sync_service": "carrier-pigeon",
                                    "limits": {"warn_ratio": 3, "dir_mb": -1}, "items": [{"path": "../x"}]}),
                        encoding="utf-8")
        report = self.check()
        self.assertIn("local:local-only-config", self.ids(report))
        text = " ".join(report["config"]["errors"])
        for expected in ("sync_service", "warn_ratio", "dir_mb", "../x"):
            self.assertIn(expected, text)
        path.write_text("{not json", encoding="utf-8")
        self.assertIn("无法读取", " ".join(self.check()["config"]["errors"]))


class ScanTests(Fixture):
    def test_virtualenvs_are_found_by_marker_at_any_depth_whatever_they_are_called(self) -> None:
        self.venv("services/api/.venv")
        self.venv("tools/my-env")
        report = self.check()
        found = {entry["path"]: entry["kind"] for entry in report["candidates"]}
        self.assertEqual(found, {"services/api/.venv": "venv", "tools/my-env": "venv"})
        self.assertEqual(sorted(self.ids(report)), ["local:real:services/api/.venv", "local:real:tools/my-env"])
        self.assertTrue(all(finding["attention"] for finding in report["findings"]))

    def test_tool_caches_are_found_by_name_or_by_cachedir_tag_and_grouped(self) -> None:
        (self.root / "a/.mypy_cache").mkdir(parents=True)
        (self.root / "b/some-tool-cache").mkdir(parents=True)
        (self.root / "b/some-tool-cache/CACHEDIR.TAG").write_text("Signature: x\n", encoding="utf-8")
        report = self.check()
        self.assertEqual(self.ids(report), ["local:cache"])
        reason = report["findings"][0]["reason"]
        self.assertIn("a/.mypy_cache（环境变量 MYPY_CACHE_DIR", reason)
        self.assertIn("b/some-tool-cache", reason)

    def test_build_outputs_and_tool_state_stay_synced_by_default(self) -> None:
        for name in ("dist", "build", ".claude", ".cursor", ".idea"):
            (self.root / name).mkdir()
            (self.root / name / "file").write_text("x\n", encoding="utf-8")
        report = self.check()
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["candidates"], [])

    def test_keep_synced_can_exempt_a_name_that_would_otherwise_be_flagged(self) -> None:
        self.venv("tools/keepme")
        path = self.root / local_only.CONFIG_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1, "keep_synced": ["keepme"]}), encoding="utf-8")
        self.assertEqual(self.check()["candidates"], [])

    def test_ignore_skips_directories_entirely_and_git_is_never_entered(self) -> None:
        self.venv("vendor/.venv")
        self.venv(".git/hooks-env")
        path = self.root / local_only.CONFIG_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1, "ignore": ["vendor"]}), encoding="utf-8")
        self.assertEqual(self.check()["candidates"], [])

    def test_runtime_state_files_conflict_copies_and_placeholders_are_reported(self) -> None:
        (self.root / "data").mkdir()
        for name in ("data/app.db-wal", "data/app.db-shm", "run.pid", "notes (conflicted copy 2026-10-29).md",
                     ".report.pdf.icloud", "uv.lock", "package-lock.json"):
            (self.root / name).write_text("x\n", encoding="utf-8")
        report = self.check()
        self.assertEqual(sorted(self.ids(report)), ["local:conflict-copies", "local:runtime-files"])
        runtime = next(f for f in report["findings"] if f["id"] == "local:runtime-files")
        self.assertNotIn("uv.lock", runtime["reason"])  # *.lock 是依赖锁，必须同步
        self.assertTrue(any("尚未下载" in note for note in report["notes"]))

    def test_quick_mode_looks_two_levels_deep_and_skips_sizes(self) -> None:
        self.venv("a/b/.venv")
        self.venv("a/b/c/d/.venv")
        report = self.check(mode="quick")
        self.assertEqual([entry["path"] for entry in report["candidates"]], ["a/b/.venv"])
        self.assertEqual(report["sizes"], [])

    def test_scanning_stops_at_the_time_budget_and_says_so(self) -> None:
        self.venv("a/.venv")
        with mock.patch.dict(local_only.BUDGETS, {"standard": -1.0}):
            report = self.check()
        self.assertTrue(report["truncated"])
        self.assertTrue(any("时间预算" in note for note in report["notes"]))

    def test_check_never_changes_the_project(self) -> None:
        self.venv()
        self.node_modules("web/node_modules", "npm")
        (self.root / "notes (conflicted copy).md").write_text("x\n", encoding="utf-8")

        def digest() -> dict:
            return {str(p.relative_to(self.base)): (p.read_bytes() if p.is_file() else None)
                    for p in sorted(self.base.rglob("*")) if not p.is_symlink()}
        before = digest()
        self.check()
        local_only.render_text(self.check())
        self.assertEqual(digest(), before)


class PackageManagerTests(Fixture):
    def managers(self) -> dict:
        return {entry["dir"]: entry for entry in self.check()["package_managers"]}

    def test_each_manager_gets_the_measured_verdict(self) -> None:
        for name, manager in (("npm-app", "npm"), ("pnpm-app", "pnpm"), ("yarn-app", "yarn"), ("bun-app", "bun")):
            self.node_modules(f"{name}/node_modules", manager)
        found = self.managers()
        self.assertEqual({k: v["verdict"] for k, v in found.items()},
                         {"npm-app": "avoid", "pnpm-app": "avoid", "yarn-app": "ok", "bun-app": "ok"})

    def test_npm_and_pnpm_in_a_synced_folder_are_advised_against_with_the_reason(self) -> None:
        self.node_modules("a/node_modules", "npm")
        self.node_modules("b/node_modules", "pnpm")
        report = self.check()
        advice = {f["id"]: f for f in report["findings"] if f["kind"] == "package-manager"}
        self.assertIn("local:pm:a", advice)
        self.assertIn("换成真实目录", advice["local:pm:a"]["reason"])
        self.assertIn("安装失败", advice["local:pm:b"]["reason"])
        self.assertIn("yarn", advice["local:pm:a"]["next"])
        for entry in report["candidates"]:
            self.assertFalse(entry["linkable"])

    def test_outside_a_synced_folder_nothing_is_said_about_npm(self) -> None:
        plain = self.base / "plain"
        (plain / "node_modules").mkdir(parents=True)
        (plain / "package-lock.json").write_text("", encoding="utf-8")
        (plain / "package.json").write_text("{}", encoding="utf-8")
        self.assertEqual(local_only.check(plain, home=self.home)["findings"], [])

    def test_package_manager_field_beats_the_lockfile_and_yarn_berry_is_unverified(self) -> None:
        self.node_modules("a/node_modules", "npm")
        (self.root / "a/package.json").write_text(json.dumps({"packageManager": "bun@1.4.4"}), encoding="utf-8")
        self.node_modules("b/node_modules", "yarn")
        (self.root / "b/package.json").write_text(json.dumps({"packageManager": "yarn@4.1.0"}), encoding="utf-8")
        found = self.managers()
        self.assertEqual((found["a"]["pm"], found["a"]["source"], found["a"]["verdict"]), ("bun", "packageManager", "ok"))
        self.assertEqual(found["b"]["verdict"], "unverified")

    def test_ambiguous_lockfiles_leave_the_manager_unknown_and_still_externalizable(self) -> None:
        self.node_modules("web/node_modules", "npm")
        (self.root / "web/yarn.lock").write_text("", encoding="utf-8")
        self.assertEqual(self.managers()["web"]["verdict"], "unknown")
        entry = self.check()["candidates"][0]
        self.assertTrue(entry["linkable"])


class ExternalizeTests(Fixture):
    def test_preview_writes_nothing_and_apply_moves_the_venv_behind_a_link(self) -> None:
        venv = self.venv()
        before = (venv / "bin/pip").read_text(encoding="utf-8")
        plan = self.plan()
        self.assertTrue(venv.is_dir() and not venv.is_symlink())
        self.assertFalse((self.root / local_only.CONFIG_PATH).exists())
        self.assertEqual([step["action"] for step in plan["steps"]], ["write-config", "externalize"])
        local_only.apply_fix(plan, home=self.home)
        target = f"{local_only.store_root(self.home)}/{plan['project_id']}/.venv"
        self.assertTrue(venv.is_symlink())
        self.assertEqual(os.readlink(venv), target)
        self.assertEqual((venv / "bin/pip").read_text(encoding="utf-8"), before)
        document = self.config()
        self.assertEqual((document["home"], document["project_id"], document["items"]),
                         (str(self.home), plan["project_id"], [{"path": ".venv", "kind": "venv"}]))

    def test_the_preview_and_the_applied_project_id_are_the_same(self) -> None:
        self.venv()
        self.assertEqual(self.plan()["project_id"], self.plan()["project_id"])

    def test_after_externalizing_the_check_is_clean_and_rerunning_fix_does_nothing(self) -> None:
        self.venv()
        self.fix()
        report = self.check()
        self.assertEqual([item["state"] for item in report["items"]], ["ok"])
        self.assertEqual(report["findings"], [])
        self.assertTrue(self.plan()["empty"])

    def test_yarn_node_modules_are_externalized_nested_and_npm_is_refused(self) -> None:
        self.node_modules("web/node_modules", "yarn")
        self.node_modules("api/node_modules", "npm")
        plan = self.plan()
        self.assertEqual([step["path"] for step in plan["steps"] if step["action"] == "externalize"], ["web/node_modules"])
        self.assertTrue(any("api/node_modules" in message and "换成真实目录" in message for message in plan["blocked"]))
        local_only.apply_fix(plan, home=self.home)
        self.assertTrue((self.root / "api/node_modules").is_dir())
        self.assertFalse((self.root / "api/node_modules").is_symlink())
        self.assertEqual((self.store(plan["project_id"]) / "web/node_modules/pkg/index.js").read_text(encoding="utf-8"), "1\n")

    def test_explicit_externalize_of_an_unknown_directory_warns_about_other_devices(self) -> None:
        (self.root / "scratch").mkdir()
        (self.root / "scratch/a.txt").write_text("x\n", encoding="utf-8")
        plan = self.plan(externalize=["scratch"])
        self.assertIn("其他设备将看不到", plan["steps"][1]["detail"])

    def test_a_directory_that_is_not_known_to_be_linkable_is_only_reported_by_default(self) -> None:
        (self.root / ".tox").mkdir()
        report = self.check()
        self.assertFalse(report["candidates"][0]["linkable"])
        self.assertTrue(self.plan()["empty"])

    def test_cross_device_moves_copy_verify_then_delete(self) -> None:
        self.venv()
        real_rename = os.rename

        def rename(source, destination):
            if str(source).endswith(".venv"):
                raise OSError(errno.EXDEV, "cross-device")
            return real_rename(source, destination)
        with mock.patch.object(os, "rename", rename):
            plan = self.fix()
        self.assertTrue((self.root / ".venv").is_symlink())
        self.assertTrue((self.store(plan["project_id"]) / ".venv/bin/pip").is_file())

    def test_a_failure_part_way_restores_the_directory_and_removes_the_new_config(self) -> None:
        self.venv()
        plan = self.plan()
        with mock.patch.object(os, "symlink", side_effect=OSError("no symlinks here")):
            with self.assertRaises(OSError):
                local_only.apply_fix(plan, home=self.home)
        self.assertTrue((self.root / ".venv/bin/pip").is_file())
        self.assertFalse((self.root / ".venv").is_symlink())
        self.assertFalse((self.root / local_only.CONFIG_PATH).exists())
        self.assertFalse(self.store(plan["project_id"]).joinpath(".venv").exists())

    def test_a_failed_cross_device_move_is_restored_too(self) -> None:
        self.venv()
        plan = self.plan()
        real_rename = os.rename

        def rename(source, destination):
            if str(source).endswith(".venv") and "local-only" in str(destination):
                raise OSError(errno.EXDEV, "cross-device")
            return real_rename(source, destination)
        with mock.patch.object(os, "rename", rename), mock.patch.object(os, "symlink", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                local_only.apply_fix(plan, home=self.home)
        self.assertTrue((self.root / ".venv/bin/pip").is_file())
        self.assertFalse((self.root / ".venv").is_symlink())

    def test_existing_config_is_backed_up_and_user_fields_survive(self) -> None:
        self.venv()
        path = self.root / local_only.CONFIG_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1, "keep_synced": ["dist"], "note": "mine"}), encoding="utf-8")
        plan = self.plan()
        result = local_only.apply_fix(plan, home=self.home, recovery_root=self.base / "recovery")
        self.assertTrue(Path(result["config_backup"]).is_file())
        document = self.config()
        self.assertEqual((document["note"], document["keep_synced"]), ("mine", ["dist"]))
        self.assertIn("items", document)

    def test_unsafe_paths_and_blocked_cases_are_refused_with_a_reason(self) -> None:
        for bad in ("../outside", "/abs", ".", ".git/x"):
            with self.subTest(path=bad):
                with self.assertRaises(local_only.LocalOnlyError):
                    self.plan(externalize=[bad])
        (self.root / "real").mkdir()
        (self.root / "real/.venv").mkdir()
        shutil.rmtree(self.root / "real")
        self.venv("linked/.venv")
        outside = self.base / "outside"
        outside.mkdir()
        shutil.move(self.root / "linked", outside / "linked")
        (self.root / "linked").symlink_to(outside / "linked", target_is_directory=True)
        with self.assertRaises(local_only.LocalOnlyError):
            self.plan(externalize=["linked/.venv"])

    def test_a_broken_project_config_is_never_overwritten_but_a_broken_user_config_does_not_block(self) -> None:
        self.venv()
        path = self.root / local_only.CONFIG_PATH
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(local_only.LocalOnlyError) as context:
            self.plan()
        self.assertEqual(context.exception.code, "config-invalid")
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")
        path.unlink()
        user = self.base / "user.json"
        user.write_text(json.dumps({"local_only": {"limits": {"dir_mb": -5}}}), encoding="utf-8")
        plan = self.plan(user_config=user)
        self.assertEqual(plan["steps"][0]["action"], "write-config")

    def test_a_target_that_already_has_content_is_not_overwritten(self) -> None:
        self.venv()
        first = self.plan()
        target = self.store(first["project_id"]) / ".venv"
        target.mkdir(parents=True)
        (target / "other-device-stuff").write_text("keep\n", encoding="utf-8")
        plan = self.plan()
        self.assertTrue(plan["empty"] or all(step["action"] != "externalize" for step in plan["steps"]))
        self.assertTrue(any("已有内容" in message for message in plan["blocked"]))
        self.assertTrue((target / "other-device-stuff").is_file())

    def test_a_link_pointing_elsewhere_is_left_alone(self) -> None:
        elsewhere = self.base / "mine"
        elsewhere.mkdir()
        (self.root / ".venv").symlink_to(elsewhere, target_is_directory=True)
        self.venv("tools/.venv")
        path = self.root / local_only.CONFIG_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1, "project_id": "p-1", "home": str(self.home),
                                    "items": [{"path": ".venv", "kind": "venv"}]}), encoding="utf-8")
        report = self.check()
        self.assertEqual(report["items"][0]["state"], "wrong-target")
        self.assertIn("local:link:.venv", self.ids(report))
        plan = self.plan(externalize=[".venv"])
        self.assertTrue(any("指向别处" in message for message in plan["blocked"]))
        self.assertEqual(os.readlink(self.root / ".venv"), str(elsewhere))

    def test_rebuild_runs_the_lockfile_command_when_the_tool_exists(self) -> None:
        self.venv()
        (self.root / "uv.lock").write_text("", encoding="utf-8")
        with mock.patch.object(shutil, "which", return_value="/usr/bin/uv"):
            plan = self.plan(rebuild=True)
        rebuild = [step for step in plan["steps"] if step["action"] == "rebuild"]
        self.assertEqual(rebuild[0]["command"], ["uv", "sync"])
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 3, "", "boom")) as run:
            result = local_only.apply_fix(plan, home=self.home)
        self.assertEqual(run.call_args.args[0], ["uv", "sync"])
        self.assertTrue(any("重建失败" in line and "退出码 3" in line for line in result["applied"]))
        self.assertTrue((self.root / ".venv").is_symlink())  # 重建失败不撤销外置

    def test_rebuild_without_a_lockfile_or_tool_is_skipped_with_a_reason(self) -> None:
        self.venv()
        plan = self.plan(rebuild=True)
        self.assertTrue(any("锁文件" in message for message in plan["blocked"]))


class SecondDeviceTests(Fixture):
    """项目文件随同步盘到了另一台设备：链接在，本机目录不在。"""

    def externalized(self) -> str:
        self.venv()
        (self.root / "uv.lock").write_text("", encoding="utf-8")
        return self.fix()["project_id"]

    def test_a_missing_local_target_is_reported_as_not_installed_with_the_rebuild_command(self) -> None:
        project_id = self.externalized()
        shutil.rmtree(self.store(project_id))
        report = self.check()
        self.assertEqual(report["items"][0]["state"], "not-installed")
        self.assertEqual(report["items"][0]["rebuild"], ["uv", "sync"])
        finding = next(f for f in report["findings"] if f["id"] == "local:link:.venv")
        self.assertFalse(finding["attention"])
        self.assertIn("uv sync", finding["next"])

    def test_fix_builds_the_local_directory_so_the_install_can_run(self) -> None:
        project_id = self.externalized()
        shutil.rmtree(self.store(project_id))
        plan = self.plan()
        self.assertEqual([step["action"] for step in plan["steps"]], ["make-target"])
        local_only.apply_fix(plan, home=self.home)
        self.assertTrue((self.store(project_id) / ".venv").is_dir())
        self.assertEqual(self.check()["items"][0]["state"], "ok")
        self.assertEqual(self.check()["findings"], [])

    def test_a_service_that_does_not_sync_links_leaves_the_item_absent_and_fix_recreates_it(self) -> None:
        self.externalized()
        (self.root / ".venv").unlink()
        self.assertEqual(self.check()["items"][0]["state"], "absent")
        local_only.apply_fix(self.plan(), home=self.home)
        self.assertEqual(self.check()["items"][0]["state"], "ok")

    def test_a_reinstall_that_replaces_the_link_is_caught_and_can_be_externalized_again(self) -> None:
        project_id = self.externalized()
        (self.root / ".venv").unlink()
        shutil.rmtree(self.store(project_id))
        self.venv()
        report = self.check()
        self.assertEqual(report["items"][0]["state"], "reverted")
        finding = next(f for f in report["findings"] if f["id"] == "local:link:.venv")
        self.assertTrue(finding["attention"])
        self.assertEqual([f["id"] for f in report["findings"]], ["local:link:.venv"])  # 不重复报 real
        self.fix()
        self.assertEqual(self.check()["items"][0]["state"], "ok")

    def test_a_different_home_is_a_hard_stop_for_writes_and_a_finding_for_reads(self) -> None:
        self.externalized()
        other = self.base / "other-home"
        other.mkdir()
        report = local_only.check(self.root, home=other)
        self.assertFalse(report["home"]["ok"])
        self.assertEqual(report["items"][0]["state"], "home-mismatch")
        finding = next(f for f in report["findings"] if f["id"] == "local:home-mismatch")
        self.assertTrue(finding["attention"])
        self.assertEqual([f["id"] for f in report["findings"]].count("local:link:.venv"), 0)
        with self.assertRaises(local_only.LocalOnlyError) as context:
            local_only.plan_fix(self.root, home=other)
        self.assertEqual(context.exception.code, "home-mismatch")

    def test_a_service_can_be_declared_and_turned_off(self) -> None:
        plain = self.base / "Documents" / "proj"
        plain.mkdir(parents=True)
        (plain / ".venv").mkdir()
        (plain / ".venv/pyvenv.cfg").write_text("x\n", encoding="utf-8")
        self.assertEqual(local_only.check(plain, home=self.home)["findings"], [])
        local_only.apply_fix(local_only.plan_fix(plain, home=self.home, service="icloud"), home=self.home)
        report = local_only.check(plain, home=self.home)
        self.assertEqual(report["service"]["source"], "declared")
        self.assertEqual(report["items"], [])
        self.assertIn("local:real:.venv", [f["id"] for f in report["findings"]])
        local_only.apply_fix(local_only.plan_fix(plain, home=self.home, service="none"), home=self.home)
        self.assertEqual(local_only.check(plain, home=self.home)["findings"], [])


class SizeGuardTests(Fixture):
    def fill(self, relative: str, size: int, files: int = 1) -> None:
        directory = self.root / relative
        directory.mkdir(parents=True, exist_ok=True)
        for number in range(files):
            (directory / f"f{number}.bin").write_bytes(b"\0" * (size // files))

    def limits(self, **limits) -> None:
        path = self.root / local_only.CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "limits": limits}), encoding="utf-8")

    def sizes(self) -> dict:
        return {entry["path"]: entry for entry in self.check()["sizes"]}

    def test_below_warn_approaching_and_exceeded(self) -> None:
        self.limits(dir_mb=10, warn_ratio=0.8)
        self.fill("small", 1024 * 1024)
        self.fill("near", 9 * 1024 * 1024)
        self.fill("over", 11 * 1024 * 1024)
        sizes = self.sizes()
        self.assertEqual([sizes[name]["state"] for name in ("small", "near", "over")], [None, "approaching", "exceeded"])
        findings = {f["id"]: f for f in self.check()["findings"]}
        self.assertNotIn("local:size:small", findings)
        self.assertFalse(findings["local:size:near"]["attention"])
        self.assertTrue(findings["local:size:over"]["attention"])

    def test_the_file_count_limit_triggers_too(self) -> None:
        self.limits(dir_mb=1000, dir_files=100)
        self.fill("many", 100, files=90)
        self.assertEqual(self.sizes()["many"]["state"], "approaching")

    def test_the_user_is_offered_the_choices_rather_than_a_blanket_action(self) -> None:
        self.limits(dir_mb=10)
        self.fill("near", 9 * 1024 * 1024)
        finding = next(f for f in self.check()["findings"] if f["id"] == "local:size:near")
        labels = "\n".join(option["label"] for option in finding["options"])
        for expected in ("--raise-limit near=", "--skip local:size:near", "不同步"):
            self.assertIn(expected, labels)
        self.assertNotIn("--externalize near", labels)  # 普通内容外置会让其他设备看不到

    def test_raising_the_limit_through_fix_clears_the_finding_and_keeps_other_settings(self) -> None:
        self.limits(dir_mb=10, warn_ratio=0.8)
        self.fill("near", 9 * 1024 * 1024)
        plan = self.plan(raise_limit={"near": {"dir_mb": 100, "dir_files": 500}})
        self.assertEqual([step["action"] for step in plan["steps"]], ["write-config", "raise-limit"])
        local_only.apply_fix(plan, home=self.home)
        document = self.config()
        self.assertEqual(document["limits"]["dir_mb"], 10)
        self.assertEqual(document["limits"]["overrides"]["near"], {"dir_mb": 100, "dir_files": 500})
        self.assertNotIn("local:size:near", self.ids(self.check()))

    def test_the_fingerprint_changes_when_the_state_or_the_limit_changes_so_old_decisions_lapse(self) -> None:
        self.limits(dir_mb=10)
        self.fill("near", 9 * 1024 * 1024)
        approaching = next(f for f in self.check()["findings"] if f["id"] == "local:size:near")["fingerprint"]
        self.fill("near", 12 * 1024 * 1024)
        exceeded = next(f for f in self.check()["findings"] if f["id"] == "local:size:near")["fingerprint"]
        self.assertNotEqual(approaching, exceeded)

    def test_directories_that_are_moved_out_do_not_count_toward_the_size(self) -> None:
        self.limits(dir_mb=10)
        venv = self.venv("app/.venv")
        (venv / "big").write_bytes(b"\0" * (20 * 1024 * 1024))
        self.assertNotIn("app", self.sizes())
        self.fix()
        self.assertNotIn("local:size:app", self.ids(self.check()))

    def test_bad_threshold_arguments_are_rejected(self) -> None:
        for bad in ("near", "near=0", "near=x", "=5", "near=5/0"):
            with self.subTest(value=bad), self.assertRaises(local_only.LocalOnlyError):
                local_only._parse_limits([bad])
        self.assertEqual(local_only._parse_limits(["near=5", "docs/=7/300"]),
                         {"near": {"dir_mb": 5.0}, "docs": {"dir_mb": 7.0, "dir_files": 300}})


class FingerprintTests(Fixture):
    def test_projects_without_a_config_have_no_fingerprint_detail(self) -> None:
        self.assertEqual(self.check()["fingerprint"], {"fingerprint": "", "detail": {}})

    def test_two_devices_that_agree_have_the_same_fingerprint_and_installing_one_makes_them_differ(self) -> None:
        self.venv()
        project_id = self.fix()["project_id"]
        mine = self.check()["fingerprint"]
        self.assertEqual(mine["detail"]["home"], str(self.home))
        self.assertEqual(local_only.explain(mine["detail"], dict(mine["detail"])), [])
        shutil.rmtree(self.store(project_id))
        theirs = self.check()["fingerprint"]
        lines = local_only.explain(mine["detail"], theirs["detail"])
        self.assertEqual(len(lines), 1)
        self.assertIn(".venv", lines[0])
        self.assertIn("ok", lines[0])
        self.assertIn("not-installed", lines[0])

    def test_a_different_home_and_link_text_are_named(self) -> None:
        self.venv()
        self.fix()
        mine = self.check()["fingerprint"]["detail"]
        theirs = dict(mine)
        theirs["home"] = "/home/other"
        theirs[".venv"] = "x"
        theirs["link:.venv"] = "ok|/home/other/.local/share/agenthelm/local-only/p/.venv"
        text = "\n".join(local_only.explain(mine, theirs))
        self.assertIn("HOME 不同", text)
        self.assertIn("链接文本不同", text)

    def test_platform_and_sizes_do_not_enter_the_fingerprint(self) -> None:
        self.venv()
        self.fix()
        first = self.check()["fingerprint"]["fingerprint"]
        (self.root / "docs").mkdir()
        (self.root / "docs/a.bin").write_bytes(b"\0" * 5000)
        with mock.patch.object(sys, "platform", "darwin"):
            second = self.check()["fingerprint"]["fingerprint"]
        self.assertEqual(first, second)


class CommandLineTests(Fixture):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = agenthelm.main(["local-only", *argv])
        return code, out.getvalue(), err.getvalue()

    def common(self) -> list[str]:
        return ["--project", str(self.root), "--home", str(self.home)]

    def test_check_fix_and_check_again_through_the_agenthelm_command(self) -> None:
        self.venv()
        code, text, _ = self.run_cli("check", *self.common())
        self.assertEqual(code, 0)
        self.assertIn("local:real:.venv", text)
        code, text, _ = self.run_cli("check", *self.common(), "--strict")
        self.assertEqual(code, 1)
        code, text, _ = self.run_cli("fix", *self.common())
        self.assertEqual(code, 0)
        self.assertIn("以上为预览", text)
        self.assertTrue((self.root / ".venv").is_dir() and not (self.root / ".venv").is_symlink())
        code, text, _ = self.run_cli("fix", *self.common(), "--apply", "--output", "json")
        payload = json.loads(text)
        self.assertTrue(payload["applied"] and payload["ok"])
        code, _, _ = self.run_cli("check", *self.common(), "--strict")
        self.assertEqual(code, 0)

    def test_errors_are_exit_code_2_with_a_message(self) -> None:
        code, _, err = self.run_cli("fix", *self.common(), "--externalize", "../x")
        self.assertEqual(code, 2)
        self.assertIn("项目内的相对路径", err)
        code, text, _ = self.run_cli("fix", *self.common(), "--externalize", "../x", "--output", "json")
        self.assertEqual((code, json.loads(text)["ok"]), (2, False))
        code, _, err = self.run_cli("fix", *self.common(), "--raise-limit", "nope")
        self.assertEqual(code, 2)

    def test_config_command_shows_effective_settings(self) -> None:
        code, text, _ = self.run_cli("config", *self.common(), "--output", "json")
        payload = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(payload["limits"], local_only.DEFAULT_LIMITS)
        self.assertEqual(payload["store"], local_only.store_root(self.home))
        self.assertIn("dist", payload["keep_synced"])

    def test_fingerprint_and_explain_across_two_devices(self) -> None:
        self.venv()
        project_id = self.fix()["project_id"]
        code, text, _ = self.run_cli("check", *self.common(), "--fingerprint", "--output", "json")
        mine = json.loads(text)
        self.assertEqual(code, 0)
        self.assertIn("link:.venv", mine["local_only_fingerprint"]["detail"])
        other = self.base / "device-a.json"
        other.write_text(text, encoding="utf-8")
        code, text, _ = self.run_cli("check", *self.common(), "--fingerprint", "--explain", str(other))
        self.assertEqual(code, 0)
        self.assertIn("一致", text)
        shutil.rmtree(self.store(project_id))
        code, text, _ = self.run_cli("check", *self.common(), "--fingerprint", "--explain", str(other))
        self.assertEqual(code, 1)
        self.assertIn("not-installed", text)
        broken = self.base / "broken.json"
        broken.write_text("{}", encoding="utf-8")
        code, _, err = self.run_cli("check", *self.common(), "--fingerprint", "--explain", str(broken))
        self.assertEqual(code, 2)

    def test_the_check_command_applies_decisions_recorded_on_this_machine(self) -> None:
        self.venv()
        env = agenthelm.current_env(self.home)
        fingerprint = next(f["fingerprint"] for f in self.check()["findings"] if f["id"] == "local:real:.venv")
        agenthelm.scaffold_module(env).decide(self.root, "local:real:.venv", "skip", reason="mine", apply=True,
                                              fingerprint=fingerprint, scope="machine", state_dir=env.state_dir)
        code, text, _ = self.run_cli("check", *self.common(), "--output", "json")
        report = json.loads(text)
        self.assertNotIn("local:real:.venv", [f["id"] for f in report["findings"]])
        self.assertEqual([item["id"] for item in report["skipped"]], ["local:real:.venv"])
        code, text, _ = self.run_cli("check", *self.common(), "--output", "json", "--reprompt")
        self.assertIn("local:real:.venv", [f["id"] for f in json.loads(text)["findings"]])


class AgentHelmCheckIntegrationTests(unittest.TestCase):
    """agenthelm check / 会话开始提示：只在同步盘或有配置的项目里多出内容，其余输出不变。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = agenthelm.Env(home=self.home, variables={"XDG_STATE_HOME": str(self.base / "state")})
        self.latest = self.base / "latest.json"
        self.latest.write_text(json.dumps(LATEST), encoding="utf-8")

    def project(self, *parts: str) -> Path:
        root = self.base.joinpath(*parts)
        root.mkdir(parents=True)
        done = subprocess.run([sys.executable, str(INIT), "--project", str(root), "--name", "Sample", "--slug", "sample",
                               "--project-type", "notes", "--vcs", "none", "--stack", "md", "--runtime", "local",
                               "--agent-cli", "none", "--no-entry", "--apply", "--output", "json"],
                              capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stdout)
        return root

    def report(self, root: Path, **kwargs) -> dict:
        return agenthelm.run_check(self.env, root, latest_source=str(self.latest), **kwargs)

    def test_a_synced_project_with_a_real_venv_counts_against_up_to_date(self) -> None:
        root = self.project("iCloud Drive", "proj")
        (root / ".venv/bin").mkdir(parents=True)
        (root / ".venv/pyvenv.cfg").write_text("x\n", encoding="utf-8")
        report = self.report(root)
        ids = [item["id"] for item in report["machine"]["findings"]]
        self.assertIn("local:real:.venv", ids)
        self.assertIn("local:sync-folder", ids)
        self.assertEqual(report["summary"]["local_only_attention"], 1)
        self.assertFalse(report["summary"]["up_to_date"])
        self.assertIn("本机专属目录", agenthelm.one_line(report))
        text = agenthelm.render_text(report)
        self.assertIn("local:real:.venv", text)

    def test_dismissing_it_on_this_machine_clears_the_count(self) -> None:
        root = self.project("iCloud Drive", "proj")
        (root / ".venv").mkdir()
        (root / ".venv/pyvenv.cfg").write_text("x\n", encoding="utf-8")
        scaffold = agenthelm.scaffold_module(self.env)
        for item in self.report(root)["machine"]["findings"]:
            scaffold.decide(root, item["id"], "skip", reason="x", apply=True, fingerprint=item["fingerprint"],
                            scope="machine", state_dir=self.env.state_dir)
        report = self.report(root)
        self.assertEqual(report["summary"]["local_only_attention"], 0)
        self.assertTrue(report["summary"]["up_to_date"])
        self.assertIn("local:real:.venv", [item["id"] for item in report["machine"]["skipped"]])

    def test_an_ordinary_project_is_untouched(self) -> None:
        root = self.project("work", "proj")
        (root / ".venv").mkdir()
        (root / ".venv/pyvenv.cfg").write_text("x\n", encoding="utf-8")
        report = self.report(root)
        self.assertEqual([item for item in report["machine"]["findings"] if item["id"].startswith("local:real")], [])
        self.assertEqual(report["summary"]["local_only_attention"], 0)
        self.assertTrue(report["summary"]["up_to_date"])

    def test_the_session_start_hook_uses_the_quick_scan(self) -> None:
        root = self.project("iCloud Drive", "proj")
        big = root / "a/b/c/d/.venv"
        big.mkdir(parents=True)
        (big / "pyvenv.cfg").write_text("x\n", encoding="utf-8")
        (root / ".venv").mkdir()
        (root / ".venv/pyvenv.cfg").write_text("x\n", encoding="utf-8")
        report = self.report(root, hook_mode=True)
        found = [item["id"] for item in report["machine"]["findings"] if item["id"].startswith("local:real")]
        self.assertEqual(found, ["local:real:.venv"])

    def test_the_fingerprint_output_gains_a_local_only_section_only_when_configured(self) -> None:
        root = self.project("iCloud Drive", "proj")
        args = ["check", "--project", str(root), "--home", str(self.home), "--latest", str(self.latest),
                "--fingerprint", "--output", "json"]
        out = io.StringIO()
        with redirect_stdout(out):
            agenthelm.main(args)
        self.assertNotIn("local_only", json.loads(out.getvalue()))
        (root / ".venv").mkdir()
        (root / ".venv/pyvenv.cfg").write_text("x\n", encoding="utf-8")
        local_only.apply_fix(local_only.plan_fix(root, home=self.home), home=self.home)
        out = io.StringIO()
        with redirect_stdout(out):
            agenthelm.main(args)
        self.assertIn("link:.venv", json.loads(out.getvalue())["local_only"]["detail"])

    def test_a_crash_inside_the_engine_does_not_break_check(self) -> None:
        root = self.project("iCloud Drive", "proj")
        with mock.patch.object(local_only, "check", side_effect=RuntimeError("boom")):
            report = self.report(root)
        self.assertIn("local:local-only-error", [item["id"] for item in report["machine"]["findings"]])


class MigrationStepTests(unittest.TestCase):
    def test_a_span_kept_for_the_release_gate_does_not_duplicate_the_single_steps_inside_it(self) -> None:
        def entry(start: str, end: str) -> dict:
            return {"plugin": "p", "from": start, "to": end, "summary": f"{start}->{end}"}
        latest = {"migrations": [entry("1.9.0", "1.10.0"), entry("1.10.0", "1.11.0"), entry("1.9.0", "1.11.0"),
                                 {**entry("1.0.0", "2.0.0"), "plugin": "other"}]}
        steps = agenthelm.migrations_between(latest, "p", "1.9.0", "1.11.0")
        self.assertEqual([step["summary"] for step in steps], ["1.9.0->1.10.0", "1.10.0->1.11.0"])
        steps = agenthelm.migrations_between(latest, "p", "1.10.0", "1.11.0")
        self.assertEqual([step["summary"] for step in steps], ["1.10.0->1.11.0"])
        self.assertEqual(agenthelm.migrations_between({"migrations": [entry("1.9.0", "1.11.0")]}, "p", "1.9.0", "1.11.0")[0]["summary"],
                         "1.9.0->1.11.0")


class McpToolTests(Fixture):
    def test_the_tool_is_listed_with_a_description_an_agent_can_act_on(self) -> None:
        tool = next(item for item in mcp_server.TOOLS if item["name"] == "agent_pack_cloud_sync_safety_check")
        self.assertEqual(tool["inputSchema"]["required"], ["project"])
        self.assertEqual(tool["inputSchema"]["properties"]["mode"]["enum"], ["quick", "standard", "full"])
        for expected in ("Read-only", "node_modules", "HOME", "agenthelm local-only fix", "never changes files"):
            self.assertIn(expected, tool["description"])

    def test_calling_it_returns_findings_and_changes_nothing(self) -> None:
        self.venv()
        before = sorted(str(path) for path in self.base.rglob("*"))
        result = mcp_server._call("agent_pack_cloud_sync_safety_check",
                                  {"project": str(self.root), "home": str(self.home), "mode": "full"})
        self.assertTrue(result["ok"])
        self.assertIn("local:real:.venv", [finding["id"] for finding in result["findings"]])
        self.assertEqual(result["summary"]["attention"], 1)
        self.assertEqual(sorted(str(path) for path in self.base.rglob("*")), before)

    def test_a_bad_mode_is_an_error(self) -> None:
        with self.assertRaises(mcp_server.AgentPackError):
            mcp_server._call("agent_pack_cloud_sync_safety_check", {"project": str(self.root), "mode": "everything"})

    def test_it_works_through_the_json_rpc_surface(self) -> None:
        response = mcp_server.handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "agent_pack_cloud_sync_safety_check",
                       "arguments": {"project": str(self.root), "home": str(self.home)}}})
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["findings"], [])


if __name__ == "__main__":
    unittest.main()
