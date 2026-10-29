#!/usr/bin/env python3
"""默认 Case 工具：在 case-workspace 项目里新建、列出、查看、更新与校验 Case。

这是 agenthelm 的官方样例实现。项目在 Project Profile 的 ``work.case.command``
声明了自己的 Case 工具时，本工具只做只读的 list / show / validate，写操作让位给
项目工具；``work.case.schema`` 可替换随包的默认 Schema。所有写入为原子替换，
Case 根目录与 CASE.md 拒绝符号链接。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
import case_structure as structure  # noqa: E402
from i18n_orchestrator import _  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import private_paths  # noqa: E402
from i18n_orchestrator import _  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = PLUGIN_ROOT / "references" / "case.schema.json"
TEMPLATES = {"zh": PLUGIN_ROOT / "templates" / "CASE.md", "en": PLUGIN_ROOT / "templates" / "CASE.en.md"}
PROFILE = Path(".agents/moe.sakanano.agent-pack/project.json")
CASE_DIR_RE = re.compile(r"^CASE-([0-9]{8})-([0-9]{3})-([a-z0-9]+(?:-[a-z0-9]+)*)$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CREDENTIAL_RE = re.compile(r"(credential|secret|token|password|passwd|private-?key)", re.IGNORECASE)
STATUS_ORDER = {"active": 0, "waiting": 1, "captured": 2, "suspended": 3, "done": 4, "abandoned": 5}
CLOSED = {"done", "abandoned"}
FIELD_ORDER = ("schema_version", "id", "title", "status", "sensitivity", "source", "goal", "done_when",
               "current_state", "next_action", "waiting_for", "resume_when", "artifact_mode", "sop")
LIST_FIELDS = ("aliases", "legacy_ids", "tags", "deliverables", "evidence")
MAX_CASE_BYTES = 2 * 1024 * 1024


class CaseError(Exception):
    def __init__(self, message: str, code: str = "invalid-request") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- frontmatter

def _scalar(value: str, key: str) -> Any:
    if value == "[]":
        return []
    if value in ("true", "false"):
        return value == "true"
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise CaseError(_("{0}: 字符串必须是 JSON 双引号格式").format(key), "frontmatter") from exc
        if not isinstance(parsed, str):
            raise CaseError(_("{0}: 只支持字符串").format(key), "frontmatter")
        return parsed
    if value[:1] in ("[", "{", "|", ">", "'", "&", "*", "!"):
        raise CaseError(_("{0}: 不支持该 YAML 写法").format(key), "frontmatter")
    return value


def parse_frontmatter(text: str) -> dict:
    """Strict subset: top-level ``key: scalar`` and ``key:`` followed by ``  - item`` lists."""
    lines, data, index = text.splitlines(), {}, 0
    while index < len(lines):
        line = lines[index]
        if not line or line[0].isspace() or ":" not in line:
            raise CaseError(_("frontmatter 第 {0} 行格式不支持").format(index + 1), "frontmatter")
        key, value = line.split(":", 1)
        if not KEY_RE.fullmatch(key) or key in data:
            raise CaseError(_("frontmatter 键无效或重复：{0}").format(key), "frontmatter")
        if value.strip():
            data[key] = _scalar(value.strip(), key)
            index += 1
            continue
        items, index = [], index + 1
        while index < len(lines) and lines[index].startswith("  - "):
            items.append(_scalar(lines[index][4:], key))
            index += 1
        data[key] = items
    return data


def read_case(path: Path) -> tuple[dict, str]:
    if path.is_symlink():
        raise CaseError(_("拒绝符号链接：{0}").format(path.name), "symlink")
    raw = path.read_bytes()
    if len(raw) > MAX_CASE_BYTES:
        raise CaseError(_("CASE.md 超过 2 MiB"), "oversize")
    text = raw.decode("utf-8")
    if not text.startswith("---\n"):
        raise CaseError(_("CASE.md 必须以 --- 开头"), "frontmatter")
    end = text.find("\n---\n", 3)
    if end < 0:
        raise CaseError(_("CASE.md 缺少 frontmatter 结束标记"), "frontmatter")
    return parse_frontmatter(text[4:end]), text[end + 5:]


def render_frontmatter(data: dict) -> str:
    lines = ["---"]
    keys = [key for key in FIELD_ORDER if key in data]
    keys += sorted(key for key in data if key not in FIELD_ORDER and key not in ("created_at", "updated_at"))
    keys += [key for key in ("created_at", "updated_at") if key in data]
    for key in keys:
        value = data[key]
        if isinstance(value, list):
            lines.append(f"{key}: []" if not value else f"{key}:")
            lines.extend(f"  - {json.dumps(item, ensure_ascii=False)}" for item in value)
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
    return "\n".join(lines) + "\n---\n"


# ---------------------------------------------------------------- schema

def validate_schema(data: dict, schema: dict) -> list[str]:
    """The keyword subset the default schema uses: const, enum, type, pattern, minLength, items, uniqueItems."""
    errors = [_("缺少字段：{0}").format(key) for key in schema.get("required", []) if key not in data]
    properties = schema.get("properties", {})
    for key, value in data.items():
        rule = properties.get(key)
        if rule is None:
            if schema.get("additionalProperties") is False:
                errors.append(_("未知字段：{0}").format(key))
            continue
        errors.extend(f"{key}: {problem}" for problem in _check(value, rule))
    return errors


def _check(value: Any, rule: dict) -> list[str]:
    problems = []
    if "const" in rule and value != rule["const"]:
        problems.append(_("必须为 {0!r}").format(rule['const']))
    if "enum" in rule and value not in rule["enum"]:
        problems.append(_("取值必须是 ") + _("、").join(map(str, rule["enum"])))
    kind = rule.get("type")
    if kind == "string" and not isinstance(value, str):
        problems.append(_("必须是字符串"))
    if kind == "array":
        if not isinstance(value, list):
            return problems + [_("必须是列表")]
        if rule.get("uniqueItems") and len(value) != len(set(map(str, value))):
            problems.append(_("列表有重复项"))
        for item in value:
            problems.extend(_check(item, rule.get("items", {})))
    if isinstance(value, str):
        if len(value) < rule.get("minLength", 0):
            problems.append(_("不能为空"))
        if "pattern" in rule and not re.search(rule["pattern"], value):
            problems.append(_("格式不符合"))
    return problems


# ---------------------------------------------------------------- project

class Project:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        profile_path = self.root / PROFILE
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CaseError(_("缺少 Project Profile：{0}").format(PROFILE), "profile-missing") from exc
        except (OSError, ValueError) as exc:
            raise CaseError(_("Project Profile 无法解析：{0}").format(exc), "profile-invalid") from exc
        self.language = structure.language_of(profile)  # the language the project's files are written in
        work = profile.get("work") or {}
        if work.get("mode") != "case-workspace":
            raise CaseError(_("项目未启用 case-workspace（Profile work.mode）"), "mode")
        case = work.get("case") or {}
        self.case_root = self._inside(case.get("root") or "cases", "work.case.root")
        self.project_command = case.get("command")
        schema_value = case.get("schema")
        schema_path = self._inside(schema_value, "work.case.schema") if schema_value else DEFAULT_SCHEMA
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))

    def _inside(self, value: str, label: str) -> Path:
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise CaseError(_("{0} 必须是项目内相对路径").format(label), "profile-invalid")
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise CaseError(_("{0} 不能经过符号链接").format(label), "symlink")
        return self.root / relative

    def require_writable(self) -> None:
        if self.project_command:
            raise CaseError(_("项目声明了自己的 Case 工具（{0}），请用它修改 Case").format(self.project_command), "project-tool")

    def case_dirs(self) -> list[Path]:
        if not self.case_root.is_dir():
            return []
        return sorted(path for path in self.case_root.iterdir()
                      if path.is_dir() and not path.is_symlink() and CASE_DIR_RE.fullmatch(path.name))

    def find(self, term: str) -> Path:
        matches = [path for path in self.case_dirs() if path.name == term]
        if not matches:
            lowered = term.lower()
            matches = [path for path in self.case_dirs() if lowered in path.name.lower()]
        if len(matches) != 1:
            raise CaseError(_("找到 {0} 个匹配 {1!r} 的 Case").format(len(matches), term), "not-found" if not matches else "ambiguous")
        return matches[0]

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()


def now() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def write_atomic(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
                                     suffix=".tmp", delete=False) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def clean(value: str | None, label: str, *, required: bool = False) -> str:
    value = (value or "").strip()
    if any(character in value for character in "\r\n\x00"):
        raise CaseError(_("{0} 只能是单行文本").format(label))
    if required and not value:
        raise CaseError(_("{0} 不能为空").format(label))
    return value


# ---------------------------------------------------------------- commands

def _new_case_fields(project: Project, args: argparse.Namespace) -> tuple[str, dict]:
    slug = clean(args.slug, "--slug", required=True)
    if not SLUG_RE.fullmatch(slug) or CREDENTIAL_RE.search(slug):
        raise CaseError(_("--slug 只能用小写字母、数字和连字符，且不能含凭据类词语"))
    status = args.status
    fields = {
        "title": clean(args.title, "--title", required=True),
        "goal": clean(args.goal, "--goal", required=True),
        "next_action": clean(args.next_action, "--next-action", required=status == "active"),
        "done_when": clean(args.done_when, "--done-when"),
        "source": clean(args.source, "--source"),
        "current_state": clean(args.current_state, "--current-state") or structure.text("just_created", project.language),
    }
    return slug, fields


def _render_new_case(project: Project, args: argparse.Namespace, fields: dict, case_id: str,
                     stamp: str) -> str:
    data = {"schema_version": "1.0", "id": case_id, "title": fields["title"], "status": args.status,
            "sensitivity": (getattr(args, "sensitivity", None) or "shared"), "source": fields["source"], "goal": fields["goal"],
            "done_when": fields["done_when"], "current_state": fields["current_state"],
            "next_action": fields["next_action"], "artifact_mode": "case-local",
            "sop": (getattr(args, "sop", None) or ""),
            "aliases": [], "legacy_ids": [], "tags": sorted(set(args.tag)), "deliverables": [], "evidence": [],
            "created_at": stamp, "updated_at": stamp}
    data = {key: value for key, value in data.items() if key in project.schema.get("properties", data)}
    if not data.get("sop"):
        data.pop("sop", None)
    errors = validate_schema(data, project.schema)
    if errors:
        raise CaseError(_("新 Case 不符合 Schema：") + _("；").join(errors), "schema")
    body = TEMPLATES[project.language].read_text(encoding="utf-8")
    for key, value in {**fields, "created_at": stamp}.items():
        body = body.replace("{{" + key + "}}", value or structure.text("to_define", project.language))
    return render_frontmatter(data) + "\n" + body


def _today_sequences(project: Project, today: str) -> list[int]:
    if not project.case_root.is_dir():
        return []
    return [int(match.group(2)) for path in project.case_root.iterdir()
            if (match := CASE_DIR_RE.fullmatch(path.name)) and match.group(1) == today]


def plan_create(project: Project, args: argparse.Namespace) -> tuple[str, str]:
    """The Case id and CASE.md text ``create`` would write now; writes nothing.

    The id is a forecast: a concurrent create may take the sequence first, and then
    ``create`` uses the next free one.
    """
    project.require_writable()
    slug, fields = _new_case_fields(project, args)
    today = datetime.now().astimezone().strftime("%Y%m%d")
    case_id = f"CASE-{today}-{max(_today_sequences(project, today), default=0) + 1:03d}-{slug}"
    return case_id, _render_new_case(project, args, fields, case_id, now())


def create(project: Project, args: argparse.Namespace) -> dict:
    project.require_writable()
    slug, fields = _new_case_fields(project, args)
    project.case_root.mkdir(parents=True, exist_ok=True)
    today = datetime.now().astimezone().strftime("%Y%m%d")
    for sequence in range(max(_today_sequences(project, today), default=0) + 1, 1000):
        case_id = f"CASE-{today}-{sequence:03d}-{slug}"
        directory = project.case_root / case_id
        try:
            directory.mkdir()
            break
        except FileExistsError:
            continue
    else:
        raise CaseError(_("今天的 Case 序号已用完"), "sequence-exhausted")
    try:
        text = _render_new_case(project, args, fields, case_id, now())
    except CaseError:
        directory.rmdir()
        raise
    write_atomic(directory / "CASE.md", text)
    result = {"ok": True, "id": case_id, "path": project.relative(directory / "CASE.md"), "status": args.status}
    hints = sop_hints(project, "case.created", read_case(directory / "CASE.md")[0])
    if hints:
        result["sop_events"] = hints
    return result


def sop_hints(project: Project, event: str, data: dict) -> list[dict]:
    """哪些已启用的 SOP 在监听这个事件；尽力而为，SOP 目录有问题时不能让 Case 的写入失败。"""
    try:
        import sop as sop_tool  # noqa: PLC0415 - sop.py 反过来依赖本模块，延迟导入避免循环
        return sop_tool.event_hints(project.root, event, data)
    except Exception:  # noqa: BLE001
        return []


def summary(project: Project, directory: Path) -> dict:
    data, _body = read_case(directory / "CASE.md")
    return {"id": data.get("id", directory.name), "title": data.get("title", ""), "status": data.get("status", ""),
            "next_action": data.get("next_action", ""), "updated_at": data.get("updated_at", ""),
            "path": project.relative(directory / "CASE.md")}


def list_cases(project: Project, args: argparse.Namespace) -> dict:
    cases, invalid = [], []
    for directory in project.case_dirs():
        try:
            item = summary(project, directory)
        except (CaseError, OSError, UnicodeDecodeError) as exc:
            invalid.append({"path": project.relative(directory), "error": str(exc)})
            continue
        if args.status and item["status"] != args.status:
            continue
        if not args.all and not args.status and item["status"] in CLOSED:
            continue
        cases.append(item)
    cases.sort(key=lambda item: item["updated_at"], reverse=True)
    cases.sort(key=lambda item: STATUS_ORDER.get(item["status"], 99))
    return {"ok": True, "cases": cases, "invalid": invalid}


def append_section(body: str, heading: str, line: str, language: str = "zh") -> str:
    """Append a bullet under the section ``heading`` (either language's name, or a logical key), replacing the placeholder text.

    The file's own spelling of the heading wins; a missing section is created in ``language``."""
    marker = f"## {structure.resolve(body, heading, language)}\n"
    start = body.find(marker)
    if start < 0:
        return body.rstrip("\n") + f"\n\n{marker}\n- {line}\n"
    section_start = start + len(marker)
    next_heading = body.find("\n## ", section_start)
    end = len(body) if next_heading < 0 else next_heading + 1
    section = body[section_start:end].strip("\n")
    if section in structure.PLACEHOLDERS:
        section = ""
    section = (section + "\n" if section else "") + f"- {line}"
    return body[:section_start] + "\n" + section + "\n\n" + body[end:].lstrip("\n")


def plan_update(project: Project, args: argparse.Namespace) -> tuple[Path, str, str, dict]:
    """(path, current text, new text, result) for an update; writes nothing."""
    project.require_writable()
    directory = project.find(args.case)
    path = directory / "CASE.md"
    data, body = read_case(path)
    current = path.read_text(encoding="utf-8")
    expected_digest = getattr(args, "expect_sha256", None)
    if expected_digest and hashlib.sha256(current.encode("utf-8")).hexdigest() != expected_digest:
        raise CaseError(_("Case 在预览之后被修改过（内容摘要不一致），请重新读取"), "conflict")
    if args.expect_updated_at and data.get("updated_at") != args.expect_updated_at:
        raise CaseError(_("Case 已被其他人修改（updated_at 不一致），请重新读取"), "conflict")
    stamp = now()
    changed = []
    for flag, key in (("status", "status"), ("next_action", "next_action"), ("current_state", "current_state"),
                      ("waiting_for", "waiting_for"), ("resume_when", "resume_when"), ("title", "title")):
        value = getattr(args, flag)
        if value is not None:
            data[key] = clean(value, f"--{flag.replace('_', '-')}")
            changed.append(key)
    for flag, key in (("add_deliverable", "deliverables"), ("add_evidence", "evidence"), ("add_tag", "tags"),
                      ("add_alias", "aliases")):
        for value in getattr(args, flag):
            item = clean(value, f"--{flag.replace('_', '-')}", required=True)
            if item not in data.setdefault(key, []):
                data[key].append(item)
                changed.append(key)
    if getattr(args, "sensitivity", None):
        if data.get("sensitivity", "shared") != args.sensitivity:
            data["sensitivity"] = args.sensitivity
            changed.append("sensitivity")
    elif data.get("sensitivity") != "private" and _touches_private(project, directory, args):
        data["sensitivity"] = "private"  # 用到了私有目录里的内容：默认标为 private（要改回去用 --sensitivity shared）
        changed.append("sensitivity")
    if data.get("status") == "active" and not data.get("next_action"):
        raise CaseError(_("active 的 Case 需要 next_action"))
    if data.get("status") == "waiting" and not data.get("waiting_for"):
        raise CaseError(_("waiting 的 Case 需要 waiting_for"))
    language = project.language
    if "current_state" in changed:
        body = _replace_section(body, "current_state", data["current_state"], language)
    if "next_action" in changed:
        body = _replace_section(body, "next_action", data["next_action"] or structure.text("to_define", language), language)
    if args.log:
        changed.append(structure.resolve(body, "work_log", language))
        body = append_section(body, "work_log", f"{stamp} {clean(args.log, '--log', required=True)}", language)
    if args.decision:
        changed.append(structure.resolve(body, "decisions", language))
        body = append_section(body, "decisions", f"{stamp} {clean(args.decision, '--decision', required=True)}", language)
    if args.result:
        changed.append(structure.resolve(body, "result", language))
        body = _replace_section(body, "result", clean(args.result, "--result", required=True), language)
    if not changed:
        raise CaseError(_("没有要更新的内容"))
    data["updated_at"] = stamp
    errors = validate_schema(data, project.schema)
    if errors:
        raise CaseError(_("更新后不符合 Schema：") + _("；").join(errors), "schema")
    return path, current, render_frontmatter(data) + body, {
        "ok": True, "id": data["id"], "changed": sorted(set(changed)), "updated_at": stamp,
        "path": project.relative(path)}


def _touches_private(project: Project, directory: Path, args: argparse.Namespace) -> bool:
    """这次登记的交付物或证据是否在私有目录里（路径可以相对项目根，也可以相对 Case 目录）。"""
    dirs = private_paths.declared(project.root)
    if not dirs:
        return False
    for value in [*getattr(args, "add_deliverable", []), *getattr(args, "add_evidence", [])]:
        for candidate in (value, str(directory / value)):
            if private_paths.is_private(project.root, candidate, dirs):
                return True
    return False


def update(project: Project, args: argparse.Namespace) -> dict:
    path, current, text, result = plan_update(project, args)
    # Re-read right before replacing: a write that landed after planning must not be lost.
    if path.read_text(encoding="utf-8") != current:
        raise CaseError(_("Case 刚被其他人修改，请重新读取"), "conflict")
    was_closed = read_case(path)[0].get("status") in CLOSED
    write_atomic(path, text)
    data, _unused = read_case(path)
    if data.get("status") in CLOSED and not was_closed:
        hints = sop_hints(project, "case.closed", data)
        if hints:
            result["sop_events"] = hints
    return result


def _replace_section(body: str, heading: str, text: str, language: str = "zh") -> str:
    marker = f"## {structure.resolve(body, heading, language)}\n"
    start = body.find(marker)
    if start < 0:
        return body.rstrip("\n") + f"\n\n{marker}\n{text}\n"
    section_start = start + len(marker)
    next_heading = body.find("\n## ", section_start)
    end = len(body) if next_heading < 0 else next_heading + 1
    return body[:section_start] + "\n" + text + "\n\n" + body[end:].lstrip("\n")


def validate(project: Project, args: argparse.Namespace) -> dict:
    directories = [project.find(args.case)] if args.case else project.case_dirs()
    results, seen = [], {}
    for directory in directories:
        path = directory / "CASE.md"
        errors: list[str] = []
        try:
            data, body = read_case(path)
            errors = validate_schema(data, project.schema)
            if data.get("id") != directory.name:
                errors.append(_("id 与目录名不一致"))
            if data.get("status") == "active" and not data.get("next_action"):
                errors.append(_("active 的 Case 需要 next_action"))
            seen.setdefault(data.get("id"), []).append(project.relative(path))
        except FileNotFoundError:
            errors = [_("缺少 CASE.md")]
        except (CaseError, OSError, UnicodeDecodeError) as exc:
            errors = [str(exc)]
        results.append({"path": project.relative(path), "valid": not errors, "errors": errors})
    for case_id, paths in seen.items():
        if len(paths) > 1:
            for result in results:
                if result["path"] in paths:
                    result["valid"] = False
                    result["errors"].append(_("重复的 id：{0}").format(case_id))
    return {"ok": all(result["valid"] for result in results), "results": results}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=_("默认 Case 工具：在 case-workspace 项目里新建、列出、查看、更新与校验 Case。"))
    result.add_argument("--project", type=Path, default=Path.cwd())
    result.add_argument("--output", choices=("text", "json"), default="text")
    # The same options are accepted after the subcommand, where people tend to type them.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--project", type=Path, default=argparse.SUPPRESS)
    shared.add_argument("--output", choices=("text", "json"), default=argparse.SUPPRESS)
    sub = result.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[shared], help=help_text)

    new = command("new", _("新建 Case 目录与 CASE.md"))
    new.add_argument("--title", required=True)
    new.add_argument("--slug", required=True, help=_("目录名后缀：小写字母、数字与连字符"))
    new.add_argument("--goal", required=True)
    new.add_argument("--next-action")
    new.add_argument("--status", choices=("active", "captured"), default="active")
    new.add_argument("--done-when")
    new.add_argument("--source")
    new.add_argument("--current-state")
    new.add_argument("--tag", action="append", default=[])
    new.add_argument("--sensitivity", choices=("shared", "private"), default="shared",
                     help=_("用到私有目录（privacy.forbidden_default_reads，缺省 private/）里内容的 Case 用 private"))
    new.add_argument("--sop", help=_("这个 Case 是 SOP 的一次执行：<sop-id>@<版本>（通常由 sop.py run 填写）"))
    listing = command("list", _("列出 Case（默认不含已关闭）"))
    listing.add_argument("--status", choices=tuple(STATUS_ORDER))
    listing.add_argument("--all", action="store_true")
    show = command("show", _("查看一个 Case 的字段与正文"))
    show.add_argument("case")
    change = command("update", _("更新状态、下一动作，追加工作记录、决策、交付物与证据"))
    change.add_argument("case")
    change.add_argument("--status", choices=tuple(STATUS_ORDER))
    change.add_argument("--title")
    change.add_argument("--next-action")
    change.add_argument("--current-state")
    change.add_argument("--waiting-for")
    change.add_argument("--resume-when")
    change.add_argument("--result", help=_("替换「结果」段"))
    change.add_argument("--log", help=_("向「工作记录」追加一条"))
    change.add_argument("--decision", help=_("向「决策记录」追加一条"))
    change.add_argument("--add-deliverable", action="append", default=[])
    change.add_argument("--add-evidence", action="append", default=[])
    change.add_argument("--sensitivity", choices=("shared", "private"),
                        help=_("改 Case 的敏感度；登记私有目录里的交付物或证据时会自动改为 private"))
    change.add_argument("--add-tag", action="append", default=[])
    change.add_argument("--add-alias", action="append", default=[])
    change.add_argument("--expect-updated-at", help=_("只在 updated_at 等于该值时写入，避免覆盖并发修改"))
    change.add_argument("--expect-sha256", help=_("只在 CASE.md 内容摘要等于该值时写入（比 updated_at 更严格）"))
    check = command("validate", _("按 Schema 校验 Case"))
    check.add_argument("case", nargs="?")
    return result


def render_text(command: str, result: dict) -> str:
    if command == "list":
        lines = [f"{item['status']:9} {item['id']}  {item['title']}" + (_("\n          下一步：{0}").format(item['next_action'])
                                                                          if item["next_action"] else "")
                 for item in result["cases"]]
        lines += [f"invalid   {item['path']}: {item['error']}" for item in result["invalid"]]
        return "\n".join(lines) or _("没有 Case。")
    if command == "validate":
        lines = [f"{'ok' if item['valid'] else 'FAIL'}  {item['path']}" +
                 "".join(f"\n      {error}" for error in item["errors"]) for item in result["results"]]
        return "\n".join(lines) or _("没有 Case。")
    if command == "show":
        return result["content"]
    text = json.dumps(result, ensure_ascii=False, indent=2)
    for hint in result.get("sop_events") or []:
        text += _("\n命中流程 {0}（{1}）：{2}\n  是否按它执行由用户决定；确认后运行：{3}").format(hint['id'], hint['title'], hint['trigger'], hint['command'])
    return text


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        project = Project(args.project)
        if args.command == "new":
            result = create(project, args)
        elif args.command == "list":
            result = list_cases(project, args)
        elif args.command == "show":
            directory = project.find(args.case)
            data, _body = read_case(directory / "CASE.md")
            result = {"ok": True, "case": data, "path": project.relative(directory / "CASE.md"),
                      "content": (directory / "CASE.md").read_text(encoding="utf-8")}
        elif args.command == "update":
            result = update(project, args)
        else:
            result = validate(project, args)
    except CaseError as exc:
        payload = {"ok": False, "code": exc.code, "error": str(exc)}
        if args.output == "json":
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.output == "json"
          else render_text(args.command, result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
