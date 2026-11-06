from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # 这些测试断言中文文案；英文见 tests/test_i18n*.py

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("init_project.py")
sys.path.insert(0, str(SCRIPT.parent))
import baseline  # noqa: E402
import init_project  # noqa: E402
import merge3  # noqa: E402

PACK_ID = "agents-init/project-contract"
KEY = "AGENTS.md"


class Merge3Tests(unittest.TestCase):
    def test_edits_in_different_places_merge_without_conflict(self) -> None:
        merged = merge3.merge_text("a\nb\nc\nd\ne\n", "a\nB\nc\nd\ne\n", "a\nb\nc\nd\nE\n")
        self.assertEqual((merged.render(), merged.conflicts, merged.auto_hunks), ("a\nB\nc\nd\nE\n", [], 0))

    def test_both_sides_making_the_same_change_is_not_a_conflict(self) -> None:
        merged = merge3.merge_text("a\nb\n", "a\nX\n", "a\nX\n")
        self.assertEqual((merged.render(), merged.conflicts, merged.auto_hunks), ("a\nX\n", [], 1))

    def test_a_shared_edit_is_a_numbered_conflict_that_keeps_both_sides_marked(self) -> None:
        merged = merge3.merge_text("a\nb\nc\nd\n", "a\nB1\nc\nD1\n", "a\nB2\nc\nD2\n")
        self.assertEqual([item.number for item in merged.conflicts], [1, 2])
        marked = merged.render()
        for line in ("B1", "B2", "b", "<<<<<<< mine", "||||||| base", "=======", ">>>>>>> theirs"):
            self.assertIn(line + "\n", marked)
        self.assertEqual(merged.render({1: "mine", 2: "theirs"}), "a\nB1\nc\nD2\n")
        self.assertEqual(merged.render({1: "both", 2: "mine"}), "a\nB1\nB2\nc\nD1\n")
        self.assertEqual([item.number for item in merged.unresolved({1: "mine"})], [2])

    def test_a_missing_final_newline_is_not_a_change(self) -> None:
        self.assertEqual(merge3.merge_text("a\nb", "a\nb", "a\nB").render(), "a\nB\n")


class HistoricalTemplateTests(unittest.TestCase):
    """rc.1 及更早的历史模板没有 rc.2 才加入的隐私区块：渲染旧基线时不能因此被当作“模板无效”。"""

    def old_template(self) -> str:
        source = init_project.TEMPLATE_PATH.read_text(encoding="utf-8")
        for name in init_project.PRIVACY_TEMPLATES:
            source = init_project.TEMPLATE_PATTERN.sub(
                lambda match, name=name: "" if match.group("name") == name else match.group(0), source)
        return source.replace("PRIVACY_SECTION", "")

    def test_a_template_without_the_privacy_blocks_is_invalid_for_the_current_release_but_usable_as_a_baseline(self) -> None:
        old = self.old_template()
        with self.assertRaises(init_project.InitError):
            init_project.parse_templates(old)
        templates = init_project.parse_templates(old, historical=True)
        self.assertEqual({templates[name] for name in init_project.PRIVACY_TEMPLATES}, {""})
        inputs = init_project.Inputs(Path("/p"), "X", "x", ("notes",), "none", ("markdown",), "local", ("claude",),
                                     "case-workspace", "menu", "zh", True)
        self.assertIn(Path("AGENTS.md"), init_project.files_for(inputs, templates, None))

    def test_find_baseline_accepts_such_a_pack_when_it_renders_the_recorded_digest(self) -> None:
        old = self.old_template()
        inputs = init_project.Inputs(Path("/p"), "X", "x", ("notes",), "none", ("markdown",), "local", ("claude",),
                                     "case-workspace", "menu", "zh", True)
        templates = init_project.parse_templates(old, historical=True)
        text = init_project.files_for(inputs, templates, None)[Path("AGENTS.md")]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        found = init_project.find_baseline(Path("/p"), Path("AGENTS.md"), digest, ["1.2.0"], None, inputs,
                                           [lambda _pack, _wanted: iter([pack_source(old, version="1.2.0")])], PACK_ID)
        self.assertIsNotNone(found)
        self.assertEqual(found["text"], text)


def pack_source(template: str, *, version: str = "1.3.0", kind: str = "git-tag", label: str = "git tag v6.8.1") -> baseline.PackSource:
    return baseline.PackSource(label, kind, version, template, None)


class MergeRefreshTests(unittest.TestCase):
    """Upgrade matrix: the user edited a shared file and the new template changed too."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "project"
        self.home = Path(self.directory.name) / "home"
        self.root.mkdir()
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        done = subprocess.run([sys.executable, str(SCRIPT), "--project", str(self.root), "--name", "Notes", "--project-type", "study",
                               "--vcs", "none", "--stack", "none", "--runtime", "local", "--agent-cli", "none", "--no-entry",
                               "--output", "json", "--apply"], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.old_template = init_project.TEMPLATE_PATH.read_text(encoding="utf-8")
        self.original = (self.root / KEY).read_text(encoding="utf-8")
        self.lines = self.original.splitlines(keepends=True)
        self.recovery = Path(self.directory.name) / "recovery"

    # ---- helpers
    def template_line(self) -> str:
        """A plain line of the AGENTS.md template (no placeholder) to change."""
        body = init_project.load_templates()[KEY]
        return next(line for line in body.splitlines() if line.strip() and "PROJECT_" not in line and "ENTRY_" not in line
                    and "PRIVACY" not in line and line in self.original and not line.startswith("#"))

    def refresh(self, new_agents: str | None, *extra: str, providers=None) -> tuple[int, dict]:
        """Run --mode refresh in-process against a newer template; ``providers`` are the baseline sources on offer."""
        templates = init_project.load_templates()
        if new_agents is not None:
            templates[KEY] = new_agents
        pack = {**init_project.contract_pack(), "version": "1.4.0"}
        providers = [] if providers is None else providers
        output = io.StringIO()
        with mock.patch.object(init_project, "load_templates", return_value=templates), \
                mock.patch.object(init_project, "contract_pack", return_value=pack), \
                mock.patch.object(init_project, "baseline_providers", return_value=providers), \
                mock.patch("sys.stdout", output):
            code = init_project.main(["--project", str(self.root), "--mode", "refresh", "--output", "json", *extra])
        return code, json.loads(output.getvalue())

    def old_provider(self, calls: list | None = None):
        def provider(pack_id, wanted):
            if calls is not None:
                calls.append((pack_id, list(wanted)))
            yield pack_source(self.old_template)
        return provider

    def item(self, result: dict, path: str = KEY) -> dict:
        return next(entry for entry in result["files"] if entry["path"] == path)

    def edit_user_file(self, old: str, new: str) -> str:
        text = (self.root / KEY).read_text(encoding="utf-8").replace(old, new)
        (self.root / KEY).write_text(text, encoding="utf-8")
        return text

    def new_template_with_suffix(self) -> str:
        return init_project.load_templates()[KEY] + "\n## 新章节\n新模板新增的内容\n"

    def lock_entry(self) -> dict:
        return json.loads((self.root / init_project.CONTENT_LOCK_PATH).read_text())["packs"][PACK_ID]

    # ---- merge succeeds
    def test_merge_preview_shows_a_diff_and_writes_nothing(self) -> None:
        mine = self.edit_user_file(self.template_line(), "我自己改过的一行")
        calls: list = []
        code, preview = self.refresh(self.new_template_with_suffix(), "--merge", KEY, providers=[self.old_provider(calls)])
        self.assertEqual(code, 0, preview)
        agents = self.item(preview)
        self.assertEqual((agents["state"], agents["action"]), ("modified", "merge"))
        self.assertNotIn("reason", agents)
        self.assertEqual(agents["merge"]["conflicts"], [])
        self.assertIn("+新模板新增的内容", agents["diff"])
        self.assertNotIn("-我自己改过的一行", agents["diff"])  # the user's edit stays
        self.assertIn("三方合并", "".join(agents["note"]))
        self.assertEqual((self.root / KEY).read_text(encoding="utf-8"), mine)
        self.assertFalse(self.recovery.exists())
        # the lock's version of the file (and the pack version) are what the baseline was looked up with
        self.assertEqual(calls, [(PACK_ID, [self.lock_entry()["version"]])])

    def test_merge_apply_backs_up_writes_the_merge_and_moves_the_baseline(self) -> None:
        mine = self.edit_user_file(self.template_line(), "我自己改过的一行")
        new_template = self.new_template_with_suffix()
        code, done = self.refresh(new_template, "--merge", KEY, "--apply", "--recovery-dir", str(self.recovery),
                                  providers=[self.old_provider()])
        self.assertEqual((code, done["applied"]), (0, True), done)
        merged = (self.root / KEY).read_text(encoding="utf-8")
        self.assertIn("我自己改过的一行", merged)
        self.assertIn("新模板新增的内容", merged)
        self.assertEqual((Path(done["recovery"]) / KEY).read_text(encoding="utf-8"), mine)
        self.assertEqual(done["recovery_files"], [KEY])
        self.assertIn("还原", "\n".join(init_project.recovery_lines(done)))
        entry = self.lock_entry()
        self.assertEqual((entry["version"], entry["file_versions"][KEY]), ("1.4.0", "1.4.0"))
        rendered = init_project.files_for(init_project.inputs_from_profile(self.root), {**init_project.load_templates(), KEY: new_template})
        self.assertEqual(entry["files"][KEY], hashlib.sha256(rendered[Path(KEY)].encode()).hexdigest())
        # the decision is recorded, so a repeat run asks nothing and changes nothing
        code, again = self.refresh(new_template, "--merge", KEY, "--apply", providers=[])
        self.assertEqual(code, 2, again)  # nothing left to merge: the file now only differs by the user's own edits
        code, plain = self.refresh(new_template, providers=[])
        self.assertEqual((code, self.item(plain)["action"]), (0, "keep"), plain)
        self.assertEqual((self.root / KEY).read_text(encoding="utf-8"), merged)

    # ---- merge with conflicts
    def conflicting(self) -> tuple[str, str]:
        line = self.template_line()
        self.edit_user_file(line, "MINE " + line)
        return line, init_project.load_templates()[KEY].replace(line, "THEIRS " + line)

    def test_conflicts_keep_both_sides_marked_and_block_until_each_block_is_chosen(self) -> None:
        line, new_template = self.conflicting()
        before = (self.root / KEY).read_text(encoding="utf-8")
        code, preview = self.refresh(new_template, "--merge", KEY, "--apply", "--recovery-dir", str(self.recovery),
                                     providers=[self.old_provider()])
        self.assertEqual(code, 1, preview)
        agents = self.item(preview)
        self.assertEqual((agents["action"], agents["reason"]), ("blocked", "merge-conflict"))
        block = agents["merge"]["conflicts"][0]
        self.assertEqual((block["id"], block["mine"].strip(), block["theirs"].strip(), block["base"].strip()),
                         (1, "MINE " + line, "THEIRS " + line, line))
        self.assertEqual(agents["merge"]["unresolved"], [1])
        for marker in ("<<<<<<< 你的版本", "||||||| 基线", ">>>>>>> 新模板"):
            self.assertIn(marker, agents["diff"])
        self.assertIn("--pick AGENTS.md#<序号>=mine|theirs|both", "".join(agents["note"]))
        self.assertFalse(preview["applied"])
        self.assertEqual((self.root / KEY).read_text(encoding="utf-8"), before)  # nothing written, nothing backed up
        self.assertFalse(self.recovery.exists())

    def test_each_conflict_block_is_resolved_with_pick(self) -> None:
        line, new_template = self.conflicting()
        for choice, expected in (("mine", ["MINE " + line]), ("theirs", ["THEIRS " + line]), ("both", ["MINE " + line, "THEIRS " + line])):
            with self.subTest(choice=choice):
                (self.root / KEY).write_text(self.original.replace(line, "MINE " + line), encoding="utf-8")
                code, preview = self.refresh(new_template, "--merge", KEY, "--pick", f"{KEY}#1={choice}", providers=[self.old_provider()])
                self.assertEqual((code, self.item(preview)["action"]), (0, "merge"), preview)
                code, done = self.refresh(new_template, "--merge", KEY, "--pick", f"{KEY}#1={choice}", "--apply",
                                          "--recovery-dir", str(self.recovery / choice), providers=[self.old_provider()])
                self.assertEqual(code, 0, done)
                text = (self.root / KEY).read_text(encoding="utf-8").splitlines()
                position = next(index for index, value in enumerate(text) if expected[0] in value)
                self.assertEqual(text[position:position + len(expected)], expected)
                self.assertNotIn("<<<<<<<", "\n".join(text))
                self.assertEqual(done["recovery_files"], [KEY])
                # reset the lock so the next subtest starts from the install-time state
                lock = json.loads((self.root / init_project.CONTENT_LOCK_PATH).read_text())
                lock["packs"][PACK_ID].update(version="1.3.0")
                lock["packs"][PACK_ID]["files"][KEY] = hashlib.sha256(self.original.encode()).hexdigest()
                lock["packs"][PACK_ID].pop("file_versions", None)
                (self.root / init_project.CONTENT_LOCK_PATH).write_text(json.dumps(lock))

    def test_pick_must_name_an_existing_block_of_a_merged_file(self) -> None:
        line, new_template = self.conflicting()
        code, refused = self.refresh(new_template, "--merge", KEY, "--pick", f"{KEY}#9=mine", providers=[self.old_provider()])
        self.assertEqual(code, 2)
        self.assertIn("冲突块不存在", refused["error"])
        code, refused = self.refresh(new_template, "--pick", f"{KEY}#1=mine", providers=[self.old_provider()])
        self.assertEqual(code, 2)
        self.assertIn("--merge", refused["error"])
        code, refused = self.refresh(new_template, "--merge", KEY, "--pick", f"{KEY}#x=mine", providers=[self.old_provider()])
        self.assertEqual(code, 2)

    # ---- fallback
    def test_no_baseline_source_falls_back_to_the_manual_choices_without_losing_anything(self) -> None:
        line, new_template = self.conflicting()
        before = (self.root / KEY).read_text(encoding="utf-8")
        code, blocked = self.refresh(new_template, "--merge", KEY, "--apply", "--recovery-dir", str(self.recovery), providers=[])
        self.assertEqual(code, 1, blocked)
        agents = self.item(blocked)
        self.assertEqual((agents["action"], agents["reason"]), ("blocked", "baseline-unavailable"))
        self.assertNotIn("merge", agents)
        self.assertIn("--keep", "".join(agents["note"]))
        self.assertIn("--accept", blocked["hint"])
        self.assertFalse(blocked["applied"])
        self.assertEqual((self.root / KEY).read_text(encoding="utf-8"), before)
        # --pick on such a file is not an error: the fallback is simply still on offer
        code, again = self.refresh(new_template, "--merge", KEY, "--pick", f"{KEY}#1=mine", providers=[])
        self.assertEqual((code, self.item(again)["reason"]), (1, "baseline-unavailable"))
        code, kept = self.refresh(new_template, "--keep", KEY, "--apply", providers=[])
        self.assertEqual(code, 0, kept)
        self.assertEqual((self.root / KEY).read_text(encoding="utf-8"), before)

    def test_old_template_render_failure_is_distinct_from_unavailable_sources(self) -> None:
        line, new_template = self.conflicting()
        old = subprocess.run(["git", "show", "v6.8.0:plugins/agents-init/skills/agents-init/AGENT.template.md"],
                             capture_output=True, text=True, check=True).stdout
        source = pack_source(old, version=self.lock_entry()["version"], label="git tag v6.8.0")
        for lang, expected in (("zh", "历史模板缺少当前渲染所需的区块"), ("en", "historical template lacks blocks")):
            with mock.patch.dict(os.environ, {"AGENTHELM_LANG": lang}):
                code, blocked = self.refresh(new_template, "--merge", KEY, providers=[lambda p, w: iter([source])])
            item = self.item(blocked)
            self.assertEqual((code, item["reason"]), (1, "baseline-template-incompatible"), blocked)
            notes = "".join(item["note"])
            self.assertIn(expected, notes)
            for choice in ("--keep", "--append", "--accept"):
                self.assertIn(choice, notes)
            self.assertNotIn("GitHub Release", notes)

    def test_rendered_template_that_misses_the_lock_digest_is_not_blamed_on_the_network(self) -> None:
        line, new_template = self.conflicting()
        source = pack_source(self.old_template.replace("机器可读权威源", "机器可读权威来源", 1), version=self.lock_entry()["version"])
        for lang, expected in (("zh", "与内容锁记录的摘要不一致"), ("en", "does not match the digest recorded in the content lock")):
            with mock.patch.dict(os.environ, {"AGENTHELM_LANG": lang}):
                code, blocked = self.refresh(new_template, "--merge", KEY, providers=[lambda p, w: iter([source])])
            item = self.item(blocked)
            self.assertEqual((code, item["reason"]), (1, "baseline-digest-mismatch"), blocked)
            notes = "".join(item["note"])
            self.assertIn(expected, notes)
            self.assertNotIn("联网" if lang == "zh" else "networking", notes)
            for choice in ("--keep", "--append", "--accept"):
                self.assertIn(choice, notes)

    def test_network_failure_is_reported_without_claiming_the_template_was_invalid(self) -> None:
        line, new_template = self.conflicting()
        def offline(pack_id, wanted):
            raise OSError("offline")
            yield
        for lang, expected in (("zh", "下载与校验未成功"), ("en", "download and verification did not succeed")):
            with mock.patch.dict(os.environ, {"AGENTHELM_LANG": lang}):
                code, blocked = self.refresh(new_template, "--merge", KEY, providers=[offline])
            item = self.item(blocked)
            self.assertEqual((code, item["reason"]), (1, "baseline-unavailable"))
            self.assertIn(expected, "".join(item["note"]))

    def test_a_source_that_does_not_render_the_recorded_baseline_is_not_trusted(self) -> None:
        line, new_template = self.conflicting()
        wrong = pack_source(self.old_template.replace(line, "some other text"), label="git tag v0.0.1")
        code, blocked = self.refresh(new_template, "--merge", KEY, providers=[lambda pack_id, wanted: iter([wrong])])
        self.assertEqual((code, self.item(blocked)["reason"]), (1, "baseline-digest-mismatch"))

    def test_a_failing_source_is_skipped_and_the_next_one_is_used(self) -> None:
        self.edit_user_file(self.template_line(), "我自己改过的一行")

        def broken(pack_id, wanted):
            raise OSError("offline")
            yield  # pragma: no cover

        empty_template = baseline.PackSource("cache", "cache", "1.3.0", "no templates here", None)
        code, preview = self.refresh(self.new_template_with_suffix(), "--merge", KEY,
                                     providers=[broken, lambda p, w: iter([empty_template]), self.old_provider()])
        self.assertEqual((code, self.item(preview)["action"]), (0, "merge"), preview)
        self.assertEqual(self.item(preview)["merge"]["source"], "git tag v6.8.1")

    def test_a_file_without_a_recorded_baseline_cannot_be_merged(self) -> None:
        self.edit_user_file(self.template_line(), "我自己改过的一行")
        lock = json.loads((self.root / init_project.CONTENT_LOCK_PATH).read_text())
        lock["packs"][PACK_ID].update(version="0.0.0", files={})
        (self.root / init_project.CONTENT_LOCK_PATH).write_text(json.dumps(lock))
        code, blocked = self.refresh(self.new_template_with_suffix(), "--merge", KEY, providers=[self.old_provider()])
        self.assertEqual((code, self.item(blocked)["reason"]), (1, "no-baseline"))
        self.assertIn("无法三方合并", "".join(self.item(blocked)["note"]))

    def test_merge_needs_a_locally_edited_file_and_excludes_other_choices(self) -> None:
        code, refused = self.refresh(self.new_template_with_suffix(), "--merge", KEY, providers=[self.old_provider()])
        self.assertEqual(code, 2)
        self.assertIn("没有需要合并的本地改动", refused["error"])
        self.edit_user_file(self.template_line(), "我自己改过的一行")
        code, refused = self.refresh(self.new_template_with_suffix(), "--merge", KEY, "--keep", KEY, providers=[])
        self.assertEqual(code, 2)
        self.assertIn("--merge", refused["error"])

    def test_default_run_never_looks_for_a_baseline_and_points_at_merge(self) -> None:
        self.edit_user_file(self.template_line(), "我自己改过的一行")
        calls: list = []
        code, blocked = self.refresh(self.new_template_with_suffix(), providers=[self.old_provider(calls)])
        self.assertEqual((code, calls), (1, []))
        agents = self.item(blocked)
        self.assertEqual((agents["action"], agents["reason"]), ("blocked", "edited"))
        self.assertIn("--merge AGENTS.md", "".join(agents["note"]))

    def test_text_output_lists_the_conflict_blocks(self) -> None:
        line, new_template = self.conflicting()
        templates = {**init_project.load_templates(), KEY: new_template}
        pack = {**init_project.contract_pack(), "version": "1.4.0"}
        output = io.StringIO()
        with mock.patch.object(init_project, "load_templates", return_value=templates), \
                mock.patch.object(init_project, "contract_pack", return_value=pack), \
                mock.patch.object(init_project, "baseline_providers", return_value=[self.old_provider()]), \
                mock.patch("sys.stdout", output):
            init_project.main(["--project", str(self.root), "--mode", "refresh", "--merge", KEY, "--output", "text"])
        text = output.getvalue()
        self.assertIn("冲突块 1：", text)
        self.assertIn("MINE " + line, text)
        self.assertIn("THEIRS " + line, text)

    def test_a_file_kept_across_an_upgrade_remembers_which_pack_version_its_baseline_came_from(self) -> None:
        # This fixture models a 1.3.0 -> 1.4.0 upgrade regardless of the current release's pack version.
        # The synthetic old provider below also labels these template bytes 1.3.0.
        lock_path = self.root / init_project.CONTENT_LOCK_PATH
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["packs"][PACK_ID]["version"] = "1.3.0"
        lock["packs"][PACK_ID].pop("file_versions", None)
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        self.edit_user_file(self.template_line(), "我自己改过的一行")
        code, kept = self.refresh(self.new_template_with_suffix(), "--keep", KEY, "--apply", providers=[])
        self.assertEqual(code, 0, kept)
        entry = self.lock_entry()
        self.assertEqual((entry["version"], entry["file_versions"][KEY]), ("1.4.0", "1.3.0"))
        calls: list = []
        newer = self.new_template_with_suffix() + "更后来的一行\n"
        code, preview = self.refresh(newer, "--merge", KEY, providers=[self.old_provider(calls)])
        self.assertEqual((code, self.item(preview)["action"]), (0, "merge"), preview)
        self.assertEqual(calls[0][1], ["1.3.0", "1.4.0"])


# ---------------------------------------------------------------- baseline sources

    # ---- rc.3 W2：A7 无基线文件、A6 预览口径、A8 隐私约定措辞
    def drop_baseline(self) -> None:
        """内容锁里没有 AGENTS.md 的基线（早期版本初始化的项目）。"""
        lock = json.loads((self.root / init_project.CONTENT_LOCK_PATH).read_text())
        lock["packs"][PACK_ID]["files"].pop(KEY)
        lock["packs"][PACK_ID].pop("file_versions", None)
        (self.root / init_project.CONTENT_LOCK_PATH).write_text(json.dumps(lock))

    def text_refresh(self, new_agents: str | None, *extra: str, lang: str = "zh", providers=None) -> str:
        templates = init_project.load_templates()
        if new_agents is not None:
            templates[KEY] = new_agents
        pack = {**init_project.contract_pack(), "version": "1.4.0"}
        output = io.StringIO()
        with mock.patch.object(init_project, "load_templates", return_value=templates), \
                mock.patch.object(init_project, "contract_pack", return_value=pack), \
                mock.patch.object(init_project, "baseline_providers", return_value=providers or []), \
                mock.patch.dict(os.environ, {"AGENTHELM_LANG": lang}), mock.patch("sys.stdout", output):
            init_project.main(["--project", str(self.root), "--mode", "refresh", "--output", "text", *extra])
        return output.getvalue()

    def test_a7_merge_without_a_baseline_gives_one_clear_reason_and_never_guesses(self) -> None:
        mine = self.edit_user_file(self.template_line(), "我自己改过的一行")
        self.drop_baseline()
        calls: list = []
        code, blocked = self.refresh(self.new_template_with_suffix(), "--merge", KEY, "--apply", "--recovery-dir", str(self.recovery),
                                     providers=[self.old_provider(calls)])
        self.assertEqual(code, 1, blocked)
        agents = self.item(blocked)
        self.assertEqual((agents["action"], agents["reason"], agents["merge_requested"]), ("blocked", "no-baseline", True))
        self.assertEqual(calls, [], "没有基线摘要就不猜：不去取任何历史版本包")
        notes = agents["note"]
        self.assertEqual(sum("基线" in note and "无法三方合并" in note for note in notes), 1, notes)
        self.assertFalse(any("建议先保留" in note for note in notes), "同一屏里不能同时说“无法合并”又说“建议先保留”")
        self.assertIn("--keep AGENTS.md / --append AGENTS.md / --accept AGENTS.md", "".join(notes))
        self.assertEqual((self.root / KEY).read_text(encoding="utf-8"), mine)
        self.assertFalse(self.recovery.exists())

    def test_a7_keep_then_merge_without_a_baseline_stays_blocked_with_a_reason_and_keeps_the_file(self) -> None:
        mine = self.edit_user_file(self.template_line(), "我自己改过的一行")
        self.drop_baseline()
        code, kept = self.refresh(self.new_template_with_suffix(), "--keep", KEY, "--apply", providers=[])
        self.assertEqual(code, 0, kept)
        self.assertFalse(any("建议先保留" in note for note in self.item(kept)["note"]), "已经 --keep 了，不能再建议保留")
        code, again = self.refresh(self.new_template_with_suffix(), "--merge", KEY, providers=[self.old_provider()])
        self.assertEqual(code, 1, again)
        agents = self.item(again)
        self.assertEqual((agents["state"], agents["action"], agents["reason"]), ("kept-by-decision", "blocked", "no-baseline"))
        self.assertIn("无法三方合并", "".join(agents["note"]))
        self.assertEqual((self.root / KEY).read_text(encoding="utf-8"), mine)

    def test_a7_append_then_merge_still_merges_when_the_lock_has_a_baseline(self) -> None:
        line = self.template_line()
        (self.root / KEY).write_text(self.original + "\n我的补充\n", encoding="utf-8")
        new_template = init_project.load_templates()[KEY].replace(line, "THEIRS " + line)
        code, appended = self.refresh(new_template, "--append", KEY, "--apply", "--recovery-dir", str(self.recovery), providers=[])
        self.assertEqual(code, 0, appended)
        after_append = (self.root / KEY).read_text(encoding="utf-8")
        self.assertIn("我的补充", after_append)
        code, preview = self.refresh(new_template, "--merge", KEY, providers=[self.old_provider()])
        self.assertEqual((code, self.item(preview)["action"]), (0, "merge"), preview)
        self.assertEqual(self.item(preview)["merge"]["conflicts"], [])
        self.assertEqual((self.root / KEY).read_text(encoding="utf-8"), after_append)  # 预览不写
        code, done = self.refresh(new_template, "--merge", KEY, "--apply", "--recovery-dir", str(self.recovery / "m"),
                                  providers=[self.old_provider()])
        self.assertEqual(code, 0, done)
        merged = (self.root / KEY).read_text(encoding="utf-8")
        self.assertIn("THEIRS " + line, merged)
        self.assertIn("我的补充", merged)
        self.assertEqual((Path(done["recovery"]) / KEY).read_text(encoding="utf-8"), after_append)

    def test_a7_merge_overrides_an_earlier_keep_decision_but_still_stops_on_unchosen_conflicts(self) -> None:
        line, new_template = self.conflicting()
        before = (self.root / KEY).read_text(encoding="utf-8")
        code, kept = self.refresh(new_template, "--keep", KEY, "--apply", providers=[])
        self.assertEqual(code, 0, kept)
        code, plain = self.refresh(new_template, providers=[])
        self.assertEqual(self.item(plain)["state"], "kept-by-decision")
        code, blocked = self.refresh(new_template, "--merge", KEY, "--apply", "--recovery-dir", str(self.recovery),
                                     providers=[self.old_provider()])
        self.assertEqual((code, self.item(blocked)["reason"]), (1, "merge-conflict"), blocked)
        self.assertEqual((self.root / KEY).read_text(encoding="utf-8"), before)
        self.assertFalse(self.recovery.exists(), "冲突没选完：不写入、不备份")
        code, done = self.refresh(new_template, "--merge", KEY, "--pick", f"{KEY}#1=both", "--apply",
                                  "--recovery-dir", str(self.recovery), providers=[self.old_provider()])
        self.assertEqual(code, 0, done)
        self.assertEqual((Path(done["recovery"]) / KEY).read_text(encoding="utf-8"), before)

    def test_a6a_status_words_follow_the_interface_language_and_json_values_do_not(self) -> None:
        self.edit_user_file(self.template_line(), "我自己改过的一行")
        new_template = self.new_template_with_suffix()
        zh = self.text_refresh(new_template, lang="zh")
        for raw in ("blocked", "[modified]", "[unchanged]"):
            self.assertNotIn(raw, zh)
        self.assertNotRegex(zh, r"(?m)^\s+keep\s")
        self.assertRegex(zh, r"阻塞\s+AGENTS\.md \[你改过\]")
        self.assertIn("[与新模板相同]", zh)
        en = self.text_refresh(new_template, lang="en")
        self.assertRegex(en, r"blocked\s+AGENTS\.md \[edited by you\]")
        self.assertRegex(en, r"keep\s+CLAUDE\.md \[same as template\]")
        code, result = self.refresh(new_template)
        self.assertEqual((self.item(result)["state"], self.item(result)["action"]), ("modified", "blocked"))
        self.assertEqual({entry["state"] for entry in result["files"]}, {"modified", "unchanged"})
        with mock.patch.dict(os.environ, {"AGENTHELM_LANG": "en"}):
            self.assertEqual([init_project.state_label(s) for s in ("kept-by-decision", "local-only", "missing", "symlink", "clean")],
                             ["kept by decision", "only your edits", "missing", "symlink", "untouched"])
            self.assertEqual(init_project.state_label("future-state"), "future-state")

    def test_a6b_diff_prints_only_the_difference(self) -> None:
        self.edit_user_file(self.template_line(), "我自己改过的一行")
        text = self.text_refresh(self.new_template_with_suffix(), "--diff", KEY)
        self.assertIn("--- a/AGENTS.md", text)
        self.assertIn("+新模板新增的内容", text)
        for other in ("AGENT.RULES.md", "CLAUDE.md", "docs/refs/README.md", "[与新模板相同]", "先看差异"):
            self.assertNotIn(other, text)
        self.assertNotIn("以上为预览", text)
        code, result = self.refresh(self.new_template_with_suffix(), "--diff", KEY)  # JSON 保持完整
        self.assertTrue(result["files"] and KEY in result["diffs"])

    def test_a6c_every_suggested_command_names_the_file(self) -> None:
        self.edit_user_file(self.template_line(), "我自己改过的一行")
        code, blocked = self.refresh(self.new_template_with_suffix())
        texts = [blocked["hint"], *self.item(blocked)["note"]]
        joined = "\n".join(texts)
        for command in ("--keep AGENTS.md", "--append AGENTS.md", "--accept AGENTS.md", "--confirm-discard AGENTS.md", "--diff AGENTS.md", "--merge AGENTS.md"):
            self.assertIn(command, joined)
        self.assertNotIn("<文件>", joined)
        self.assertNotRegex(joined, r"--(keep|append|accept|diff|merge) (?!\S*\.md)")
        code, refused = self.refresh(self.new_template_with_suffix(), "--accept", KEY)
        self.assertEqual(code, 1, refused)
        self.assertIn("--confirm-discard AGENTS.md", refused["hint"])
        self.assertNotIn("<文件>", refused["hint"])

    def test_a6d_a_file_kept_by_decision_is_not_suggested_to_be_kept_again(self) -> None:
        for baseline_dropped in (False, True):
            with self.subTest(baseline_dropped=baseline_dropped):
                self.setUp()
                self.edit_user_file(self.template_line(), "我自己改过的一行")
                if baseline_dropped:
                    self.drop_baseline()
                new_template = self.new_template_with_suffix()
                code, kept = self.refresh(new_template, "--keep", KEY, "--apply", providers=[])
                self.assertEqual(code, 0, kept)
                code, plain = self.refresh(new_template, providers=[])
                agents = self.item(plain)
                self.assertEqual((code, agents["state"], agents["action"]), (0, "kept-by-decision", "keep"), plain)
                notes = "".join(agents["note"])
                self.assertNotIn("建议先保留", notes)
                self.assertNotIn("需要你选择", notes)
                self.assertIn("已按你的决定保留", notes)
                self.assertIn("--accept AGENTS.md", notes)
                self.assertEqual("--merge AGENTS.md" in notes, not baseline_dropped)
                self.assertNotIn("先看差异", notes)
                self.assertEqual(plain["blocked"], [])

    # ---- A8：隐私约定只在“声明了私有路径且文件里没有这条约定”时才追加，预览用条件式措辞说明
    def declare_private_paths(self, paths: list[str]) -> None:
        profile_path = self.root / init_project.PROFILE_PATH
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile.setdefault("privacy", {})["forbidden_default_reads"] = paths
        profile_path.write_text(json.dumps(profile), encoding="utf-8")

    def append_preview(self, user_text: str, paths: list[str]) -> dict:
        self.declare_private_paths(paths)
        (self.root / KEY).write_text(user_text, encoding="utf-8")
        code, preview = self.refresh(None, "--append", KEY)
        self.assertEqual(code, 0, preview)
        return self.item(preview)

    def test_a8_undeclared_private_paths_say_the_privacy_convention_is_not_appended(self) -> None:
        agents = self.append_preview("# 我自己写的契约\n", [])
        self.assertEqual((agents["action"], agents["regions"], agents["privacy_contract"]), ("append", ["agents-contract"], "undeclared-skipped"))
        notes = "".join(agents["note"])
        self.assertIn("未声明私有路径，不追加隐私约定", notes)
        self.assertIn("agents-contract", notes)
        self.assertNotIn("privacy-contract", notes.replace("隐私约定只在", ""))

    def test_a8_declared_private_paths_without_the_convention_append_it_and_say_why(self) -> None:
        agents = self.append_preview("# 我自己写的契约\n", [".env"])
        self.assertEqual((agents["regions"], agents["privacy_contract"]), (["agents-contract", "privacy-contract"], "appended"))
        notes = "".join(agents["note"])
        self.assertIn("项目声明了私有路径", notes)
        self.assertIn("追加隐私约定区块", notes)
        self.assertNotIn("未声明私有路径", notes)

    def test_a8_a_custom_privacy_convention_in_the_file_is_not_duplicated(self) -> None:
        agents = self.append_preview("# 我自己写的契约\n私有目录见 `privacy.forbidden_default_reads`，不要读。\n", [".env"])
        self.assertEqual((agents["regions"], agents["privacy_contract"]), (["agents-contract"], "present-skipped"))
        notes = "".join(agents["note"])
        self.assertIn("已有隐私约定", notes)
        self.assertNotIn("追加隐私约定区块", notes)

    def test_a8_the_blocked_suggestion_states_the_append_condition(self) -> None:
        self.edit_user_file(self.template_line(), "我自己改过的一行")
        code, blocked = self.refresh(self.new_template_with_suffix())
        notes = "".join(self.item(blocked)["note"])
        self.assertIn("隐私约定只在项目声明了私有路径、且文件里还没有这条约定时才追加", notes)


def build_pack(directory: Path, version: str, template: str) -> dict:
    """plugins/agents-init layout (content.json + sources) under ``directory``; returns the content.json data."""
    sources = {"skills/agents-init/AGENT.template.md": template, "skills/agents-init/launcher.sh": "#!/bin/sh\necho " + version + "\n"}
    for name, text in sources.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    content = {"schema_version": 1, "packs": [{"id": PACK_ID, "version": version, "sources": list(sources),
                                               "files": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sources.items()}}]}
    (directory / "content.json").write_text(json.dumps(content), encoding="utf-8")
    return content


class BaselineSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)

    def test_runtime_cache_yields_verified_packs_with_the_wanted_version_first(self) -> None:
        for version in ("6.7.0", "6.8.0"):
            build_pack(self.tmp / "runtimes" / version / "plugins/agents-init", {"6.7.0": "1.2.0", "6.8.0": "1.3.0"}[version], f"T{version}")
        broken = self.tmp / "runtimes/6.9.0/plugins/agents-init"
        build_pack(broken, "1.4.0", "T6.9.0")
        (broken / "skills/agents-init/AGENT.template.md").write_text("tampered", encoding="utf-8")  # digest no longer matches
        found = list(baseline.from_runtime_cache(PACK_ID, ["1.2.0"], self.tmp / "runtimes"))
        self.assertEqual([item.version for item in found], ["1.2.0", "1.3.0"])
        self.assertEqual((found[0].template, found[0].kind), ("T6.7.0", "cache"))
        self.assertEqual(list(baseline.from_runtime_cache(PACK_ID, [], self.tmp / "missing")), [])

    def test_git_tags_are_read_without_a_checkout(self) -> None:
        repo = self.tmp / "repo"
        pack_dir = repo / "plugins/agents-init"
        repo.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo)]
        subprocess.run([*git, "init", "-q"], check=True)
        for tag, version in (("v6.7.0", "1.2.0"), ("v6.8.0", "1.3.0")):
            build_pack(pack_dir, version, "T" + version)
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-q", "-m", tag], check=True)
            subprocess.run([*git, "tag", tag], check=True)
        found = list(baseline.from_git_tags(PACK_ID, ["1.2.0"], pack_dir))
        self.assertEqual([(item.version, item.label) for item in found], [("1.2.0", "git tag v6.7.0"), ("1.3.0", "git tag v6.8.0")])
        self.assertEqual(found[0].template, "T1.2.0")
        self.assertEqual(list(baseline.from_git_tags(PACK_ID, [], self.tmp)), [])  # not a repository

    def release_fixture(self, *, tamper: bool = False) -> tuple[baseline.Fetch, list[str]]:
        pack_dir = self.tmp / "agenthelm-v6.8.0/plugins/agents-init"
        content = build_pack(pack_dir, "1.3.0", "T1.3.0")
        archive_path = self.tmp / "release.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(self.tmp / "agenthelm-v6.8.0", arcname="agenthelm-v6.8.0")
        archive_bytes = archive_path.read_bytes()
        digest = hashlib.sha256(archive_bytes).hexdigest()
        requested: list[str] = []
        base = "https://github.com/lc4t/agenthelm/releases/download/v6.8.0"

        def fetch(url: str) -> bytes:
            requested.append(url)
            if url.startswith("https://api.github.com/repos/lc4t/agenthelm/releases"):
                return json.dumps([{"tag_name": "v6.9.0"}, {"tag_name": "v6.8.0"}]).encode()
            if url.endswith("/v6.9.0/plugins/agents-init/content.json"):
                return json.dumps({"packs": [{"id": PACK_ID, "version": "1.4.0"}]}).encode()
            if url.endswith("/v6.8.0/plugins/agents-init/content.json"):
                return json.dumps(content).encode()
            if url == f"{base}/release.json":
                return json.dumps({"tag": "v6.8.0", "tarball_sha256": ("0" * 64) if tamper else digest}).encode()
            if url == f"{base}/agenthelm-v6.8.0.tar.gz":
                return archive_bytes
            raise OSError("unexpected " + url)

        return fetch, requested

    def test_github_release_downloads_only_a_wanted_version_and_checks_its_sha256(self) -> None:
        fetch, requested = self.release_fixture()
        found = list(baseline.from_github_release(PACK_ID, ["1.3.0"], fetch, environ={}))
        self.assertEqual([(item.version, item.kind, item.label) for item in found], [("1.3.0", "release", "GitHub Release v6.8.0")])
        self.assertEqual(found[0].template, "T1.3.0")
        self.assertFalse([url for url in requested if "v6.9.0" in url and "download" in url])  # the other release was never fetched
        fetch, _requested = self.release_fixture(tamper=True)
        self.assertEqual(list(baseline.from_github_release(PACK_ID, ["1.3.0"], fetch, environ={})), [])

    def test_offline_or_disabled_network_yields_nothing_and_never_raises(self) -> None:
        def offline(url: str) -> bytes:
            raise OSError("no network")

        self.assertEqual(list(baseline.from_github_release(PACK_ID, ["1.3.0"], offline, environ={})), [])
        fetch, requested = self.release_fixture()
        self.assertEqual(list(baseline.from_github_release(PACK_ID, ["1.3.0"], fetch, environ={"AGENTHELM_NO_UPDATE_CHECK": "1"})), [])
        self.assertEqual(requested, [])

    def test_the_default_provider_order_is_cache_then_git_then_release(self) -> None:
        names = [getattr(item, "__name__", "") for item in baseline.default_providers()]
        self.assertEqual(names[:2], ["from_runtime_cache", "from_git_tags"])
        self.assertEqual(len(names), 3)


class EndToEndBaselineTests(unittest.TestCase):
    """The real renderer + a baseline from a (fixture) git tag: nothing mocked except where the old pack comes from."""

    def test_old_pack_from_a_tag_rebuilds_the_baseline_and_merges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home, root = Path(temporary) / "home", Path(temporary) / "project"
            home.mkdir()
            root.mkdir()
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                done = subprocess.run([sys.executable, str(SCRIPT), "--project", str(root), "--name", "Notes", "--project-type", "study",
                                       "--vcs", "none", "--stack", "none", "--runtime", "local", "--agent-cli", "none", "--entry-name", "menu",
                                       "--output", "json", "--apply"], capture_output=True, text=True, check=False)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                pack = init_project.contract_pack()
                old_dir = Path(temporary) / "old/plugins/agents-init"
                build_pack(old_dir, pack["version"], init_project.TEMPLATE_PATH.read_text(encoding="utf-8"))
                # the launcher of the old pack is what the project's launcher was rendered with
                (old_dir / "skills/agents-init/launcher.sh").write_text(init_project.LAUNCHER_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
                content = json.loads((old_dir / "content.json").read_text())
                content["packs"][0]["files"]["skills/agents-init/launcher.sh"] = hashlib.sha256(
                    (old_dir / "skills/agents-init/launcher.sh").read_bytes()).hexdigest()
                (old_dir / "content.json").write_text(json.dumps(content))
                providers = [lambda pack_id, wanted: baseline.from_runtime_cache(pack_id, wanted, Path(temporary) / "old-runtimes")]
                (Path(temporary) / "old-runtimes/6.8.0/plugins").mkdir(parents=True)
                shutil.copytree(old_dir, Path(temporary) / "old-runtimes/6.8.0/plugins/agents-init")
                (root / "AGENTS.md").write_text((root / "AGENTS.md").read_text().replace("# ", "# 我的 ", 1), encoding="utf-8")
                newer = {**init_project.load_templates()}
                newer["AGENTS.md"] += "\n## 新章节\n新内容\n"
                output = io.StringIO()
                with mock.patch.object(init_project, "load_templates", return_value=newer), \
                        mock.patch.object(init_project, "contract_pack", return_value={**pack, "version": "9.9.0"}), \
                        mock.patch.object(init_project, "baseline_providers", return_value=providers), \
                        mock.patch("sys.stdout", output):
                    code = init_project.main(["--project", str(root), "--mode", "refresh", "--merge", "AGENTS.md", "--output", "json"])
                result = json.loads(output.getvalue())
                agents = next(item for item in result["files"] if item["path"] == "AGENTS.md")
                self.assertEqual((code, agents["action"], agents["merge"]["source"]), (0, "merge", "runtime cache 6.8.0"), result)
                self.assertIn("+新内容", agents["diff"])


if __name__ == "__main__":
    unittest.main()
