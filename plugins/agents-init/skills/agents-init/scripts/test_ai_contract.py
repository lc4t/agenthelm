"""AI 研判部分的契约测试（issue #25 第 5 项）。

分工：规则可确定的部分（按标题关键词的启发式分类）直接测；AI 的输出（逐段判断、选哪个策略）
CI 里没有 AI 接口，用**录制的响应**做结构校验。AI 的输出只能变成 init_project.py 的命令行参数，
所以契约有三条：

1. 录制响应的结构必须能由 ``scan`` 报告给出的选项/章节序号校验通过；
2. 把它交给确定性命令后，预演不写；``--apply`` 才落盘；没给策略就一律保留用户原文；
3. 结构不合法的响应被命令拒绝且不写任何东西，工具本身不含任何调用 AI / 联网的代码。

建议质量评测（AI 给的建议好不好）不在这里，应放在单独密钥的定时任务里，不挂 PR 门禁。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_fixtures as fx
from test_fixtures import adopt

HERE = Path(__file__).resolve().parent

AGENTS_TEXT = (
    "# 我的项目\n\n简介。\n\n## 常用命令\n\n- make test\n\n## 编码规则\n\n- 必须先跑测试\n\n"
    "## 回复偏好\n\n- 用中文\n\n## 发布流程\n\n1. 打 tag\n"
)

# 录制的 AI 响应：字段名与取值就是 SKILL.md 里写给 AI 的输出约定（逐段去向 + 策略），不含任何文件内容。
RECORDED_GOOD = {
    "agents_policy": "split",
    "claude_policy": "add-import",
    "routes": {"3": "rules", "4": "opinion", "5": "sop"},
    "register_roots": [],
}
RECORDED_BAD = {
    "unknown-destination": {"agents_policy": "split", "routes": {"3": "trash"}},
    "section-does-not-exist": {"agents_policy": "split", "routes": {"99": "rules"}},
    "unknown-policy": {"agents_policy": "overwrite"},
    "not-an-object-route": {"agents_policy": "split", "routes": ["3=rules"]},
}

# 启发式分类：（标题，期望去向）。中英文各取代表；没有命中的标题缺省留在 agents。
HEURISTIC_CASES = (
    ("常用命令", "agents"), ("Build and Test", "agents"), ("Architecture overview", "agents"),
    ("编码规则", "rules"), ("Code Style", "rules"), ("Security", "rules"), ("禁止事项", "rules"),
    ("回复偏好", "opinion"), ("Tone", "opinion"), ("沟通语言", "opinion"),
    ("发布流程", "sop"), ("Release checklist", "sop"), ("Deploy runbook", "sop"),
    ("我的项目", "agents"), ("随便写点什么", "agents"),
)


def validate_recorded_response(response: dict, report: dict) -> list[str]:
    """用 scan 报告校验一份 AI 响应的结构；返回问题列表（空 = 合法）。"""
    problems = []
    allowed = {"agents_policy", "claude_policy", "routes", "register_roots"}
    problems += [f"unknown key {key}" for key in response if key not in allowed]
    finding = {item["id"]: item for item in report["findings"]}
    agents_options = {o["id"] for o in finding["adopt:contract:AGENTS.md"]["options"]}
    claude_options = {o["id"] for o in finding["adopt:contract:CLAUDE.md"]["options"]}
    if response.get("agents_policy", "keep") not in agents_options:
        problems.append(f"agents_policy not offered: {response['agents_policy']}")
    if response.get("claude_policy", "keep") not in claude_options:
        problems.append(f"claude_policy not offered: {response['claude_policy']}")
    routes = response.get("routes", {})
    if not isinstance(routes, dict):
        problems.append("routes must be an object")
        routes = {}
    numbers = {str(s["index"]) for s in finding["adopt:contract:AGENTS.md"]["detail"]["sections"]}
    for number, destination in routes.items():
        if number not in numbers:
            problems.append(f"route for a section that does not exist: {number}")
        if destination not in adopt.ROUTE_DESTINATIONS:
            problems.append(f"route destination not allowed: {destination}")
    return problems


def to_flags(response: dict) -> list[str]:
    flags = ["--agents-policy", response.get("agents_policy", "keep"), "--claude-policy", response.get("claude_policy", "keep")]
    routes = response.get("routes", {})
    if isinstance(routes, dict):
        for number, destination in routes.items():
            flags += ["--route", f"{number}={destination}"]
    for root in response.get("register_roots", []):
        flags += ["--register-root", root]
    return flags


class HeuristicTests(unittest.TestCase):
    def test_title_heuristics_are_deterministic(self) -> None:
        for heading, expected in HEURISTIC_CASES:
            with self.subTest(heading=heading):
                self.assertEqual(adopt.classify(heading)[0], expected)
                self.assertEqual(adopt.classify(heading), adopt.classify(heading))

    def test_every_destination_a_heuristic_can_return_is_a_valid_route(self) -> None:
        for _heading, expected in HEURISTIC_CASES:
            self.assertIn(expected, adopt.ROUTE_DESTINATIONS)

    def test_scan_suggestions_say_which_keyword_matched(self) -> None:
        for section in adopt.sections(AGENTS_TEXT):
            if section.suggestion != "agents" or section.matched:
                self.assertTrue(section.matched, f"{section.heading} 的建议必须说明命中的关键词")


class AiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.base = Path(self._temporary.name).resolve()
        (self.base / "home").mkdir()
        self.root = self.base / "proj"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text(AGENTS_TEXT, encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("# mine\n", encoding="utf-8")
        self.ctx = fx.Ctx(self.base, self.root)
        self.state = ["--state-dir", str(self.base / "state")]

    def cli(self, mode: str, *extra: str):
        return fx.cli(self.root, mode, *extra, *self.state, home=self.base / "home")

    def scan(self) -> dict:
        code, report = self.cli("scan")
        self.assertEqual(code, 0, report)
        return report

    def test_the_recorded_good_response_passes_the_structure_check(self) -> None:
        self.assertEqual(validate_recorded_response(RECORDED_GOOD, self.scan()), [])

    def test_each_recorded_bad_response_fails_the_structure_check(self) -> None:
        report = self.scan()
        for name, response in RECORDED_BAD.items():
            with self.subTest(name=name):
                self.assertTrue(validate_recorded_response(response, report), f"{name} 应当校验失败")

    def test_scan_is_the_only_thing_the_ai_reads_and_it_writes_nothing(self) -> None:
        before = fx.tree(self.root)
        self.scan()
        self.assertEqual(fx.changed(before, fx.tree(self.root)), [])

    def test_the_response_only_reaches_disk_through_the_command_and_only_with_apply(self) -> None:
        flags = to_flags(RECORDED_GOOD)
        before = fx.tree(self.root)
        code, preview = self.cli("adopt", *flags)
        self.assertEqual(code, 0, preview)
        self.assertFalse(preview["applied"])
        self.assertEqual(fx.changed(before, fx.tree(self.root)), [], "没有 --apply 就不写")
        self.assertTrue(preview["needs_recovery_dir"] or preview["modify"], "会改用户文件的方案必须在预演里显式列出")
        self.assertIn("AGENTS.md", [item["path"] for item in preview["modify"]])

    def test_user_must_choose_before_a_user_file_changes(self) -> None:
        # 缺省策略 = keep：即使 AI 什么都没说，用户文件也一字节不动。
        before = fx.tree(self.root)
        code, applied = self.cli("adopt", "--apply")
        self.assertEqual(code, 0, applied)
        for key in ("0:AGENTS.md", "0:CLAUDE.md"):
            self.assertEqual(fx.tree(self.root)[key], before[key])
        # 选择了改写：先备份，原文进备份目录。
        recovery = self.base / "recovery"
        shutil_home = self.base / "home2"
        shutil_home.mkdir()
        other = self.base / "proj2"
        other.mkdir()
        (other / "AGENTS.md").write_text(AGENTS_TEXT, encoding="utf-8")
        code, changed = fx.cli(other, "adopt", "--apply", *to_flags({"agents_policy": "append"}), "--recovery-dir", str(recovery),
                               "--state-dir", str(self.base / "state2"), home=shutil_home)
        self.assertEqual(code, 0, changed)
        self.assertEqual((recovery / "AGENTS.md").read_text(encoding="utf-8"), AGENTS_TEXT)

    def test_invalid_responses_are_refused_by_the_command_and_write_nothing(self) -> None:
        for name, response in RECORDED_BAD.items():
            if name == "not-an-object-route":
                continue  # 形状错误在结构校验这一层就被挡下，根本不会变成命令行参数（见上一个用例）
            with self.subTest(name=name):
                before = fx.tree(self.root)
                code, result = self.cli("adopt", "--apply", *to_flags(response))
                self.assertNotEqual(code, 0, result)
                self.assertEqual(fx.changed(before, fx.tree(self.root)), [], "被拒绝的响应不得留下任何文件")

    def test_recorded_good_response_applies_and_loses_no_user_line(self) -> None:
        code, applied = self.cli("adopt", "--apply", *to_flags(RECORDED_GOOD), "--recovery-dir", str(self.base / "recovery"))
        self.assertEqual(code, 0, applied)
        everything = "".join(p.read_text(encoding="utf-8") for p in self.root.rglob("*.md"))
        for line in (l for l in AGENTS_TEXT.splitlines() if l.strip()):
            self.assertIn(line, everything, f"用户原文丢了一行：{line}")

    def test_the_tool_contains_no_code_that_calls_an_ai_or_the_network(self) -> None:
        forbidden = re.compile(r"^\s*(import|from)\s+(anthropic|openai|requests|urllib|http|socket|httpx|aiohttp)\b", re.M)
        for name in ("adopt.py", "scaffold.py", "init_project.py", "regions.py"):
            text = (HERE / name).read_text(encoding="utf-8")
            self.assertIsNone(forbidden.search(text), f"{name} 不得 import AI/网络库：写入必须是确定性的")


if __name__ == "__main__":
    unittest.main()
