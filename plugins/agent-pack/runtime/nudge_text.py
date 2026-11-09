"""nudge 面向用户和模型的文案（中文、英文）与语言选择。

语言按这个顺序决定：配置 ``nudge.language``（``auto`` / ``zh`` / ``en``，用户配置优先于项目 Profile）→ 环境变量
``AGENTHELM_LANG`` → 系统 locale（``LC_ALL``、``LC_MESSAGES``、``LANG``）→ 英文。``auto``、空值和 C/POSIX 视为
未指定；中文 locale 用中文，其他语言的 locale 暂用英文。解析出的语言同时传给 opinion-manager 的 ``inbox``，所以同一次提示
里用户和模型看到的是同一种语言。候选内容是用户自己的话，不翻译。关键词列表是数据，不属于文案。
"""

from __future__ import annotations

import re
import shlex
from typing import Any

LANGUAGES = ("zh", "en")
PLACEHOLDER = re.compile(r"\{(\w+)\}")


from i18n_agent_pack import DEFAULT_LANGUAGE, normalize, resolve_language  # noqa: E402,F401


MESSAGES: dict[str, dict[str, str]] = {
    "label_profile": {"zh": "项目 Profile opinion.nudge", "en": "Project Profile opinion.nudge"},
    "label_user": {"zh": "用户配置 nudge", "en": "User config nudge"},
    "warn_object": {"zh": "{label} 必须是对象，已忽略", "en": "{label} must be an object; ignored"},
    "warn_unknown": {"zh": "{label}.{key} 不是已知设置，已忽略", "en": "{label}.{key} is not a known setting; ignored"},
    "warn_bool": {"zh": "{label}.{key} 必须是布尔值，已忽略", "en": "{label}.{key} must be a boolean; ignored"},
    "warn_stop": {"zh": "{label}.stop_prompt 须为 {modes} 或 {{客户端: 模式}}，已忽略",
                  "en": "{label}.stop_prompt must be {modes} or {{client: mode}}; ignored"},
    "warn_words": {"zh": "{label}.{key} 须为非空的字符串数组，已忽略", "en": "{label}.{key} must be a non-empty array of strings; ignored"},
    "warn_int": {"zh": "{label}.{key} 须为正整数，已忽略", "en": "{label}.{key} must be a positive integer; ignored"},
    "warn_language": {"zh": "{label}.language 须为 auto、zh 或 en，已忽略", "en": "{label}.language must be auto, zh or en; ignored"},
    "reminder": {
        "zh": "[Opinion] 有 {count} 条候选观点待确认{age}{trial}。在对话里让 Agent 逐条确认，或自行运行 opinion-manager 的 inbox list。",
        "en": "[Opinion] {count} candidate opinions await confirmation{age}{trial}. Ask the Agent to go through them in the conversation, or run opinion-manager's inbox list yourself.",
    },
    "reminder_age": {"zh": "（最久 {days} 天未处理）", "en": " (the oldest untouched for {days} days)"},
    "reminder_trial": {"zh": "，另有 {trial} 条在项目内试行", "en": ", and {trial} more are on trial in this project"},
    "reminder_hint": {
        "zh": "\n用户已看到提示：「{text}」。不要打断当前任务；用户提起或任务间隙再协助逐条确认（inbox list --status pending --output markdown）。",
        "en": "\nThe user has seen this notice: \"{text}\". Do not interrupt the current task; help go through the candidates when the user brings it up or between tasks (inbox list --status pending --output markdown).",
    },
    "relay": {
        "zh": "\n[给用户的提示，他可能没有看到；请在首次回复里用一句话如实转告，是否处理由用户决定，不要自行改 .gitignore]\n{notice}",
        "en": "\n[A notice for the user, who may not have seen it. Pass it on in one sentence in your first reply, as written; what to do about it is the user's decision, and do not edit .gitignore yourself]\n{notice}",
    },
    "add_command": {
        "zh": 'python3 {manager} inbox add --project {root} --title "<标题>" --text "<完整规则>" --scope "<适用范围>" --quote "<用户原话>"{session} --lang {lang}',
        "en": 'python3 {manager} inbox add --project {root} --title "<title>" --text "<the full rule>" --scope "<where it applies>" --quote "<the user\'s own words>"{session} --lang {lang}',
    },
    "add_command_fallback": {"zh": "opinion-manager 的 inbox add", "en": "opinion-manager's inbox add"},
    "sep": {"zh": "、", "en": ", "},
    "prompt_hint": {
        "zh": "[Opinion] 用户这条消息里有可能是长期偏好或对你行为的纠正的表述（命中：{hits}）。"
              "若确属长期偏好、规则或纠正，先静默暂存为候选，不打断当前工作：{command}。"
              "仅限本次任务的要求、项目事实和命令不是候选，忽略即可。任务完成前再请用户集中确认。",
        "en": "[Opinion] This user message may state a lasting preference or a correction of your behavior (matched: {hits}). "
              "If it really is a lasting preference, rule or correction, stash it as a candidate silently without interrupting the current work: {command}. "
              "Requirements for this task only, project facts and commands are not candidates; ignore them. Ask the user to confirm candidates together before the task ends.",
    },
    "stop_request": {
        "zh": "[Opinion] 本次会话暂存了 {count} 条候选观点，尚未向用户确认。请在不重复已完成工作的前提下，"
              "用下面的编号列表一次性请用户裁决：正式采纳 / 项目内试行 / 暂缓 / 丢弃（也可以逐条说明）。"
              "用户明确表态后：试行 / 暂缓 / 丢弃运行 python3 {manager} inbox decide <编号…> --project {root} --to trial|held|rejected --confirmed --lang {lang}；"
              "用户说“采纳”则不要 decide，直接运行 python3 {manager} inbox adopt <编号…> --project {root} --lang {lang} 预览（零写入），展示结果后经用户确认再加 --apply --confirm <confirmation_sha256> 一次写入并读回。"
              "「已批准」不等于「已生效」：只有 adopt --apply 成功才写入 OPINION.md；未经确认不要写入。\n\n{listing}",
        "en": "[Opinion] You stashed {count} candidate opinions in this session and have not confirmed them with the user yet. Without repeating finished work, "
              "ask the user once, using the numbered list below: adopt as formal / trial in this project / hold / discard (they may also answer item by item). "
              "After the user states a decision explicitly: for trial / hold / discard run python3 {manager} inbox decide <ids...> --project {root} --to trial|held|rejected --confirmed --lang {lang}; "
              "when the user says \"adopt\", do not decide: run python3 {manager} inbox adopt <ids...> --project {root} --lang {lang} to preview (writes nothing), show the result, and once the user confirms add --apply --confirm <confirmation_sha256> to write and read back in one step. "
              "\"Approved\" is not \"in effect\": OPINION.md is written only when adopt --apply succeeds. Do not write anything before the user confirms.\n\n{listing}",
    },
    "note_cursor": {
        "zh": "Cursor 的 sessionStart 注入是否到达模型取决于 Cursor 版本（社区有未到达的反馈）；"
              "协议同时写在 opinion-manager 的 Skill 中，未到达时仍可按需触发。stop 提示默认关闭，"
              "需要时把 nudge.stop_prompt.cursor 设为 on-completion。",
        "en": "Whether Cursor's sessionStart injection reaches the model depends on the Cursor version (the community reports cases where it does not). "
              "The protocol is also written in opinion-manager's Skill, so it can still be triggered on demand. The stop prompt is off by default; "
              "set nudge.stop_prompt.cursor to on-completion if you want it.",
    },
    "note_no_project": {
        "zh": "当前目录不在启用了 opinion-manager 的项目里：hook 不会产生任何输出。",
        "en": "This directory is not in a project that uses opinion-manager: the hooks produce no output here.",
    },
    "err_hooks_json": {"zh": "{target} 不是合法 JSON，已停止：{error}", "en": "{target} is not valid JSON; stopped: {error}"},
    "err_hooks_root": {"zh": "{target} 的根节点必须是对象", "en": "The root of {target} must be an object"},
    "err_backup": {"zh": "无法备份 {target} 到 {backup}（{error}）；没有改动任何文件。",
                   "en": "Could not back up {target} to {backup} ({error}); nothing was changed."},
    "err_unstable_runtime": {
        "zh": "当前运行的 agenthelm 位于临时缓存 {runtime}，缓存清理后 hook 会失效，且没有找到已安装的稳定入口（已查找：{searched}）；"
              "没有写入任何文件。先安装稳定入口：agenthelm install --client cursor --apply，或 agenthelm runtime install --apply，再重新运行 nudge install-hooks。",
        "en": "The running agenthelm is in a temporary cache ({runtime}), so the hooks would break once the cache is cleaned, and no installed stable entry was found (looked in: {searched}); "
              "nothing was written. Install a stable entry first: agenthelm install --client cursor --apply, or agenthelm runtime install --apply, then run nudge install-hooks again.",
    },
    "note_runtime_replaced": {
        "zh": "注意：当前运行的 agenthelm 在临时缓存 {ephemeral}，hook 命令改用稳定入口 {stable}。",
        "en": "Note: the running agenthelm is in a temporary cache ({ephemeral}); the hook commands use the stable entry {stable} instead.",
    },
    "install_blocked": {"zh": "预览被阻塞，不会写入", "en": "Preview blocked; nothing would be written"},
    "status_project": {"zh": "项目：{project}", "en": "Project: {project}"},
    "status_no_project": {"zh": "（不在启用 opinion-manager 的项目里）", "en": "(not in a project that uses opinion-manager)"},
    "status_switch": {"zh": "开关：{state}", "en": "Switch: {state}"},
    "status_env_off": {"zh": "已被环境变量 AGENTHELM_NO_NUDGE 关闭", "en": "turned off by the environment variable AGENTHELM_NO_NUDGE"},
    "status_on": {"zh": "开启", "en": "on"},
    "status_off": {"zh": "关闭", "en": "off"},
    "status_thresholds": {
        "zh": "阈值：待确认 ≥{pending} 条或最久 ≥{stale} 天；每会话确认 ≤{prompts} 次；试行 {expire} 天未转正回退；任务结束提示 {stop}；语言 {language}",
        "en": "Thresholds: pending ≥ {pending} or oldest age (days) ≥ {stale}; confirmation requests per session ≤ {prompts}; trial expiry (days): {expire}; end-of-task prompt {stop}; language {language}",
    },
    "status_current": {
        "zh": "当前：待确认 {pending} 条，试行 {trial} 条，最久 {days} 天", "en": "Now: {pending} pending, {trial} on trial, the oldest {days} days old"},
    "status_quiet": {"zh": "按当前阈值，开新会话不会提示。", "en": "With the current thresholds, a new session shows no reminder."},
    "status_exposure": {"zh": "收件箱的可见范围：{message}", "en": "Who can see the inbox: {message}"},
    "status_hook_error": {
        "zh": "最近一次钩子错误（共记录 {count} 条，钩子已静默跳过，不影响会话）：{line}\n运行 agenthelm nudge status --clear-errors 清除。",
        "en": "Last hook error ({count} recorded; the hook skipped silently and the session was not affected): {line}\nRun agenthelm nudge status --clear-errors to clear it."},
    "status_errors_cleared": {"zh": "已清除钩子错误记录。", "en": "Hook error records cleared."},
    "status_warning": {"zh": "配置警告：{item}", "en": "Config warning: {item}"},
    "install_written": {"zh": "已写入", "en": "Written"},
    "install_unchanged": {"zh": "没有变化", "en": "No change"},
    "install_preview": {"zh": "将写入（加 --apply 执行）", "en": "Would write (add --apply to do it)"},
    "install_line": {"zh": "{verb}：{target}", "en": "{verb}: {target}"},
}


# 这些消息里的 {manager} {root} 会被模型原样当作命令行执行，代入的路径必须经 shell 引用（POSIX）；
# 其他消息（如 status_project）是给人看的，不引用。
COMMAND_MESSAGES = {"add_command", "stop_request"}
PATH_FIELDS = ("manager", "root")


def session_flag(session: str) -> str:
    """命令行里的 `` --session <id>``（会话 ID 同样经 shlex.quote）；没有会话 ID 时为空。"""
    return f" --session {shlex.quote(str(session))}" if session else ""


def text(message_id: str, language: str, /, **fields: Any) -> str:
    if message_id in COMMAND_MESSAGES:
        fields = {key: shlex.quote(str(value)) if key in PATH_FIELDS else value for key, value in fields.items()}
    return MESSAGES[message_id][language].format(**fields)
