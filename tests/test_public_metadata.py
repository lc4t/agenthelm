"""对外元数据英文优先：npm、市场、插件清单与 Skill 触发描述。

注册表、市场与客户端插件列表只显示这些字段；英文在前便于检索。为后续语言预留扩展：
文档每种语言一个文件（README.<语言>.md）并互相链接；只有一个字段可用的长描述与 Skill
触发描述用 “English … 中文：…” 分段，以后的语言按同样格式追加。Skill 描述是 YAML
frontmatter 的普通标量，不能含 ASCII 的 ": "，长度不超过 1024。
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CJK = re.compile(r"[　-〿一-鿿＀-￯]")


def read_json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


class PublicMetadataTests(unittest.TestCase):
    def assertEnglish(self, text: str, label: str) -> None:
        self.assertTrue(text and not CJK.search(text), f"{label} 应为英文：{text!r}")

    def assertEnglishFirst(self, text: str, label: str) -> None:
        self.assertRegex(text, r"^[A-Za-z]", f"{label} 应以英文开头")

    def test_npm_package_and_readme(self) -> None:
        self.assertEnglish(read_json("packages/cli/package.json")["description"], "npm description")
        package = ROOT / "packages/cli"
        readme = (package / "README.md").read_text(encoding="utf-8")
        switcher, body = readme.split("\n", 3)[2], readme.split("\n", 3)[3]
        self.assertFalse(CJK.search(body), "README.md 正文为英文；其他语言各自一个 README.<语言>.md")
        # 每种语言一个文件，互相链接：以后加语言只需加文件并补上切换行。
        for translated in sorted(package.glob("README.*.md")):
            self.assertIn(f"packages/cli/{translated.name}", switcher, translated.name)
            first_lines = translated.read_text(encoding="utf-8").split("\n", 3)[2]
            self.assertIn("packages/cli/README.md", first_lines, translated.name)
        self.assertTrue(list(package.glob("README.*.md")), "至少有一种其他语言")

    def test_llms_txt_is_english_with_a_language_list(self) -> None:
        text = (ROOT / "llms.txt").read_text(encoding="utf-8")
        body, _, languages = text.partition("\nOther languages:\n")
        self.assertFalse(CJK.search(body), "llms.txt 正文为英文")
        self.assertRegex(languages, r"^- .+ \(zh-CN\): https://", "其他语言按 “- 名称 (语言标签): 链接” 列出")

    def test_marketplace_and_plugin_manifests(self) -> None:
        marketplace = read_json(".claude-plugin/marketplace.json")
        self.assertEnglish(marketplace["metadata"]["description"], "marketplace description")
        for entry in marketplace["plugins"]:
            self.assertEnglish(entry["description"], f"marketplace {entry['name']}")
        for manifest in sorted((ROOT / "plugins").glob("*/plugin.json")):
            self.assertEnglish(json.loads(manifest.read_text(encoding="utf-8"))["description"], str(manifest))

    def test_codex_adapters(self) -> None:
        for adapter in sorted((ROOT / "plugins").glob("*/.codex-plugin/plugin.json")):
            data = json.loads(adapter.read_text(encoding="utf-8"))
            interface = data["interface"]
            for key in ("shortDescription", "defaultPrompt"):
                self.assertEnglish(interface[key], f"{adapter} {key}")
            self.assertEnglish(data["description"], f"{adapter} description")
            self.assertEnglishFirst(interface["longDescription"], f"{adapter} longDescription")

    def test_skill_descriptions_are_english_then_chinese(self) -> None:
        for skill in sorted((ROOT / "plugins").glob("*/skills/*/SKILL.md")):
            front = skill.read_text(encoding="utf-8").split("---\n")[1]
            value = re.search(r"^description: (.*)$", front, re.M).group(1)
            self.assertEnglishFirst(value, str(skill))
            english, _, chinese = value.partition(" 中文：")
            self.assertEnglish(english, f"{skill} 英文部分")
            self.assertTrue(CJK.search(chinese), f"{skill} 缺中文部分")
            self.assertNotIn(": ", value, f"{skill}：YAML 普通标量里不能有 ': '")
            self.assertLessEqual(len(value), 1024, str(skill))

    def test_codex_skill_interfaces(self) -> None:
        for path in sorted((ROOT / "plugins").glob("*/skills/*/agents/openai.yaml")):
            text = path.read_text(encoding="utf-8")
            for key in ("short_description", "default_prompt"):
                self.assertEnglish(re.search(rf'{key}: "(.*)"', text).group(1), f"{path} {key}")


if __name__ == "__main__":
    unittest.main()
