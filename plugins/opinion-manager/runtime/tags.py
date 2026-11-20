"""schema 2.2 的标签选择引擎：按 Profile 的标签声明只读切分 OPINION.md 的规则，不写任何文件。

标签是多对多的成员关系：一条规则可属于多个标签；规则组就是具名的标签集合。选择语义：
多个 --tag 取并集并去重；常驻规则（loading.always，以及没有任何标签的规则）只由 core 输出；常驻与按需冲突时归常驻。
用户 custom 段里带 ``[tags …]`` 标记且标签全部有效的行按标签按需加载，其余行（没有标记、认不出）永远常驻。
"""

from __future__ import annotations

import argparse
from typing import Any

import rule_lines
import versioned
from i18n_opinion import _  # noqa: E402


EXIT_OK, EXIT_MISSING = 0, 1
SIGNAL_KEY = "touches"
NONE = "none"


def is_tag_profile(profile: dict[str, Any]) -> bool:
    return isinstance(profile.get("loading"), dict) and "tags" in profile["loading"]


def split_custom(profile: dict[str, Any]) -> tuple[list[tuple[str, tuple[str, ...]]], int]:
    """custom 段逐行归类：返回 [(行, 标签元组)] 与「标记存在但无效、因此保持常驻」的行数。标签元组为空表示常驻。"""
    known = {tag["id"] for tag in profile["loading"]["tags"]}
    lines: list[tuple[str, tuple[str, ...]]] = []
    rejected = 0
    for line in (profile["custom"] or "").split("\n"):
        parsed = rule_lines.parse_line(line) if line.strip() else None
        tags = tuple((parsed or {}).get("tags") or ())
        if tags and not set(tags) <= known:
            tags, rejected = (), rejected + 1
        elif parsed and parsed.get("tags_error"):
            rejected += 1
        lines.append((line, tags))
    return lines, rejected


def plan(profile: dict[str, Any]) -> dict[str, Any]:
    """每条有效规则恰好归一处：常驻，或按需（属于至少一个标签且不在 always 里）。"""
    loading = profile["loading"]
    rules = versioned.effective_rules(profile)
    always = set(loading["always"])
    members = loading["members"]
    on_demand = {identity: members[identity] for identity in rules if identity in members and identity not in always}
    resident = [identity for identity in rules if identity not in on_demand]
    lines, rejected = split_custom(profile)
    tags: dict[str, dict[str, Any]] = {}
    for tag in loading["tags"]:
        name = tag["id"]
        tagged = [identity for identity in rules if name in members.get(identity, ())]
        own = [identity for identity in on_demand if name in on_demand[identity]]
        custom = [line for line, owners in lines if name in owners]
        tags[name] = {"tag": tag, "rules": own, "custom": custom,
                      "all_members": tagged, "state": ("ready" if own or custom else ("resident" if tagged else "empty"))}
    return {"rules": rules, "resident": resident, "tags": tags, "lines": lines, "rejected": rejected}


def selectable(parts: dict[str, Any]) -> list[str]:
    return [name for name, item in parts["tags"].items() if item["state"] == "ready"]


def resident_custom(parts: dict[str, Any]) -> str:
    """去掉按需行后的 custom 段；连续空行折叠为一行。"""
    kept: list[str] = []
    for line, owners in parts["lines"]:
        if owners or (not line.strip() and (not kept or not kept[-1].strip())):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def listing(names: list[str]) -> str:
    return _("、").join(names) if names else "-"


def custom_block(text: str, language: str) -> list[str]:
    return ["", versioned.FRAME[language]["custom"], "", text] if text else []


def tag_index_line(profile: dict[str, Any], parts: dict[str, Any], name: str, language: str) -> str:
    item = parts["tags"][name]
    tag = item["tag"]
    return _("- `{0}`（{1}，{2} 条）：{3}。不适用：{4}。").format(
        name, versioned.pick(tag["name"], language)[0], len(item["rules"]) + len(item["custom"]),
        versioned.pick(tag["trigger"], language)[0], versioned.pick(tag["exclude"], language)[0])


def core_text(profile: dict[str, Any], lock: dict[str, Any] | None, language: str = "zh", *, index: bool = True) -> str:
    parts = plan(profile)
    origin = _("正文指纹：`{0}`").format(lock["opinion_sha256"]) if lock else _("候选 Profile 预览，尚未生效")
    lines = [_("# Agent Opinion 核心规则"), "",
             _("Profile：`{0}@{1}`；{2}。").format(profile["id"], profile["version"], origin),
             _("本输出是完整正文的只读切片，始终适用。按需规则见文末标签索引，命中时再读取。")]
    for identity in parts["resident"]:
        lines.extend(("", versioned.render_rule(profile, parts["rules"][identity], language)))
    lines.extend(custom_block(resident_custom(parts), language))
    if index:
        lines.extend(("", _("## 标签索引"), "", _("任务命中标签的触发条件时用 `context select --tag <标签>` 读取对应规则；可重复 --tag，取并集；判断不清时一并读取。")))
        lines.extend(tag_index_line(profile, parts, name, language) for name in selectable(parts))
    return "\n".join(lines).rstrip() + "\n"


def check_selection(parts: dict[str, Any], names: list[str]) -> list[str]:
    """校验 --tag：未知、core、常驻、空集都给出明确错误；通过则返回去重后的标签（保持输入顺序）。"""
    valid = selectable(parts)
    if not names:
        versioned.fail(_("select 需要至少一个 --tag；有效标签：{0}").format(listing(valid)))
    chosen: list[str] = []
    for name in names:
        if name == "core":
            versioned.fail(_("core 是常驻集合，由 context core 输出，不能作为 --tag 选择"))
        if name not in parts["tags"]:
            versioned.fail(_("未知标签：{0}；有效标签：{1}").format(name, listing(valid)))
        state = parts["tags"][name]["state"]
        if state == "resident":
            versioned.fail(_("标签 {0} 的规则全部常驻，已随 context core 输出，无需选择").format(name))
        if state == "empty":
            versioned.fail(_("标签 {0} 当前没有有效规则（成员规则已停用）").format(name))
        if name not in chosen:
            chosen.append(name)
    return chosen


def select_text(profile: dict[str, Any], names: list[str], language: str = "zh") -> str:
    parts = plan(profile)
    chosen = check_selection(parts, names)
    # 并集：按 OPINION.md 里的顺序输出每条规则一次，常驻规则不在其中
    wanted = {identity for name in chosen for identity in parts["tags"][name]["rules"]}
    picked = [identity for identity in parts["rules"] if identity in wanted]
    custom = [line for line, owners in parts["lines"] if set(owners) & set(chosen)]
    lines = [_("# 按需规则：{0}").format(listing([f"`{name}`" for name in chosen]))]
    for identity in picked:
        lines.extend(("", versioned.render_rule(profile, parts["rules"][identity], language)))
    lines.extend(custom_block("\n".join(custom), language))
    return "\n".join(lines).rstrip() + "\n"


def index_view(profile: dict[str, Any], lock: dict[str, Any] | None, language: str = "zh") -> dict[str, Any]:
    parts = plan(profile)
    entries = []
    for name, item in parts["tags"].items():
        ready = item["state"] == "ready"
        entries.append({
            "id": name, "name": versioned.pick(item["tag"]["name"], language)[0], "state": item["state"],
            "rules": len(item["rules"]), "rule_ids": list(item["rules"]), "custom_lines": len(item["custom"]),
            "trigger": versioned.pick(item["tag"]["trigger"], language)[0], "exclude": versioned.pick(item["tag"]["exclude"], language)[0],
            "bytes": len(select_text(profile, [name], language).encode("utf-8")) if ready else 0,
            "index_bytes": len((tag_index_line(profile, parts, name, language) + "\n").encode("utf-8")) if ready else 0})
    return {
        "ok": True, "mode": "tags", "profile": f"{profile['id']}@{profile['version']}", "profile_sha256": versioned.digest(profile),
        "opinion_sha256": lock["opinion_sha256"] if lock else None,
        "resident": {"rules": list(parts["resident"]), "custom_lines": sum(1 for line, owners in parts["lines"] if line.strip() and not owners)},
        "tags": entries, "on_demand_tags": len(selectable(parts)), "rejected_custom_lines": parts["rejected"],
        "resident_bytes": len(core_text(profile, lock, language, index=False).encode("utf-8")),
        "core_bytes": len(core_text(profile, lock, language).encode("utf-8")),
        "complete_bytes": len(versioned.render(profile, language).encode("utf-8")),
    }


def declared(profile: dict[str, Any], signals: list[str], tags: list[str]) -> list[str]:
    """--signal touches=<标签> 与 --tag 等价；返回去重后的标签，none 单独出现表示本次不涉及任何按需标签。"""
    parts = plan(profile)
    names = list(tags)
    for item in signals:
        key, _sep, value = item.partition("=")
        if key != SIGNAL_KEY or not value:
            versioned.fail(_("未声明的交付信号：{0}").format(item))
        names.append(value)
    if not names:
        versioned.fail(_("check 至少需要一个 --tag 或 --signal"))
    if NONE in names:
        if len(set(names)) > 1:
            versioned.fail(_("none 表示不涉及按需标签，不能与其他标签同时声明"))
        return []
    return check_selection(parts, names)


def check(profile: dict[str, Any], signals: list[str], tags: list[str], loaded: list[str]) -> dict[str, Any]:
    """由标签派生信号：声明涉及的标签 -> 应读取的规则；已读取的标签并集 -> 已读规则；缺失为退出码 1。"""
    parts = plan(profile)
    names = declared(profile, signals, tags)
    have = check_selection(parts, loaded) if loaded else []
    wanted = [identity for identity in parts["rules"] if any(identity in parts["tags"][name]["rules"] for name in names)]
    read = {identity for name in have for identity in parts["tags"][name]["rules"]}
    required_lines = [line for line, owners in parts["lines"] if set(owners) & set(names)]
    read_lines = {line for line, owners in parts["lines"] if set(owners) & set(have)}
    missing_rules = [identity for identity in wanted if identity not in read]
    missing_tags = [name for name in names
                    if any(identity in missing_rules for identity in parts["tags"][name]["rules"])
                    or any(line not in read_lines for line in parts["tags"][name]["custom"])]
    return {"ok": not missing_tags, "signals": {SIGNAL_KEY: sorted(names)}, "required": names, "required_rules": wanted,
            "loaded": have, "missing": missing_tags, "missing_rules": missing_rules,
            "next": [f"context select {' '.join('--tag ' + name for name in missing_tags)}"] if missing_tags else [],
            "custom_lines_required": len(required_lines)}


def execute(args: argparse.Namespace, profile: dict[str, Any], lock: dict[str, Any] | None, language: str) -> tuple[int, str]:
    parts = plan(profile)
    if args.action != "select" and args.action != "check" and args.tag:
        versioned.fail(_("--tag 只用于 select 与 check"))
    if args.action == "core":
        return EXIT_OK, core_text(profile, lock, language)
    if args.action == "index":
        return EXIT_OK, versioned.pretty(index_view(profile, lock, language))
    if args.action == "bundle":
        versioned.fail(_("该 Profile 使用标签选择（schema 2.2），请用 context select --tag <标签>；有效标签：{0}").format(listing(selectable(parts))))
    if args.action == "select":
        if args.names:
            versioned.fail(_("select 只接受 --tag，不接受位置参数：{0}").format(" ".join(args.names)))
        return EXIT_OK, select_text(profile, args.tag, language)
    result = check(profile, args.signal, args.tag, args.loaded)
    return (EXIT_OK if result["ok"] else EXIT_MISSING), versioned.pretty(result)
