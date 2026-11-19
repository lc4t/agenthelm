"""路由规则（opinion 规则指定「某类任务使用某个 skill」）的只读清单与机械检查。

清单来源：收件箱候选，以及 OPINION.md 自定义规则小节里本工具自己渲染的行（行末 ``[route …]`` 标记）。认不出的行标「未结构化」，
从不猜测。机械检查只比较标记里的字段与字节数，只警告、不阻断，也不检查 skill 是否存在（那需要 agenthelm inventory，属于下一版）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import versioned
from rule_lines import candidate_rule, custom_section_lines, parse_line, parse_marker

MAX_ROUTES = 6          # 每个项目的路由条数预算（设计文档第 6 节）
MAX_LINE_BYTES = 500    # 单条路由规则行的字节预算
LIVE_STATUSES = ("pending", "trial", "formal")  # held / rejected 不在生效或待裁决范围内
UNCHECKED = ("skill_exists", "semantic_overlap", "indirect_cycle", "skill_behavior_drift")


def line_bytes(line: str) -> int:
    return len(line.encode("utf-8"))


def route_entry(candidate: dict[str, Any], language: str) -> dict[str, Any]:
    route = candidate["route"]
    return {"source": "candidate", "ref": candidate["id"], "title": candidate["title"], "status": candidate["status"], "structure": "route",
            **route_fields(route), "bytes": line_bytes(candidate_rule(candidate, language))}


def route_fields(route: dict[str, Any]) -> dict[str, Any]:
    return {"skill": route["skill"], "when": route["when"], "final": bool(route.get("final")), "on": route.get("on") or ""}


def custom_entries(root: Path) -> list[dict[str, Any]]:
    path = root / "OPINION.md"
    try:
        content = versioned.read_text(path) if path.exists() else ""
    except (OSError, versioned.OpinionVersionError, UnicodeDecodeError):
        return []
    found = []
    for number, line in custom_section_lines(content):
        parsed = parse_line(line)
        base = {"source": "custom_line", "ref": f"OPINION.md:{number}", "line": number, "bytes": line_bytes(line)}
        if parsed is None:
            route, problem = parse_marker(line)
            if route or problem or "skill" in line.lower():
                found.append({**base, "structure": "unstructured", "text": " ".join(line.split())[:80]})
        elif parsed.get("route"):
            found.append({**base, "structure": "route", "title": parsed["title"], **route_fields(parsed["route"])})
        elif parsed.get("route_error"):
            found.append({**base, "structure": parsed["route_error"], "title": parsed["title"]})
    return found


def collect(root: Path, data: dict[str, Any], language: str) -> list[dict[str, Any]]:
    """候选在前（按创建时间），OPINION.md 的行在后（按行号）。已转正并写入的候选以 OPINION.md 的行为准，不重复计数。"""
    custom = custom_entries(root)
    written = {(item.get("title"), item.get("skill")) for item in custom if item["structure"] == "route"}
    entries = []
    for candidate in sorted(data["candidates"].values(), key=lambda item: (item["created_at"], item["id"])):
        if candidate["status"] in LIVE_STATUSES and candidate.get("route"):
            if candidate["status"] == "formal" and (candidate["title"], candidate["route"]["skill"]) in written:
                continue
            entries.append(route_entry(candidate, language))
    return entries + custom


def checks(entries: list[dict[str, Any]], say: Callable[..., str]) -> list[dict[str, Any]]:
    """机械检查，全部只警告。只比较标记字段与字节数，不判断语义。"""
    routed = [item for item in entries if item["structure"] == "route"]
    found: list[dict[str, Any]] = []

    def add(code: str, refs: list[str], **fields: Any) -> None:
        found.append({"code": code, "severity": "warn", "refs": refs, "detail": say(f"routes_check_{code}", **fields), "note": say("routes_check_note")})

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in routed:
        if item["on"]:
            groups.setdefault((item["on"], item["when"]), []).append(item)
    for (tag, when), members in sorted(groups.items()):
        skills = sorted({item["skill"] for item in members})
        if len(skills) > 1:
            add("same_tag_different_skill", [item["ref"] for item in members], on=tag, when=when, skills=say("sep").join(skills))
    same: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in routed:
        same.setdefault((item["skill"], item["on"], item["when"]), []).append(item)
    for (skill, tag, when), members in sorted(same.items()):
        if len(members) > 1:
            add("duplicate_route", [item["ref"] for item in members], skill=skill, on=tag or say("routes_no_tag"), when=when, count=len(members))
    finals = [item for item in routed if item["final"]]
    if len(finals) > 1:
        add("multiple_final", [item["ref"] for item in finals], count=len(finals))
    if len(routed) > MAX_ROUTES:
        add("too_many_routes", [item["ref"] for item in routed], count=len(routed), limit=MAX_ROUTES)
    for item in routed:
        if item["bytes"] > MAX_LINE_BYTES:
            add("line_too_long", [item["ref"]], bytes=item["bytes"], limit=MAX_LINE_BYTES)
    return found


def listing(root: Path, data: dict[str, Any], language: str, say: Callable[..., str]) -> dict[str, Any]:
    entries = collect(root, data, language)
    found = checks(entries, say)
    return {"ok": True, "status": "read-only", "routes": entries, "checks": found, "unchecked": list(UNCHECKED), "note": say("routes_check_note"),
            "summary": {"routes": sum(1 for item in entries if item["structure"] == "route"), "unstructured": sum(1 for item in entries if item["structure"] != "route"),
                        "warnings": len(found)}}


def entry_line(item: dict[str, Any], say: Callable[..., str]) -> str:
    if item["structure"] != "route":
        return say("routes_line_unstructured", ref=item["ref"], text=item.get("text") or item.get("title") or "")
    return say("routes_line", ref=item["ref"], title=item.get("title") or "", route=route_text(item, say), bytes=item["bytes"])


def route_text(route: dict[str, Any], say: Callable[..., str]) -> str:
    """skill X（时机，收尾，标签）：list / brief / present 共用的一段路由说明。"""
    extras = [say(f"route_when_{route['when']}")]
    if route.get("final"):
        extras.append(say("route_final"))
    if route.get("on"):
        extras.append(say("route_on", on=route["on"]))
    return say("route_text", skill=route["skill"], extras=say("brief_join_short").join(extras))


def markdown(result: dict[str, Any], say: Callable[..., str]) -> str:
    if not result["routes"]:
        return say("routes_empty") + "\n"
    lines = [entry_line(item, say) for item in result["routes"]]
    lines += [say("routes_check_line", detail=item["detail"], note=item["note"]) for item in result["checks"]]
    lines.append(say("routes_unchecked", items=", ".join(result["unchecked"])))
    return "\n".join(lines) + "\n"
