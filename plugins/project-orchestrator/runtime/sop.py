#!/usr/bin/env python3
"""SOP（Standard Operating Procedure，标准作业流程）管理：一个 SOP 一个目录，索引实时生成；只依赖标准库。

    sop.py list / index / show <SOP> / validate [<SOP>]
    sop.py interview [<SOP>]            创建与配置时要和用户逐项谈清的事（编号问题清单）
    sop.py defaults [--set 键=值 …]      本项目的使用习惯（出错怎么办、中间产物放哪等），一次谈好，新 SOP 沿用
    sop.py preflight <SOP>              执行前检查所需的命令、模块、环境变量、技能、MCP 是否就绪
    sop.py new / activate / revise / retire / restore     创建与版本管理（默认只预览，--apply 写入）
    sop.py run <SOP> / step <Case> <n> / status <Case>    一次执行 = 一个 Case，逐步记录
    sop.py match <话> / due / events <事件>               触发：关键词预筛、到期、事件

SOP 是用户自己的步骤序列，不放宽任何授权：执行到写入、外发、删除类步骤，仍按用户当次授权。
一次执行的状态只记在 Case 里（Case 是唯一持久工作容器），SOP 目录里不保存执行状态。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import case as case_tool  # noqa: E402
import case_structure  # noqa: E402
import private_paths  # noqa: E402
import sop_structure as vocab  # noqa: E402
from i18n_orchestrator import _, N_  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = PLUGIN_ROOT / "sop-templates"
PROFILE = case_tool.PROFILE
DEFAULT_ROOT = "sops"
DEFAULTS_FILE = "defaults.json"
SNAPSHOTS = ".versions"
MAX_BYTES = 1024 * 1024
MAX_INDEX_ENTRIES = 30

ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)\Z")
REF_RE = re.compile(r"([a-z0-9]+(?:-[a-z0-9]+)*)@(\d+\.\d+\.\d+)\Z")
SCHEDULE_RE = re.compile(r"(daily|weekly:(mon|tue|wed|thu|fri|sat|sun)|monthly:([1-9]|1\d|2[0-8])|every:[1-9]\d{0,2}d)\Z")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
EVENTS = ("case.created", "case.closed")
PLACEHOLDER = vocab.PLACEHOLDERS[0]  # 中文占位标记；英文的 SOP 用 vocab.PLACEHOLDERS[1]，读取时两种都认

STATUSES = ("draft", "active", "retired")
SCALARS = ("schema_version", "id", "title", "status", "version", "description", "trigger", "exclude", "schedule",
           "confirm", "creates_case", "on_error", "max_retries", "on_missing_tool", "artifact_dir", "artifact_keep",
           "resume", "confirm_costly", "created", "verified_at", "review_after", "origin")
LISTS = ("aliases", "examples_hit", "examples_miss", "on_events", "event_tags", "outputs", "sources", "decided")
REQUIRED = ("schema_version", "id", "title", "status", "version", "description", "created")

# 用户习惯：每项给出可选值、缺省、以及创建时必须谈的问题。第一个标注推荐的是我们的倾向，用户可改。
CHOICES: dict[str, dict[str, Any]] = {
    "on_error": {"values": ("ask", "stop", "retry", "skip-step"), "default": "ask",
                 "labels": {"ask": N_("停下来告诉我，由我决定（推荐）"), "stop": N_("立即停止并记录现场，不再继续"),
                            "retry": N_("自动重试 max_retries 次，仍失败再问我"), "skip-step": N_("跳过该步继续，事后列出所有跳过的步骤")}},
    "max_retries": {"default": "2"},
    "on_missing_tool": {"values": ("ask", "stop", "skip-step", "alternative"), "default": "ask",
                        "labels": {"ask": N_("停下来，列出缺什么、怎么装，等我处理（推荐）"), "stop": N_("停止，不继续执行"),
                                   "skip-step": N_("跳过用到该工具的步骤"), "alternative": N_("改用我声明的替代方案；没有替代就问我")}},
    "artifact_dir": {"default": ""},
    "artifact_keep": {"values": ("all", "final-only", "on-error", "ask"), "default": "ask",
                      "labels": {"all": N_("全部保留"), "final-only": N_("成功后只保留最终产物，清理登记过的中间产物"),
                                 "on-error": N_("成功后清理中间产物，失败时保留以便排查"), "ask": N_("每次完成后问我（推荐）")}},
    "resume": {"values": ("from-failed-step", "restart", "ask"), "default": "ask",
               "labels": {"from-failed-step": N_("从失败的那一步继续，已完成的步骤不重做（推荐）"), "restart": N_("从头重做"),
                          "ask": N_("每次问我")}},
    "confirm_costly": {"values": ("ask", "never"), "default": "ask",
                       "labels": {"ask": N_("调用付费 API、下载大文件、预计耗时数分钟以上的步骤前先问我（推荐）"),
                                  "never": N_("不用问，直接执行")}},
    "confirm": {"values": ("ask", "auto"), "default": "ask",
                "labels": {"ask": N_("命中触发后先问我是否按它执行（推荐）"), "auto": N_("命中后直接执行（只适合没有外部副作用的流程）")}},
    "creates_case": {"values": ("always", "ask", "never"), "default": "always"},
}
# 创建时必须与用户确认的事项（编号顺序即提问顺序）。defaults 项表示可由项目习惯（defaults.json）代为回答。
DECISIONS = (
    ("trigger", None, N_("什么情况下应该用这个流程？请写：①触发场景（一句话）；②明确不该触发的相近场景；"
                      "③有没有固定周期（每天 / 每周几 / 每月几号 / 每 N 天），或由事件触发（新建 Case / 关闭 Case；"
                      "要不要只对带某些标签的 Case 触发，否则每个 Case 新建或关闭都会提示）；"
                      "④再给 1–2 句你平时会怎么说的话（用来校准识别）。")),
    ("requires", None, N_("这个流程用到哪些程序、Python 模块、脚本、模型、账号或 MCP？逐项确认：名称、做什么用、"
                        "没有时怎么安装、有没有替代方案、缺了怎么办。API 密钥只写环境变量名，不写密钥本身。")),
    ("on_error", ("on_error",), N_("某一步出错时怎么办？")),
    ("on_missing_tool", ("on_missing_tool",), N_("执行前发现缺程序、缺模型或缺账号时怎么办？（AI 不会在没有你同意时自行安装软件）")),
    ("artifacts", ("artifact_keep",), N_("中间产物（如下载的视频、转出的音频、临时文本）放在哪里、完成后怎么处理？"
                                       "缺省放在本次 Case 的 data/ 目录；体积很大的文件可以指定项目外的目录。")),
    ("resume", ("resume",), N_("中途失败后重新执行时，从哪里开始？")),
    ("confirm_costly", ("confirm_costly",), N_("遇到花钱、占用大量时间或网络流量的步骤，要不要先问你？")),
    ("scope", None, N_("这个流程会读取哪些事项或资料？是每次由你指定，还是固定范围（不要默认纳入全部）？会不会用到私有目录"
                    "（private/）里的内容？用到的话，每次读取前都会请你确认，执行记录自动标为 private，内容默认不进 git。"
                    "产物会放进哪个项目，那个项目会不会公开？")),
)
# 习惯项 -> 它所属的“创建时必须确认”事项；设置这些项等于用户确认了对应事项。
FIELD_DECISION = {"on_error": "on_error", "max_retries": "on_error", "on_missing_tool": "on_missing_tool",
                  "artifact_keep": "artifacts", "artifact_dir": "artifacts", "resume": "resume",
                  "confirm_costly": "confirm_costly"}
DECISION_OPTIONS = {"on_error": "on_error", "on_missing_tool": "on_missing_tool", "artifacts": "artifact_keep",
                    "resume": "resume", "confirm_costly": "confirm_costly"}

KIND_LABEL = {"command": N_("命令"), "module": N_("Python 模块"), "env": N_("环境变量"), "path": N_("路径"), "skill": N_("技能"),
              "mcp": "MCP", "manual": N_("手动确认")}
SECRET_PATTERNS = (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
                   re.compile(r"AKIA[0-9A-Z]{16}"), re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
                   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
                   re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\"']?[A-Za-z0-9/+_\-]{16,}"))
MARKS = {" ": N_("待办"), "x": N_("完成"), "-": N_("跳过"), "!": N_("失败")}
CHECK_RE = re.compile(r"^- \[( |x|-|!)\] (\d+)\. (.*)$")
SOP_HOME_ENV = "AGENTHELM_SOP_HOME"


class SopError(Exception):
    def __init__(self, message: str, code: str = "usage", next_steps: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.next_steps = next_steps or []


def fail(message: str, code: str = "usage", next_steps: list[str] | None = None) -> None:
    raise SopError(message, code, next_steps)


# ---------------------------------------------------------------- 路径与读写

def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def today() -> date:
    return datetime.now().astimezone().date()


def read_profile(project: Path) -> dict:
    path = project / PROFILE
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def inside(project: Path, relative: str, label: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        fail(_("{0} 必须是项目内的相对路径：{1}").format(label, relative))
    current = project
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            fail(_("{0} 不能经过符号链接：{1}").format(label, relative))
    return project / path


def project_root(project: Path) -> Path:
    section = read_profile(project).get("sop")
    declared = section.get("root") if isinstance(section, dict) else None
    return inside(project, declared or DEFAULT_ROOT, "sop.root")


def user_root() -> Path:
    if os.environ.get(SOP_HOME_ENV):
        return Path(os.environ[SOP_HOME_ENV]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "agenthelm" / "sops"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    case_tool.write_atomic(path, text)


def display(path: Path, project: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        home = Path.home()
        try:
            return "~/" + path.relative_to(home).as_posix()
        except ValueError:
            return str(path)


# ---------------------------------------------------------------- 文件格式

def parse_sop_text(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        fail(_("SOP.md 必须以 --- 开头"), "invalid")
    end = text.find("\n---\n", 3)
    if end < 0:
        fail(_("SOP.md 缺少 frontmatter 结束标记"), "invalid")
    try:
        meta = case_tool.parse_frontmatter(text[4:end])
    except case_tool.CaseError as exc:
        raise SopError(str(exc), "invalid") from exc
    return meta, text[end + 5:]


def as_text(value: Any) -> str:
    return "true" if value is True else "false" if value is False else str(value)


def render_sop(meta: dict, body: str) -> str:
    lines = ["---"]
    for key in SCALARS:
        if key in meta and (meta[key] != "" or key in REQUIRED):
            lines.append(f"{key}: {json.dumps(as_text(meta[key]), ensure_ascii=False)}")
    for key in LISTS:
        if key in meta:
            items = meta[key]
            lines.append(f"{key}: []" if not items else f"{key}:")
            lines.extend(f"  - {json.dumps(item, ensure_ascii=False)}" for item in items)
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def load_dir(directory: Path, scope: str) -> dict:
    sop = {"id": directory.name, "scope": scope, "dir": directory, "path": directory / "SOP.md",
           "meta": {}, "body": "", "text": "", "error": None}
    try:
        if sop["path"].is_symlink() or directory.is_symlink():
            fail(_("拒绝符号链接"), "invalid")
        raw = sop["path"].read_bytes()
        if len(raw) > MAX_BYTES:
            fail(_("SOP.md 超过 1 MiB"), "invalid")
        sop["text"] = raw.decode("utf-8")
        sop["meta"], sop["body"] = parse_sop_text(sop["text"])
    except FileNotFoundError:
        sop["error"] = _("缺少 SOP.md")
    except (SopError, UnicodeDecodeError, OSError) as exc:
        sop["error"] = str(exc)
    return sop


def load_all(project: Path | None, *, user: bool = True) -> list[dict]:
    """项目级在前；同 id 时项目级覆盖用户级（被覆盖的用户级记为 shadowed）。"""
    found: list[dict] = []
    roots = ([("project", project_root(project))] if project else []) + ([("user", user_root())] if user else [])
    seen: set[str] = set()
    for scope, root in roots:
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.name.startswith((".", "_")):
                continue
            sop = load_dir(directory, scope)
            sop["shadowed"] = directory.name in seen
            seen.add(directory.name)
            found.append(sop)
    return found


def find_sop(sops: list[dict], token: str) -> dict:
    wanted, _unused, _unused = token.partition("@")
    live = [sop for sop in sops if not sop["shadowed"]]
    exact = [sop for sop in live if sop["id"] == wanted]
    matches = exact or [sop for sop in live if wanted.lower() in sop["id"] or wanted in str(sop["meta"].get("title", ""))]
    if not matches:
        fail(_("没有找到 SOP：{0}").format(token), "not-found", [_("sop.py list 查看全部")])
    if len(matches) > 1:
        fail(_("有 {0} 个 SOP 匹配“{1}”").format(len(matches), token), "ambiguous", [sop["id"] for sop in matches[:10]])
    return matches[0]


# ---------------------------------------------------------------- 正文解析

def split_sections(body: str) -> dict[str, str]:
    result: dict[str, list[str]] = {}
    current: str | None = None
    fenced = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        match = None if fenced else re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = match.group(1)
            result.setdefault(current, [])
        elif current is not None:
            result[current].append(line)
    return {key: "\n".join(lines).strip("\n") for key, lines in result.items()}


def split_list(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[、,，;；]", value) if item.strip() and item.strip().lower() not in vocab.LIST_NONE]


def parse_steps(text: str, language: str = "zh") -> tuple[list[dict], list[str]]:
    steps: list[dict] = []
    problems: list[str] = []
    for line in text.splitlines():
        head = re.match(r"^(\d+)[.、)]\s*(.+?)\s*$", line)
        if head:
            steps.append({"n": int(head.group(1)), "title": head.group(2), "tools": [], "inputs": "", "intermediates": [],
                          "finals": [], "checkpoint": "", "on_failure": "", "confirm": False, "note": ""})
            continue
        if not line.strip():
            continue
        if not steps:
            continue  # 第一个编号步骤之前的说明文字不参与解析
        sub = re.match(r"^\s+[-*]\s*([^：:]{1,32})[：:]\s*(.*?)\s*$", line)
        if not sub:
            steps[-1]["note"] = (steps[-1]["note"] + " " + line.strip()).strip()
            continue
        key = vocab.step_key(sub.group(1))
        if key is None:
            problems.append(_("步骤 {0} 有未知字段“{1}”（可用：{2}）").format(
                steps[-1]['n'], sub.group(1), _('、').join(vocab.name(vocab.STEP_KEYS, item, language) for item in vocab.STEP_KEYS)))
        elif key in ("tools", "intermediates", "finals"):
            steps[-1][key] = split_list(sub.group(2))
        elif key == "confirm":
            steps[-1][key] = sub.group(2).strip().lower() in vocab.TRUE_WORDS
        else:
            steps[-1][key] = sub.group(2)
    for index, step in enumerate(steps, 1):
        if step["n"] != index:
            problems.append(_("步骤编号应从 1 连续递增：第 {0} 个步骤写成了 {1}").format(index, step['n']))
            break
    return steps, problems


def parse_requirements(text: str, language: str = "zh") -> tuple[list[dict], list[str]]:
    reqs: list[dict] = []
    problems: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.rstrip("。.").lower() in vocab.NONE_WORDS:
            continue
        if not stripped.startswith("- "):
            continue
        parts = [part.strip() for part in re.split(r"\s*｜\s*|\s+\|\s+", stripped[2:]) if part.strip()]
        head = re.match(r"^([^：:]{1,32})[：:]\s*(.+)$", parts[0]) if parts else None
        if not head or vocab.kind_key(head.group(1)) is None:
            problems.append(_("无法识别的前置条件：{0}（写法：- 命令：名称｜用途：…｜安装：…｜缺失时：询问用户）").format(stripped[:40]))
            continue
        req = {"kind": vocab.kind_key(head.group(1)), "name": head.group(2).strip().strip("`"), "purpose": "", "install": "",
               "alternative": "", "if_missing": "", "version": "", "when": ""}
        for part in parts[1:]:
            pair = re.match(r"^([^：:]{1,32})[：:]\s*(.*)$", part)
            key = vocab.req_key(pair.group(1)) if pair else None
            if key is None:
                problems.append(_("{0}：未知字段“{1}”（可用：{2}）").format(
                    req['name'], part[:20], _('、').join(vocab.name(vocab.REQ_KEYS, item, language) for item in vocab.REQ_KEYS)))
            elif key == "if_missing":
                value = vocab.missing_value(pair.group(2))
                if value is None:
                    problems.append(_("{0}：缺失时只能写 {1}").format(
                        req['name'], _('、').join(vocab.name(vocab.MISSING, item, language) for item in vocab.MISSING)))
                else:
                    req[key] = value
            else:
                req[key] = pair.group(2).strip()
        reqs.append(req)
    return reqs, problems


def parsed(sop: dict, default: str = "zh") -> dict:
    sections = split_sections(sop["body"])
    language = vocab.detect(sop["body"], default)
    steps, step_problems = parse_steps(vocab.section_text(sections, "steps"), language)
    reqs, req_problems = parse_requirements(vocab.section_text(sections, "requires"), language)
    return {"language": language, "sections": sections, "steps": steps, "step_problems": step_problems, "requires": reqs,
            "require_problems": req_problems}


# ---------------------------------------------------------------- 习惯与缺口

def defaults_path(project: Path | None) -> Path | None:
    return project_root(project) / DEFAULTS_FILE if project else None


def read_defaults(project: Path | None) -> dict:
    path = defaults_path(project)
    if not path or not path.is_file():
        return {"conventions": {}, "decided_at": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SopError(_("{0} 无法解析：{1}").format(DEFAULTS_FILE, exc), "invalid") from exc
    conventions = data.get("conventions") if isinstance(data, dict) else None
    if not isinstance(conventions, dict):
        fail(_("{0} 的 conventions 必须是对象").format(DEFAULTS_FILE), "invalid")
    return {"conventions": {key: str(value) for key, value in conventions.items() if key in CHOICES},
            "decided_at": str(data.get("decided_at", ""))}


def check_choice(key: str, value: str) -> None:
    spec = CHOICES.get(key)
    if spec is None:
        fail(_("未知的习惯项：{0}（可用：{1}）").format(key, _('、').join(CHOICES)))
    if "values" in spec and value not in spec["values"]:
        fail(_("{0} 只能是 {1}：{2}").format(key, ' / '.join(spec['values']), value))
    if key == "max_retries" and not re.fullmatch(r"[1-9]", value):
        fail(_("max_retries 只能是 1–9"))
    if key == "artifact_dir" and ("\n" in value or "\x00" in value):
        fail(_("artifact_dir 只能是单行路径"))


def effective(meta: dict, defaults: dict) -> dict[str, dict]:
    """每项习惯的最终取值与来源：sop（该 SOP 自己确认的）/ project-default（项目习惯）/ builtin（未确认的内置缺省）。"""
    decided = set(meta.get("decided") or [])
    own = FIELD_DECISION
    result = {}
    for key, spec in CHOICES.items():
        if key in own and own[key] in decided and meta.get(key) not in (None, ""):
            result[key] = {"value": as_text(meta[key]), "source": "sop"}
        elif key not in own and meta.get(key) not in (None, ""):
            result[key] = {"value": as_text(meta[key]), "source": "sop"}
        elif key in defaults["conventions"]:
            result[key] = {"value": defaults["conventions"][key], "source": "project-default"}
        elif key in own and own[key] in decided:
            result[key] = {"value": spec["default"], "source": "sop"}
        else:
            result[key] = {"value": spec["default"], "source": "builtin"}
    return result


def gaps(meta: dict, defaults: dict) -> list[dict]:
    decided = set(meta.get("decided") or [])
    found = []
    for key, fields, question in DECISIONS:
        if key in decided or (fields and all(field in defaults["conventions"] for field in fields)):
            continue
        found.append({"key": key, "question": _(question), "options": options_for(key)})
    return found


def options_for(key: str) -> list[dict]:
    field = DECISION_OPTIONS.get(key)
    if not field:
        return []
    spec = CHOICES[field]
    return [{"value": value, "label": _(spec["labels"][value]), "recommended": "推荐" in spec["labels"][value]}
            for value in spec["values"]]


# ---------------------------------------------------------------- 校验

def issue(level: str, code: str, message: str) -> dict:
    return {"level": level, "code": code, "message": message}


def check_sop(sop: dict, defaults: dict, *, strict: bool | None = None, default_language: str = "zh") -> list[dict]:
    """strict 缺省按 status：active 严格；draft / retired 只提示。"""
    if sop["error"]:
        return [issue("error", "unreadable", sop["error"])]
    meta = sop["meta"]
    strict = meta.get("status") == "active" if strict is None else strict
    level = "error" if strict else "warn"
    found: list[dict] = []
    add = lambda lvl, code, message: found.append(issue(lvl, code, message))  # noqa: E731

    allowed = set(SCALARS) | set(LISTS)
    unknown = sorted(set(meta) - allowed)
    if unknown:
        add("error", "unknown-field", _("未知字段：{0}").format(_('、').join(unknown)))
    for key in REQUIRED:
        if not str(meta.get(key, "")).strip():
            add("error", "missing-field", _("缺少字段 {0}").format(key))
    if meta.get("schema_version") not in (None, "1.0"):
        add("error", "schema-version", _("schema_version 必须为 1.0"))
    if meta.get("id") and (not ID_RE.fullmatch(str(meta["id"])) or meta["id"] != sop["id"]):
        add("error", "id-mismatch", _("id 必须是小写字母、数字与连字符，且与目录名一致（目录：{0}）").format(sop['id']))
    if meta.get("status") and meta["status"] not in STATUSES:
        add("error", "invalid-status", _("status 必须为：{0}").format(_('、').join(STATUSES)))
    if meta.get("version") and not VERSION_RE.fullmatch(str(meta["version"])):
        add("error", "invalid-version", _("version 必须是 主.次.修订，例如 1.0.0"))
    for key in ("confirm", "creates_case", "on_error", "on_missing_tool", "artifact_keep", "resume", "confirm_costly"):
        value = meta.get(key)
        if value not in (None, "") and value not in CHOICES[key]["values"]:
            add("error", "invalid-choice", _("{0} 只能是：{1}").format(key, ' / '.join(CHOICES[key]['values'])))
    if meta.get("max_retries") not in (None, "") and not re.fullmatch(r"[1-9]", str(meta["max_retries"])):
        add("error", "invalid-choice", _("max_retries 只能是 1–9"))
    if meta.get("schedule") and not SCHEDULE_RE.fullmatch(str(meta["schedule"])):
        add("error", "invalid-schedule", _("schedule 写法：daily / weekly:mon..sun / monthly:1..28 / every:N天（如 every:14d）"))
    for event in meta.get("on_events") or []:
        if event not in EVENTS:
            add("error", "invalid-event", _("on_events 只支持：{0}").format(_('、').join(EVENTS)))
    if meta.get("event_tags") and not meta.get("on_events"):
        add("warn", "event-tags-without-event", _("设了 event_tags 但没有 on_events：标签过滤不会生效"))
    for key in ("created", "verified_at", "review_after"):
        if meta.get(key):
            try:
                date.fromisoformat(str(meta[key]))
            except ValueError:
                add("error", "invalid-date", _("{0} 必须为 YYYY-MM-DD").format(key))
    for item in meta.get("decided") or []:
        if item not in {key for key, _unused, _unused in DECISIONS}:
            add("error", "invalid-decided", _("decided 里有未知项：{0}").format(item))
    if meta.get("schedule") and meta.get("creates_case") == "never":
        add("warn", "schedule-without-case", _("设了周期但 creates_case=never：没有执行记录，无法判断是否到期"))
    if len(str(meta.get("description", ""))) > 80:
        add("warn", "description-long", _("description 超过 80 字符：索引里每个 SOP 只占一行"))
    if meta.get("review_after"):
        try:
            if date.fromisoformat(str(meta["review_after"])) < today():
                add("warn", "review-due", _("已超过复核日期 {0}：核对后更新 verified_at / review_after").format(meta['review_after']))
        except ValueError:
            pass
    if strict and not str(meta.get("trigger", "")).strip():
        add("error", "missing-trigger", _("active 的 SOP 必须写 trigger（什么情况下用它）"))

    info = parsed(sop, default_language)
    for key in vocab.STRICT_SECTIONS:
        if not vocab.section_text(info["sections"], key).strip():
            add(level, "missing-section", _("缺少或为空的小节：## {0}").format(vocab.name(vocab.SECTIONS, key, info["language"])))
    for problem in info["step_problems"]:
        add(level, "step-format", problem)
    for problem in info["require_problems"]:
        add(level, "requirement-format", problem)
    if strict and not info["steps"]:
        add("error", "no-steps", _("## {0} 下至少要有一个编号步骤").format(vocab.name(vocab.SECTIONS, "steps", info["language"])))
    names = {req["name"].lower() for req in info["requires"]}
    for req in info["requires"]:
        if req["alternative"] and req["alternative"].lower() not in names:
            add("error", "unknown-alternative", _("{0} 的替代项“{1}”没有在前置条件里声明").format(req['name'], req['alternative']))
    for step in info["steps"]:
        for tool in step["tools"]:
            if tool.lower() not in names:
                add(level, "undeclared-tool", _("步骤 {0} 用到“{1}”，但前置条件里没有声明（会让执行前检查漏掉它）").format(step['n'], tool))
    if vocab.count_placeholders(sop["text"]):
        marker = next(item for item in vocab.PLACEHOLDERS if item in sop["text"])
        add(level, "placeholder", _("还有 {0} 处{1}需要填写").format(vocab.count_placeholders(sop["text"]), marker))
    if any(pattern.search(sop["text"]) for pattern in SECRET_PATTERNS):
        add("error", "secret", _("SOP 里疑似写入了密钥或口令：只写环境变量名，不写密钥本身"))
    if strict:
        for gap in gaps(meta, defaults):
            add("error", "undecided", _("尚未与用户确认：{0}（sop.py interview {1} 查看问题）").format(gap['key'], sop['id']))
    snapshot = sop["dir"] / SNAPSHOTS / f"{meta.get('version', '')}.md"
    if snapshot.is_file():
        if snapshot.read_text(encoding="utf-8") != sop["text"]:
            add("warn", "version-not-bumped", _("内容比版本快照新，但版本号没升：用 sop.py revise --bump … 记录这次修改"))
    elif meta.get("status") == "active":
        add("warn", "no-snapshot", _("active 的 SOP 缺少当前版本的快照：运行 sop.py revise 或 activate 补上"))
    for name in sorted(path.name for path in sop["dir"].iterdir() if path.is_file()) if sop["dir"].is_dir() else []:
        if name != "SOP.md" and re.match(r"^SOP\b.*\.md$", name):
            add("warn", "conflict-copy", _("{0} 像是同步盘留下的冲突副本：合并到 SOP.md 后删除").format(name))
    return found


def check_all(sops: list[dict], defaults: dict, project: Path | None) -> list[dict]:
    results = []
    active_alias: dict[str, str] = {}
    language = vocab.language_of(read_profile(project)) if project else "zh"
    for sop in sops:
        issues = check_sop(sop, defaults, default_language=language)
        if project and not sop["error"]:
            for source in sop["meta"].get("sources") or []:
                try:
                    if not inside(project, source, "sources").exists():
                        issues.append(issue("warn", "missing-source", _("来源路径不存在：{0}").format(source)))
                except SopError as exc:
                    issues.append(issue("warn", "invalid-source", str(exc)))
        if sop["meta"].get("status") == "active" and not sop["shadowed"]:
            for name in [str(sop["meta"].get("title", "")), *(sop["meta"].get("aliases") or [])]:
                other = active_alias.setdefault(name.lower(), sop["id"]) if name else sop["id"]
                if other != sop["id"]:
                    issues.append(issue("warn", "trigger-overlap", _("“{0}”同时是 {1} 的名称或别名，识别时会混淆").format(name, other)))
        if sop["shadowed"]:
            issues.append(issue("warn", "shadowed", _("同 id 的项目级 SOP 覆盖了这份用户级 SOP")))
        results.append({"id": sop["id"], "scope": sop["scope"], "status": sop["meta"].get("status", "?"),
                        "issues": issues, "valid": not any(item["level"] == "error" for item in issues)})
    if sum(1 for sop in sops if sop["meta"].get("status") == "active" and not sop["shadowed"]) > MAX_INDEX_ENTRIES:
        results.append({"id": "(index)", "scope": "", "status": "", "valid": True,
                        "issues": [issue("warn", "index-budget", _("active 的 SOP 超过 {0} 个：索引会占用过多上下文，建议退役不用的").format(MAX_INDEX_ENTRIES))]})
    return results


# ---------------------------------------------------------------- 触发：索引、匹配、周期

def index_line(sop: dict, due: dict | None = None) -> str:
    meta = sop["meta"]
    parts = [_("- `{0}`@{1}：{2}").format(sop['id'], meta.get('version', '?'), meta.get('description', ''))]
    if meta.get("trigger"):
        parts.append(_("触发：{0}").format(meta['trigger']))
    if meta.get("exclude"):
        parts.append(_("不触发：{0}").format(meta['exclude']))
    if meta.get("schedule"):
        parts.append(_("周期：{0}").format(meta['schedule']))
    if meta.get("on_events"):
        tags = meta.get("event_tags") or []
        parts.append(_("事件：") + _("、").join(meta["on_events"]) + (_("（仅标签 {0}）").format(_('、').join(tags)) if tags else ""))
    if (meta.get("confirm") or "ask") == "auto":
        parts.append(_("命中即执行"))
    if due and due.get("due"):
        parts.append(_("已到期"))
    return _("｜").join(parts)


def next_occurrence(schedule: str, base: date) -> date:
    kind, _unused, arg = schedule.partition(":")
    if kind == "daily":
        return base + timedelta(days=1)
    if kind == "every":
        return base + timedelta(days=int(arg.rstrip("d")))
    if kind == "weekly":
        day = base + timedelta(days=1)
        while day.weekday() != WEEKDAYS.index(arg):
            day += timedelta(days=1)
        return day
    day = base + timedelta(days=1)  # monthly
    while day.day != int(arg):
        day += timedelta(days=1)
    return day


def last_runs(project: Path) -> tuple[dict[str, dict], str | None]:
    try:
        cases = case_tool.Project(project)
    except case_tool.CaseError as exc:
        return {}, str(exc)
    runs: dict[str, dict] = {}
    for directory in cases.case_dirs():
        try:
            data, _unused = case_tool.read_case(directory / "CASE.md")
        except (case_tool.CaseError, OSError, UnicodeDecodeError):
            continue
        sop_id, _unused, version = str(data.get("sop") or "").partition("@")
        if not sop_id:
            continue
        stamp = str(data.get("created_at", ""))[:10]
        if sop_id not in runs or stamp > runs[sop_id]["date"]:
            runs[sop_id] = {"date": stamp, "case": data.get("id", directory.name), "status": data.get("status", ""),
                            "version": version}
    return runs, None


def due_info(sop: dict, runs: dict[str, dict], now: date) -> dict | None:
    schedule = sop["meta"].get("schedule")
    if not schedule or sop["meta"].get("status") != "active":
        return None
    last = runs.get(sop["id"])
    try:
        base = date.fromisoformat(last["date"] if last else str(sop["meta"].get("created", "")))
    except ValueError:
        return None
    following = next_occurrence(schedule, base)
    return {"id": sop["id"], "title": sop["meta"].get("title", ""), "schedule": schedule,
            "last_run": last["date"] if last else "", "last_case": last["case"] if last else "",
            "next": following.isoformat(), "due": following <= now,
            "overdue_days": max(0, (now - following).days)}


def event_hints(project: Path, event: str, case_data: dict, sops: list[dict] | None = None) -> list[dict]:
    """某个 Case 发生事件时，哪些已启用的 SOP 在监听。SOP 自己的执行 Case 不再触发事件，避免循环；
    SOP 声明了 event_tags 时，只对带其中任一标签的 Case 触发。命中只是提示，是否执行默认先问用户。"""
    if case_data.get("sop"):
        return []
    tags = set(case_data.get("tags") or [])
    hints = []
    for sop in sops if sops is not None else load_all(project):
        meta = sop["meta"]
        if sop["shadowed"] or meta.get("status") != "active" or event not in (meta.get("on_events") or []):
            continue
        wanted = set(meta.get("event_tags") or [])
        if wanted and not wanted & tags:
            continue
        case_id = str(case_data.get("id", ""))
        hints.append({"id": sop["id"], "title": meta.get("title", ""), "version": meta.get("version", ""),
                      "trigger": meta.get("trigger", ""), "confirm": meta.get("confirm") or "ask", "event": event,
                      "case": case_id, "command": f"sop run {sop['id']} --event {event} --event-case {case_id}"})
    return hints


def bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text.lower())
    return {compact[i:i + 2] for i in range(len(compact) - 1)} or {compact}


def match_sops(sops: list[dict], text: str) -> list[dict]:
    """关键词预筛：名称 / 别名命中，或与示例话术足够相似。最终是否触发由 Agent 对照 trigger / exclude 判断。"""
    lowered = text.lower()
    found = []
    for sop in sops:
        if sop["meta"].get("status") != "active" or sop["shadowed"]:
            continue
        meta, reasons = sop["meta"], []
        for name in [str(meta.get("title", "")), sop["id"], *(meta.get("aliases") or [])]:
            if name and name.lower() in lowered:
                reasons.append(_("提到“{0}”").format(name))
        for example in meta.get("examples_hit") or []:
            score = len(bigrams(text) & bigrams(example)) / max(1, len(bigrams(text) | bigrams(example)))
            if score >= 0.45:
                reasons.append(_("与示例话术“{0}”相似（{1:.2f}）").format(example, score))
        excluded = [example for example in (meta.get("examples_miss") or [])
                    if len(bigrams(text) & bigrams(example)) / max(1, len(bigrams(text) | bigrams(example))) >= 0.6]
        if reasons:
            found.append({"id": sop["id"], "title": meta.get("title", ""), "version": meta.get("version", ""),
                          "reasons": reasons, "maybe_excluded": bool(excluded) or bool(meta.get("exclude")),
                          "exclude": meta.get("exclude", ""), "confirm": meta.get("confirm") or "ask"})
    return found


# ---------------------------------------------------------------- 执行前检查

def lookup_skill(name: str, project: Path) -> str | None:
    home = Path.home()
    for base in (project / ".agents" / "skills", home / ".claude" / "skills", home / ".agents" / "skills",
                 home / ".codex" / "skills", home / ".cursor" / "skills"):
        if (base / name / "SKILL.md").is_file():
            return str(base / name)
    return None


def lookup_mcp(name: str, project: Path) -> str | None:
    for path in (project / ".agents" / "mcp.json", Path.home() / ".claude.json", project / ".mcp.json"):
        try:
            servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(servers, dict) and name in servers:
            return str(path)
    return None


def check_requirement(req: dict, project: Path) -> tuple[str, str]:
    """(状态, 说明)。ok 已确认可用；missing 确认缺失；unknown 本工具无法确认，必须由 Agent 或用户核对。值从不输出。"""
    kind, name = req["kind"], req["name"]
    if kind == "command":
        if "/" in name:
            path = (project / name) if not Path(name).expanduser().is_absolute() else Path(name).expanduser()
            return ("ok", str(path)) if path.is_file() and os.access(path, os.X_OK) else ("missing", _("不存在或不可执行：{0}").format(name))
        found = shutil.which(name)
        return ("ok", found) if found else ("missing", _("PATH 上找不到"))
    if kind == "module":
        try:
            found_spec = importlib.util.find_spec(name.split(".")[0])
        except (ImportError, ValueError):
            found_spec = None
        return ("ok", _("运行本工具的 Python 里可导入；若流程用别的 Python 环境，请人工确认")) if found_spec \
            else ("unknown", _("运行本工具的 Python 里找不到；若流程用别的 Python 环境，请到那个环境里确认"))
    if kind == "env":
        return ("ok", _("已设置（值不显示）")) if os.environ.get(name) else ("missing", _("当前环境没有设置或为空"))
    if kind == "path":
        path = Path(name).expanduser()
        path = path if path.is_absolute() else project / path
        return ("ok", str(path)) if path.exists() else ("missing", _("不存在：{0}").format(path))
    if kind == "skill":
        found = lookup_skill(name, project)
        return ("ok", found) if found else ("unknown", _("常见位置没找到；请对照当前会话的可用技能列表确认"))
    if kind == "mcp":
        found = lookup_mcp(name, project)
        return ("ok", found) if found else ("unknown", _("配置文件里没找到；请对照当前会话的可用工具确认"))
    return "unknown", _("需要用户确认")


def preflight(sop: dict, project: Path, defaults: dict) -> dict:
    info = parsed(sop)
    habit = effective(sop["meta"], defaults)
    used: dict[str, list[int]] = {}
    for step in info["steps"]:
        for tool in step["tools"]:
            used.setdefault(tool.lower(), []).append(step["n"])
    rows = []
    for req in info["requires"]:
        status, detail = check_requirement(req, project)
        rows.append({**req, "label": _(KIND_LABEL[req["kind"]]), "status": status, "detail": detail,
                     "if_missing": req["if_missing"] or habit["on_missing_tool"]["value"],
                     "used_by_steps": used.get(req["name"].lower(), [])})
    by_name = {row["name"].lower(): row for row in rows}
    primaries: dict[str, list[dict]] = {}
    for row in rows:
        if row["alternative"]:
            primaries.setdefault(row["alternative"].lower(), []).append(row)
    for row in rows:
        row["optional"] = row["status"] != "ok" and bool(primaries.get(row["name"].lower())) \
            and all(item["status"] == "ok" for item in primaries[row["name"].lower()])
        alternative = by_name.get(row["alternative"].lower()) if row["alternative"] else None
        row["alternative_status"] = alternative["status"] if alternative else ""
        # 只有用户约定“缺失时使用替代”，且替代项就绪，才算已解决；否则仍要按约定询问或停止。
        row["resolved_by"] = alternative["name"] if row["status"] != "ok" and row["if_missing"] == "alternative" \
            and alternative and alternative["status"] == "ok" else ""
    open_rows = [row for row in rows if row["status"] != "ok" and not row["optional"] and not row["resolved_by"]]
    # 带“仅当”的前置条件只在本次输入符合条件时才需要：工具无法判断输入，列出来由 Agent 对照本次输入判断。
    conditional = [row for row in open_rows if row["when"]]
    blocking = [row for row in open_rows if not row["when"]]
    counts = {"ok": sum(row["status"] == "ok" for row in rows),
              "missing": sum(row["status"] == "missing" for row in blocking),
              "unknown": sum(row["status"] == "unknown" for row in blocking),
              "conditional": len(conditional)}
    return {"id": sop["id"], "version": sop["meta"].get("version", ""), "requires": rows, "counts": counts,
            "ready": not blocking, "conditional": [{"name": row["name"], "label": row["label"], "when": row["when"],
                                                    "status": row["status"], "install": row["install"]} for row in conditional],
            "on_missing_tool": habit["on_missing_tool"]["value"]}


# ---------------------------------------------------------------- 写入计划

def apply_writes(writes: list[dict]) -> list[str]:
    """writes: {path, content, expect(None 表示必须不存在，str 表示当前内容的 sha256)}；写前逐个核对，写后读回。"""
    for item in writes:
        path: Path = item["path"]
        exists = path.exists()
        if item["expect"] is None and exists:
            fail(_("已存在，拒绝覆盖：{0}").format(path.name), "conflict")
        if item["expect"] is not None and (not exists or sha(path.read_text(encoding="utf-8")) != item["expect"]):
            fail(_("{0} 在预览之后被修改过，请重新预览").format(path.name), "conflict")
    for item in writes:
        write_text(item["path"], item["content"])
    for item in writes:
        if item["path"].read_text(encoding="utf-8") != item["content"]:
            fail(_("写入后读回不一致：{0}").format(item['path'].name), "failed")
    return [str(item["path"]) for item in writes]


def plan_result(project: Path, writes: list[dict], apply: bool, extra: dict) -> dict:
    result = {"applied": False, "writes": [{"path": display(item["path"], project),
                                            "action": "create" if item["expect"] is None else "update",
                                            "sha256_before": item["expect"]} for item in writes], **extra}
    if apply:
        apply_writes(writes)
        result["applied"] = True
        result["verify"] = _("已写入并读回核对：{0} 个文件").format(len(writes))
    return result


def snapshot_write(sop_dir: Path, version: str, text: str) -> dict | None:
    path = sop_dir / SNAPSHOTS / f"{version}.md"
    if path.exists():
        return None if path.read_text(encoding="utf-8") == text else {"path": path, "content": text, "expect": sha(path.read_text(encoding="utf-8"))}
    return {"path": path, "content": text, "expect": None}


def bump(version: str, part: str) -> str:
    major, minor, patch = map(int, VERSION_RE.fullmatch(version).groups())
    return {"major": f"{major + 1}.0.0", "minor": f"{major}.{minor + 1}.0", "patch": f"{major}.{minor}.{patch + 1}"}[part]


def append_revision(body: str, line: str, default: str = "zh") -> str:
    """The revision log lives under the heading the file itself uses (detected from its headings)."""
    language = vocab.detect(body, default)
    return case_tool.append_section(body, vocab.name(vocab.SECTIONS, "revisions", language), line, language)


def revision_line(body: str, version: str, note: str, default: str = "zh") -> str:
    return vocab.text("revision_line", vocab.detect(body, default), date=today().isoformat(), version=version, note=note)


# ---------------------------------------------------------------- 命令

class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.project = args.project.expanduser().resolve()
        self.args = args
        self.defaults = read_defaults(self.project)
        self.language = vocab.language_of(read_profile(self.project))

    def sops(self, *, user: bool = True) -> list[dict]:
        return load_all(self.project, user=user)

    def one(self, token: str) -> dict:
        return find_sop(self.sops(), token)


def cmd_list(ctx: Context) -> tuple[int, dict, list[str]]:
    sops = ctx.sops()
    runs, _unused = last_runs(ctx.project)
    rows = []
    for sop in sops:
        meta = sop["meta"]
        if meta.get("status") == "retired" and not ctx.args.all:
            continue
        if ctx.args.scope and sop["scope"] != ctx.args.scope:
            continue
        due = due_info(sop, runs, today())
        rows.append({"id": sop["id"], "title": meta.get("title", ""), "status": meta.get("status", "?"),
                     "version": meta.get("version", ""), "scope": sop["scope"], "schedule": meta.get("schedule", ""),
                     "due": bool(due and due["due"]), "shadowed": sop["shadowed"], "error": sop["error"]})
    lines = [_("{0:8} {1}@{2}  {3}（{4}）").format(row['status'], row['id'], row['version'], row['title'], _('项目') if row['scope'] == 'project' else _('用户'))
             + (_("  周期 {0}").format(row['schedule']) if row["schedule"] else "") + (_("  已到期") if row["due"] else "")
             + (_("  读取失败：{0}").format(row['error']) if row["error"] else "") for row in rows]
    return 0, {"sops": rows}, lines or [_("还没有 SOP。用 sop new 创建；先看 sop interview 要谈清哪些事。")]


def cmd_index(ctx: Context) -> tuple[int, dict, list[str]]:
    runs, _unused = last_runs(ctx.project)
    sops = ctx.sops()
    active = [sop for sop in sops if sop["meta"].get("status") == "active" and not sop["shadowed"] and not sop["error"]]
    lines = [index_line(sop, due_info(sop, runs, today())) for sop in active]
    drafts = sum(1 for sop in sops if sop["meta"].get("status") == "draft")
    return 0, {"entries": [{"id": sop["id"], "line": line} for sop, line in zip(active, lines)], "drafts": drafts}, \
        lines or [_("没有 active 的 SOP。")]


def cmd_show(ctx: Context) -> tuple[int, dict, list[str]]:
    sop = ctx.one(ctx.args.sop)
    if sop["error"]:
        fail(_("{0} 无法读取：{1}").format(sop['id'], sop['error']), "invalid")
    info = parsed(sop, ctx.language)
    habit = effective(sop["meta"], ctx.defaults)
    data = {"id": sop["id"], "scope": sop["scope"], "path": display(sop["path"], ctx.project), "meta": sop["meta"],
            "steps": info["steps"], "requires": info["requires"], "conventions": habit,
            "gaps": gaps(sop["meta"], ctx.defaults), "issues": check_sop(sop, ctx.defaults, default_language=ctx.language),
            "private": private_paths.mentions(sop["text"], private_paths.declared(ctx.project))}
    meta = sop["meta"]
    lines = [_("{0}（{1}@{2}，{3}，{4}级）").format(meta.get('title'), sop['id'], meta.get('version'), meta.get('status'), _('项目') if sop['scope'] == 'project' else _('用户')),
             _("文件：{0}").format(data['path']), _("触发：{0}").format(meta.get('trigger', '')), _("不触发：{0}").format(meta.get('exclude', ''))]
    lines += ["", _("步骤：")] + [f"  {step['n']}. {step['title']}" + (_("（工具：{0}）").format(_('、').join(step['tools'])) if step["tools"] else "")
                              for step in info["steps"]]
    lines += ["", _("前置条件：")] + [_("  {0}：{1}").format(_(KIND_LABEL[req['kind']]), req['name'])
                                  + (_("（{0}）").format(req['purpose']) if req["purpose"] else "") for req in info["requires"]]
    lines += ["", _("执行约定（来源）：")] + [_("  {0} = {1}（{2}）").format(key, item['value'] or _('（Case 的 data/）'), item['source'])
                                          for key, item in habit.items() if key in ("on_error", "on_missing_tool", "artifact_dir", "artifact_keep", "resume", "confirm_costly")]
    if data["gaps"]:
        lines += ["", _("尚有 {0} 项没有与用户确认：").format(len(data['gaps'])) + _("、").join(gap["key"] for gap in data["gaps"])]
    return 0, data, lines


def cmd_validate(ctx: Context) -> tuple[int, dict, list[str]]:
    sops = [ctx.one(ctx.args.sop)] if ctx.args.sop else ctx.sops()
    results = check_all(sops, ctx.defaults, ctx.project)
    lines = []
    for result in results:
        mark = "ok  " if result["valid"] else "FAIL"
        lines.append(_("{0} {1}（{2}）").format(mark, result['id'], result['status']))
        lines += [f"      {item['level']}: {item['message']}" for item in result["issues"]]
    bad = [result for result in results if not result["valid"]]
    return (1 if bad else 0), {"ok": not bad, "results": results}, lines or [_("还没有 SOP。")]


def cmd_interview(ctx: Context) -> tuple[int, dict, list[str]]:
    """没有 SOP 参数：完整问题清单；给了 SOP：只列还没谈的。编号问题，Agent 应打包成一次提问。"""
    if ctx.args.sop:
        sop = ctx.one(ctx.args.sop)
        open_keys = {gap["key"] for gap in gaps(sop["meta"], ctx.defaults)}
        habit = effective(sop["meta"], ctx.defaults)
    else:
        sop, open_keys, habit = None, {key for key, _unused, _unused in DECISIONS}, effective({}, ctx.defaults)
    items = []
    for number, (key, fields, question) in enumerate(DECISIONS, 1):
        field = DECISION_OPTIONS.get(key)
        current = habit[field] if field else None
        covered = bool(fields) and all(field in ctx.defaults["conventions"] for field in fields)
        if sop is not None and key not in open_keys:
            continue
        items.append({"no": number, "key": key, "question": _(question), "options": options_for(key),
                      "current": current["value"] if current else None,
                      "project_default": covered, "needs_answer": key in open_keys and not (covered and sop is None)})
    lines = []
    for item in items:
        lines.append(f"{item['no']}. {item['question']}")
        for option in item["options"]:
            lines.append(_("     - {0}：{1}").format(option['value'], option['label']))
        if item["project_default"]:
            lines.append(_("     （本项目习惯已定为 {0}；沿用则无需再问）").format(item['current']))
    return (1 if sop is not None and items else 0), {"questions": items, "sop": sop["id"] if sop else None}, \
        lines or [_("该 SOP 的所有事项都已确认。")]


def cmd_defaults(ctx: Context) -> tuple[int, dict, list[str]]:
    current = ctx.defaults
    updates: dict[str, str] = {}
    for pair in ctx.args.set or []:
        key, separator, value = pair.partition("=")
        if not separator:
            fail(_("--set 需要 键=值：{0}").format(pair))
        check_choice(key.strip(), value.strip())
        updates[key.strip()] = value.strip()
    merged = {**current["conventions"], **updates}
    lines = [f"{key} = {value or _('（Case 的 data/ 目录）')}" for key, value in merged.items()] or [_("本项目还没有记录使用习惯。")]
    if not updates:
        return 0, {"conventions": merged, "decided_at": current["decided_at"]}, lines
    path = defaults_path(ctx.project)
    content = json.dumps({"schema_version": "1.0", "decided_at": today().isoformat(), "conventions": merged},
                         ensure_ascii=False, indent=2) + "\n"
    expect = sha(path.read_text(encoding="utf-8")) if path.is_file() else None
    result = plan_result(ctx.project, [{"path": path, "content": content, "expect": expect}], ctx.args.apply,
                         {"conventions": merged})
    return 0, result, lines + ["", _("已写入。新 SOP 创建时沿用这些习惯。") if result["applied"] else _("以上为预览，没有写入。确认后加 --apply。")]


def cmd_preflight(ctx: Context) -> tuple[int, dict, list[str]]:
    sop = ctx.one(ctx.args.sop)
    report = preflight(sop, ctx.project, ctx.defaults)
    lines = [_("{0}@{1} 执行前检查：{2} 项就绪，{3} 项缺失，{4} 项需要核对").format(sop['id'], report['version'], report['counts']['ok'], report['counts']['missing'], report['counts']['unknown']) + (_("，{0} 项条件项待判断").format(report['counts']['conditional']) if report["counts"]["conditional"] else "")]
    for row in report["requires"]:
        tag = _("不需要") if row["optional"] else _("已由替代项解决") if row["resolved_by"] else \
            _("条件项·{0}").format({'missing': _('缺失'), 'unknown': _('待核对')}[row['status']]) if row["when"] and row["status"] != "ok" else \
            {"ok": _("就绪"), "missing": _("缺失"), "unknown": _("待核对")}[row["status"]]
        lines.append(_("  [{0}] {1}：{2}").format(tag, row['label'], row['name'])
                     + (_("（{0}）").format(row['purpose']) if row["purpose"] else "") + (f" — {row['detail']}" if row["status"] != "ok" else "")
                     + (_("｜仅当：{0}").format(row['when']) if row["when"] else ""))
        if row["status"] != "ok" and not row["optional"] and not row["resolved_by"]:
            if row["install"]:
                lines.append(_("        安装/获取方式：{0}").format(row['install']))
            if row["alternative"]:
                lines.append(_("        替代方案：{0}").format(row['alternative']))
            lines.append(_("        缺失时的约定：{0}").format(row['if_missing']))
    if report["counts"]["conditional"]:
        lines.append(_("其中 {0} 项是条件项：只有本次输入符合“仅当”的描述才需要，请对照本次输入判断；符合就按缺失处理。").format(report['counts']['conditional']))
    if not report["ready"]:
        lines.append(_("需要先处理缺失与待核对项；不要在用户未同意时自行安装软件。"))
    return (0 if report["ready"] else 1), report, lines


def template_source(name: str, language: str = "zh") -> tuple[str, dict[str, str]]:
    """The sample's SOP.md and its extra files in the given language.

    ``SOP.en.md`` and ``<name>.en.<ext>`` are the English variants of ``SOP.md`` and ``<name>.<ext>``; a sample
    without an English variant falls back to the Chinese one."""
    base = TEMPLATE_DIR / name
    if not ID_RE.fullmatch(name) or not (base / "SOP.md").is_file():
        names = sorted(path.name for path in TEMPLATE_DIR.iterdir() if (path / "SOP.md").is_file()) if TEMPLATE_DIR.is_dir() else []
        fail(_("没有这个样例：{0}").format(name), "not-found", [_("可用：") + _("、").join(names)])
    english = language == "en"

    def variant(relative: Path) -> Path:
        if not english:
            return relative
        candidate = relative.with_name(relative.stem + ".en" + relative.suffix)
        return candidate if (base / candidate).is_file() else relative

    extra = {}
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        if not path.is_file() or relative.name in ("SOP.md", "SOP.en.md") or relative.stem.endswith(".en"):
            continue
        extra[relative.as_posix()] = (base / variant(relative)).read_text(encoding="utf-8")
    return (base / variant(Path("SOP.md"))).read_text(encoding="utf-8"), extra


def cmd_new(ctx: Context) -> tuple[int, dict, list[str]]:
    args = ctx.args
    slug = (args.slug or "").strip()
    if not ID_RE.fullmatch(slug):
        fail(_("--slug 只能用小写字母、数字和连字符，例如 weekly-review"))
    root = user_root() if args.scope == "user" else project_root(ctx.project)
    target = root / slug
    if target.exists():
        fail(_("已存在：{0}").format(display(target, ctx.project)), "conflict")
    extra: dict[str, str] = {}
    stamp = today().isoformat()
    language = args.language or ctx.language
    if args.from_file:
        text = Path(args.from_file).expanduser().read_text(encoding="utf-8")
        meta, body = parse_sop_text(text)
    elif args.from_case:
        meta, body = parse_sop_text(template_source("blank", language)[0])
        meta, body = from_case(ctx, args.from_case, meta, body, language)
    else:
        text, extra = template_source(args.template, language)
        meta, body = parse_sop_text(text)
    previous = str(meta.get("title") or "")
    title = args.title or (previous if previous and "{{" not in previous else slug)
    meta.update({"schema_version": "1.0", "id": slug, "title": title, "status": "draft", "created": stamp,
                 "version": str(meta.get("version") or "1.0.0")})
    meta.pop("verified_at", None)
    meta["decided"] = []
    meta.setdefault("origin", f"template:{args.template}" if not (args.from_file or args.from_case) else "")
    def fill(value: str) -> str:
        return value.replace("{{title}}", title).replace("{{id}}", slug).replace("{{date}}", stamp)

    body = fill(body)
    text = render_sop(meta, body)
    writes = [{"path": target / "SOP.md", "content": text, "expect": None}]
    writes += [{"path": target / relative, "content": fill(content), "expect": None} for relative, content in extra.items()]
    issues = check_sop({"id": slug, "scope": args.scope, "dir": target, "path": target / "SOP.md", "meta": meta,
                        "body": body, "text": text, "error": None, "shadowed": False}, ctx.defaults, strict=False, default_language=language)
    result = plan_result(ctx.project, writes, args.apply, {"id": slug, "status": "draft", "issues": issues,
                                                           "next": [_("sop.py interview {0}（谈清 {1} 项）").format(slug, len(gaps(meta, ctx.defaults))),
                                                                    _("填完后 sop.py validate，再 sop.py activate")]})
    lines = [_("草稿 {0}（{1} 级）→ {2}").format(slug, args.scope, display(target / 'SOP.md', ctx.project))] + \
            [f"  {item['level']}: {item['message']}" for item in issues] + \
            ["", result.get("verify") or _("以上为预览，没有写入。确认后加 --apply。草稿状态不会被触发。")]
    return 0, result, lines


def from_case(ctx: Context, token: str, meta: dict, body: str, language: str) -> tuple[dict, str]:
    try:
        cases = case_tool.Project(ctx.project)
        directory = cases.find(token)
        data, case_body = case_tool.read_case(directory / "CASE.md")
    except case_tool.CaseError as exc:
        raise SopError(_("读不了 Case：{0}").format(exc), "unavailable") from exc
    heading = case_structure.existing_heading(case_body, "work_log") or case_structure.heading("work_log", language)
    marker = f"## {heading}\n"
    start = case_body.find(marker)
    end = case_body.find("\n## ", start + len(marker)) if start >= 0 else -1
    created = vocab.TEXTS["case_created_marker"]
    records = [re.sub(r"^\d{4}-\d{2}-\d{2}T\S+\s+", "", line[2:].strip()) for line in
               (case_body[start + len(marker):None if end < 0 else end].splitlines() if start >= 0 else []) if line.startswith("- ")]
    placeholder = vocab.PLACEHOLDERS[vocab.LANGUAGES.index(language)]
    steps = [vocab.text("from_case_step", language, index=index, text=text, p=placeholder, case=data.get("id"))
             for index, text in enumerate([r for r in records if not any(item in r for item in created)], 1)]
    meta["description"] = vocab.text("from_case_description", language, p=placeholder, case=data.get("id"))
    meta["sources"] = [cases.relative(directory / "CASE.md")]
    meta["origin"] = f"case:{data.get('id')}"
    sections = split_sections(body)
    chosen = {key: vocab.section_text(sections, key) or placeholder for key in vocab.SECTIONS}
    chosen["steps"] = "\n".join(steps) or f"1. {placeholder}"
    return meta, "# {{title}}\n\n" + "".join(f"## {vocab.name(vocab.SECTIONS, key, language)}\n\n{chosen[key]}\n\n" for key in vocab.SECTIONS)


def set_status_plan(ctx: Context, sop: dict, status: str, note: str) -> dict:
    meta = dict(sop["meta"])
    meta["status"] = status
    if status == "active":
        meta["verified_at"] = today().isoformat()
    body = append_revision(sop["body"], revision_line(sop["body"], str(meta["version"]), note, ctx.language), ctx.language)
    text = render_sop(meta, body)
    writes = [{"path": sop["path"], "content": text, "expect": sha(sop["text"])}]
    snapshot = snapshot_write(sop["dir"], str(meta["version"]), text)
    if snapshot and status == "active":
        writes.append(snapshot)
    return {"writes": writes, "meta": meta, "text": text}


def cmd_activate(ctx: Context) -> tuple[int, dict, list[str]]:
    sop = ctx.one(ctx.args.sop)
    if sop["error"]:
        fail(_("无法读取：{0}").format(sop['error']), "invalid")
    if sop["meta"].get("status") == "active":
        fail(_("已经是 active"), "conflict")
    probe = dict(sop, meta={**sop["meta"], "status": "active"})
    blockers = [item for item in check_sop(probe, ctx.defaults, strict=True) if item["level"] == "error"]
    if blockers:
        lines = [_("{0} 还不能启用：").format(sop['id'])] + [f"  - {item['message']}" for item in blockers]
        return 1, {"id": sop["id"], "applied": False, "blockers": blockers}, lines
    plan = set_status_plan(ctx, sop, "active", vocab.text("note_activated", vocab.detect(sop["body"], ctx.language)))
    result = plan_result(ctx.project, plan["writes"], ctx.args.apply, {"id": sop["id"], "status": "active"})
    return 0, result, [f"{_('已启用') if result['applied'] else _('将启用')} {sop['id']}@{sop['meta']['version']}"] + \
        ([] if result["applied"] else [_("以上为预览，没有写入。确认后加 --apply。")])


def cmd_retire(ctx: Context) -> tuple[int, dict, list[str]]:
    sop = ctx.one(ctx.args.sop)
    target = "retired" if ctx.args.command == "retire" else "active"
    if sop["meta"].get("status") == target:
        fail(_("已经是 {0}").format(target), "conflict")
    if target == "active":
        probe = dict(sop, meta={**sop["meta"], "status": "active"})
        blockers = [item for item in check_sop(probe, ctx.defaults, strict=True) if item["level"] == "error"]
        if blockers:
            return 1, {"id": sop["id"], "applied": False, "blockers": blockers}, \
                [_("{0} 不能恢复为 active：").format(sop['id'])] + [f"  - {item['message']}" for item in blockers]
    language = vocab.detect(sop["body"], ctx.language)
    plan = set_status_plan(ctx, sop, target, ctx.args.note or vocab.text("note_retired" if target == "retired" else "note_restored", language))
    result = plan_result(ctx.project, plan["writes"], ctx.args.apply, {"id": sop["id"], "status": target})
    return 0, result, [(_("已退役 {0}") if result["applied"] else _("将退役 {0}")).format(sop["id"]) if target == "retired"
                      else (_("已恢复 {0}") if result["applied"] else _("将恢复 {0}")).format(sop["id"])] + \
        ([] if result["applied"] else [_("以上为预览，没有写入。确认后加 --apply。")])


def cmd_revise(ctx: Context) -> tuple[int, dict, list[str]]:
    """你改完 SOP.md 之后，用它登记新版本：升版本号、写修订记录、存当前内容的快照。"""
    sop = ctx.one(ctx.args.sop)
    if sop["error"]:
        fail(_("无法读取：{0}").format(sop['error']), "invalid")
    if not ctx.args.note:
        fail(_("revise 需要 --note 说明这次改了什么、为什么"))
    version = bump(str(sop["meta"]["version"]), ctx.args.bump)
    meta = {**sop["meta"], "version": version, "verified_at": today().isoformat()}
    body = append_revision(sop["body"], revision_line(sop["body"], version, ctx.args.note, ctx.language), ctx.language)
    text = render_sop(meta, body)
    writes = [{"path": sop["path"], "content": text, "expect": sha(sop["text"])}]
    snapshot = snapshot_write(sop["dir"], version, text)
    if snapshot:
        writes.append(snapshot)
    old_snapshot = sop["dir"] / SNAPSHOTS / f"{sop['meta']['version']}.md"
    if not old_snapshot.exists() and sop["meta"].get("status") == "active":
        writes.append({"path": old_snapshot, "content": sop["text"], "expect": None})
    result = plan_result(ctx.project, writes, ctx.args.apply, {"id": sop["id"], "from": sop["meta"]["version"], "to": version})
    return 0, result, [_("{0} {1} → {2}（{3}）：{4}").format(sop['id'], sop['meta']['version'], version, ctx.args.bump, ctx.args.note)] + \
        ([result["verify"]] if result["applied"] else [_("以上为预览，没有写入。确认后加 --apply。")])


def cmd_decide(ctx: Context) -> tuple[int, dict, list[str]]:
    """记录用户已经确认的事项：--confirm trigger / requires 表示该部分内容用户看过并认可；--set 键=值 同时视为确认对应事项。"""
    sop = ctx.one(ctx.args.sop)
    if sop["error"]:
        fail(_("无法读取：{0}").format(sop['error']), "invalid")
    meta = dict(sop["meta"])
    decided = list(meta.get("decided") or [])
    valid = {key for key, _unused, _unused in DECISIONS}
    for key in ctx.args.confirm or []:
        if key not in valid:
            fail(_("--confirm 只能是：{0}").format(_('、').join(sorted(valid))))
        decided.append(key)
    for pair in ctx.args.set or []:
        key, separator, value = pair.partition("=")
        if not separator:
            fail(_("--set 需要 键=值：{0}").format(pair))
        check_choice(key.strip(), value.strip())
        meta[key.strip()] = value.strip()
        if key.strip() in FIELD_DECISION:
            decided.append(FIELD_DECISION[key.strip()])
    meta["decided"] = sorted(set(decided), key=[key for key, _unused, _unused in DECISIONS].index)
    text = render_sop(meta, sop["body"])
    remaining = [gap["key"] for gap in gaps(meta, ctx.defaults)]
    result = plan_result(ctx.project, [{"path": sop["path"], "content": text, "expect": sha(sop["text"])}], ctx.args.apply,
                         {"id": sop["id"], "decided": meta["decided"], "remaining": remaining})
    return 0, result, [_("{0} 已确认：{1}；还差：{2}").format(sop['id'], _('、').join(meta['decided']) or _('（无）'), _('、').join(remaining) or _('无'))] + \
        ([result["verify"]] if result["applied"] else [_("以上为预览，没有写入。确认后加 --apply。")])


def cmd_match(ctx: Context) -> tuple[int, dict, list[str]]:
    found = match_sops(ctx.sops(), " ".join(ctx.args.text))
    lines = [_("{0}@{1}（{2}）").format(item['id'], item['version'], _('；').join(item['reasons']))
             + (_(" 注意不触发条件：{0}").format(item['exclude']) if item["exclude"] else "") for item in found]
    return (0 if found else 1), {"matches": found, "note": _("仅为关键词预筛；是否真的触发由 Agent 对照 trigger / exclude 判断，默认先问用户。")}, \
        lines or [_("没有关键词命中的 SOP（语义上是否相关仍由 Agent 判断）。")]


def cmd_due(ctx: Context) -> tuple[int, dict, list[str]]:
    runs, problem = last_runs(ctx.project)
    now = today()
    rows = [info for sop in ctx.sops() if not sop["shadowed"] and (info := due_info(sop, runs, now))]
    due = [row for row in rows if row["due"]]
    lines = [_("{0} {1}（{2}，{3}）：上次 {4}，下次 {5}").format(_('已到期') if row['due'] else _('未到期'), row['id'], row['title'], row['schedule'], row['last_run'] or _('从未执行'), row['next']) + (_("，已超 {0} 天").format(row['overdue_days']) if row["overdue_days"] else "")
             for row in rows]
    if problem:
        lines.append(_("注意：读不到执行记录（{0}），按 SOP 创建日期计算。").format(problem))
    return 0, {"due": due, "schedules": rows, "today": now.isoformat(), "problem": problem}, lines or [_("没有设置周期的 active SOP。")]


def cmd_events(ctx: Context) -> tuple[int, dict, list[str]]:
    if ctx.args.event not in EVENTS:
        fail(_("事件只支持：{0}").format(_('、').join(EVENTS)))
    if ctx.args.case:
        data = read_event_case(ctx, ctx.args.case)
        hints = event_hints(ctx.project, ctx.args.event, data, ctx.sops())
        note = _("这个 Case 本身是 SOP 的一次执行，不再触发事件。") if data.get("sop") else ""
        lines = [_("{0}：{1}（运行：{2}）").format(hint['id'], hint['trigger'], hint['command']) for hint in hints]
        return 0, {"event": ctx.args.event, "case": data.get("id"), "sops": hints, "note": note}, \
            lines or [note or _("没有 SOP 对这个 Case 的该事件感兴趣。")]
    found = [sop for sop in ctx.sops() if sop["meta"].get("status") == "active" and not sop["shadowed"]
             and ctx.args.event in (sop["meta"].get("on_events") or [])]
    rows = [{"id": sop["id"], "title": sop["meta"].get("title", ""), "trigger": sop["meta"].get("trigger", ""),
             "event_tags": sop["meta"].get("event_tags") or []} for sop in found]
    return 0, {"event": ctx.args.event, "sops": rows}, \
        [_("{0}：{1}").format(row['id'], row['trigger']) + (_("（仅标签 {0}）").format(_('、').join(row['event_tags'])) if row["event_tags"] else "")
         for row in rows] or [_("没有 SOP 监听这个事件。")]


def read_event_case(ctx: Context, token: str) -> dict:
    try:
        cases = case_tool.Project(ctx.project)
        data, _unused = case_tool.read_case(cases.find(token) / "CASE.md")
    except case_tool.CaseError as exc:
        raise SopError(_("读不了 Case：{0}").format(exc), "not-found") from exc
    return data


# ---------------------------------------------------------------- 执行：一次执行 = 一个 Case

def checklist_text(sop: dict, info: dict, habit: dict, params: list[str], runtime_note: str,
                   private_hits: list[str] | None = None, language: str = "zh") -> str:
    def t(key: str, **fields: object) -> str:
        return vocab.text(key, language, **fields)

    version = sop["meta"]["version"]
    keep = habit["artifact_keep"]["value"]
    place = habit["artifact_dir"]["value"] or t("cl_data_dir")
    joiner = t("list_join")
    lines = ["## " + vocab.name(vocab.CASE_SECTIONS, "checklist", language), "", t("cl_source", id=sop["id"], version=version, note=runtime_note), "",
             t("cl_conventions"),
             t("cl_on_error") + habit["on_error"]["value"] + (t("cl_retries", n=habit["max_retries"]["value"]) if habit["on_error"]["value"] == "retry" else ""),
             t("cl_on_missing") + habit["on_missing_tool"]["value"], t("cl_artifacts_label") + t("cl_artifacts", place=place, keep=keep),
             t("cl_resume") + habit["resume"]["value"], t("cl_costly") + habit["confirm_costly"]["value"]]
    if private_hits:
        lines.append(t("cl_private", dirs=joiner.join(private_hits)))
    lines.append("")
    if params:
        lines += [t("cl_params")] + [f"- {item}" for item in params] + [""]
    lines += [t("cl_steps")] + [f"- [ ] {step['n']}. {step['title']}" + (t("cl_tools", tools=joiner.join(step["tools"])) if step["tools"] else "")
                               for step in info["steps"]]
    return "\n".join(lines)


def insert_before(body: str, heading: str, text: str) -> str:
    """Insert ``text`` before the section whose logical key (or heading) is ``heading``, in either language."""
    try:
        key = case_structure.key_of(heading)
    except KeyError:
        key = None
    found = case_structure.existing_heading(body, key) if key else None
    marker = f"## {found or heading}\n"
    position = body.find(marker)
    block = text.rstrip("\n") + "\n\n"
    return body.rstrip("\n") + "\n\n" + block if position < 0 else body[:position] + block + body[position:]


def cmd_run(ctx: Context) -> tuple[int, dict, list[str]]:
    sop = ctx.one(ctx.args.sop)
    status = sop["meta"].get("status")
    if sop["error"]:
        fail(_("无法读取：{0}").format(sop['error']), "invalid")
    if status != "active" and not ctx.args.trial:
        fail(_("{0} 是 {1}，不能正式执行").format(sop['id'], status), "conflict", [_("草稿试跑请加 --trial（Case 会带 sop-trial 标签）"), _("正式使用先 sop.py activate")])
    if sop["meta"].get("creates_case") == "never":
        fail(_("这个 SOP 声明了 creates_case=never，没有执行记录可建"), "conflict")
    info = parsed(sop, ctx.language)
    if not info["steps"]:
        fail(_("SOP 没有可执行的步骤"), "invalid")
    language = ctx.language  # 执行 Case 用项目语言写；SOP 文件自己的语言只影响怎么解析它
    habit = effective(sop["meta"], ctx.defaults)
    report = preflight(sop, ctx.project, ctx.defaults)
    private_dirs = private_paths.declared(ctx.project)
    private_hits = private_paths.mentions(sop["text"], private_dirs)
    private_warnings = [_("{0} 尚未被 git 忽略：先运行 sop private --apply").format(item)
                        for item in private_hits if private_paths.git_state(ctx.project, item)["ignored"] is False]
    version = str(sop["meta"]["version"])
    existing = sop["dir"] / SNAPSHOTS / f"{version}.md"
    if status == "active" and existing.is_file() and existing.read_text(encoding="utf-8") != sop["text"]:
        fail(_("{0}@{1} 的内容比版本快照新，但版本号没升").format(sop['id'], version), "conflict",
             [_("先 sop.py revise {0} --bump patch --note …，再执行").format(sop['id'])])
    snapshot = snapshot_write(sop["dir"], version, sop["text"])
    note = vocab.text("snapshot_note", ctx.language, path=display(sop["dir"] / SNAPSHOTS / (version + ".md"), ctx.project))
    event_note = ""
    params = list(ctx.args.param or [])
    if ctx.args.event:
        if ctx.args.event not in (sop["meta"].get("on_events") or []):
            fail(_("{0} 没有声明监听 {1}").format(sop['id'], ctx.args.event), "conflict", [_("在 SOP 的 on_events 里声明后用 sop.py revise 登记")])
        if not ctx.args.event_case:
            fail(_("--event 需要同时给出 --event-case（触发它的 Case）"))
        trigger = read_event_case(ctx, ctx.args.event_case)
        if trigger.get("sop"):
            fail(_("SOP 自己的执行 Case 不能再触发事件"), "conflict")
        event_note = vocab.text("event_note", language, event=ctx.args.event, id=trigger.get("id"))
        params.insert(0, vocab.text("cl_event_param", language, event=ctx.args.event, id=trigger.get("id"), title=trigger.get("title", "")))
    goal = (vocab.section_text(info["sections"], "purpose").strip().splitlines() or [sop["meta"].get("title", sop["id"])])[0]
    done_when = vocab.text("list_join", language).replace("、", "；").replace(", ", "; ").join(
        line.strip("- ").strip() for line in vocab.section_text(info["sections"], "done").splitlines() if line.strip())[:300]
    title = ctx.args.title or f"{sop['meta'].get('title', sop['id'])} {today().isoformat()}"
    case_args = argparse.Namespace(title=title, slug=sop["id"], goal=goal, next_action=vocab.text("next_first", language, title=info["steps"][0]["title"]),
                                   status="active", done_when=done_when,
                                   source=f"SOP {sop['id']}@{version}" + (vocab.text("source_event", language, note=event_note) if event_note else ""),
                                   current_state=vocab.text("progress0", language, total=len(info["steps"])),
                                   tag=["sop", *(["sop-trial"] if status != "active" else [])], sop=f"{sop['id']}@{version}",
                                   sensitivity="private" if private_hits else "shared")
    try:
        cases = case_tool.Project(ctx.project)
        case_id, case_text = case_tool.plan_create(cases, case_args)
    except case_tool.CaseError as exc:
        raise SopError(_("不能建立执行记录：{0}").format(exc), "unavailable", [_("执行记录需要项目启用 case-workspace（Profile work.mode）")]) from exc
    section = checklist_text(sop, info, habit, params, note, private_hits, language)
    preview = {"case_id": case_id, "title": title, "steps": [step["title"] for step in info["steps"]],
               "preflight": {"ready": report["ready"], "counts": report["counts"]}, "snapshot": bool(snapshot),
               "checklist": section, "private": private_hits, "private_warnings": private_warnings}
    lines = [_("将建立 Case：{0}（{1}）").format(case_id, title), "", section, "", _("中间产物：执行中用 sop.py step … --artifact 路径:说明 登记。"),
             _("执行前检查：{0} 项就绪，{1} 项缺失，{2} 项待核对").format(report['counts']['ok'], report['counts']['missing'], report['counts']['unknown'])
             + ("" if report["ready"] else _("（先处理：sop.py preflight ") + sop["id"] + _("）"))]
    if private_hits:
        lines.append(_("这个流程会用到私有目录 {0}：执行 Case 将标为 private，读取前须你逐次确认。").format(_('、').join(private_hits)))
    lines += [_("注意：{0}").format(item) for item in private_warnings]
    if not ctx.args.apply:
        return 0, {"applied": False, **preview}, lines + [_("以上为预览，没有写入。确认后加 --apply。")]
    if snapshot:
        apply_writes([snapshot])
    created = case_tool.create(cases, case_args)
    path = ctx.project / created["path"]
    data, body = case_tool.read_case(path)
    intermediate = vocab.name(vocab.CASE_SECTIONS, "intermediate", language)
    body = insert_before(body, "blockers", section + f"\n\n## {intermediate}\n\n" + vocab.text("empty_intermediates", language))
    body = case_tool.append_section(body, "work_log", vocab.text(
        "log_started", language, stamp=case_tool.now(), id=sop["id"], version=version,
        event=vocab.text("log_started_event", language, note=event_note) if event_note else "",
        ok=report["counts"]["ok"], missing=report["counts"]["missing"], unknown=report["counts"]["unknown"]), language)
    case_tool.write_atomic(path, case_tool.render_frontmatter(data) + body)
    return 0, {"applied": True, **preview, "case_id": created["id"], "path": created["path"]}, \
        lines + [_("已建立：{0}").format(created['path'])]


def read_run(ctx: Context, token: str) -> tuple[Path, dict, str, list[dict]]:
    try:
        cases = case_tool.Project(ctx.project)
        path = cases.find(token) / "CASE.md"
        data, body = case_tool.read_case(path)
    except case_tool.CaseError as exc:
        raise SopError(str(exc), "not-found") from exc
    sections = split_sections(body)
    section = next((sections[name] for name in vocab.checklist_headings() if name in sections), "")
    steps = []
    for line in section.splitlines():
        match = CHECK_RE.match(line)
        if match:
            steps.append({"n": int(match.group(2)), "mark": match.group(1), "state": MARKS[match.group(1)], "text": match.group(3)})
    if not steps:
        fail(_("{0} 不是 SOP 执行记录（没有「SOP 执行清单」）").format(data.get('id', token)), "not-found")
    return path, data, body, steps


def case_artifacts_heading(body: str, language: str) -> str:
    """The heading of the intermediate-artifacts section in a run Case, as the Case itself spells it."""
    for name in vocab.CASE_SECTIONS["intermediate"]:
        if f"## {name}\n" in body or body.rstrip().endswith(f"## {name}"):
            return name
    return vocab.name(vocab.CASE_SECTIONS, "intermediate", language)


def section_value(body: str, text_key: str) -> str:
    """The value of a ``- label: value`` line in the run checklist, whichever language the Case is written in."""
    sections = split_sections(body)
    section = next((sections[name] for name in vocab.checklist_headings() if name in sections), "")
    for line in section.splitlines():
        for prefix in vocab.prefixes(text_key):
            if line.startswith(prefix):
                return line[len(prefix):].strip()
    return ""


def cmd_step(ctx: Context) -> tuple[int, dict, list[str]]:
    args = ctx.args
    path, data, body, steps = read_run(ctx, args.case)
    language = vocab.detect_case(body, ctx.language)
    current = path.read_text(encoding="utf-8")
    if args.expect_sha256 and sha(current) != args.expect_sha256:
        fail(_("Case 在预览之后被修改过，请重新读取"), "conflict")
    wanted = next((step for step in steps if step["n"] == args.n), None)
    if wanted is None:
        fail(_("没有步骤 {0}（共 {1} 步）").format(args.n, len(steps)), "not-found")
    flags = [name for name in ("done", "skip", "fail", "pending") if getattr(args, name)]
    if len(flags) != 1:
        fail(_("--done / --skip / --fail / --pending 必须且只能给一个"))
    state = flags[0]
    if state in ("skip", "fail") and not (args.note or "").strip():
        fail(_("--skip 和 --fail 必须用 --note 写明原因"))
    mark = {"done": "x", "skip": "-", "fail": "!", "pending": " "}[state]
    label = vocab.text("state_" + state, language)
    shown = {"done": _("完成"), "skip": _("跳过"), "fail": _("失败"), "pending": _("重置为待办")}[state]
    body = body.replace(f"- [{wanted['mark']}] {wanted['n']}. {wanted['text']}", f"- [{mark}] {wanted['n']}. {wanted['text']}", 1)
    stamp = case_tool.now()
    note = vocab.text("note_join", language, note=args.note.strip()) if args.note else ""
    body = case_tool.append_section(body, "work_log", vocab.text(
        "log_step", language, stamp=stamp, n=args.n, title=vocab.strip_tools(wanted["text"]), label=label, note=note), language)
    intermediate = case_artifacts_heading(body, language)
    for item in args.artifact or []:
        location, _unused, what = item.partition(":")
        body = case_tool.append_section(body, intermediate, vocab.text("artifact_line", language, n=args.n, location=location.strip())
                                        + (vocab.text("artifact_what", language, what=what.strip()) if what.strip() else ""), language)
    states = {step["n"]: step["mark"] for step in steps}
    states[args.n] = mark
    pending = [step for step in steps if states[step["n"]] == " "]
    failed = [step for step in steps if states[step["n"]] == "!"]
    habit = section_value(body, "cl_on_error") or "ask"
    if failed:
        next_action = vocab.text("next_failed", language, n=failed[0]["n"], habit=habit) \
            + (vocab.text("note_join", language, note=args.note.strip()) if args.note and state == "fail" else "")
    elif pending:
        next_action = vocab.text("next_step", language, n=pending[0]["n"], title=vocab.strip_tools(pending[0]["text"]))
    else:
        next_action = vocab.text("next_done", language)
    finished = sum(1 for key in states.values() if key in ("x", "-"))
    data.update({"next_action": next_action, "current_state": vocab.text("state_progress", language, done=finished, total=len(steps))
                 + (vocab.text("state_failed_suffix", language, n=failed[0]["n"]) if failed else ""), "updated_at": stamp})
    body = case_tool._replace_section(body, "current_state", data["current_state"], language)  # noqa: SLF001
    body = case_tool._replace_section(body, "next_action", next_action, language)  # noqa: SLF001
    errors = case_tool.validate_schema(data, case_tool.Project(ctx.project).schema)
    if errors:
        fail(_("更新后 Case 不符合 Schema：") + _("；").join(errors), "invalid")
    text = case_tool.render_frontmatter(data) + body
    result = {"case": data["id"], "step": args.n, "state": state, "next_action": next_action, "progress": f"{finished}/{len(steps)}",
              "applied": False, "sha256_before": sha(current)}
    if args.apply:
        if path.read_text(encoding="utf-8") != current:
            fail(_("Case 刚被其他人修改，请重新读取"), "conflict")
        case_tool.write_atomic(path, text)
        result["applied"] = True
    return 0, result, [_("{0} 步骤 {1} {2}；进度 {3}/{4}；下一步：{5}").format(data['id'], args.n, shown, finished, len(steps), next_action)] + \
        ([] if args.apply else [_("以上为预览，没有写入。确认后加 --apply（用户已确认执行该 SOP 时，步骤记录属于这次授权的范围）。")])


def cmd_status(ctx: Context) -> tuple[int, dict, list[str]]:
    path, data, body, steps = read_run(ctx, ctx.args.case)
    sections = split_sections(body)
    intermediate = next((sections[name] for name in vocab.CASE_SECTIONS["intermediate"] if name in sections), "")
    artifacts = [line[2:] for line in intermediate.splitlines() if line.startswith("- ")]
    pending = [step for step in steps if step["mark"] == " "]
    failed = [step for step in steps if step["mark"] == "!"]
    skipped = [step for step in steps if step["mark"] == "-"]
    complete = not pending and not failed
    lines = [_("{0}（{1}）进度：{2}/{3}").format(data['id'], data.get('sop', ''), len(steps) - len(pending) - len(failed), len(steps))]
    lines += [_("  [{0}] {1}. {2}（{3}）").format(step['mark'], step['n'], step['text'], _(step['state'])) for step in steps]
    lines += [_("中间产物：") + (_("；").join(artifacts) if artifacts else _("无"))]
    lines += [_("可以核对完成标准并收尾。") if complete else _("还有未完成或失败的步骤。")]
    return (0 if complete else 1), {"case": data["id"], "sop": data.get("sop", ""), "steps": steps, "complete": complete,
                                    "skipped": [step["n"] for step in skipped], "failed": [step["n"] for step in failed],
                                    "artifacts": artifacts,
                                    "keep_policy": section_value(body, "cl_artifacts_label")}, lines


def cmd_confirm_read(ctx: Context) -> tuple[int, dict, list[str]]:
    """记录“用户已确认读取这些私有路径”：写进 Case 的决策记录，并把 Case 标为 private。确认本身必须先问过用户。"""
    dirs = private_paths.declared(ctx.project)
    outside = [item for item in ctx.args.paths if not private_paths.is_private(ctx.project, item, dirs)]
    if outside:
        fail(_("这些路径不在私有目录里，无需确认：{0}").format(_('、').join(outside)), "usage",
             [_("私有目录：") + (_("、").join(dirs) or _("（无；在 Profile 的 privacy.forbidden_default_reads 声明，或使用项目根的 private/）"))])
    try:
        cases = case_tool.Project(ctx.project)
        path = cases.find(ctx.args.case) / "CASE.md"
        data, body = case_tool.read_case(path)
    except case_tool.CaseError as exc:
        raise SopError(str(exc), "not-found") from exc
    current = path.read_text(encoding="utf-8")
    language = vocab.detect_case(body, ctx.language)
    stamp = case_tool.now()
    note = vocab.text("confirm_note", language, note=ctx.args.note.strip()) if ctx.args.note else ""
    body = case_tool.append_section(body, "decisions", vocab.text(
        "log_confirm_read", language, stamp=stamp, paths=vocab.text("list_join", language).join(ctx.args.paths), note=note), language)
    marked = data.get("sensitivity") != "private"
    data.update({"sensitivity": "private", "updated_at": stamp})
    errors = case_tool.validate_schema(data, cases.schema)
    if errors:
        fail(_("更新后 Case 不符合 Schema：") + _("；").join(errors), "invalid")
    result = plan_result(ctx.project, [{"path": path, "content": case_tool.render_frontmatter(data) + body, "expect": sha(current)}],
                         ctx.args.apply, {"case": data["id"], "paths": ctx.args.paths, "marked_private": marked})
    return 0, result, [_("{0}：用户确认读取 {1}").format(data['id'], _('、').join(ctx.args.paths)) + (_("；Case 已标为 private") if marked else "")] + \
        ([result["verify"]] if result["applied"] else [_("以上为预览，没有写入。确认后加 --apply（只有用户确实确认过才写）。")])


def cmd_private(ctx: Context) -> tuple[int, dict, list[str]]:
    """私有目录现状：哪些路径、是否被 git 忽略、是否已有文件被跟踪；--apply 补上缺失的 .gitignore。"""
    dirs = private_paths.declared(ctx.project)
    if not dirs and ctx.args.create:
        dirs = [private_paths.CONVENTION]
    rows, writes, lines = [], [], []
    for item in dirs:
        state = private_paths.git_state(ctx.project, item)
        plan = private_paths.plan_ignore(ctx.project, item) if (ctx.project / item).is_dir() or ctx.args.create else []
        writes += plan
        rows.append({**state, "needs_gitignore": bool(plan)})
        status = _("不在 git 仓库里或没有 git") if state["ignored"] is None else _("已被 git 忽略") if state["ignored"] else _("未被 git 忽略")
        lines.append(_("{0}/：{1}；{2}").format(item, _('存在') if state['exists'] else _('不存在'), status)
                     + (_("；已有 {0} 个文件被 git 跟踪").format(len(state['tracked'])) if state["tracked"] else "")
                     + (_("；将创建 .gitignore") if plan else ""))
        if state["tracked"]:
            lines.append(_("  已被跟踪的文件不会因为 .gitignore 而消失：确认后用 git rm -r --cached {0} 取消跟踪（历史里的内容仍在）").format(item))
    if not dirs:
        lines.append(_("没有私有目录。把私有内容放进项目根的 private/，或在 Profile 的 privacy.forbidden_default_reads 声明路径；"
                     "运行 sop private --create --apply 创建 private/。"))
    result = plan_result(ctx.project, writes, ctx.args.apply, {"dirs": rows})
    return (1 if any(row["ignored"] is False or row["tracked"] for row in rows) and not result["applied"] else 0), result, \
        lines + ([] if result["applied"] or not writes else [_("以上为预览，没有写入。确认后加 --apply。")])


def cmd_init(ctx: Context) -> tuple[int, dict, list[str]]:
    root = project_root(ctx.project)
    writes = [] if root.is_dir() else [{"path": root / ".gitkeep", "content": "", "expect": None}]
    result = plan_result(ctx.project, writes, ctx.args.apply, {"root": display(root, ctx.project)})
    return 0, result, [_("SOP 目录：{0}").format(display(root, ctx.project)) + (_("（已存在）") if not writes else ""),
                       _("可在 Profile 加 \"sop\": {\"root\": \"sops\"}，让 check 把它列入脚手架（可选，不加也能用）。")] + \
        ([] if result["applied"] or not writes else [_("以上为预览，没有写入。确认后加 --apply。")])


COMMANDS = {"list": cmd_list, "index": cmd_index, "show": cmd_show, "validate": cmd_validate, "interview": cmd_interview,
            "defaults": cmd_defaults, "preflight": cmd_preflight, "new": cmd_new, "activate": cmd_activate,
            "retire": cmd_retire, "restore": cmd_retire, "decide": cmd_decide, "revise": cmd_revise, "match": cmd_match, "due": cmd_due,
            "events": cmd_events, "run": cmd_run, "step": cmd_step, "status": cmd_status, "init": cmd_init,
            "confirm-read": cmd_confirm_read, "private": cmd_private}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="sop.py", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    result.add_argument("--project", type=Path, default=Path.cwd())
    result.add_argument("--output", choices=("text", "json"), default="text")
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--project", type=Path, default=argparse.SUPPRESS)
    shared.add_argument("--output", choices=("text", "json"), default=argparse.SUPPRESS)
    sub = result.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, *, write: bool = False) -> argparse.ArgumentParser:
        item = sub.add_parser(name, parents=[shared], help=help_text)
        if write:
            item.add_argument("--apply", action="store_true", help=_("执行写入（缺省只预览）"))
        return item

    listing = add("list", _("列出 SOP"))
    listing.add_argument("--all", action="store_true", help=_("含已退役"))
    listing.add_argument("--scope", choices=("project", "user"))
    add("index", _("active SOP 的索引（每个一行，Agent 读它判断是否触发）"))
    add("show", _("查看一个 SOP")).add_argument("sop")
    add("validate", _("校验结构、前置条件、触发与版本")).add_argument("sop", nargs="?")
    add("interview", _("创建与配置时要谈清的事")).add_argument("sop", nargs="?")
    defaults = add("defaults", _("查看或设置本项目的使用习惯"), write=True)
    defaults.add_argument("--set", action="append", metavar=_("键=值"))
    add("preflight", _("执行前检查所需程序与环境")).add_argument("sop")
    new = add("new", _("创建草稿"), write=True)
    new.add_argument("--slug", required=True)
    new.add_argument("--title")
    new.add_argument("--template", default="blank", help=_("样例：blank / video-to-transcript / weekly-review"))
    new.add_argument("--language", choices=vocab.LANGUAGES, help=_("新 SOP 的文件语言（缺省取项目 Profile 的 language，再缺省为 zh）"))
    new.add_argument("--from-file", help=_("用现成的 SOP.md 作为草稿"))
    new.add_argument("--from-case", help=_("从一个已完成的 Case 的工作记录提炼草稿"))
    new.add_argument("--scope", choices=("project", "user"), default="project")
    add("activate", _("启用草稿（要求内容与约定都已确认）"), write=True).add_argument("sop")
    for name, text in (("retire", _("退役")), ("restore", _("恢复为 active"))):
        item = add(name, text, write=True)
        item.add_argument("sop")
        item.add_argument("--note")
    decide = add("decide", _("记录用户已确认的事项（触发、前置条件、出错/缺工具/中间产物等约定）"), write=True)
    decide.add_argument("sop")
    decide.add_argument("--confirm", action="append", metavar="trigger|requires")
    decide.add_argument("--set", action="append", metavar=_("键=值"))
    revise = add("revise", _("登记新版本：升版本号、写修订记录、存快照"), write=True)
    revise.add_argument("sop")
    revise.add_argument("--bump", choices=("patch", "minor", "major"), required=True)
    revise.add_argument("--note")
    add("match", _("关键词预筛")).add_argument("text", nargs="+")
    add("due", _("哪些有周期的 SOP 到期了"))
    events = add("events", _("监听某事件的 SOP；加 --case 只看对这个 Case 会触发的"))
    events.add_argument("event")
    events.add_argument("--case", help=_("发生事件的 Case：按它的标签过滤，SOP 自己的执行 Case 不触发"))
    run = add("run", _("按 SOP 建立一次执行（Case）"), write=True)
    run.add_argument("sop")
    run.add_argument("--title")
    run.add_argument("--param", action="append", metavar=_("名称=值"))
    run.add_argument("--trial", action="store_true", help=_("试跑草稿"))
    run.add_argument("--event", choices=EVENTS, help=_("由哪个事件触发（需同时给 --event-case）"))
    run.add_argument("--event-case", help=_("触发事件的 Case"))
    step = add("step", _("记录某一步的结果"), write=True)
    step.add_argument("case")
    step.add_argument("n", type=int)
    for name in ("done", "skip", "fail", "pending"):
        step.add_argument(f"--{name}", action="store_true")
    step.add_argument("--note")
    step.add_argument("--artifact", action="append", metavar=_("路径[:说明]"),
                      help=_("登记中间产物；路径相对本 Case 目录（目录外的用绝对路径或 ../）"))
    step.add_argument("--expect-sha256")
    add("status", _("一次执行的进度与中间产物")).add_argument("case")
    add("init", _("创建 SOP 目录"), write=True)
    confirm = add("confirm-read", _("记录用户已确认读取某些私有路径（写入 Case 的决策记录，Case 标为 private）"), write=True)
    confirm.add_argument("case")
    confirm.add_argument("paths", nargs="+")
    confirm.add_argument("--note")
    private = add("private", _("私有目录现状：是否被 git 忽略；--apply 补上 .gitignore"), write=True)
    private.add_argument("--create", action="store_true", help=_("没有私有目录时创建项目根的 private/"))
    return result


def execute(argv: list[str]) -> tuple[int, dict, list[str]]:
    args = parser().parse_args(argv)
    return COMMANDS[args.command](Context(args))


EXIT_BY_CODE = {"usage": 2, "invalid": 2, "not-found": 1, "ambiguous": 1, "conflict": 5, "unavailable": 4, "failed": 6}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    output = "json" if "--output" in argv and argv[argv.index("--output") + 1:][:1] == ["json"] else "text"
    try:
        code, data, lines = execute(argv)
    except SopError as exc:
        if output == "json":
            print(json.dumps({"ok": False, "code": exc.code, "error": str(exc), "next": exc.next_steps}, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
            for step in exc.next_steps:
                print(f"  - {step}", file=sys.stderr)
        return EXIT_BY_CODE.get(exc.code, 2)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 2
    if output == "json":
        print(json.dumps({"ok": code == 0, "exit_code": code, **data}, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
