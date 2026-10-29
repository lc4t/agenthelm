"""SOP 文件的结构：小节名、字段名、前置条件的写法与写进 Case 的文字，按项目文件语言（Project Profile 的 ``language``）。

和 Case 一样（见 case_structure.py），SOP 写进项目的文件用什么语言是项目的属性，随项目同步，不随命令行界面语言
（AGENTHELM_LANG）变化：``language`` 为 ``en`` 的项目新建的 SOP 用英文小节名与字段名，缺省或 ``zh`` 的项目保持中文。
读取时两套词表都认，所以手写的、从别的项目拷来的、换过语言的 SOP 都能照常解析；追加内容时沿用文件自己的写法。

frontmatter 的键、枚举值（ask / stop / retry …）本来就是英文，与语言无关，不在这里。
"""
from __future__ import annotations

import re

import case_structure

LANGUAGES = case_structure.LANGUAGES
language_of = case_structure.language_of

SECTIONS = {
    "purpose": ("目的", "Purpose"),
    "requires": ("前置条件", "Prerequisites"),
    "steps": ("步骤", "Steps"),
    "done": ("完成标准", "Completion criteria"),
    "exceptions": ("例外与回退", "Exceptions and fallback"),
    "revisions": ("修订记录", "Revision log"),
}
STRICT_SECTIONS = ("requires", "steps", "done")

STEP_KEYS = {
    "tools": ("工具", "Tools"),
    "inputs": ("输入", "Inputs"),
    "intermediates": ("中间产物", "Intermediates"),
    "finals": ("最终产物", "Final outputs"),
    "checkpoint": ("检查点", "Checkpoint"),
    "on_failure": ("失败时", "On failure"),
    "confirm": ("需确认", "Needs confirmation"),
    "note": ("说明", "Note"),
}
REQ_KINDS = {
    "command": ("命令", "Command"),
    "module": ("模块", "Module"),
    "env": ("环境变量", "Environment variable"),
    "path": ("路径", "Path"),
    "skill": ("技能", "Skill"),
    "mcp": ("MCP", "MCP"),
    "manual": ("手动确认", "Manual check"),
}
REQ_KEYS = {
    "purpose": ("用途", "Purpose"),
    "install": ("安装", "Install"),
    "alternative": ("替代", "Alternative"),
    "if_missing": ("缺失时", "If missing"),
    "version": ("版本", "Version"),
    "when": ("仅当", "Only if"),
}
MISSING = {
    "ask": ("询问用户", "Ask the user"),
    "stop": ("停止", "Stop"),
    "skip-step": ("跳过该步", "Skip the step"),
    "alternative": ("使用替代", "Use the alternative"),
}
# 只在读取时认的别名（简写）。
KIND_ALIASES = {"手动": "manual"}
MISSING_ALIASES = {"询问": "ask", "跳过": "skip-step", "替代": "alternative", "ask": "ask", "skip": "skip-step"}

TRUE_WORDS = ("是", "y", "yes", "true", "需要", "required")
NONE_WORDS = ("无", "（无）", "(无)", "不需要", "none", "(none)", "n/a", "not needed")
LIST_NONE = ("无", "-", "—", "none")

PLACEHOLDERS = ("【待补充】", "[TO FILL IN]")
EMPTY_REVISIONS = ("暂无。", "None yet.")

# 执行 Case 里 SOP 自己加的小节（Case 的标准小节见 case_structure）。
CASE_SECTIONS = {
    "checklist": ("SOP 执行清单", "SOP checklist"),
    "intermediate": ("中间产物", "Intermediate artifacts"),
}

# 写进 Case 与 SOP 文件的文字。{…} 是 str.format 的字段。
TEXTS = {
    "revision_line": ("{date} v{version}：{note}", "{date} v{version}: {note}"),
    "note_activated": ("启用（内容与执行约定已与用户确认）", "Activated (content and run conventions confirmed with the user)"),
    "note_retired": ("退役", "Retired"),
    "note_restored": ("恢复", "Restored"),
    "from_case_description": ("{p}（从 {case} 提炼）", "{p} (distilled from {case})"),
    "from_case_step": ("{index}. {text}\n   - 说明：{p}整理这一步（来自 {case} 的工作记录，需改写成可重复执行的动作）",
                       "{index}. {text}\n   - Note: {p} tidy this step (from the work log of {case}; rewrite it as a repeatable action)"),
    "case_created_marker": ("Case 已创建", "Case created"),
    # 执行清单
    "cl_source": ("来源：`{id}@{version}`（{note}）", "Source: `{id}@{version}` ({note})"),
    "snapshot_note": ("快照 {path}", "Snapshot {path}"),
    "cl_conventions": ("执行约定（开始时从 SOP 与项目习惯固化，执行中不随 SOP 改动）：",
                       "Run conventions (frozen from the SOP and the project habits at the start; they do not follow later SOP edits):"),
    "cl_on_error": ("- 出错时：", "- On error: "),
    "cl_retries": ("（最多重试 {n} 次）", " (at most {n} retries)"),
    "cl_on_missing": ("- 缺少工具时：", "- On a missing tool: "),
    "cl_artifacts_label": ("- 中间产物：", "- Intermediate artifacts: "),
    "cl_artifacts": ("放在 {place}；完成后：{keep}", "kept in {place}; afterwards: {keep}"),
    "cl_data_dir": ("本 Case 的 data/ 目录", "this Case's data/ directory"),
    "cl_resume": ("- 失败后重新执行：", "- Resuming after a failure: "),
    "cl_costly": ("- 花钱或耗时的步骤：", "- Costly or slow steps: "),
    "cl_private": ("- 私有目录：本次会用到 {dirs}；读取前须经用户确认（用 sop confirm-read 记录），内容默认不进入 git；本 Case 已标为 private",
                   "- Private directories: this run will use {dirs}; the user must confirm before each read (record it with sop confirm-read), the content stays out of git by default; this Case is marked private"),
    "cl_params": ("参数：", "Parameters:"),
    "cl_steps": ("步骤：", "Steps:"),
    "cl_tools": ("（工具：{tools}）", " (tools: {tools})"),
    "cl_event_param": ("触发事件：{event}（{id}：{title}）", "Trigger event: {event} ({id}: {title})"),
    "event_note": ("由 {event} 触发（{id}）", "Triggered by {event} ({id})"),
    "source_event": ("；{note}", "; {note}"),
    "next_first": ("步骤 1：{title}", "Step 1: {title}"),
    "progress0": ("SOP 进度 0/{total}", "SOP progress 0/{total}"),
    "empty_intermediates": ("暂无。", "None yet."),
    "log_started": ("{stamp} 按 {id}@{version} 开始执行{event}；执行前检查：就绪 {ok}、缺失 {missing}、待核对 {unknown}",
                    "{stamp} Started running {id}@{version}{event}; preflight: {ok} ready, {missing} missing, {unknown} to verify"),
    "log_started_event": ("，{note}", ", {note}"),
    # 逐步记录
    "state_done": ("完成", "done"),
    "state_skip": ("跳过", "skipped"),
    "state_fail": ("失败", "failed"),
    "state_pending": ("重置为待办", "reset to to-do"),
    "log_step": ("{stamp} SOP 步骤 {n}「{title}」{label}{note}", "{stamp} SOP step {n} \"{title}\" {label}{note}"),
    "note_join": ("：{note}", ": {note}"),
    "artifact_line": ("步骤 {n}：`{location}`", "Step {n}: `{location}`"),
    "artifact_what": ("（{what}）", " ({what})"),
    "next_failed": ("处理步骤 {n} 的失败（约定：{habit}）", "Handle the failure of step {n} (convention: {habit})"),
    "next_step": ("步骤 {n}：{title}", "Step {n}: {title}"),
    "next_done": ("核对完成标准（sop.py status）后关闭 Case 或登记交付物",
                  "Check the completion criteria (sop.py status), then close the Case or register deliverables"),
    "state_progress": ("SOP 进度 {done}/{total}", "SOP progress {done}/{total}"),
    "state_failed_suffix": ("，步骤 {n} 失败", ", step {n} failed"),
    "log_confirm_read": ("{stamp} 用户确认读取私有路径：{paths}{note}", "{stamp} The user confirmed reading private paths: {paths}{note}"),
    "list_join": ("、", ", "),
    "confirm_note": ("；{note}", "; {note}"),
}

_TOOLS_SPLIT = re.compile(r"（工具|\s\(tools:")


def text(key: str, language: str, /, **fields: object) -> str:
    return TEXTS[key][LANGUAGES.index(language)].format(**fields)


def name(table: dict, key: str, language: str) -> str:
    return table[key][LANGUAGES.index(language)]


def _lookup(table: dict) -> dict[str, str]:
    return {variant.lower(): key for key, variants in table.items() for variant in variants}


_SECTION_LOOKUP = _lookup(SECTIONS)
_STEP_LOOKUP = _lookup(STEP_KEYS)
_KIND_LOOKUP = {**_lookup(REQ_KINDS), **KIND_ALIASES}
_REQ_LOOKUP = _lookup(REQ_KEYS)
_MISSING_LOOKUP = {**_lookup(MISSING), **{alias.lower(): key for alias, key in MISSING_ALIASES.items()}}


def section_key(heading: str) -> str | None:
    return _SECTION_LOOKUP.get(heading.strip().lower())


def step_key(label: str) -> str | None:
    return _STEP_LOOKUP.get(label.strip().lower())


def kind_key(label: str) -> str | None:
    return _KIND_LOOKUP.get(label.strip().lower())


def req_key(label: str) -> str | None:
    return _REQ_LOOKUP.get(label.strip().lower())


def missing_value(label: str) -> str | None:
    return _MISSING_LOOKUP.get(label.strip().lower())


def section_text(sections: dict[str, str], key: str) -> str:
    """The body of a logical section, whichever language the file spells its heading in."""
    for variant in SECTIONS[key]:
        if variant in sections:
            return sections[variant]
    for heading, body in sections.items():
        if section_key(heading) == key:
            return body
    return ""


def detect(body: str, default: str = "zh") -> str:
    """The language this SOP body is written in, judged by its section headings."""
    counts = {language: 0 for language in LANGUAGES}
    for line in body.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            for index, language in enumerate(LANGUAGES):
                counts[language] += any(variants[index] == match.group(1) for variants in SECTIONS.values())
    if counts["zh"] == counts["en"]:
        return default
    return "zh" if counts["zh"] > counts["en"] else "en"


def detect_case(body: str, default: str) -> str:
    """The language of a SOP run Case, judged by its checklist heading."""
    for index, language in enumerate(LANGUAGES):
        if f"## {CASE_SECTIONS['checklist'][index]}\n" in body or body.rstrip().endswith(f"## {CASE_SECTIONS['checklist'][index]}"):
            return language
    return default


def checklist_headings() -> tuple[str, ...]:
    return CASE_SECTIONS["checklist"]


def prefixes(key: str) -> tuple[str, ...]:
    return TEXTS[key]


def strip_tools(step_text: str) -> str:
    return _TOOLS_SPLIT.split(step_text)[0]


def is_placeholder_free(value: str) -> bool:
    return not any(marker in value for marker in PLACEHOLDERS)


def count_placeholders(value: str) -> int:
    return sum(value.count(marker) for marker in PLACEHOLDERS)
