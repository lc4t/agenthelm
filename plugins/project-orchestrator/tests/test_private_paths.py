from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import case  # noqa: E402
import private_paths  # noqa: E402
import project_cli  # noqa: E402
import sop  # noqa: E402

HAS_GIT = shutil.which("git") is not None


class PrivateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.profile()
        patcher = mock.patch.dict(os.environ, {sop.SOP_HOME_ENV: str(Path(self.temporary.name) / "user-sops")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def profile(self, forbidden: list | None = None, work: dict | None = None) -> None:
        path = self.root / private_paths.PROFILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": "1.0", "name": "t", "privacy": {"forbidden_default_reads": forbidden or [], "generated_outputs": []},
                                    "work": work or {"mode": "case-workspace", "case": {"root": "cases"}}}), encoding="utf-8")

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def init_git(self) -> None:
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.test")
        self.git("config", "user.name", "t")

    def run_sop(self, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            code = sop.main(["--project", str(self.root), "--output", "json", *args])
        return code, json.loads(out.getvalue())

    def run_case(self, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = case.main(["--project", str(self.root), "--output", "json", *args])
        return code, json.loads(out.getvalue())

    def cli(self, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            code = project_cli.main(["--project", str(self.root), "--offline", "--output", "json", *args])
        return code, json.loads(out.getvalue())

    def new_case(self, slug: str = "work") -> str:
        code, made = self.run_case("new", "--title", "一件事", "--slug", slug, "--goal", "g", "--next-action", "n")
        self.assertEqual(code, 0, made)
        return made["id"]

    def sensitivity(self, case_id: str) -> str:
        return next(self.root.glob(f"cases/{case_id}/CASE.md")).read_text(encoding="utf-8").split('sensitivity: "', 1)[1].split('"', 1)[0]

    def active_sop_using(self, text: str) -> None:
        """从 weekly-review 样例出发，在第 2 步的输入里加入 text，并走完确认与启用。"""
        self.assertEqual(self.run_sop("new", "--slug", "review", "--template", "weekly-review", "--apply")[0], 0)
        path = self.root / "sops" / "review" / "SOP.md"
        original = path.read_text(encoding="utf-8")
        edited = original.replace("上一步确定的范围", "上一步确定的范围" + text)
        if text:
            self.assertNotEqual(original, edited)
        path.write_text(edited, encoding="utf-8")
        self.run_sop("defaults", "--set", "on_error=ask", "--set", "on_missing_tool=ask", "--set", "artifact_keep=ask",
                     "--set", "resume=ask", "--set", "confirm_costly=ask", "--apply")
        self.run_sop("decide", "review", "--confirm", "trigger", "--confirm", "requires", "--confirm", "scope", "--apply")
        code, result = self.run_sop("activate", "review", "--apply")
        self.assertEqual(code, 0, result)


class DeclarationTests(PrivateCase):
    def test_the_convention_directory_counts_only_when_it_exists(self) -> None:
        self.assertEqual(private_paths.declared(self.root), [])
        (self.root / "private").mkdir()
        self.assertEqual(private_paths.declared(self.root), ["private"])

    def test_profile_entries_are_normalised_and_unsafe_ones_ignored(self) -> None:
        self.profile(["secret-notes/", "./journal", "../outside", "/abs", "~/x", "", 3, "secret-notes"])
        self.assertEqual(private_paths.declared(self.root), ["secret-notes", "journal"])

    def test_membership_uses_path_boundaries_and_accepts_absolute_paths(self) -> None:
        self.profile(["private/"])
        for value in ("private", "private/a.txt", "./private/a/b", str(self.root / "private" / "a.txt")):
            self.assertTrue(private_paths.is_private(self.root, value), value)
        for value in ("private-notes/a.txt", "docs/private/a.txt", "/elsewhere/private/a.txt", "../private/a"):
            self.assertFalse(private_paths.is_private(self.root, value), value)

    def test_mentions_finds_paths_used_as_paths_only(self) -> None:
        text = "输入：private/journal/；另见 ./private/x.md、`private/y`。private-notes/ 与 docs/private/ 不算，private 一词不算。"
        self.assertEqual(private_paths.mentions(text, ["private"]), ["private"])
        self.assertEqual(private_paths.mentions("private-notes/ 与 docs/private/ 和 private 这个词", ["private"]), [])


@unittest.skipUnless(HAS_GIT, "需要 git")
class GitBehaviourTests(PrivateCase):
    def test_a_private_directory_is_not_ignored_until_the_gitignore_exists(self) -> None:
        self.init_git()
        (self.root / "private").mkdir()
        (self.root / "private" / "notes.txt").write_text("秘密", encoding="utf-8")
        self.assertIs(private_paths.git_state(self.root, "private")["ignored"], False)
        code, preview = self.run_sop("private")
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "private" / ".gitignore").exists())
        code, applied = self.run_sop("private", "--apply")
        self.assertEqual((code, applied["applied"]), (0, True))
        self.assertEqual((self.root / "private" / ".gitignore").read_text(encoding="utf-8"), private_paths.IGNORE_TEXT)
        self.assertIs(private_paths.git_state(self.root, "private")["ignored"], True)
        self.git("add", "-A")
        listed = subprocess.run(["git", "-C", str(self.root), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
        self.assertIn("private/.gitignore", listed)  # 忽略规则本身随仓库走
        self.assertNotIn("private/notes.txt", listed)  # 内容不进 git

    def test_an_existing_gitignore_is_never_modified(self) -> None:
        self.init_git()
        (self.root / "private").mkdir()
        (self.root / "private" / ".gitignore").write_text("# 我自己的规则\n", encoding="utf-8")
        _, result = self.run_sop("private", "--apply")
        self.assertEqual(result["writes"], [])
        self.assertEqual((self.root / "private" / ".gitignore").read_text(encoding="utf-8"), "# 我自己的规则\n")

    def test_already_tracked_files_are_reported_but_never_untracked_for_you(self) -> None:
        self.init_git()
        (self.root / "private").mkdir()
        (self.root / "private" / "old.txt").write_text("x", encoding="utf-8")
        self.git("add", "-A")
        self.run_sop("private", "--apply")
        state = private_paths.git_state(self.root, "private")
        self.assertEqual(state["tracked"], ["private/old.txt"])
        self.assertTrue(subprocess.run(["git", "-C", str(self.root), "ls-files", "private/old.txt"], capture_output=True, text=True).stdout.strip())

    def test_outside_a_git_repository_nothing_is_claimed(self) -> None:
        (self.root / "private").mkdir()
        self.assertIsNone(private_paths.git_state(self.root, "private")["ignored"])

    def test_health_reports_unignored_tracked_and_clean_states(self) -> None:
        self.init_git()
        (self.root / "private").mkdir()
        (self.root / "private" / "a.txt").write_text("x", encoding="utf-8")
        _, health = self.cli("health")
        self.assertIn("private-ignore:private", {item["id"] for item in health["data"]["findings"]})
        self.run_sop("private", "--apply")
        _, health = self.cli("health")
        self.assertFalse([item for item in health["data"]["findings"] if item["area"] == "私有目录"])
        self.assertTrue(any("私有目录 private/" in line for line in health["data"]["ok"]))
        self.git("add", "-f", "private/a.txt")
        _, health = self.cli("health")
        self.assertIn("private-tracked:private", {item["id"] for item in health["data"]["findings"]})


class CaseMarkingTests(PrivateCase):
    def test_registering_private_files_marks_the_case_private_automatically(self) -> None:
        (self.root / "private").mkdir()
        case_id = self.new_case()
        self.assertEqual(self.sensitivity(case_id), "shared")
        code, plain = self.run_case("update", case_id, "--add-deliverable", "docs/plain.md")
        self.assertNotIn("sensitivity", plain["changed"])
        code, result = self.run_case("update", case_id, "--add-evidence", "private/diary.txt")
        self.assertEqual(code, 0, result)
        self.assertIn("sensitivity", result["changed"])
        self.assertEqual(self.sensitivity(case_id), "private")

    def test_paths_relative_to_the_case_directory_count_too(self) -> None:
        self.profile(["cases/CASE-X-private"])  # 声明在 Case 目录之外的路径也要认
        (self.root / "private").mkdir()
        case_id = self.new_case()
        self.profile(["private/"])
        code, result = self.run_case("update", case_id, "--add-evidence", "../../private/a.txt")
        self.assertIn("sensitivity", result["changed"])

    def test_explicit_flag_both_ways_and_new_with_flag(self) -> None:
        case_id = self.new_case()
        self.assertEqual(self.run_case("update", case_id, "--sensitivity", "private")[0], 0)
        self.assertEqual(self.sensitivity(case_id), "private")
        self.assertEqual(self.run_case("update", case_id, "--sensitivity", "shared")[0], 0)
        self.assertEqual(self.sensitivity(case_id), "shared")
        code, made = self.run_case("new", "--title", "t", "--slug", "p", "--goal", "g", "--next-action", "n", "--sensitivity", "private")
        self.assertEqual(self.sensitivity(made["id"]), "private")

    def test_a_shared_case_with_private_references_is_flagged_by_health(self) -> None:
        (self.root / "private").mkdir()
        case_id = self.new_case()
        self.run_case("update", case_id, "--add-evidence", "private/a.txt")
        self.run_case("update", case_id, "--sensitivity", "shared")
        _, health = self.cli("health")
        self.assertIn("private-cases", {item["id"] for item in health["data"]["findings"]})


class SopIntegrationTests(PrivateCase):
    def test_a_sop_that_uses_the_private_directory_runs_as_a_private_case(self) -> None:
        (self.root / "private").mkdir()
        self.active_sop_using("；私有笔记 private/journal/")
        code, preview = self.run_sop("run", "review")
        self.assertEqual(preview["private"], ["private"])
        self.assertIn("读取前须你逐次确认", "\n".join(sop.execute(["run", "review", "--project", str(self.root)])[2]))
        code, ran = self.run_sop("run", "review", "--apply")
        self.assertEqual(code, 0, ran)
        text = (self.root / ran["path"]).read_text(encoding="utf-8")
        self.assertIn('sensitivity: "private"', text)
        self.assertIn("- 私有目录：本次会用到 private；读取前须经用户确认", text)

    def test_a_sop_without_private_paths_stays_shared(self) -> None:
        (self.root / "private").mkdir()
        self.active_sop_using("")  # 不引用私有路径（上面的替换是原样替换）
        _, ran = self.run_sop("run", "review", "--apply")
        self.assertEqual(ran["private"], [])
        self.assertIn('sensitivity: "shared"', (self.root / ran["path"]).read_text(encoding="utf-8"))

    @unittest.skipUnless(HAS_GIT, "需要 git")
    def test_run_warns_when_the_private_directory_is_not_ignored(self) -> None:
        self.init_git()
        (self.root / "private").mkdir()
        self.active_sop_using("；private/journal/")
        _, preview = self.run_sop("run", "review")
        self.assertTrue(preview["private_warnings"])

    def test_confirm_read_records_the_confirmation_and_marks_the_case(self) -> None:
        (self.root / "private").mkdir()
        case_id = self.new_case()
        code, preview = self.run_sop("confirm-read", case_id, "private/journal/2026.md")
        self.assertEqual((code, preview["applied"]), (0, False))
        self.assertEqual(self.sensitivity(case_id), "shared")
        code, applied = self.run_sop("confirm-read", case_id, "private/journal/2026.md", "private/ideas/", "--note", "只读标题", "--apply")
        self.assertEqual(code, 0, applied)
        text = next(self.root.glob(f"cases/{case_id}/CASE.md")).read_text(encoding="utf-8")
        self.assertIn("用户确认读取私有路径：private/journal/2026.md、private/ideas/；只读标题", text)
        self.assertEqual(self.sensitivity(case_id), "private")
        self.assertEqual(self.run_case("validate")[0], 0)

    def test_confirm_read_refuses_paths_outside_the_private_directories(self) -> None:
        (self.root / "private").mkdir()
        case_id = self.new_case()
        code, result = self.run_sop("confirm-read", case_id, "docs/readme.md", "--apply")
        self.assertEqual(code, 2, result)
        self.assertIn("无需确认", result["error"])

    def test_private_command_without_directories_says_how_to_create_one(self) -> None:
        code, result = self.run_sop("private")
        self.assertEqual(result["dirs"], [])
        code, created = self.run_sop("private", "--create", "--apply")
        self.assertTrue((self.root / "private" / ".gitignore").is_file())

    def test_find_is_not_special_cased_by_the_private_directory(self) -> None:
        (self.root / "private").mkdir()
        (self.root / "private" / "diary.md").write_text("# diary\n", encoding="utf-8")
        _, found = self.cli("find", "diary")
        self.assertEqual(found["data"]["results"], [])  # private/ 不是资料根，本来就不会被扫描


if __name__ == "__main__":
    unittest.main()
