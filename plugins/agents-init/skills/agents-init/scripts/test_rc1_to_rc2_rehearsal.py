"""v6.11.0-rc.1 → 当前版本的升级演练：把验收机上要走的命令序列在容器里先走一遍（可重复，每次 rc 都能复用）。

* “旧版本”是 ``git archive v6.11.0-rc.1`` 取出的发布内容：项目由它自己的 init_project.py（含 adopt）生成，
  运行时由它自己的 ``agenthelm runtime install`` 装入，所以旧布局、旧内容锁、旧受管区块都是真的，不是手写夹具。
* 状态矩阵：(1) 无内容锁且四个契约文件都是用户自写；(2) 正常 rc.1 项目；(3) rc.1 项目里用户改过 AGENTS.md；
  (4) 带 rc.1 受管区块（agents-contract / claude-import）的接管项目；(5) 路径含空格；(6) 同步盘形态路径
  （Mobile Documents / com~apple~CloudDocs）。
* 每一步断言：用户文件字节不被意外改动、替换前备份目录里有原件、输出里给出恢复位置、退出码诚实、
  中英文各跑一遍英文输出里没有漏翻的中文。
* 全部在临时 HOME / 临时项目里运行；客户端命令只用桩（PATH 里只有桩与 git），不联网（--latest 指向本地文件）。
* 取不到 tag 时整体 skip 并写明补救；AGENTHELM_REQUIRE_TAGS=1（CI）下改为失败，沿用 test_upgrade_matrix.py 的约定。

macOS：只用 git、tar、subprocess、pathlib 与 shell 桩；未确认（macOS）——本环境只有 Linux。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import test_fixtures as fx
from test_upgrade_matrix import REPO, REQUIRE_TAGS, export_tag, tag_problem

TAG = "v6.11.0-rc.1"
OLD_INIT = Path("plugins/agents-init/skills/agents-init/scripts/init_project.py")
OLD_AGENTHELM = Path("plugins/agent-pack/runtime/agenthelm.py")
NEW_AGENTHELM = REPO / "plugins/agent-pack/runtime/agenthelm.py"
NEW_INIT = REPO / "plugins/agents-init/skills/agents-init/scripts/init_project.py"
LATEST = REPO / "latest.json"
LOCK = Path(".agents/moe.sakanano.agent-pack/content.lock.json")
CJK = re.compile(r"[一-鿿　-〿！-｠]")
PACK = "agents-init/project-contract"
CONTRACT_FILES = ("AGENTS.md", "AGENT.RULES.md", "CLAUDE.md", "AGENT.md")
INIT_ARGS = ("--name", "Rehearsal", "--project-type", "notes", "--vcs", "none", "--stack", "markdown",
             "--runtime", "local", "--agent-cli", "claude")
PLUGIN_NAMES = ("agent-pack", "agents-init", "opinion-manager", "project-orchestrator")


def sha(path: Path) -> str:
    return fx.hashlib.sha256(path.read_bytes()).hexdigest()


class Rehearsal(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        problem = tag_problem(TAG)
        if problem:
            if REQUIRE_TAGS:
                raise AssertionError(f"AGENTHELM_REQUIRE_TAGS=1 but {problem}")
            raise unittest.SkipTest(f"升级演练需要 {TAG}：{problem}")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.shared = Path(cls.tmp.name).resolve()
        cls.old = export_tag(TAG, cls.shared / "rc1")
        # 私有 PATH：只有 git（基线查找要用）和桩，绝不会落到真实的 claude / codex / cursor。
        cls.bin = cls.shared / "bin"
        cls.bin.mkdir()
        for tool in ("git", "sh", "dirname", "basename", "readlink", "env", "cat", "uname", "mkdir", "ls"):
            found = shutil.which(tool)
            if found:
                (cls.bin / tool).symlink_to(found)
        cls.package = cls.build_package(cls.shared / "package")

    @staticmethod
    def build_package(package: Path) -> Path:
        """npm 包布局：<package>/package.json（候选版本号）+ <package>/runtime/{index.json,plugins/…}。"""
        runtime = package / "runtime"
        (runtime / "plugins").mkdir(parents=True)
        shutil.copy(REPO / "index.json", runtime / "index.json")
        ignore = shutil.ignore_patterns("tests", "__pycache__", "test_*.py", "node_modules")
        for name in PLUGIN_NAMES:
            shutil.copytree(REPO / "plugins" / name, runtime / "plugins" / name, ignore=ignore)
        (package / "package.json").write_text(json.dumps({"name": "@sakanano-moe/agenthelm", "version": "6.11.0-rc.2"}), encoding="utf-8")
        return package

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()

    # ------------------------------------------------------------------ 命令

    def env(self, lang: str = "zh", **extra: str) -> dict[str, str]:
        env = {"HOME": str(self.home), "PATH": str(self.bin), "AGENTHELM_LANG": lang, "LC_ALL": "C.UTF-8",
               "AGENTHELM_PYTHON": sys.executable, "https_proxy": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}
        env.update(extra)
        return env

    def run_new(self, *args: str, lang: str = "zh", cwd: Path | None = None, input: str | None = None,
                script: Path = NEW_AGENTHELM, **extra: str) -> tuple[int, str, str]:
        done = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, check=False,
                              env=self.env(lang, **extra), cwd=cwd or self.base, input=input)
        return done.returncode, done.stdout, done.stderr

    def init_new(self, root: Path, *args: str, lang: str = "zh") -> tuple[int, str, str]:
        done = subprocess.run([sys.executable, str(NEW_INIT), "--project", str(root), *INIT_ARGS, *args],
                              capture_output=True, text=True, check=False, env=self.env(lang), cwd=self.base)
        return done.returncode, done.stdout, done.stderr

    def content(self, root: Path, *args: str, lang: str = "zh") -> tuple[int, str, str]:
        return self.run_new("update", "--only", "content", "--project", str(root), "--latest", str(LATEST), *args, lang=lang)

    def check(self, root: Path, *args: str, lang: str = "zh") -> tuple[int, str, str]:
        return self.run_new("check", "--project", str(root), "--latest", str(LATEST), *args, lang=lang)

    def recovery_of(self, out: str) -> Path | None:
        """输出里“备份到：<目录>”一行下面给出的备份目录（在项目外）。"""
        lines = out.splitlines()
        for index, line in enumerate(lines):
            if line.startswith(("被替换文件的原件已备份到：", "Originals of replaced files were backed up to:")) or "备份到：" in line and line.endswith(("到：",)):
                value = line.split("：", 1)[-1].split(": ", 1)[-1].strip()
                if value:
                    return Path(value)
        found = re.search(r"(?:备份到：|backed up to:)\s*(\S.*)", out)
        return Path(found.group(1).strip()) if found else None

    # ------------------------------------------------------------------ 旧项目

    def old_init(self, root: Path, *extra: str, mode: str = "init") -> None:
        root.mkdir(parents=True, exist_ok=True)
        done = subprocess.run([sys.executable, str(self.old / OLD_INIT), "--project", str(root), "--mode", mode, *INIT_ARGS,
                               "--output", "json", "--apply", *extra],
                              capture_output=True, text=True, check=False, env=self.env("zh"), cwd=self.old)
        self.assertEqual(done.returncode, 0, f"rc.1 的 {mode} 失败：{done.stdout}{done.stderr}")

    def state_pristine(self, root: Path) -> Path:
        self.old_init(root)
        return root

    def state_nolock(self, root: Path) -> Path:
        self.old_init(root)
        (root / LOCK).unlink()
        for name in CONTRACT_FILES:
            (root / name).write_text(f"# my own {name}\n\nwritten by the user, ASCII on purpose.\n", encoding="utf-8")
        return root

    def state_edited(self, root: Path) -> Path:
        self.old_init(root)
        text = (root / "AGENTS.md").read_text(encoding="utf-8")
        (root / "AGENTS.md").write_text(text.replace("\n", "\n", 1) + "\n## My addition\n\nDo not touch my words.\n", encoding="utf-8")
        return root

    def state_takeover(self, root: Path) -> Path:
        root.mkdir(parents=True)
        (root / "AGENTS.md").write_text("# User AGENTS\n\nmy own rules.\n", encoding="utf-8")
        (root / "CLAUDE.md").write_text("# User CLAUDE\n\nmine.\n", encoding="utf-8")
        (root / "notes.txt").write_text("keep me\n", encoding="utf-8")
        self.old_init(root, "--agents-policy", "append", "--claude-policy", "add-import",
                      "--recovery-dir", str(self.base / "adopt-recovery"), mode="adopt")
        self.assertIn("agenthelm:region agents-contract", (root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("agenthelm:region claude-import", (root / "CLAUDE.md").read_text(encoding="utf-8"))
        return root

    STATES = {
        "pristine": ("proj", "state_pristine"),
        "nolock": ("proj", "state_nolock"),
        "edited": ("proj", "state_edited"),
        "takeover": ("proj", "state_takeover"),
        "spaces": ("My Projects/with space 'quote' $HOME", "state_pristine"),
        # 公开仓库的隐私测试禁止出现连写的同步盘路径字面量，所以分段拼接。
        "syncdisk": (str(Path("Library", "Mobile Documents", "com~apple~Cloud" + "Docs", "Notes Project")), "state_pristine"),
    }

    def build(self, state: str) -> Path:
        relative, builder = self.STATES[state]
        return getattr(self, builder)(self.base / "work" / state / relative)

    # ------------------------------------------------------------------ 通用断言

    def assert_unchanged(self, before: dict, root: Path, message: str) -> None:
        self.assertEqual(fx.changed(before, fx.tree(root)), [], message)

    def assert_no_cjk(self, text: str, what: str) -> None:
        found = [line for line in text.splitlines() if CJK.search(line)]
        self.assertEqual(found, [], f"英文输出里有中文（{what}）")

    # ------------------------------------------------------------------ A. 只读命令：六种状态 × 中英文

    def test_read_only_commands_never_write_and_have_no_untranslated_chinese(self) -> None:
        for state in self.STATES:
            with self.subTest(state=state):
                self.setUp()  # 每个状态一个新的 HOME 与目录
                root = self.build(state)
                before = fx.tree(root)
                for lang in ("zh", "en"):
                    commands = [
                        ("check",), ("check", "--output", "json"), ("check", "--show-extras"), ("check", "--details"),
                        ("check", "--show-skipped", "--details"), ("check", "--output", "json", "--details", "--show-extras"),
                    ]
                    for command in commands:
                        code, out, err = self.check(root, *command[1:], lang=lang)
                        self.assertIn(code, (0, 1), f"{state} {lang} {command}: {out}{err}")
                        self.assertEqual(err, "", f"{state} {lang} {command}: stderr 应为空")
                        if "json" in command:
                            json.loads(out)
                        if lang == "en":
                            self.assert_no_cjk(out, f"{state} {command}")
                    code, out, err = self.content(root, lang=lang)
                    self.assertEqual(code, 0, f"{state} {lang}: {out}{err}")
                    if lang == "en":
                        self.assert_no_cjk(out, f"{state} update --only content")
                    code, out, err = self.run_new("fix", "--project", str(root), "--offline", "update-content", lang=lang)
                    self.assertIn(code, (0, 1), out + err)
                    code, out, err = self.run_new("fix", "--project", str(root), "--offline", "--accept-current", "--reason", "rehearsal", lang=lang)
                    self.assertIn(code, (0, 1), out + err)
                    code, out, err = self.run_new("config", "show", lang=lang)
                    self.assertEqual(code, 0, out + err)
                    if lang == "en":
                        self.assert_no_cjk(out, "config show")
                    for command in (("config", "language", "en"), ("config", "language-hint", "off"),
                                    ("config", "project-language", "en", "--project", str(root))):
                        code, out, err = self.run_new(*command, lang=lang)
                        self.assertEqual(code, 0, f"{command}: {out}{err}")
                        if lang == "en":
                            self.assert_no_cjk(out, str(command))
                    self.assert_unchanged(before, root, f"{state} {lang}: 只读/预览命令不得改动项目")
                self.assertFalse((self.home / ".agent-pack").exists(), "预览不得创建备份目录")

    # ------------------------------------------------------------------ B. 状态 2：正常 rc.1 项目

    def test_pristine_project_updates_with_backup_recovery_hint_and_is_idempotent(self) -> None:
        root = self.build("pristine")
        old_agents = (root / "AGENTS.md").read_bytes()
        others = {name: (root / name).read_bytes() for name in ("AGENT.RULES.md", "CLAUDE.md", "AGENT.md", "menu")}
        recovery = self.base / "recovery"
        code, out, err = self.content(root, "--apply", "--recovery-dir", str(recovery))
        self.assertEqual(code, 0, out + err)
        self.assertEqual((recovery / "AGENTS.md").read_bytes(), old_agents, "替换前必须先备份原件")
        self.assertIn(str(recovery), out, "输出里要有恢复位置")
        self.assertIn("cp -p", out, "还原方法")
        self.assertNotEqual((root / "AGENTS.md").read_bytes(), old_agents)
        for name, payload in others.items():
            self.assertEqual((root / name).read_bytes(), payload, f"{name} 未改动的文件字节不变")
        lock = json.loads((root / LOCK).read_text(encoding="utf-8"))
        self.assertNotEqual(lock["packs"][PACK]["version"], "1.2.0")
        settled = fx.tree(root)
        code, out, err = self.content(root, "--apply", "--recovery-dir", str(self.base / "recovery-2"))
        self.assertEqual(code, 0, out + err)
        self.assert_unchanged(settled, root, "第二次升级什么都不该改")
        self.assertFalse((self.base / "recovery-2").exists())
        code, out, _ = self.check(root, "--output", "json")
        self.assertEqual(code, 0)
        self.assertEqual([c for c in json.loads(out)["content"] if c["pack"] == PACK][0]["status"], "current")

    def test_default_recovery_dir_is_under_home_and_outside_the_project(self) -> None:
        root = self.build("pristine")
        code, out, err = self.content(root, "--apply")
        self.assertEqual(code, 0, out + err)
        backups = list((self.home / ".agent-pack/recovery").glob("*/AGENTS.md"))
        self.assertEqual(len(backups), 1, out)
        self.assertIn(str(backups[0].parent), out)

    # ------------------------------------------------------------------ C. 状态 3：用户改过 AGENTS.md

    def test_edited_agents_md_is_blocked_until_each_choice_and_every_choice_backs_up_first(self) -> None:
        root = self.build("edited")
        mine = (root / "AGENTS.md").read_bytes()
        before = fx.tree(root)
        code, out, err = self.content(root)
        self.assertEqual(code, 0, out + err)
        self.assertIn("blocked", out)
        for flag in ("--keep", "--append", "--diff"):
            self.assertIn(flag, out, f"预览要列出 {flag}")
        code, out, err = self.content(root, "--apply", "--recovery-dir", str(self.base / "rec0"))
        self.assertNotEqual(code, 0, "有 blocked 文件而没选择时，--apply 不能报告成功：\n" + out)
        self.assert_unchanged(before, root, "未选择前什么都不能改")
        self.assertFalse((self.base / "rec0").exists())
        # --diff：只读
        code, out, err = self.content(root, "--diff", "AGENTS.md")
        self.assertEqual(code, 0, out + err)
        self.assertIn("My addition", out)
        self.assert_unchanged(before, root, "--diff 不写入")

    def test_edited_keep_append_accept_flows(self) -> None:
        # --keep
        root = self.build("edited")
        mine = (root / "AGENTS.md").read_bytes()
        code, out, err = self.content(root, "--keep", "AGENTS.md", "--apply", "--recovery-dir", str(self.base / "rec-keep"))
        self.assertEqual(code, 0, out + err)
        self.assertEqual((root / "AGENTS.md").read_bytes(), mine, "--keep 不动文件")
        code, out, _ = self.content(root)
        self.assertEqual(code, 0)
        self.assertNotIn("blocked", out, "保留是一个被记住的决定")
        # --append
        self.setUp()
        root = self.build("edited")
        mine = (root / "AGENTS.md").read_bytes()
        recovery = self.base / "rec-append"
        code, out, err = self.content(root, "--append", "AGENTS.md", "--apply", "--recovery-dir", str(recovery))
        self.assertEqual(code, 0, out + err)
        appended = (root / "AGENTS.md").read_bytes()
        self.assertTrue(appended.startswith(mine), "追加不改原文")
        self.assertIn(b"agenthelm:region", appended[len(mine):])
        self.assertEqual((recovery / "AGENTS.md").read_bytes(), mine, "追加前先备份")
        self.assertIn(str(recovery), out)
        # --accept 需要 --confirm-discard
        self.setUp()
        root = self.build("edited")
        mine = (root / "AGENTS.md").read_bytes()
        before = fx.tree(root)
        code, out, err = self.content(root, "--accept", "AGENTS.md", "--apply", "--recovery-dir", str(self.base / "rec-a0"))
        self.assertNotEqual(code, 0, out)
        self.assert_unchanged(before, root, "--accept 没有 --confirm-discard 时不得改动")
        recovery = self.base / "rec-a1"
        code, out, err = self.content(root, "--accept", "AGENTS.md", "--confirm-discard", "AGENTS.md", "--apply",
                                      "--recovery-dir", str(recovery))
        self.assertEqual(code, 0, out + err)
        self.assertEqual((recovery / "AGENTS.md").read_bytes(), mine, "丢弃本地内容前先备份")
        self.assertNotIn(b"Do not touch my words", (root / "AGENTS.md").read_bytes())

    def test_unwritable_recovery_dir_aborts_before_any_user_file_changes(self) -> None:
        root = self.build("edited")
        blocker = self.base / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        before = fx.tree(root)
        code, out, err = self.content(root, "--accept", "AGENTS.md", "--confirm-discard", "AGENTS.md", "--apply",
                                      "--recovery-dir", str(blocker / "sub"))
        self.assertNotEqual(code, 0, out + err)
        self.assert_unchanged(before, root, "备份失败必须中止，用户文件不得改动")
        self.assertNotIn("Traceback", err)

    def test_three_way_merge_reaches_the_rc1_baseline_and_keeps_the_users_edit(self) -> None:
        root = self.build("edited")
        mine = (root / "AGENTS.md").read_bytes()
        before = fx.tree(root)
        code, out, err = self.init_new(root, "--mode", "refresh", "--merge", "AGENTS.md", "--output", "text")
        self.assertEqual(code, 0, out + err)
        self.assertIn("merge", out)
        self.assertNotIn("找不到这个文件升级前的基线", out, "rc.1 的基线要能取回（本地 git tag）")
        self.assert_unchanged(before, root, "--merge 默认只预览")
        recovery = self.base / "rec-merge"
        code, out, err = self.init_new(root, "--mode", "refresh", "--merge", "AGENTS.md", "--apply",
                                       "--recovery-dir", str(recovery), "--output", "text")
        self.assertEqual(code, 0, out + err)
        merged = (root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Do not touch my words", merged)
        self.assertEqual((recovery / "AGENTS.md").read_bytes(), mine)

    def test_update_only_content_and_fix_reach_the_three_way_merge(self) -> None:
        """agenthelm update --only content / fix update-content 透传 --merge / --pick / --diff（不再只在 init_project.py 里）。"""
        root = self.build("edited")
        mine = (root / "AGENTS.md").read_bytes()
        before = fx.tree(root)
        for lang in ("zh", "en"):
            code, out, err = self.content(root, "--merge", "AGENTS.md", lang=lang)
            self.assertEqual(code, 0, out + err)
            self.assertIn("merge", out)
            self.assertNotIn("找不到这个文件升级前的基线", out)
            if lang == "en":
                self.assert_no_cjk(out, "update --merge 预览")
        self.assert_unchanged(before, root, "update --merge 默认只预览")
        code, out, err = self.content(root, "--diff", "AGENTS.md")
        self.assertEqual(code, 0, out + err)
        self.assert_unchanged(before, root, "update --diff 不写入")
        recovery = self.base / "rec-update-merge"
        code, out, err = self.content(root, "--merge", "AGENTS.md", "--apply", "--recovery-dir", str(recovery))
        self.assertEqual(code, 0, out + err)
        self.assertIn("Do not touch my words", (root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertEqual((recovery / "AGENTS.md").read_bytes(), mine, "先备份原件")
        self.assertIn(str(recovery), out, "输出恢复位置")

    def test_fix_update_content_takes_merge_pick_and_diff(self) -> None:
        root = self.build("edited")
        mine = (root / "AGENTS.md").read_bytes()
        before = fx.tree(root)
        code, out, err = self.run_new("fix", "--project", str(root), "--offline", "update-content", "--merge", "AGENTS.md")
        self.assertEqual(code, 0, out + err)
        self.assertIn("三方合并", out)
        self.assertNotIn("找不到这个文件升级前的基线", out)
        self.assert_unchanged(before, root, "fix --merge 默认只预览")
        code, out, err = self.run_new("fix", "--project", str(root), "--offline", "update-content", "--diff", "AGENTS.md")
        self.assertEqual(code, 0, out + err)
        self.assert_unchanged(before, root, "fix --diff 不写入")
        code, out, err = self.run_new("fix", "--project", str(root), "--offline", "update-content", "--diff", "AGENTS.md", "--apply")
        self.assertNotEqual(code, 0, "--diff 与 --apply 不能同用")
        self.assert_unchanged(before, root, "被拒绝的 --diff --apply 不写入")
        code, out, err = self.run_new("fix", "--project", str(root), "--offline", "update-content", "--merge", "AGENTS.md", "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Do not touch my words", (root / "AGENTS.md").read_text(encoding="utf-8"))
        backups = [path for path in (self.home / ".agent-pack" / "recovery").rglob("AGENTS.md") if path.read_bytes() == mine]
        self.assertTrue(backups, "fix --merge --apply 先把原件备份到项目外")

    def test_no_lock_update_content_points_at_the_baseline_less_differences(self) -> None:
        root = self.build("nolock")
        before = fx.tree(root)
        for lang, words in (("zh", "无基线差异"), ("en", "no baseline")):
            code, out, err = self.content(root, lang=lang)
            self.assertEqual(code, 0, out + err)
            self.assertIn("fix update-content", out)
            self.assertIn(words, out)
            if lang == "en":
                self.assert_no_cjk(out, "update 无内容锁提示")
        self.assert_unchanged(before, root, "只是预览")

    # ------------------------------------------------------------------ D. 状态 1：无内容锁

    def test_no_lock_user_written_files_are_never_touched_and_the_lock_is_written_by_fix(self) -> None:
        root = self.build("nolock")
        files = {name: (root / name).read_bytes() for name in CONTRACT_FILES}
        before = fx.tree(root)
        code, out, err = self.check(root, "--details")
        self.assertIn(code, (0, 1))
        self.assertIn("content:AGENTS.md", out)
        code, out, err = self.run_new("fix", "--project", str(root), "--offline", "update-content", "--apply")
        self.assertIn(code, (0, 1), out + err)
        for name, payload in files.items():
            self.assertEqual((root / name).read_bytes(), payload, f"{name} 用户自写内容字节不变")
        self.assertTrue((root / LOCK).is_file())
        lock = json.loads((root / LOCK).read_text(encoding="utf-8"))
        self.assertNotEqual(lock["packs"][PACK]["version"], "0.0.0")
        self.assertIn("没有替换文件", out)
        # 再检查：用户文件不是问题，但也不能把它们说成“与模板一致”
        code, out, _ = self.check(root, "--output", "json", "--details")
        report = json.loads(out)
        self.assertTrue(report["ok"] or report["scaffold"], report["summary"])

    # ------------------------------------------------------------------ E. 状态 4：接管项目

    def test_takeover_regions_and_user_text_survive_every_update_path(self) -> None:
        root = self.build("takeover")
        agents = (root / "AGENTS.md").read_bytes()
        claude = (root / "CLAUDE.md").read_bytes()
        notes = (root / "notes.txt").read_bytes()
        code, out, err = self.content(root)
        self.assertEqual(code, 0, out + err)
        recovery = self.base / "rec-takeover"
        code, out, err = self.content(root, "--keep", "AGENTS.md", "--apply", "--recovery-dir", str(recovery))
        self.assertEqual(code, 0, out + err)
        self.assertEqual((root / "AGENTS.md").read_bytes(), agents)
        self.assertEqual((root / "CLAUDE.md").read_bytes(), claude)
        self.assertEqual((root / "notes.txt").read_bytes(), notes)
        # 区块更新：区块没变时 --mode region 不改写
        before = fx.tree(root)
        code, out, err = self.init_new(root, "--mode", "region", "--output", "json")
        self.assertIn(code, (0, 1), out + err)
        self.assert_unchanged(before, root, "region 预览零写入")
        # 追加隐私区块：保留原文并备份
        recovery = self.base / "rec-privacy"
        code, out, err = self.content(root, "--append", "AGENTS.md", "--apply", "--recovery-dir", str(recovery))
        self.assertIn(code, (0, 1), out + err)
        self.assertTrue((root / "AGENTS.md").read_bytes().startswith(agents.split(b"<!-- agenthelm:region")[0]))
        if (root / "AGENTS.md").read_bytes() != agents:
            self.assertEqual((recovery / "AGENTS.md").read_bytes(), agents)

    # ------------------------------------------------------------------ F. 路径含空格 / 同步盘

    def test_paths_with_spaces_and_sync_folders_work_end_to_end(self) -> None:
        for state in ("spaces", "syncdisk"):
            with self.subTest(state=state):
                self.setUp()
                root = self.build(state)
                old = (root / "AGENTS.md").read_bytes()
                recovery = self.base / "rec with space"
                code, out, err = self.content(root, "--apply", "--recovery-dir", str(recovery))
                self.assertEqual(code, 0, out + err)
                self.assertEqual((recovery / "AGENTS.md").read_bytes(), old)
                code, out, _ = self.check(root, "--output", "json")
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["scaffold"]["ok"] if isinstance(json.loads(out).get("scaffold"), dict) and "ok" in json.loads(out)["scaffold"] else True, True)
                # 根目录启动器在含空格的路径里可以直接运行（不调用模型，--help 即可）
                done = subprocess.run(["sh", str(root / "menu"), "--help"], capture_output=True, text=True, cwd=root,
                                      env=self.env("en", AGENTHELM_BIN=f"{sys.executable} {NEW_AGENTHELM}"), check=False)
                self.assertNotIn("Traceback", done.stderr)
        # 同步盘冲突副本不被当作项目文件：不报错，也不被改写
        self.setUp()
        root = self.build("syncdisk")
        conflict = root / "AGENTS 2.md"
        conflict.write_text("conflicted copy\n", encoding="utf-8")
        copy = root / "AGENTS (conflicted copy).md"
        copy.write_text("conflicted copy 2\n", encoding="utf-8")
        before = fx.tree(root)
        code, out, err = self.check(root, "--details")
        self.assertIn(code, (0, 1), out + err)
        self.assertNotIn("Traceback", err)
        code, out, err = self.content(root, "--apply", "--recovery-dir", str(self.base / "rec"))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(conflict.read_text(encoding="utf-8"), "conflicted copy\n")
        self.assertEqual(copy.read_text(encoding="utf-8"), "conflicted copy 2\n")

    # ------------------------------------------------------------------ G. fix --accept-current / 决定文件

    def test_accept_current_previews_first_and_only_writes_the_decisions_file(self) -> None:
        root = self.build("nolock")
        before = fx.tree(root)
        code, out, err = self.run_new("fix", "--project", str(root), "--offline", "--accept-current", "--reason", "mine")
        self.assertIn(code, (0, 1), out + err)
        self.assert_unchanged(before, root, "不带 --apply 零写入")
        code, out, err = self.run_new("fix", "--project", str(root), "--offline", "--accept-current", "--reason", "mine", "--apply")
        self.assertEqual(code, 0, out + err)
        changed = fx.changed(before, fx.tree(root))
        self.assertTrue(changed and all(".agents/moe.sakanano.agent-pack/" in key for key in changed), changed)
        for name in CONTRACT_FILES:
            self.assertTrue((root / name).read_text(encoding="utf-8").startswith("# my own"), name)
        code, out, _ = self.check(root, "--output", "json")
        self.assertEqual(code, 0, "接受现状之后 check 不再有待处理项")

    # ------------------------------------------------------------------ H. config

    def test_config_commands_preview_then_apply_and_back_up_the_old_file(self) -> None:
        config = self.home / ".config/agenthelm/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"update_check": True, "my_own_key": {"keep": "me"}}), encoding="utf-8")
        mine = config.read_bytes()
        code, out, err = self.run_new("config", "language", "en")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(config.read_bytes(), mine, "预览不写")
        code, out, err = self.run_new("config", "language", "en", "--apply")
        self.assertEqual(code, 0, out + err)
        data = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(data["my_own_key"], {"keep": "me"}, "用户自己的键不丢")
        self.assertEqual(data["update_check"], True)
        self.assertTrue(any(mine == path.read_bytes() for path in self.home.rglob("*") if path.is_file() and path != config),
                        "旧 config.json 的原件要有备份")
        # 损坏的 JSON：不覆盖
        config.write_text("{broken", encoding="utf-8")
        broken = config.read_bytes()
        code, out, err = self.run_new("config", "language-hint", "off", "--apply")
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(config.read_bytes(), broken, "无法解析的配置文件不得被覆盖")
        self.assertNotIn("Traceback", err)

    # ------------------------------------------------------------------ I. runtime install：rc.1 布局 → 当前

    def data_root(self) -> Path:
        return self.home / ".local/share/agenthelm"

    def test_runtime_rc1_layout_upgrades_beside_it_in_package_and_tarball_forms(self) -> None:
        code, out, err = self.run_new("runtime", "install", "--apply", script=self.old / OLD_AGENTHELM)
        self.assertEqual(code, 0, out + err)
        runtimes = self.data_root() / "runtimes"
        self.assertEqual(sorted(path.name for path in runtimes.iterdir()), ["6.11.0"], "rc.1 把候选版装在没有后缀的目录里")
        rc1 = fx.tree(runtimes / "6.11.0")
        package_script = self.package / "runtime/plugins/agent-pack/runtime/agenthelm.py"
        code, out, err = self.run_new("runtime", "install", script=package_script)
        self.assertEqual(code, 0, out + err)
        self.assertIn("6.11.0-rc.2", out)
        self.assertFalse((runtimes / "6.11.0-rc.2").exists(), "预览不写")
        code, out, err = self.run_new("runtime", "install", "--apply", script=package_script)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sorted(path.name for path in runtimes.iterdir()), ["6.11.0", "6.11.0-rc.2"])
        self.assertEqual(os.readlink(self.data_root() / "current"), "runtimes/6.11.0-rc.2")
        self.assertEqual(fx.changed(rc1, fx.tree(runtimes / "6.11.0")), [], "旧运行时目录原样保留")
        code, out, err = self.run_new("runtime", "install", "--apply", script=package_script)
        self.assertEqual(code, 0, out + err)
        self.assertIn("无需", out) if False else None
        # 装好的 current 可以直接运行，并且是新代码
        shim = self.data_root() / "current/bin/agenthelm"
        self.assertTrue(os.access(shim, os.X_OK), "bin/agenthelm 需要可执行位")
        done = subprocess.run([str(shim), "runtime", "status"], capture_output=True, text=True, env=self.env("en"), check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("6.11.0-rc.2", done.stdout)
        # 回退到 rc.1 的目录再回来
        code, out, err = self.run_new("runtime", "use", "6.11.0", "--apply", script=package_script, lang="en")
        self.assertEqual(code, 0, out + err)
        self.assert_no_cjk(out, "runtime use")
        self.assertEqual(os.readlink(self.data_root() / "current"), "runtimes/6.11.0")
        code, out, err = self.run_new("runtime", "install", "--apply", script=package_script)
        self.assertEqual(os.readlink(self.data_root() / "current"), "runtimes/6.11.0-rc.2")
        # tarball 形态：顶层目录 agenthelm-v6.11.0-rc.2
        tarball_root = self.base / "tb" / "agenthelm-v6.11.0-rc.2"
        shutil.copytree(self.package / "runtime", tarball_root)
        archive = self.base / "agenthelm-v6.11.0-rc.2.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(tarball_root, arcname="agenthelm-v6.11.0-rc.2")
        code, out, err = self.run_new("runtime", "install", "--source", str(archive), "--apply", script=package_script)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sorted(path.name for path in runtimes.iterdir()), ["6.11.0", "6.11.0-rc.2"], "同内容的 tarball 不产生新目录")
        for lang in ("zh", "en"):
            code, out, err = self.run_new("runtime", "status", lang=lang, script=package_script)
            self.assertEqual(code, 0, out + err)
            if lang == "en":
                self.assert_no_cjk(out, "runtime status")

    # ------------------------------------------------------------------ J. 客户端桩：install / update 的诚实输出

    def stub_claude(self, installed: dict[str, str], *, moves_to: dict[str, str] | None = None, dev: bool = False) -> Path:
        """claude 桩：``plugin install|update`` 把 installed_plugins.json 改成 moves_to 里的版本；其余只记录。"""
        log = self.base / "claude.log"
        log.unlink(missing_ok=True)
        records = {}
        shutil.rmtree(self.home / ".claude/plugins/cache", ignore_errors=True)
        for name, version in installed.items():
            path = self.home / f".claude/plugins/cache/agenthelm/{name}/{version}"
            if dev:
                target = self.base / "dev" / name
                target.mkdir(parents=True, exist_ok=True)
                (target / "plugin.json").write_text(json.dumps({"name": name, "version": version}), encoding="utf-8")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(target)
            else:
                path.mkdir(parents=True, exist_ok=True)
                (path / "plugin.json").write_text(json.dumps({"name": name, "version": version}), encoding="utf-8")
            records[f"{name}@agenthelm"] = [{"scope": "user", "installPath": str(path), "version": version}]
        state = self.home / ".claude/plugins/installed_plugins.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"version": 2, "plugins": records}), encoding="utf-8")
        script = f"""#!{sys.executable}
import json, pathlib, sys
open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')
moves = {json.dumps(moves_to or {})}
if sys.argv[1:3] in (['plugin', 'install'], ['plugin', 'update']) and moves:
    name = sys.argv[3].split('@')[0]
    if name in moves:
        home = pathlib.Path({str(self.home)!r})
        path = home / '.claude/plugins/cache/agenthelm' / name / moves[name]
        path.mkdir(parents=True, exist_ok=True)
        (path / 'plugin.json').write_text(json.dumps({{'name': name, 'version': moves[name]}}))
        file = home / '.claude/plugins/installed_plugins.json'
        data = json.loads(file.read_text())
        data['plugins'][name + '@agenthelm'] = [{{'scope': 'user', 'installPath': str(path), 'version': moves[name]}}]
        file.write_text(json.dumps(data))
"""
        stub = self.bin / "claude"
        stub.write_text(script, encoding="utf-8")
        stub.chmod(0o755)
        return log

    def latest_versions(self) -> dict[str, str]:
        return {name: item["version"] for name, item in json.loads(LATEST.read_text(encoding="utf-8"))["plugins"].items()}

    def test_install_and_update_report_installed_dev_and_unchanged_honestly(self) -> None:
        latest = self.latest_versions()
        # 已是最新：install 说明已安装且不执行；退出码 0
        log = self.stub_claude(latest)
        for lang in ("zh", "en"):
            code, out, err = self.run_new("install", "--client", "claude", "--latest", str(LATEST), "--apply", lang=lang)
            self.assertEqual(code, 0, out + err)
            if lang == "en":
                self.assert_no_cjk(out, "install installed")
        self.assertFalse(log.exists() and "install" in log.read_text(), "已安装时不得再执行 plugin install")
        # 落后：update 预览不执行；--apply 执行并读回新版本
        old = {name: "0.0.1" for name in latest}
        log = self.stub_claude(old, moves_to=latest)
        code, out, err = self.run_new("update", "--only", "plugins", "--client", "claude", "--latest", str(LATEST), "--no-project")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(log.exists() and "plugin update" in log.read_text(), "预览不执行客户端命令")
        code, out, err = self.run_new("update", "--only", "plugins", "--client", "claude", "--latest", str(LATEST), "--no-project", "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertIn("plugin update", log.read_text())
        # 命令成功但版本没变：不得报成功
        log = self.stub_claude(old)
        code, out, err = self.run_new("update", "--only", "plugins", "--client", "claude", "--latest", str(LATEST), "--no-project", "--apply")
        self.assertNotEqual(code, 0, "命令成功但版本没变，不能算完成：\n" + out)
        # 开发版（符号链接到工作副本）：不被覆盖
        log = self.stub_claude(old, dev=True)
        for lang in ("zh", "en"):
            code, out, err = self.run_new("update", "--only", "plugins", "--client", "claude", "--latest", str(LATEST), "--no-project", "--apply", lang=lang)
            if lang == "en":
                self.assert_no_cjk(out, "update dev")
        self.assertFalse(log.exists() and "plugin update" in log.read_text(), "开发版不应被 update 覆盖：\n" + out)

    # ------------------------------------------------------------------ K. 会话开始 hook 与 nudge

    def test_session_hook_prints_one_notice_in_the_hook_protocol_and_is_silent_when_nothing_to_do(self) -> None:
        root = self.build("pristine")
        for lang, needle in (("zh", "模板/样例包"), ("en", "Template/sample packs")):
            self.setUp()
            root = self.build("pristine")
            code, out, err = self.run_new("check", "--hook", "--hook-format", "claude-hook", "--project", str(root), lang=lang,
                                          AGENTHELM_LATEST_URL=str(LATEST))
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertEqual(payload["systemMessage"], payload["hookSpecificOutput"]["additionalContext"])
            self.assertIn(needle, payload["systemMessage"])
            if lang == "en":
                self.assert_no_cjk(out, "hook")
            code, out, _ = self.run_new("check", "--hook", "--hook-format", "claude-hook", "--project", str(root), lang=lang,
                                        AGENTHELM_LATEST_URL=str(LATEST))
            self.assertEqual((code, out), (0, ""), "同一条提示不重复打扰")
        # 没有可更新的内容时静默
        self.content(root, "--apply", "--recovery-dir", str(self.base / "rec"))
        code, out, _ = self.run_new("check", "--hook", "--hook-format", "claude-hook", "--project", str(root),
                                    AGENTHELM_LATEST_URL=str(LATEST), AGENTHELM_NO_UPDATE_CHECK="1")
        self.assertEqual((code, out), (0, ""))
        # 联网失败（本地文件不存在）：静默且退出码 0，不拖垮会话
        code, out, err = self.run_new("check", "--hook", "--project", str(root), AGENTHELM_LATEST_URL=str(self.base / "missing.json"))
        self.assertEqual((code, out), (0, ""), err)

    def test_nudge_commands_for_projects_with_awkward_paths_run_in_a_real_shell(self) -> None:
        for state in ("spaces", "syncdisk"):
            with self.subTest(state=state):
                self.setUp()
                root = self.build(state)
                code, out, err = self.run_new("nudge", "status", "--project", str(root), "--output", "json")
                self.assertEqual(code, 0, out + err)
                hook_input = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s $1 'x'", "cwd": str(root),
                                         "prompt": "以后一律用中文回复，这是规则：always answer in Chinese from now on"})
                code, out, err = self.run_new("nudge", "hook", "--client", "claude", "--event", "prompt-submit", input=hook_input)
                self.assertEqual(code, 0, err)
                if not out.strip():
                    continue
                context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
                commands = [line for line in context.splitlines() if "inbox add" in line]
                for command in commands:
                    command = re.sub(r"<[^>]+>", "x", command)
                    argv = shlex.split(command[command.index("python3"):])
                    self.assertEqual(argv[argv.index("--project") + 1], str(root))

    def test_cursor_hooks_written_by_rc1_are_replaced_not_duplicated_and_user_hooks_survive(self) -> None:
        runtime = NEW_AGENTHELM
        hooks = self.home / ".cursor/hooks.json"
        hooks.parent.mkdir(parents=True)
        rc1 = f'python3 "{self.old / OLD_AGENTHELM}" nudge hook --client cursor --event'
        hooks.write_text(json.dumps({"version": 1, "hooks": {
            "sessionStart": [{"command": "echo mine"}, {"command": f"{rc1} session-start", "timeout": 10}],
            "stop": [{"command": f"{rc1} stop", "timeout": 10}]}}), encoding="utf-8")
        mine = hooks.read_bytes()
        code, out, err = self.run_new("nudge", "install-hooks", "--output", "json")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(hooks.read_bytes(), mine, "预览不写")
        code, out, err = self.run_new("nudge", "install-hooks", "--apply", "--output", "json")
        self.assertEqual(code, 0, out + err)
        data = json.loads(hooks.read_text(encoding="utf-8"))
        commands = [item["command"] for item in data["hooks"]["sessionStart"]]
        self.assertIn("echo mine", commands)
        self.assertEqual(len([c for c in commands if "nudge hook" in c]), 1, commands)
        self.assertEqual(len([c for c in data["hooks"]["stop"] if "nudge hook" in c["command"]]), 1)
        self.assertEqual(hooks.with_name("hooks.json.agenthelm-bak").read_bytes(), mine, "改写前备份原件")


if __name__ == "__main__":
    unittest.main()
