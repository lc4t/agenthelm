"""收件箱面向用户和模型的文案（中文、英文）与语言选择。

语言按这个顺序决定：命令行 ``--lang`` → 环境变量 ``AGENTHELM_LANG`` → 系统 locale（``LC_ALL``、``LC_MESSAGES``、
``LANG``）→ 中文。``auto``、空值和 C/POSIX 视为未指定；中文 locale 用中文，其他语言的 locale 暂用英文。
候选内容本身（标题、规则、用户原话）是用户的话，从不翻译；文件里的字段名与历史记录使用与语言无关的编码。
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping


LANGUAGES = ("zh", "en")
DEFAULT_LANGUAGE = "zh"


def normalize(value: str | None) -> str | None:
    text = (value or "").strip().lower().replace("_", "-")
    if not text or text in ("auto", "c", "posix") or text.startswith(("c.", "posix.")):
        return None
    return "zh" if text.startswith("zh") else "en"


def resolve_language(requested: str | None = None, environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    for candidate in (requested, environ.get("AGENTHELM_LANG"), environ.get("LC_ALL"), environ.get("LC_MESSAGES"), environ.get("LANG")):
        language = normalize(candidate)
        if language:
            return language
    return DEFAULT_LANGUAGE


MESSAGES: dict[str, dict[str, str]] = {
    "protocol": {
        "zh": """[Opinion 协议 · agenthelm]
1. 实施前读取项目 OPINION.md（如有）并按其要求工作；下列「试行」候选同样要遵循，交付时说明是否适用。
2. 用户在对话里表达长期偏好、纠正或规则（如“以后都…”“不要再…”“必须…”“记住…”）时，先静默暂存为候选，不打断当前工作：
   python3 {runtime} inbox add --project {project} --title "<标题>" --text "<完整规则>" --scope "<适用范围>" --quote "<用户原话>"{session} --lang {lang}
   一次性的任务要求、项目事实和命令不是候选。暂存不需要确认，也不会改变任何规则。
3. 任务完成或即将完成时，若本次暂存了候选，用编号列表一次性请用户裁决：正式采纳 / 项目内试行 / 暂缓 / 丢弃。
   用户明确表态后记录：python3 {runtime} inbox decide <编号…> --project {project} --to trial|formal|held|rejected --confirmed --lang {lang}
   采纳为正式只改状态；写入 OPINION.md 须走 opinion-manager 的完整预览与确认。
4. 未经用户确认，不得把候选写入 OPINION.md，也不得把一次出现当作确认。
5. inbox add 的结果带 notice（首次暂存时出现）时，用一句话如实转告用户。收件箱保存用户原话，可能含敏感信息；
   是否公开仓库、是否排除 .opinion/ 由用户决定，不要自行修改 .gitignore、删除或改写内容。
6. 向用户转告和确认时，使用用户正在使用的语言。""",
        "en": """[Opinion protocol · agenthelm]
1. Before working, read the project's OPINION.md (if any) and follow it. The "trial" candidates listed below apply too; say in your delivery whether they applied.
2. When the user states a lasting preference, a correction of your behavior or a rule during the conversation (e.g. "from now on...", "never again...", "must...", "remember..."), stash it as a candidate silently, without interrupting the current work:
   python3 {runtime} inbox add --project {project} --title "<title>" --text "<the full rule>" --scope "<where it applies>" --quote "<the user's own words>"{session} --lang {lang}
   One-off task requirements, project facts and commands are not candidates. Stashing needs no confirmation and changes no rule.
3. When the task is done or nearly done and you stashed candidates in this session, ask the user once, as a numbered list: adopt as formal / trial in this project / hold / discard.
   After the user states a decision explicitly, record it: python3 {runtime} inbox decide <ids...> --project {project} --to trial|formal|held|rejected --confirmed --lang {lang}
   Adopting as formal only records the decision; writing it into OPINION.md goes through opinion-manager's full preview and confirmation.
4. Never write a candidate into OPINION.md without the user's confirmation, and never treat a single occurrence as confirmation.
5. When the result of inbox add carries a notice (shown on the first stash), pass it on to the user in one sentence, as written. The inbox stores the user's own words, which may contain sensitive information; whether to make the repository public or to exclude .opinion/ is the user's decision. Do not edit .gitignore, delete or rewrite anything yourself.
6. Relay and confirm in the language the user is using.""",
    },
    "exposure_notice": {
        "zh": "收件箱会把你的原话保存在 .opinion/inbox.json，其中可能含敏感信息。启用本功能的项目建议保持私有；"
              "若要公开仓库或让他人看到，是否排除 .opinion/ 及其中内容由你决定。本工具不会替你决定，也不会修改 .gitignore。",
        "en": "The inbox stores your own words in .opinion/inbox.json, which may contain sensitive information. "
              "Projects that use this feature should stay private; if you want to make the repository public or visible to others, "
              "whether to exclude .opinion/ and its contents is your decision. This tool will not decide for you and never edits .gitignore.",
    },
    "gap": {"zh": "", "en": " "},
    "state_ignored": {"zh": "当前已被 Git 忽略。", "en": "It is currently ignored by Git."},
    "state_tracked": {"zh": "当前已被 Git 跟踪，提交和推送会包含它。", "en": "It is currently tracked by Git, so commits and pushes include it."},
    "state_not-ignored": {"zh": "当前未被 Git 忽略，提交时会被包含。", "en": "It is currently not ignored by Git, so commits will include it."},
    "state_no-git": {"zh": "当前目录不是 Git 仓库；放在同步盘或共享目录里同样会被他人看到。",
                     "en": "This directory is not a Git repository; in a synced or shared folder it is just as visible to others."},
    "state_unknown": {"zh": "无法判断 Git 状态。", "en": "The Git state could not be determined."},
    "sep": {"zh": "、", "en": ", "},
    "label_title": {"zh": "标题", "en": "Title"},
    "label_text": {"zh": "规则正文", "en": "Rule text"},
    "label_quote": {"zh": "用户原话", "en": "User's words"},
    "label_scope": {"zh": "适用范围", "en": "Scope"},
    "err_symlink": {"zh": "拒绝使用符号链接：{path}", "en": "Refusing to use a symbolic link: {path}"},
    "err_format": {"zh": "收件箱格式不正确：{path}", "en": "The inbox file is malformed: {path}"},
    "err_empty": {"zh": "{label}不能为空", "en": "{label} must not be empty"},
    "err_too_long": {"zh": "{label}超过 {limit} 个字符；请拆成多条候选或精简",
                     "en": "{label} is longer than {limit} characters; split it into several candidates or shorten it"},
    "err_scope": {"zh": "候选需要至少一个 --scope（适用范围）", "en": "A candidate needs at least one --scope"},
    "err_confirm": {"zh": "状态变化需要用户明确表态：先向用户展示候选全文，用户确认后加 --confirmed",
                    "en": "A status change needs the user's explicit decision: show the user the full candidate first, then add --confirmed once they agree"},
    "err_to": {"zh": "--to 必须是 {choices} 之一", "en": "--to must be one of: {choices}"},
    "err_missing": {"zh": "没有这个候选：{id}", "en": "No such candidate: {id}"},
    "err_days": {"zh": "--days 必须是正整数", "en": "--days must be a positive integer"},
    "err_status": {"zh": "未知状态：{status}", "en": "Unknown status: {status}"},
    "err_project": {"zh": "项目目录不存在：{path}", "en": "Project directory does not exist: {path}"},
    "err_ids": {"zh": "需要至少一个候选编号", "en": "At least one candidate id is required"},
    "err_need_to": {"zh": "decide 需要 --to", "en": "decide needs --to"},
    "err_need_days": {"zh": "expire 需要 --days", "en": "expire needs --days"},
    "formal_reminder": {"zh": "正式采纳只记录状态；写入 OPINION.md 须经 opinion-manager 的完整预览与确认。",
                        "en": "Adopting as formal only records the status; writing into OPINION.md needs opinion-manager's full preview and confirmation."},
    "list_empty": {"zh": "没有符合条件的候选。", "en": "No matching candidates."},
    "list_rule": {"zh": "   - 规则：{text}", "en": "   - Rule: {text}"},
    "list_scope": {"zh": "   - 适用：{scopes}", "en": "   - Applies to: {scopes}"},
    "list_quote": {"zh": "   - 用户原话：{quote}", "en": "   - User's words: {quote}"},
    "brief_trial_header": {
        "zh": "\n[试行候选 · 用户已同意在本项目试用，尚未正式；按其工作并在交付时说明是否适用]",
        "en": "\n[Trial candidates · the user agreed to try these in this project; not formal yet. Follow them and say in your delivery whether they applied]"},
    "brief_trial_entry": {"zh": "- {id} {title}：{text}（适用：{scopes}）", "en": "- {id} {title}: {text} (applies to: {scopes})"},
    "brief_trial_more": {"zh": "- …另有 {count} 条试行候选未展示：运行 inbox list --status trial 查看",
                         "en": "- ...{count} more trial candidates not shown: run inbox list --status trial"},
    "brief_pending": {"zh": "\n[待确认候选 {count} 条，尚未生效；任务结束前可向用户集中确认]",
                      "en": "\n[{count} candidates await confirmation and are not in effect yet; ask the user to decide them together before the task ends]"},
}

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def text(message_id: str, language: str, /, **fields: Any) -> str:
    return MESSAGES[message_id][language].format(**fields)
