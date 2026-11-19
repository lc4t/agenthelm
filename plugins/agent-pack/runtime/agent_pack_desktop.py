#!/usr/bin/env python3
"""Read-only paste-ready snippet for Claude Desktop's ``claude_desktop_config.json`` (writes nothing, ever).

Built from an ``agenthelm inventory`` MCP entry, which is already redacted: only the structure (``command``, ``args``,
``url``) and variable *names* appear; every credential is a placeholder. An entry with an inline secret is blocked and
produces no snippet at all.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))
import agent_pack_ext_common as ext  # noqa: E402
from i18n_agent_pack import _  # noqa: E402

REDACTED = "<redacted>"
# Official location of the Desktop config (design #82 section 2.3; first-party page not re-read in this change).
DESKTOP_CONFIG = {
    "darwin": "~/Library/Application Support/Claude/claude_desktop_config.json",
    "linux": "~/.config/Claude/claude_desktop_config.json",
}


def config_path(platform: str | None = None) -> str | None:
    platform = platform or sys.platform
    return DESKTOP_CONFIG["darwin" if platform == "darwin" else "linux"] if (
        platform == "darwin" or platform.startswith("linux")) else None


def placeholder(variable: str) -> str:
    return _("<在此填入你的 {0}>").format(variable)


def build(entry: dict, platform: str | None = None) -> dict:
    """``{path, snippet, blocked, notes, restart}`` for one inventory MCP entry (no file is read or written)."""
    path = config_path(platform)
    result: dict[str, Any] = {"ref": entry["ref"], "path": path, "snippet": None, "blocked": [], "notes": [],
                              "promise": "cannot-promise",
                              "restart": _("保存后请完全退出并重启 Claude Desktop")}
    if path is None:
        result["blocked"].append(ext.blocked("unsupported-platform", _("只支持 macOS 与 Linux")))
        return result
    if entry.get("evidence") != "file":
        result["blocked"].append(ext.blocked("evidence-unknown", _("这个条目没有文件级证据，只读")))
    if entry.get("has_inline_secret"):
        result["blocked"].append(ext.blocked(
            "inline-secret", _("这个条目含有内联的凭据值：不生成片段。请先把凭据改成环境变量引用，再重新运行")))
    if result["blocked"]:
        return result
    server: dict[str, Any] = {}
    transport = entry.get("transport")
    if entry.get("command"):
        server["command"] = entry["command"]
        args = [placeholder(_("凭据")) if str(arg) == REDACTED else arg for arg in entry.get("args") or []]
        if any(str(arg) == REDACTED for arg in entry.get("args") or []):
            result["notes"].append(ext.warning("args-redacted", _("参数里有被脱敏的值，已换成占位符，请自行填入")))
        if args:
            server["args"] = args
    elif entry.get("url"):
        server["url"] = placeholder("URL") if REDACTED in str(entry["url"]) else entry["url"]
        result["notes"].append(ext.warning("remote-unconfirmed",
                                           _("官方文档没有说明 claude_desktop_config.json 是否加载远程 url 条目：未确认")))
    else:
        result["blocked"].append(ext.blocked("no-launch-spec", _("条目里没有 command 或 url，无法生成片段")))
        return result
    names = list(entry.get("env_names") or [])
    if names:
        server["env"] = {name: placeholder(name) for name in names}
    if entry.get("header_names"):
        result["notes"].append(ext.warning("headers-not-included", _("请求头 {0} 未写入片段：Desktop 是否支持未确认").format(
            ", ".join(entry["header_names"])), header_names=list(entry["header_names"])))
    if transport == "sse":
        result["notes"].append(ext.warning("sse-transport", _("这是 SSE 传输；Desktop 是否加载未确认")))
    result["snippet"] = {"mcpServers": {entry["name"]: server}}
    result["notes"].append(ext.warning("desktop-load-unknown", _("Desktop 是否会加载这一项没有可核实的证据，工具只给出文件内容")))
    return result


def render_text(document: dict) -> str:
    import json  # noqa: PLC0415
    lines = [_("Claude Desktop 配置片段（只读输出，不写任何文件）：{0}").format(document["ref"])]
    lines.append(_("目标文件：{0}").format(document["path"] or "-"))
    for item in document["blocked"]:
        lines.append(_("已阻止 [{0}]：{1}").format(item["code"], item["message"]))
    if document["snippet"] is not None:
        lines.append(_("把下面的内容合并进文件里的 mcpServers（已有同名条目先自行比较）："))
        lines.append(json.dumps(document["snippet"], ensure_ascii=False, indent=2))
    for item in document["notes"]:
        lines.append(_("注意：{0}").format(item["message"]))
    if document["snippet"] is not None:
        lines.append(document["restart"])
    return "\n".join(lines) + "\n"
