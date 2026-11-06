"""升级矩阵（issue #25 第 3 项）：用历史 tag 的发布内容生成旧版本项目，模拟用户修改，再用当前版本升级。

* 旧项目由 ``git archive <tag>`` 取出的**旧版 init_project.py** 生成（不是手写的夹具），所以旧版本真实的
  模板、内容锁和 Profile 形状都被覆盖。
* 取不到 tag 时整个测试 skip 并写明原因与补救（``git fetch --tags``；CI 的 ``fetch-depth: 0`` 会带上全部 tag）。
  设置 ``AGENTHELM_REQUIRE_TAGS=1``（tests.yml 里专门的步骤已设置）后，缺 tag 改为失败，避免 CI 静默通过。
* 断言的是“用户数据怎样被处置”：unchanged / local-only / clean / modified / missing / kept-by-decision，
  自有 Profile 字段，新增必需骨架路径被占用。

macOS：只用 git、tar、subprocess 与 pathlib；未确认（macOS）——本环境只有 Linux。
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import test_fixtures as fx

REPO = Path(__file__).resolve().parents[5]
# issue #25：v6.5.x – v6.8.0 与 v6.11.0-rc.1。更早的 tag 目录布局不同（插件在仓库根），不在升级支持范围内。
TAGS = ("v6.5.0", "v6.5.1", "v6.5.2", "v6.5.3", "v6.6.0", "v6.7.0", "v6.8.0", "v6.11.0-rc.1")
OLD_SCRIPT = Path("plugins/agents-init/skills/agents-init/scripts/init_project.py")
PROFILE = Path(".agents/moe.sakanano.agent-pack/project.json")
REQUIRE_TAGS = os.environ.get("AGENTHELM_REQUIRE_TAGS") == "1"


def git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, check=False)


def tag_problem(tag: str) -> str | None:
    if git("rev-parse", "--git-dir").returncode != 0:
        return f"{REPO} is not a git checkout, so release tag {tag} cannot be read"
    if git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}").returncode != 0:
        return (f"release tag {tag} is not in this clone; run `git fetch --tags origin` "
                "(in CI use actions/checkout with fetch-depth: 0, which also fetches tags)")
    return None


def export_tag(tag: str, destination: Path) -> Path:
    """The tag's tree, unpacked; returns the root of the old release."""
    archive = git("archive", "--format=tar", tag)
    if archive.returncode != 0:
        raise RuntimeError(f"git archive {tag} failed: {archive.stderr.decode(errors='replace')}")
    import io

    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as handle:
        handle.extractall(destination, filter="data")
    return destination


def per_tag(test):
    """对每个可用 tag 各生成一个旧项目并调用 test(self, tag, base, root)；每个 tag 是独立的 subTest。"""
    @functools.wraps(test)
    def wrapper(self):
        for tag in self.available:
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                test(self, tag, base, self.old_project(tag, base))
    return wrapper


class UpgradeMatrixTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        missing = {tag: tag_problem(tag) for tag in TAGS}
        missing = {tag: reason for tag, reason in missing.items() if reason}
        cls.available = [tag for tag in TAGS if tag not in missing]
        cls.missing = missing
        if missing and REQUIRE_TAGS:
            raise AssertionError("AGENTHELM_REQUIRE_TAGS=1 but release tags are missing: " + "; ".join(missing.values()))
        if not cls.available:
            raise unittest.SkipTest("no historical release tag is available. " + next(iter(missing.values())))
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.old_roots = {tag: export_tag(tag, Path(cls.tmp.name).resolve() / tag) for tag in cls.available}

    # -------------------------------------------------------------- 旧项目与命令

    def old_project(self, tag: str, base: Path) -> Path:
        home = base / "home"
        home.mkdir()
        root = base / "proj"
        root.mkdir()
        script = self.old_roots[tag] / OLD_SCRIPT
        done = subprocess.run(
            [sys.executable, str(script), "--project", str(root), *fx.BASE_ARGS, "--apply"],
            capture_output=True, text=True, check=False, cwd=self.old_roots[tag], env={**os.environ, "HOME": str(home)})
        self.assertEqual(done.returncode, 0, f"{tag} 的 init 失败：{done.stdout}{done.stderr}")
        return root

    def refresh(self, root: Path, base: Path, *extra: str) -> tuple[int, dict]:
        return fx.cli(root, "refresh", *extra, home=base / "home")

    @staticmethod
    def states(result: dict) -> dict[str, tuple[str, str]]:
        return {item["path"]: (item["state"], item["action"]) for item in result["files"]}


    def test_rc1_adoption_compares_only_regions_after_full_template_changes(self) -> None:
        tag = "v6.11.0-rc.1"
        if tag not in self.available:
            self.skipTest(self.missing.get(tag, "rc.1 unavailable"))
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root, home = base / "adopted", base / "home"
            root.mkdir()
            home.mkdir()
            user = "# Existing project\n\nRun my own build command.\n"
            (root / "AGENTS.md").write_text(user)
            done = subprocess.run([sys.executable, str(self.old_roots[tag] / OLD_SCRIPT), "--project", str(root),
                                   *fx.BASE_ARGS, "--mode", "adopt", "--agents-policy", "append",
                                   "--recovery-dir", str(base / "old-backup"), "--apply"],
                                  capture_output=True, text=True, check=False, env={**os.environ, "HOME": str(home)})
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            before = (root / "AGENTS.md").read_bytes()
            decision_path = root / fx.scaffold.DECISIONS_PATH
            decisions = decision_path.read_bytes()
            code, report = fx.cli(root, "check", home=home)
            self.assertNotIn("content:AGENTS.md", {f["id"] for f in report["findings"]})
            # rc.1's contract body itself is unchanged; make an intact older block and matching lock baseline
            # so that the same historical project also exercises a region update independently of user text.
            region = fx.init_project._regions()
            old_body = "Previous managed instructions.\n"
            agents_path = root / "AGENTS.md"
            agents_path.write_text(region.upsert(agents_path.read_text(), "agents-contract", old_body, force=True)[0])
            lock_path = root / fx.init_project.CONTENT_LOCK_PATH
            lock = json.loads(lock_path.read_text())
            lock["packs"]["agents-init/project-contract"]["regions"]["AGENTS.md"]["agents-contract"] = region.digest(old_body)
            lock_path.write_text(json.dumps(lock))
            before = agents_path.read_bytes()
            code, report = fx.cli(root, "check", home=home)
            self.assertIn("region-outdated", {f["kind"] for f in report["findings"]})
            code, preview = self.refresh(root, base)
            self.assertEqual(code, 0, preview)
            item = next(row for row in preview["files"] if row["path"] == "AGENTS.md")
            self.assertEqual((item["comparison"], item["action"]), ("regions", "keep"))
            self.assertNotIn("AGENTS.md", preview["blocked"])
            code, updated = fx.cli(root, "region", "--apply", home=home)
            self.assertEqual(code, 0, updated)
            self.assertTrue((root / "AGENTS.md").read_text().startswith(user))
            self.assertEqual(decision_path.read_bytes(), decisions)
            code, report = fx.cli(root, "check", home=home)
            self.assertNotIn("content:AGENTS.md", {f["id"] for f in report["findings"]})
            self.assertNotEqual((root / "AGENTS.md").read_bytes(), before)

    # -------------------------------------------------------------- 状态处置

    @per_tag
    def test_untouched_old_project_upgrades_cleanly_with_backup_and_is_idempotent(self, tag: str, base: Path, root: Path) -> None:
        before = fx.tree(root)
        code, preview = self.refresh(root, base)
        self.assertEqual(code, 0, preview)
        self.assertEqual(fx.changed(before, fx.tree(root)), [], "预演零写入")
        states = self.states(preview)
        for path, (state, action) in states.items():
            self.assertIn(state, {"unchanged", "clean"}, f"{tag} {path}")
            self.assertEqual(action, "replace" if state == "clean" else "keep")
        self.assertEqual(states["AGENT.RULES.md"], ("clean", "replace"), "隐私一行改为引用 Profile 字段，没改过的 RULES 直接更新")
        self.assertEqual(states["AGENTS.md"], ("clean", "replace"), "旧模板渲染出的、未被改动的文件要换成新模板")
        replaced = [path for path, (_s, action) in states.items() if action == "replace"]
        old_bytes = {path: (root / path).read_bytes() for path in replaced}
        recovery = base / "recovery"
        code, applied = self.refresh(root, base, "--apply", "--recovery-dir", str(recovery))
        self.assertEqual((code, applied["applied"]), (0, True), applied)
        for path, payload in old_bytes.items():
            self.assertEqual((recovery / path).read_bytes(), payload, f"{path} 替换前必须先备份原件")
        lock = json.loads((root / fx.init_project.CONTENT_LOCK_PATH).read_text(encoding="utf-8"))
        self.assertEqual(lock["packs"]["agents-init/project-contract"]["version"], applied["pack"].split("@")[1])
        settled = fx.tree(root)
        code, again = self.refresh(root, base, "--apply", "--recovery-dir", str(base / "recovery-2"))
        self.assertEqual(code, 0, again)
        self.assertEqual(fx.changed(settled, fx.tree(root)), [], "重复升级不得再改任何文件")
        self.assertFalse((base / "recovery-2").exists(), "没有替换就不应出现备份目录")

    @per_tag
    def test_local_edit_where_the_template_changed_is_modified_and_blocks_until_the_user_chooses(self, tag: str, base: Path, root: Path) -> None:
        (root / "AGENTS.md").write_text("# 我改过的 AGENTS.md\n\n只此一份。\n", encoding="utf-8")
        mine = fx.tree(root)
        code, preview = self.refresh(root, base)
        self.assertEqual(code, 1, preview)
        self.assertEqual(self.states(preview)["AGENTS.md"][0], "modified")
        self.assertEqual(preview["blocked"], ["AGENTS.md"])
        code, blocked = self.refresh(root, base, "--apply", "--recovery-dir", str(base / "recovery"))
        self.assertEqual((code, blocked["applied"]), (1, False), blocked)
        self.assertEqual(fx.changed(mine, fx.tree(root)), [], "未选择前，用户文件与内容锁都不得改动")
        self.assertFalse((base / "recovery").exists())

    @per_tag
    def test_edited_rules_md_blocks_on_the_privacy_line_and_keep_preserves_it(self, tag: str, base: Path, root: Path) -> None:
        (root / "AGENT.RULES.md").write_text("项目自己的规则\n", encoding="utf-8")
        code, preview = self.refresh(root, base)
        self.assertEqual(code, 1, preview)
        self.assertEqual(self.states(preview)["AGENT.RULES.md"][0], "modified")
        code, kept = self.refresh(root, base, "--apply", "--keep", "AGENT.RULES.md", "--recovery-dir", str(base / "recovery"))
        self.assertEqual((code, kept["applied"]), (0, True), kept)
        self.assertEqual((root / "AGENT.RULES.md").read_text(encoding="utf-8"), "项目自己的规则\n")

    @per_tag
    def test_a_deleted_file_is_missing_and_only_recreated_not_overwriting_anything(self, tag: str, base: Path, root: Path) -> None:
        (root / "docs/refs/README.md").unlink()
        code, preview = self.refresh(root, base)
        self.assertEqual(code, 0, preview)
        state, action = self.states(preview)["docs/refs/README.md"]
        self.assertEqual((state, action), ("missing", "skip"), "用户删掉的文件缺省不会被偷偷建回")
        code, applied = self.refresh(root, base, "--apply", "--recovery-dir", str(base / "recovery"))
        self.assertEqual(code, 0, applied)
        self.assertFalse((root / "docs/refs/README.md").exists(), "没有选择就不创建")
        code, created = self.refresh(root, base, "--apply", "--accept", "docs/refs/README.md", "--recovery-dir", str(base / "recovery"))
        self.assertEqual(code, 0, created)
        self.assertTrue((root / "docs/refs/README.md").is_file(), "明确选择后才按新模板建回")

    @per_tag
    def test_keep_is_remembered_as_a_decision_and_the_file_is_not_asked_about_again(self, tag: str, base: Path, root: Path) -> None:
        (root / "AGENTS.md").write_text("# 我改过的 AGENTS.md\n", encoding="utf-8")
        code, kept = self.refresh(root, base, "--apply", "--keep", "AGENTS.md")
        self.assertEqual((code, kept["applied"]), (0, True), kept)
        self.assertEqual((root / "AGENTS.md").read_text(encoding="utf-8"), "# 我改过的 AGENTS.md\n")
        code, preview = self.refresh(root, base)
        self.assertEqual(code, 0, preview)
        self.assertEqual(preview["blocked"], [])
        self.assertEqual(self.states(preview)["AGENTS.md"], ("kept-by-decision", "keep"))
        settled = fx.tree(root)
        code, again = self.refresh(root, base, "--apply", "--keep", "AGENTS.md")
        self.assertEqual(code, 0, again)
        # 决定文件里的 decided_at 每次会刷新（秒级时间戳），其余都必须不变。
        self.assertLessEqual(set(fx.changed(settled, fx.tree(root))), {"0:.agents/moe.sakanano.agent-pack/check-decisions.json"})
        decisions = json.loads((root / ".agents/moe.sakanano.agent-pack/check-decisions.json").read_text(encoding="utf-8"))
        self.assertEqual(decisions["decisions"]["content:AGENTS.md"]["decision"], "keep")

    # -------------------------------------------------------------- 自有 Profile 字段

    @per_tag
    def test_user_own_profile_fields_survive_refresh_and_profile_fixes(self, tag: str, base: Path, root: Path) -> None:
        path = root / PROFILE
        profile = json.loads(path.read_text(encoding="utf-8"))
        profile["team_wiki"] = {"url": "https://wiki.example.invalid/team", "owners": ["甲", "乙"]}
        profile.setdefault("runtime", {})["my_note"] = "自有字段"
        path.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        mine = path.read_bytes()
        code, applied = self.refresh(root, base, "--apply", "--keep", "AGENTS.md", "--recovery-dir", str(base / "recovery"))
        self.assertIn(code, (0, 1), applied)
        self.assertEqual(path.read_bytes(), mine, "refresh 不碰 Profile")
        code, report = fx.cli(root, "check", "--state-dir", str(base / "state"), home=base / "home")
        flagged = {item.get("id", "") for item in report["findings"]}
        self.assertFalse([i for i in flagged if "team_wiki" in i or "my_note" in i], "自有字段不得被当成问题")
        code, fixed = fx.cli(root, "profile", "--apply", "--recovery-dir", str(base / "profile-recovery"),
                             "--state-dir", str(base / "state"), home=base / "home")
        self.assertEqual(code, 0, fixed)
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(after["team_wiki"], profile["team_wiki"])
        self.assertEqual(after["runtime"]["my_note"], "自有字段")
        self.assertEqual(after["name"], profile["name"])

    # -------------------------------------------------------------- 新增必需骨架路径被占用

    @per_tag
    def test_a_new_required_skeleton_path_occupied_by_the_user_is_reported_not_silently_passed(self, tag: str, base: Path, root: Path) -> None:
        path = root / PROFILE
        profile = json.loads(path.read_text(encoding="utf-8"))
        profile["sop"] = {"root": "sops"}  # 新版本才有的 SOP 目录约定
        profile.setdefault("privacy", {})["forbidden_default_reads"] = ["private/"]
        path.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        for existing in ("sops", "private"):  # v6.11.0-rc.1 已经建过这两个目录；换成用户自己的东西
            if (root / existing).exists():
                shutil.rmtree(root / existing)
        (root / "sops").write_text("用户自己的文件，占着新骨架的目录名\n", encoding="utf-8")
        (root / "private").mkdir()
        (root / "private/mine.txt").write_text("私密\n", encoding="utf-8")
        mine = fx.tree(root)
        code, report = fx.cli(root, "check", "--state-dir", str(base / "state"), home=base / "home")
        self.assertEqual(code, 1, f"check 有待处理的发现时退出码为 1，且不得崩溃：{report}")
        self.assertTrue(report["required_ok"] and not report["ok"], report)
        ids = {item["id"] for item in report["findings"]}
        self.assertIn("skel:sops/.gitkeep", ids, "被占用的新骨架路径必须出现在发现里")
        self.assertIn("skel:private/.gitignore", ids)
        self.assertEqual(fx.changed(mine, fx.tree(root)), [], "check 只读")
        code, preview = fx.cli(root, "add-skeleton", "--target", "private/.gitignore", "--state-dir", str(base / "state"), home=base / "home")
        self.assertEqual(code, 0, preview)
        self.assertEqual((root / "private/mine.txt").read_text(encoding="utf-8"), "私密\n")
        code, refused = fx.cli(root, "add-skeleton", "--apply", "--target", "sops/.gitkeep", "--target", "private/.gitignore",
                               "--state-dir", str(base / "state"), home=base / "home")
        self.assertEqual((code, refused.get("code")), (2, "invalid-request"), refused)
        self.assertEqual(fx.changed(mine, fx.tree(root)), [], "被占用时整批拒绝，不留半成品")


class TagAvailabilityTests(unittest.TestCase):
    def test_missing_tags_are_named_with_the_remedy(self) -> None:
        # 这个用例始终运行：取不到 tag 时，让日志里能看到“跳过了什么、为什么”，而不是静默。
        missing = {tag: tag_problem(tag) for tag in TAGS if tag_problem(tag)}
        if missing:
            print("\n[upgrade-matrix] tags not available, matrix skipped for: " + ", ".join(missing), file=sys.stderr)
            self.assertTrue(all("git fetch --tags" in reason or "not a git checkout" in reason for reason in missing.values()))
        self.assertFalse(missing and REQUIRE_TAGS, "AGENTHELM_REQUIRE_TAGS=1 requires every release tag to be present")


if __name__ == "__main__":
    unittest.main()
