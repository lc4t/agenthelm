"""候选与现有规则之间的机械关系提示：只比较文本，不做语义判断，永不阻断。

比较对象：其他候选（不含已丢弃）、OPINION.md 的自定义规则行（按本工具自己的渲染格式解析）、Profile 里已选的公开规则文本。
三种提示：规范化文本相同（same_text）、字符二元组重合度（text_overlap）、条件词重合（condition_overlap）。阈值保守，
由固定样例测试守住。冲突、合并、放大缩小、拆分这类语义判断由 Agent 做，写入候选的 relation 备注。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import versioned
from rule_lines import custom_section_lines, is_global, parse_line

TEXT_OVERLAP = 0.5      # 正文字符二元组 Jaccard 重合度下限
CONDITION_OVERLAP = 0.4  # 触发条件字符二元组 Jaccard 重合度下限
MIN_TEXT_CHARS, MIN_CONDITION_CHARS = 6, 4  # 规范化后太短的文本不比较，避免短词误报
MAX_RELATIONS = 5
EVIDENCE_CHARS = 80
ORDER = {"same_text": 0, "text_overlap": 1, "condition_overlap": 2}


def squash(text: str) -> str:
    """规范化：小写并去掉空白与标点。"""
    return re.sub(r"[\W_]+", "", (text or "").lower())


def bigrams(text: str) -> set[str]:
    return {text[index:index + 2] for index in range(len(text) - 1)}


def overlap(left: str, right: str) -> float:
    one, other = bigrams(squash(left)), bigrams(squash(right))
    return len(one & other) / len(one | other) if one and other else 0.0


def snippet(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= EVIDENCE_CHARS else text[:EVIDENCE_CHARS] + "…"


def plain(value: Any) -> str:
    """规则字段可能是字符串，也可能是 {zh, en}（PR4）；机械比对取字符串，本地化字段取其全部文字。"""
    if isinstance(value, dict):
        return " ".join(str(item) for item in value.values() if isinstance(item, str))
    return value if isinstance(value, str) else ""


def custom_targets(root: Path) -> list[dict[str, str]]:
    path = root / "OPINION.md"
    try:
        content = versioned.read_text(path) if path.exists() else ""
    except (OSError, versioned.OpinionVersionError, UnicodeDecodeError):
        return []
    found = []
    for number, line in custom_section_lines(content):
        parsed = parse_line(line)
        found.append({"type": "custom_line", "ref": f"OPINION.md:{number}", "title": parsed["title"] if parsed else "",
                      "text": parsed["text"] if parsed else line.lstrip("- ").strip(), "trigger": parsed["trigger"] if parsed else ""})
    return found


def public_targets(root: Path) -> list[dict[str, str]]:
    """Profile 里已选的公开规则（锁里的有效规则）；没有锁或读不了时为空。"""
    try:
        profile = versioned.read_object(root / "opinion.lock.json")["profile"]
        rules = versioned.effective_rules(profile).values()
        return [{"type": "public_rule", "ref": f"{rule['id']}@{rule['version']}", "title": plain(rule["title"]),
                 "text": plain(rule["text"]), "trigger": plain(rule.get("trigger", ""))} for rule in rules]
    except (OSError, ValueError, KeyError, TypeError, versioned.OpinionVersionError):
        return []


def targets(root: Path, data: dict[str, Any]) -> list[dict[str, str]]:
    """一次性准备全部比较对象；candidate 的 ref 为候选编号。"""
    others = [{"type": "candidate", "ref": item["id"], "title": item["title"], "text": item["text"], "trigger": item.get("trigger") or ""}
              for item in data["candidates"].values() if item["status"] != "rejected"]
    return others + custom_targets(root) + public_targets(root)


def relations_for(candidate: dict[str, Any], pool: list[dict[str, str]]) -> list[dict[str, Any]]:
    """candidate 与 pool 的机械提示，最相关的在前，最多 MAX_RELATIONS 条；没有则为空列表。"""
    text, trigger = squash(candidate["text"]), candidate.get("trigger") or ""
    found = []
    for other in pool:
        if other["type"] == "candidate" and other["ref"] == candidate["id"]:
            continue
        other_text = squash(other["text"])
        kind, score = None, 0.0
        if text and (text == other_text or squash(candidate["title"] + candidate["text"]) == squash(other["title"] + other["text"])):
            kind, score = "same_text", 1.0
        elif len(text) >= MIN_TEXT_CHARS and len(other_text) >= MIN_TEXT_CHARS and (score := overlap(text, other_text)) >= TEXT_OVERLAP:
            kind = "text_overlap"
        elif (trigger and other["trigger"] and not is_global(trigger) and not is_global(other["trigger"])
              and len(squash(trigger)) >= MIN_CONDITION_CHARS and len(squash(other["trigger"])) >= MIN_CONDITION_CHARS
              and (score := overlap(trigger, other["trigger"])) >= CONDITION_OVERLAP):
            kind = "condition_overlap"
        if kind:
            found.append({"kind": kind, "with": {"type": other["type"], "ref": other["ref"]}, "title": other["title"],
                          "score": round(score, 2), "evidence": snippet(other["trigger"] if kind == "condition_overlap" else other["text"])})
    found.sort(key=lambda item: (ORDER[item["kind"]], -item["score"], item["with"]["ref"]))
    return found[:MAX_RELATIONS]
