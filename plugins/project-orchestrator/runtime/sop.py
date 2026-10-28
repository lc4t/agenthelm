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
PLACEHOLDER = "【待补充】"

STATUSES = ("draft", "active", "retired")
SCALARS = ("schema_version", "id", "title", "status", "version", "description", "trigger", "exclude", "schedule",
           "confirm", "creates_case", "on_error", "max_retries", "on_missing_tool", "artifact_dir", "artifact_keep",
           "resume", "confirm_costly", "sensitivity", "created", "verified_at", "review_after", "origin")
LISTS = ("aliases", "examples_hit", "examples_miss", "on_events", "event_tags", "outputs", "sources", "decided")
REQUIRED = ("schema_version", "id", "title", "status", "version", "description", "created")
SECTIONS = ("目的", "前置条件", "步骤", "完成标准", "例外与回退", "修订记录")
STRICT_SECTIONS = ("前置条件", "步骤", "完成标准")

# 用户习惯：每项给出可选值、缺省、以及创建时必须谈的问题。第一个标注推荐的是我们的倾向，用户可改。
CHOICES: dict[str, dict[str, Any]] = {
    "on_error": {"values": ("ask", "stop", "retry", "skip-step"), "default": "ask",
                 "labels": {"ask": "停下来告诉我，由我决定（推荐）", "stop": "立即停止并记录现场，不再继续",
                            "retry": "自动重试 max_retries 次，仍失败再问我", "skip-step": "跳过该步继续，事后列出所有跳过的步骤"}},
    "max_retries": {"default": "2"},
    "on_missing_tool": {"values": ("ask", "stop", "skip-step", "alternative"), "default": "ask",
                        "labels": {"ask": "停下来，列出缺什么、怎么装，等我处理（推荐）", "stop": "停止，不继续执行",
                                   "skip-step": "跳过用到该工具的步骤", "alternative": "改用我声明的替代方案；没有替代就问我"}},
    "artifact_dir": {"default": ""},
    "artifact_keep": {"values": ("all", "final-only", "on-error", "ask"), "default": "ask",
                      "labels": {"all": "全部保留", "final-only": "成功后只保留最终产物，清理登记过的中间产物",
                                 "on-error": "成功后清理中间产物，失败时保留以便排查", "ask": "每次完成后问我（推荐）"}},
    "resume": {"values": ("from-failed-step", "restart", "ask"), "default": "ask",
               "labels": {"from-failed-step": "从失败的那一步继续，已完成的步骤不重做（推荐）", "restart": "从头重做",
                          "ask": "每次问我"}},
    "confirm_costly": {"values": ("ask", "never"), "default": "ask",
                       "labels": {"ask": "调用付费 API、下载大文件、预计耗时数分钟以上的步骤前先问我（推荐）",
                                  "never": "不用问，直接执行"}},
    "sensitivity": {"values": ("shared", "private"), "default": "shared",
                    "labels": {"shared": "可以共享：纯知识整理、规则整理等，不含个人进度或私人素材",
                               "private": "私密（推荐用于涉及个人进度、学习记录、账号、原始素材的流程）：执行记录不进资料索引，产物只放本 Case 目录内"}},
    "confirm": {"values": ("ask", "auto"), "default": "ask",
                "labels": {"ask": "命中触发后先问我是否按它执行（推荐）", "auto": "命中后直接执行（只适合没有外部副作用的流程）"}},
    "creates_case": {"values": ("always", "ask", "never"), "default": "always"},
}
# 创建时必须与用户确认的事项（编号顺序即提问顺序）。defaults 项表示可由项目习惯（defaults.json）代为回答。
DECISIONS = (
    ("trigger", None, "什么情况下应该用这个流程？请写：①触发场景（一句话）；②明确不该触发的相近场景；"
                      "③有没有固定周期（每天 / 每周几 / 每月几号 / 每 N 天），或由事件触发（新建 Case / 关闭 Case；"
                      "要不要只对带某些标签的 Case 触发，否则每个 Case 新建或关闭都会提示）；"
                      "④再给 1–2 句你平时会怎么说的话（用来校准识别）。"),
    ("requires", None, "这个流程用到哪些程序、Python 模块、脚本、模型、账号或 MCP？逐项确认：名称、做什么用、"
                        "没有时怎么安装、有没有替代方案、缺了怎么办。API 密钥只写环境变量名，不写密钥本身。"),
    ("on_error", ("on_error",), "某一步出错时怎么办？"),
    ("on_missing_tool", ("on_missing_tool",), "执行前发现缺程序、缺模型或缺账号时怎么办？（AI 不会在没有你同意时自行安装软件）"),
    ("artifacts", ("artifact_keep",), "中间产物（如下载的视频、转出的音频、临时文本）放在哪里、完成后怎么处理？"
                                       "缺省放在本次 Case 的 data/ 目录；体积很大的文件可以指定项目外的目录。"),
    ("resume", ("resume",), "中途失败后重新执行时，从哪里开始？"),
    ("confirm_costly", ("confirm_costly",), "遇到花钱、占用大量时间或网络流量的步骤，要不要先问你？"),
    ("privacy", ("sensitivity",), "这个流程的执行记录和产物，能不能出现在会公开或共享的地方？涉及你个人的进度、学习记录、账号或原始素材的，"
                                  "建议选私密；纯知识整理、规则整理这类可以共享。流程要读取哪些事项（范围）也请一起定下，不要默认纳入全部。"),
)
# 习惯项 -> 它所属的“创建时必须确认”事项；设置这些项等于用户确认了对应事项。
FIELD_DECISION = {"on_error": "on_error", "max_retries": "on_error", "on_missing_tool": "on_missing_tool",
                  "artifact_keep": "artifacts", "artifact_dir": "artifacts", "resume": "resume",
                  "confirm_costly": "confirm_costly", "sensitivity": "privacy"}
DECISION_OPTIONS = {"on_error": "on_error", "on_missing_tool": "on_missing_tool", "artifacts": "artifact_keep",
                    "resume": "resume", "confirm_costly": "confirm_costly", "privacy": "sensitivity"}

STEP_KEYS = {"工具": "tools", "输入": "inputs", "中间产物": "intermediates", "最终产物": "finals", "检查点": "checkpoint",
             "失败时": "on_failure", "需确认": "confirm", "说明": "note"}
KIND_MAP = {"命令": "command", "模块": "module", "环境变量": "env", "路径": "path", "技能": "skill", "MCP": "mcp",
            "mcp": "mcp", "手动确认": "manual", "手动": "manual"}
KIND_LABEL = {"command": "命令", "module": "Python 模块", "env": "环境变量", "path": "路径", "skill": "技能",
              "mcp": "MCP", "manual": "手动确认"}
REQ_KEYS = {"用途": "purpose", "安装": "install", "替代": "alternative", "缺失时": "if_missing", "版本": "version",
            "仅当": "when"}
MISSING_MAP = {"询问用户": "ask", "询问": "ask", "停止": "stop", "跳过该步": "skip-step", "跳过": "skip-step",
               "使用替代": "alternative", "替代": "alternative"}
SECRET_PATTERNS = (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
                   re.compile(r"AKIA[0-9A-Z]{16}"), re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
                   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
                   re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\"']?[A-Za-z0-9/+_\-]{16,}"))
MARKS = {" ": "待办", "x": "完成", "-": "跳过", "!": "失败"}
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
        fail(f"{label} 必须是项目内的相对路径：{relative}")
    current = project
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            fail(f"{label} 不能经过符号链接：{relative}")
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
        fail("SOP.md 必须以 --- 开头", "invalid")
    end = text.find("\n---\n", 3)
    if end < 0:
        fail("SOP.md 缺少 frontmatter 结束标记", "invalid")
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
            fail("拒绝符号链接", "invalid")
        raw = sop["path"].read_bytes()
        if len(raw) > MAX_BYTES:
            fail("SOP.md 超过 1 MiB", "invalid")
        sop["text"] = raw.decode("utf-8")
        sop["meta"], sop["body"] = parse_sop_text(sop["text"])
    except FileNotFoundError:
        sop["error"] = "缺少 SOP.md"
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
    wanted, _, _ = token.partition("@")
    live = [sop for sop in sops if not sop["shadowed"]]
    exact = [sop for sop in live if sop["id"] == wanted]
    matches = exact or [sop for sop in live if wanted.lower() in sop["id"] or wanted in str(sop["meta"].get("title", ""))]
    if not matches:
        fail(f"没有找到 SOP：{token}", "not-found", ["sop.py list 查看全部"])
    if len(matches) > 1:
        fail(f"有 {len(matches)} 个 SOP 匹配“{token}”", "ambiguous", [sop["id"] for sop in matches[:10]])
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
    return [item.strip() for item in re.split(r"[、,，;；]", value) if item.strip() and item.strip() not in ("无", "-", "—")]


def parse_steps(text: str) -> tuple[list[dict], list[str]]:
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
        sub = re.match(r"^\s+[-*]\s*([^：:]{1,8})[：:]\s*(.*?)\s*$", line)
        if not sub:
            steps[-1]["note"] = (steps[-1]["note"] + " " + line.strip()).strip()
            continue
        key = STEP_KEYS.get(sub.group(1).strip())
        if key is None:
            problems.append(f"步骤 {steps[-1]['n']} 有未知字段“{sub.group(1)}”（可用：{'、'.join(STEP_KEYS)}）")
        elif key in ("tools", "intermediates", "finals"):
            steps[-1][key] = split_list(sub.group(2))
        elif key == "confirm":
            steps[-1][key] = sub.group(2).strip().lower() in ("是", "y", "yes", "true", "需要")
        else:
            steps[-1][key] = sub.group(2)
    for index, step in enumerate(steps, 1):
        if step["n"] != index:
            problems.append(f"步骤编号应从 1 连续递增：第 {index} 个步骤写成了 {step['n']}")
            break
    return steps, problems


def parse_requirements(text: str) -> tuple[list[dict], list[str]]:
    reqs: list[dict] = []
    problems: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.rstrip("。") in ("无", "（无）", "(无)", "不需要"):
            continue
        if not stripped.startswith("- "):
            continue
        parts = [part.strip() for part in re.split(r"\s*｜\s*|\s+\|\s+", stripped[2:]) if part.strip()]
        head = re.match(r"^([^：:]{1,8})[：:]\s*(.+)$", parts[0]) if parts else None
        if not head or head.group(1) not in KIND_MAP:
            problems.append(f"无法识别的前置条件：{stripped[:40]}（写法：- 命令：名称｜用途：…｜安装：…｜缺失时：询问用户）")
            continue
        req = {"kind": KIND_MAP[head.group(1)], "name": head.group(2).strip().strip("`"), "purpose": "", "install": "",
               "alternative": "", "if_missing": "", "version": "", "when": ""}
        for part in parts[1:]:
            pair = re.match(r"^([^：:]{1,8})[：:]\s*(.*)$", part)
            key = REQ_KEYS.get(pair.group(1)) if pair else None
            if key is None:
                problems.append(f"{req['name']}：未知字段“{part[:20]}”（可用：{'、'.join(REQ_KEYS)}）")
            elif key == "if_missing":
                value = MISSING_MAP.get(pair.group(2).strip())
                if value is None:
                    problems.append(f"{req['name']}：缺失时只能写 {'、'.join(sorted(set(MISSING_MAP) - {'询问', '跳过', '替代'}))}")
                else:
                    req[key] = value
            else:
                req[key] = pair.group(2).strip()
        reqs.append(req)
    return reqs, problems


def parsed(sop: dict) -> dict:
    sections = split_sections(sop["body"])
    steps, step_problems = parse_steps(sections.get("步骤", ""))
    reqs, req_problems = parse_requirements(sections.get("前置条件", ""))
    return {"sections": sections, "steps": steps, "step_problems": step_problems, "requires": reqs,
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
        raise SopError(f"{DEFAULTS_FILE} 无法解析：{exc}", "invalid") from exc
    conventions = data.get("conventions") if isinstance(data, dict) else None
    if not isinstance(conventions, dict):
        fail(f"{DEFAULTS_FILE} 的 conventions 必须是对象", "invalid")
    return {"conventions": {key: str(value) for key, value in conventions.items() if key in CHOICES},
            "decided_at": str(data.get("decided_at", ""))}


def check_choice(key: str, value: str) -> None:
    spec = CHOICES.get(key)
    if spec is None:
        fail(f"未知的习惯项：{key}（可用：{'、'.join(CHOICES)}）")
    if "values" in spec and value not in spec["values"]:
        fail(f"{key} 只能是 {' / '.join(spec['values'])}：{value}")
    if key == "max_retries" and not re.fullmatch(r"[1-9]", value):
        fail("max_retries 只能是 1–9")
    if key == "artifact_dir" and ("\n" in value or "\x00" in value):
        fail("artifact_dir 只能是单行路径")


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
        found.append({"key": key, "question": question, "options": options_for(key)})
    return found


def options_for(key: str) -> list[dict]:
    field = DECISION_OPTIONS.get(key)
    if not field:
        return []
    spec = CHOICES[field]
    return [{"value": value, "label": spec["labels"][value], "recommended": "推荐" in spec["labels"][value]}
            for value in spec["values"]]


# ---------------------------------------------------------------- 校验

def issue(level: str, code: str, message: str) -> dict:
    return {"level": level, "code": code, "message": message}


def check_sop(sop: dict, defaults: dict, *, strict: bool | None = None) -> list[dict]:
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
        add("error", "unknown-field", f"未知字段：{'、'.join(unknown)}")
    for key in REQUIRED:
        if not str(meta.get(key, "")).strip():
            add("error", "missing-field", f"缺少字段 {key}")
    if meta.get("schema_version") not in (None, "1.0"):
        add("error", "schema-version", "schema_version 必须为 1.0")
    if meta.get("id") and (not ID_RE.fullmatch(str(meta["id"])) or meta["id"] != sop["id"]):
        add("error", "id-mismatch", f"id 必须是小写字母、数字与连字符，且与目录名一致（目录：{sop['id']}）")
    if meta.get("status") and meta["status"] not in STATUSES:
        add("error", "invalid-status", f"status 必须为：{'、'.join(STATUSES)}")
    if meta.get("version") and not VERSION_RE.fullmatch(str(meta["version"])):
        add("error", "invalid-version", "version 必须是 主.次.修订，例如 1.0.0")
    for key in ("confirm", "creates_case", "on_error", "on_missing_tool", "artifact_keep", "resume", "confirm_costly",
                "sensitivity"):
        value = meta.get(key)
        if value not in (None, "") and value not in CHOICES[key]["values"]:
            add("error", "invalid-choice", f"{key} 只能是：{' / '.join(CHOICES[key]['values'])}")
    if meta.get("max_retries") not in (None, "") and not re.fullmatch(r"[1-9]", str(meta["max_retries"])):
        add("error", "invalid-choice", "max_retries 只能是 1–9")
    if meta.get("schedule") and not SCHEDULE_RE.fullmatch(str(meta["schedule"])):
        add("error", "invalid-schedule", "schedule 写法：daily / weekly:mon..sun / monthly:1..28 / every:N天（如 every:14d）")
    for event in meta.get("on_events") or []:
        if event not in EVENTS:
            add("error", "invalid-event", f"on_events 只支持：{'、'.join(EVENTS)}")
    if meta.get("event_tags") and not meta.get("on_events"):
        add("warn", "event-tags-without-event", "设了 event_tags 但没有 on_events：标签过滤不会生效")
    for key in ("created", "verified_at", "review_after"):
        if meta.get(key):
            try:
                date.fromisoformat(str(meta[key]))
            except ValueError:
                add("error", "invalid-date", f"{key} 必须为 YYYY-MM-DD")
    for item in meta.get("decided") or []:
        if item not in {key for key, _, _ in DECISIONS}:
            add("error", "invalid-decided", f"decided 里有未知项：{item}")
    if meta.get("schedule") and meta.get("creates_case") == "never":
        add("warn", "schedule-without-case", "设了周期但 creates_case=never：没有执行记录，无法判断是否到期")
    if len(str(meta.get("description", ""))) > 80:
        add("warn", "description-long", "description 超过 80 字符：索引里每个 SOP 只占一行")
    if meta.get("review_after"):
        try:
            if date.fromisoformat(str(meta["review_after"])) < today():
                add("warn", "review-due", f"已超过复核日期 {meta['review_after']}：核对后更新 verified_at / review_after")
        except ValueError:
            pass
    if strict and not str(meta.get("trigger", "")).strip():
        add("error", "missing-trigger", "active 的 SOP 必须写 trigger（什么情况下用它）")

    info = parsed(sop)
    for name in SECTIONS if strict else ():
        if name in STRICT_SECTIONS and not info["sections"].get(name, "").strip():
            add("error", "missing-section", f"缺少或为空的小节：## {name}")
    if not strict:
        for name in STRICT_SECTIONS:
            if not info["sections"].get(name, "").strip():
                add("warn", "missing-section", f"缺少或为空的小节：## {name}")
    for problem in info["step_problems"]:
        add(level, "step-format", problem)
    for problem in info["require_problems"]:
        add(level, "requirement-format", problem)
    if strict and not info["steps"]:
        add("error", "no-steps", "## 步骤 下至少要有一个编号步骤")
    names = {req["name"].lower() for req in info["requires"]}
    for req in info["requires"]:
        if req["alternative"] and req["alternative"].lower() not in names:
            add("error", "unknown-alternative", f"{req['name']} 的替代项“{req['alternative']}”没有在前置条件里声明")
    for step in info["steps"]:
        for tool in step["tools"]:
            if tool.lower() not in names:
                add(level, "undeclared-tool", f"步骤 {step['n']} 用到“{tool}”，但前置条件里没有声明（会让执行前检查漏掉它）")
    if PLACEHOLDER in sop["text"]:
        add(level, "placeholder", f"还有 {sop['text'].count(PLACEHOLDER)} 处{PLACEHOLDER}需要填写")
    if any(pattern.search(sop["text"]) for pattern in SECRET_PATTERNS):
        add("error", "secret", "SOP 里疑似写入了密钥或口令：只写环境变量名，不写密钥本身")
    if strict:
        for gap in gaps(meta, defaults):
            add("error", "undecided", f"尚未与用户确认：{gap['key']}（sop.py interview {sop['id']} 查看问题）")
    snapshot = sop["dir"] / SNAPSHOTS / f"{meta.get('version', '')}.md"
    if snapshot.is_file():
        if snapshot.read_text(encoding="utf-8") != sop["text"]:
            add("warn", "version-not-bumped", "内容比版本快照新，但版本号没升：用 sop.py revise --bump … 记录这次修改")
    elif meta.get("status") == "active":
        add("warn", "no-snapshot", "active 的 SOP 缺少当前版本的快照：运行 sop.py revise 或 activate 补上")
    for name in sorted(path.name for path in sop["dir"].iterdir() if path.is_file()) if sop["dir"].is_dir() else []:
        if name != "SOP.md" and re.match(r"^SOP\b.*\.md$", name):
            add("warn", "conflict-copy", f"{name} 像是同步盘留下的冲突副本：合并到 SOP.md 后删除")
    return found


def check_all(sops: list[dict], defaults: dict, project: Path | None) -> list[dict]:
    results = []
    active_alias: dict[str, str] = {}
    for sop in sops:
        issues = check_sop(sop, defaults)
        if project and not sop["error"]:
            for source in sop["meta"].get("sources") or []:
                try:
                    if not inside(project, source, "sources").exists():
                        issues.append(issue("warn", "missing-source", f"来源路径不存在：{source}"))
                except SopError as exc:
                    issues.append(issue("warn", "invalid-source", str(exc)))
        if sop["meta"].get("status") == "active" and not sop["shadowed"]:
            for name in [str(sop["meta"].get("title", "")), *(sop["meta"].get("aliases") or [])]:
                other = active_alias.setdefault(name.lower(), sop["id"]) if name else sop["id"]
                if other != sop["id"]:
                    issues.append(issue("warn", "trigger-overlap", f"“{name}”同时是 {other} 的名称或别名，识别时会混淆"))
        if sop["shadowed"]:
            issues.append(issue("warn", "shadowed", "同 id 的项目级 SOP 覆盖了这份用户级 SOP"))
        results.append({"id": sop["id"], "scope": sop["scope"], "status": sop["meta"].get("status", "?"),
                        "issues": issues, "valid": not any(item["level"] == "error" for item in issues)})
    if sum(1 for sop in sops if sop["meta"].get("status") == "active" and not sop["shadowed"]) > MAX_INDEX_ENTRIES:
        results.append({"id": "(index)", "scope": "", "status": "", "valid": True,
                        "issues": [issue("warn", "index-budget", f"active 的 SOP 超过 {MAX_INDEX_ENTRIES} 个：索引会占用过多上下文，建议退役不用的")]})
    return results


# ---------------------------------------------------------------- 触发：索引、匹配、周期

def index_line(sop: dict, due: dict | None = None) -> str:
    meta = sop["meta"]
    parts = [f"- `{sop['id']}`@{meta.get('version', '?')}：{meta.get('description', '')}"]
    if meta.get("trigger"):
        parts.append(f"触发：{meta['trigger']}")
    if meta.get("exclude"):
        parts.append(f"不触发：{meta['exclude']}")
    if meta.get("schedule"):
        parts.append(f"周期：{meta['schedule']}")
    if meta.get("on_events"):
        tags = meta.get("event_tags") or []
        parts.append("事件：" + "、".join(meta["on_events"]) + (f"（仅标签 {'、'.join(tags)}）" if tags else ""))
    if (meta.get("confirm") or "ask") == "auto":
        parts.append("命中即执行")
    if due and due.get("due"):
        parts.append("已到期")
    return "｜".join(parts)


def next_occurrence(schedule: str, base: date) -> date:
    kind, _, arg = schedule.partition(":")
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
            data, _ = case_tool.read_case(directory / "CASE.md")
        except (case_tool.CaseError, OSError, UnicodeDecodeError):
            continue
        sop_id, _, version = str(data.get("sop") or "").partition("@")
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
                reasons.append(f"提到“{name}”")
        for example in meta.get("examples_hit") or []:
            score = len(bigrams(text) & bigrams(example)) / max(1, len(bigrams(text) | bigrams(example)))
            if score >= 0.45:
                reasons.append(f"与示例话术“{example}”相似（{score:.2f}）")
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
            return ("ok", str(path)) if path.is_file() and os.access(path, os.X_OK) else ("missing", f"不存在或不可执行：{name}")
        found = shutil.which(name)
        return ("ok", found) if found else ("missing", "PATH 上找不到")
    if kind == "module":
        try:
            found_spec = importlib.util.find_spec(name.split(".")[0])
        except (ImportError, ValueError):
            found_spec = None
        return ("ok", "运行本工具的 Python 里可导入；若流程用别的 Python 环境，请人工确认") if found_spec \
            else ("unknown", "运行本工具的 Python 里找不到；若流程用别的 Python 环境，请到那个环境里确认")
    if kind == "env":
        return ("ok", "已设置（值不显示）") if os.environ.get(name) else ("missing", "当前环境没有设置或为空")
    if kind == "path":
        path = Path(name).expanduser()
        path = path if path.is_absolute() else project / path
        return ("ok", str(path)) if path.exists() else ("missing", f"不存在：{path}")
    if kind == "skill":
        found = lookup_skill(name, project)
        return ("ok", found) if found else ("unknown", "常见位置没找到；请对照当前会话的可用技能列表确认")
    if kind == "mcp":
        found = lookup_mcp(name, project)
        return ("ok", found) if found else ("unknown", "配置文件里没找到；请对照当前会话的可用工具确认")
    return "unknown", "需要用户确认"


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
        rows.append({**req, "label": KIND_LABEL[req["kind"]], "status": status, "detail": detail,
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
            fail(f"已存在，拒绝覆盖：{path.name}", "conflict")
        if item["expect"] is not None and (not exists or sha(path.read_text(encoding="utf-8")) != item["expect"]):
            fail(f"{path.name} 在预览之后被修改过，请重新预览", "conflict")
    for item in writes:
        write_text(item["path"], item["content"])
    for item in writes:
        if item["path"].read_text(encoding="utf-8") != item["content"]:
            fail(f"写入后读回不一致：{item['path'].name}", "failed")
    return [str(item["path"]) for item in writes]


def plan_result(project: Path, writes: list[dict], apply: bool, extra: dict) -> dict:
    result = {"applied": False, "writes": [{"path": display(item["path"], project),
                                            "action": "create" if item["expect"] is None else "update",
                                            "sha256_before": item["expect"]} for item in writes], **extra}
    if apply:
        apply_writes(writes)
        result["applied"] = True
        result["verify"] = f"已写入并读回核对：{len(writes)} 个文件"
    return result


def snapshot_write(sop_dir: Path, version: str, text: str) -> dict | None:
    path = sop_dir / SNAPSHOTS / f"{version}.md"
    if path.exists():
        return None if path.read_text(encoding="utf-8") == text else {"path": path, "content": text, "expect": sha(path.read_text(encoding="utf-8"))}
    return {"path": path, "content": text, "expect": None}


def bump(version: str, part: str) -> str:
    major, minor, patch = map(int, VERSION_RE.fullmatch(version).groups())
    return {"major": f"{major + 1}.0.0", "minor": f"{major}.{minor + 1}.0", "patch": f"{major}.{minor}.{patch + 1}"}[part]


def append_revision(body: str, line: str) -> str:
    return case_tool.append_section(body, "修订记录", line)


# ---------------------------------------------------------------- 命令

class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.project = args.project.expanduser().resolve()
        self.args = args
        self.defaults = read_defaults(self.project)

    def sops(self, *, user: bool = True) -> list[dict]:
        return load_all(self.project, user=user)

    def one(self, token: str) -> dict:
        return find_sop(self.sops(), token)


def cmd_list(ctx: Context) -> tuple[int, dict, list[str]]:
    sops = ctx.sops()
    runs, _ = last_runs(ctx.project)
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
    lines = [f"{row['status']:8} {row['id']}@{row['version']}  {row['title']}（{'项目' if row['scope'] == 'project' else '用户'}）"
             + (f"  周期 {row['schedule']}" if row["schedule"] else "") + ("  已到期" if row["due"] else "")
             + (f"  读取失败：{row['error']}" if row["error"] else "") for row in rows]
    return 0, {"sops": rows}, lines or ["还没有 SOP。用 sop new 创建；先看 sop interview 要谈清哪些事。"]


def cmd_index(ctx: Context) -> tuple[int, dict, list[str]]:
    runs, _ = last_runs(ctx.project)
    sops = ctx.sops()
    active = [sop for sop in sops if sop["meta"].get("status") == "active" and not sop["shadowed"] and not sop["error"]]
    lines = [index_line(sop, due_info(sop, runs, today())) for sop in active]
    drafts = sum(1 for sop in sops if sop["meta"].get("status") == "draft")
    return 0, {"entries": [{"id": sop["id"], "line": line} for sop, line in zip(active, lines)], "drafts": drafts}, \
        lines or ["没有 active 的 SOP。"]


def cmd_show(ctx: Context) -> tuple[int, dict, list[str]]:
    sop = ctx.one(ctx.args.sop)
    if sop["error"]:
        fail(f"{sop['id']} 无法读取：{sop['error']}", "invalid")
    info = parsed(sop)
    habit = effective(sop["meta"], ctx.defaults)
    data = {"id": sop["id"], "scope": sop["scope"], "path": display(sop["path"], ctx.project), "meta": sop["meta"],
            "steps": info["steps"], "requires": info["requires"], "conventions": habit,
            "gaps": gaps(sop["meta"], ctx.defaults), "issues": check_sop(sop, ctx.defaults)}
    meta = sop["meta"]
    lines = [f"{meta.get('title')}（{sop['id']}@{meta.get('version')}，{meta.get('status')}，{'项目' if sop['scope'] == 'project' else '用户'}级）",
             f"文件：{data['path']}", f"触发：{meta.get('trigger', '')}", f"不触发：{meta.get('exclude', '')}"]
    lines += ["", "步骤："] + [f"  {step['n']}. {step['title']}" + (f"（工具：{'、'.join(step['tools'])}）" if step["tools"] else "")
                              for step in info["steps"]]
    lines += ["", "前置条件："] + [f"  {KIND_LABEL[req['kind']]}：{req['name']}"
                                  + (f"（{req['purpose']}）" if req["purpose"] else "") for req in info["requires"]]
    lines += ["", "执行约定（来源）："] + [f"  {key} = {item['value'] or '（Case 的 data/）'}（{item['source']}）"
                                          for key, item in habit.items() if key in ("on_error", "on_missing_tool", "artifact_dir", "artifact_keep", "resume", "confirm_costly", "sensitivity")]
    if data["gaps"]:
        lines += ["", f"尚有 {len(data['gaps'])} 项没有与用户确认：" + "、".join(gap["key"] for gap in data["gaps"])]
    return 0, data, lines


def cmd_validate(ctx: Context) -> tuple[int, dict, list[str]]:
    sops = [ctx.one(ctx.args.sop)] if ctx.args.sop else ctx.sops()
    results = check_all(sops, ctx.defaults, ctx.project)
    lines = []
    for result in results:
        mark = "ok  " if result["valid"] else "FAIL"
        lines.append(f"{mark} {result['id']}（{result['status']}）")
        lines += [f"      {item['level']}: {item['message']}" for item in result["issues"]]
    bad = [result for result in results if not result["valid"]]
    return (1 if bad else 0), {"ok": not bad, "results": results}, lines or ["还没有 SOP。"]


def cmd_interview(ctx: Context) -> tuple[int, dict, list[str]]:
    """没有 SOP 参数：完整问题清单；给了 SOP：只列还没谈的。编号问题，Agent 应打包成一次提问。"""
    if ctx.args.sop:
        sop = ctx.one(ctx.args.sop)
        open_keys = {gap["key"] for gap in gaps(sop["meta"], ctx.defaults)}
        habit = effective(sop["meta"], ctx.defaults)
    else:
        sop, open_keys, habit = None, {key for key, _, _ in DECISIONS}, effective({}, ctx.defaults)
    items = []
    for number, (key, fields, question) in enumerate(DECISIONS, 1):
        field = DECISION_OPTIONS.get(key)
        current = habit[field] if field else None
        covered = bool(fields) and all(field in ctx.defaults["conventions"] for field in fields)
        if sop is not None and key not in open_keys:
            continue
        items.append({"no": number, "key": key, "question": question, "options": options_for(key),
                      "current": current["value"] if current else None,
                      "project_default": covered, "needs_answer": key in open_keys and not (covered and sop is None)})
    lines = []
    for item in items:
        lines.append(f"{item['no']}. {item['question']}")
        for option in item["options"]:
            lines.append(f"     - {option['value']}：{option['label']}")
        if item["project_default"]:
            lines.append(f"     （本项目习惯已定为 {item['current']}；沿用则无需再问）")
    return (1 if sop is not None and items else 0), {"questions": items, "sop": sop["id"] if sop else None}, \
        lines or ["该 SOP 的所有事项都已确认。"]


def cmd_defaults(ctx: Context) -> tuple[int, dict, list[str]]:
    current = ctx.defaults
    updates: dict[str, str] = {}
    for pair in ctx.args.set or []:
        key, separator, value = pair.partition("=")
        if not separator:
            fail(f"--set 需要 键=值：{pair}")
        check_choice(key.strip(), value.strip())
        updates[key.strip()] = value.strip()
    merged = {**current["conventions"], **updates}
    lines = [f"{key} = {value or '（Case 的 data/ 目录）'}" for key, value in merged.items()] or ["本项目还没有记录使用习惯。"]
    if not updates:
        return 0, {"conventions": merged, "decided_at": current["decided_at"]}, lines
    path = defaults_path(ctx.project)
    content = json.dumps({"schema_version": "1.0", "decided_at": today().isoformat(), "conventions": merged},
                         ensure_ascii=False, indent=2) + "\n"
    expect = sha(path.read_text(encoding="utf-8")) if path.is_file() else None
    result = plan_result(ctx.project, [{"path": path, "content": content, "expect": expect}], ctx.args.apply,
                         {"conventions": merged})
    return 0, result, lines + ["", "已写入。新 SOP 创建时沿用这些习惯。" if result["applied"] else "以上为预览，没有写入。确认后加 --apply。"]


def cmd_preflight(ctx: Context) -> tuple[int, dict, list[str]]:
    sop = ctx.one(ctx.args.sop)
    report = preflight(sop, ctx.project, ctx.defaults)
    lines = [f"{sop['id']}@{report['version']} 执行前检查：{report['counts']['ok']} 项就绪，{report['counts']['missing']} 项缺失，"
             f"{report['counts']['unknown']} 项需要核对" + (f"，{report['counts']['conditional']} 项条件项待判断" if report["counts"]["conditional"] else "")]
    for row in report["requires"]:
        tag = "不需要" if row["optional"] else "已由替代项解决" if row["resolved_by"] else \
            f"条件项·{ {'missing': '缺失', 'unknown': '待核对'}[row['status']] }" if row["when"] and row["status"] != "ok" else \
            {"ok": "就绪", "missing": "缺失", "unknown": "待核对"}[row["status"]]
        lines.append(f"  [{tag}] {row['label']}：{row['name']}"
                     + (f"（{row['purpose']}）" if row["purpose"] else "") + (f" — {row['detail']}" if row["status"] != "ok" else "")
                     + (f"｜仅当：{row['when']}" if row["when"] else ""))
        if row["status"] != "ok" and not row["optional"] and not row["resolved_by"]:
            if row["install"]:
                lines.append(f"        安装/获取方式：{row['install']}")
            if row["alternative"]:
                lines.append(f"        替代方案：{row['alternative']}")
            lines.append(f"        缺失时的约定：{row['if_missing']}")
    if report["counts"]["conditional"]:
        lines.append(f"其中 {report['counts']['conditional']} 项是条件项：只有本次输入符合“仅当”的描述才需要，请对照本次输入判断；符合就按缺失处理。")
    if not report["ready"]:
        lines.append("需要先处理缺失与待核对项；不要在用户未同意时自行安装软件。")
    return (0 if report["ready"] else 1), report, lines


def template_source(name: str) -> tuple[str, dict[str, str]]:
    base = TEMPLATE_DIR / name
    if not ID_RE.fullmatch(name) or not (base / "SOP.md").is_file():
        names = sorted(path.name for path in TEMPLATE_DIR.iterdir() if (path / "SOP.md").is_file()) if TEMPLATE_DIR.is_dir() else []
        fail(f"没有这个样例：{name}", "not-found", ["可用：" + "、".join(names)])
    extra = {path.relative_to(base).as_posix(): path.read_text(encoding="utf-8") for path in sorted(base.rglob("*"))
             if path.is_file() and path.name != "SOP.md"}
    return (base / "SOP.md").read_text(encoding="utf-8"), extra


def cmd_new(ctx: Context) -> tuple[int, dict, list[str]]:
    args = ctx.args
    slug = (args.slug or "").strip()
    if not ID_RE.fullmatch(slug):
        fail("--slug 只能用小写字母、数字和连字符，例如 weekly-review")
    root = user_root() if args.scope == "user" else project_root(ctx.project)
    target = root / slug
    if target.exists():
        fail(f"已存在：{display(target, ctx.project)}", "conflict")
    extra: dict[str, str] = {}
    stamp = today().isoformat()
    if args.from_file:
        text = Path(args.from_file).expanduser().read_text(encoding="utf-8")
        meta, body = parse_sop_text(text)
    elif args.from_case:
        meta, body = parse_sop_text(template_source("blank")[0])
        meta, body = from_case(ctx, args.from_case, meta, body)
    else:
        text, extra = template_source(args.template)
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
                        "body": body, "text": text, "error": None, "shadowed": False}, ctx.defaults, strict=False)
    result = plan_result(ctx.project, writes, args.apply, {"id": slug, "status": "draft", "issues": issues,
                                                           "next": [f"sop.py interview {slug}（谈清 {len(gaps(meta, ctx.defaults))} 项）",
                                                                    "填完后 sop.py validate，再 sop.py activate"]})
    lines = [f"草稿 {slug}（{args.scope} 级）→ {display(target / 'SOP.md', ctx.project)}"] + \
            [f"  {item['level']}: {item['message']}" for item in issues] + \
            ["", result.get("verify") or "以上为预览，没有写入。确认后加 --apply。草稿状态不会被触发。"]
    return 0, result, lines


def from_case(ctx: Context, token: str, meta: dict, body: str) -> tuple[dict, str]:
    try:
        cases = case_tool.Project(ctx.project)
        directory = cases.find(token)
        data, case_body = case_tool.read_case(directory / "CASE.md")
    except case_tool.CaseError as exc:
        raise SopError(f"读不了 Case：{exc}", "unavailable") from exc
    marker = "## 工作记录\n"
    start = case_body.find(marker)
    end = case_body.find("\n## ", start + len(marker)) if start >= 0 else -1
    records = [re.sub(r"^\d{4}-\d{2}-\d{2}T\S+\s+", "", line[2:].strip()) for line in
               (case_body[start + len(marker):None if end < 0 else end].splitlines() if start >= 0 else []) if line.startswith("- ")]
    steps = [f"{index}. {text}\n   - 说明：{PLACEHOLDER}整理这一步（来自 {data.get('id')} 的工作记录，需改写成可重复执行的动作）"
             for index, text in enumerate([r for r in records if "Case 已创建" not in r], 1)]
    meta["description"] = f"{PLACEHOLDER}（从 {data.get('id')} 提炼）"
    meta["sources"] = [cases.relative(directory / "CASE.md")]
    meta["origin"] = f"case:{data.get('id')}"
    sections = split_sections(body)
    sections["步骤"] = "\n".join(steps) or f"1. {PLACEHOLDER}"
    return meta, "# {{title}}\n\n" + "".join(f"## {name}\n\n{sections.get(name, PLACEHOLDER)}\n\n" for name in SECTIONS)


def set_status_plan(ctx: Context, sop: dict, status: str, note: str) -> dict:
    meta = dict(sop["meta"])
    meta["status"] = status
    if status == "active":
        meta["verified_at"] = today().isoformat()
    body = append_revision(sop["body"], f"{today().isoformat()} v{meta['version']}：{note}")
    text = render_sop(meta, body)
    writes = [{"path": sop["path"], "content": text, "expect": sha(sop["text"])}]
    snapshot = snapshot_write(sop["dir"], str(meta["version"]), text)
    if snapshot and status == "active":
        writes.append(snapshot)
    return {"writes": writes, "meta": meta, "text": text}


def cmd_activate(ctx: Context) -> tuple[int, dict, list[str]]:
    sop = ctx.one(ctx.args.sop)
    if sop["error"]:
        fail(f"无法读取：{sop['error']}", "invalid")
    if sop["meta"].get("status") == "active":
        fail("已经是 active", "conflict")
    probe = dict(sop, meta={**sop["meta"], "status": "active"})
    blockers = [item for item in check_sop(probe, ctx.defaults, strict=True) if item["level"] == "error"]
    if blockers:
        lines = [f"{sop['id']} 还不能启用："] + [f"  - {item['message']}" for item in blockers]
        return 1, {"id": sop["id"], "applied": False, "blockers": blockers}, lines
    plan = set_status_plan(ctx, sop, "active", "启用（内容与执行约定已与用户确认）")
    result = plan_result(ctx.project, plan["writes"], ctx.args.apply, {"id": sop["id"], "status": "active"})
    return 0, result, [f"{'已启用' if result['applied'] else '将启用'} {sop['id']}@{sop['meta']['version']}"] + \
        ([] if result["applied"] else ["以上为预览，没有写入。确认后加 --apply。"])


def cmd_retire(ctx: Context) -> tuple[int, dict, list[str]]:
    sop = ctx.one(ctx.args.sop)
    target = "retired" if ctx.args.command == "retire" else "active"
    if sop["meta"].get("status") == target:
        fail(f"已经是 {target}", "conflict")
    if target == "active":
        probe = dict(sop, meta={**sop["meta"], "status": "active"})
        blockers = [item for item in check_sop(probe, ctx.defaults, strict=True) if item["level"] == "error"]
        if blockers:
            return 1, {"id": sop["id"], "applied": False, "blockers": blockers}, \
                [f"{sop['id']} 不能恢复为 active："] + [f"  - {item['message']}" for item in blockers]
    plan = set_status_plan(ctx, sop, target, ctx.args.note or ("退役" if target == "retired" else "恢复"))
    result = plan_result(ctx.project, plan["writes"], ctx.args.apply, {"id": sop["id"], "status": target})
    return 0, result, [f"{'已' if result['applied'] else '将'}{'退役' if target == 'retired' else '恢复'} {sop['id']}"] + \
        ([] if result["applied"] else ["以上为预览，没有写入。确认后加 --apply。"])


def cmd_revise(ctx: Context) -> tuple[int, dict, list[str]]:
    """你改完 SOP.md 之后，用它登记新版本：升版本号、写修订记录、存当前内容的快照。"""
    sop = ctx.one(ctx.args.sop)
    if sop["error"]:
        fail(f"无法读取：{sop['error']}", "invalid")
    if not ctx.args.note:
        fail("revise 需要 --note 说明这次改了什么、为什么")
    version = bump(str(sop["meta"]["version"]), ctx.args.bump)
    meta = {**sop["meta"], "version": version, "verified_at": today().isoformat()}
    body = append_revision(sop["body"], f"{today().isoformat()} v{version}：{ctx.args.note}")
    text = render_sop(meta, body)
    writes = [{"path": sop["path"], "content": text, "expect": sha(sop["text"])}]
    snapshot = snapshot_write(sop["dir"], version, text)
    if snapshot:
        writes.append(snapshot)
    old_snapshot = sop["dir"] / SNAPSHOTS / f"{sop['meta']['version']}.md"
    if not old_snapshot.exists() and sop["meta"].get("status") == "active":
        writes.append({"path": old_snapshot, "content": sop["text"], "expect": None})
    result = plan_result(ctx.project, writes, ctx.args.apply, {"id": sop["id"], "from": sop["meta"]["version"], "to": version})
    return 0, result, [f"{sop['id']} {sop['meta']['version']} → {version}（{ctx.args.bump}）：{ctx.args.note}"] + \
        ([result["verify"]] if result["applied"] else ["以上为预览，没有写入。确认后加 --apply。"])


def cmd_decide(ctx: Context) -> tuple[int, dict, list[str]]:
    """记录用户已经确认的事项：--confirm trigger / requires 表示该部分内容用户看过并认可；--set 键=值 同时视为确认对应事项。"""
    sop = ctx.one(ctx.args.sop)
    if sop["error"]:
        fail(f"无法读取：{sop['error']}", "invalid")
    meta = dict(sop["meta"])
    decided = list(meta.get("decided") or [])
    valid = {key for key, _, _ in DECISIONS}
    for key in ctx.args.confirm or []:
        if key not in valid:
            fail(f"--confirm 只能是：{'、'.join(sorted(valid))}")
        decided.append(key)
    for pair in ctx.args.set or []:
        key, separator, value = pair.partition("=")
        if not separator:
            fail(f"--set 需要 键=值：{pair}")
        check_choice(key.strip(), value.strip())
        meta[key.strip()] = value.strip()
        if key.strip() in FIELD_DECISION:
            decided.append(FIELD_DECISION[key.strip()])
    meta["decided"] = sorted(set(decided), key=[key for key, _, _ in DECISIONS].index)
    text = render_sop(meta, sop["body"])
    remaining = [gap["key"] for gap in gaps(meta, ctx.defaults)]
    result = plan_result(ctx.project, [{"path": sop["path"], "content": text, "expect": sha(sop["text"])}], ctx.args.apply,
                         {"id": sop["id"], "decided": meta["decided"], "remaining": remaining})
    return 0, result, [f"{sop['id']} 已确认：{'、'.join(meta['decided']) or '（无）'}；还差：{'、'.join(remaining) or '无'}"] + \
        ([result["verify"]] if result["applied"] else ["以上为预览，没有写入。确认后加 --apply。"])


def cmd_match(ctx: Context) -> tuple[int, dict, list[str]]:
    found = match_sops(ctx.sops(), " ".join(ctx.args.text))
    lines = [f"{item['id']}@{item['version']}（{'；'.join(item['reasons'])}）"
             + (f" 注意不触发条件：{item['exclude']}" if item["exclude"] else "") for item in found]
    return (0 if found else 1), {"matches": found, "note": "仅为关键词预筛；是否真的触发由 Agent 对照 trigger / exclude 判断，默认先问用户。"}, \
        lines or ["没有关键词命中的 SOP（语义上是否相关仍由 Agent 判断）。"]


def cmd_due(ctx: Context) -> tuple[int, dict, list[str]]:
    runs, problem = last_runs(ctx.project)
    now = today()
    rows = [info for sop in ctx.sops() if not sop["shadowed"] and (info := due_info(sop, runs, now))]
    due = [row for row in rows if row["due"]]
    lines = [f"{'已到期' if row['due'] else '未到期'} {row['id']}（{row['title']}，{row['schedule']}）："
             f"上次 {row['last_run'] or '从未执行'}，下次 {row['next']}" + (f"，已超 {row['overdue_days']} 天" if row["overdue_days"] else "")
             for row in rows]
    if problem:
        lines.append(f"注意：读不到执行记录（{problem}），按 SOP 创建日期计算。")
    return 0, {"due": due, "schedules": rows, "today": now.isoformat(), "problem": problem}, lines or ["没有设置周期的 active SOP。"]


def cmd_events(ctx: Context) -> tuple[int, dict, list[str]]:
    if ctx.args.event not in EVENTS:
        fail(f"事件只支持：{'、'.join(EVENTS)}")
    if ctx.args.case:
        data = read_event_case(ctx, ctx.args.case)
        hints = event_hints(ctx.project, ctx.args.event, data, ctx.sops())
        note = "这个 Case 本身是 SOP 的一次执行，不再触发事件。" if data.get("sop") else ""
        lines = [f"{hint['id']}：{hint['trigger']}（运行：{hint['command']}）" for hint in hints]
        return 0, {"event": ctx.args.event, "case": data.get("id"), "sops": hints, "note": note}, \
            lines or [note or "没有 SOP 对这个 Case 的该事件感兴趣。"]
    found = [sop for sop in ctx.sops() if sop["meta"].get("status") == "active" and not sop["shadowed"]
             and ctx.args.event in (sop["meta"].get("on_events") or [])]
    rows = [{"id": sop["id"], "title": sop["meta"].get("title", ""), "trigger": sop["meta"].get("trigger", ""),
             "event_tags": sop["meta"].get("event_tags") or []} for sop in found]
    return 0, {"event": ctx.args.event, "sops": rows}, \
        [f"{row['id']}：{row['trigger']}" + (f"（仅标签 {'、'.join(row['event_tags'])}）" if row["event_tags"] else "")
         for row in rows] or ["没有 SOP 监听这个事件。"]


def read_event_case(ctx: Context, token: str) -> dict:
    try:
        cases = case_tool.Project(ctx.project)
        data, _ = case_tool.read_case(cases.find(token) / "CASE.md")
    except case_tool.CaseError as exc:
        raise SopError(f"读不了 Case：{exc}", "not-found") from exc
    return data


# ---------------------------------------------------------------- 执行：一次执行 = 一个 Case

def checklist_text(sop: dict, info: dict, habit: dict, params: list[str], runtime_note: str) -> str:
    version = sop["meta"]["version"]
    keep = habit["artifact_keep"]["value"]
    place = habit["artifact_dir"]["value"] or "本 Case 的 data/ 目录"
    lines = ["## SOP 执行清单", "", f"来源：`{sop['id']}@{version}`（{runtime_note}）", "", "执行约定（开始时从 SOP 与项目习惯固化，执行中不随 SOP 改动）：",
             f"- 出错时：{habit['on_error']['value']}" + (f"（最多重试 {habit['max_retries']['value']} 次）" if habit["on_error"]["value"] == "retry" else ""),
             f"- 缺少工具时：{habit['on_missing_tool']['value']}", f"- 中间产物：放在 {place}；完成后：{keep}",
             f"- 失败后重新执行：{habit['resume']['value']}", f"- 花钱或耗时的步骤：{habit['confirm_costly']['value']}",
             f"- 敏感度：{habit['sensitivity']['value']}" + ("（执行记录不进资料索引；产物只放本 Case 目录内，不要提交到公开仓库）"
                                                        if habit["sensitivity"]["value"] == "private" else ""), ""]
    if params:
        lines += ["参数："] + [f"- {item}" for item in params] + [""]
    lines += ["步骤："] + [f"- [ ] {step['n']}. {step['title']}" + (f"（工具：{'、'.join(step['tools'])}）" if step["tools"] else "")
                        for step in info["steps"]]
    return "\n".join(lines)


def insert_before(body: str, heading: str, text: str) -> str:
    marker = f"## {heading}\n"
    position = body.find(marker)
    block = text.rstrip("\n") + "\n\n"
    return body.rstrip("\n") + "\n\n" + block if position < 0 else body[:position] + block + body[position:]


def cmd_run(ctx: Context) -> tuple[int, dict, list[str]]:
    sop = ctx.one(ctx.args.sop)
    status = sop["meta"].get("status")
    if sop["error"]:
        fail(f"无法读取：{sop['error']}", "invalid")
    if status != "active" and not ctx.args.trial:
        fail(f"{sop['id']} 是 {status}，不能正式执行", "conflict", ["草稿试跑请加 --trial（Case 会带 sop-trial 标签）", "正式使用先 sop.py activate"])
    if sop["meta"].get("creates_case") == "never":
        fail("这个 SOP 声明了 creates_case=never，没有执行记录可建", "conflict")
    info = parsed(sop)
    if not info["steps"]:
        fail("SOP 没有可执行的步骤", "invalid")
    habit = effective(sop["meta"], ctx.defaults)
    report = preflight(sop, ctx.project, ctx.defaults)
    version = str(sop["meta"]["version"])
    existing = sop["dir"] / SNAPSHOTS / f"{version}.md"
    if status == "active" and existing.is_file() and existing.read_text(encoding="utf-8") != sop["text"]:
        fail(f"{sop['id']}@{version} 的内容比版本快照新，但版本号没升", "conflict",
             [f"先 sop.py revise {sop['id']} --bump patch --note …，再执行"])
    snapshot = snapshot_write(sop["dir"], version, sop["text"])
    note = f"快照 {display(sop['dir'] / SNAPSHOTS / (version + '.md'), ctx.project)}"
    event_note = ""
    params = list(ctx.args.param or [])
    if ctx.args.event:
        if ctx.args.event not in (sop["meta"].get("on_events") or []):
            fail(f"{sop['id']} 没有声明监听 {ctx.args.event}", "conflict", ["在 SOP 的 on_events 里声明后用 sop.py revise 登记"])
        if not ctx.args.event_case:
            fail("--event 需要同时给出 --event-case（触发它的 Case）")
        trigger = read_event_case(ctx, ctx.args.event_case)
        if trigger.get("sop"):
            fail("SOP 自己的执行 Case 不能再触发事件", "conflict")
        event_note = f"由 {ctx.args.event} 触发（{trigger.get('id')}）"
        params.insert(0, f"触发事件：{ctx.args.event}（{trigger.get('id')}：{trigger.get('title', '')}）")
    goal = (info["sections"].get("目的", "").strip().splitlines() or [sop["meta"].get("title", sop["id"])])[0]
    done_when = "；".join(line.strip("- ").strip() for line in info["sections"].get("完成标准", "").splitlines() if line.strip())[:300]
    title = ctx.args.title or f"{sop['meta'].get('title', sop['id'])} {today().isoformat()}"
    case_args = argparse.Namespace(title=title, slug=sop["id"], goal=goal, next_action=f"步骤 1：{info['steps'][0]['title']}",
                                   status="active", done_when=done_when,
                                   source=f"SOP {sop['id']}@{version}" + (f"；{event_note}" if event_note else ""),
                                   current_state=f"SOP 进度 0/{len(info['steps'])}",
                                   tag=["sop", *(["sop-trial"] if status != "active" else [])], sop=f"{sop['id']}@{version}",
                                   sensitivity=habit["sensitivity"]["value"])
    try:
        cases = case_tool.Project(ctx.project)
        case_id, case_text = case_tool.plan_create(cases, case_args)
    except case_tool.CaseError as exc:
        raise SopError(f"不能建立执行记录：{exc}", "unavailable", ["执行记录需要项目启用 case-workspace（Profile work.mode）"]) from exc
    section = checklist_text(sop, info, habit, params, note)
    preview = {"case_id": case_id, "title": title, "steps": [step["title"] for step in info["steps"]],
               "preflight": {"ready": report["ready"], "counts": report["counts"]}, "snapshot": bool(snapshot),
               "checklist": section}
    lines = [f"将建立 Case：{case_id}（{title}）", "", section, "", "中间产物：执行中用 sop.py step … --artifact 路径:说明 登记。",
             f"执行前检查：{report['counts']['ok']} 项就绪，{report['counts']['missing']} 项缺失，{report['counts']['unknown']} 项待核对"
             + ("" if report["ready"] else "（先处理：sop.py preflight " + sop["id"] + "）")]
    if not ctx.args.apply:
        return 0, {"applied": False, **preview}, lines + ["以上为预览，没有写入。确认后加 --apply。"]
    if snapshot:
        apply_writes([snapshot])
    created = case_tool.create(cases, case_args)
    path = ctx.project / created["path"]
    data, body = case_tool.read_case(path)
    body = insert_before(body, "阻塞与恢复条件", section + "\n\n## 中间产物\n\n暂无。")
    body = case_tool.append_section(body, "工作记录", f"{case_tool.now()} 按 {sop['id']}@{version} 开始执行{('，' + event_note) if event_note else ''}；执行前检查：就绪 {report['counts']['ok']}、缺失 {report['counts']['missing']}、待核对 {report['counts']['unknown']}")
    case_tool.write_atomic(path, case_tool.render_frontmatter(data) + body)
    return 0, {"applied": True, **preview, "case_id": created["id"], "path": created["path"]}, \
        lines + [f"已建立：{created['path']}"]


def read_run(ctx: Context, token: str) -> tuple[Path, dict, str, list[dict]]:
    try:
        cases = case_tool.Project(ctx.project)
        path = cases.find(token) / "CASE.md"
        data, body = case_tool.read_case(path)
    except case_tool.CaseError as exc:
        raise SopError(str(exc), "not-found") from exc
    section = split_sections(body).get("SOP 执行清单", "")
    steps = []
    for line in section.splitlines():
        match = CHECK_RE.match(line)
        if match:
            steps.append({"n": int(match.group(2)), "mark": match.group(1), "state": MARKS[match.group(1)], "text": match.group(3)})
    if not steps:
        fail(f"{data.get('id', token)} 不是 SOP 执行记录（没有「SOP 执行清单」）", "not-found")
    return path, data, body, steps


def section_value(body: str, heading: str, prefix: str) -> str:
    for line in split_sections(body).get(heading, "").splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def cmd_step(ctx: Context) -> tuple[int, dict, list[str]]:
    args = ctx.args
    path, data, body, steps = read_run(ctx, args.case)
    current = path.read_text(encoding="utf-8")
    if args.expect_sha256 and sha(current) != args.expect_sha256:
        fail("Case 在预览之后被修改过，请重新读取", "conflict")
    wanted = next((step for step in steps if step["n"] == args.n), None)
    if wanted is None:
        fail(f"没有步骤 {args.n}（共 {len(steps)} 步）", "not-found")
    flags = [name for name in ("done", "skip", "fail", "pending") if getattr(args, name)]
    if len(flags) != 1:
        fail("--done / --skip / --fail / --pending 必须且只能给一个")
    state = flags[0]
    if state in ("skip", "fail") and not (args.note or "").strip():
        fail("--skip 和 --fail 必须用 --note 写明原因")
    mark = {"done": "x", "skip": "-", "fail": "!", "pending": " "}[state]
    label = {"done": "完成", "skip": "跳过", "fail": "失败", "pending": "重置为待办"}[state]
    body = body.replace(f"- [{wanted['mark']}] {wanted['n']}. {wanted['text']}", f"- [{mark}] {wanted['n']}. {wanted['text']}", 1)
    stamp = case_tool.now()
    note = f"：{args.note.strip()}" if args.note else ""
    body = case_tool.append_section(body, "工作记录", f"{stamp} SOP 步骤 {args.n}「{wanted['text'].split('（工具')[0]}」{label}{note}")
    warnings = []
    for item in args.artifact or []:
        location, _, what = item.partition(":")
        if data.get("sensitivity") == "private" and not (path.parent / Path(location.strip()).expanduser()).resolve().is_relative_to(path.parent.resolve()):
            warnings.append(f"这次执行是 private，但产物 {location.strip()} 不在本 Case 目录内：请确认它不会被提交到公开位置")
        body = case_tool.append_section(body, "中间产物", f"步骤 {args.n}：`{location.strip()}`" + (f"（{what.strip()}）" if what.strip() else ""))
    states = {step["n"]: step["mark"] for step in steps}
    states[args.n] = mark
    pending = [step for step in steps if states[step["n"]] == " "]
    failed = [step for step in steps if states[step["n"]] == "!"]
    habit = section_value(body, "SOP 执行清单", "- 出错时：") or "ask"
    if failed:
        next_action = f"处理步骤 {failed[0]['n']} 的失败（约定：{habit}）" + (f"：{args.note.strip()}" if args.note and state == "fail" else "")
    elif pending:
        next_action = f"步骤 {pending[0]['n']}：{pending[0]['text'].split('（工具')[0]}"
    else:
        next_action = "核对完成标准（sop.py status）后关闭 Case 或登记交付物"
    finished = sum(1 for key in states.values() if key in ("x", "-"))
    data.update({"next_action": next_action, "current_state": f"SOP 进度 {finished}/{len(steps)}"
                 + (f"，步骤 {failed[0]['n']} 失败" if failed else ""), "updated_at": stamp})
    body = case_tool._replace_section(body, "当前可信状态", data["current_state"])  # noqa: SLF001
    body = case_tool._replace_section(body, "下一动作", next_action)  # noqa: SLF001
    errors = case_tool.validate_schema(data, case_tool.Project(ctx.project).schema)
    if errors:
        fail("更新后 Case 不符合 Schema：" + "；".join(errors), "invalid")
    text = case_tool.render_frontmatter(data) + body
    result = {"case": data["id"], "step": args.n, "state": state, "next_action": next_action, "progress": f"{finished}/{len(steps)}",
              "applied": False, "sha256_before": sha(current), "warnings": warnings}
    if args.apply:
        if path.read_text(encoding="utf-8") != current:
            fail("Case 刚被其他人修改，请重新读取", "conflict")
        case_tool.write_atomic(path, text)
        result["applied"] = True
    return 0, result, [f"{data['id']} 步骤 {args.n} {label}；进度 {finished}/{len(steps)}；下一步：{next_action}"] + \
        [f"注意：{warning}" for warning in warnings] + \
        ([] if args.apply else ["以上为预览，没有写入。确认后加 --apply（用户已确认执行该 SOP 时，步骤记录属于这次授权的范围）。"])


def cmd_status(ctx: Context) -> tuple[int, dict, list[str]]:
    path, data, body, steps = read_run(ctx, ctx.args.case)
    artifacts = [line[2:] for line in split_sections(body).get("中间产物", "").splitlines() if line.startswith("- ")]
    pending = [step for step in steps if step["mark"] == " "]
    failed = [step for step in steps if step["mark"] == "!"]
    skipped = [step for step in steps if step["mark"] == "-"]
    complete = not pending and not failed
    lines = [f"{data['id']}（{data.get('sop', '')}）进度：{len(steps) - len(pending) - len(failed)}/{len(steps)}"]
    lines += [f"  [{step['mark']}] {step['n']}. {step['text']}（{step['state']}）" for step in steps]
    lines += ["中间产物：" + ("；".join(artifacts) if artifacts else "无")]
    lines += ["可以核对完成标准并收尾。" if complete else "还有未完成或失败的步骤。"]
    return (0 if complete else 1), {"case": data["id"], "sop": data.get("sop", ""), "steps": steps, "complete": complete,
                                    "skipped": [step["n"] for step in skipped], "failed": [step["n"] for step in failed],
                                    "artifacts": artifacts,
                                    "keep_policy": section_value(body, "SOP 执行清单", "- 中间产物：")}, lines


def cmd_init(ctx: Context) -> tuple[int, dict, list[str]]:
    root = project_root(ctx.project)
    writes = [] if root.is_dir() else [{"path": root / ".gitkeep", "content": "", "expect": None}]
    result = plan_result(ctx.project, writes, ctx.args.apply, {"root": display(root, ctx.project)})
    return 0, result, [f"SOP 目录：{display(root, ctx.project)}" + ("（已存在）" if not writes else ""),
                       "可在 Profile 加 \"sop\": {\"root\": \"sops\"}，让 check 把它列入脚手架（可选，不加也能用）。"] + \
        ([] if result["applied"] or not writes else ["以上为预览，没有写入。确认后加 --apply。"])


COMMANDS = {"list": cmd_list, "index": cmd_index, "show": cmd_show, "validate": cmd_validate, "interview": cmd_interview,
            "defaults": cmd_defaults, "preflight": cmd_preflight, "new": cmd_new, "activate": cmd_activate,
            "retire": cmd_retire, "restore": cmd_retire, "decide": cmd_decide, "revise": cmd_revise, "match": cmd_match, "due": cmd_due,
            "events": cmd_events, "run": cmd_run, "step": cmd_step, "status": cmd_status, "init": cmd_init}


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
            item.add_argument("--apply", action="store_true", help="执行写入（缺省只预览）")
        return item

    listing = add("list", "列出 SOP")
    listing.add_argument("--all", action="store_true", help="含已退役")
    listing.add_argument("--scope", choices=("project", "user"))
    add("index", "active SOP 的索引（每个一行，Agent 读它判断是否触发）")
    add("show", "查看一个 SOP").add_argument("sop")
    add("validate", "校验结构、前置条件、触发与版本").add_argument("sop", nargs="?")
    add("interview", "创建与配置时要谈清的事").add_argument("sop", nargs="?")
    defaults = add("defaults", "查看或设置本项目的使用习惯", write=True)
    defaults.add_argument("--set", action="append", metavar="键=值")
    add("preflight", "执行前检查所需程序与环境").add_argument("sop")
    new = add("new", "创建草稿", write=True)
    new.add_argument("--slug", required=True)
    new.add_argument("--title")
    new.add_argument("--template", default="blank", help="样例：blank / video-to-transcript / weekly-review")
    new.add_argument("--from-file", help="用现成的 SOP.md 作为草稿")
    new.add_argument("--from-case", help="从一个已完成的 Case 的工作记录提炼草稿")
    new.add_argument("--scope", choices=("project", "user"), default="project")
    add("activate", "启用草稿（要求内容与约定都已确认）", write=True).add_argument("sop")
    for name, text in (("retire", "退役"), ("restore", "恢复为 active")):
        item = add(name, text, write=True)
        item.add_argument("sop")
        item.add_argument("--note")
    decide = add("decide", "记录用户已确认的事项（触发、前置条件、出错/缺工具/中间产物等约定）", write=True)
    decide.add_argument("sop")
    decide.add_argument("--confirm", action="append", metavar="trigger|requires")
    decide.add_argument("--set", action="append", metavar="键=值")
    revise = add("revise", "登记新版本：升版本号、写修订记录、存快照", write=True)
    revise.add_argument("sop")
    revise.add_argument("--bump", choices=("patch", "minor", "major"), required=True)
    revise.add_argument("--note")
    add("match", "关键词预筛").add_argument("text", nargs="+")
    add("due", "哪些有周期的 SOP 到期了")
    events = add("events", "监听某事件的 SOP；加 --case 只看对这个 Case 会触发的")
    events.add_argument("event")
    events.add_argument("--case", help="发生事件的 Case：按它的标签过滤，SOP 自己的执行 Case 不触发")
    run = add("run", "按 SOP 建立一次执行（Case）", write=True)
    run.add_argument("sop")
    run.add_argument("--title")
    run.add_argument("--param", action="append", metavar="名称=值")
    run.add_argument("--trial", action="store_true", help="试跑草稿")
    run.add_argument("--event", choices=EVENTS, help="由哪个事件触发（需同时给 --event-case）")
    run.add_argument("--event-case", help="触发事件的 Case")
    step = add("step", "记录某一步的结果", write=True)
    step.add_argument("case")
    step.add_argument("n", type=int)
    for name in ("done", "skip", "fail", "pending"):
        step.add_argument(f"--{name}", action="store_true")
    step.add_argument("--note")
    step.add_argument("--artifact", action="append", metavar="路径[:说明]",
                      help="登记中间产物；路径相对本 Case 目录（目录外的用绝对路径或 ../）")
    step.add_argument("--expect-sha256")
    add("status", "一次执行的进度与中间产物").add_argument("case")
    add("init", "创建 SOP 目录", write=True)
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
