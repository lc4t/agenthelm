from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import context
import inbox
import lifecycle
import versioned
from constants import KNOWN_PLACEHOLDERS
from i18n_opinion import _  # noqa: E402


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PLUGIN_ROOT / "templates"
PLUGIN_MANIFEST = PLUGIN_ROOT / "plugin.json"
OPINION_FILE = "OPINION.md"
ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class OpinionError(RuntimeError):
    pass


def read_json_object(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OpinionError(_("拒绝读取非普通文件：{0}").format(path))
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise OpinionError(_("JSON 根节点必须是对象：{0}").format(path))
    return value


def validate_rule(rule: Any, template_id: str) -> dict[str, Any]:
    if not isinstance(rule, dict):
        raise OpinionError(_("模板 {0} 的 rules 项必须是对象").format(template_id))
    required = {"id", "title", "level", "scopes", "text"}
    missing = required - set(rule)
    if missing:
        raise OpinionError(_("规则缺少字段 {0}：{1}").format(sorted(missing), template_id))
    unknown = set(rule) - required
    if unknown:
        raise OpinionError(_("规则包含未知字段 {0}：{1}").format(sorted(unknown), template_id))
    rule_id = rule["id"]
    if not isinstance(rule_id, str) or not ID_PATTERN.fullmatch(rule_id):
        raise OpinionError(_("规则编号无效：{0!r}").format(rule_id))
    if rule["level"] not in {"required", "preferred"}:
        raise OpinionError(_("规则 level 无效：{0}").format(rule_id))
    if not isinstance(rule["title"], str) or not rule["title"].strip():
        raise OpinionError(_("规则标题为空：{0}").format(rule_id))
    if not isinstance(rule["text"], str) or not rule["text"].strip():
        raise OpinionError(_("规则正文为空：{0}").format(rule_id))
    scopes = rule["scopes"]
    if not isinstance(scopes, list) or not scopes or not all(
        isinstance(scope, str) and scope.strip() for scope in scopes
    ) or len(scopes) != len(set(scopes)):
        raise OpinionError(_("规则 scopes 必须是非空字符串数组：{0}").format(rule_id))
    return rule


def validate_template(value: dict[str, Any], path: Path) -> dict[str, Any]:
    required = {"id", "version", "name", "description", "scopes", "rules", "questions"}
    missing = required - set(value)
    if missing:
        raise OpinionError(_("模板缺少字段 {0}：{1}").format(sorted(missing), path))
    unknown = set(value) - required
    if unknown:
        raise OpinionError(_("模板包含未知字段 {0}：{1}").format(sorted(unknown), path))
    template_id = value["id"]
    if not isinstance(template_id, str) or not ID_PATTERN.fullmatch(template_id):
        raise OpinionError(_("模板编号无效：{0!r}").format(template_id))
    if path.stem != template_id:
        raise OpinionError(_("模板文件名必须与编号一致：{0}").format(path.name))
    if not isinstance(value["version"], str) or not VERSION_PATTERN.fullmatch(value["version"]):
        raise OpinionError(_("模板版本必须使用三段数字：{0}").format(template_id))
    for field in ("name", "description"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise OpinionError(_("模板字段 {0} 不能为空：{1}").format(field, template_id))
    if not isinstance(value["scopes"], list) or not value["scopes"] or not all(
        isinstance(scope, str) and scope.strip() for scope in value["scopes"]
    ) or len(value["scopes"]) != len(set(value["scopes"])):
        raise OpinionError(_("模板 scopes 必须是非空数组：{0}").format(template_id))
    rules = value["rules"]
    if not isinstance(rules, list) or not rules:
        raise OpinionError(_("模板 rules 必须是非空数组：{0}").format(template_id))
    rule_ids: set[str] = set()
    for rule in rules:
        validated = validate_rule(rule, template_id)
        if validated["id"] in rule_ids:
            raise OpinionError(_("模板内存在重复规则编号：{0}").format(validated['id']))
        rule_ids.add(validated["id"])
    questions = value["questions"]
    if not isinstance(questions, list):
        raise OpinionError(_("模板 questions 必须是数组：{0}").format(template_id))
    question_ids: set[str] = set()
    for question in questions:
        if not isinstance(question, dict) or set(question) != {"id", "prompt", "options"}:
            raise OpinionError(_("模板问题结构无效：{0}").format(template_id))
        if not isinstance(question["id"], str) or not ID_PATTERN.fullmatch(question["id"]):
            raise OpinionError(_("模板问题编号无效：{0}").format(template_id))
        if question["id"] in question_ids:
            raise OpinionError(_("模板问题编号重复：{0}").format(question['id']))
        question_ids.add(question["id"])
        if not isinstance(question["prompt"], str) or not question["prompt"].strip():
            raise OpinionError(_("模板问题正文为空：{0}").format(template_id))
        if not isinstance(question["options"], list) or len(question["options"]) < 2:
            raise OpinionError(_("模板问题至少需要两个选项：{0}").format(template_id))
        for option in question["options"]:
            if not isinstance(option, dict) or set(option) != {"label", "recommend"}:
                raise OpinionError(_("模板问题选项结构无效：{0}").format(template_id))
            if not isinstance(option["label"], str) or not option["label"].strip():
                raise OpinionError(_("模板问题选项标签为空：{0}").format(template_id))
            recommendations = option["recommend"]
            if not isinstance(recommendations, list) or not recommendations or not all(
                isinstance(recommendation, str) and recommendation in rule_ids
                for recommendation in recommendations
            ) or len(recommendations) != len(set(recommendations)):
                raise OpinionError(_("模板问题引用了未知规则：{0}").format(template_id))
    return value


def load_catalog() -> OrderedDict[str, dict[str, Any]]:
    if not TEMPLATE_ROOT.is_dir() or TEMPLATE_ROOT.is_symlink():
        raise OpinionError(_("模板目录不可用：{0}").format(TEMPLATE_ROOT))
    catalog: OrderedDict[str, dict[str, Any]] = OrderedDict()
    global_rules: dict[str, dict[str, Any]] = {}
    for path in sorted(TEMPLATE_ROOT.glob("*.json")):
        template = validate_template(read_json_object(path), path)
        template_id = template["id"]
        if template_id in catalog:
            raise OpinionError(_("模板编号重复：{0}").format(template_id))
        for rule in template["rules"]:
            current = global_rules.get(rule["id"])
            if current is not None and current != rule:
                raise OpinionError(_("相同规则编号的内容不一致：{0}").format(rule['id']))
            global_rules[rule["id"]] = rule
        catalog[template_id] = template
    return catalog


def parse_template_spec(spec: str, catalog: OrderedDict[str, dict[str, Any]]) -> str:
    template_id, separator, version = spec.partition("@")
    if template_id not in catalog:
        raise OpinionError(_("未知模板：{0}").format(template_id))
    current = catalog[template_id]["version"]
    if separator and version != current:
        raise OpinionError(_("模板版本不匹配：{0}@{1}，当前版本为 {2}").format(template_id, version, current))
    return template_id


def rule_index(catalog: OrderedDict[str, dict[str, Any]]) -> dict[str, tuple[str, dict[str, Any]]]:
    indexed: dict[str, tuple[str, dict[str, Any]]] = {}
    for template_id, template in catalog.items():
        for rule in template["rules"]:
            indexed.setdefault(rule["id"], (template_id, rule))
    return indexed


def read_custom_file(path: Path | None) -> str | None:
    if path is None:
        return None
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OpinionError(_("自定义规则必须来自普通文件：{0}").format(path))
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise OpinionError(_("自定义规则文件为空"))
    return content


def select_rules(
    catalog: OrderedDict[str, dict[str, Any]],
    template_specs: list[str],
    rule_ids: list[str],
) -> tuple[OrderedDict[str, list[dict[str, Any]]], list[str]]:
    selected: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    selected_ids: set[str] = set()
    source_versions: OrderedDict[str, str] = OrderedDict()
    for spec in template_specs:
        template_id = parse_template_spec(spec, catalog)
        template = catalog[template_id]
        source_versions[template_id] = template["version"]
        for rule in template["rules"]:
            if rule["id"] not in selected_ids:
                selected.setdefault(template_id, [])
                selected[template_id].append(rule)
                selected_ids.add(rule["id"])
    indexed = rule_index(catalog)
    for rule_id in rule_ids:
        if rule_id not in indexed:
            raise OpinionError(_("未知规则：{0}").format(rule_id))
        template_id, rule = indexed[rule_id]
        source_versions[template_id] = catalog[template_id]["version"]
        if rule_id not in selected_ids:
            selected.setdefault(template_id, [])
            selected[template_id].append(rule)
            selected_ids.add(rule_id)
    sources = [f"{template_id}@{version}" for template_id, version in source_versions.items()]
    return selected, sources


def selection_mode(template_specs: list[str], rule_ids: list[str], custom: str | None) -> str:
    kinds = sum(bool(value) for value in (template_specs, rule_ids, custom))
    if kinds > 1:
        return "mixed"
    if template_specs:
        return "template"
    if rule_ids:
        return "guided"
    return "custom"


def render_opinion(
    catalog: OrderedDict[str, dict[str, Any]],
    selected: OrderedDict[str, list[dict[str, Any]]],
    sources: list[str],
    custom: str | None,
    mode: str,
) -> tuple[str, dict[str, Any]]:
    ordered_rule_ids = [rule["id"] for rules in selected.values() for rule in rules]
    metadata = {
        "schema_version": "1.0",
        "provider": "opinion-manager",
        "provider_version": read_json_object(PLUGIN_MANIFEST)["version"],
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "mode": mode,
        "templates": sources,
        "rules": ordered_rule_ids,
        "custom": custom is not None,
    }
    lines = [
        "<!-- opinion-manager:metadata",
        json.dumps(metadata, ensure_ascii=False, indent=2),
        "-->",
        "",
        "# Agent Opinion",
        "",
        "本文件是当前环境中用户确认的 Agent 行为、表达、判断与交付要求。",
    ]
    if custom:
        lines.extend(("", "## 用户自定义规则", "", custom))
    for template_id, rules in selected.items():
        template = catalog[template_id]
        lines.extend((
            "",
            f"## {template['name']}",
            "",
            f"> 模板：`{template_id}@{template['version']}`。{template['description']}",
        ))
        for level, title in (("required", "强制规则"), ("preferred", "偏好")):
            matching = [rule for rule in rules if rule["level"] == level]
            if not matching:
                continue
            lines.extend(("", f"### {title}", ""))
            for rule in matching:
                scopes = "、".join(rule["scopes"])
                lines.extend((
                    f"<!-- opinion-rule:{rule['id']} -->",
                    f"- **{rule['title']}**（适用：{scopes}）：{rule['text']}",
                ))
    return "\n".join(lines).rstrip() + "\n", metadata


def known_placeholder(content: str) -> bool:
    return content in KNOWN_PLACEHOLDERS


def write_atomic(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_opinion(project: Path, content: str, *, replace: bool) -> str:
    root = project.expanduser().resolve()
    if not root.is_dir():
        raise OpinionError(_("项目目录不存在：{0}").format(root))
    destination = root / OPINION_FILE
    if destination.is_symlink():
        raise OpinionError(_("拒绝写入符号链接：{0}").format(destination))
    if destination.exists():
        if not destination.is_file():
            raise OpinionError(_("目标路径不是普通文件：{0}").format(destination))
        current = destination.read_text(encoding="utf-8")
        if current == content:
            return "unchanged"
        if not replace and not known_placeholder(current):
            raise OpinionError(_("OPINION.md 已包含内容；预览差异并明确使用 --replace"))
    write_atomic(destination, content)
    if destination.read_text(encoding="utf-8") != content:
        raise OpinionError(_("写入后核对 OPINION.md 失败"))
    return "written"


def catalog_markdown(catalog: OrderedDict[str, dict[str, Any]]) -> str:
    lines = [_("# Opinion 模板目录"), ""]
    if not catalog:
        lines.append(_("当前没有已批准的公开模板。"))
        return "\n".join(lines).rstrip() + "\n"
    for template in catalog.values():
        identity = f"{template['family']}/{template['variant']}" if "family" in template else template["id"]
        lines.extend((
            f"## {template['name']} `{identity}@{template['version']}`",
            "",
            template["description"],
            "",
            _("规则数量：{0}；适用场景：{1}。").format(len(template['rules']), _('、').join(template['scopes'])),
            "",
        ))
    return "\n".join(lines).rstrip() + "\n"


def catalog_payload(catalog: OrderedDict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": True,
        "templates": list(catalog.values()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=_("管理版本化 Opinion 模板并生成统一 OPINION.md"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog = subparsers.add_parser("catalog", help=_("查看模板、规则和引导问题"))
    catalog.add_argument("--output", choices=("json", "markdown"), default="markdown")

    compose = subparsers.add_parser("compose", help=_("组合模板、规则和自定义内容"))
    compose.add_argument("--project", type=Path, required=True)
    compose.add_argument("--template", action="append", default=[])
    compose.add_argument("--rule", action="append", default=[])
    compose.add_argument("--custom-file", type=Path)
    compose.add_argument("--output", choices=("json", "markdown"), default="markdown")
    compose.add_argument("--apply", action="store_true")
    compose.add_argument("--replace", action="store_true")
    versioned.add_commands(subparsers, compose, catalog)
    context.add_command(subparsers)
    lifecycle.add_command(subparsers)
    inbox.add_command(subparsers)
    return parser


def execute(args: argparse.Namespace) -> tuple[int, str]:
    if args.command == "context":
        return context.execute(args)
    if args.command == "lifecycle":
        return lifecycle.execute(args)
    if args.command == "inbox":
        return inbox.execute(args)
    if versioned.handles(args):
        return versioned.execute(args)
    catalog = load_catalog()
    if args.command == "catalog":
        releases = versioned.load_catalog(versioned.PUBLIC_CATALOG)
        combined = OrderedDict(catalog)
        combined.update(releases["templates"])
        if args.output == "json":
            payload = catalog_payload(combined)
            payload["rules"] = list(releases["rules"].values())
            return 0, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        return 0, catalog_markdown(combined)

    lock_path = args.project.expanduser() / "opinion.lock.json"
    if lock_path.exists() or lock_path.is_symlink():
        raise OpinionError(_("项目已经使用版本锁定；请用 compose --profile 更新"))

    custom = read_custom_file(args.custom_file)
    if not args.template and not args.rule and custom is None:
        raise OpinionError(_("至少选择一个模板、一个规则或一份自定义规则文件"))
    selected, sources = select_rules(catalog, args.template, args.rule)
    mode = selection_mode(args.template, args.rule, custom)
    content, metadata = render_opinion(catalog, selected, sources, custom, mode)
    status = "preview"
    if args.apply:
        status = apply_opinion(args.project, content, replace=args.replace)
    if args.output == "json":
        payload = {
            "ok": True,
            "applied": args.apply,
            "status": status,
            "path": str(args.project.expanduser().resolve() / OPINION_FILE),
            "metadata": metadata,
            "content": content,
        }
        return 0, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return 0, content


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code, output = execute(args)
    except (OpinionError, versioned.OpinionVersionError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if getattr(args, "output", "markdown") == "json":
            output = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2) + "\n"
        else:
            output = _("错误：{0}\n").format(exc)
        code = 2
    sys.stdout.write(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
