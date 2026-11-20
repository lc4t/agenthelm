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


# 路由（opinion 规则指定某类任务使用某个 skill）：标记是纯 ASCII，与项目语言无关；自动约束的文字随项目文件语言。
ROUTE_WHEN = ("before", "after", "instead")
SKILL_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")  # Agent Skills 规范：小写字母、数字、连字符
TAG_NAME = re.compile(r"^[a-z0-9-]{1,32}$")
MAX_SKILL = 64
MARKER = re.compile(r"^(?P<head>.*) \[route (?P<body>[^\[\]]*)\]$")
ROUTE_WORDS = {
    "zh": {
        "exclude": "skill {skill} 自身的执行过程",
        "before": "开始主体工作前先使用 skill {skill}",
        "after": "主体完成后、交付前使用 skill {skill} 处理成果",
        "final": "主体完成后、交付前最后一步使用 skill {skill} 处理成果，其后不再改动该成果的文字",
        "instead": "用 skill {skill} 取代默认做法完成本项工作",
        "suffix": " 路由：{timing}；该 skill 执行期间不再触发本规则；该 skill 不可用时告知用户并按目的自行完成。",
    },
    "en": {
        "exclude": "the execution of skill {skill} itself",
        "before": "before starting the main work, use skill {skill}",
        "after": "after the main work and before delivery, use skill {skill} on the result",
        "final": "as the last step before delivery, use skill {skill} on the result; do not change its wording afterwards",
        "instead": "use skill {skill} instead of the default approach for this work",
        "suffix": " Routing: {timing}; this rule does not trigger again while that skill runs; if it is unavailable, tell the user and finish the work yourself per the purpose.",
    },
}


def route_problem(route: dict[str, Any]) -> str | None:
    """路由对象的形式校验：返回第一个问题的代码（skill / when / final / on），没有问题为 None。"""
    skill = route.get("skill")
    if not isinstance(skill, str) or len(skill) > MAX_SKILL or not SKILL_NAME.match(skill):
        return "skill"
    if route.get("when") not in ROUTE_WHEN:
        return "when"
    if route.get("final") and route["when"] != "after":
        return "final"
    on = route.get("on") or ""
    if on and (not isinstance(on, str) or not TAG_NAME.match(on)):
        return "on"
    return None


def route_marker(route: dict[str, Any]) -> str:
    """行末的机器可读标记，固定键序；final / on 缺省则省略。"""
    parts = [f"skill={route['skill']}", f"when={route['when']}"]
    if route.get("final"):
        parts.append("final=1")
    if route.get("on"):
        parts.append(f"on={route['on']}")
    return "[route " + " ".join(parts) + "]"


def route_exclude(route: dict[str, Any], language: str) -> str:
    return ROUTE_WORDS[language]["exclude"].format(skill=route["skill"])


def route_suffix(route: dict[str, Any], language: str) -> str:
    words = ROUTE_WORDS[language]
    timing = words["final" if route.get("final") else route["when"]].format(skill=route["skill"])
    return words["suffix"].format(timing=timing)


def parse_marker(line: str) -> tuple[dict[str, Any] | None, str | None]:
    """行末标记 -> (路由, 错误)。没有标记为 (None, None)；标记损坏为 (None, 原因)。只认固定的四个键。"""
    matched = MARKER.match(line.rstrip())
    if not matched:
        return None, ("marker_damaged" if re.search(r"\[route\b", line) else None)
    route: dict[str, Any] = {"final": False, "on": ""}
    seen: set[str] = set()
    for token in matched["body"].split(" "):
        key, found, value = token.partition("=")
        if not found or key not in ("skill", "when", "final", "on") or key in seen or not value:
            return None, "marker_damaged"
        seen.add(key)
        if key == "final":
            if value != "1":
                return None, "marker_damaged"
            route["final"] = True
        else:
            route[key] = value
    if not {"skill", "when"} <= seen or route_problem(route):
        return None, "marker_damaged"
    return route, None


# 标签（自定义规则按标签按需加载）：行末的纯 ASCII 标记 ` [tags a b]`，标签按字母序、不重复；与项目语言无关。
# 同时有路由时顺序固定为 `[route …] [tags …]`。没有标记的行与手写行永远常驻。
TAGS_MARKER = re.compile(r"^(?P<head>.*) \[tags (?P<body>[^\[\]]*)\]$")
MAX_TAG = 32


def tags_marker(tags: list[str]) -> str:
    return "[tags " + " ".join(sorted(set(tags))) + "]"


def tag_problem(tags: Any) -> str | None:
    """标签列表的形式校验：返回第一个问题的代码（empty / form），没有问题为 None。"""
    if not isinstance(tags, list) or not tags:
        return "empty"
    if len(tags) != len(set(tags)) or any(not isinstance(tag, str) or len(tag) > MAX_TAG or not SKILL_NAME.match(tag) for tag in tags):
        return "form"
    return None


def split_tags(line: str) -> tuple[list[str] | None, str | None, str]:
    """行末标签标记 -> (标签, 错误, 去掉标记后的行)。没有标记为 (None, None, 原行)；标记损坏为 (None, 原因, 原行)。"""
    matched = TAGS_MARKER.match(line.rstrip())
    if not matched:
        return None, ("marker_damaged" if re.search(r"\[tags\b", line) else None), line
    tags = matched["body"].split(" ")
    if tag_problem(tags):
        return None, "marker_damaged", line
    return tags, None, matched["head"]


def candidate_rule(candidate: dict[str, Any], language: str = "zh") -> str:
    line = _candidate_rule(candidate, language)
    return f"{line} {tags_marker(candidate['tags'])}" if candidate.get("tags") else line


def _candidate_rule(candidate: dict[str, Any], language: str = "zh") -> str:
    words = RULE_WORDS[language]
    route = candidate.get("route")
    if not route:
        return f"- **{candidate['title']}**{words['open']}{rule_qualifiers(candidate, language)}{words['close']}{words['colon']}{candidate['text']}"
    # 路由行：不适用里自动追加「skill 自身的执行过程」，正文后追加时机句、不再触发与降级条款，行末带标记
    user_exclude = candidate.get("exclude") or ""
    exclude = words["sep"].join(part for part in (user_exclude, route_exclude(route, language)) if part)
    shown = {**candidate, "exclude": exclude}
    return (f"- **{candidate['title']}**{words['open']}{rule_qualifiers(shown, language)}{words['close']}{words['colon']}"
            f"{candidate['text']}{route_suffix(route, language)} {route_marker(route)}")


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
    """按两种渲染格式解析一行；不是本工具渲染的格式返回 None（调用方标「未结构化」）。
    行末的标签标记先剥离；没有标记的行结果与旧版逐字节相同（不含 tags 键）。"""
    tags, problem, body = split_tags(line)
    parsed = _parse_line(body)
    if parsed is not None:
        if tags:
            parsed["tags"] = tags
        elif problem:
            parsed["tags_error"] = problem
    return parsed


def _parse_line(line: str) -> dict[str, Any] | None:
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
            parsed = {"format": language, "title": matched["title"], "scopes": [item.strip() for item in values["applies"].split(separator) if item.strip()],
                      "trigger": values.get("trigger", ""), "purpose": values.get("purpose", ""), "exclude": values.get("exclude", ""), "text": matched["text"]}
            route, problem = parse_marker(matched["text"])
            if route:
                parsed["route"] = route
                # 按同一渲染规则反向剥离自动追加的部分，往返还原用户写的正文与不适用
                tail = route_suffix(route, language) + f" {route_marker(route)}"
                if parsed["text"].endswith(tail):
                    parsed["text"] = parsed["text"][:-len(tail)]
                automatic = route_exclude(route, language)
                if parsed["exclude"] == automatic:
                    parsed["exclude"] = ""
                elif parsed["exclude"].endswith(words["sep"] + automatic):
                    parsed["exclude"] = parsed["exclude"][:-len(words["sep"] + automatic)]
            elif problem:
                parsed["route_error"] = problem
            return parsed
    return None
