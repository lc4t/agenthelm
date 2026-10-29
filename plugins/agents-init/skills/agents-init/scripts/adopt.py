#!/usr/bin/env python3
"""接管已有内容的目录：只读扫描冲突，用户选择后只新增、不动历史。

- ``scan``：只读。列出骨架文件与已有路径的碰撞、目录含义是否相同（语义碰撞）、`AGENTS.md` / `CLAUDE.md`
  的状况，每项给出选项和推荐；推荐只是建议，工具不替用户选择。
- ``adopt``：默认预览，``--apply`` 才写入。已有文件默认原样保留，补上缺失的骨架；改动已有文件
  （追加受管区块、拆分、并排）必须由用户显式选择，并先备份到项目外的 ``--recovery-dir``。
  保留下来的、与模板不同的文件记入决定文件，``check`` 不再追问。

历史内容不需要逐项处理：与框架骨架冲突的旧文件可以长期并存，新增内容按标准结构落位，
历史迁移由用户在合适的时候选择。
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import regions
from i18n_init import _  # noqa: E402

init: Any = None  # init_project 模块；由 bind() 设置

AGENTS = Path("AGENTS.md")
CLAUDE = Path("CLAUDE.md")
USER_FILE = Path("AGENT.USER.md")
RULES_FILE = Path("AGENT.RULES.md")
AGENTS_POLICIES = ("keep", "append", "split", "side-by-side")
CLAUDE_POLICIES = ("keep", "add-import")
ROUTE_DESTINATIONS = ("agents", "rules", "user", "opinion", "sop")
# 名字像“工作容器”的顶层目录：登记为 work.assets.roots 后可被查找，内容不动。
CONTAINER_NAMES = {"cases", "case", "tasks", "task", "projects", "project", "work", "jobs", "案例", "项目", "事项", "任务", "工作"}
OTHER_AGENT_FILES = (".cursorrules", ".cursor/rules", ".github/copilot-instructions.md", "GEMINI.md", ".windsurfrules")
IMPORT_RE = re.compile(r"^\s*@(?:\./)?AGENTS\.md\s*$", re.MULTILINE)
HEADING_RE = re.compile(r"^(#{1,2})\s+(.*\S)\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
# (目的地, 关键词)；按顺序匹配，先匹配到的生效。关键词只是启发式，最终由用户或 session 里的判断决定。
SECTION_HINTS = (
    ("sop", ("流程", "步骤", "sop", "workflow", "checklist", "发布", "release", "部署", "deploy", "runbook", "手册")),
    ("rules", ("规则", "约束", "规范", "禁止", "必须", "不得", "不要", "never", "must", "do not", "don't", "安全", "隐私",
               "security", "privacy", "rules", "constraints", "conventions", "约定", "代码风格", "code style", "lint", "格式", "format")),
    ("opinion", ("偏好", "语气", "口吻", "回复", "表达", "tone", "persona", "沟通", "prefer", "语言")),
    ("agents", ("命令", "构建", "测试", "运行", "安装", "目录", "结构", "架构", "概览", "简介", "overview", "command",
                "build", "test", "run", "install", "architecture", "structure", "setup", "开发", "环境")),
)


# 写进项目文件的文字跟项目文件语言（Profile 的 language），不随界面语言变化。
FILE_TEXT = {
    "zh": {"user_header": "# 接管前的用户内容\n\n本文件由 agenthelm 在接管项目时从 AGENTS.md 移出，原文保留。`AGENTS.md` 要求与本文件一并遵循。\n\n",
           "rules_heading": "\n## 接管前的用户规则（来自原 AGENTS.md）\n\n",
           "opinion": "待交给 opinion-manager 处理（偏好与表达类）", "sop": "待并入 SOP 机制（流程类）"},
    "en": {"user_header": "# User content from before adoption\n\nagenthelm moved this out of AGENTS.md when it adopted the project; the original text is kept. "
                          "`AGENTS.md` asks agents to follow this file as well.\n\n",
           "rules_heading": "\n## User rules from before adoption (from the original AGENTS.md)\n\n",
           "opinion": "To hand over to opinion-manager (preferences and style)", "sop": "To merge into the SOP mechanism (procedures)"},
}


def file_text(language: str, key: str) -> str:
    return FILE_TEXT.get(language, FILE_TEXT["zh"])[key]


def bind(module: Any) -> None:
    global init
    init = module


def _fail(message: str, code: str = "invalid-request") -> Exception:
    return init.InitError(message, code=code)


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _read(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------- AGENTS.md sections

@dataclass(frozen=True)
class Section:
    index: int
    heading: str
    first: int  # 起始行（含）
    last: int   # 结束行（不含）
    suggestion: str
    matched: str | None


def classify(heading: str) -> tuple[str, str | None]:
    lowered = heading.lower()
    for destination, words in SECTION_HINTS:
        for word in words:
            if word in lowered:
                return destination, word
    return "agents", None


def sections(text: str) -> list[Section]:
    """Split by level 1-2 headings outside code fences; the text before the first heading is section 0."""
    lines = text.splitlines(keepends=True)
    starts: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(lines):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else HEADING_RE.match(line)
        if match:
            starts.append((number, match.group(2)))
    result: list[Section] = []
    if not starts or starts[0][0] > 0:
        end = starts[0][0] if starts else len(lines)
        if end > 0 and "".join(lines[:end]).strip():
            result.append(Section(0, _("（开头）"), 0, end, "agents", None))
    for position, (number, heading) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        destination, matched = classify(heading)
        result.append(Section(0, heading, number, end, destination, matched))
    return [Section(i + 1, s.heading, s.first, s.last, s.suggestion, s.matched) for i, s in enumerate(result)]


def parse_routes(values: list[str], found: list[Section]) -> dict[int, str]:
    routes: dict[int, str] = {}
    valid = {section.index for section in found}
    for value in values:
        number, _sep, destination = value.partition("=")
        if not number.strip().isdigit() or destination not in ROUTE_DESTINATIONS:
            raise _fail(_("--route 格式为 <序号>=<{0}>：{1}").format("|".join(ROUTE_DESTINATIONS), value))
        if int(number) not in valid:
            raise _fail(_("--route 序号不存在：{0}（先用 scan 查看章节序号）").format(number))
        routes[int(number)] = destination
    return routes


# ---------------------------------------------------------------- scan

def _option(option_id: str, label: str, flag: str | None = None) -> dict:
    return {"id": option_id, "label": label, "flag": flag}


def _finding(identifier: str, kind: str, path: str, state: str, summary: str, *, options: list[dict] | None = None,
             recommended: str | None = None, detail: dict | None = None) -> dict:
    return {"id": identifier, "kind": kind, "path": path, "state": state, "summary": summary,
            "options": options or [], "recommended": recommended, "detail": detail or {}}


def _git_dirty(root: Path) -> int | None:
    if not (root / ".git").exists():
        return None
    try:
        done = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True,
                              check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return len([line for line in done.stdout.splitlines() if line.strip()]) if done.returncode == 0 else None


def _contract_findings(root: Path, expected: dict[Path, str]) -> list[dict]:
    found: list[dict] = []
    text = _read(root / AGENTS)
    if _lstat(root / AGENTS) is None:
        found.append(_finding("adopt:contract:AGENTS.md", "contract", "AGENTS.md", "absent", _("没有 AGENTS.md，将按模板新建")))
    elif text is None:
        found.append(_finding("adopt:contract:AGENTS.md", "contract", "AGENTS.md", "unreadable",
                              _("AGENTS.md 是符号链接或无法按 UTF-8 读取，保持原样")))
    elif text == expected[AGENTS]:
        found.append(_finding("adopt:contract:AGENTS.md", "contract", "AGENTS.md", "same", _("AGENTS.md 与模板一致")))
    else:
        found_sections = sections(text)
        region = regions.get(text, "agents-contract")
        found.append(_finding(
            "adopt:contract:AGENTS.md", "contract", "AGENTS.md", "user-content",
            _("AGENTS.md 已有用户内容（{0} 字节，{1} 个章节）").format(len(text.encode('utf-8')), len(found_sections)),
            options=[
                _option("keep", _("保持原样；之后项目契约不会出现在 AGENTS.md 里"), "--agents-policy keep"),
                _option("append", _("原文不动，末尾追加带标记的项目契约区块（可随时撤回）"), "--agents-policy append"),
                _option("split", _("按章节决定去向：留在 AGENTS.md，或移到 AGENT.RULES.md / AGENT.USER.md；并追加契约区块"),
                        _("--agents-policy split --route <序号>=<agents|rules|user|opinion|sop>")),
                _option("side-by-side", _("原文整体移到 AGENT.USER.md，AGENTS.md 换成模板（AGENTS.md 里会指向 AGENT.USER.md）"),
                        "--agents-policy side-by-side"),
            ],
            recommended="append",
            detail={"has_contract_region": region is not None,
                    "sections": [{"index": s.index, "heading": s.heading, "lines": s.last - s.first,
                                  "suggested": s.suggestion, "matched": s.matched,
                                  "confidence": "keyword" if s.matched else "default"} for s in found_sections],
                    "note": _("章节去向只是关键词启发式的建议；opinion / sop 暂由 opinion-manager 与后续 SOP 机制处理，"
                              "工具会先把它们放进 AGENT.USER.md 并标注待交接。")},
        ))
    claude = _lstat(root / CLAUDE)
    if claude is None:
        found.append(_finding("adopt:contract:CLAUDE.md", "contract", "CLAUDE.md", "absent",
                              _("没有 CLAUDE.md，将新建一行 @AGENTS.md 的导入桩")))
    elif (root / CLAUDE).is_symlink():
        target = os.path.realpath(root / CLAUDE)
        same = target == os.path.realpath(root / AGENTS)
        found.append(_finding("adopt:contract:CLAUDE.md", "contract", "CLAUDE.md", "symlink-agents" if same else "symlink-other",
                              _("CLAUDE.md 是指向 AGENTS.md 的符号链接，Claude 读到的就是 AGENTS.md") if same
                              else _("CLAUDE.md 是指向其他位置的符号链接，保持原样")))
    else:
        body = _read(root / CLAUDE)
        if body is None:
            found.append(_finding("adopt:contract:CLAUDE.md", "contract", "CLAUDE.md", "unreadable", _("CLAUDE.md 无法读取，保持原样")))
        elif body == expected[CLAUDE]:
            found.append(_finding("adopt:contract:CLAUDE.md", "contract", "CLAUDE.md", "same", _("CLAUDE.md 与模板一致")))
        elif IMPORT_RE.search(body):
            found.append(_finding("adopt:contract:CLAUDE.md", "contract", "CLAUDE.md", "imports-agents",
                                  _("CLAUDE.md 已导入 AGENTS.md，Claude 能读到项目契约")))
        else:
            found.append(_finding(
                "adopt:contract:CLAUDE.md", "contract", "CLAUDE.md", "shadows-agents",
                _("CLAUDE.md 已有内容但没有导入 AGENTS.md：Claude Code 两者并存时只读 CLAUDE.md，AGENTS.md 里的项目契约不会加载"),
                options=[_option("keep", _("保持原样"), "--claude-policy keep"),
                         _option("add-import", _("原文不动，末尾追加带标记的 @AGENTS.md 导入区块（可随时撤回）"), "--claude-policy add-import")],
                recommended="add-import"))
    return found


def _top_entries(path: Path) -> list[Path]:
    try:
        return [p for p in sorted(path.iterdir()) if p.name != ".gitkeep"]
    except OSError:
        return []


def _semantic_findings(root: Path, case_root: str, memory_root: str, work_mode: str) -> list[dict]:
    found: list[dict] = []
    cases = root / case_root
    if _is_dir(cases) and work_mode == "case-workspace":
        entries = _top_entries(cases)
        ours = [p.name for p in entries if _is_dir(p) and (p / "CASE.md").is_file()]
        foreign = [p.name for p in entries if p.name not in ours]
        if not foreign:
            found.append(_finding(f"adopt:semantic:{case_root}", "semantic", case_root, "ours" if ours else "empty",
                                  _("{0}/ 已存在，里面的 {1} 个目录都有 CASE.md").format(case_root, len(ours)) if ours
                                  else _("{0}/ 已存在且为空").format(case_root)))
        else:
            suggestion = _unused_name(root, "cases-managed")
            found.append(_finding(
                f"adopt:semantic:{case_root}", "semantic", case_root, "mixed" if ours else "foreign",
                _("{0}/ 已有 {1} 个没有 CASE.md 的条目（{2}），它们是历史内容：Case 工具会忽略它们，新 Case 与它们并存").format(
                    case_root, len(foreign), ", ".join(foreign[:3]) + ("…" if len(foreign) > 3 else "")),
                options=[_option("coexist", _("并存：新 Case 放进同一目录，历史条目不动"), None),
                         _option("alt-case-root", _("新 Case 改放到另一个目录（如 {0}/），{1}/ 完全不碰").format(suggestion, case_root),
                                 f"--case-root {suggestion}")],
                recommended="coexist", detail={"foreign": foreign[:50], "ours": ours[:50]}))
    for entry in sorted(root.iterdir()) if root.is_dir() else []:
        name = entry.name
        if name in (case_root, memory_root) or name.lower() not in CONTAINER_NAMES or not _is_dir(entry):
            continue
        children = [p for p in _top_entries(entry) if _is_dir(p)]
        if children:
            found.append(_finding(
                f"adopt:semantic:container:{name}", "semantic", name, "container",
                _("{0}/ 看起来是已有的工作容器（{1} 个子目录），内容不动").format(name, len(children)),
                options=[_option("register-root", _("登记为额外资料根（work.assets.roots），之后可被查找，不改任何文件"),
                                 f"--register-root {name}"),
                         _option("ignore", _("不登记"), None)],
                recommended="register-root"))
    memory = root / memory_root
    if _is_dir(memory):
        entries = _top_entries(memory)
        if entries:
            suggestion = _unused_name(root, "agent-memory")
            found.append(_finding(
                f"adopt:semantic:{memory_root}", "semantic", memory_root, "foreign",
                _("{0}/ 已有 {1} 个条目，格式未知：随包记忆工具会在这个目录里写自己的条目，建议分开").format(memory_root, len(entries)),
                options=[_option("coexist", _("共用同一目录（记忆工具的索引可能把历史文件当作异常）"), None),
                         _option("alt-memory-root", _("记忆条目改放到 {0}/，{1}/ 不碰").format(suggestion, memory_root), f"--memory-root {suggestion}")],
                recommended="alt-memory-root", detail={"entries": [p.name for p in entries[:50]]}))
    docs = root / "docs"
    if _is_dir(docs):
        notes = [_("docs/{0}/ 已存在").format(name) for name in ("refs", "drafts") if _is_dir(docs / name)]
        found.append(_finding("adopt:semantic:docs", "semantic", "docs", "shared",
                              _("docs/ 已存在：骨架只在里面新增 refs/README.md 与 drafts/，已有文件不动")
                              + ("; " + ", ".join(notes) if notes else "")))
    agent_doc = root / ".agent-doc"
    if _is_dir(agent_doc):
        found.append(_finding("adopt:semantic:.agent-doc", "semantic", ".agent-doc", "shared",
                              _(".agent-doc/ 已存在：只补上缺少的 plan.md / progress.md / chat-summary.md")))
    return found


def _unused_name(root: Path, base: str) -> str:
    name, number = base, 1
    while (root / name).exists():
        number += 1
        name = f"{base}-{number}"
    return name


def scan(root: Path, inputs: Any, *, case_root: str = "cases", memory_root: str = "memory") -> dict:
    """Read-only report of how this directory's existing content meets the skeleton."""
    blockers: list[dict] = []
    if (root / init.LEGACY_PROFILE_PATH).exists() and not (root / init.PROFILE_PATH).exists():
        blockers.append({"code": "migrate-required", "message": _("检测到 v5 Project Profile，请改用 --mode migrate")})
    if (root / init.PROFILE_PATH).exists():
        blockers.append({"code": "already-initialized", "message": _("已有 v6 Project Profile，不是接管场景：用 check / refresh")})
    files = init.files_for(inputs)
    files.pop(Path("cases/.gitkeep"), None)
    files.pop(Path("memory/.gitkeep"), None)
    expected = files
    create, collisions, symlinked = init.plan_writes(root, files, skip_symlinked_parents=True)
    findings = _contract_findings(root, expected)
    findings += _semantic_findings(root, case_root, memory_root, inputs.work_mode)
    launcher = inputs.entry_launcher
    if launcher and _lstat(root / launcher) is not None:
        findings.append(_finding(
            f"adopt:launcher:{launcher}", "launcher", launcher, "name-taken",
            _("根目录已有 {0}，是你的文件：不创建启动器，也不在 Profile 里声明 entry").format(launcher),
            options=[_option("other-name", _("换一个启动器名称"), _("--entry-name <名称>")), _option("no-entry", _("不要启动器"), "--no-entry")],
            recommended="other-name"))
    other = [p for p in collisions if str(p) not in ("AGENTS.md", "CLAUDE.md") and str(p) != launcher]
    if other:
        tracked = {x.as_posix() for x in init.tracked_paths(init.contract_pack(), launcher)}
        differing = [str(p) for p in other if str(p) in tracked and _read(root / p) != expected.get(p)]
        findings.append(_finding(
            "adopt:exists", "exists", "", "preserved",
            _("{0} 个骨架路径已存在，原样保留（不覆盖）").format(len(other)),
            detail={"paths": [str(p) for p in other], "differs_from_template": differing}))
    if symlinked:
        findings.append(_finding("adopt:symlink", "symlink", "", "skipped",
                                 _("{0} 个骨架路径的父目录是符号链接，跳过").format(len(symlinked)),
                                 detail={"paths": [str(p) for p in symlinked]}))
    others = [name for name in OTHER_AGENT_FILES if _lstat(root / name) is not None]
    if others:
        findings.append(_finding("adopt:other-agent-files", "info", "", "present",
                                 _("其他客户端的指令文件不会被触碰：") + ", ".join(others), detail={"paths": others}))
    recommended = [option["flag"] for item in findings if item["recommended"]
                   for option in item["options"] if option["id"] == item["recommended"] and option["flag"]]
    return {"blockers": blockers, "findings": findings, "recommended_flags": recommended,
            "git_dirty_files": _git_dirty(root), "would_create": [str(p) for p in create],
            "would_preserve": [str(p) for p in collisions]}


# ---------------------------------------------------------------- adopt

def _validate_root(value: str, label: str, root: Path, *, must_exist: bool = False) -> str:
    path = Path(value)
    reserved = {".git", ".agents", ".codex", ".cursor", ".claude", "local-ai", "confidential"}
    if (not value or value == "." or path.is_absolute() or ".." in path.parts or "\\" in value
            or re.match(r"^[A-Za-z]:", value) or set(path.parts) & reserved):
        raise _fail(_("{0} 必须是项目内明确的相对目录：{1}").format(label, value), "profile-invalid")
    target = root / path
    if must_exist and not _is_dir(target):
        raise _fail(_("{0} 不是项目里已有的目录：{1}").format(label, value))
    if not must_exist and _lstat(target) is not None and not _is_dir(target):
        raise _fail(_("{0} 已被同名文件占用：{1}").format(label, value))
    for parent in [target, *target.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise _fail(_("{0} 经过符号链接：{1}").format(label, value))
    return path.as_posix()


def _moved_block(found: list[Section], lines: list[str], note: str | None = None) -> str:
    pieces = []
    for section in found:
        body = "".join(lines[section.first:section.last]).rstrip("\n")
        pieces.append((f"> {note}\n\n" if note else "") + body)
    return "\n\n".join(pieces)


def _plan_agents(text: str, policy: str, routes: dict[int, str], expected: str, body: str, language: str = "zh") -> dict:
    """What a policy does to a user's AGENTS.md: the new text, files that receive moved sections, region records."""
    plan: dict[str, Any] = {"policy": policy, "new_text": None, "replace": False, "user_text": None, "rules_block": None,
                            "region": None, "pending_handoff": [], "moved": {}}
    if policy == "keep":
        return plan
    if policy == "side-by-side":
        plan.update(new_text=expected, replace=True, user_text=text, moved={"user": [_("（整个文件）")]})
        return plan
    lines = text.splitlines(keepends=True)
    found = sections(text)
    stay, rules, user, handoff = [], [], [], []
    if policy == "split":
        for section in found:
            destination = routes.get(section.index, "agents")
            if destination == "agents":
                stay.append(section)
            elif destination == "rules":
                rules.append(section)
            else:
                user.append(section)
                if destination in ("opinion", "sop"):
                    handoff.append({"section": section.index, "heading": section.heading, "destination": destination})
        moved = {section.first for section in [*rules, *user]}
        kept_lines = [line for number, line in enumerate(lines)
                      if not any(s.first <= number < s.last for s in [*rules, *user])] if moved else lines
        text = "".join(kept_lines)
        if rules:
            plan["rules_block"] = file_text(language, "rules_heading") + _moved_block(rules, lines) + "\n"
            plan["moved"]["rules"] = [s.heading for s in rules]
        if user:
            blocks = []
            for section in user:
                destination = routes[section.index]
                note = file_text(language, destination) if destination in ("opinion", "sop") else None
                blocks.append(_moved_block([section], lines, note))
            plan["user_text"] = "\n\n".join(blocks)
            plan["moved"]["user"] = [s.heading for s in user]
        plan["pending_handoff"] = handoff
    new_text, state = regions.upsert(text, "agents-contract", body)
    plan.update(new_text=new_text, region=("agents-contract", regions.digest(body), state))
    return plan


def run(args: Any, root: Path, inputs: Any) -> tuple[int, dict]:
    """``--mode adopt``: preview, or with ``--apply`` add the skeleton around existing content."""
    from dataclasses import replace as dc_replace
    import json

    scaffold = init._scaffold()
    templates = init.load_templates()
    policy = getattr(args, "agents_policy", None) or "keep"
    claude_policy = getattr(args, "claude_policy", None) or "keep"
    case_root = _validate_root(getattr(args, "case_root", None) or "cases", "--case-root", root)
    memory_root = _validate_root(getattr(args, "memory_root", None) or "memory", "--memory-root", root)
    if case_root == memory_root:
        raise _fail(_("--case-root 与 --memory-root 不能相同"))
    registered = sorted({_validate_root(value, "--register-root", root, must_exist=True)
                         for value in getattr(args, "register_root", None) or []})
    taken = None
    if inputs.entry_launcher and _lstat(root / inputs.entry_launcher) is not None:
        taken = inputs.entry_launcher
        inputs = dc_replace(inputs, entry_launcher=None)
    report = scan(root, inputs, case_root=case_root, memory_root=memory_root)
    if report["blockers"]:
        raise _fail(report["blockers"][0]["message"], report["blockers"][0]["code"])

    pristine = init.files_for(inputs)
    files = dict(pristine)
    profile = json.loads(files[init.PROFILE_PATH])
    files.pop(Path("cases/.gitkeep"), None)
    files.pop(Path("memory/.gitkeep"), None)
    if inputs.work_mode == "case-workspace":
        profile["work"]["case"]["root"] = case_root
        files[Path(case_root) / ".gitkeep"] = ""
    profile["memory"]["root"] = memory_root
    files[Path(memory_root) / ".gitkeep"] = ""
    if registered:
        profile["work"]["assets"] = {"roots": registered}
    files[init.PROFILE_PATH] = init.json_text(profile)
    if not (root / "README.md").exists():
        files[Path("README.md")] = f"# {inputs.name}\n\n{init.readme_block(inputs.entry_launcher, inputs.language)}"
    readme = init.readme_plan(root, inputs.name, inputs.entry_launcher, inputs.language) if getattr(args, "readme_entry", False) else None

    modifications: list[dict] = []  # 改动已有文件：先备份再写入
    adopted: list[str] = []
    notes: list[str] = []
    region_records: dict[str, dict[str, str]] = {}
    pending_handoff: list[dict] = []
    extras: dict[Path, str] = {}
    agents_text = _read(root / AGENTS)
    agents_plan = None
    if policy != "keep" and (agents_text is None or agents_text == pristine[AGENTS]):
        notes.append(_("--agents-policy {0} 没有生效：AGENTS.md 不存在、不可读，或已与模板一致").format(policy))
    elif policy != "keep":
        agents_plan = _plan_agents(agents_text, policy, init_routes(args, agents_text), pristine[AGENTS],
                                   templates["region/agents-contract"], inputs.language)
        if agents_plan["replace"]:
            modifications.append({"path": AGENTS, "kind": "replace", "new_text": agents_plan["new_text"]})
        else:
            modifications.append({"path": AGENTS, "kind": "modify", "new_text": agents_plan["new_text"],
                                  "region": "agents-contract"})
            region_records.setdefault("AGENTS.md", {})["agents-contract"] = agents_plan["region"][1]
        if agents_plan["user_text"] is not None:
            extras[USER_FILE] = file_text(inputs.language, "user_header") + agents_plan["user_text"].rstrip("\n") + "\n"
        pending_handoff = agents_plan["pending_handoff"]
    elif agents_text is not None and agents_text != pristine[AGENTS]:
        adopted.append("AGENTS.md")
    claude_text = _read(root / CLAUDE)
    claude_state = next((f["state"] for f in report["findings"] if f["id"] == "adopt:contract:CLAUDE.md"), None)
    if claude_policy == "add-import":
        if claude_state != "shadows-agents":
            notes.append(_("--claude-policy add-import 没有生效：CLAUDE.md 不存在，或已能读到 AGENTS.md，或是符号链接"))
        else:
            new_text, _state = regions.upsert(claude_text, "claude-import", templates["region/claude-import"])
            modifications.append({"path": CLAUDE, "kind": "modify", "new_text": new_text, "region": "claude-import"})
            region_records.setdefault("CLAUDE.md", {})["claude-import"] = regions.digest(templates["region/claude-import"])
    elif claude_state == "shadows-agents":
        adopted.append("CLAUDE.md")
    if agents_plan and agents_plan["rules_block"]:
        rules_path = root / RULES_FILE
        if _lstat(rules_path) is None:
            files[RULES_FILE] = files[RULES_FILE].rstrip("\n") + "\n" + agents_plan["rules_block"]
            adopted.append(str(RULES_FILE))
        elif _read(rules_path) is None:
            raise _fail(_("AGENT.RULES.md 不可读，无法移入规则章节"))
        else:
            modifications.append({"path": RULES_FILE, "kind": "modify",
                                  "new_text": _read(rules_path).rstrip("\n") + "\n" + agents_plan["rules_block"]})
    for path, text in extras.items():
        if _lstat(root / path) is not None:
            raise _fail(_("{0} 已存在，不会被覆盖：先改名或删除后再选择拆分").format(path), "collision")
        files[path] = text
    if taken:
        notes.append(_("{0} 已存在：没有创建启动器。之后可用 --mode entry --entry-name <其他名称> 补上").format(taken))

    create, collisions, symlinked = init.plan_writes(root, files, skip_symlinked_parents=True)
    moving = {m["path"] for m in modifications}
    preserved = [p for p in collisions if p not in moving]
    for item in modifications:
        if init.plan_writes(root, {item["path"]: ""}, skip_symlinked_parents=False)[1] == []:
            raise _fail(_("{0} 不存在或不是普通文件").format(item['path']), "collision")
    needs_recovery = bool(modifications)
    result: dict[str, Any] = {
        "ok": True, "mode": "adopt", "applied": False, "scan": report,
        "policy": {"agents": policy, "claude": claude_policy, "case_root": case_root, "memory_root": memory_root,
                   "register_root": registered},
        "create": [str(p) for p in sorted(create, key=str)],
        "modify": [{"path": str(m["path"]), "action": m["kind"], "region": m.get("region")} for m in modifications],
        "preserve": [str(p) for p in sorted(preserved, key=str)],
        "skipped_symlinked_parents": [str(p) for p in symlinked],
        "needs_recovery_dir": needs_recovery, "pending_handoff": pending_handoff, "notes": notes,
        "readme": readme["action"] if readme else ("create" if Path("README.md") in create else "untouched"),
    }
    if not getattr(args, "apply", False):
        return 0, result
    recovery_dir = getattr(args, "recovery_dir", None)
    if needs_recovery and recovery_dir is None:
        raise _fail(_("改动已有文件前需要 --recovery-dir（项目外的备份目录）"))
    if recovery_dir is not None and needs_recovery:
        resolved = recovery_dir.expanduser().resolve()
        if resolved == root or root in resolved.parents:
            raise _fail("--recovery-dir must be outside the project root")

    originals: dict[Path, tuple[str, int]] = {}
    try:
        for item in modifications:
            path = root / item["path"]
            if path.is_symlink() or not path.is_file():
                raise _fail(_("{0} 不是普通文件，拒绝改动").format(item['path']))
            original = path.read_text(encoding="utf-8")
            originals[item["path"]] = (original, os.stat(path).st_mode & 0o7777)
        for item in modifications:
            scaffold._backup(root, recovery_dir, item["path"])
        for item in modifications:
            init._atomic_write(root / item["path"], item["new_text"], originals[item["path"]][1])
        init.apply_writes(root, files, create)
    except Exception:
        for relative, (original, mode) in originals.items():
            try:
                init._atomic_write(root / relative, original, mode)
            except OSError:
                pass
        raise
    launcher = inputs.entry_launcher
    if launcher and Path(launcher) in create:
        os.chmod(root / launcher, 0o755)
    if readme:
        init._atomic_write(root / readme["path"], readme["text"])
    pack = init.contract_pack()
    replaced = [m["path"] for m in modifications if m["kind"] == "replace"]
    baseline = init.content_baseline(pack, files, [*create, *replaced], launcher)
    for name in adopted:
        baseline.pop(name, None)
    if pack is not None:
        init.write_content_lock(root, pack, baseline, inputs=inputs, regions=region_records or None)
    decided = []
    after = scaffold.check(root, state_dir=getattr(args, "state_dir", None), reprompt=True)
    for finding in after["findings"]:
        if finding["kind"] == "content-differs":
            scaffold.decide(root, finding["id"], "keep", reason="adopt: kept the file that existed before adoption",
                            fingerprint=finding["fingerprint"], scope="project", apply=True,
                            state_dir=getattr(args, "state_dir", None))
            decided.append(finding["id"])
    final = scaffold.check(root, state_dir=getattr(args, "state_dir", None))
    result.update(applied=True, recovery=str(recovery_dir.expanduser().resolve()) if needs_recovery else None,
                  decisions_recorded=decided,
                  check_after={"ok": final["ok"], "findings": [f["id"] for f in final["findings"]]})
    return 0, result


def init_routes(args: Any, text: str) -> dict[int, str]:
    return parse_routes(list(getattr(args, "route", None) or []), sections(text))
