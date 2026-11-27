"""第 9 屏「技能与 MCP」：用输入注入驱动真实的菜单循环，只读、不阻塞、降级如实说明。

数据来自假的 agenthelm（回放 tests/fixtures 里的夹具）或演示项目；覆盖 inventory 不可用、schema_version 未知、
空清单、含 unknown 的清单、含假令牌的 MCP 条目、演示项目，以及 --no-input 不等待输入。
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import contextlib
import io
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_project_extensions import CLI, FAKE_SECRET, ExtensionsCase  # noqa: E402

RUNTIME = CLI.parent
sys.path.insert(0, str(RUNTIME))
import project_demo  # noqa: E402
import project_extensions as ext  # noqa: E402
import project_menu  # noqa: E402
import project_real  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")
CJK = re.compile("[一-鿿]")


class MenuCase(ExtensionsCase):
    def provider(self, **kwargs):
        with mock.patch.dict(os.environ, self.env(**kwargs)):
            return project_real.RealProvider(self.project, True, offline=True, host=None, via_launcher=None)

    def run_menu(self, provider, inputs: list[str], *, lang: str = "zh", **env_kwargs) -> tuple[int, str]:
        """真实的菜单循环；``inputs`` 用完后读到 EOF，相当于用户退出。"""
        feed = iter(inputs)

        def fake_input(prompt: str = "") -> str:
            try:
                return next(feed)
            except StopIteration:
                raise EOFError from None

        buffer = io.StringIO()
        env = self.env(**env_kwargs)
        env["AGENTHELM_LANG"] = lang
        with mock.patch.dict(os.environ, env), mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(buffer):
            code = project_menu.run(provider)
        return code, ANSI.sub("", buffer.getvalue())


class MenuStructureTests(MenuCase):
    def test_numbers_one_to_eight_keep_their_places_and_nine_is_new(self) -> None:
        self.assertEqual([entry[3] if entry else None for entry in project_menu.MENU],
                         ["find", "progress", "guide", "health", None, "fix", "record", "tools", "sop", "extensions"])
        self.assertEqual([entry[0] for entry in project_menu.MENU if entry], [str(n) for n in range(1, 10)])
        self.assertEqual(sorted(project_menu.SCREENS), [str(n) for n in range(1, 9)] + ["9"])
        self.assertIs(project_menu.SCREENS["8"], project_menu.sop_screen)
        self.assertIs(project_menu.SCREENS["4"], project_menu.health_screen)

    def test_home_lists_the_new_entry_after_the_sop_entry(self) -> None:
        _code, text = self.run_menu(self.provider(), [])
        lines = [line for line in text.splitlines() if re.match(r"^\s+\d  ", line)]
        self.assertEqual([line.split()[0] for line in lines], [str(n) for n in range(1, 10)])
        self.assertIn("技能与 MCP", lines[-1])
        self.assertNotIn("不可用", lines[-1])

    def test_home_explains_why_the_entry_is_unavailable(self) -> None:
        _code, text = self.run_menu(self.provider(path=self.empty), [], path=self.empty)
        self.assertIn("不可用：未找到 agent-pack 的 inventory", text)

    def test_no_input_never_waits_and_lists_the_capability(self) -> None:
        result = subprocess.run([sys.executable, str(CLI), "--project", str(self.project), "--no-input"], input="", capture_output=True,
                                text=True, env=self.env(), timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("技能与 MCP", result.stdout)
        result = subprocess.run([sys.executable, str(CLI), "--project", str(self.project), "--no-input", "extensions", "check"], input="",
                                capture_output=True, text=True, env=self.env(), timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


class ExtensionsScreenTests(MenuCase):
    def test_overview_has_the_summary_and_six_entries_all_available(self) -> None:
        _code, text = self.run_menu(self.provider(), ["9"])
        self.assertIn("技能 7 个 · MCP 8 个", text)
        self.assertIn("每会话 description 常驻：claude ≈", text)
        self.assertIn("重复 5 组（MCP 3 · 技能 2）  未知 4 项", text)
        screen = text[text.index("按客户端看") - 200:]
        for number, title in (("1", "按客户端看"), ("2", "按项目看"), ("3", "按重复看"), ("4", "技能体检")):
            self.assertRegex(screen, rf"{number}  {title}")
        self.assertRegex(screen, r"5  移动或复制")
        self.assertRegex(screen, r"6  设置生效范围")
        self.assertNotIn("本版尚未提供", screen)  # N2b：5、6 是真实向导（tests/test_project_extensions_wizard.py）
        self.assertIn("? 帮助", screen)

    def test_entries_five_and_six_ask_for_an_entry_and_do_not_pretend(self) -> None:
        _code, text = self.run_menu(self.provider(), ["9", "5", "no-such-entry", "6", "no-such-entry"])
        self.assertEqual(text.count("没有这个条目：no-such-entry"), 2)
        self.assertNotIn("本版尚未提供", text)

    def test_help_and_unknown_words(self) -> None:
        _code, text = self.run_menu(self.provider(), ["9", "?", "no-such-entry"])
        self.assertIn("清单与体检只读", text)
        self.assertIn("确认前零写入", text)
        self.assertNotIn("全程只读", text)
        self.assertIn("没有这个条目：no-such-entry", text)

    def test_by_client_then_detail_then_back_then_quit(self) -> None:
        code, text = self.run_menu(self.provider(), ["9", "1", "pdf-notes", "", "", "", "q"])
        self.assertEqual(code, 0)
        self.assertIn("claude · 证据 file", text)
        self.assertIn("SKILL.md 体积", text)
        self.assertIn("[无 description]", text)
        self.assertIn("codex · 证据 file", text)

    def test_by_project_groups_by_reach(self) -> None:
        _code, text = self.run_menu(self.provider(), ["9", "2"])
        self.assertIn("本项目（仅在此项目里生效）", text)
        self.assertIn("所有项目共用（用户级）", text)
        self.assertIn("仅 Desktop 应用", text)
        self.assertIn("没在本机同步过的项目不可见", text)

    def test_known_projects_are_listed_by_name_only(self) -> None:
        self.set_inventory(lambda data: data.update(projects=[{"name": "sibling", "path": "/not/shown", "last_synced": "2026-11-01"}]))
        _code, text = self.run_menu(self.provider(), ["9", "2"])
        self.assertIn("已同步过的其他项目", text)
        self.assertIn("sibling", text)
        self.assertNotIn("/not/shown", text)

    def test_by_duplicate_numbers_open_the_member_details(self) -> None:
        _code, text = self.run_menu(self.provider(), ["9", "3", "1"])
        self.assertIn("重复 MCP（3 组）", text)
        self.assertIn("生效：未知", text)
        self.assertIn("mcp:fetch@cursor:project", text)
        self.assertIn("指纹（前 8 位）", text)

    def test_cross_client_group_is_not_called_a_conflict(self) -> None:
        _code, text = self.run_menu(self.provider(), ["9", "3", "2"])
        self.assertIn("各客户端独立加载，彼此不冲突", text)
        self.assertIn("不推荐退役", text)

    def test_check_screen_and_the_similarity_toggle(self) -> None:
        _code, text = self.run_menu(self.provider(), ["9", "4"])
        self.assertIn("不打分、不排名、不建议删除", text)
        self.assertNotIn("description 相似（机械判断", text)
        _code, text = self.run_menu(self.provider(), ["9", "4", "d"])
        self.assertIn("description 相似（机械判断", text)
        self.assertIn("没有外部依据", text)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any("--include-description" in call for call in calls))

    def test_nothing_is_written_and_only_inventory_is_called(self) -> None:
        before = sorted(path.relative_to(self.base).as_posix() for path in self.base.rglob("*") if path.is_file() and path != self.log)
        self.run_menu(self.provider(), ["9", "1", "", "2", "", "3", "1", "", "", "4", "d", "", "", "q"])
        after = sorted(path.relative_to(self.base).as_posix() for path in self.base.rglob("*") if path.is_file() and path != self.log)
        self.assertEqual(before, after)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(call[0] == "inventory" or call[:2] == ["dedupe", "--list"] for call in calls), calls)  # 只有只读调用

    def test_no_screen_prints_a_secret(self) -> None:
        _code, text = self.run_menu(self.provider(), ["9", "1", "mcp:notes-server@claude:user", "", "", "2", "", "3", "1", "", "2", "", "", "4", "d", "",
                                                      "mcp:remote-docs@claude:project", "", "mcp:local-db@claude-desktop:desktop", "", "", "q"])
        for needle in (FAKE_SECRET, "Bearer", "X-Api-Key", "--token", "--password", "?token="):
            self.assertNotIn(needle, text)
        self.assertIn("NOTES_API_KEY", text)
        self.assertIn("docs.example.com", text)
        self.assertIn("含内联密钥值", text)

    def test_unknown_evidence_shows_unknown_on_every_screen(self) -> None:
        _code, text = self.run_menu(self.provider(), ["9", "1"])
        rows = [line for line in text.splitlines() if re.match(r"^\s+\? ", line)]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all("[未知]" in line for line in rows), rows)
        _code, text = self.run_menu(self.provider(), ["9", "3", "1"])
        self.assertIn("证据不足", text)

    def test_empty_inventory(self) -> None:
        self.set_inventory(lambda data: data.update(skills=[], mcps=[], clients=[], unknown=[]))
        code, text = self.run_menu(self.provider(), ["9", "1", "", "2", "", "3", "", "4", ""])
        self.assertEqual(code, 0)
        self.assertIn("技能 0 个 · MCP 0 个", text)
        self.assertIn("没有匹配的条目", text)
        self.assertIn("没有发现重复", text)
        self.assertIn("没有技能条目", text)

    def test_unavailable_inventory_stops_at_the_reason(self) -> None:
        code, text = self.run_menu(self.provider(path=self.empty), ["9", "1"], path=self.empty)
        self.assertEqual(code, 0)
        self.assertIn("不可用：未找到 agent-pack 的 inventory", text)
        self.assertNotIn("1  按客户端看", text)

    def test_unknown_schema_version_is_unavailable_with_the_reason(self) -> None:
        self.set_inventory(lambda data: data.update(schema_version=7))
        _code, text = self.run_menu(self.provider(), ["9"])
        self.assertIn("不可用：inventory 的 schema_version 7 超出本菜单支持的主版本 1", text)
        self.assertNotIn("1  按客户端看", text)

    def test_broken_inventory_output_is_unavailable(self) -> None:
        _code, text = self.run_menu(self.provider(mode="bad-json"), ["9"], mode="bad-json")
        self.assertIn("不可用：", text)
        self.assertNotIn("1  按客户端看", text)

    def test_narrow_terminal_keeps_names_and_scopes(self) -> None:
        _code, text = self.run_menu(self.provider(), ["9", "1"], extra={"COLUMNS": "60"})
        rows = [line for line in text.splitlines() if re.match(r"^\s+[●⚙⌘?] ", line)]
        self.assertTrue(rows)
        for name in ("code-review", "long-guide", "notes-server", "remote-docs"):
            self.assertTrue(any(name in row for row in rows), name)
        self.assertTrue(all(re.search(r"(用户级|项目级|本机项目级|应用级)", row) for row in rows))


class ReferencedByTests(MenuCase):
    def test_column_is_absent_without_the_field(self) -> None:
        _code, text = self.run_menu(self.provider(), ["9", "1"])
        self.assertNotIn("被引用", text)
        self.assertNotIn("规则引用", text)

    def test_column_shows_for_every_row_once_any_entry_has_it_and_no_opinion_command_runs(self) -> None:
        def add(data: dict) -> None:
            next(s for s in data["skills"] if s["name"] == "pdf-notes")["referenced_by"] = [{"rule_id": "r1", "source": "p"}, {"rule_id": "r2"}]
            next(s for s in data["skills"] if s["name"] == "pdf-notes")["referenced_by"].append("garbage")
        self.set_inventory(add)
        _code, text = self.run_menu(self.provider(), ["9", "1"])
        self.assertIn("被 2 条规则引用", text)
        self.assertIn("被引用 —", text)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(call[0] == "inventory" for call in calls), "不调用任何 opinion 命令")

    def test_empty_list_counts_as_absent(self) -> None:
        self.set_inventory(lambda data: data["skills"][0].update(referenced_by=[]))
        _code, text = self.run_menu(self.provider(), ["9", "1"])
        self.assertNotIn("被引用", text)


class HealthJumpTests(MenuCase):
    def health(self, **kwargs) -> dict:
        provider = self.provider(**kwargs)
        with mock.patch.dict(os.environ, self.env(**kwargs)):
            return provider.health()

    def finding_number(self, text: str) -> int:
        match = re.search(r"^\s*(\d+)\s+\[技能与 MCP\] 发现 (\d+) 组重复", text, re.M)
        self.assertIsNotNone(match, text)
        return int(match.group(1))

    def test_health_has_an_advisory_finding_with_an_ext_verb_and_the_exit_code_is_unchanged(self) -> None:
        data = self.health()
        finding = next(item for item in data["findings"] if item["id"] == "extensions-duplicates")
        self.assertEqual(finding["level"], "info")
        self.assertRegex(finding["next"][0]["action"], r"^ext:[ms]-[0-9a-f]{6}$")
        self.assertIn("跨客户端的重复不计入", finding["reason"])
        base = self.health(path=self.empty)
        self.assertFalse(any(item["id"] == "extensions-duplicates" for item in base["findings"]))
        self.assertEqual([item for item in data["findings"] if item["id"] != "extensions-duplicates"], base["findings"])
        self.assertEqual(data["ok"], base["ok"])
        for path, expected in ((self.bin, 0), (self.empty, 0)):
            result = subprocess.run([sys.executable, str(CLI), "--project", str(self.project), "--offline", "--no-input", "health"], input="",
                                    capture_output=True, text=True, env=self.env(path=path), timeout=60, check=False)
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_the_ext_verb_jumps_to_the_duplicate_group(self) -> None:
        _code, text = self.run_menu(self.provider(), ["4"])
        number = self.finding_number(text)
        _code, text = self.run_menu(self.provider(), ["4", str(number)])
        self.assertIn("按重复看", text)
        self.assertIn("类型", text)
        self.assertRegex(text, r"(skill|mcp):[a-z-]+@[a-z-]+:[a-z]+")

    def test_no_finding_when_nothing_needs_attention(self) -> None:
        self.set_inventory(lambda data: data.update(skills=[], mcps=[data["mcps"][0], data["mcps"][1]]))
        findings = self.health()["findings"]
        self.assertFalse(any(item["id"] == "extensions-duplicates" for item in findings), "只有跨客户端重复时不提示")


class DemoMenuTests(unittest.TestCase):
    def run_demo(self, scenario: str, inputs: list[str], lang: str = "zh") -> str:
        feed = iter(inputs)

        def fake_input(prompt: str = "") -> str:
            try:
                return next(feed)
            except StopIteration:
                raise EOFError from None

        buffer = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENTHELM_LANG": lang, "NO_COLOR": "1"}), mock.patch("builtins.input", fake_input), \
                contextlib.redirect_stdout(buffer):
            project_menu.run(project_demo.DemoProvider(scenario))
        return ANSI.sub("", buffer.getvalue())

    def test_every_scenario_walks_through_the_screen(self) -> None:
        for scenario in project_demo.SCENARIOS:
            text = self.run_demo(scenario, ["9", "1", "", "2", "", "3", "1", "", "", "4", "d", "", "", "9", "code-review@claude:user", "", "", "q"])
            self.assertIn("技能 7 个 · MCP 10 个", text, scenario)
            self.assertIn("按重复看", text)
            self.assertIn("演示数据", text)
            self.assertNotIn(FAKE_SECRET, text)

    def test_demo_menu_speaks_english(self) -> None:
        text = self.run_demo("study", ["9", "1", "", "2", "", "3", "1", "", "", "4", "d", "", "?", "code-review@claude:user", "", "5", "", "q"], "en")
        self.assertFalse([line for line in text.splitlines() if CJK.search(line)], [line for line in text.splitlines() if CJK.search(line)][:3])
        self.assertIn("Skills & MCP", text)
        self.assertIn("Move or copy", text)


class TargetClassTests(MenuCase):
    """T3：同指纹的条目只在 target_class 相同时才算「不同名 · 同指纹」；mcp_hints 是一行信息，不算重复。没有这两个键就是旧版 agent-pack。"""

    COPY = {"name": "notes-copy", "client": "claude", "scope": "user", "store": "~/.claude.json", "evidence": "file",
            "transport": "stdio", "fingerprint": "a1b2c3d4e5f60718"}
    HINT = {"kind": "same-fingerprint-different-target", "client": "claude", "names": ["notes-server", "notes-copy"],
            "stores": ["~/.claude.json"], "fingerprint": "a1b2c3d4e5f60718", "target_relation": "different",
            "differs_in": ["cwd"], "reasons": [], "refs": []}

    def setup_inventory(self, copy_class: str | None, *, hint: dict | None = None, old: bool = False) -> None:
        def change(data: dict) -> None:
            data["mcps"].append(dict(self.COPY))
            if not old:
                for entry in data["mcps"]:
                    if entry["fingerprint"] == "a1b2c3d4e5f60718":
                        entry["target_class"] = "t-a1b2c3d4.1"
                data["mcps"][-1]["target_class"] = copy_class
                if hint:
                    data["mcp_hints"] = [hint]
        self.set_inventory(change)

    def dups(self) -> dict:
        return json.loads(self.run_cli("extensions", "dups", "--output", "json").stdout)["data"]

    def test_old_agent_pack_without_the_keys_keeps_the_old_grouping_and_output(self) -> None:
        self.setup_inventory(None, old=True)
        data = self.dups()
        self.assertEqual([g["label"] for g in data["groups"]].count("不同名 · 同指纹"), 1)
        self.assertNotIn("hints", data)
        text = self.run_menu(self.provider(), ["9", "3"])[1]
        self.assertIn("重复 MCP（4 组）", text)
        self.assertNotIn("MCP 提示", text)

    def test_same_target_class_still_groups(self) -> None:
        self.setup_inventory("t-a1b2c3d4.1")
        self.assertEqual([g["label"] for g in self.dups()["groups"]].count("不同名 · 同指纹"), 1)

    def test_different_target_class_is_not_a_duplicate_and_shows_one_hint_line(self) -> None:
        self.setup_inventory("t-a1b2c3d4.2", hint=self.HINT)
        data = self.dups()
        self.assertNotIn("不同名 · 同指纹", [g["label"] for g in data["groups"]])
        self.assertEqual([g["kind"] for g in data["groups"]].count("mcp"), 3)
        self.assertEqual(len(data["hints"]), 1)
        text = self.run_menu(self.provider(), ["9", "3"])[1]
        self.assertIn("重复 MCP（3 组）", text)
        self.assertIn("MCP 提示（不算重复，1 条）", text)
        self.assertIn("claude：notes-server、notes-copy 指纹相同，目标不同（差异：工作目录）", text)
        self.assertIn("重复 5 组（MCP 3 · 技能 2）", self.run_menu(self.provider(), ["9"])[1])

    def test_hint_without_a_group_still_shows_and_the_screen_says_no_duplicates(self) -> None:
        def clean(data: dict) -> None:
            data["mcps"] = [{**self.COPY, "name": "a", "target_class": "t-a1b2c3d4.1"},
                            {**self.COPY, "name": "b", "target_class": "t-a1b2c3d4.2"}]
            data["skills"] = []
            data["mcp_hints"] = [{**self.HINT, "names": ["a", "b"], "kind": "same-fingerprint-target-unconfirmed", "differs_in": []}]
        self.set_inventory(clean)
        text = self.run_menu(self.provider(), ["9", "3"])[1]
        self.assertIn("没有发现重复", text)
        self.assertIn("claude：a、b 指纹相同，无法确认目标是否相同", text)

    def test_hint_lines_follow_the_interface_language(self) -> None:
        self.setup_inventory("t-a1b2c3d4.2", hint=self.HINT)
        text = self.run_menu(self.provider(), ["9", "3"], lang="en")[1]
        self.assertIn("MCP hints (not duplicates, 1)", text)
        self.assertIn("claude: notes-server, notes-copy have the same fingerprint, target differs (differs in: working directory)", text)
        self.assertFalse([line for line in text.splitlines() if CJK.search(line)])

    def test_one_fingerprint_split_into_two_target_classes_gives_two_groups_with_distinct_ids(self) -> None:
        def split(data: dict) -> None:
            for name, tc in (("notes-copy", "t-a1b2c3d4.2"), ("notes-twin", "t-a1b2c3d4.2")):
                data["mcps"].append({**self.COPY, "name": name, "target_class": tc})
            for entry in data["mcps"]:
                if entry["fingerprint"] == "a1b2c3d4e5f60718" and "target_class" not in entry:
                    entry["target_class"] = "t-a1b2c3d4.1"
            data["mcps"][1]["name"] = "notes-other"  # codex 的那份改名：.1 这一类里有两个不同的名字
        self.set_inventory(split)
        groups = [g for g in self.dups()["groups"] if g["class"] == "diff-name-same-fp"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(len({g["id"] for g in groups}), 2)

    def test_malformed_hints_are_ignored_and_an_unknown_schema_version_still_wins(self) -> None:
        self.set_inventory(lambda data: data.update(mcp_hints=["x", {"kind": "other", "names": ["a"]}, {"kind": self.HINT["kind"]}]))
        self.assertNotIn("hints", self.dups())
        self.set_inventory(lambda data: data.update(schema_version=7, mcp_hints=[self.HINT]))
        text = self.run_menu(self.provider(), ["9", "3"])[1]
        self.assertIn("不可用：inventory 的 schema_version 7", text)
        self.assertNotIn("MCP 提示", text)

    def test_demo_inventory_shows_the_hint_and_no_extra_group(self) -> None:
        text = DemoMenuTests().run_demo("study", ["9", "3"])
        self.assertIn("MCP 提示（不算重复，1 条）", text)
        self.assertIn("claude：docs-a、docs-b 指纹相同，目标不同（差异：工作目录）", text)
        self.assertIn("重复 MCP（3 组）", text)


class RealMenuEnglishTests(MenuCase):
    def test_real_menu_speaks_english_too(self) -> None:
        text = self.run_menu(self.provider(), ["9", "1", "", "2", "", "3", "1", "", "", "4", "d", "", "", "q"], lang="en")[1]
        self.assertFalse([line for line in text.splitlines() if CJK.search(line)])
        text = self.run_menu(self.provider(path=self.empty), ["9"], lang="en", path=self.empty)[1]
        self.assertFalse([line for line in text.splitlines() if CJK.search(line)])
        self.assertIn("agent-pack inventory not found", text)


class FunctionTests(unittest.TestCase):
    def test_reach_classification(self) -> None:
        data = ext.validate(json.loads((Path(__file__).resolve().parent / "fixtures" / "extensions_inventory.json").read_text(encoding="utf-8")))
        result = ext.by_project(ext.analyse(data["inventory"]))
        self.assertEqual([part["key"] for part in result["groups"]], ["project", "all", "app"])
        self.assertTrue(all(row["scope"] in ("project", "local") for row in result["groups"][0]["entries"]))

    def test_unknown_scope_is_unknown_not_guessed(self) -> None:
        analysis = ext.analyse({"skills": [{"name": "a", "client": "x", "scope": "weird", "evidence": "file"}], "mcps": []})
        self.assertEqual([part["key"] for part in ext.by_project(analysis)["groups"]], ["unknown"])


if __name__ == "__main__":
    unittest.main()
