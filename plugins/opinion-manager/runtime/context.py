"""按 Profile 中用户确认的加载声明切分完整正文；只读，不写任何文件。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import versioned
from i18n_opinion import _, N_  # noqa: E402


FALLBACK = N_("完整读取项目根目录 OPINION.md")
PROJECT_PROFILE = Path(".agents") / "moe.sakanano.agent-pack" / "project.json"
DIRECT = "(direct)"
EXIT_OK, EXIT_MISSING, EXIT_ERROR, EXIT_FULL = 0, 1, 2, 3


class FullReading(RuntimeError):
    """当前契约本身要求完整读取，调用方按 fallback 执行即可。"""


def project_mode(root: Path) -> str:
    path = root / PROJECT_PROFILE
    if not path.is_file():
        return "tiered"
    mode = versioned.read_object(path).get("opinion", {}).get("loading_mode", "tiered")
    if mode not in ("tiered", "full"):
        versioned.fail(_("Project Profile 的 opinion.loading_mode 必须为 tiered 或 full：{0!r}").format(mode))
    return mode


def load(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """返回 Profile 与锁；使用候选 Profile 预览时锁为 None。"""
    if args.profile is not None:
        profile, lock = versioned.load_profile(args.profile), None
    else:
        if args.project is None:
            versioned.fail(_("context 需要 --project 或 --profile"))
        root = versioned.safe_path(args.project)
        versioned.verify_project(root)
        lock = versioned.read_object(root / "opinion.lock.json")
        profile = lock["profile"]
        if project_mode(root) == "full":
            raise FullReading(_("Project Profile 设为完整读取"))
    if "loading" not in profile:
        raise FullReading(_("当前 Profile 未声明分层加载"))
    return profile, lock


def partition(profile: dict[str, Any]) -> dict[str, Any]:
    """把每条有效规则分到唯一的模板系列；常驻系列优先，未登记的系列按常驻处理。"""
    loading = profile["loading"]
    rules = versioned.effective_rules(profile)
    routed = [bundle["family"] for bundle in loading["bundles"]]
    templates = [(source["ref"], source["ref"].split("/")[0], source["value"]) for source in profile["sources"]["templates"]]
    unlisted = [family for _, family, _ in templates if family not in loading["core"] and family not in routed]
    always = set(loading["core"]) | set(unlisted)
    assigned: dict[str, str] = {}
    for wanted in (True, False):
        for _ref, family, value in templates:
            if (family in always) is not wanted:
                continue
            for ref in value["rules"]:
                identity = versioned.parse_ref(ref, "rules")[0]
                if identity in rules:
                    assigned.setdefault(identity, family)
    groups: dict[str, list[str]] = {}
    for identity in rules:
        groups.setdefault(assigned.get(identity, DIRECT), []).append(identity)
    return {
        "rules": rules, "groups": groups, "unlisted": unlisted,
        "refs": {family: ref for ref, family, _ in templates},
        "names": {family: value["name"] for _, family, value in templates},
        "always": [family for family in (*loading["core"], *unlisted, DIRECT) if family in groups],
        "bundles": [bundle for bundle in loading["bundles"] if bundle["family"] in groups],
    }


def group_text(profile: dict[str, Any], parts: dict[str, Any], family: str) -> str:
    return "\n\n".join(versioned.render_rule(profile, parts["rules"][identity]) for identity in parts["groups"].get(family, []))


def core_text(profile: dict[str, Any], lock: dict[str, Any] | None) -> str:
    parts = partition(profile)
    origin = f"正文指纹：`{lock['opinion_sha256']}`" if lock else "候选 Profile 预览，尚未生效"
    lines = ["# Agent Opinion 核心规则", "", f"Profile：`{profile['id']}@{profile['version']}`；{origin}。",
             "本输出是完整正文的只读切片，始终适用。场景规则束见文末索引，命中时再读取。"]
    for family in parts["always"]:
        lines.extend(("", group_text(profile, parts, family)))
    if profile["custom"]:
        lines.extend(("", "## 用户自定义规则", "", profile["custom"]))
    lines.extend(("", "## 规则束索引", "", "任务命中触发条件时用 `context bundle <系列>` 读取对应规则束；判断不清时一并读取。"))
    for bundle in parts["bundles"]:
        family = bundle["family"]
        lines.append(f"- `{family}`（{parts['names'][family]}，{len(parts['groups'][family])} 条）：{bundle['trigger']}。不适用：{bundle['exclude']}。")
    return "\n".join(lines).rstrip() + "\n"


def resolve_bundle(parts: dict[str, Any], name: str) -> str:
    family = {ref: family for family, ref in parts["refs"].items()}.get(name, name)
    if family not in {bundle["family"] for bundle in parts["bundles"]}:
        versioned.fail(_("未知的按需规则束：{0}").format(name))
    return family


def bundle_text(profile: dict[str, Any], names: list[str]) -> str:
    if not names:
        versioned.fail(_("bundle 需要至少一个模板系列或精确模板引用"))
    parts = partition(profile)
    lines: list[str] = []
    for name in names:
        family = resolve_bundle(parts, name)
        lines.extend((f"# 规则束 `{parts['refs'][family]}`", "", group_text(profile, parts, family), ""))
    return "\n".join(lines).rstrip() + "\n"


def index_view(profile: dict[str, Any], lock: dict[str, Any] | None) -> dict[str, Any]:
    parts = partition(profile)

    def entry(family: str) -> dict[str, Any]:
        return {"family": family, "ref": parts["refs"].get(family), "name": parts["names"].get(family),
                "rules": len(parts["groups"][family]), "bytes": len(group_text(profile, parts, family).encode("utf-8"))}

    return {
        "ok": True, "profile": f"{profile['id']}@{profile['version']}", "profile_sha256": versioned.digest(profile),
        "opinion_sha256": lock["opinion_sha256"] if lock else None,
        "core": [entry(family) for family in parts["always"]],
        "custom_bytes": len((profile["custom"] or "").encode("utf-8")),
        "bundles": [{**entry(bundle["family"]), "trigger": bundle["trigger"], "exclude": bundle["exclude"], "routes": bundle["routes"]} for bundle in parts["bundles"]],
        "unlisted": parts["unlisted"], "signals": profile["loading"]["signals"],
        "core_bytes": len(core_text(profile, lock).encode("utf-8")), "complete_bytes": len(versioned.render(profile).encode("utf-8")),
    }


def check(profile: dict[str, Any], signals: list[str], loaded: list[str]) -> dict[str, Any]:
    parts = partition(profile)
    vocabulary = profile["loading"]["signals"]
    declared: dict[str, set[str]] = {}
    for item in signals:
        key, _sep, value = item.partition("=")
        if key not in vocabulary or value not in vocabulary[key]:
            versioned.fail(_("未声明的交付信号：{0}").format(item))
        declared.setdefault(key, set()).add(value)
    if not declared:
        versioned.fail(_("check 至少需要一个 --signal"))
    have = {resolve_bundle(parts, name) for name in loaded}
    required = [bundle["family"] for bundle in parts["bundles"]
                if any(all(declared.get(key, set()) & set(values) for key, values in route.items()) for route in bundle["routes"])]
    missing = [family for family in required if family not in have]
    return {"ok": not missing, "signals": {key: sorted(values) for key, values in declared.items()},
            "required": required, "loaded": sorted(have), "missing": missing}


def add_command(subparsers: Any) -> None:
    parser = subparsers.add_parser("context", help=_("按已确认的加载声明只读切分正文，并核对交付信号"))
    parser.add_argument("action", choices=("core", "bundle", "index", "check"))
    parser.add_argument("names", nargs="*", help=_("bundle 的模板系列或精确模板引用"))
    parser.add_argument("--project", type=Path)
    parser.add_argument("--profile", type=Path, help=_("用候选或已保存的 Profile 预览，不读取项目锁"))
    parser.add_argument("--signal", action="append", default=[], help=_("交付信号，格式为 键=值，可重复"))
    parser.add_argument("--loaded", action="append", default=[], help=_("本次已读取的规则束，可重复"))
    parser.add_argument("--output", choices=("json", "markdown"), default="markdown")


def execute(args: argparse.Namespace) -> tuple[int, str]:
    try:
        profile, lock = load(args)
        if args.action == "core":
            return EXIT_OK, core_text(profile, lock)
        if args.action == "bundle":
            return EXIT_OK, bundle_text(profile, args.names)
        if args.action == "index":
            return EXIT_OK, versioned.pretty(index_view(profile, lock))
        result = check(profile, args.signal, args.loaded)
        return (EXIT_OK if result["ok"] else EXIT_MISSING), versioned.pretty(result)
    except FullReading as exc:
        return EXIT_FULL, versioned.pretty({"ok": False, "error": str(exc), "fallback": _(FALLBACK)})
    except (versioned.OpinionVersionError, OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return EXIT_ERROR, versioned.pretty({"ok": False, "error": str(exc), "fallback": _(FALLBACK)})
