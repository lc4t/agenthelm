#!/usr/bin/env python3
"""条目式项目记忆：一条一文件，索引实时生成；只依赖标准库。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from i18n_orchestrator import _  # noqa: E402


PROJECT_PROFILE = Path(".agents") / "moe.sakanano.agent-pack" / "project.json"
DEFAULT_ROOT = "memory"
TYPES = ("reference", "project", "assumption", "conflict")
STATUSES = ("active", "retired")
SCALARS = ("schema_version", "id", "type", "status", "description", "created", "verified_at", "review_after", "supersedes", "promoted_to")
REQUIRED = ("schema_version", "id", "type", "status", "description", "created", "verified_at")
SUMMARY_SECTIONS = ("待确认假设", "未解决冲突", "Opinion 演化候选")
IDENTITY = re.compile(r"MEM-\d{8}-\d{3}-[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
DUE_MARK = "可能过期"


class EntryError(ValueError):
    pass


def fail(message: str) -> None:
    raise EntryError(message)


def confined(root: Path, relative: str | Path) -> Path:
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        fail(_("路径必须是项目内的相对路径：{0}").format(relative))
    path = root / relative
    if any(item.is_symlink() for item in (path, *path.parents) if root in (item, *item.parents)):
        fail(_("拒绝使用符号链接：{0}").format(relative))
    return path


def parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        fail(_("{0} 必须为 YYYY-MM-DD：{1!r}").format(label, value))


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        fail(_("JSON 根节点必须是对象：{0}").format(path.name))
    return value


def settings(args: argparse.Namespace) -> tuple[Path, Path, dict[str, Any]]:
    """解析项目根、记忆目录与参数；命令行优先，其次读取 Project Profile 的 memory 段。"""
    project = args.project.resolve()
    if not project.is_dir():
        fail(_("项目目录不存在：{0}").format(project))
    declared: dict[str, Any] = {}
    profile = project / PROJECT_PROFILE
    if profile.is_file():
        declared = read_json(profile).get("memory") or {}
    root = confined(project, args.root or declared.get("root") or DEFAULT_ROOT)
    policy_path = args.policy or declared.get("policy")
    policy = read_json(confined(project, policy_path)) if policy_path else {}
    return project, root, policy


def parse_entry(text: str) -> tuple[dict[str, Any], str]:
    """解析受限的 frontmatter：标量为 JSON 字符串，sources 为字符串列表。"""
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        fail(_("缺少 frontmatter"))
    header, body = text[4:].split("\n---\n", 1)
    meta: dict[str, Any] = {}
    current: str | None = None
    for line in header.splitlines():
        if line.startswith("  - ") and current == "sources":
            meta["sources"].append(json.loads(line[4:]))
            continue
        key, separator, raw = line.partition(":")
        if not separator or key != key.strip() or not key:
            fail(_("无法解析的 frontmatter 行：{0!r}").format(line))
        current = key
        if key == "sources":
            if raw.strip() not in ("", "[]"):
                fail(_("sources 必须写成列表"))
            meta["sources"] = []
        else:
            value = json.loads(raw.strip())
            if not isinstance(value, str):
                fail(_("{0} 必须是字符串").format(key))
            meta[key] = value
    return meta, body


def render_entry(meta: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for key in SCALARS:
        if key in meta:
            lines.append(f"{key}: {json.dumps(meta[key], ensure_ascii=False)}")
    lines.append("sources:" if meta.get("sources") else "sources: []")
    lines.extend(f"  - {json.dumps(item, ensure_ascii=False)}" for item in meta.get("sources", []))
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def load_entries(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    entries: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []
    for path in sorted(root.glob("MEM-*.md")) if root.is_dir() else []:
        try:
            meta, body = parse_entry(path.read_text(encoding="utf-8"))
        except (EntryError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            problems.append({"code": "unreadable", "entry": path.name, "message": str(exc)})
            continue
        entries.append({"path": path, "meta": meta, "body": body})
    return entries, problems


def due_date(meta: dict[str, Any], policy: dict[str, Any]) -> date | None:
    if meta.get("review_after"):
        return parse_date(meta["review_after"], "review_after")
    interval = policy.get("review_interval_days", {}).get(meta.get("type"))
    if interval and meta.get("verified_at"):
        return parse_date(meta["verified_at"], "verified_at") + timedelta(days=int(interval))
    return None


def is_due(meta: dict[str, Any], policy: dict[str, Any], today: date) -> bool:
    deadline = due_date(meta, policy)
    return deadline is not None and deadline < today


def index_lines(entries: list[dict[str, Any]], policy: dict[str, Any], today: date) -> list[str]:
    lines = []
    for entry in entries:
        meta = entry["meta"]
        if meta.get("status") != "active":
            continue
        try:
            mark = f"｜{DUE_MARK}" if is_due(meta, policy, today) else ""
        except EntryError:
            mark = ""
        lines.append(f"- `{meta.get('id', entry['path'].stem)}`（{meta.get('type', '?')}）：{meta.get('description', '')}{mark}")
    return lines


def check_entry(project: Path, entry: dict[str, Any], policy: dict[str, Any]) -> list[dict[str, str]]:
    meta, name = entry["meta"], entry["path"].name
    found: list[dict[str, str]] = []

    def problem(code: str, message: str) -> None:
        found.append({"code": code, "entry": name, "message": message})

    for key in REQUIRED:
        if not str(meta.get(key, "")).strip():
            problem("missing-field", _("缺少字段 {0}").format(key))
    unknown = set(meta) - set(SCALARS) - {"sources"}
    if unknown:
        problem("unknown-field", _("未知字段：{0}").format(_('、').join(sorted(unknown))))
    if meta.get("id") and (not IDENTITY.fullmatch(meta["id"]) or meta["id"] != entry["path"].stem):
        problem("id-mismatch", _("编号格式无效或与文件名不一致"))
    types = tuple(policy.get("types", TYPES))
    if meta.get("type") and meta["type"] not in types:
        problem("invalid-type", _("type 必须为：{0}").format(_('、').join(types)))
    if meta.get("status") and meta["status"] not in STATUSES:
        problem("invalid-status", _("status 必须为：{0}").format(_('、').join(STATUSES)))
    for key in ("created", "verified_at", "review_after"):
        if meta.get(key):
            try:
                parse_date(meta[key], key)
            except EntryError as exc:
                problem("invalid-date", str(exc))
    if meta.get("type") == "assumption" and meta.get("status") == "active" and not meta.get("review_after"):
        problem("assumption-without-review", _("未确认的假设必须设置 review_after"))
    if meta.get("status") == "active" and meta.get("promoted_to"):
        problem("promoted-still-active", _("已晋升的条目应当退役"))
    limit = policy.get("index", {}).get("max_description_chars")
    if limit and len(meta.get("description", "")) > int(limit):
        problem("description-too-long", _("description 超过 {0} 字符").format(limit))
    if meta.get("status") == "active":
        for source in meta.get("sources", []):
            try:
                missing = not confined(project, source).exists()
            except EntryError as exc:
                problem("invalid-source", str(exc))
                continue
            if missing:
                problem("missing-source", _("来源路径不存在：{0}").format(source))
    return found


def check_budgets(project: Path, lines: list[str], policy: dict[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    index = policy.get("index", {})
    size = len("\n".join(lines).encode("utf-8"))
    if index.get("max_lines") and len(lines) > int(index["max_lines"]):
        found.append({"code": "index-too-long", "entry": "", "message": _("索引 {0} 行，超过 {1} 行").format(len(lines), index['max_lines'])})
    if index.get("max_bytes") and size > int(index["max_bytes"]):
        found.append({"code": "index-too-large", "entry": "", "message": _("索引 {0} 字节，超过 {1} 字节").format(size, index['max_bytes'])})
    bundle = policy.get("startup_bundle", {})
    if bundle.get("max_bytes") and bundle.get("files"):
        total = sum(confined(project, item).stat().st_size for item in bundle["files"] if confined(project, item).is_file()) + size
        if total > int(bundle["max_bytes"]):
            found.append({"code": "startup-too-large", "entry": "", "message": _("启动层合计 {0} 字节，超过 {1} 字节").format(total, bundle['max_bytes'])})
    summary = policy.get("chat_summary", {})
    path = confined(project, summary.get("path", ".agent-doc/chat-summary.md"))
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        headings = [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]
        for section in summary.get("required_sections", SUMMARY_SECTIONS):
            # 按前缀匹配，项目可以在固定小节名之后补充说明。
            if not any(heading.startswith(section) for heading in headings):
                found.append({"code": "summary-section-missing", "entry": path.name, "message": _("缺少小节：{0}").format(section)})
        if summary.get("max_bytes") and len(text.encode("utf-8")) > int(summary["max_bytes"]):
            found.append({"code": "summary-too-large", "entry": path.name, "message": _("超过 {0} 字节").format(summary['max_bytes'])})
    return found


def command_check(project: Path, root: Path, policy: dict[str, Any], today: date) -> dict[str, Any]:
    entries, problems = load_entries(root)
    seen: dict[str, str] = {}
    for entry in entries:
        problems.extend(check_entry(project, entry, policy))
        identity = entry["meta"].get("id")
        if identity in seen:
            problems.append({"code": "duplicate-id", "entry": entry["path"].name, "message": _("与 {0} 编号重复").format(seen[identity])})
        seen.setdefault(identity, entry["path"].name)
    lines = index_lines(entries, policy, today)
    problems.extend(check_budgets(project, lines, policy))
    active = [entry["meta"] for entry in entries if entry["meta"].get("status") == "active"]
    due = []
    for meta in active:
        try:
            if is_due(meta, policy, today):
                due.append(meta.get("id"))
        except EntryError:
            continue
    return {"ok": not problems, "entries": len(entries), "active": len(active), "index_lines": len(lines),
            "index_bytes": len("\n".join(lines).encode("utf-8")), "due": due, "problems": problems}


def command_create(args: argparse.Namespace, project: Path, root: Path, policy: dict[str, Any], today: date) -> dict[str, Any]:
    types = tuple(policy.get("types", TYPES))
    if args.type not in types:
        fail(_("--type 必须为：{0}").format(_('、').join(types)))
    if not args.slug or not SLUG.fullmatch(args.slug):
        fail(_("--slug 只能包含小写字母、数字和连字符"))
    if not args.description or not args.description.strip():
        fail(_("--description 说明何时应当想起这条记忆，不能为空"))
    body = confined(project, args.body_file).read_text(encoding="utf-8") if args.body_file else ""
    if not body.strip():
        fail(_("--body-file 必须提供非空正文"))
    created = parse_date(args.date, "--date") if args.date else today
    if args.review_after:
        parse_date(args.review_after, "--review-after")
    stamp = created.strftime("%Y%m%d")
    taken = {int(path.name[13:16]) for path in root.glob(f"MEM-{stamp}-*.md")} if root.is_dir() else set()
    identity = f"MEM-{stamp}-{max(taken, default=0) + 1:03d}-{args.slug}"
    meta = {"schema_version": "1.0", "id": identity, "type": args.type, "status": "active", "description": args.description.strip(),
            "created": created.isoformat(), "verified_at": created.isoformat(), "review_after": args.review_after or "",
            "supersedes": args.supersedes or "", "promoted_to": "", "sources": list(args.source)}
    path = root / f"{identity}.md"
    problems = check_entry(project, {"path": path, "meta": meta, "body": body}, policy)
    if problems:
        fail(_("；").join(item["message"] for item in problems))
    content = render_entry(meta, "\n" + body.strip() + "\n")
    if args.apply:
        root.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(content)
    return {"ok": True, "status": "written" if args.apply else "preview", "path": path.relative_to(project).as_posix(), "content": content}


def command_update(args: argparse.Namespace, project: Path, root: Path, today: date) -> dict[str, Any]:
    if not args.id:
        fail(_("{0} 需要 --id").format(args.action))
    path = root / f"{args.id}.md"
    if not IDENTITY.fullmatch(args.id) or not path.is_file():
        fail(_("找不到记忆条目：{0}").format(args.id))
    meta, body = parse_entry(path.read_text(encoding="utf-8"))
    if args.action == "retire":
        if not args.reason and not args.promoted_to:
            fail(_("retire 需要 --reason 或 --promoted-to"))
        meta["status"] = "retired"
        meta["promoted_to"] = args.promoted_to or meta.get("promoted_to", "")
        body = body.rstrip() + f"\n\n**退役**：{today.isoformat()}，{args.reason or '已晋升到 ' + args.promoted_to}\n"
    else:
        meta["verified_at"] = today.isoformat()
        if args.review_after:
            parse_date(args.review_after, "--review-after")
            meta["review_after"] = args.review_after
    content = render_entry(meta, body)
    if args.apply:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    return {"ok": True, "status": "written" if args.apply else "preview", "path": path.relative_to(project).as_posix(), "content": content}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=_("条目式项目记忆：生成索引、检查、创建、核实与退役"))
    parser.add_argument("action", choices=("index", "list", "check", "create", "verify", "retire"))
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--root", help=_("记忆目录，默认取 Project Profile 的 memory.root，再默认 memory"))
    parser.add_argument("--policy", help=_("项目自己的参数文件；未提供时不做体积与间隔检查"))
    parser.add_argument("--today", help=_("用于测试与回放的日期，YYYY-MM-DD"))
    parser.add_argument("--output", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--apply", action="store_true", help=_("create、verify、retire 默认只预览"))
    parser.add_argument("--id")
    parser.add_argument("--type")
    parser.add_argument("--slug")
    parser.add_argument("--description", help=_("一句话说明何时应当想起这条记忆"))
    parser.add_argument("--body-file", help=_("项目内的正文文件"))
    parser.add_argument("--source", action="append", default=[], help=_("项目内的来源路径，可重复"))
    parser.add_argument("--review-after", help=_("复核日期，YYYY-MM-DD"))
    parser.add_argument("--supersedes")
    parser.add_argument("--date", help=_("create 的创建日期，默认今天"))
    parser.add_argument("--reason")
    parser.add_argument("--promoted-to")
    return parser


def run(args: argparse.Namespace) -> tuple[int, str]:
    project, root, policy = settings(args)
    today = parse_date(args.today, "--today") if args.today else date.today()
    if args.action == "index":
        entries, _problems = load_entries(root)
        lines = index_lines(entries, policy, today)
        if args.output == "json":
            return 0, json.dumps({"ok": True, "lines": lines}, ensure_ascii=False, indent=2) + "\n"
        return 0, "\n".join(lines) + ("\n" if lines else "")
    if args.action == "list":
        entries, problems = load_entries(root)
        value = [{**entry["meta"], "path": entry["path"].relative_to(project).as_posix()} for entry in entries]
        return 0, json.dumps({"ok": True, "entries": value, "problems": problems}, ensure_ascii=False, indent=2) + "\n"
    if args.action == "check":
        result = command_check(project, root, policy, today)
        return (0 if result["ok"] else 1), json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.action == "create":
        result = command_create(args, project, root, policy, today)
    else:
        result = command_update(args, project, root, today)
    return 0, json.dumps(result, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code, output = run(args)
    except (ValueError, OSError) as exc:
        code, output = 2, json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2) + "\n"
    sys.stdout.write(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
