from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
import inbox  # noqa: E402
import inbox_text  # noqa: E402
import versioned  # noqa: E402

SCRIPT = RUNTIME / "opinion_manager.py"
START = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def cli(*arguments: str) -> tuple[int, dict]:
    completed = subprocess.run([sys.executable, str(SCRIPT), "inbox", *arguments], capture_output=True, text=True, check=False,
                               env={**os.environ, "AGENTHELM_LANG": "zh"})
    return completed.returncode, json.loads(completed.stdout)


class InboxBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.moment = START
        patcher = mock.patch.object(inbox, "now", lambda: self.moment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_action(self, action: str, *ids: str, **options):
        defaults = dict(project=self.root, title=None, text=None, scope=[], quote=None, client=None, session=None,
                        status=None, unpresented=False, to=None, confirmed=False, note=None, days=None,
                        max_chars=6000, output="json", lang="zh", apply=False, confirm=None)
        defaults.update(options)
        import argparse
        return inbox.run(argparse.Namespace(action=action, ids=list(ids), **defaults))

    def add(self, title: str = "先给结论", text: str = "回答先给结论再展开", **options):
        options.setdefault("scope", ["回答"])
        options.setdefault("quote", "以后都先说结论")
        return self.run_action("add", title=title, text=text, **options)



class InboxTests(InboxBase):
    def test_add_is_unconfirmed_pending_and_never_writes_stable_rules(self) -> None:
        result = self.add(session="s1", client="claude")
        self.assertEqual((result["status"], result["state"], result["id"]), ("added", "pending", "cand-20261001-001"))
        self.assertFalse(result["writes_stable_rules"])
        self.assertEqual([path.name for path in self.root.iterdir()], [".opinion"])
        stored = json.loads((self.root / ".opinion/inbox.json").read_text(encoding="utf-8"))
        candidate = stored["candidates"]["cand-20261001-001"]
        self.assertEqual((candidate["status"], candidate["quotes"], candidate["session"]), ("pending", ["以后都先说结论"], "s1"))

    def test_duplicate_text_is_merged_and_keeps_new_evidence(self) -> None:
        self.add()
        again = self.add(title="先给结论 ", text="回答先给结论  再展开", quote="再说一遍，先说结论")
        self.assertEqual((again["status"], again["id"]), ("duplicate", "cand-20261001-001"))
        (item,) = self.run_action("list")["value"]
        self.assertEqual(item["quotes"], ["以后都先说结论", "再说一遍，先说结论"])

    def test_identifiers_count_up_per_day(self) -> None:
        first, second = self.add(), self.add(title="用中文", text="使用中文回复")
        self.moment += timedelta(days=1)
        third = self.add(title="表格不超过三列", text="表格列数不超过三列")
        self.assertEqual([first["id"], second["id"], third["id"]],
                         ["cand-20261001-001", "cand-20261001-002", "cand-20261002-001"])

    def test_add_requires_complete_content_and_scope(self) -> None:
        for options in ({"title": "", "text": "x"}, {"title": "x", "text": ""}, {"title": "x", "text": "y", "scope": []},
                        {"title": "x" * 500, "text": "y"}, {"title": "x", "text": "y" * 5000}):
            with self.assertRaises(inbox.InboxError):
                self.run_action("add", **{"scope": ["回答"], **options})

    def test_decide_needs_explicit_confirmation_and_records_history(self) -> None:
        identity = self.add()["id"]
        with self.assertRaisesRegex(inbox.InboxError, "明确表态"):
            self.run_action("decide", identity, to="trial")
        self.moment += timedelta(days=2)
        result = self.run_action("decide", identity, to="trial", confirmed=True, note="先试一周")
        self.assertEqual((result["changed"], result["to"]), ([identity], "trial"))
        (item,) = self.run_action("list", status="trial")["value"]
        self.assertEqual(item["trial_since"], "2026-10-03T08:00:00Z")
        stored = json.loads((self.root / ".opinion/inbox.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["to"] for entry in stored["candidates"][identity]["history"]], ["pending", "trial"])

    def test_decide_is_all_or_nothing_for_a_batch(self) -> None:
        identity = self.add()["id"]
        with self.assertRaisesRegex(inbox.InboxError, "没有这个候选"):
            self.run_action("decide", identity, "cand-missing", to="rejected", confirmed=True)
        self.assertEqual(self.run_action("list", status="pending")["value"][0]["id"], identity)

    def test_formal_is_only_bookkeeping(self) -> None:
        identity = self.add()["id"]
        result = self.run_action("decide", identity, to="formal", confirmed=True)
        self.assertIn("尚未写入 OPINION.md", result["reminder"])
        self.assertIn("inbox adopt", result["next_step"])
        self.assertFalse(result["writes_stable_rules"])
        self.assertFalse((self.root / "OPINION.md").exists())

    def test_expire_returns_old_trials_to_pending_but_keeps_staleness(self) -> None:
        old, recent = self.add()["id"], self.add(title="用中文", text="使用中文回复")["id"]
        self.run_action("decide", old, to="trial", confirmed=True)
        self.moment += timedelta(days=20)
        self.run_action("decide", recent, to="trial", confirmed=True)
        self.moment += timedelta(days=11)
        result = self.run_action("expire", days=30)
        self.assertEqual(result["reverted"], [old])
        states = {item["id"]: item["status"] for item in self.run_action("list")["value"]}
        self.assertEqual(states, {old: "pending", recent: "trial"})
        summary = self.run_action("pending")
        self.assertEqual(summary["oldest_pending_days"], 31.0)
        self.assertEqual(self.run_action("expire", days=30)["status"], "unchanged")

    def test_pending_follows_the_provider_contract(self) -> None:
        mine = self.add(session="s1")["id"]
        self.add(title="用中文", text="使用中文回复", session="s2")
        rejected = self.add(title="别用表情", text="不要使用表情")["id"]
        self.run_action("decide", rejected, to="rejected", confirmed=True)
        self.moment += timedelta(days=9)
        result = self.run_action("pending", session="s1")
        self.assertEqual(result["source"], "opinion")
        self.assertEqual(result["counts"], {"pending": 2, "trial": 0, "formal": 0, "held": 0, "rejected": 1})
        self.assertEqual(result["session_unpresented"], [mine])
        self.assertEqual(result["oldest_pending_days"], 9.0)
        for item in result["items"]:
            self.assertEqual(set(item), {"id", "source", "kind", "title", "status", "created_at", "last_action_at",
                                          "age_days", "hint_command"})
        self.assertEqual(len(result["items"]), 2)

    def test_presented_candidates_leave_the_unpresented_list(self) -> None:
        identity = self.add(session="s1")["id"]
        self.assertEqual(self.run_action("list", unpresented=True, session="s1")["value"][0]["id"], identity)
        self.assertEqual(self.run_action("present", identity)["marked"], [identity])
        self.assertEqual(self.run_action("list", unpresented=True, session="s1")["value"], [])
        self.assertEqual(self.run_action("pending", session="s1")["session_unpresented"], [])

    def test_brief_injects_protocol_and_only_trial_rules(self) -> None:
        trial = self.add()["id"]
        self.add(title="用中文", text="使用中文回复")
        self.run_action("decide", trial, to="trial", confirmed=True)
        result = self.run_action("brief", session="s9")
        self.assertIn("--session s9", result["text"])
        self.assertIn("回答先给结论再展开", result["text"])
        self.assertNotIn("使用中文回复", result["text"])
        self.assertIn("待确认候选 1 条，尚未生效", result["text"])
        self.assertEqual((result["trial"], result["pending"], result["trial_shown"]), (1, 1, [trial]))

    def test_brief_respects_the_size_budget(self) -> None:
        baseline = len(self.run_action("brief")["text"])
        for number in range(6):
            identity = self.add(title=f"规则{number}", text="详" * 200)["id"]
            self.run_action("decide", identity, to="trial", confirmed=True)
        result = self.run_action("brief", max_chars=baseline + 700)
        self.assertLess(len(result["trial_shown"]), 6)
        self.assertIn("未展示", result["text"])

    def test_markdown_listing_is_numbered_for_the_user(self) -> None:
        self.add()
        text = self.run_action("list", output="markdown")
        self.assertTrue(text.startswith("1. **先给结论**"))
        self.assertIn("用户原话：以后都先说结论", text)

    def git(self, *arguments: str) -> None:
        subprocess.run(["git", "-C", str(self.root), *arguments], capture_output=True, check=True)

    def test_first_add_warns_about_the_users_words_without_deciding_anything(self) -> None:
        self.git("init", "-q")
        first = self.add()
        self.assertIn("可能含敏感信息", first["notice"])
        self.assertIn("由你决定", first["notice"])
        self.assertIn("本工具不会替你决定", first["notice"])
        self.assertIn("当前未被 Git 忽略", first["notice"])
        self.assertNotIn("notice", self.add(title="用中文", text="使用中文回复"))
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [".git", ".opinion"], "no .gitignore is created")

    def test_no_warning_when_the_user_already_ignores_the_inbox(self) -> None:
        self.git("init", "-q")
        (self.root / ".gitignore").write_text(".opinion/\n", encoding="utf-8")
        self.assertNotIn("notice", self.add())
        self.assertEqual(self.run_action("exposure")["git"], "ignored")
        self.assertFalse(self.run_action("exposure")["needs_notice"])

    def test_exposure_distinguishes_tracked_untracked_and_non_git_projects(self) -> None:
        self.assertEqual(self.run_action("exposure")["git"], "no-git")
        self.assertIn("同步盘", self.run_action("exposure")["message"])
        self.git("init", "-q")
        self.add()
        self.assertEqual(self.run_action("exposure")["git"], "not-ignored")
        self.git("add", "-f", ".opinion/inbox.json")
        report = self.run_action("exposure")
        self.assertEqual(report["git"], "tracked")
        self.assertIn("提交和推送会包含它", report["message"])

    def test_first_add_outside_git_still_warns(self) -> None:
        self.assertIn("同步盘", self.add()["notice"])

    def test_symlinked_store_is_refused(self) -> None:
        target = self.root / "elsewhere"
        target.mkdir()
        (self.root / ".opinion").symlink_to(target)
        with self.assertRaisesRegex(inbox.InboxError, "符号链接"):
            self.add()

    def test_command_line_round_trip(self) -> None:
        code, added = cli("add", "--project", str(self.root), "--title", "先给结论", "--text", "回答先给结论",
                          "--scope", "回答", "--quote", "以后都先说结论")
        self.assertEqual((code, added["status"]), (0, "added"))
        code, refused = cli("decide", added["id"], "--project", str(self.root), "--to", "trial")
        self.assertEqual((code, refused["ok"]), (2, False))
        code, done = cli("decide", added["id"], "--project", str(self.root), "--to", "trial", "--confirmed")
        self.assertEqual((code, done["to"]), (0, "trial"))
        code, brief = cli("brief", "--project", str(self.root))
        self.assertIn("opinion_manager.py inbox add", brief["text"])
        self.assertIn(added["id"], brief["text"])


class AdoptTests(InboxBase):
    """inbox adopt：预览一次、确认一次、写入并读回；只复用 versioned 的 profile / compose / verify。"""

    def tree(self) -> dict[str, str]:
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(self.root.rglob("*")) if path.is_file() and path.name != ".inbox.lock"}

    def statuses(self) -> dict[str, str]:
        return {key: item["status"] for key, item in inbox.load(self.root)["candidates"].items()}

    def two(self) -> tuple[str, str]:
        return (self.add("先给结论", "回答先给结论再展开")["id"], self.add("不写套话", "不写无信息量的客套话")["id"])

    def preview(self, *ids: str, **options) -> dict:
        return self.run_action("adopt", *ids, **options)

    def apply(self, *ids: str, confirm: str | None = None, **options) -> dict:
        return self.run_action("adopt", *ids, apply=True, confirm=confirm, **options)

    def test_preview_writes_nothing_byte_for_byte(self) -> None:
        ids = self.two()
        before = self.tree()
        result = self.preview(*ids)
        self.assertEqual(self.tree(), before)
        self.assertEqual((result["status"], result["writes_stable_rules"], result["blocked"]), ("preview", False, None))
        self.assertIn("回答先给结论再展开", result["content"])
        self.assertIn("不写无信息量的客套话", result["content"])
        self.assertRegex(result["confirmation_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn(result["confirmation_sha256"], result["next_step"])
        self.assertEqual(set(self.statuses().values()), {"pending"})
        self.assertFalse((self.root / "OPINION.md").exists())

    def test_apply_writes_opinion_lock_and_reads_back_then_marks_formal(self) -> None:
        ids = self.two()
        preview = self.preview(*ids)
        result = self.apply(*ids, confirm=preview["confirmation_sha256"])
        self.assertEqual((result["status"], result["writes_stable_rules"], result["changed"]), ("written", True, list(ids)))
        opinion = (self.root / "OPINION.md").read_text(encoding="utf-8")
        self.assertEqual(opinion, preview["content"])
        lock = json.loads((self.root / "opinion.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["opinion_sha256"], versioned.text_digest(opinion))
        self.assertEqual(result["verified"]["opinion_sha256"], lock["opinion_sha256"])
        self.assertEqual(versioned.verify_project(self.root)["status"], "verified")
        self.assertTrue(Path(preview["profile_path"]).is_file())
        self.assertEqual(set(self.statuses().values()), {"formal"})
        history = inbox.load(self.root)["candidates"][ids[0]]["history"][-1]
        self.assertEqual(history["to"], "formal")
        self.assertTrue(history["note"].startswith("adopted:"))
        self.assertEqual(self.tree().keys() & {"OPINION.md", "opinion.lock.json"}, {"OPINION.md", "opinion.lock.json"})

    def test_second_adoption_revises_the_profile_and_keeps_earlier_rules(self) -> None:
        first, second = self.two()
        self.apply(first, confirm=self.preview(first)["confirmation_sha256"])
        preview = self.preview(second)
        self.assertNotEqual(preview["profile"].rsplit("@", 1)[1], "1.0.0")
        self.apply(second, confirm=preview["confirmation_sha256"])
        opinion = (self.root / "OPINION.md").read_text(encoding="utf-8")
        self.assertIn("回答先给结论再展开", opinion)
        self.assertIn("不写无信息量的客套话", opinion)
        self.assertEqual(versioned.verify_project(self.root)["status"], "verified")

    def test_missing_or_already_formal_ids_fail_the_whole_call_with_a_clear_message(self) -> None:
        first, second = self.two()
        self.run_action("decide", second, to="formal", confirmed=True)
        before = self.tree()
        with self.assertRaisesRegex(inbox.InboxError, "cand-20261001-999"):
            self.preview(first, "cand-20261001-999")
        with self.assertRaisesRegex(inbox.InboxError, second):
            self.preview(first, second)
        with self.assertRaisesRegex(inbox.InboxError, second):
            self.apply(first, second, confirm="0" * 64)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.statuses()[first], "pending")

    def test_apply_needs_the_previewed_fingerprint(self) -> None:
        ids = self.two()
        before = self.tree()
        with self.assertRaisesRegex(inbox.InboxError, "--confirm"):
            self.apply(*ids)
        with self.assertRaisesRegex(inbox.InboxError, "preview"):
            self.apply(*ids, confirm="0" * 64, lang="en")
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / "OPINION.md").exists())

    def test_fingerprint_mismatch_after_the_preview_refuses_and_changes_nothing(self) -> None:
        (first,) = self.two()[:1]
        preview = self.preview(first)
        (self.root / "OPINION.md").write_text("# 我手写的规则\n", encoding="utf-8")
        before = self.tree()
        with self.assertRaises(inbox.InboxError):
            self.apply(first, confirm=preview["confirmation_sha256"])
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.statuses()[first], "pending")

    def test_existing_hand_written_opinion_is_blocked_not_overwritten(self) -> None:
        (first,) = self.two()[:1]
        (self.root / "OPINION.md").write_text("# 我手写的规则\n", encoding="utf-8")
        before = self.tree()
        preview = self.preview(first)
        self.assertTrue(preview["blocked"])
        with self.assertRaises(inbox.InboxError):
            self.apply(first, confirm=preview["confirmation_sha256"])
        self.assertEqual(self.tree(), before)

    def test_failure_after_writing_rolls_back_files_profile_and_status(self) -> None:
        first, second = self.two()
        self.apply(first, confirm=self.preview(first)["confirmation_sha256"])
        settled = self.tree()
        preview = self.preview(second)
        with mock.patch.object(versioned, "verify_project", side_effect=versioned.OpinionVersionError("boom")):
            with self.assertRaisesRegex(versioned.OpinionVersionError, "boom"):
                self.apply(second, confirm=preview["confirmation_sha256"])
        self.assertEqual(self.tree(), settled, "OPINION.md、锁、Profile 文件都回到失败前")
        self.assertEqual(self.statuses()[second], "pending")

    def inject(self, point: str):
        """在 adopt --apply 的三个真实位置注入失败：Profile 写入后 / compose 写入后 / 我们自己的读回 verify。"""
        real_compose, real_verify = versioned.compose_command, versioned.verify_project
        calls = {"verify": 0}

        def compose_fails_before_writing(args):
            if args.apply:
                raise versioned.OpinionVersionError("after-profile")
            return real_compose(args)

        def compose_fails_after_writing(args):
            result = real_compose(args)
            if args.apply:
                raise versioned.OpinionVersionError("after-compose")
            return result

        def second_verify_fails(project):
            calls["verify"] += 1
            if calls["verify"] >= 2:
                raise versioned.OpinionVersionError("after-verify")
            return real_verify(project)

        return {"profile": mock.patch.object(versioned, "compose_command", compose_fails_before_writing),
                "compose": mock.patch.object(versioned, "compose_command", compose_fails_after_writing),
                "verify": mock.patch.object(versioned, "verify_project", second_verify_fails)}[point]

    def test_failure_at_each_of_three_points_restores_everything(self) -> None:
        for settled in (False, True):
            for point, marker in (("profile", "after-profile"), ("compose", "after-compose"), ("verify", "after-verify")):
                with self.subTest(settled=settled, point=point):
                    self.tearDown_project()
                    first, second = self.two()
                    if settled:
                        self.apply(first, confirm=self.preview(first)["confirmation_sha256"])
                    target = first if not settled else second
                    before, states = self.tree(), self.statuses()
                    preview = self.preview(target)
                    with self.inject(point), self.assertRaisesRegex(versioned.OpinionVersionError, marker):
                        self.apply(target, confirm=preview["confirmation_sha256"])
                    self.assertEqual(self.tree(), before, "OPINION.md、锁、Profile 文件都回到失败前（逐字节）")
                    self.assertEqual(self.statuses(), states)
                    self.assertEqual(self.preview(target)["confirmation_sha256"], preview["confirmation_sha256"], "回滚后可以用同一指纹重试")

    def tearDown_project(self) -> None:
        import shutil
        for path in self.root.iterdir():
            shutil.rmtree(path) if path.is_dir() else path.unlink()

    def test_any_change_after_the_preview_rejects_the_confirmation_with_zero_writes(self) -> None:
        first, second = self.two()
        self.apply(first, confirm=self.preview(first)["confirmation_sha256"])

        def candidate_text() -> None:
            data = inbox.load(self.root)
            data["candidates"][second]["text"] = "改过的正文"
            inbox.save(self.root, data)

        def profile() -> None:
            lock = json.loads((self.root / "opinion.lock.json").read_text(encoding="utf-8"))
            changed = {**lock["profile"], "custom": lock["profile"]["custom"] + "\n\n- 另一条"}
            content = versioned.render(changed)
            (self.root / "OPINION.md").write_text(content, encoding="utf-8")
            (self.root / "opinion.lock.json").write_text(versioned.pretty(versioned.lock_value(changed, content)), encoding="utf-8")

        def opinion() -> None:
            with (self.root / "OPINION.md").open("a", encoding="utf-8") as handle:
                handle.write("\n手工追加\n")

        for name, change in (("candidate", candidate_text), ("profile", profile), ("opinion", opinion)):
            with self.subTest(change=name):
                self.tearDown_project()
                first, second = self.two()
                self.apply(first, confirm=self.preview(first)["confirmation_sha256"])
                preview = self.preview(second)
                change()
                before, states = self.tree(), self.statuses()
                with self.assertRaises(inbox.InboxError):
                    self.apply(second, confirm=preview["confirmation_sha256"])
                self.assertEqual(self.tree(), before)
                self.assertEqual(self.statuses(), states)

    def test_failure_on_a_fresh_project_leaves_no_opinion_lock_or_profile(self) -> None:
        ids = self.two()
        preview = self.preview(*ids)
        before = self.tree()
        with mock.patch.object(inbox, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply(*ids, confirm=preview["confirmation_sha256"])
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / ".opinion" / "profiles").exists())
        self.assertEqual(set(self.statuses().values()), {"pending"})

    def test_decide_formal_says_it_is_not_in_opinion_yet_and_names_adopt(self) -> None:
        (first,) = self.two()[:1]
        result = self.run_action("decide", first, to="formal", confirmed=True, lang="en")
        self.assertFalse(result["in_opinion"])
        self.assertFalse(result["writes_stable_rules"])
        self.assertIn("inbox adopt", result["next_step"])
        self.assertIn(first, result["next_step"])
        self.assertIn("not in OPINION.md", result["reminder"])
        self.assertIn("尚未写入", self.run_action("decide", first, to="trial", confirmed=True, lang="zh")["reminder"] + inbox_text.MESSAGES["formal_next"]["zh"])

    def test_english_cli_output_has_no_chinese_outside_the_user_content(self) -> None:
        arguments = ("--project", str(self.root), "--lang", "en")
        cli("add", *arguments, "--title", "Lead with the answer", "--text", "Answer first, then details", "--scope", "replies")
        code, preview = cli("adopt", "cand-" + START.strftime("%Y%m%d") + "-001", *arguments)
        self.assertEqual(code, 2, "the fake clock is not used by the subprocess; the id date differs")
        identity = next(iter(inbox.load(self.root)["candidates"]))
        code, preview = cli("adopt", identity, *arguments)
        self.assertEqual(code, 0)
        outside = {key: value for key, value in preview.items() if key != "content"}
        self.assertFalse(CatalogTests.CJK.search(json.dumps(outside, ensure_ascii=False)), outside)
        self.assertIn("inbox adopt", preview["next_step"])
        code, result = cli("adopt", identity, *arguments, "--apply", "--confirm", preview["confirmation_sha256"])
        self.assertEqual((code, result["status"]), (0, "written"))
        self.assertFalse(CatalogTests.CJK.search(json.dumps({k: v for k, v in result.items() if k != "verified"}, ensure_ascii=False)), result)
        code, error = cli("adopt", identity, *arguments)
        self.assertEqual(code, 2)
        self.assertIn("Already formal", error["error"])
        self.assertFalse(CatalogTests.CJK.search(error["error"]))


class LanguageTests(InboxBase):
    """同一组行为在英文下：提示、错误与协议换成英文，数据与命令保持不变。"""

    def run_action(self, action: str, *ids: str, **options):
        return super().run_action(action, *ids, **{"lang": "en", **options})

    def test_english_prompts_errors_and_protocol(self) -> None:
        identity = self.add()["id"]
        with self.assertRaisesRegex(inbox.InboxError, "explicit decision"):
            self.run_action("decide", identity, to="trial")
        with self.assertRaisesRegex(inbox.InboxError, "No such candidate"):
            self.run_action("decide", "cand-missing", to="trial", confirmed=True)
        with self.assertRaisesRegex(inbox.InboxError, "Title must not be empty"):
            self.run_action("add", title="", text="x", scope=["a"])
        self.assertIn("is not in OPINION.md yet", self.run_action("decide", identity, to="formal", confirmed=True)["reminder"])
        self.run_action("decide", identity, to="trial", confirmed=True)
        text = self.run_action("brief", session="s9")["text"]
        self.assertTrue(text.startswith("[Opinion protocol"))
        self.assertIn("--lang en", text)
        self.assertIn("--session s9", text)
        self.assertIn("Relay and confirm in the language the user is using", text)
        self.assertIn("Trial candidates", text)
        self.assertEqual(self.run_action("list", output="markdown").splitlines()[1], "   - Rule: 回答先给结论再展开")

    def test_english_notice_keeps_the_decision_with_the_user(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        notice = self.add()["notice"]
        self.assertIn("is your decision", notice)
        self.assertIn("never edits .gitignore", notice)
        self.assertIn("It is currently not ignored by Git", notice)
        self.assertNotRegex(notice, r"[\u4e00-\u9fff]")

    def test_english_is_the_fallback_when_nothing_names_a_language(self) -> None:
        self.add()
        with mock.patch.dict(os.environ, {"LANG": "C", "LC_ALL": "", "LC_MESSAGES": ""}):
            os.environ.pop("AGENTHELM_LANG", None)
            text = super().run_action("brief", lang=None)["text"]
            self.assertTrue(text.startswith("[Opinion protocol"))
            self.assertIn("--lang en", text)
        with mock.patch.dict(os.environ, {"LC_ALL": "zh_CN.UTF-8"}):
            os.environ.pop("AGENTHELM_LANG", None)
            self.assertTrue(super().run_action("brief", lang=None)["text"].startswith("[Opinion 协议"))

    def test_stored_history_is_language_neutral(self) -> None:
        identity = self.add()["id"]
        self.run_action("decide", identity, to="trial", confirmed=True)
        self.moment += timedelta(days=40)
        self.run_action("expire", days=30)
        notes = [entry["note"] for entry in json.loads((self.root / ".opinion/inbox.json").read_text(encoding="utf-8"))["candidates"][identity]["history"]]
        self.assertEqual(notes, ["captured", "", "trial-expired:30d"])


class CatalogTests(unittest.TestCase):
    CJK = __import__("re").compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")

    def test_both_languages_have_the_same_keys_and_placeholders(self) -> None:
        for key, versions in inbox_text.MESSAGES.items():
            self.assertEqual(set(versions), set(inbox_text.LANGUAGES), key)
            self.assertEqual(set(inbox_text.PLACEHOLDER.findall(versions["zh"])),
                             set(inbox_text.PLACEHOLDER.findall(versions["en"])), key)

    def test_english_text_has_no_chinese_and_chinese_text_is_chinese(self) -> None:
        for key, versions in inbox_text.MESSAGES.items():
            self.assertFalse(self.CJK.search(versions["en"]), f"{key} 的英文里有中文")
        for key in ("protocol", "exposure_notice", "err_confirm", "brief_trial_header"):
            self.assertTrue(self.CJK.search(inbox_text.MESSAGES[key]["zh"]), key)

    def test_language_resolution_order(self) -> None:
        resolve = inbox_text.resolve_language
        self.assertEqual(resolve("en", {"AGENTHELM_LANG": "zh", "LANG": "zh_CN.UTF-8"}), "en")
        self.assertEqual(resolve(None, {"AGENTHELM_LANG": "en", "LANG": "zh_CN.UTF-8"}), "en")
        self.assertEqual(resolve("auto", {"LC_ALL": "zh_TW.UTF-8", "LANG": "en_US.UTF-8"}), "zh")
        self.assertEqual(resolve(None, {"LC_ALL": "C", "LANG": "en_US.UTF-8"}), "en")
        self.assertEqual(resolve(None, {"LANG": "fr_FR.UTF-8"}), "en")
        self.assertEqual(resolve(None, {"LANG": "C.UTF-8"}), "en")
        self.assertEqual(resolve(None, {}), "en")
        self.assertEqual(resolve("", {"LANG": "POSIX"}), "en")
        self.assertEqual(resolve(None, {"LC_ALL": "C", "LANG": "zh_CN.UTF-8"}), "zh", "C is skipped, the next variable decides")


if __name__ == "__main__":
    unittest.main()
