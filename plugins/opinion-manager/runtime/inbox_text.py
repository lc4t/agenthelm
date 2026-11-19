"""收件箱面向用户和模型的文案（中文、英文）与语言选择。

语言按这个顺序决定：命令行 ``--lang`` → 环境变量 ``AGENTHELM_LANG`` → 系统 locale（``LC_ALL``、``LC_MESSAGES``、
``LANG``）→ 英文。``auto``、空值和 C/POSIX 视为未指定；中文 locale 用中文，其他语言的 locale 暂用英文。
候选内容本身（标题、规则、用户原话）是用户的话，从不翻译；文件里的字段名与历史记录使用与语言无关的编码。
"""

from __future__ import annotations

import re
import shlex
from typing import Any


LANGUAGES = ("zh", "en")


from i18n_opinion import DEFAULT_LANGUAGE, normalize, resolve_language  # noqa: E402,F401


MESSAGES: dict[str, dict[str, str]] = {
    "protocol": {
        "zh": """[Opinion 协议 · agenthelm]
1. 实施前读取项目 OPINION.md（如有）并按其要求工作；下列「试行」候选同样要遵循，交付时说明是否适用。
2. 用户在对话里表达长期偏好、纠正或规则（如“以后都…”“不要再…”“必须…”“记住…”）时，先静默暂存为候选，不打断当前工作：
   python3 {runtime} inbox add --project {project} --title "<标题>" --text "<完整规则>" --scope "<适用范围>" --trigger "<何时适用>" --purpose "<为什么>" --quote "<用户原话>"{session} --lang {lang}
   暂存时就填好 trigger 与 purpose（缺失仅警告，展示与裁决时按「不完整」处理；可选 --exclude 写不适用情形）；不得写「所有」之类占位词，确需全局须用户明确要求并带 --quote。一次性的任务要求、项目事实和命令不是候选。暂存不需要确认，也不会改变任何规则。
3. {confirm}
4. 未经用户确认，不得把候选写入 OPINION.md，也不得把一次出现当作确认。
5. inbox add 的结果带 notice（首次暂存时出现）时，用一句话如实转告用户。收件箱保存用户原话，可能含敏感信息；
   是否公开仓库、是否排除 .opinion/ 由用户决定，不要自行修改 .gitignore、删除或改写内容。
6. 向用户转告和确认时，使用用户正在使用的语言。""",
        "en": """[Opinion protocol · agenthelm]
1. Before working, read the project's OPINION.md (if any) and follow it. The "trial" candidates listed below apply too; say in your delivery whether they applied.
2. When the user states a lasting preference, a correction of your behavior or a rule during the conversation (e.g. "from now on...", "never again...", "must...", "remember..."), stash it as a candidate silently, without interrupting the current work:
   python3 {runtime} inbox add --project {project} --title "<title>" --text "<the full rule>" --scope "<where it applies>" --trigger "<when it applies>" --purpose "<why>" --quote "<the user's own words>"{session} --lang {lang}
   Fill trigger and purpose at stash time (missing ones only warn, and the candidate is shown as incomplete when deciding; optional --exclude states when it does not apply); no placeholders like "all"; global only when the user explicitly asked, with --quote. One-off task requirements, project facts and commands are not candidates. Stashing needs no confirmation and changes no rule.
3. {confirm}
4. Never write a candidate into OPINION.md without the user's confirmation, and never treat a single occurrence as confirmation.
5. When the result of inbox add carries a notice (shown on the first stash), pass it on to the user in one sentence, as written. The inbox stores the user's own words, which may contain sensitive information; whether to make the repository public or to exclude .opinion/ is the user's decision. Do not edit .gitignore, delete or rewrite anything yourself.
6. Relay and confirm in the language the user is using.""",
    },
    "protocol_ask": {
        "zh": "确认（ask）：本会话暂存了候选、任务完成或即将完成、要向用户确认前，先运行 python3 {runtime} inbox gate --project {project}{session}{gate_options}；allowed 为 false 则不问，继续工作。可以问时，用编号列表请用户裁决：正式采纳 / 项目内试行 / 暂缓 / 丢弃；展示时运行 python3 {runtime} inbox present <编号…> --project {project} 记录快照。用户之后更新需求本身不触发确认。\n   {handle}",
        "en": "Confirmation (ask): when you stashed candidates in this session, the task is done or nearly done and you are about to confirm with the user, first run python3 {runtime} inbox gate --project {project}{session}{gate_options}; if allowed is false, do not ask and keep working. When allowed, ask as a numbered list: adopt as formal / trial in this project / hold / discard; while showing it, run python3 {runtime} inbox present <ids...> --project {project} to record the snapshot. The user later updating a requirement is not itself a reason to confirm.\n   {handle}",
    },
    "protocol_digest": {
        "zh": "确认（digest）：对话里不单独询问。本会话暂存了候选时，只在任务回复末尾顺带一句说明有几条待处理，每会话至多一次；用户想处理时，用编号列表展示全文并运行 python3 {runtime} inbox present <编号…> --project {project}。其余由会话开始的汇总提示。\n   {handle}",
        "en": "Confirmation (digest): do not interrupt the conversation with a separate ask. When you stashed candidates in this session, add one closing sentence to a task reply saying how many are waiting, at most once per session; when the user wants to handle them, show the full numbered list and run python3 {runtime} inbox present <ids...> --project {project}. The rest is covered by the summary at session start.\n   {handle}",
    },
    "protocol_quiet": {
        "zh": "确认（quiet）：不要在对话里主动提起候选，只由会话开始的汇总提示。用户主动询问或要求处理时，用编号列表展示全文并运行 python3 {runtime} inbox present <编号…> --project {project}。\n   {handle}",
        "en": "Confirmation (quiet): do not bring candidates up in the conversation; the summary at session start covers them. When the user asks about them or wants to handle them, show the full numbered list and run python3 {runtime} inbox present <ids...> --project {project}.\n   {handle}",
    },
    "protocol_handle": {
        "zh": "表态后记录：python3 {runtime} inbox decide <编号…> --project {project} --to trial|held|rejected --confirmed\n   用户说“采纳”时不要 decide：运行 python3 {runtime} inbox adopt <编号…> --project {project} --lang {lang} 预览（零写入）。shown_matches 为真、无 blocked、无冲突时，视为已确认，直接加 --apply --confirm <confirmation_sha256> 写入并读回，告知结果与回退方式（上一个 Profile 版本仍在 .opinion/profiles/ 下）；否则展示预览差异后再问一次。「已批准」不等于「已生效」：只有 adopt --apply 成功才写入 OPINION.md。",
        "en": "After a decision, record it: python3 {runtime} inbox decide <ids...> --project {project} --to trial|held|rejected --confirmed\n   When the user says \"adopt\", do not decide: run python3 {runtime} inbox adopt <ids...> --project {project} --lang {lang} to preview (writes nothing). If shown_matches is true with no blocked and no conflicts, treat it as confirmed and add --apply --confirm <confirmation_sha256> to write and read back, then tell the user the result and how to go back (the previous Profile version is still under .opinion/profiles/); otherwise show the preview differences and ask once more. \"Approved\" is not \"in effect\": OPINION.md is written only when adopt --apply succeeds.",
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
    "label_trigger": {"zh": "触发条件", "en": "Trigger"},
    "label_purpose": {"zh": "目的", "en": "Purpose"},
    "label_exclude": {"zh": "不适用情形", "en": "Exclusion"},
    "label_relation": {"zh": "关系备注", "en": "Relation note"},
    "err_placeholder": {"zh": "{label}不能是占位词（{value}）：请写具体的条件；确需全局时用标准全局值并附 --quote",
                        "en": "{label} must not be a placeholder ({value}): write a concrete condition; if it really is global, use the standard global value and add --quote"},
    "err_global_quote": {"zh": "全局声明（触发条件为「全部场景，用户要求全局」）必须同时带非空 --quote（用户原话）",
                         "en": "A global declaration (trigger \"All scenarios; the user asked for this to apply globally\") needs a non-empty --quote with the user's own words"},
    "err_missing_fields": {"zh": "缺少必填项：{fields}", "en": "Missing required fields: {fields}"},
    "warn_missing_fields": {"zh": "已暂存，但缺少：{fields}。候选会按「不完整」展示；请补全（inbox add 时带 --trigger / --purpose）后再请用户裁决。",
                            "en": "Stashed, but missing: {fields}. The candidate will be shown as incomplete; fill them in (--trigger / --purpose on inbox add) before asking the user to decide."},
    "global_mark": {"zh": " [全局（请用户确认）]", "en": " [Global (ask the user to confirm)]"},
    "list_trigger": {"zh": "   - 条件：{value}{mark}", "en": "   - Trigger: {value}{mark}"},
    "list_purpose": {"zh": "   - 目的：{value}{mark}", "en": "   - Purpose: {value}{mark}"},
    "list_exclude": {"zh": "   - 不适用：{value}{mark}", "en": "   - Not for: {value}{mark}"},
    "list_relation": {"zh": "   - 关系备注：{value}{mark}", "en": "   - Relation note: {value}{mark}"},
    "list_incomplete": {"zh": "   - 不完整：缺少{fields}", "en": "   - Incomplete: missing {fields}"},
    "kind_same_text": {"zh": "正文相同", "en": "same text"},
    "kind_text_overlap": {"zh": "正文相近", "en": "similar text"},
    "kind_condition_overlap": {"zh": "条件相近", "en": "similar condition"},
    "relations_note": {"zh": "机械比对，仅供参考", "en": "Mechanical comparison, for reference only"},
    "list_relations": {"zh": "   - 可能相关（{kind}，机械比对，仅供参考）：{ref}：{evidence}",
                       "en": "   - Possibly related ({kind}; mechanical comparison, for reference only): {ref}: {evidence}"},
    "err_revise_one": {"zh": "revise 需要且只能有一个候选编号", "en": "revise needs exactly one candidate id"},
    "err_revise_nothing": {"zh": "revise 至少要改一项：--title / --text / --scope / --trigger / --purpose / --exclude / --relation / --quote",
                           "en": "revise must change something: --title / --text / --scope / --trigger / --purpose / --exclude / --relation / --quote"},
    "err_revise_confirm": {"zh": "候选状态为 {status}（或已展示给用户）：修订需要用户确认，先向用户展示修订后的全文，同意后加 --confirmed",
                           "en": "The candidate is {status} (or was already shown to the user): revising it needs the user's confirmation; show the user the revised text first and add --confirmed once they agree"},
    "err_revise_collision": {"zh": "修订后的标题与正文和候选 {id} 重复；请改为合并到该候选，或区分两者",
                             "en": "The revised title and text duplicate candidate {id}; merge into that candidate or tell them apart"},
    "warn_revise_formal": {"zh": "候选已是 formal：OPINION.md 不会随之改变；要改已写入的规则，走 adopt 的完整预览流程。",
                           "en": "The candidate is formal: OPINION.md does not change with it; to change a rule already written, go through the full adopt preview flow."},
    "check_next": {"zh": "逐条给出建议的条件与目的：候选用 revise_command 补全（尚未展示时无需确认，已展示需 --confirmed）；OPINION.md 里的行走现有的 adopt 完整预览流程，由用户一次批量确认后写入，本命令不写任何文件。「未结构化」的行只作报告，不要擅自改写。",
                   "en": "Propose a suggested trigger and purpose for each item: fix candidates with revise_command (no confirmation while not yet shown, --confirmed once shown); lines in OPINION.md go through the existing full adopt preview flow, confirmed by the user in one batch; this command writes no file. Lines marked unstructured are only reported; do not rewrite them on your own."},
    "check_clean": {"zh": "没有缺少条件或目的的候选，OPINION.md 里也没有需要补全的行。", "en": "No candidate lacks a trigger or purpose, and no line in OPINION.md needs completing."},
    "err_cooldown": {"zh": "--cooldown-hours 不能为负数", "en": "--cooldown-hours must not be negative"},
    "err_max_per_session": {"zh": "--max-per-session 必须是正整数", "en": "--max-per-session must be a positive integer"},
    "gate_ok": {"zh": "可以询问。", "en": "An ask is allowed."},
    "gate_cooldown": {"zh": "距上次询问不足冷却时间：本次不要询问。", "en": "Still within the cooldown since the last ask: do not ask this time."},
    "gate_session_limit": {"zh": "本会话已询问到上限：本次不要询问。", "en": "This session has reached its ask limit: do not ask this time."},
    "brief_trigger": {"zh": "条件：{value}", "en": "when: {value}"},
    "brief_purpose": {"zh": "目的：{value}", "en": "purpose: {value}"},
    "brief_exclude": {"zh": "不适用：{value}", "en": "not for: {value}"},
    "brief_join": {"zh": "；", "en": "; "},
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
    "formal_reminder": {"zh": "「已批准」不等于「已生效」：候选只是记为 formal，尚未写入 OPINION.md，后续会话不会加载它；下一步是 inbox adopt。",
                        "en": "\"Approved\" is not \"in effect\": the candidate is only recorded as formal and is not in OPINION.md yet, so later sessions do not load it. The next step is inbox adopt."},
    "formal_next": {"zh": "尚未写入 OPINION.md；下一步预览并写入：{command}",
                    "en": "Not written to OPINION.md yet; next, preview and write it: {command}"},
    "adopt_next": {"zh": "向用户展示 content 并取得确认后运行：{command}",
                   "en": "Show the user the content and, once they confirm, run: {command}"},
    "adopt_done": {"zh": "已写入 OPINION.md 并读回核对；{count} 条候选已生效。", "en": "Written to OPINION.md and read back; {count} candidate(s) now in effect."},
    "err_adopt_formal": {"zh": "候选已是 formal：{ids}（已批准或已写入）；若尚未写入 OPINION.md，先用 decide --to pending --confirmed 退回再采纳",
                         "en": "Already formal: {ids} (approved or already written). If it is not in OPINION.md yet, move it back with decide --to pending --confirmed, then adopt"},
    "err_adopt_confirm": {"zh": "写入需要先预览并让用户确认：去掉 --apply 预览，用户确认后加 --apply --confirm <confirmation_sha256>",
                          "en": "Writing needs a preview the user has confirmed: preview without --apply, then add --apply --confirm <confirmation_sha256> once they agree"},
    "err_adopt_stale": {"zh": "预览之后内容已变化（候选、Profile 或 OPINION.md），未写入；请重新预览。当前指纹：{fingerprint}",
                        "en": "The content changed since the preview (candidates, Profile or OPINION.md); nothing was written. Preview again. Current fingerprint: {fingerprint}"},
    "err_adopt_unresolved": {"zh": "Profile 升级存在未确认的冲突：{ids}", "en": "The Profile upgrade has unresolved conflicts: {ids}"},
    "err_adopt_readback": {"zh": "写入后读回的 OPINION.md 与预览不一致，已回滚", "en": "The OPINION.md read back differs from the preview; rolled back"},
    "list_empty": {"zh": "没有符合条件的候选。", "en": "No matching candidates."},
    "list_rule": {"zh": "   - 规则：{text}", "en": "   - Rule: {text}"},
    "list_scope": {"zh": "   - 适用：{scopes}", "en": "   - Applies to: {scopes}"},
    "list_quote": {"zh": "   - 用户原话：{quote}", "en": "   - User's words: {quote}"},
    "err_route_need_skill": {"zh": "--route-when / --route-final / --route-on 需要 --route-skill（revise 时候选已有路由则可省略）",
                             "en": "--route-when / --route-final / --route-on need --route-skill (on revise it may be omitted when the candidate already has a route)"},
    "err_route_skill": {"zh": "--route-skill 必须是 skill 名称：小写字母、数字、连字符，不超过 64 字符（不接受 plugin:skill）：{value}",
                        "en": "--route-skill must be a skill name: lowercase letters, digits and hyphens, at most 64 characters (plugin:skill is not accepted): {value}"},
    "err_route_when": {"zh": "--route-when 必须是 before、after、instead 之一：{value}", "en": "--route-when must be one of before, after, instead: {value}"},
    "err_route_final": {"zh": "--route-final 只能与 after 合用（不给 --route-when 时默认 after）", "en": "--route-final only goes with after (without --route-when it defaults to after)"},
    "err_route_on": {"zh": "--route-on 必须是短标签：小写字母、数字、连字符，1 到 32 字符：{value}", "en": "--route-on must be a short tag: lowercase letters, digits and hyphens, 1 to 32 characters: {value}"},
    "warn_route_long": {"zh": "路由规则行 {bytes} 字节，超过 {limit} 字节的预算（只警告）：请精简标题、正文或条件。",
                        "en": "The route rule line is {bytes} bytes, over the {limit}-byte budget (warning only): shorten the title, text or trigger."},
    "route_text": {"zh": "skill {skill}（{extras}）", "en": "skill {skill} ({extras})"},
    "route_when_before": {"zh": "开始前", "en": "before"},
    "route_when_after": {"zh": "完成后", "en": "after"},
    "route_when_instead": {"zh": "取代默认做法", "en": "instead"},
    "route_final": {"zh": "收尾", "en": "final"},
    "route_on": {"zh": "标签 {on}", "en": "tag {on}"},
    "brief_join_short": {"zh": "，", "en": ", "},
    "list_route": {"zh": "   - 路由：{value}", "en": "   - Route: {value}"},
    "brief_route": {"zh": "路由：{value}", "en": "route: {value}"},
    "routes_check_note": {"zh": "机械检查，仅供参考", "en": "Mechanical check, for reference only"},
    "routes_empty": {"zh": "没有路由规则。", "en": "No route rules."},
    "routes_no_tag": {"zh": "无标签", "en": "no tag"},
    "routes_line": {"zh": "- {ref} {title}：{route}，{bytes} 字节", "en": "- {ref} {title}: {route}, {bytes} bytes"},
    "routes_line_unstructured": {"zh": "- {ref} 未结构化（本工具认不出的格式，只作报告）：{text}", "en": "- {ref} unstructured (a format this tool does not recognize; reported only): {text}"},
    "routes_check_line": {"zh": "- 提示（{note}）：{detail}", "en": "- Notice ({note}): {detail}"},
    "routes_unchecked": {"zh": "本命令不检查：{items}（skill 是否存在需 agenthelm inventory，下一版提供）", "en": "Not checked by this command: {items} (whether a skill exists needs agenthelm inventory, planned for the next release)"},
    "routes_check_same_tag_different_skill": {"zh": "标签 {on} 下，同一时机（{when}）路由到不同 skill：{skills}", "en": "Under tag {on}, the same timing ({when}) routes to different skills: {skills}"},
    "routes_check_duplicate_route": {"zh": "重复路由：skill {skill}、标签 {on}、时机 {when} 共 {count} 条", "en": "Duplicate route: skill {skill}, tag {on}, timing {when}, {count} rules"},
    "routes_check_multiple_final": {"zh": "项目里有 {count} 条收尾（--route-final）路由，通常只应有一条", "en": "The project has {count} final (--route-final) routes; normally there should be only one"},
    "routes_check_too_many_routes": {"zh": "共 {count} 条路由，超过 {limit} 条的预算", "en": "{count} routes in total, over the budget of {limit}"},
    "routes_check_line_too_long": {"zh": "单条 {bytes} 字节，超过 {limit} 字节的预算", "en": "A single rule is {bytes} bytes, over the budget of {limit}"},
    "err_route_clear_exclusive": {"zh": "--route-clear 不能与其他 --route-* 参数同时使用", "en": "--route-clear cannot be combined with the other --route-* options"},
    "err_routes_action": {"zh": "routes 只支持 list", "en": "routes only supports list"},
    "brief_trial_header": {
        "zh": "\n[试行候选 · 用户已同意在本项目试用，尚未正式；按其工作并在交付时说明是否适用]",
        "en": "\n[Trial candidates · the user agreed to try these in this project; not formal yet. Follow them and say in your delivery whether they applied]"},
    "brief_trial_entry": {"zh": "- {id} {title}：{text}（适用：{scopes}）", "en": "- {id} {title}: {text} (applies to: {scopes})"},
    "brief_trial_more": {"zh": "- …另有 {count} 条试行候选未展示：运行 inbox list --status trial 查看",
                         "en": "- ...{count} more trial candidates not shown: run inbox list --status trial"},
    "brief_pending_quiet": {"zh": "\n[待确认候选 {count} 条，尚未生效；不要在对话里单独询问，由会话开始的汇总提示]",
                            "en": "\n[{count} candidates await confirmation and are not in effect yet; do not ask about them separately in the conversation]"},
    "brief_pending": {"zh": "\n[待确认候选 {count} 条，尚未生效；任务结束前可向用户集中确认]",
                      "en": "\n[{count} candidates await confirmation and are not in effect yet; ask the user to decide them together before the task ends]"},
}

PLACEHOLDER = re.compile(r"\{(\w+)\}")


# protocol 里的 {runtime} {project} 会被模型原样当作命令行执行，代入的路径必须经 shell 引用（POSIX）。
COMMAND_MESSAGES = {"protocol", "protocol_ask", "protocol_digest", "protocol_quiet", "protocol_handle"}
PATH_FIELDS = ("runtime", "project")


def session_flag(session: str) -> str:
    """命令行里的 `` --session <id>``（会话 ID 同样经 shlex.quote）；没有会话 ID 时为空。"""
    return f" --session {shlex.quote(str(session))}" if session else ""


def text(message_id: str, language: str, /, **fields: Any) -> str:
    if message_id in COMMAND_MESSAGES:
        fields = {key: shlex.quote(str(value)) if key in PATH_FIELDS else value for key, value in fields.items()}
    return MESSAGES[message_id][language].format(**fields)


def protocol(language: str, *, runtime: Any, project: Any, lang: str, session: str = "", mode: str = "ask",
             cooldown: float = 24, limit: int = 1) -> str:
    """注入协议：公共部分加按确认模式（ask / digest / quiet）生成的第 3 条。session 是会话 ID（空串表示未知）。"""
    flag = session_flag(session)
    handle = text("protocol_handle", language, runtime=runtime, project=project, lang=lang)
    options = (f" --cooldown-hours {cooldown:g}" if cooldown != 24 else "") + (f" --max-per-session {limit}" if limit != 1 else "")
    confirm = text(f"protocol_{mode}", language, runtime=runtime, project=project, lang=lang, session=flag,
                   gate_options=options, handle=handle)
    return text("protocol", language, runtime=runtime, project=project, lang=lang, session=flag, confirm=confirm)
