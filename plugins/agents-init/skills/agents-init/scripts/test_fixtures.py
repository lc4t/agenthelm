"""非空目录夹具矩阵与写入类不变量（issue #25 第 1、2 项）。

两层结构：

* ``FIXTURES``：一个夹具 = 一个函数，把「接管前就存在的用户目录」建在临时目录里。
  新增场景只需在这里加一项（见仓库根的 CONTRIBUTING.md「如何新增夹具」）。
* ``assert_*`` 通用断言：对任意夹具、任意写入命令成立的不变量（预演零写入、用户字节不变、幂等、
  备份先于替换且冲突时失败不覆盖、失败不留半成品）。其他测试文件可 ``import test_fixtures as fx`` 复用。

所有命令都在临时 HOME 里以子进程运行，不碰真实的 ~/.agent-pack、~/.config。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from unittest import mock

os.environ["AGENTHELM_LANG"] = "zh"  # 断言中文输出；英文由 tests/test_i18n*.py 覆盖

TEST_HOME = tempfile.mkdtemp(prefix="agenthelm-fixture-home-")
os.environ["HOME"] = TEST_HOME
import atexit  # noqa: E402

atexit.register(shutil.rmtree, TEST_HOME, ignore_errors=True)

SCRIPT = Path(__file__).with_name("init_project.py")
sys.path.insert(0, str(SCRIPT.parent))
import adopt  # noqa: E402
import init_project  # noqa: E402
import scaffold  # noqa: E402

scaffold.bind(init_project)
adopt.bind(init_project)

BASE_ARGS = ("--name", "Fixture Project", "--project-type", "notes", "--vcs", "none", "--stack", "markdown",
             "--runtime", "local", "--agent-cli", "claude", "--output", "json")


# ------------------------------------------------------------------ 快照与命令


def tree(*roots: Path) -> dict[str, tuple]:
    """目录树的逐字节快照：文件 = (sha256, 是否可执行)，符号链接 = 目标文本（不跟随），目录 = 标记。

    键带上根的序号，所以同时盯住项目目录、上级目录里的文件和符号链接指向的外部目录。
    """
    result: dict[str, tuple] = {}
    for number, root in enumerate(roots):
        if not os.path.lexists(root):
            result[f"{number}:<absent>"] = ("absent",)
            continue
        if root.is_file() and not root.is_symlink():
            result[f"{number}:."] = ("f", hashlib.sha256(root.read_bytes()).hexdigest(), bool(root.stat().st_mode & 0o111))
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            for name in sorted(directories + files):
                path = Path(current) / name
                key = f"{number}:{path.relative_to(root).as_posix()}"
                if path.is_symlink():
                    result[key] = ("l", os.readlink(path))
                elif path.is_dir():
                    result[key] = ("d",)
                else:
                    result[key] = ("f", hashlib.sha256(path.read_bytes()).hexdigest(), bool(path.stat().st_mode & 0o111))
    return result


def changed(before: dict, after: dict) -> list[str]:
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def cli(root: Path, mode: str, *extra: str, state: Path | None = None, home: Path | None = None) -> tuple[int, dict]:
    """运行 init_project.py；返回 (退出码, JSON)。输出不是 JSON 时放进 {"raw": ...} 以便断言信息。"""
    env = {**os.environ, "AGENTHELM_LANG": "zh"}
    if home is not None:
        env["HOME"] = str(home)
    command = [sys.executable, str(SCRIPT), "--project", str(root), "--mode", mode, *BASE_ARGS]
    if state is not None:
        command += ["--state-dir", str(state)]
    done = subprocess.run([*command, *extra], capture_output=True, text=True, check=False, env=env)
    try:
        return done.returncode, json.loads(done.stdout)
    except json.JSONDecodeError:
        return done.returncode, {"raw": done.stdout, "stderr": done.stderr}


# ------------------------------------------------------------------ 夹具


@dataclass
class Ctx:
    """夹具的工作区：base/proj 是项目目录，base 下的其他东西（上级文件、符号链接目标）也被监视。"""
    base: Path
    root: Path
    watch: list[Path] = field(default_factory=list)  # 项目目录以外需要保持不变的路径

    def write(self, relative: str, text: str = "x\n", *, executable: bool = False) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if executable:
            path.chmod(0o755)
        return path

    def outside(self, relative: str, text: str | None = "outside\n") -> Path:
        """项目目录以外的路径（上级目录文件，或符号链接的目标）；会被监视。"""
        path = self.base / relative
        if text is None:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.watch.append(path)
        return path

    def link(self, relative: str, target: Path) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target, target_is_directory=target.is_dir())


USER_AGENTS = "# 我的项目\n\n## 编码规则\n\n- 必须先跑测试\n\n## 回复偏好\n\n- 用中文\n"


@dataclass(frozen=True)
class Fixture:
    name: str
    build: Callable[[Ctx], None]
    expect: str = "ok"  # ok：adopt 成功；refuse：adopt 以 invalid-request 拒绝且不写任何东西
    note: str = ""


def _agents(c: Ctx) -> None: c.write("AGENTS.md", USER_AGENTS)
def _claude(c: Ctx) -> None: c.write("CLAUDE.md", "# 我的 Claude 说明\n\n用 plan mode。\n")
def _claude_local(c: Ctx) -> None: c.write("CLAUDE.local.md", "只属于我这台机器的说明\n")
def _all_three(c: Ctx) -> None: _agents(c); _claude(c); _claude_local(c)
def _parent_only(c: Ctx) -> None: c.outside("AGENTS.md", USER_AGENTS); c.outside("CLAUDE.md", "# parent\n")
def _foreign_dirs(c: Ctx) -> None:
    c.write("cases/2024-旧事项/notes.md", "历史\n")
    c.write("memory/old-note.txt", "旧记忆\n")
    c.write("docs/guide.md", "指南\n")
def _custom_case_names(c: Ctx) -> None:
    for name in ("alpha-launch", "2024Q3 复盘", "ticket_1042", "CASE-1-no-date"):
        c.write(f"cases/{name}/case.md", f"# {name}\n")
def _symlink_docs_dir(c: Ctx) -> None: c.link("docs", c.outside("shared-docs", None))
def _symlink_memory_dir(c: Ctx) -> None:
    target = c.outside("shared-memory", None)
    (target / "n.txt").write_text("共享记忆\n", encoding="utf-8")
    c.link("memory", target)
def _symlink_agents_file(c: Ctx) -> None: c.link("AGENTS.md", c.outside("elsewhere/AGENTS.md", USER_AGENTS))
def _symlink_claude_local(c: Ctx) -> None: c.link("CLAUDE.local.md", c.outside("elsewhere/local.md"))
def _launcher_script(c: Ctx) -> None: c.write("menu", "#!/bin/sh\necho 我自己的菜单\n", executable=True)
def _launcher_dir(c: Ctx) -> None: (c.root / "menu").mkdir()
def _memory_is_a_file(c: Ctx) -> None: c.write("memory", "我是文件，不是目录\n")
def _cases_is_a_file(c: Ctx) -> None: c.write("cases", "我是文件，不是目录\n")
def _readme_and_other_agents(c: Ctx) -> None:
    c.write("README.md", "# 原有 README\n")
    c.write(".cursorrules", "别的客户端的规则\n")
    c.write(".github/copilot-instructions.md", "copilot\n")


FIXTURES: tuple[Fixture, ...] = (
    Fixture("agents-md", _agents),
    Fixture("claude-md", _claude),
    Fixture("claude-local-md", _claude_local),
    Fixture("all-three-contract-files", _all_three),
    Fixture("contract-files-only-in-parent", _parent_only, note="上级目录的 AGENTS.md / CLAUDE.md 不属于本项目，不得被读写"),
    Fixture("foreign-cases-memory-docs", _foreign_dirs),
    Fixture("user-case-naming", _custom_case_names),
    Fixture("symlinked-docs-dir", _symlink_docs_dir, note="符号链接父目录：不得写进链接目标"),
    Fixture("symlinked-memory-dir", _symlink_memory_dir, expect="refuse",
            note="现状：指向目录的符号链接被当成“同名文件”拒绝（提示语不提符号链接）；见汇报中的新问题草稿"),
    Fixture("symlinked-agents-file", _symlink_agents_file),
    Fixture("symlinked-claude-local", _symlink_claude_local),
    Fixture("launcher-name-taken-by-script", _launcher_script),
    Fixture("launcher-name-taken-by-dir", _launcher_dir),
    Fixture("readme-and-other-client-files", _readme_and_other_agents),
    Fixture("memory-root-is-a-file", _memory_is_a_file, expect="refuse"),
    Fixture("case-root-is-a-file", _cases_is_a_file, expect="refuse"),
)


def build_fixture(fixture: Fixture, base: Path) -> Ctx:
    root = base / "proj"
    root.mkdir()
    ctx = Ctx(base=base, root=root)
    fixture.build(ctx)
    return ctx


def watched(ctx: Ctx) -> tuple[Path, ...]:
    return (ctx.root, *ctx.watch)


# ------------------------------------------------------------------ 通用不变量断言


def assert_dry_run_writes_nothing(test: unittest.TestCase, ctx: Ctx, mode: str, *extra: str, home: Path | None = None) -> tuple[int, dict]:
    """预演：不带 --apply 的命令不得改动项目、上级目录或符号链接目标里的任何字节（含权限位里的可执行位）。"""
    before = tree(*watched(ctx))
    code, result = cli(ctx.root, mode, *extra, home=home)
    test.assertEqual(changed(before, tree(*watched(ctx))), [], f"{mode} 预演写了东西：{result}")
    return code, result


def assert_user_bytes_unchanged(test: unittest.TestCase, before: dict, after: dict, protected: list[str] | None = None) -> None:
    """保留/跳过路径下，接管前存在的每个条目（文件字节、符号链接目标、可执行位）都还在且相同。

    ``protected`` 缺省为接管前的全部条目；只给出部分时仅检查这些键。
    """
    keys = protected if protected is not None else [k for k, v in before.items() if v[0] != "absent"]
    for key in keys:
        if before[key][0] == "d":
            test.assertIn(key, after, f"目录 {key} 消失了")
        else:
            test.assertEqual(after.get(key), before[key], f"用户条目 {key} 被改动")


def assert_idempotent(test: unittest.TestCase, ctx: Ctx, mode: str, *extra: str, home: Path | None = None) -> None:
    """已应用后再应用一次：要么成功且没有任何变化，要么以明确的 code 拒绝且没有任何变化。"""
    after_first = tree(*watched(ctx))
    code, result = cli(ctx.root, mode, "--apply", *extra, home=home)
    test.assertEqual(changed(after_first, tree(*watched(ctx))), [], f"第二次 {mode} --apply 改动了文件：{result}")
    if code != 0:
        test.assertEqual(code, 2, result)
        test.assertEqual(result.get("code"), "already-initialized", result)


def fail_nth_write(n: int):
    """让 init_project._write_exclusive 在第 n 次调用时抛 OSError（注入中途失败）。"""
    original = init_project._write_exclusive
    calls = {"n": 0}

    def flaky(parent_fd: int, name: str, payload: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == n:
            raise OSError("injected failure")
        original(parent_fd, name, payload)

    return mock.patch.object(init_project, "_write_exclusive", side_effect=flaky)


# ------------------------------------------------------------------ 矩阵


class FixtureMatrixTests(unittest.TestCase):
    """对每个夹具：scan / adopt 预演零写入 → adopt --apply 保留用户字节 → 再执行一次是幂等的。"""

    def test_every_fixture_obeys_the_write_invariants(self) -> None:
        for fixture in FIXTURES:
            with self.subTest(fixture=fixture.name), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                (base / "home").mkdir()
                home = base / "home"
                ctx = build_fixture(fixture, base)
                state = ["--state-dir", str(base / "state")]
                watch_before = tree(*watched(ctx))

                code, scan = assert_dry_run_writes_nothing(self, ctx, "scan", home=home)
                self.assertEqual(code, 0, scan)
                code, preview = assert_dry_run_writes_nothing(self, ctx, "adopt", home=home)

                if fixture.expect == "refuse":
                    self.assertEqual((code, preview.get("code")), (2, "invalid-request"), preview)
                    code, applied = cli(ctx.root, "adopt", "--apply", *state, home=home)
                    self.assertEqual((code, applied.get("code")), (2, "invalid-request"), applied)
                    self.assertEqual(changed(watch_before, tree(*watched(ctx))), [], "拒绝时不得留下任何文件")
                    continue

                self.assertEqual(code, 0, preview)
                self.assertFalse(preview.get("applied"), preview)
                code, applied = cli(ctx.root, "adopt", "--apply", *state, home=home)
                self.assertEqual(code, 0, applied)
                self.assertEqual(applied.get("modify"), [], "缺省策略不得修改任何已有文件")
                after = tree(*watched(ctx))
                assert_user_bytes_unchanged(self, watch_before, after)
                self.assertIn("0:.agents/moe.sakanano.agent-pack/project.json", after, "骨架没有建出来")
                assert_idempotent(self, ctx, "adopt", *state, home=home)

    def test_files_behind_a_symlinked_parent_are_skipped_and_reported(self) -> None:
        by_name = {f.name: f for f in FIXTURES}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            (base / "home").mkdir()
            ctx = build_fixture(by_name["symlinked-docs-dir"], base)
            code, applied = cli(ctx.root, "adopt", "--apply", "--state-dir", str(base / "state"), home=base / "home")
            self.assertEqual(code, 0, applied)
            self.assertEqual(sorted(applied["skipped_symlinked_parents"]), ["docs/drafts/.gitkeep", "docs/refs/README.md"])
            self.assertEqual(list((base / "shared-docs").iterdir()), [], "链接目标里不得被写入文件")

    def test_symlinks_stay_symlinks(self) -> None:
        by_name = {f.name: f for f in FIXTURES}
        for name, relative in (("symlinked-agents-file", "AGENTS.md"), ("symlinked-claude-local", "CLAUDE.local.md")):
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                (base / "home").mkdir()
                ctx = build_fixture(by_name[name], base)
                target = os.readlink(ctx.root / relative)
                code, applied = cli(ctx.root, "adopt", "--apply", "--state-dir", str(base / "state"), home=base / "home")
                self.assertEqual(code, 0, applied)
                self.assertTrue((ctx.root / relative).is_symlink())
                self.assertEqual(os.readlink(ctx.root / relative), target)

    def test_a_user_launcher_keeps_its_name_and_no_other_launcher_is_invented(self) -> None:
        by_name = {f.name: f for f in FIXTURES}
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            (base / "home").mkdir()
            ctx = build_fixture(by_name["launcher-name-taken-by-script"], base)
            original = (ctx.root / "menu").read_bytes()
            code, applied = cli(ctx.root, "adopt", "--apply", "--state-dir", str(base / "state"), home=base / "home")
            self.assertEqual(code, 0, applied)
            self.assertEqual((ctx.root / "menu").read_bytes(), original)
            self.assertTrue(os.access(ctx.root / "menu", os.X_OK), "用户脚本的可执行位不得被去掉")
            self.assertNotIn("menu", applied.get("create", []))


# ------------------------------------------------------------------ 已初始化项目：缺锁、0.0.0 锁、v5 Profile


def init_project_dir(ctx: Ctx, home: Path, *extra: str) -> None:
    code, result = cli(ctx.root, "init", "--apply", *extra, home=home)
    assert code == 0, result


def lock_path(root: Path) -> Path:
    return root / init_project.CONTENT_LOCK_PATH


class InitializedFixtureTests(unittest.TestCase):
    """旧项目夹具：无内容锁、锁含 0.0.0、v5 Profile。预演零写入，应用后用户字节不变，重复执行幂等。"""

    def make(self, base: Path) -> tuple[Ctx, Path]:
        (base / "home").mkdir()
        root = base / "proj"
        root.mkdir()
        return Ctx(base=base, root=root), base / "home"

    def test_project_without_a_content_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, home = self.make(Path(temporary).resolve())
            init_project_dir(ctx, home)
            lock_path(ctx.root).unlink()
            (ctx.root / "AGENT.RULES.md").write_text("我自己的规则\n", encoding="utf-8")
            code, preview = assert_dry_run_writes_nothing(self, ctx, "refresh", home=home)
            self.assertEqual(code, 1, preview)
            self.assertIn("AGENT.RULES.md", preview["blocked"], "没有基线时，与模板不同的文件都算用户改过")
            before = tree(ctx.root)
            code, kept = cli(ctx.root, "refresh", "--apply", "--keep", "AGENT.RULES.md", "--keep", "AGENTS.md",
                             "--keep", "CLAUDE.md", "--keep", "AGENT.md", "--keep", "docs/refs/README.md", "--keep", "menu", home=home)
            self.assertEqual(code, 0, kept)
            after = tree(ctx.root)
            for key in ("0:AGENT.RULES.md",):
                self.assertEqual(after[key], before[key])
            assert_idempotent(self, ctx, "refresh", "--keep", "AGENT.RULES.md", home=home)

    def test_lock_with_version_zero_is_reported_and_user_text_survives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, home = self.make(Path(temporary).resolve())
            init_project_dir(ctx, home)
            lock = json.loads(lock_path(ctx.root).read_text(encoding="utf-8"))
            for entry in lock["packs"].values():
                entry["version"] = "0.0.0"
            lock_path(ctx.root).write_text(json.dumps(lock), encoding="utf-8")
            (ctx.root / "AGENTS.md").write_text("# 我改过\n", encoding="utf-8")
            code, check = assert_dry_run_writes_nothing(self, ctx, "check", "--state-dir", str(ctx.base / "state"), home=home)
            self.assertIn("lock", check)
            before = tree(ctx.root)
            code, refreshed = cli(ctx.root, "refresh", "--apply", "--keep", "AGENTS.md", home=home)
            self.assertEqual(code, 0, refreshed)
            self.assertEqual(tree(ctx.root)["0:AGENTS.md"], before["0:AGENTS.md"])
            entry = json.loads(lock_path(ctx.root).read_text(encoding="utf-8"))["packs"]["agents-init/project-contract"]
            self.assertNotEqual(entry["version"], "0.0.0", "刷新后锁应写入真实版本")

    def test_v5_profile_is_not_adoptable_and_migrate_previews_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, home = self.make(Path(temporary).resolve())
            legacy = ctx.root / init_project.LEGACY_PROFILE_PATH
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps({"schema_version": "1.0", "name": "Legacy", "profile": {"project_type": ["docs"]}}), encoding="utf-8")
            ctx.write("AGENTS.md", USER_AGENTS)
            code, scan = assert_dry_run_writes_nothing(self, ctx, "scan", home=home)
            self.assertEqual(code, 1)
            self.assertEqual(scan["blockers"][0]["code"], "migrate-required")
            code, preview = assert_dry_run_writes_nothing(self, ctx, "migrate", home=home)
            self.assertEqual(code, 0, preview)
            self.assertNotIn("AGENTS.md", preview.get("replace", []), "未被选中的用户文件不在替换清单里")
            before = tree(ctx.root)
            recovery = ctx.base / "recovery"
            code, applied = cli(ctx.root, "migrate", "--apply", "--recovery-dir", str(recovery), home=home)
            self.assertEqual(code, 0, applied)
            self.assertEqual(tree(ctx.root)["0:AGENTS.md"], before["0:AGENTS.md"])
            self.assertTrue((recovery / init_project.LEGACY_PROFILE_PATH).is_file(), "旧 Profile 先备份再退役")
            assert_idempotent(self, ctx, "migrate", "--recovery-dir", str(ctx.base / "recovery2"), home=home)


# ------------------------------------------------------------------ 备份与原子性


class BackupAndAtomicityTests(unittest.TestCase):
    def prepared(self, base: Path) -> tuple[Ctx, Path]:
        (base / "home").mkdir()
        root = base / "proj"
        root.mkdir()
        ctx = Ctx(base=base, root=root)
        init_project_dir(ctx, base / "home")
        (root / "AGENTS.md").write_text("我自己的规则\n", encoding="utf-8")
        # 模拟“旧版本渲染出的基线 + 用户改过”：基线与本地、与新模板三者都不同 => modified，必须由用户选择。
        lock = json.loads(lock_path(root).read_text(encoding="utf-8"))
        lock["packs"]["agents-init/project-contract"]["files"]["AGENTS.md"] = "0" * 64
        lock_path(root).write_text(json.dumps(lock), encoding="utf-8")
        return ctx, base / "home"

    def test_replacing_a_user_file_always_leaves_a_backup_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, home = self.prepared(Path(temporary).resolve())
            recovery = ctx.base / "recovery"
            code, result = cli(ctx.root, "refresh", "--apply", "--accept", "AGENTS.md", "--confirm-discard", "AGENTS.md",
                               "--recovery-dir", str(recovery), home=home)
            self.assertEqual(code, 0, result)
            self.assertEqual((recovery / "AGENTS.md").read_text(encoding="utf-8"), "我自己的规则\n")
            self.assertNotEqual((ctx.root / "AGENTS.md").read_text(encoding="utf-8"), "我自己的规则\n")

    def test_a_backup_that_already_exists_stops_the_command_and_nothing_is_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, home = self.prepared(Path(temporary).resolve())
            recovery = ctx.base / "recovery"
            earlier = recovery / "AGENTS.md"
            earlier.parent.mkdir(parents=True)
            earlier.write_text("更早的备份\n", encoding="utf-8")
            before = tree(ctx.root, recovery)
            code, result = cli(ctx.root, "refresh", "--apply", "--accept", "AGENTS.md", "--confirm-discard", "AGENTS.md",
                               "--recovery-dir", str(recovery), home=home)
            self.assertNotEqual(code, 0, result)
            self.assertEqual(changed(before, tree(ctx.root, recovery)), [], "备份冲突时项目文件和已有备份都不能被改动")

    def test_a_backup_folder_inside_the_project_is_refused_before_any_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, home = self.prepared(Path(temporary).resolve())
            before = tree(ctx.root)
            code, result = cli(ctx.root, "refresh", "--apply", "--accept", "AGENTS.md", "--confirm-discard", "AGENTS.md",
                               "--recovery-dir", str(ctx.root / "backup"), home=home)
            self.assertNotEqual(code, 0, result)
            self.assertEqual(changed(before, tree(ctx.root)), [])

    def test_a_failure_while_creating_leaves_no_half_built_skeleton(self) -> None:
        # 对每个夹具、在第 1..N 次写入处注入失败：要么什么都没写，要么已写的全部回滚。
        for fixture in FIXTURES:
            if fixture.expect != "ok":
                continue
            for n in (1, 3, 7):
                with self.subTest(fixture=fixture.name, failing_write=n), tempfile.TemporaryDirectory() as temporary:
                    base = Path(temporary).resolve()
                    ctx = build_fixture(fixture, base)
                    before = tree(*watched(ctx))
                    args = mock.Mock(agents_policy=None, claude_policy=None, route=[], register_root=[], case_root=None,
                                     memory_root=None, readme_entry=False, apply=True, recovery_dir=base / "rec",
                                     state_dir=base / "state")
                    inputs = init_project.Inputs(ctx.root, "Fixture", "fixture", ("notes",), "none", ("markdown",), "local",
                                                 ("claude",), "case-workspace", "menu")
                    with fail_nth_write(n):
                        try:
                            adopt.run(args, ctx.root, inputs)
                        except init_project.InitError:
                            pass
                    after = tree(*watched(ctx))
                    leftovers = [k for k in changed(before, after) if before.get(k) is not None]
                    self.assertEqual(leftovers, [], "用户原有条目被改动")
                    added = [k for k in changed(before, after) if before.get(k) is None]
                    if added:  # 注入点超过实际写入次数时，整套骨架已正常写完；否则必须一个不留
                        self.assertIn("0:.agents/moe.sakanano.agent-pack/project.json", after)
                        self.assertIn("0:.agents/moe.sakanano.agent-pack/content.lock.json", after)

    def refresh_failing_at_the_lock(self, base: Path) -> tuple[Path, dict]:
        """进程内运行 refresh --accept，让第 2 次 os.replace（写内容锁）失败；返回 (项目目录, 失败前快照)。"""
        (base / "home").mkdir()
        root = base / "proj"
        root.mkdir()
        init_project_dir(Ctx(base=base, root=root), base / "home")
        lock = json.loads(lock_path(root).read_text(encoding="utf-8"))
        lock["packs"]["agents-init/project-contract"]["files"]["AGENTS.md"] = "0" * 64
        lock_path(root).write_text(json.dumps(lock), encoding="utf-8")
        (root / "AGENTS.md").write_text("我自己的规则\n", encoding="utf-8")
        before = tree(root)
        real, calls = os.replace, {"n": 0}

        def flaky(src, dst, *a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("injected failure")
            return real(src, dst, *a, **k)

        argv = ["--project", str(root), "--mode", "refresh", *BASE_ARGS, "--apply", "--accept", "AGENTS.md",
                "--confirm-discard", "AGENTS.md", "--recovery-dir", str(base / "recovery")]
        with mock.patch.dict(os.environ, {"HOME": str(base / "home")}), \
                mock.patch.object(init_project.os, "replace", side_effect=flaky), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = init_project.main(argv)
        self.assertEqual(code, 2, "写锁失败必须以 InitError 报错（退出码 2），不能抛出未包装的 OSError")
        self.assertIn("injected failure", out.getvalue())
        self.assertIn("重新运行", out.getvalue())
        return root, before

    def test_a_failure_while_writing_the_lock_loses_no_user_text_and_the_next_refresh_converges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root, _before = self.refresh_failing_at_the_lock(base)
            self.assertEqual((base / "recovery/AGENTS.md").read_text(encoding="utf-8"), "我自己的规则\n", "替换前的备份必须已经在")
            code, result = cli(root, "refresh", home=base / "home")
            self.assertEqual((code, result["blocked"]), (0, []), result)
            self.assertEqual({item["path"]: item["state"] for item in result["files"]}["AGENTS.md"], "unchanged")

    def test_a_failure_while_writing_the_lock_leaves_no_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root, _before = self.refresh_failing_at_the_lock(base)
            strays = [p.name for p in root.rglob("*.tmp")]
            self.assertEqual(strays, [])

    def test_a_failure_replacing_a_file_leaves_no_temporary_file_and_the_next_refresh_converges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            (base / "home").mkdir()
            root = base / "proj"
            root.mkdir()
            init_project_dir(Ctx(base=base, root=root), base / "home")
            lock = json.loads(lock_path(root).read_text(encoding="utf-8"))
            lock["packs"]["agents-init/project-contract"]["files"]["AGENTS.md"] = "0" * 64
            lock_path(root).write_text(json.dumps(lock), encoding="utf-8")
            (root / "AGENTS.md").write_text("我自己的规则\n", encoding="utf-8")
            argv = ["--project", str(root), "--mode", "refresh", *BASE_ARGS, "--apply", "--accept", "AGENTS.md",
                    "--confirm-discard", "AGENTS.md", "--recovery-dir", str(base / "recovery")]
            with mock.patch.dict(os.environ, {"HOME": str(base / "home")}), \
                    mock.patch.object(init_project.os, "replace", side_effect=OSError("injected failure")), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = init_project.main(argv)
            self.assertEqual(code, 2)
            self.assertEqual([p.name for p in root.rglob("*.tmp")], [])
            self.assertEqual((root / "AGENTS.md").read_text(encoding="utf-8"), "我自己的规则\n")
            code, result = cli(root, "refresh", "--apply", "--accept", "AGENTS.md", "--confirm-discard", "AGENTS.md",
                               "--recovery-dir", str(base / "recovery2"), home=base / "home")
            self.assertEqual((code, result["blocked"]), (0, []), result)


if __name__ == "__main__":
    unittest.main()
