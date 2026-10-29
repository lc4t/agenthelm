"""agenthelm nudge：在 Claude Code、Codex、Cursor 的 hook 里自动采集观点候选、注入试行规则并提示未决事项。

``nudge hook`` 由客户端的 hook 调用（stdin 为 hook 的 JSON），永远退出 0、只在有话要说时输出一段 JSON：

* ``session-start``：向模型注入候选采集协议与项目内试行的候选；未决候选超过阈值或久未处理时附一行用户可见提示。
* ``prompt-submit``（Claude Code、Codex）：用户消息命中长期偏好关键词时，提醒模型把它暂存为候选。
* ``stop``：任务看起来已完成且本次会话暂存了还没展示过的候选时，让模型一次性请用户裁决。

候选保存在项目的 ``.opinion/inbox.json``（由 opinion-manager 的 ``inbox`` 维护）；本模块只读取汇总，
按「待确认项提供者」契约聚合各来源，不写规则。阈值写在 Project Profile 的 ``opinion.nudge`` 或用户配置
``~/.config/agenthelm/config.json`` 的 ``nudge``（用户配置优先）。``nudge status`` 显示生效配置与当前汇总，
``nudge install-hooks --client cursor`` 预览或写入 Cursor 的 hooks.json。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ENV_DISABLE = "AGENTHELM_NO_NUDGE"
ENV_DEBUG = "AGENTHELM_NUDGE_DEBUG"
CLIENTS = ("claude", "codex", "cursor")
EVENTS = ("session-start", "prompt-submit", "stop")
STOP_MODES = ("off", "on-completion", "always")
OURS = re.compile(r"agenthelm\.py\"?\s+nudge\s+hook")
SUBPROCESS_TIMEOUT = 5
MAX_PROMPT_CHARS = 1500
SESSION_RETENTION_SECONDS = 14 * 24 * 3600
SESSION_LIMIT = 200

DEFAULT_KEYWORDS = [
    "以后都", "以后要", "以后不要", "以后别", "今后", "从现在起", "从现在开始", "每次都", "总是", "永远", "别再", "不要再",
    "不要总是", "必须", "一定要", "务必", "记住", "记一下", "下次", "我希望你", "我不喜欢", "我喜欢你",
    "always", "never", "from now on", "going forward", "remember to", "don't ever", "stop doing", "next time",
]
DEFAULT_COMPLETION = [
    "完成", "搞定", "已提交", "已推送", "已创建", "已实现", "已修复", "已合并", "总结",
    "done", "completed", "finished", "all set", "merged", "pushed", "implemented", "fixed",
]
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "pending_threshold": 5,
    "stale_days": 7,
    "max_prompts_per_session": 1,
    "capture_hints_per_session": 3,
    "trial_expire_days": 30,
    "reminder_repeat_hours": 24,
    "stop_prompt": {"claude": "on-completion", "codex": "on-completion", "cursor": "off"},
    "keywords": DEFAULT_KEYWORDS,
    "completion_keywords": DEFAULT_COMPLETION,
}


# ---------------------------------------------------------------- configuration

def _positive_int(value: Any, fallback: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else fallback


def _words(value: Any, fallback: list[str]) -> list[str]:
    if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
        return [item.strip().lower() for item in value]
    return [item.lower() for item in fallback]


def merge_config(*layers: Any) -> tuple[dict[str, Any], list[str]]:
    """内置默认 ← 项目 Profile ← 用户配置；非法值忽略并报告，不让会话失败。"""
    config = {key: (dict(value) if isinstance(value, dict) else value) for key, value in DEFAULTS.items()}
    warnings: list[str] = []
    for index, layer in enumerate(layers):
        label = ("项目 Profile opinion.nudge", "用户配置 nudge")[index]
        if layer is None:
            continue
        if not isinstance(layer, dict):
            warnings.append(f"{label} 必须是对象，已忽略")
            continue
        for key, value in layer.items():
            if key not in DEFAULTS:
                warnings.append(f"{label}.{key} 不是已知设置，已忽略")
            elif key == "enabled":
                if isinstance(value, bool):
                    config[key] = value
                else:
                    warnings.append(f"{label}.enabled 必须是布尔值，已忽略")
            elif key == "stop_prompt":
                modes = {client: value for client in CLIENTS} if isinstance(value, str) else value
                if isinstance(modes, dict) and all(client in CLIENTS and mode in STOP_MODES for client, mode in modes.items()):
                    config[key] = {**config[key], **modes}
                else:
                    warnings.append(f"{label}.stop_prompt 须为 {'/'.join(STOP_MODES)} 或 {{客户端: 模式}}，已忽略")
            elif key in ("keywords", "completion_keywords"):
                parsed = _words(value, [])
                if parsed:
                    config[key] = parsed
                else:
                    warnings.append(f"{label}.{key} 须为非空的字符串数组，已忽略")
            elif isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                config[key] = value
            else:
                warnings.append(f"{label}.{key} 须为正整数，已忽略")
    config["keywords"] = _words(config["keywords"], DEFAULT_KEYWORDS)
    config["completion_keywords"] = _words(config["completion_keywords"], DEFAULT_COMPLETION)
    return config, warnings


def project_profile(host: Any, root: Path) -> dict:
    try:
        data = host.read_json(root / host.PROFILE_FILE)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config(host: Any, env: Any, root: Path | None) -> tuple[dict[str, Any], list[str]]:
    profile_layer = None
    if root is not None:
        opinion = project_profile(host, root).get("opinion")
        profile_layer = opinion.get("nudge") if isinstance(opinion, dict) else None
    try:
        user = host.read_json(env.config_file)
        user_layer = user.get("nudge") if isinstance(user, dict) else None
    except (OSError, ValueError):
        user_layer = None
    return merge_config(profile_layer, user_layer)


# ---------------------------------------------------------------- project and manager discovery

def find_project(host: Any, start: Path | None) -> Path | None:
    """从 start 向上找启用了 opinion-manager 的项目根；与 Opinion 无关的目录不产生任何输出。"""
    if start is None:
        return None
    base = start.expanduser().resolve()
    for path in [base, *base.parents]:
        if (path / host.PROFILE_FILE).is_file():
            opinion = project_profile(host, path).get("opinion")
            provider = opinion.get("provider") if isinstance(opinion, dict) else None
            enabled = provider == "opinion-manager" or (path / ".opinion").is_dir() or (path / "opinion.lock.json").is_file()
            return path if enabled else None
        if (path / ".opinion").is_dir() or (path / "opinion.lock.json").is_file():
            return path
    return None


def payload_directory(payload: dict, client: str, environment: dict) -> Path | None:
    roots = payload.get("workspace_roots")
    candidate = (roots[0] if isinstance(roots, list) and roots else None) or payload.get("cwd") \
        or environment.get("CURSOR_PROJECT_DIR") or environment.get("CLAUDE_PROJECT_DIR")
    return Path(candidate) if isinstance(candidate, str) and candidate else None


def detect_client(requested: str, payload: dict, environment: dict) -> str:
    if requested != "auto":
        return requested
    if "workspace_roots" in payload or "conversation_id" in payload:
        return "cursor"
    if "turn_id" in payload or environment.get("PLUGIN_ROOT"):
        return "codex"
    return "claude"


def find_manager(host: Any, env: Any) -> Path | None:
    installed = host.installed_plugins(env, {"opinion-manager"})
    found = host.locate_companion("opinion-manager", installed)
    return found / "runtime" / "opinion_manager.py" if found is not None else None


def run_manager(manager: Path, *arguments: str) -> Any:
    try:
        completed = subprocess.run([sys.executable, str(manager), *arguments], capture_output=True, text=True,
                                   check=False, timeout=SUBPROCESS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    if "markdown" in arguments:
        return completed.stdout
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        return None
    return result if isinstance(result, dict) and result.get("ok") else None


# ---------------------------------------------------------------- pending-item providers

def opinion_provider(manager: Path | None, root: Path, session: str) -> dict | None:
    """待确认项提供者契约：返回 {source, items[{id, source, title, status, age_days, hint_command…}],
    counts, oldest_pending_days, last_action_at, session_unpresented}。其他来源（如 SOP 待办）按同一结构接入。"""
    if manager is None or not (root / ".opinion" / "inbox.json").is_file():
        return None
    arguments = ["inbox", "pending", "--project", str(root)] + (["--session", session] if session else [])
    return run_manager(manager, *arguments)


PROVIDERS = (opinion_provider,)


def collect(manager: Path | None, root: Path, session: str) -> dict:
    summaries = [result for provider in PROVIDERS if (result := provider(manager, root, session))]
    waiting = [item for summary in summaries for item in summary["items"] if item["status"] == "pending"]
    return {"summaries": summaries, "pending": len(waiting),
            "trial": sum(1 for summary in summaries for item in summary["items"] if item["status"] == "trial"),
            "oldest_days": max((item["age_days"] for item in waiting), default=0.0),
            "hint": next((item["hint_command"] for item in waiting), None),
            "session_unpresented": [identity for summary in summaries for identity in summary.get("session_unpresented", [])]}


def reminder_text(summary: dict, config: dict) -> str | None:
    count, oldest = summary["pending"], summary["oldest_days"]
    if not count or not (count >= config["pending_threshold"] or oldest >= config["stale_days"]):
        return None
    text = f"[Opinion] 有 {count} 条候选观点待确认"
    if oldest >= 1:
        text += f"（最久 {int(oldest)} 天未处理）"
    if summary["trial"]:
        text += f"，另有 {summary['trial']} 条在项目内试行"
    return text + "。在对话里让 Agent 逐条确认，或自行运行 opinion-manager 的 inbox list。"


# ---------------------------------------------------------------- machine-local state

def state_file(env: Any) -> Path:
    return env.state_dir / "agenthelm" / "nudge.json"


def load_state(host: Any, env: Any) -> dict:
    try:
        data = host.read_json(state_file(env))
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            data.setdefault("reminders", {})
            data.setdefault("exposure_noted", {})
            return data
    except (OSError, ValueError):
        pass
    return {"sessions": {}, "reminders": {}, "exposure_noted": {}}


def save_state(host: Any, env: Any, state: dict) -> None:
    moment = time.time()
    sessions = {key: value for key, value in state["sessions"].items() if moment - value.get("at", 0) < SESSION_RETENTION_SECONDS}
    if len(sessions) > SESSION_LIMIT:
        sessions = dict(sorted(sessions.items(), key=lambda item: item[1].get("at", 0))[-SESSION_LIMIT:])
    state["sessions"] = sessions
    try:
        host.write_json_atomic(state_file(env), state)
    except OSError:
        pass


def session_record(state: dict, session: str) -> dict:
    record = state["sessions"].setdefault(session or "unknown", {"capture_hints": 0, "stop_prompts": 0})
    record["at"] = time.time()
    return record


# ---------------------------------------------------------------- output per client

def render(client: str, event: str, *, context: str | None = None, system: str | None = None,
           follow_up: bool = False) -> dict | None:
    """各客户端的 hook 输出格式。context 为给模型的文字，system 为给用户看的一行。"""
    if not context and not system:
        return None
    if client == "cursor":
        if event == "session-start":
            return {"additional_context": context} if context else None
        return {"followup_message": context} if context and event == "stop" else None
    if event == "stop" and client == "codex":
        return {"decision": "block", "reason": context} if context else None
    name = {"session-start": "SessionStart", "prompt-submit": "UserPromptSubmit", "stop": "Stop"}[event]
    output: dict[str, Any] = {}
    if system:
        output["systemMessage"] = system
    if context:
        output["hookSpecificOutput"] = {"hookEventName": name, "additionalContext": context}
    return output


# ---------------------------------------------------------------- events

def session_start(host: Any, env: Any, payload: dict, client: str, root: Path, config: dict) -> dict | None:
    manager = find_manager(host, env)
    if manager is None:
        return None
    session = str(payload.get("session_id") or payload.get("conversation_id") or "")
    source = str(payload.get("source") or "startup")
    if (root / ".opinion" / "inbox.json").is_file():
        run_manager(manager, "inbox", "expire", "--project", str(root), "--days", str(config["trial_expire_days"]))
    brief = run_manager(manager, "inbox", "brief", "--project", str(root), *(["--session", session] if session else []))
    context = brief["text"] if brief else None
    if source == "compact":
        return render(client, "session-start", context=context)
    summary = collect(manager, root, session)
    text = reminder_text(summary, config)
    state = load_state(host, env)
    shown: list[str] = []
    if text:
        previous = state["reminders"].get(str(root), {})
        if time.time() - previous.get("at", 0) >= config["reminder_repeat_hours"] * 3600:
            state["reminders"][str(root)] = {"at": time.time()}
            shown.append(text)
            hint = f"\n用户已看到提示：「{text}」。不要打断当前任务；用户提起或任务间隙再协助逐条确认（inbox list --status pending --output markdown）。"
            context = (context or "") + hint
    notice = exposure_notice(manager, root, state)
    if notice:
        shown.append(notice)
        if client != "claude":
            context = (context or "") + f"\n[给用户的提示，他可能没有看到；请在首次回复里用一句话如实转告，是否处理由用户决定，不要自行改 .gitignore]\n{notice}"
    if shown:
        save_state(host, env, state)
    return render(client, "session-start", context=context, system="\n".join(shown) or None)


def exposure_notice(manager: Path, root: Path, state: dict) -> str | None:
    """收件箱含用户原话：每个项目只在第一次提醒一次是否会随仓库或同步盘被他人看到。只告知，不替用户决定，不改任何文件。"""
    if not (root / ".opinion" / "inbox.json").is_file() or str(root) in state["exposure_noted"]:
        return None
    result = run_manager(manager, "inbox", "exposure", "--project", str(root))
    if not result or not result.get("needs_notice"):
        return None
    state["exposure_noted"][str(root)] = {"at": time.time(), "git": result["git"]}
    return "[Opinion] " + result["message"]


def prompt_submit(host: Any, env: Any, payload: dict, client: str, root: Path, config: dict) -> dict | None:
    prompt = payload.get("prompt")
    if client == "cursor" or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        return None
    lowered = prompt.lower()
    hits = [word for word in config["keywords"] if word in lowered]
    if not hits:
        return None
    session = str(payload.get("session_id") or "")
    state = load_state(host, env)
    record = session_record(state, session)
    if record["capture_hints"] >= config["capture_hints_per_session"]:
        return None
    record["capture_hints"] += 1
    save_state(host, env, state)
    manager = find_manager(host, env)
    command = (f'python3 {manager} inbox add --project {root} --title "<标题>" --text "<完整规则>" --scope "<适用范围>" '
               f'--quote "<用户原话>"' + (f" --session {session}" if session else "")) if manager else "opinion-manager 的 inbox add"
    context = (f"[Opinion] 用户这条消息里有可能是长期偏好或对你行为的纠正的表述（命中：{'、'.join(hits[:3])}）。"
               f"若确属长期偏好、规则或纠正，先静默暂存为候选，不打断当前工作：{command}。"
               "仅限本次任务的要求、项目事实和命令不是候选，忽略即可。任务完成前再请用户集中确认。")
    return render(client, "prompt-submit", context=context)


def stop(host: Any, env: Any, payload: dict, client: str, root: Path, config: dict) -> dict | None:
    mode = config["stop_prompt"].get(client, "off")
    if mode == "off" or payload.get("stop_hook_active") or payload.get("loop_count"):
        return None
    if client == "cursor" and payload.get("status") not in (None, "completed"):
        return None
    session = str(payload.get("session_id") or payload.get("conversation_id") or "")
    inbox = root / ".opinion" / "inbox.json"
    if not session or not inbox.is_file() or session not in inbox.read_text(encoding="utf-8", errors="replace"):
        return None
    state = load_state(host, env)
    record = session_record(state, session)
    if record["stop_prompts"] >= config["max_prompts_per_session"]:
        return None
    message = str(payload.get("last_assistant_message") or "").lower()
    if mode == "on-completion" and client != "cursor" and not any(word in message for word in config["completion_keywords"]):
        return None
    manager = find_manager(host, env)
    if manager is None:
        return None
    summary = collect(manager, root, session)
    identifiers = summary["session_unpresented"]
    if not identifiers:
        return None
    listing = run_manager(manager, "inbox", "list", "--project", str(root), "--session", session, "--unpresented",
                          "--status", "pending", "--output", "markdown")
    if not listing:
        return None
    run_manager(manager, "inbox", "present", *identifiers, "--project", str(root))
    record["stop_prompts"] += 1
    save_state(host, env, state)
    context = (f"[Opinion] 本次会话暂存了 {len(identifiers)} 条候选观点，尚未向用户确认。请在不重复已完成工作的前提下，"
               "用下面的编号列表一次性请用户裁决：正式采纳 / 项目内试行 / 暂缓 / 丢弃（也可以逐条说明）。"
               f"用户明确表态后运行：python3 {manager} inbox decide <编号…> --project {root} --to trial|formal|held|rejected --confirmed。"
               "采纳为正式只改状态，写入 OPINION.md 须走 opinion-manager 的完整预览与确认；未经确认不要写入。\n\n" + listing)
    return render(client, "stop", context=context)


HANDLERS = {"session-start": session_start, "prompt-submit": prompt_submit, "stop": stop}


def read_payload() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def run_hook(args: argparse.Namespace, host: Any, payload: dict | None = None) -> dict | None:
    """hook 入口：任何异常都吞掉，保证不影响客户端会话。"""
    try:
        env = host.current_env(args.home)
        if env.get(ENV_DISABLE) not in (None, "0", "false"):
            return None
        payload = read_payload() if payload is None else payload
        client = detect_client(args.client, payload, dict(os.environ))
        root = find_project(host, payload_directory(payload, client, dict(os.environ)))
        if root is None:
            return None
        config, _ = load_config(host, env, root)
        if not config["enabled"]:
            return None
        return HANDLERS[args.event](host, env, payload, client, root, config)
    except Exception:  # noqa: BLE001 - a nudge must never break a session
        if os.environ.get(ENV_DEBUG):
            import traceback
            traceback.print_exc(file=sys.stderr)
        return None


# ---------------------------------------------------------------- status and hook installation

def status(args: argparse.Namespace, host: Any) -> dict:
    env = host.current_env(args.home)
    root = find_project(host, args.project or Path.cwd())
    config, warnings = load_config(host, env, root)
    result: dict[str, Any] = {"ok": True, "project": str(root) if root else None, "config": config, "warnings": warnings,
                              "disabled_by_environment": env.get(ENV_DISABLE) not in (None, "0", "false")}
    if root is None:
        result["note"] = "当前目录不在启用了 opinion-manager 的项目里：hook 不会产生任何输出。"
        return result
    manager = find_manager(host, env)
    result["opinion_manager"] = str(manager) if manager else None
    summary = collect(manager, root, "")
    result["summary"] = {key: summary[key] for key in ("pending", "trial", "oldest_days")}
    if manager is not None and (root / ".opinion" / "inbox.json").is_file():
        result["exposure"] = run_manager(manager, "inbox", "exposure", "--project", str(root))
    result["reminder"] = reminder_text(summary, config)
    return result


def cursor_commands(runtime: Path) -> dict[str, dict]:
    base = f'python3 "{runtime}" nudge hook --client cursor --event'
    return {"sessionStart": {"command": f"{base} session-start", "timeout": 10},
            "stop": {"command": f"{base} stop", "timeout": 10}}


def merged_cursor_hooks(existing: dict, runtime: Path, remove: bool) -> dict:
    data = json.loads(json.dumps(existing)) if existing else {}
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    wanted = cursor_commands(runtime)
    for event in list(hooks):
        kept = [entry for entry in hooks[event] if not (isinstance(entry, dict) and OURS.search(str(entry.get("command", ""))))]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not remove:
        for event, entry in wanted.items():
            hooks.setdefault(event, []).append(entry)
    return data


def install_hooks(args: argparse.Namespace, host: Any) -> dict:
    env = host.current_env(args.home)
    if args.scope == "project":
        project = (args.project or Path.cwd()).expanduser().resolve()
        target = project / ".cursor" / "hooks.json"
    else:
        target = env.home / ".cursor" / "hooks.json"
    try:
        existing = host.read_json(target) if target.is_file() else {}
    except ValueError as exc:
        raise host.CheckError(f"{target} 不是合法 JSON，已停止：{exc}") from exc
    if not isinstance(existing, dict):
        raise host.CheckError(f"{target} 的根节点必须是对象")
    runtime = Path(__file__).resolve().with_name("agenthelm.py")
    merged = merged_cursor_hooks(existing, runtime, args.remove)
    changed = merged != existing
    result = {"ok": True, "applied": False, "target": str(target), "changed": changed, "remove": args.remove,
              "hooks": merged.get("hooks", {})}
    if args.apply and changed:
        if target.is_file():
            backup = target.with_name(target.name + ".agenthelm-bak")
            if not backup.exists():
                backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
                result["backup"] = str(backup)
        host.write_json_atomic(target, merged)
        result["applied"] = True
    result["note"] = ("Cursor 的 sessionStart 注入是否到达模型取决于 Cursor 版本（社区有未到达的反馈）；"
                      "协议同时写在 opinion-manager 的 Skill 中，未到达时仍可按需触发。stop 提示默认关闭，"
                      "需要时把 nudge.stop_prompt.cursor 设为 on-completion。")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="agenthelm nudge", description=__doc__.split("\n\n")[0])
    sub = result.add_subparsers(dest="action", required=True)
    hook = sub.add_parser("hook", help="由客户端的 hook 调用：stdin 为 hook JSON，永远退出 0")
    hook.add_argument("--event", choices=EVENTS, required=True)
    hook.add_argument("--client", choices=("auto", *CLIENTS), default="auto")
    hook.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    state = sub.add_parser("status", help="显示生效的配置、当前未决汇总与是否会提示")
    state.add_argument("--project", type=Path)
    state.add_argument("--output", choices=("text", "json"), default="text")
    state.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    install = sub.add_parser("install-hooks", help="把 nudge 的 hook 写入 Cursor 的 hooks.json（默认只预览）")
    install.add_argument("--client", choices=("cursor",), default="cursor")
    install.add_argument("--scope", choices=("user", "project"), default="user")
    install.add_argument("--project", type=Path)
    install.add_argument("--remove", action="store_true", help="移除 agenthelm 写入的条目")
    install.add_argument("--apply", action="store_true")
    install.add_argument("--output", choices=("text", "json"), default="text")
    install.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    return result


def main(argv: list[str], host: Any) -> int:
    args = parser().parse_args(argv)
    if args.action == "hook":
        output = run_hook(args, host)
        if output is not None:
            print(json.dumps(output, ensure_ascii=False))
        return 0
    try:
        result = status(args, host) if args.action == "status" else install_hooks(args, host)
    except host.CheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.action == "status":
        lines = [f"项目：{result['project'] or '（不在启用 opinion-manager 的项目里）'}",
                 f"开关：{'已被环境变量 AGENTHELM_NO_NUDGE 关闭' if result['disabled_by_environment'] else ('开启' if result['config']['enabled'] else '关闭')}"]
        config = result["config"]
        lines.append(f"阈值：待确认 ≥{config['pending_threshold']} 条或最久 ≥{config['stale_days']} 天；每会话确认 ≤{config['max_prompts_per_session']} 次；"
                     f"试行 {config['trial_expire_days']} 天未转正回退；任务结束提示 {config['stop_prompt']}")
        if result.get("summary"):
            summary = result["summary"]
            lines.append(f"当前：待确认 {summary['pending']} 条，试行 {summary['trial']} 条，最久 {int(summary['oldest_days'])} 天")
            lines.append(result["reminder"] or "按当前阈值，开新会话不会提示。")
        if result.get("exposure"):
            lines.append(f"收件箱的可见范围：{result['exposure']['message']}")
        lines += [f"配置警告：{item}" for item in result["warnings"]]
        if result.get("note"):
            lines.append(result["note"])
        print("\n".join(lines))
    else:
        verb = "已写入" if result["applied"] else ("没有变化" if not result["changed"] else "将写入（加 --apply 执行）")
        print(f"{verb}：{result['target']}")
        print(json.dumps(result["hooks"], ensure_ascii=False, indent=2))
        print(result["note"])
    return 0
