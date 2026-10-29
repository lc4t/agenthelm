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

from nudge_text import LANGUAGES, resolve_language, text as say

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
    "language": "auto",
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
    problems: list[tuple[str, dict]] = []
    for index, layer in enumerate(layers):
        label = ("label_profile", "label_user")[index]
        if layer is None:
            continue
        if not isinstance(layer, dict):
            problems.append(("warn_object", {"label": label}))
            continue
        for key, value in layer.items():
            if key not in DEFAULTS:
                problems.append(("warn_unknown", {"label": label, "key": key}))
            elif key == "enabled":
                if isinstance(value, bool):
                    config[key] = value
                else:
                    problems.append(("warn_bool", {"label": label, "key": key}))
            elif key == "language":
                if value in ("auto", *LANGUAGES):
                    config[key] = value
                else:
                    problems.append(("warn_language", {"label": label}))
            elif key == "stop_prompt":
                modes = {client: value for client in CLIENTS} if isinstance(value, str) else value
                if isinstance(modes, dict) and all(client in CLIENTS and mode in STOP_MODES for client, mode in modes.items()):
                    config[key] = {**config[key], **modes}
                else:
                    problems.append(("warn_stop", {"label": label, "modes": "/".join(STOP_MODES)}))
            elif key in ("keywords", "completion_keywords"):
                parsed = _words(value, [])
                if parsed:
                    config[key] = parsed
                else:
                    problems.append(("warn_words", {"label": label, "key": key}))
            elif isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                config[key] = value
            else:
                problems.append(("warn_int", {"label": label, "key": key}))
    config["keywords"] = _words(config["keywords"], DEFAULT_KEYWORDS)
    config["completion_keywords"] = _words(config["completion_keywords"], DEFAULT_COMPLETION)
    config["lang"] = resolve_language(config["language"])
    warnings = [say(key, config["lang"], **{**fields, **({"label": say(fields["label"], config["lang"])} if "label" in fields else {})})
                for key, fields in problems]
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


def run_manager(manager: Path, *arguments: str, lang: str | None = None) -> Any:
    command = [*arguments, "--lang", lang] if lang else list(arguments)
    try:
        completed = subprocess.run([sys.executable, str(manager), *command], capture_output=True, text=True,
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

def opinion_provider(manager: Path | None, root: Path, session: str, lang: str) -> dict | None:
    """待确认项提供者契约：返回 {source, items[{id, source, title, status, age_days, hint_command…}],
    counts, oldest_pending_days, last_action_at, session_unpresented}。其他来源（如 SOP 待办）按同一结构接入。"""
    if manager is None or not (root / ".opinion" / "inbox.json").is_file():
        return None
    arguments = ["inbox", "pending", "--project", str(root)] + (["--session", session] if session else [])
    return run_manager(manager, *arguments, lang=lang)


PROVIDERS = (opinion_provider,)


def collect(manager: Path | None, root: Path, session: str, lang: str) -> dict:
    summaries = [result for provider in PROVIDERS if (result := provider(manager, root, session, lang))]
    waiting = [item for summary in summaries for item in summary["items"] if item["status"] == "pending"]
    return {"summaries": summaries, "pending": len(waiting),
            "trial": sum(1 for summary in summaries for item in summary["items"] if item["status"] == "trial"),
            "oldest_days": max((item["age_days"] for item in waiting), default=0.0),
            "hint": next((item["hint_command"] for item in waiting), None),
            "session_unpresented": [identity for summary in summaries for identity in summary.get("session_unpresented", [])]}


def reminder_text(summary: dict, config: dict) -> str | None:
    count, oldest, lang = summary["pending"], summary["oldest_days"], config["lang"]
    if not count or not (count >= config["pending_threshold"] or oldest >= config["stale_days"]):
        return None
    return say("reminder", lang, count=count,
               age=say("reminder_age", lang, days=int(oldest)) if oldest >= 1 else "",
               trial=say("reminder_trial", lang, trial=summary["trial"]) if summary["trial"] else "")


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
        run_manager(manager, "inbox", "expire", "--project", str(root), "--days", str(config["trial_expire_days"]), lang=config["lang"])
    brief = run_manager(manager, "inbox", "brief", "--project", str(root), *(["--session", session] if session else []), lang=config["lang"])
    context = brief["text"] if brief else None
    if source == "compact":
        return render(client, "session-start", context=context)
    summary = collect(manager, root, session, config["lang"])
    text = reminder_text(summary, config)
    state = load_state(host, env)
    shown: list[str] = []
    if text:
        previous = state["reminders"].get(str(root), {})
        if time.time() - previous.get("at", 0) >= config["reminder_repeat_hours"] * 3600:
            state["reminders"][str(root)] = {"at": time.time()}
            shown.append(text)
            context = (context or "") + say("reminder_hint", config["lang"], text=text)
    notice = exposure_notice(manager, root, state, config["lang"])
    if notice:
        shown.append(notice)
        if client != "claude":
            context = (context or "") + say("relay", config["lang"], notice=notice)
    if shown:
        save_state(host, env, state)
    return render(client, "session-start", context=context, system="\n".join(shown) or None)


def exposure_notice(manager: Path, root: Path, state: dict, lang: str) -> str | None:
    """收件箱含用户原话：每个项目只在第一次提醒一次是否会随仓库或同步盘被他人看到。只告知，不替用户决定，不改任何文件。"""
    if not (root / ".opinion" / "inbox.json").is_file() or str(root) in state["exposure_noted"]:
        return None
    result = run_manager(manager, "inbox", "exposure", "--project", str(root), lang=lang)
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
    lang = config["lang"]
    command = say("add_command", lang, manager=manager, root=root, lang=lang,
                  session=f" --session {session}" if session else "") if manager else say("add_command_fallback", lang)
    context = say("prompt_hint", lang, hits=say("sep", lang).join(hits[:3]), command=command)
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
    summary = collect(manager, root, session, config["lang"])
    identifiers = summary["session_unpresented"]
    if not identifiers:
        return None
    listing = run_manager(manager, "inbox", "list", "--project", str(root), "--session", session, "--unpresented",
                          "--status", "pending", "--output", "markdown", lang=config["lang"])
    if not listing:
        return None
    run_manager(manager, "inbox", "present", *identifiers, "--project", str(root), lang=config["lang"])
    record["stop_prompts"] += 1
    save_state(host, env, state)
    context = say("stop_request", config["lang"], count=len(identifiers), manager=manager, root=root, lang=config["lang"], listing=listing)
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
        result["note"] = say("note_no_project", config["lang"])
        return result
    manager = find_manager(host, env)
    result["opinion_manager"] = str(manager) if manager else None
    summary = collect(manager, root, "", config["lang"])
    result["summary"] = {key: summary[key] for key in ("pending", "trial", "oldest_days")}
    if manager is not None and (root / ".opinion" / "inbox.json").is_file():
        result["exposure"] = run_manager(manager, "inbox", "exposure", "--project", str(root), lang=config["lang"])
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
    lang = load_config(host, env, None)[0]["lang"]
    if args.scope == "project":
        project = (args.project or Path.cwd()).expanduser().resolve()
        target = project / ".cursor" / "hooks.json"
    else:
        target = env.home / ".cursor" / "hooks.json"
    try:
        existing = host.read_json(target) if target.is_file() else {}
    except ValueError as exc:
        raise host.CheckError(say("err_hooks_json", lang, target=target, error=exc)) from exc
    if not isinstance(existing, dict):
        raise host.CheckError(say("err_hooks_root", lang, target=target))
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
    result["note"] = say("note_cursor", lang)
    result["lang"] = lang
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
        config = result["config"]
        lang = config["lang"]
        if result["disabled_by_environment"]:
            state = say("status_env_off", lang)
        else:
            state = say("status_on", lang) if config["enabled"] else say("status_off", lang)
        lines = [say("status_project", lang, project=result["project"] or say("status_no_project", lang)),
                 say("status_switch", lang, state=state),
                 say("status_thresholds", lang, pending=config["pending_threshold"], stale=config["stale_days"],
                     prompts=config["max_prompts_per_session"], expire=config["trial_expire_days"],
                     stop=config["stop_prompt"], language=f"{config['language']} ({lang})")]
        if result.get("summary"):
            summary = result["summary"]
            lines.append(say("status_current", lang, pending=summary["pending"], trial=summary["trial"], days=int(summary["oldest_days"])))
            lines.append(result["reminder"] or say("status_quiet", lang))
        if result.get("exposure"):
            lines.append(say("status_exposure", lang, message=result["exposure"]["message"]))
        lines += [say("status_warning", lang, item=item) for item in result["warnings"]]
        if result.get("note"):
            lines.append(result["note"])
        print("\n".join(lines))
    else:
        lang = result["lang"]
        verb = say("install_written" if result["applied"] else ("install_unchanged" if not result["changed"] else "install_preview"), lang)
        print(say("install_line", lang, verb=verb, target=result["target"]))
        print(json.dumps(result["hooks"], ensure_ascii=False, indent=2))
        print(result["note"])
    return 0
