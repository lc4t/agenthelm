"""第 9 屏的写入向导：用输入注入驱动真实的菜单循环与真实的 agenthelm relocate / dedupe。

覆盖：取消（零写入）、yes 之后写入并读回、预览之后内容变化（摘要不一致）、blocked、放宽需键入条目名、读回失败、
证据未知与 MCP 只给手工步骤、没有对应 dedupe 分组的重复只读、输入中途 EOF（不阻塞、零写入）。
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import contextlib
import io
import json
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_project_extensions_actions import FAKE_SECRET, ActionCase  # noqa: E402

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
import project_cli  # noqa: E402
import project_ext_actions as actions  # noqa: E402
import project_menu  # noqa: E402
import project_real  # noqa: E402

AGENTHELM = Path(__file__).resolve().parents[3] / "plugins/agent-pack/runtime/agenthelm.py"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class WizardCase(ActionCase):
    def setUp(self) -> None:
        super().setUp()
        bin_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [item.unlink() for item in bin_dir.iterdir()] and bin_dir.rmdir())
        shim = bin_dir / "agenthelm"
        shim.write_text(f"#!/bin/sh\nexec {sys.executable} {AGENTHELM} \"$@\"\n", encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        self.env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def run_menu(self, inputs: list) -> tuple[int, str]:
        """inputs 里的可调用项在读到它时执行（用来在预览与 yes 之间改动磁盘），其返回值是这一行输入；用完后读到 EOF。"""
        feed = iter(inputs)

        def fake_input(prompt: str = "") -> str:
            try:
                item = next(feed)
            except StopIteration:
                raise EOFError from None
            return item() if callable(item) else item

        buffer = io.StringIO()
        with mock.patch.dict(os.environ, self.env), mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(buffer):
            provider = project_real.RealProvider(self.project, True, offline=True, host=None, via_launcher=None)
            code = project_menu.run(provider)
        return code, ANSI.sub("", buffer.getvalue())

    def move_inputs(self, ref: str, target: str, *rest) -> list:
        return ["9", "5", ref, target, "", *rest]


class MoveWizardTests(WizardCase):
    SOLO = "skill:solo@claude:user"

    def test_cancel_writes_nothing(self) -> None:
        before = self.tree()
        code, out = self.run_menu(self.move_inputs(self.SOLO, "2", ""))  # 2 = codex:user，确认处回车
        self.assertEqual(code, 0)
        self.assertEqual(self.tree(), before)
        self.assertIn("已取消，没有写入。", out)
        self.assertIn("plan_digest：", out)  # 预览已展示

    def test_eof_in_the_middle_of_the_wizard_never_blocks_and_writes_nothing(self) -> None:
        before = self.tree()
        code, _out = self.run_menu(self.move_inputs(self.SOLO, "2"))  # 到确认处输入用完
        self.assertEqual(code, 0)
        self.assertEqual(self.tree(), before)

    def test_yes_writes_then_reads_back(self) -> None:
        code, out = self.run_menu(self.move_inputs(self.SOLO, "2", "yes"))
        self.assertEqual(code, 0, out)
        self.assertTrue((self.home / ".agents/skills/solo/SKILL.md").is_file())
        self.assertFalse((self.home / ".claude/skills/solo").exists())
        self.assertIn("读回：一致", out)
        self.assertNotIn("读回失败", out)

    def test_a_change_after_the_preview_is_refused(self) -> None:
        def touch_then_yes() -> str:
            (self.home / ".claude/skills/solo/SKILL.md").write_text("---\nname: solo\ndescription: changed\n---\nnew\n", encoding="utf-8")
            return "yes"

        code, out = self.run_menu(self.move_inputs(self.SOLO, "2", touch_then_yes))
        self.assertEqual(code, 1)
        self.assertIn("摘要不一致", out)
        self.assertFalse((self.home / ".agents/skills/solo").exists())
        self.assertTrue((self.home / ".claude/skills/solo/SKILL.md").is_file())

    def test_a_blocked_plan_has_no_confirmation_step(self) -> None:
        before = self.tree()
        code, out = self.run_menu(self.move_inputs("skill:dup-same@claude:user", "1", "yes"))  # claude:project 里已有同名
        self.assertEqual(code, 0)
        self.assertIn("blocked", out)
        self.assertIn("不可执行", out)
        self.assertEqual(self.tree(), before)

    def test_user_to_project_shows_the_project_content_notice(self) -> None:
        code, out = self.run_menu(self.move_inputs(self.SOLO, "1", ""))  # claude:project
        self.assertEqual(code, 0)
        self.assertIn("将进入项目目录", out)
        self.assertFalse((self.project / ".claude/skills/solo").exists())

    def test_a_failed_readback_is_shown_and_sets_the_exit_code(self) -> None:
        bad = {"ok": False, "checks": [{"id": "target-present", "ok": False, "message": "目标条目存在"}]}
        with mock.patch.object(actions, "readback_move", return_value=bad):
            code, out = self.run_menu(self.move_inputs(self.SOLO, "2", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("读回失败", out)
        self.assertIn("recovery_dir", out)


class ReadOnlyTests(WizardCase):
    def test_an_unknown_evidence_entry_only_gets_manual_steps(self) -> None:
        provider = mock.MagicMock()
        entry = {"ref": "skill:x@claude:user", "kind": "skill", "name": "x", "client": "claude", "scope": "user", "evidence": "unknown"}
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            project_menu.extensions_move_wizard(provider, entry)
            project_menu.extensions_scope_wizard(provider, entry)
        provider.extensions_move.assert_not_called()
        provider.extensions_scope.assert_not_called()
        self.assertIn("证据为未知", ANSI.sub("", buffer.getvalue()))

    def test_an_mcp_entry_only_gets_the_manual_command_and_snippet(self) -> None:
        raw = (self.home / ".claude.json").read_bytes()
        before = self.tree()
        code, out = self.run_menu(["9", "5", "mcp:dupm@claude:user"])
        self.assertEqual(code, 0)
        self.assertIn("claude mcp remove --scope user dupm", out)
        self.assertIn("只给手工命令与可粘贴片段", out)
        self.assertNotIn(FAKE_SECRET, out)
        self.assertEqual(self.tree(), before)
        self.assertEqual((self.home / ".claude.json").read_bytes(), raw)

    def test_the_p_action_prints_the_profile_snippet(self) -> None:
        _code, out = self.run_menu(["9", "solo", "p"])
        self.assertIn('"skill_scope"', out)
        self.assertIn('"solo": "project"', out)

    def test_a_group_without_a_dedupe_list_entry_is_read_only(self) -> None:
        agents = self.home / ".agents/skills/cross"
        agents.mkdir(parents=True)
        (agents / "SKILL.md").write_text("---\nname: cross\ndescription: Demo cross\n---\nbody\n", encoding="utf-8")
        claude = self.home / ".claude/skills/cross"
        claude.mkdir(parents=True)
        (claude / "SKILL.md").write_text("---\nname: cross\ndescription: Demo cross\n---\nbody\n", encoding="utf-8")
        self.assertFalse([g for g in self.groups() if g["key"] == "cross"])  # dedupe --list 看不到跨客户端的同名技能
        before = self.tree()
        _code, out = self.run_menu(["9", "3", "2"])  # 重复屏里第 2 组：cross（dup-same 排第 1）
        self.assertIn(actions.NO_WRITE_GROUP, out)
        self.assertEqual(self.tree(), before)


class DedupeAndScopeWizardTests(WizardCase):
    def test_dedupe_from_the_entry_detail_retires_the_other_copy(self) -> None:
        code, out = self.run_menu(["9", "skill:dup-same@claude:user", "d", "1", "1", "yes"])  # 保留第 1 份
        self.assertEqual(code, 0, out)
        self.assertIn("读回：一致", out)
        remaining = [(self.project / ".claude/skills/dup-same").exists(), (self.home / ".claude/skills/dup-same").exists()]
        self.assertEqual(sorted(remaining), [False, True])  # 保留一份，另一份已归档

    def test_widening_needs_the_entry_name(self) -> None:
        profile = json.loads(self.profile.read_text(encoding="utf-8"))
        profile["capabilities"]["skill_scope"] = {"solo": "project"}
        self.profile.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
        before = self.tree()
        _code, out = self.run_menu(["9", "6", "skill:solo@claude:user", "2", "wrong", "yes"])
        self.assertIn("放宽", out)
        self.assertIn("已取消，没有写入。", out)
        self.assertEqual(self.tree(), before)
        code, out = self.run_menu(["9", "6", "skill:solo@claude:user", "2", "solo", "yes"])
        self.assertEqual(code, 0, out)
        written = json.loads(self.profile.read_text(encoding="utf-8"))["capabilities"]["skill_scope"]
        self.assertEqual(written, {"solo": "user"})

    def test_narrowing_writes_the_declaration_after_yes(self) -> None:
        code, out = self.run_menu(["9", "6", "skill:solo@claude:user", "1", "yes"])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(self.profile.read_text(encoding="utf-8"))["capabilities"]["skill_scope"], {"solo": "project"})

    def test_the_menu_labels_for_five_and_six_are_no_longer_marked_unavailable(self) -> None:
        _code, out = self.run_menu(["9"])
        self.assertNotIn("本版尚未提供", out)
        self.assertEqual(project_cli.EXIT_ATTENTION, 1)


if __name__ == "__main__":
    unittest.main()
