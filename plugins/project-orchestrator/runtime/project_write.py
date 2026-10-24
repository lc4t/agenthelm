"""项目入口的写入：记录事项与项目专用工具。

写入一律先预览、确认后执行，执行后回读验证：

- 默认 Case 工具：经 case.py 的 plan_create / plan_update 生成新内容，写入前核对内容摘要与
  ``updated_at``，别人刚改过就拒绝（退出码 5）。未知字段与其他小节原样保留。
- 项目声明了自己的事项工具：只有在 ``entry.items.write`` 为 true 时经它写入（约定见
  case-workspaces.md「项目入口适配」）；否则如实说明缺口，绝不绕过它直接改文件。
- 项目工具（tools.json）：只运行明确声明、当前可用、由人选中并确认的那一个；参数列表直接
  执行，不经过 shell。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import case as case_tool
from project_base import DEFAULT_TOOLS, EntryError, inside

TOOL_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TOOL_TIMEOUT_SECONDS = 600
DIFF_LIMIT = 80


# ---------------------------------------------------------------- record

def record_capability(provider: Any) -> tuple[str, str]:
    adapter = provider.entry["items"]
    if adapter:
        if adapter.get("write"):
            return "available", f"经项目工具 {' '.join(adapter['command'])} 写入"
        return "unavailable", f"项目事项工具 {' '.join(adapter['command'])} 没有声明写接口（entry.items.write）"
    if provider.case_command:
        return "unavailable", f"项目声明了自己的 Case 工具（{provider.case_command}），本入口不绕过它写入"
    if provider.work_mode != "case-workspace":
        return "unavailable", "项目没有使用 Case 工作目录"
    return "available", ""


def _gap(provider: Any) -> EntryError:
    state, reason = record_capability(provider)
    steps = [reason]
    if provider.entry["items"] or provider.case_command:
        steps.append("需要时请直接用项目自己的工具；或请项目维护者按 case-workspaces.md「项目入口适配」补上写接口")
    return EntryError("这个项目不能在本入口记录事项", "unavailable", steps)


def _diff(path: str, current: str, new: str) -> list[str]:
    lines = [line.rstrip("\n") for line in difflib.unified_diff(
        current.splitlines(keepends=True), new.splitlines(keepends=True), lineterm="", n=1)][2:]
    return lines[:DIFF_LIMIT] + ([f"…另有 {len(lines) - DIFF_LIMIT} 行"] if len(lines) > DIFF_LIMIT else [])


def _slug(change: dict) -> str:
    raw = change.get("slug") or re.sub(r"[^a-z0-9]+", "-", str(change.get("title") or "").lower()).strip("-")
    return raw or "item"


def _case_args(change: dict, **extra: Any) -> argparse.Namespace:
    values = {"status": change.get("status"), "next_action": change.get("next_action"),
              "current_state": None, "waiting_for": change.get("waiting_for"),
              "resume_when": change.get("resume_when"), "title": None, "log": change.get("log"),
              "decision": None, "result": None, "add_deliverable": list(change.get("add_deliverable") or []),
              "add_evidence": [], "add_tag": [], "add_alias": [], "expect_updated_at": None, "expect_sha256": None}
    values.update(extra)
    return argparse.Namespace(**values)


def _new_args(change: dict) -> argparse.Namespace:
    next_action = change.get("next_action") or ""
    return argparse.Namespace(title=change.get("title"), slug=_slug(change), goal=change.get("goal"),
                              next_action=next_action, status="active" if next_action else "captured",
                              done_when=None, source=None, current_state=None, tag=[])


def _project_relative(root: Path, value: str) -> str:
    """Accept an absolute path inside the project (as copied from 找资料) as well as a relative one."""
    path = Path(value.strip())
    if path.is_absolute():
        resolved = path.resolve()
        if root in resolved.parents:
            return resolved.relative_to(root).as_posix()
    return value.strip()


def _case_relative(root: Path, case_dir: Path, value: str) -> str:
    """Files inside the Case are recorded relative to it, the way CASE.md links are written."""
    absolute = (root / value).resolve()
    return absolute.relative_to(case_dir).as_posix() if case_dir in absolute.parents else value


def preview(provider: Any, target: str | None, change: dict) -> dict:
    state, _ = record_capability(provider)
    if state != "available":
        raise _gap(provider)
    change = {**change, "add_deliverable": [_project_relative(provider.root, value)
                                            for value in change.get("add_deliverable") or []]}
    for path in change["add_deliverable"]:
        inside(provider.root, path, "交付物路径")
        if not (provider.root / path).exists():
            raise EntryError(f"要登记的文件不存在：{path}", "usage", ["路径相对于项目根目录"])
    if provider.entry["items"]:
        return _tool_preview(provider, target, change)
    try:
        project = case_tool.Project(provider.root)
        if target is None:
            if not (change.get("title") and change.get("goal")):
                raise EntryError("新建事项需要标题和目标", "usage")
            case_id, text = case_tool.plan_create(project, _new_args(change))
            path = f"{project.relative(project.case_root)}/{case_id}/CASE.md"
            front = text.split("\n---\n", 1)[0].splitlines()[1:]
            lines = [f"+{line}" for line in front] + ["  （正文按 Case 模板生成：背景、下一动作、工作记录等小节）"]
            return {"kind": "new", "target": None, "path": path, "lines": lines,
                    "expect_updated_at": None, "expect_sha256": None, "change": change, "simulated": False,
                    "note": "编号以写入时为准（同一天有人先建了事项时顺延）"}
        item = provider.item(target)
        case_dir = (provider.root / item["path"]).parent
        change = {**change, "add_deliverable": [_case_relative(provider.root, case_dir, value)
                                                for value in change.get("add_deliverable") or []]}
        path, current, text, _ = case_tool.plan_update(project, _case_args(change, case=item["id"]))
    except case_tool.CaseError as exc:
        raise EntryError(str(exc), "conflict" if exc.code == "conflict" else "usage") from exc
    return {"kind": "update", "target": item["id"], "path": project.relative(path),
            "lines": _diff(project.relative(path), current, text), "expect_updated_at": item["updated_at"],
            "expect_sha256": hashlib.sha256(current.encode("utf-8")).hexdigest(), "change": change,
            "simulated": False}


def apply(provider: Any, planned: dict) -> dict:
    if provider.entry["items"]:
        return _tool_apply(provider, planned)
    change = planned["change"]
    try:
        project = case_tool.Project(provider.root)
        if planned["kind"] == "new":
            result = case_tool.create(project, _new_args(change))
            data, _ = case_tool.read_case(provider.root / result["path"])
            verified = data.get("title") == change.get("title")
            return {"path": result["path"], "id": result["id"], "simulated": False,
                    "verify": ("写入后重新读取：已建立 " if verified else "写入后读回不一致：") + result["path"]}
        before, _ = case_tool.read_case(provider.root / planned["path"])
        result = case_tool.update(project, _case_args(change, case=planned["target"],
                                                      expect_updated_at=planned["expect_updated_at"],
                                                      expect_sha256=planned["expect_sha256"]))
        after, _ = case_tool.read_case(provider.root / planned["path"])
    except case_tool.CaseError as exc:
        if exc.code == "conflict":
            raise EntryError("这个事项在你预览之后被修改过，没有写入", "conflict",
                             [str(exc), "重新打开预览，看过对方的改动后再提交"]) from exc
        raise EntryError(str(exc), "usage") from exc
    kept = [key for key in before if key in after]
    return {"path": planned["path"], "id": result["id"], "simulated": False, "changed": result["changed"],
            "verify": f"写入后重新读取：已更新 {'、'.join(result['changed'])}；原有 {len(kept)} 个字段与其他小节保留"}


def _tool_flags(change: dict) -> list[str]:
    flags = []
    for key, flag in (("title", "--title"), ("goal", "--goal"), ("next_action", "--next-action"),
                      ("waiting_for", "--waiting-for"), ("status", "--status"), ("log", "--log")):
        if change.get(key):
            flags += [flag, str(change[key])]
    for path in change.get("add_deliverable") or []:
        flags += ["--add-deliverable", path]
    return flags


def _run_items_tool(provider: Any, arguments: list[str]) -> tuple[int, dict]:
    command = [*provider.entry["items"]["command"], *arguments, "--output", "json"]
    try:
        completed = subprocess.run(command, cwd=provider.root, capture_output=True, text=True, check=False,
                                   timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EntryError(f"项目事项工具无法运行：{exc}", "failed", [" ".join(command)]) from exc
    try:
        payload = json.loads(completed.stdout or "{}")
    except ValueError:
        payload = {"error": (completed.stderr or completed.stdout).strip()[:300]}
    return completed.returncode, payload if isinstance(payload, dict) else {}


def _tool_preview(provider: Any, target: str | None, change: dict) -> dict:
    if target is None:
        arguments = ["new", *_tool_flags(change), "--dry-run"]
        item = None
    else:
        item = provider.item(target)
        arguments = ["update", item["id"], *_tool_flags(change), "--dry-run"]
    code, payload = _run_items_tool(provider, arguments)
    if code != 0:
        raise EntryError(f"项目事项工具拒绝了这次修改：{payload.get('error') or payload}", "usage")
    lines = payload.get("preview") or (payload.get("diff") or "").splitlines() or ["（项目工具没有给出预览内容）"]
    return {"kind": "new" if item is None else "update", "target": item["id"] if item else None,
            "path": payload.get("path") or (item["path"] if item else ""), "lines": [str(line) for line in lines],
            "expect_updated_at": item["updated_at"] if item else None, "expect_sha256": None, "change": change,
            "simulated": False}


def _tool_apply(provider: Any, planned: dict) -> dict:
    change = planned["change"]
    if planned["kind"] == "new":
        arguments = ["new", *_tool_flags(change)]
    else:
        arguments = ["update", planned["target"], *_tool_flags(change)]
        if planned["expect_updated_at"]:
            arguments += ["--expect-updated-at", planned["expect_updated_at"]]
    code, payload = _run_items_tool(provider, arguments)
    if code != 0:
        if payload.get("code") == "conflict":
            raise EntryError("这个事项在你预览之后被修改过，项目工具没有写入", "conflict",
                             ["重新打开预览，看过对方的改动后再提交"])
        raise EntryError(f"项目事项工具写入失败：{payload.get('error') or payload}", "failed")
    provider._items = None
    return {"path": payload.get("path") or planned["path"], "simulated": False,
            "verify": "项目工具已写入；重新读取列表：" + ("找到该事项" if _still_listed(provider, planned, payload)
                                                     else "列表里没有找到，请用项目工具核对")}


def _still_listed(provider: Any, planned: dict, payload: dict) -> bool:
    wanted = payload.get("id") or planned["target"]
    try:
        return any(item["id"] == wanted for item in provider.items()["items"])
    except EntryError:
        return False


# ---------------------------------------------------------------- project tools

def load_tools(provider: Any) -> dict:
    """Declared tools with their current availability; never runs anything."""
    declared = provider.entry["tools"]
    relative = declared or DEFAULT_TOOLS.as_posix()
    try:
        path = inside(provider.root, relative, "entry.tools")
    except EntryError as exc:
        return {"tools": [], "error": str(exc), "path": relative}
    if not path.is_file():
        return {"tools": [], "error": f"entry.tools 指向的文件不存在：{relative}" if declared else "",
                "path": relative}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data["tools"]
        if not isinstance(raw, list):
            raise TypeError("tools 必须是列表")
        tools = [_tool(provider, item) for item in raw]
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        return {"tools": [], "error": f"{relative} 格式不对：{exc}", "path": relative}
    ids = [tool["id"] for tool in tools]
    if len(ids) != len(set(ids)):
        return {"tools": [], "error": f"{relative} 里有重复的工具 id", "path": relative}
    return {"tools": tools, "error": "", "path": relative}


def _tool(provider: Any, item: Any) -> dict:
    if not isinstance(item, dict):
        raise TypeError("每个工具必须是对象")
    tool_id, command = item.get("id"), item.get("command")
    if not isinstance(tool_id, str) or not TOOL_ID_RE.fullmatch(tool_id):
        raise ValueError(f"工具 id 只能用小写字母、数字和连字符：{tool_id!r}")
    if not (isinstance(command, list) and command and all(isinstance(part, str) and part for part in command)):
        raise ValueError(f"{tool_id}: command 必须是非空参数列表（不经过 shell）")
    requires = item.get("requires") or {}
    commands = [str(value) for value in requires.get("commands", [command[0]])]
    env_names = [str(value) for value in requires.get("env", [])]
    writes = [str(value) for value in item.get("writes", [])]
    network = bool(item.get("network", False))
    missing = [name for name in commands if not _command_available(provider.root, name)]
    unset = [name for name in env_names if not os.environ.get(name)]
    reasons = ([f"缺少命令 {'、'.join(missing)}"] if missing else []) + \
        ([f"缺少环境变量 {'、'.join(unset)}"] if unset else []) + \
        (["当前离线"] if network and provider.offline else [])
    return {"id": tool_id, "title": str(item.get("title") or tool_id), "description": str(item.get("description") or ""),
            "command": command, "network": network, "account": "、".join(env_names) or None, "writes": writes,
            "requires": commands + env_names, "state": "unavailable" if reasons else "available",
            "reason": "；".join(reasons)}


def _command_available(root: Path, name: str) -> bool:
    if Path(name).is_absolute():
        return Path(name).is_file() and os.access(name, os.X_OK)
    if "/" in name:
        try:
            candidate = inside(root, name, "工具命令")
        except EntryError:
            return False
        return candidate.is_file()
    return shutil.which(name) is not None


def check_tools(provider: Any, findings: list[dict], ok: list[str]) -> None:
    loaded = load_tools(provider)
    if loaded["error"]:
        findings.append({"id": "tools-invalid", "level": "suggest", "area": "项目工具", "title": "项目工具声明有误",
                         "reason": loaded["error"], "impact": "项目工具暂不可用，其他功能不受影响",
                         "next": [{"label": f"修正 {loaded['path']}", "action": ""}]})
        return
    for tool in loaded["tools"]:
        if tool["state"] != "available":
            findings.append({"id": f"tool-{tool['id']}", "level": "unavailable", "area": "项目工具",
                             "title": f"“{tool['title']}”当前不可用", "reason": tool["reason"],
                             "impact": "只影响这一项工具", "next": [{"label": "补齐后重新检查", "action": ""}]})
    if loaded["tools"]:
        ok.append(f"项目工具（{sum(tool['state'] == 'available' for tool in loaded['tools'])}/{len(loaded['tools'])} 可用）")


def run_tool(provider: Any, tool_id: str, *, capture: bool = True) -> dict:
    tool = next((tool for tool in load_tools(provider)["tools"] if tool["id"] == tool_id), None)
    if tool is None:
        raise EntryError(f"项目没有声明这个工具：{tool_id}", "not-found")
    if tool["state"] != "available":
        raise EntryError(f"“{tool['title']}”当前不可用", "unavailable", [tool["reason"]])
    try:
        completed = subprocess.run(tool["command"], cwd=provider.root, check=False, timeout=TOOL_TIMEOUT_SECONDS,
                                   capture_output=capture, text=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EntryError(f"“{tool['title']}”无法运行：{exc}", "failed") from exc
    output = ((completed.stdout or "") + (completed.stderr or "")).strip() if capture else ""
    if completed.returncode != 0:
        raise EntryError(f"“{tool['title']}”运行失败（退出码 {completed.returncode}）", "failed",
                         [line for line in output.splitlines()[-5:]] or ["没有输出"])
    return {"id": tool_id, "simulated": False, "exit_code": completed.returncode, "output": output[-2000:],
            "message": f"已运行：{' '.join(tool['command'])}" + (f"\n{output[-1200:]}" if output else "")}
