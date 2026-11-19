"""项目文件里的规则行：渲染（采纳写入）与解析（check / 机械关系提示读取 OPINION.md 的自定义规则）。

行的标签随项目文件语言，不随界面语言。解析只认本工具自己的两种渲染格式；认不出的行一律报告为「未结构化」，从不猜测。
"""

from __future__ import annotations

import re
from typing import Any


# 用户要求全局时必须使用的固定标准值（并带非空 --quote）；工具只做形式校验，无法验证是谁提出了全局。
GLOBAL_VALUES = {"zh": "全部场景，用户要求全局", "en": "All scenarios; the user asked for this to apply globally"}


def is_global(value: str) -> bool:
    return value in GLOBAL_VALUES.values()


def localized_global(value: str, language: str) -> str:
    """写进项目文件时把标准全局值换成项目语言的版本，避免 en 项目里出现中文。"""
    return GLOBAL_VALUES[language] if is_global(value) else value


# 自定义行的标签随项目文件语言（不随界面语言）。没有新字段的候选，zh 渲染与旧版逐字节相同。
RULE_WORDS = {
    "zh": {"applies": "适用", "trigger": "条件", "purpose": "目的", "exclude": "不适用", "sep": "、", "join": "；", "colon": "：", "open": "（", "close": "）"},
    "en": {"applies": "Applies to", "trigger": "When", "purpose": "Purpose", "exclude": "Not for", "sep": ", ", "join": "; ", "colon": ": ", "open": " (", "close": ")"},
}


def rule_qualifiers(candidate: dict[str, Any], language: str) -> str:
    """括号内的内容：适用范围，随后是条件 / 目的 / 不适用（有才写）。"""
    words = RULE_WORDS[language]
    parts = [f"{words['applies']}{words['colon']}{words['sep'].join(candidate['scopes'])}"]
    for name in ("trigger", "purpose", "exclude"):
        value = candidate.get(name) or ""
        if value:
            parts.append(f"{words[name]}{words['colon']}{localized_global(value, language) if name == 'trigger' else value}")
    return words["join"].join(parts)


def candidate_rule(candidate: dict[str, Any], language: str = "zh") -> str:
    words = RULE_WORDS[language]
    return f"- **{candidate['title']}**{words['open']}{rule_qualifiers(candidate, language)}{words['close']}{words['colon']}{candidate['text']}"


CUSTOM_HEADINGS = ("## 用户自定义规则", "## User-defined rules")
LINE_PATTERNS = {
    "zh": re.compile(r"^- \*\*(?P<title>.+?)\*\*（(?P<qual>.*?)）：(?P<text>.*)$"),
    "en": re.compile(r"^- \*\*(?P<title>.+?)\*\* \((?P<qual>.*?)\): (?P<text>.*)$"),
}


def custom_section_lines(content: str) -> list[tuple[int, str]]:
    """OPINION.md 自定义规则小节里的非空行及其行号（从 1 起）；没有该小节时为空。"""
    lines = content.splitlines()
    for start, line in enumerate(lines):
        if line.strip() in CUSTOM_HEADINGS:
            found = []
            for number in range(start + 1, len(lines)):
                if lines[number].startswith("## "):
                    break
                if lines[number].strip():
                    found.append((number + 1, lines[number]))
            return found
    return []


def parse_line(line: str) -> dict[str, Any] | None:
    """按两种渲染格式解析一行；不是本工具渲染的格式返回 None（调用方标「未结构化」）。"""
    for language, pattern in LINE_PATTERNS.items():
        matched = pattern.match(line)
        if not matched:
            continue
        words = RULE_WORDS[language]
        colon = words["colon"].strip()
        labels = {words[name]: name for name in ("applies", "trigger", "purpose", "exclude")}
        # 只在后面紧跟「已知标签 + 冒号」处切分，值里自带的分号不会被切开
        marker = re.compile("%s(?=(?:%s)%s)" % (re.escape(words["join"]), "|".join(re.escape(label) for label in labels if label != words["applies"]), re.escape(colon)))
        values: dict[str, str] = {}
        for part in marker.split(matched["qual"]):
            label, found, value = part.partition(colon)
            name = labels.get(label.strip())
            if not found or name is None or name in values or not value.strip():
                values = {}
                break
            values[name] = value.strip()
        if "applies" in values:
            separator = words["sep"].strip()
            return {"format": language, "title": matched["title"], "scopes": [item.strip() for item in values["applies"].split(separator) if item.strip()],
                    "trigger": values.get("trigger", ""), "purpose": values.get("purpose", ""), "exclude": values.get("exclude", ""), "text": matched["text"]}
    return None
