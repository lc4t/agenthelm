"""端到端：用真实的 ``agenthelm inventory``（不是回放夹具）驱动 ``agenthelm project extensions`` 与 health。

临时 HOME 里放：同内容与不同内容的重复技能、同名的 MCP（Claude 的 local / project / user 与 Codex）、带假令牌的内联密钥，
以及一个有用户信息与 headers 的远程 MCP。验证真实输出的形状与菜单侧的假设一致，且任何输出里都没有假令牌。
"""
from __future__ import annotations

import os

os.environ["AGENTHELM_LANG"] = "zh"  # these tests assert the Chinese messages; tests/test_i18n*.py cover English

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AGENTHELM = ROOT / "plugins/agent-pack/runtime/agenthelm.py"
FAKE_SECRET = "FAKE_SECRET_VALUE_0001"


def skill(root: Path, name: str, description: str, body: str, extra: str = "") -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}\n", encoding="utf-8")


class RealInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        base = Path(cls.directory.name).resolve()
        cls.home, cls.project = base / "home", base / "project"
        skill(cls.home / ".claude/skills", "dup-same", "Same content skill", "body")
        skill(cls.project / ".claude/skills", "dup-same", "Same content skill", "body")
        skill(cls.home / ".claude/skills", "dup-diff", "User copy", "body one")
        skill(cls.project / ".claude/skills", "dup-diff", "Project copy", "body two")
        skill(cls.home / ".claude/skills", "solo", "Solo skill", "x", "when_to_use: Use when testing\n")
        profile = cls.project / ".agents/moe.sakanano.agent-pack/project.json"
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({"schema_version": "2.0", "language": "en"}), encoding="utf-8")
        (cls.home / ".codex").mkdir(parents=True)
        (cls.home / ".claude.json").write_text(json.dumps({
            "mcpServers": {
                "dupm": {"command": "srv", "args": ["--token", FAKE_SECRET], "env": {"API_KEY": FAKE_SECRET}},
                "remote": {"type": "http", "url": f"https://user:{FAKE_SECRET}@docs.example.com/mcp?token={FAKE_SECRET}",
                           "headers": {"Authorization": f"Bearer {FAKE_SECRET}"}}},
            "projects": {str(cls.project): {"mcpServers": {"dupm": {"command": "srv", "args": ["--local"]}}}}}), encoding="utf-8")
        (cls.project / ".mcp.json").write_text(json.dumps({"mcpServers": {"dupm": {"command": "srv", "args": ["--token", "${TOK}"]}}}),
                                               encoding="utf-8")
        (cls.home / ".codex/config.toml").write_text('[mcp_servers.dupm]\ncommand = "srv"\nargs = ["--token", "${TOK}"]\n', encoding="utf-8")
        cls.env = {**{key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "AGENTHELM_", "CLAUDE", "CODEX"))},
                   "HOME": str(cls.home), "AGENTHELM_LANG": "zh", "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"}

    def run_project(self, *args: str, lang: str = "zh") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(AGENTHELM), "project", "--project", str(self.project), "--no-input", *args],
                              input="", capture_output=True, text=True, env={**self.env, "AGENTHELM_LANG": lang}, timeout=120, check=False)

    def run_json(self, *args: str) -> dict:
        result = self.run_project(*args, "--output", "json")
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return json.loads(result.stdout)

    def raw_inventory(self) -> dict:
        result = subprocess.run([sys.executable, str(AGENTHELM), "inventory", "--output", "json", "--client", "all", "--project", str(self.project)],
                                capture_output=True, text=True, env=self.env, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_the_real_inventory_has_the_shape_the_menu_assumes(self) -> None:
        data = self.raw_inventory()
        self.assertEqual(data["schema_version"], 1)
        skills = {entry["ref"]: entry for entry in data["skills"]}
        mcps = {entry["ref"]: entry for entry in data["mcps"]}
        self.assertEqual(sorted(skills), ["skill:dup-diff@claude:project", "skill:dup-diff@claude:user", "skill:dup-same@claude:project",
                                          "skill:dup-same@claude:user", "skill:solo@claude:user"])
        self.assertEqual(sorted(mcps), ["mcp:dupm@claude:local", "mcp:dupm@claude:project", "mcp:dupm@claude:user",
                                        "mcp:dupm@codex:user", "mcp:remote@claude:user"])
        for entry in [*skills.values(), *mcps.values()]:
            self.assertEqual(entry["loaded"], "unknown", "loaded 恒为字符串 unknown")
            self.assertIn(entry["evidence"], ("file", "unknown"))
        self.assertEqual(skills["skill:dup-same@claude:user"]["precedence"], "winner")
        shadowed = skills["skill:dup-same@claude:project"]
        self.assertEqual(shadowed["precedence"], "shadowed")
        self.assertEqual(shadowed["shadowed_by"]["rule"], "personal-over-project")
        self.assertIsInstance(skills["skill:solo@claude:user"]["origin"], dict)
        self.assertEqual(skills["skill:solo@claude:user"]["when_to_use_chars"], len("Use when testing"))
        self.assertIs(mcps["mcp:dupm@claude:user"]["has_inline_secret"], True)
        self.assertIs(mcps["mcp:dupm@claude:project"]["has_inline_secret"], False)
        self.assertEqual(mcps["mcp:remote@claude:user"]["url_host"], "docs.example.com")
        self.assertEqual(mcps["mcp:dupm@claude:local"]["precedence"], "winner")
        self.assertEqual(mcps["mcp:dupm@claude:user"]["shadowed_by"]["rule"], "local-over-project-over-user")
        self.assertNotIn(FAKE_SECRET, json.dumps(data))
        self.assertIn({"store": "desktop-config", "client": "claude-desktop", "reason": "not-found"}, data["unknown"])

    def test_list_counts_and_does_not_count_a_missing_desktop_config_as_unknown(self) -> None:
        payload = self.run_json("extensions", "list")
        summary = payload["data"]["summary"]
        self.assertEqual((payload["exit_code"], summary["skills"], summary["mcps"]), (0, 5, 5))
        self.assertEqual(summary["duplicate_groups"], {"total": 3, "mcp": 1, "skill": 2})
        self.assertEqual(summary["unknown"], 0, "desktop-config not-found 是没有这个存储，不是未知项")

    def test_dups_follow_the_inventory_conclusions(self) -> None:
        groups = {group["name"]: group for group in self.run_json("extensions", "dups")["data"]["groups"]}
        self.assertEqual(sorted(groups), ["dup-diff", "dup-same", "dupm"])
        self.assertEqual(groups["dup-same"]["class"], "same-name-same")
        self.assertEqual(groups["dup-diff"]["class"], "same-name-differs")
        self.assertEqual(groups["dupm"]["class"], "same-name-differs")
        for group in groups.values():
            self.assertTrue(group["effective"]["known"], group)
        self.assertIn("mcp:dupm@claude:local", groups["dupm"]["effective"]["lines"][0])
        self.assertIn("local-over-project-over-user", groups["dupm"]["effective"]["lines"][0])
        self.assertFalse(groups["dupm"]["cross_client"], "Claude 内部有多份，不是纯跨客户端")

    def test_check_counts_only_what_loads_and_adds_when_to_use(self) -> None:
        cost = {row["client"]: row for row in self.run_json("extensions", "check")["data"]["cost"]}
        # user copies load (dup-same 18, dup-diff 9, solo 4 + when_to_use 16); both project copies are shadowed.
        self.assertEqual((cost["claude"]["counted"], cost["claude"]["uncounted"]), (3, 2))
        self.assertEqual(cost["claude"]["resident_bytes"], len("Same content skill") + len("User copy") + len("Solo skill") + len("Use when testing"))

    def test_inline_secret_is_a_flag_and_no_secret_is_printed_anywhere(self) -> None:
        entry = self.run_json("extensions", "show", "mcp:dupm@claude:user")["data"]["entry"]
        self.assertIs(entry["has_inline_secret"], True)
        for command in (("list",), ("dups",), ("check", "--include-description"), ("show", "remote"), ("show", "mcp:dupm@claude:user")):
            for output in ("text", "json"):
                result = self.run_project("extensions", *command, "--output", output)
                combined = result.stdout + result.stderr
                for needle in (FAKE_SECRET, "Bearer", "user:"):
                    self.assertNotIn(needle, combined, f"{command} {output}")

    def test_health_has_an_info_level_finding_and_the_exit_code_does_not_change(self) -> None:
        payload = self.run_json("health", "--offline")
        finding = next(item for item in payload["data"]["findings"] if item["id"] == "extensions-duplicates")
        self.assertEqual(finding["level"], "info")
        self.assertIn("3 组", finding["title"])
        self.assertRegex(finding["next"][0]["action"], r"^ext:[ms]-[0-9a-f]{6}$")
        self.assertFalse(any(item["level"] == "must" and item["id"] == "extensions-duplicates" for item in payload["data"]["findings"]))

    def test_english_wording_does_not_depend_on_the_count(self) -> None:
        text = self.run_project("extensions", "list", lang="en").stdout + self.run_project("extensions", "dups", lang="en").stdout
        self.assertIn("Duplicate groups: 3", text)
        self.assertIn("Duplicate MCP servers (groups: 1)", text)
        self.assertNotIn("1 groups", text)
        self.assertIn("Skills: 5 · MCP servers: 5", text)

    def test_capability_is_available_through_the_host_agenthelm(self) -> None:
        item = next(item for item in self.run_json("capabilities")["data"] if item["id"] == "extensions")
        self.assertEqual((item["state"], item["reason"]), ("available", ""))


if __name__ == "__main__":
    unittest.main()
