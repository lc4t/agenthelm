#!/usr/bin/env python3
"""agenthelm config：用户级设置（``~/.config/agenthelm/config.json``）与语言提示。

有两种语言，互不相干：

* **界面语言**：命令行输出、菜单、提示用哪种语言。顺序是调用方指定 → 环境变量 ``AGENTHELM_LANG`` →
  用户配置的 ``language`` → 系统 locale → 英文。只影响屏幕上的文字，不改动任何项目文件。
* **项目文件语言**：写进项目里的文件（Case 小节名、README 追加的说明、SOP）用哪种语言，是项目 Profile 顶层的
  ``language``（``zh`` | ``en``，没有就按中文），随项目同步。

没有任何地方指定界面语言时，``check`` 和会话开始的提示说明当前语言从哪来，并给出设置命令；
``language_hint`` 设为 ``false`` 永久关闭这些提示。设置命令默认只预览，``--apply`` 才写入，
并且只改动要改的键：文件里的其他设置原样保留，替换前先备份。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from i18n_agent_pack import _, N_, normalize  # noqa: E402

LANGUAGE_NAMES = {"zh": N_("中文"), "en": "English"}
SOURCE_NAMES = {"locale": N_("系统 locale"), "default": N_("默认值")}
KNOWN_KEYS = ("language", "language_hint", "update_check")


def read_settings(path: Path) -> tuple[dict, str | None]:
    """(设置, 错误)：文件不存在算空设置；无法解析时返回空设置和原因，调用方不得覆盖这个文件。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, str(exc)
    try:
        data = json.loads(text)
    except ValueError as exc:
        return {}, str(exc)
    if not isinstance(data, dict):
        return {}, _("根节点必须是对象")
    return data, None


def language_status(variables: Mapping[str, str], settings: Mapping[str, Any]) -> dict:
    """界面语言从哪来，以及要不要提示用户去设置。"""
    explicit_env = normalize(variables.get("AGENTHELM_LANG"))
    configured = normalize(settings.get("language"))
    if explicit_env:
        language, source = explicit_env, "env"
    elif configured:
        language, source = configured, "config"
    else:
        language, source = "en", "default"
        for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
            found = normalize(variables.get(name))
            if found:
                language, source = found, "locale"
                break
    explicit = source in ("env", "config")
    dismissed = settings.get("language_hint") is False
    return {"language": language, "source": source, "explicit": explicit, "dismissed": dismissed,
            "hint": not explicit and not dismissed}


def interface_hint(status: dict) -> str:
    return _("界面语言未设置，当前使用 {1}（来自{0}）。固定语言：agenthelm config language zh（中文）或 agenthelm config language en（English），"
             "默认只预览，加 --apply 写入；不想再看到这条提示：agenthelm config language-hint off。"
             "界面语言只影响命令输出，不改动任何项目文件。").format(
        _(SOURCE_NAMES.get(status["source"], status["source"])), _(LANGUAGE_NAMES[status["language"]]))


def project_hint() -> str:
    return _("这个项目没有声明项目文件语言（Profile 的 language），写进项目的文件按中文生成。要改成英文：agenthelm config project-language en"
             "（预览；加 --apply 写入，只改 Profile，不改写已有文件）。")


def short_hint(status: dict) -> str:
    """会话开始用的一行。"""
    return _("界面语言未设置（当前 {0}）：agenthelm config language zh|en 固定；agenthelm config language-hint off 关闭这条提示。").format(
        _(LANGUAGE_NAMES[status["language"]]))


def report_section(variables: Mapping[str, str], settings: Mapping[str, Any], scaffold: dict | None) -> dict:
    """check 报告里的 ``language`` 段（无论是否提示都给出，便于脚本读取）。"""
    status = language_status(variables, settings)
    declared_known = isinstance(scaffold, dict) and isinstance(scaffold.get("project_language"), dict)
    declared = scaffold["project_language"].get("declared") if declared_known else None
    project_needs_hint = declared_known and declared is None and not status["dismissed"]
    return {"interface": {"language": status["language"], "source": status["source"], "explicit": status["explicit"]},
            "project": {"declared": declared, "effective": declared or "zh"} if declared_known else None,
            "hint_dismissed": status["dismissed"],
            "hints": ([interface_hint(status)] if status["hint"] else []) + ([project_hint()] if project_needs_hint else []),
            "session_hint": short_hint(status) if status["hint"] else None}


# ---------------------------------------------------------------- the command

def build_parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="agenthelm config", description=_("用户级设置：界面语言、语言提示、项目文件语言。默认只预览，--apply 才写入。"))
    result.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    sub = result.add_subparsers(dest="action", required=True)

    def common(parser: argparse.ArgumentParser, project: bool = False) -> None:
        parser.add_argument("--output", choices=("text", "json"), default="text")
        parser.add_argument("--apply", action="store_true", help=_("写入（缺省只预览）"))
        if project:
            parser.add_argument("--project", type=Path, help=_("项目目录；缺省当前目录"))

    show = sub.add_parser("show", help=_("显示生效的设置与文件位置"))
    show.add_argument("--output", choices=("text", "json"), default="text")
    language = sub.add_parser("language", help=_("设置界面语言（命令行输出用哪种语言）；auto 表示跟随系统 locale"))
    language.add_argument("value", choices=("zh", "en", "auto"))
    common(language)
    hint = sub.add_parser("language-hint", help=_("开关“语言未设置”的提示"))
    hint.add_argument("value", choices=("on", "off"))
    common(hint)
    project = sub.add_parser("project-language", help=_("设置当前项目的文件语言（Profile 的 language；只改 Profile，不改写已有文件）"))
    project.add_argument("value", choices=("zh", "en"))
    common(project, project=True)
    return result


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _change(settings: dict, key: str, value: Any) -> tuple[dict, list[dict]]:
    """``value`` 为 None 表示删除该键。其余键原样保留。"""
    updated = dict(settings)
    before = settings.get(key)
    if value is None:
        updated.pop(key, None)
    else:
        updated[key] = value
    return updated, ([{"key": key, "from": before, "to": value}] if before != value else [])


def _emit(args: argparse.Namespace, payload: dict, lines: list[str]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.output == "json" else "\n".join(lines))


def _show(args: argparse.Namespace, host: Any, env: Any) -> int:
    settings, error = read_settings(env.config_file)
    status = language_status(env.variables, settings)
    unknown = sorted(key for key in settings if key not in KNOWN_KEYS)
    payload = {"ok": error is None, "path": str(env.config_file), "exists": env.config_file.is_file(), "error": error,
               "language": {"effective": status["language"], "source": status["source"], "configured": settings.get("language")},
               "language_hint": not status["dismissed"], "update_check": settings.get("update_check", True) is not False,
               "other_keys": unknown}
    sources = {"env": _("环境变量 AGENTHELM_LANG"), "config": _("用户配置"), "locale": _("系统 locale"), "default": _("默认值")}
    lines = [_("用户配置：{0}（{1}）").format(env.config_file, _("存在") if payload["exists"] else _("不存在")),
             _("界面语言：{0}（来自{1}）").format(_(LANGUAGE_NAMES[status["language"]]), sources[status["source"]]),
             _("语言未设置的提示：{0}").format(_("开") if payload["language_hint"] else _("关")),
             _("联网检查更新：{0}").format(_("开") if payload["update_check"] else _("关"))]
    if unknown:
        lines.append(_("其他设置（不由本命令管理）：{0}").format(_("、").join(unknown)))
    if error:
        lines.append(_("配置文件无法读取：{0}").format(error))
    _emit(args, payload, lines)
    return 0 if error is None else 2


def _set_key(args: argparse.Namespace, host: Any, env: Any, key: str, value: Any, label: str) -> int:
    path = env.config_file
    settings, error = read_settings(path)
    if error is not None:
        message = _("{0} 无法读取：{1}。先修正它；本命令不会覆盖无法解析的文件。").format(path, error)
        _emit(args, {"ok": False, "error": message}, [message])
        return 2
    updated, changes = _change(settings, key, value)
    payload: dict[str, Any] = {"ok": True, "applied": False, "path": str(path), "changes": changes,
                               "effective_language": language_status(env.variables, updated)["language"]}
    lines = []
    for item in changes:
        before = _("（未设置）") if item["from"] is None else json.dumps(item["from"], ensure_ascii=False)
        after = _("（删除）") if item["to"] is None else json.dumps(item["to"], ensure_ascii=False)
        lines.append(f"  {item['key']}: {before} → {after}")
    if not changes:
        lines.append("  " + _("{0} 已经是这个值，没有需要改的。").format(label))
    elif args.apply:
        if path.is_file():  # 替换前备份，和其他会替换文件的命令一样
            recovery = host.recovery_dir_for(env, "config")
            recovery.mkdir(parents=True, mode=0o700)
            (recovery / path.name).write_bytes(path.read_bytes())
            payload["recovery"] = str(recovery)
            lines.append(_("原件已备份到：{0}").format(recovery))
        _write_settings(path, updated)
        payload["applied"] = True
        lines.append(_("其他设置原样保留。"))
    else:
        lines.append(_("确认后加 --apply 写入；其他设置原样保留。"))
    header = _("已写入：用户配置 {0}") if payload["applied"] else _("预览：用户配置 {0}")
    _emit(args, payload, [header.format(path), *lines])
    return 0


def _project_language(args: argparse.Namespace, host: Any, env: Any) -> int:
    project = (args.project or Path.cwd()).resolve()
    manager = host.locate_companion("agents-init", host.installed_plugins(env, {"agents-init"}))
    script = manager / "skills" / "agents-init" / "scripts" / "init_project.py" if manager else None
    if script is None or "language" not in script.read_text(encoding="utf-8"):
        message = _("没有找到支持设置项目文件语言的 agents-init：更新插件后再试。")
        _emit(args, {"ok": False, "error": message}, [message])
        return 3
    command = [sys.executable, str(script), "--project", str(project), "--mode", "language", "--language", args.value,
               "--output", "json"] + (["--apply"] if args.apply else [])
    completed = subprocess.run(command, capture_output=True, text=True, check=False, env=env.child_environment())
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        message = (completed.stderr or completed.stdout).strip()[:300] or _("agents-init 没有返回结果")
        _emit(args, {"ok": False, "error": message}, [message])
        return 2
    if completed.returncode != 0 or not result.get("ok", True):
        message = str(result.get("error") or result)
        _emit(args, {"ok": False, "error": message}, [message])
        return 2
    header = _("已写入：项目 Profile 的 language {0} → {1}") if result.get("applied") else _("预览：项目 Profile 的 language {0} → {1}")
    lines = [header.format(result.get("from") or _("（未声明，按中文）"), result["language"])]
    if not result.get("changed"):
        lines.append("  " + _("项目文件语言已经是 {0}，没有需要改的。").format(result["language"]))
    if result.get("diff"):
        lines.append(result["diff"].rstrip())
    if result.get("recovery"):
        lines.append(_("原件已备份到：{0}").format(result["recovery"]))
    if result["changed"] and not result.get("applied"):
        lines.append(_("确认后加 --apply 写入；只改 Profile，已有文件不会被改写。"))
    _emit(args, result, lines)
    return 0


def main(argv: list[str] | None, host: Any) -> int:
    args = build_parser().parse_args(argv)
    env = host.current_env(args.home)
    if args.action == "show":
        return _show(args, host, env)
    if args.action == "language":
        return _set_key(args, host, env, "language", None if args.value == "auto" else args.value, _("界面语言"))
    if args.action == "language-hint":
        return _set_key(args, host, env, "language_hint", None if args.value == "on" else False, _("语言提示"))
    return _project_language(args, host, env)
