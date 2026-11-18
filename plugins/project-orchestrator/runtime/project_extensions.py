"""技能与 MCP 的只读清单：取数（子进程调用 ``agenthelm inventory``）、重复归类、机械体检与文字渲染。

- 不 import agent-pack：只用参数列表（不经 shell）运行 ``agenthelm inventory --output json`` 并解析 JSON。
  找不到命令、命令失败、输出不是 JSON、``schema_version`` 主版本未知时一律降级为「不可用」并写明原因，不猜测。
- 全程只读、不联网。MCP 条目只取白名单字段（名字、客户端、作用域、存储、传输、指纹前缀、变量名、url 主机名）；
  命令行参数、url 全文、环境变量值、headers 从不进入任何输出。
- 「哪一份生效」只在官方文档写明优先级的场景下下结论，其余一律「未知」；证据为 unknown 的副本永远显示「未知」。
- 体检只输出机械事实与阈值，不打分、不排名、不建议删除。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

from wf import S, display_width, term_width
from i18n_orchestrator import _, N_  # noqa: E402

SUPPORTED_SCHEMA_MAJOR = 1
INVENTORY_TIMEOUT_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 10
EVIDENCE_LEVELS = ("file", "setting", "cli", "unknown")
# 阈值：来自 Agent Skills 规范与 Claude Code Skills 文档（2026-11-18 读取），是社区或厂商约定，不是对本仓库的强制要求。
SPEC_DESCRIPTION_CHARS = 1024
CLAUDE_DESCRIPTION_CAP_CHARS = 1536
SPEC_SKILL_MD_LINES = 500
# description 相似度提示的阈值：没有外部依据，只用于提示（设计文档 D-CB8）。
SIMILARITY_THRESHOLD = 0.6
SIMILARITY_MAX_ENTRIES = 1500
SOURCE_SPEC = N_("Agent Skills 规范 agentskills.io/specification")
SOURCE_CLAUDE = N_("Claude Code Skills 文档 code.claude.com/docs/en/skills")

SCOPE_LABEL = {"user": N_("用户级"), "project": N_("项目级"), "local": N_("本机项目级"),
               "managed": N_("企业级"), "enterprise": N_("企业级"), "desktop": N_("应用级")}
KIND_LABEL = {"skill": N_("技能"), "mcp": "MCP"}
BADGE = {"file": "●", "setting": "⚙", "cli": "⌘", "unknown": "?"}
# 官方文档写明优先级的场景（设计文档 §4.2、§5.2）；数值越大越优先。其余组合一律「未知」。
MCP_PRECEDENCE = {"claude": {"local": 3, "project": 2, "user": 1}, "codex": {"project": 2, "user": 1}}
SKILL_PRECEDENCE = {"claude": {"managed": 3, "enterprise": 3, "user": 2, "personal": 2, "project": 1}}
FORBIDDEN_NAME = re.compile("[<>\\u200b\\u200c\\u200d\\u2060\\ufeff]")

TAG_LABEL = {"no-description": N_("无 description"), "desc-too-long": N_("描述会被截断"), "skill-md-long": N_("体积大"),
             "name-dir-mismatch": N_("名称与目录不一致"), "name-bad-chars": N_("name 含禁用字符"),
             "frontmatter-incomplete": N_("frontmatter 不全"), "stale": N_("过期")}


# ---------------------------------------------------------------- 取数

def agenthelm_command(host: Any) -> list[str] | None:
    """经 ``agenthelm project`` 启动时用同一个 agent-pack 的 agenthelm.py；否则找 PATH 里的 agenthelm。"""
    if host is not None:
        script = Path(host.__file__).resolve().parent / "agenthelm.py"
        if script.is_file():
            return [sys.executable, str(script)]
    found = shutil.which("agenthelm")
    return [found] if found else None


def _run(command: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OSError(_("等待 agenthelm 超过 {0} 秒").format(timeout)) from None
    return done.returncode, done.stdout, (done.stderr or "").strip()[:200]


def unavailable(reason: str) -> dict:
    return {"state": "unavailable", "reason": reason, "inventory": None}


def probe(host: Any) -> tuple[bool, str]:
    """能力探测：找得到命令且 ``inventory --help`` 能运行才算可用。不读取任何清单。"""
    command = agenthelm_command(host)
    if command is None:
        return False, _("未找到 agent-pack 的 inventory：没有可运行的 agenthelm 命令")
    try:
        code, _out, _err = _run([*command, "inventory", "--help"], PROBE_TIMEOUT_SECONDS)
    except OSError as exc:
        return False, _("无法运行 agenthelm inventory：{0}").format(exc)
    if code != 0:
        return False, _("当前的 agenthelm 没有 inventory 命令（需要更新 agent-pack）")
    return True, ""


def inventory_argv(command: list[str], root: Path | None, include_description: bool) -> list[str]:
    argv = [*command, "inventory", "--output", "json"]
    if root is not None:
        argv += ["--project", str(root)]
    if include_description:
        argv.append("--include-description")
    return argv


def load(host: Any, root: Path | None, *, include_description: bool = False) -> dict:
    """运行一次 inventory；返回 {state, reason, inventory}。不抛错：不可用本身就是结果。"""
    command = agenthelm_command(host)
    if command is None:
        return unavailable(_("未找到 agent-pack 的 inventory：没有可运行的 agenthelm 命令"))
    try:
        code, out, err = _run(inventory_argv(command, root, include_description), INVENTORY_TIMEOUT_SECONDS)
    except OSError as exc:
        return unavailable(_("无法运行 agenthelm inventory：{0}").format(exc))
    if code == 2 and not out.strip():  # argparse 的用法错误：这个 agenthelm 不认识 inventory 子命令
        return unavailable(_("当前的 agenthelm 没有 inventory 命令（需要更新 agent-pack）"))
    try:
        data = json.loads(out)
    except ValueError:
        detail = err or _("输出不是 JSON")
        return unavailable(_("agenthelm inventory 没有返回可解析的结果：{0}").format(detail))
    if not isinstance(data, dict):
        return unavailable(_("agenthelm inventory 返回的不是对象"))
    if code != 0 and not isinstance(data.get("skills"), list) and not isinstance(data.get("mcps"), list):
        return unavailable(_("agenthelm inventory 运行失败（退出码 {0}）：{1}").format(code, err or _("无详情")))
    return validate(data)


def validate(data: dict) -> dict:
    version = data.get("schema_version")
    major = str(version).split(".")[0] if version is not None else ""
    if not major.isdigit():
        return unavailable(_("inventory 没有 schema_version：无法确认字段含义，不猜测"))
    if int(major) != SUPPORTED_SCHEMA_MAJOR:
        return unavailable(_("inventory 的 schema_version {0} 超出本菜单支持的主版本 {1}").format(version, SUPPORTED_SCHEMA_MAJOR))
    if not isinstance(data.get("skills", []), list) or not isinstance(data.get("mcps", []), list):
        return unavailable(_("inventory 的 skills 或 mcps 不是列表"))
    return {"state": "available", "reason": "", "inventory": data}


# ---------------------------------------------------------------- 规范化（白名单字段）

def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() and value.strip().lower() != "unknown" else None


def _evidence(value: Any) -> str:
    return value if value in EVIDENCE_LEVELS else "unknown"


def _origin(raw: Any) -> dict:
    if isinstance(raw, str):
        raw = {"type": raw}
    raw = raw if isinstance(raw, dict) else {}
    return {"type": _text(raw.get("type")), "id": _text(raw.get("id")), "version": _text(raw.get("version")),
            "latest_known": _text(raw.get("latest_known")), "evidence": _evidence(raw.get("evidence"))}


def _referenced(raw: Any) -> list[dict]:
    """opinion 预留位：只认 [{"rule_id", "source"}]；缺失或形状不对就当没有。不调用任何 opinion 命令。"""
    if not isinstance(raw, list):
        return []
    return [{"rule_id": str(item["rule_id"]), "source": str(item.get("source") or "")}
            for item in raw if isinstance(item, dict) and item.get("rule_id")]


def _ref(kind: str, name: str, client: str, scope: str, given: Any) -> str:
    return given if isinstance(given, str) and given else f"{kind}:{name}@{client}:{scope}"


def _entry(kind: str, raw: Any) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"]:
        return None
    name = raw["name"]
    client = _text(raw.get("client")) or "unknown"
    scope = _text(raw.get("scope")) or "unknown"
    entry: dict[str, Any] = {"ref": _ref(kind, name, client, scope, raw.get("ref")), "kind": kind, "name": name,
                             "client": client, "scope": scope, "store": _text(raw.get("store")),
                             "evidence": _evidence(raw.get("evidence"))}
    if kind == "skill":
        front = raw.get("frontmatter") if isinstance(raw.get("frontmatter"), dict) else {}
        description = front.get("description") if isinstance(front.get("description"), str) else None
        sha = _text(raw.get("content_sha256"))
        entry.update({
            "description_bytes": _int(raw.get("description_bytes")), "description_chars": _int(raw.get("description_chars")),
            "skill_md_bytes": _int(raw.get("skill_md_bytes")), "skill_md_lines": _int(raw.get("skill_md_lines")),
            "dir_bytes": _int(raw.get("dir_bytes")), "file_count": _int(raw.get("file_count")),
            "content_sha8": sha[:8] if sha else None, "_sha": sha,
            "is_symlink": raw.get("is_symlink") if isinstance(raw.get("is_symlink"), bool) else raw.get("symlink")
            if isinstance(raw.get("symlink"), bool) else None,
            "link_target_kind": _text(raw.get("link_target_kind")),
            "name_matches_dir": raw.get("name_matches_dir") if isinstance(raw.get("name_matches_dir"), bool) else None,
            "frontmatter_ok": raw.get("frontmatter_ok") if isinstance(raw.get("frontmatter_ok"), bool) else None,
            "frontmatter_name": _text(front.get("name")), "origin": _origin(raw.get("origin")),
            "version": _text(raw.get("version")),
            "stale": raw.get("stale") if isinstance(raw.get("stale"), bool) else None,
            "loaded": raw.get("loaded") if isinstance(raw.get("loaded"), bool) else None,
            "shadowed_by": _text(raw.get("shadowed_by")) or (raw["shadowed_by"] if isinstance(raw.get("shadowed_by"), list)
                                                              and raw["shadowed_by"] else None),
            "_description": description})
    else:
        fp = _text(raw.get("fingerprint"))
        names = raw.get("env_var_names")
        fields = raw.get("fingerprint_fields")
        entry.update({
            "transport": _text(raw.get("transport")), "fingerprint8": fp[:8] if fp else None, "_fp": fp,
            "fingerprint_fields": [item for item in fields if isinstance(item, str)] if isinstance(fields, list) else None,
            "env_var_names": [item for item in names if isinstance(item, str)] if isinstance(names, list) else None,
            "has_inline_secret": raw.get("has_inline_secret") if isinstance(raw.get("has_inline_secret"), bool) else None,
            "url_host": _text(raw.get("url_host"))})
    referenced = _referenced(raw.get("referenced_by"))
    if referenced:
        entry["referenced_by"] = referenced
    return entry


def normalize(inventory: dict) -> dict:
    entries = [item for kind, key in (("skill", "skills"), ("mcp", "mcps"))
               for item in (_entry(kind, raw) for raw in inventory.get(key, [])) if item]
    clients = inventory.get("clients")
    names = {item if isinstance(item, str) else item.get("name") for item in clients
             if isinstance(item, (str, dict))} if isinstance(clients, list) else set()
    unknown_extra = inventory.get("unknown")
    return {"entries": entries, "clients": sorted({*(name for name in names if isinstance(name, str)),
                                                   *(entry["client"] for entry in entries)}),
            "unknown_extra": len(unknown_extra) if isinstance(unknown_extra, list) else 0}


def public(entry: dict) -> dict:
    """对外（JSON、文本）可见的字段：去掉下划线开头的内部字段。"""
    return {key: value for key, value in entry.items() if not key.startswith("_")}


# ---------------------------------------------------------------- 重复归类与「哪一份生效」

def _group_id(kind: str, label: str, key: str) -> str:
    return f"{'m' if kind == 'mcp' else 's'}-{hashlib.sha1(f'{kind}|{label}|{key}'.encode()).hexdigest()[:6]}"


def _is_desktop(client: str) -> bool:
    return "desktop" in client


def _client_effective(kind: str, client: str, members: list[dict]) -> tuple[bool, str]:
    """同一客户端内、同名多份时哪份生效。(是否已确定, 说明)"""
    unknown = _("{0}：生效：未知（{1}）")
    if any(member["evidence"] == "unknown" for member in members):
        return False, unknown.format(client, _("有副本的证据为未知"))
    shadowed = [member for member in members if member.get("shadowed_by")]
    if shadowed:
        return True, _("{0}：{1} 被屏蔽（inventory 给出的结论）").format(
            client, _("、").join(member["ref"] for member in shadowed))
    if kind == "mcp" and client in MCP_PRECEDENCE:
        ranks = MCP_PRECEDENCE[client]
        order = sorted(members, key=lambda member: ranks.get(member["scope"], 0), reverse=True)
        if all(member["scope"] in ranks for member in members) and ranks[order[0]["scope"]] > ranks[order[1]["scope"]]:
            rule = "local > project > user" if client == "claude" else _("项目层 > 用户层")
            doc = _("Claude Code MCP 文档") if client == "claude" else _("Codex MCP 文档")
            return True, _("{0}：本项目里生效的是 {1} 那份（{2}，{3}）").format(
                client, _(SCOPE_LABEL.get(order[0]["scope"], order[0]["scope"])), rule, doc)
    if kind == "skill" and client in SKILL_PRECEDENCE:
        ranks = SKILL_PRECEDENCE[client]
        order = sorted(members, key=lambda member: ranks.get(member["scope"], 0), reverse=True)
        if all(member["scope"] in ranks for member in members) and ranks[order[0]["scope"]] > ranks[order[1]["scope"]]:
            return True, _("{0}：{1} 优先（企业 > 个人 > 项目，{2}）").format(
                client, _(SCOPE_LABEL.get(order[0]["scope"], order[0]["scope"])), _(SOURCE_CLAUDE))
    if kind == "skill" and client == "codex":
        return True, _("{0}：同名的两份都会出现在技能选择里，不存在屏蔽（Codex 官方说明）").format(client)
    return False, unknown.format(client, _("优先级未文档化，不下结论"))


def _effective(kind: str, cls: str, members: list[dict]) -> dict:
    clients: dict[str, list[dict]] = {}
    for member in members:
        clients.setdefault(member["client"], []).append(member)
    lines: list[str] = []
    known = True
    if cls == "diff-name-same-fp":
        return {"known": True, "basis": "distinct-names", "lines": [_("名字不同，不构成同名覆盖")]}
    if any(member["evidence"] == "unknown" for member in members):
        reasons = [member["ref"] for member in members if member["evidence"] == "unknown"]
        return {"known": False, "basis": "evidence-unknown",
                "lines": [_("生效：未知（证据不足：{0}）").format(_("、").join(reasons))]}
    for client, group in clients.items():
        if len(group) > 1:
            ok, text = _client_effective(kind, client, group)
            known = known and ok
            lines.append(text)
    desktop = [name for name in clients if _is_desktop(name)]
    if kind == "mcp" and desktop and "claude" in clients:
        lines.append(_("Desktop 配置与 claude 同名：只在 Desktop 应用的本地 Code 标签会话里 Desktop 配置那份优先；独立 CLI 不读它"))
    elif len(clients) > 1 and not lines:
        lines.append(_("各客户端独立加载，彼此不冲突"))
    elif len(clients) > 1:
        lines.append(_("不同客户端之间各自独立加载，彼此不冲突"))
    return {"known": known, "basis": "documented" if known else "undocumented", "lines": lines}


CLASS_LABEL = {
    ("mcp", "same-name-same"): N_("同名 · 同指纹"), ("mcp", "same-name-differs"): N_("同名 · 指纹不同（内容漂移）"),
    ("mcp", "same-name-unknown"): N_("同名 · 证据不足"), ("mcp", "diff-name-same-fp"): N_("不同名 · 同指纹"),
    ("skill", "same-name-same"): N_("同名 · 内容相同"), ("skill", "same-name-differs"): N_("同名 · 内容不同（漂移）"),
    ("skill", "same-name-unknown"): N_("同名 · 证据不足")}


def build_groups(entries: list[dict]) -> list[dict]:
    groups: list[dict] = []
    for kind in ("mcp", "skill"):
        key = "_fp" if kind == "mcp" else "_sha"
        scoped = [entry for entry in entries if entry["kind"] == kind]
        by_name: dict[str, list[dict]] = {}
        for entry in scoped:
            by_name.setdefault(entry["name"], []).append(entry)
        for name, members in by_name.items():
            if len(members) < 2:
                continue
            keys = [member[key] for member in members]
            cls = "same-name-unknown" if None in keys else "same-name-same" if len(set(keys)) == 1 else "same-name-differs"
            groups.append(_group(kind, cls, name, members))
        if kind == "mcp":
            by_fp: dict[str, list[dict]] = {}
            for entry in scoped:
                if entry["_fp"]:
                    by_fp.setdefault(entry["_fp"], []).append(entry)
            for fingerprint, members in by_fp.items():
                if len({member["name"] for member in members}) > 1:
                    groups.append(_group(kind, "diff-name-same-fp", fingerprint[:8], members))
    return sorted(groups, key=lambda group: (group["kind"] != "mcp", group["name"], group["class"]))


def _group(kind: str, cls: str, name: str, members: list[dict]) -> dict:
    clients = [member["client"] for member in members]
    desktop_pair = kind == "mcp" and any(_is_desktop(client) for client in clients) and "claude" in clients
    cross = len(set(clients)) == len(clients) and not desktop_pair
    ordered = sorted(members, key=lambda member: (member["client"], member["scope"], member["store"] or ""))
    return {"id": _group_id(kind, cls, name), "kind": kind, "class": cls, "name": name,
            "label": CLASS_LABEL[(kind, cls)], "cross_client": cross,
            "evidence": "unknown" if any(member["evidence"] == "unknown" for member in members) else
            min((member["evidence"] for member in members), key=EVIDENCE_LEVELS.index),
            "members": [member["ref"] for member in ordered], "effective": _effective(kind, cls, ordered),
            "_members": ordered}


def public_group(group: dict) -> dict:
    return {key: value for key, value in group.items() if not key.startswith("_")}


# ---------------------------------------------------------------- 体检（只给机械事实与阈值）

def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9]+", lowered))
    cjk = [char for char in lowered if 0x4E00 <= ord(char) <= 0x9FFF or 0x3040 <= ord(char) <= 0x30FF]
    return words | {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}


def _finding(entry: dict, code: str, fact: str, threshold: str | None = None, source: str | None = None) -> dict:
    return {"ref": entry["ref"], "code": code, "fact": fact, "threshold": threshold, "source": source}


def skill_findings(entry: dict) -> tuple[list[dict], list[str]]:
    """一个技能条目的机械发现，以及 inventory 没有提供、因此没有检查的字段。证据未知的条目不下任何结论。"""
    if entry["evidence"] == "unknown":
        return [], []
    found: list[dict] = []
    missing: list[str] = []
    chars, bytes_ = entry["description_chars"], entry["description_bytes"]
    if bytes_ is None and chars is None:
        missing.append("description_bytes")
    elif bytes_ == 0 or chars == 0:
        found.append(_finding(entry, "no-description", _("缺 description"), _("规范要求必填"), _(SOURCE_SPEC)))
    if chars is None:
        missing.append("description_chars")
    elif chars > CLAUDE_DESCRIPTION_CAP_CHARS:
        found.append(_finding(entry, "desc-too-long", _("description {0} 字符，超过 Claude Code 技能列表截断上限 {1}，超出部分会被截断").format(
            chars, CLAUDE_DESCRIPTION_CAP_CHARS), _("{0} 字符").format(CLAUDE_DESCRIPTION_CAP_CHARS), _(SOURCE_CLAUDE)))
    elif chars > SPEC_DESCRIPTION_CHARS:
        found.append(_finding(entry, "desc-too-long", _("description {0} 字符，超过规范上限 {1}").format(
            chars, SPEC_DESCRIPTION_CHARS), _("{0} 字符").format(SPEC_DESCRIPTION_CHARS), _(SOURCE_SPEC)))
    if entry["skill_md_lines"] is None:
        missing.append("skill_md_lines")
    elif entry["skill_md_lines"] > SPEC_SKILL_MD_LINES:
        found.append(_finding(entry, "skill-md-long", _("SKILL.md {0} 行，超过规范建议的 {1} 行").format(
            entry["skill_md_lines"], SPEC_SKILL_MD_LINES), _("{0} 行（建议值，不是强制）").format(SPEC_SKILL_MD_LINES), _(SOURCE_SPEC)))
    if entry["name_matches_dir"] is None:
        missing.append("name_matches_dir")
    elif entry["name_matches_dir"] is False:
        found.append(_finding(entry, "name-dir-mismatch", _("目录名与 name 不一致"), _("规范要求一致"), _(SOURCE_SPEC)))
    if FORBIDDEN_NAME.search(entry["name"]) or any(unicodedata.category(char) == "Cc" for char in entry["name"]):
        found.append(_finding(entry, "name-bad-chars", _("name 含 < >、零宽或控制字符"), _("Claude Code 会拒绝这样的 name"), _(SOURCE_CLAUDE)))
    if entry["frontmatter_ok"] is None:
        missing.append("frontmatter_ok")
    elif entry["frontmatter_ok"] is False and not any(item["code"] == "no-description" for item in found):
        found.append(_finding(entry, "frontmatter-incomplete", _("frontmatter 缺少必填字段"), _("规范要求 name 与 description"), _(SOURCE_SPEC)))
    origin = entry["origin"]
    if origin["version"] and origin["latest_known"] and origin["version"] != origin["latest_known"]:
        found.append(_finding(entry, "stale", _("来源 {0} 已安装 {1}，本机已知最新 {2}").format(
            origin["id"] or origin["type"] or _("未知"), origin["version"], origin["latest_known"]),
            _("版本不同即提示；不用修改时间推断"), None))
    elif entry["stale"] is True:
        found.append(_finding(entry, "stale", _("inventory 标记该技能有新版本"), _("不用修改时间推断"), None))
    return found, missing


def cost_per_client(skills: list[dict]) -> list[dict]:
    """每客户端每会话 description 常驻字节。只对「会加载」的求和：证据非 unknown，且没被屏蔽、没标为不加载。"""
    result: dict[str, dict] = {}
    for entry in skills:
        row = result.setdefault(entry["client"], {"client": entry["client"], "bytes": 0, "counted": 0, "uncounted": 0,
                                                   "capped": 0, "evidence": set()})
        known_bytes = entry["description_bytes"]
        if entry["evidence"] == "unknown" or entry["loaded"] is False or entry.get("shadowed_by") or known_bytes is None:
            row["uncounted"] += 1
            continue
        chars = entry["description_chars"]
        if chars and chars > CLAUDE_DESCRIPTION_CAP_CHARS and entry["client"] == "claude":
            known_bytes = round(known_bytes * CLAUDE_DESCRIPTION_CAP_CHARS / chars)
            row["capped"] += 1
        row["bytes"] += known_bytes
        row["counted"] += 1
        row["evidence"].add(entry["evidence"])
    return [{"client": row["client"], "resident_bytes": row["bytes"], "counted": row["counted"],
             "uncounted": row["uncounted"], "capped": row["capped"], "evidence": sorted(row["evidence"], key=EVIDENCE_LEVELS.index),
             "token_estimate": {"low": row["bytes"] // 4, "high": row["bytes"] // 2,
                                "label": _("估算，仅用于排序与对比；字节÷4（拉丁文）至÷2（中文），没有一手依据")}}
            for row in sorted(result.values(), key=lambda row: row["client"])]


def similar_pairs(skills: list[dict]) -> tuple[list[dict], str | None]:
    """description 文本相似的技能对（机械判断，不代表功能重复）。只需要 inventory 以 --include-description 输出的文本。"""
    candidates = [entry for entry in skills if entry["_description"] and entry["evidence"] != "unknown"]
    if len(candidates) > SIMILARITY_MAX_ENTRIES:
        return [], _("技能超过 {0} 个，没有做 description 相似度比较").format(SIMILARITY_MAX_ENTRIES)
    tokens = {entry["ref"]: _tokens(entry["_description"]) for entry in candidates}
    pairs = []
    for index, first in enumerate(candidates):
        for second in candidates[index + 1:]:
            if first["name"] == second["name"]:
                continue
            a, b = tokens[first["ref"]], tokens[second["ref"]]
            if a and b and len(a & b) / len(a | b) >= SIMILARITY_THRESHOLD:
                pairs.append({"refs": sorted([first["ref"], second["ref"]]), "jaccard": round(len(a & b) / len(a | b), 2)})
    return sorted(pairs, key=lambda pair: pair["refs"]), None


def check(norm: dict, include_description: bool) -> dict:
    skills = [entry for entry in norm["entries"] if entry["kind"] == "skill"]
    findings: list[dict] = []
    missing_counts: dict[str, int] = {}
    for entry in skills:
        found, missing = skill_findings(entry)
        findings += found
        for field in missing:
            missing_counts[field] = missing_counts.get(field, 0) + 1
    groups = build_groups(norm["entries"])
    for group in groups:
        if group["kind"] == "skill" and group["class"] == "same-name-differs":
            for ref in group["members"]:
                findings.append({"ref": ref, "code": "drift", "fact": _("同名技能内容不一致（{0} 份），来源是否相同需要 origin 才能判断").format(
                    len(group["members"])), "threshold": None, "source": None, "group": group["id"]})
    notes = [_("{0} 个技能缺少 {1}：这一项没有检查（inventory 未提供）").format(count, field)
             for field, count in sorted(missing_counts.items())]
    stale_unknown = sum(1 for entry in skills if entry["evidence"] != "unknown" and not entry["origin"]["version"]
                        and entry["stale"] is None)
    if stale_unknown:
        notes.append(_("过期：{0} 个技能的来源或版本无法识别，显示为未知（不用修改时间推断）").format(stale_unknown))
    similar: list[dict] = []
    if include_description:
        similar, why = similar_pairs(skills)
        if why:
            notes.append(why)
    else:
        notes.append(_("description 相似度提示默认不做；加 --include-description 才比较（文本只在本机内存里用）"))
    notes.append(_("MCP 工具定义的上下文成本：未知（需要连接服务器，本命令不连接）"))
    return {"findings": findings, "cost": cost_per_client(skills), "similar": similar, "notes": notes,
            "similarity": {"threshold": SIMILARITY_THRESHOLD, "enabled": include_description,
                           "basis": _("初始值 0.6，没有外部依据；只作提示")},
            "thresholds": {"description_chars_spec": SPEC_DESCRIPTION_CHARS, "description_chars_claude": CLAUDE_DESCRIPTION_CAP_CHARS,
                           "skill_md_lines_advisory": SPEC_SKILL_MD_LINES}}


# ---------------------------------------------------------------- 组合：清单、概览、详情

def tags_for(entries: list[dict], groups: list[dict], findings: list[dict]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {entry["ref"]: [] for entry in entries}
    size: dict[str, int] = {}
    for group in groups:
        for ref in group["members"]:
            size[ref] = max(size.get(ref, 0), len(group["members"]))
    for entry in entries:
        tags = result[entry["ref"]]
        if entry["evidence"] == "unknown":
            tags.append(_("未知"))
        if size.get(entry["ref"]):
            tags.append(_("重复 {0}").format(size[entry["ref"]]))
        if entry.get("has_inline_secret"):
            tags.append(_("含内联密钥值"))
    seen: set[tuple[str, str]] = set()  # 同一条目同一类发现只标一次（按代码去重，不比较译文）
    for finding in findings:
        if finding["code"] in TAG_LABEL and finding["ref"] in result and (finding["ref"], finding["code"]) not in seen:
            seen.add((finding["ref"], finding["code"]))
            result[finding["ref"]].append(_(TAG_LABEL[finding["code"]]))
    return result


def summarize(norm: dict, groups: list[dict], cost: list[dict]) -> dict:
    entries = norm["entries"]
    unknown_entries = sum(1 for entry in entries if entry["evidence"] == "unknown")
    return {"skills": sum(entry["kind"] == "skill" for entry in entries), "mcps": sum(entry["kind"] == "mcp" for entry in entries),
            "clients": norm["clients"], "per_client": cost,
            "duplicate_groups": {"total": len(groups), "mcp": sum(group["kind"] == "mcp" for group in groups),
                                 "skill": sum(group["kind"] == "skill" for group in groups)},
            "unknown": unknown_entries + norm["unknown_extra"],
            "unknown_detail": {"entries": unknown_entries, "unreadable_sources": norm["unknown_extra"]}}


def analyse(inventory: dict) -> dict:
    norm = normalize(inventory)
    groups = build_groups(norm["entries"])
    skills = [entry for entry in norm["entries"] if entry["kind"] == "skill"]
    findings = [finding for entry in skills for finding in skill_findings(entry)[0]]
    return {"norm": norm, "groups": groups, "findings": findings,
            "tags": tags_for(norm["entries"], groups, findings),
            "summary": summarize(norm, groups, cost_per_client(skills))}


SORT_KIND = {"skill": 0, "mcp": 1}


def listing(analysis: dict, *, kind: str | None = None, client: str | None = None, scope: str | None = None,
            group: str = "client") -> dict:
    entries = [entry for entry in analysis["norm"]["entries"]
               if (kind is None or entry["kind"] == kind) and (client is None or entry["client"] == client)
               and (scope is None or entry["scope"] == scope)]
    entries.sort(key=lambda entry: (entry["client"], SORT_KIND[entry["kind"]], entry["scope"], entry["store"] or "", entry["name"]))
    rows = [{**public(entry), "tags": analysis["tags"][entry["ref"]]} for entry in entries]
    if group == "dup":
        parts = []
        for item in analysis["groups"]:
            members = [row for row in rows if row["ref"] in item["members"]]
            if members and (kind is None or item["kind"] == kind):
                parts.append({"key": item["id"], "label": item["label"], "name": item["name"], "entries": members})
        return {"group_by": group, "groups": parts}
    field = "scope" if group == "scope" else "client"
    keys: dict[str, list[dict]] = {}
    for row in rows:
        keys.setdefault(row[field], []).append(row)
    return {"group_by": group, "groups": [{"key": key, "label": key, "entries": members} for key, members in keys.items()]}


def dups(analysis: dict, kind: str | None = None) -> dict:
    return {"groups": [public_group(group) for group in analysis["groups"] if kind is None or group["kind"] == kind]}


def find_entries(analysis: dict, token: str) -> list[dict]:
    entries = analysis["norm"]["entries"]
    for matches in (lambda entry: entry["ref"] == token,
                    lambda entry: entry["ref"].split(":", 1)[-1] == token,  # 省略类型前缀：name@client:scope
                    lambda entry: entry["name"] == token):
        found = [entry for entry in entries if matches(entry)]
        if found:
            return found
    return []


def show_entry(analysis: dict, entry: dict) -> dict:
    groups = [group["id"] for group in analysis["groups"] if entry["ref"] in group["members"]]
    findings = [finding for finding in analysis["findings"] if finding["ref"] == entry["ref"]]
    return {**public(entry), "tags": analysis["tags"][entry["ref"]], "groups": groups, "findings": findings}


# ---------------------------------------------------------------- 渲染（文字）

def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def fmt_bytes(value: int | None) -> str:
    if value is None:
        return _("未知")
    return f"{value} B" if value < 1024 else f"{value / 1024:.1f} KB"


def scope_text(scope: str) -> str:
    return _(SCOPE_LABEL[scope]) if scope in SCOPE_LABEL else scope


def evidence_text(evidence: str) -> str:
    return _("未知") if evidence == "unknown" else evidence


def badge(entry: dict) -> str:
    mark = BADGE.get(entry["evidence"], "?")
    return S.dim(mark) if entry["evidence"] == "unknown" else mark


def origin_value(entry: dict) -> str:
    origin = entry.get("origin") or {}
    if not origin.get("type") and not origin.get("id"):
        return _("未知")
    base = origin.get("id") or origin.get("type")
    kind = f"{origin['type']}:" if origin.get("type") and origin.get("id") else ""
    return kind + base + (f"@{origin['version']}" if origin.get("version") else "")


def origin_text(entry: dict) -> str:
    return _("来源 {0}").format(origin_value(entry))


def entry_columns(entry: dict) -> list[tuple[str, str]]:
    """(列名, 文字)；缩列时按 来源 → 体积 → desc 字节 的顺序先丢后面的列。"""
    if entry["kind"] == "skill":
        return [("size", fmt_bytes(entry.get("skill_md_bytes"))),
                ("desc", _("desc {0}").format(fmt_bytes(entry.get("description_bytes")))), ("origin", origin_text(entry))]
    return [("transport", entry.get("transport") or _("未知")),
            ("fingerprint", _("指纹 {0}…").format(entry["fingerprint8"]) if entry.get("fingerprint8") else _("指纹 未知"))]


def entry_line(entry: dict, name_width: int, *, show_client: bool = False, width: int | None = None) -> list[str]:
    """一个条目的一行（放不下时标签移到下一行）。条目名与作用域永不省略，也不在词内换行。"""
    width = width or term_width()
    scope = scope_text(entry["scope"])
    place = f"{entry['client']}:{scope}" if show_client else scope
    columns = entry_columns(entry)
    tags = " ".join(f"[{tag}]" for tag in entry.get("tags", []))
    head = f"  {badge(entry)} {S.bold(pad(entry['name'], name_width))}  {pad(place, 10)}"
    plain_head = display_width(f"  X {pad(entry['name'], name_width)}  {pad(place, 10)}")
    order = ["origin", "size", "desc"]  # 缩列顺序
    keep = [col for col in columns]
    while True:
        body = "  ".join(text for _name, text in keep)
        total = plain_head + 2 + display_width(body) + (2 + display_width(tags) if tags else 0)
        if total <= width:
            break
        drop = next((name for name in order if any(col[0] == name for col in keep)), None)
        if drop is None:
            break
        keep = [col for col in keep if col[0] != drop]
    body = "  ".join(text for _name, text in keep)
    line = f"{head}  {S.dim(body)}" if body else head
    if not tags:
        return [line]
    if plain_head + 2 + display_width(body) + 2 + display_width(tags) <= width:
        return [f"{line}  {S.yellow(tags)}"]
    return [line, f"      {S.yellow(tags)}"]


def render_summary(summary: dict) -> list[str]:
    clients = " / ".join(f"{name}{_('(只读)') if _is_desktop(name) else ''}" for name in summary["clients"]) or _("无")
    lines = [_("技能 {0} 个 · MCP {1} 个 · 客户端 {2}").format(summary["skills"], summary["mcps"], clients)]
    for row in summary["per_client"]:
        if not row["counted"] and not row["uncounted"]:
            continue
        evidence = _("、").join(evidence_text(item) for item in row["evidence"]) or _("未知")
        extra = _("；另有 {0} 个未计入").format(row["uncounted"]) if row["uncounted"] else ""
        if not row["counted"]:
            lines.append(_("每会话 description 常驻：{0} 未知（没有可计入的条目）{1}").format(row["client"], extra))
            continue
        lines.append(_("每会话 description 常驻：{0} ≈ {1}（证据：{2}）{3}").format(
            row["client"], fmt_bytes(row["resident_bytes"]), evidence, extra))
    dup = summary["duplicate_groups"]
    lines.append(_("重复 {0} 组（MCP {1} · 技能 {2}）  未知 {3} 项（证据不足，不做结论）").format(
        dup["total"], dup["mcp"], dup["skill"], summary["unknown"]))
    return lines


def render_unavailable(reason: str) -> list[str]:
    return [S.yellow(_("不可用：{0}").format(reason)),
            S.dim(_("需要 agent-pack 提供 agenthelm inventory；更新 agent-pack 或运行时后再试。其他功能不受影响。"))]


def render_list(data: dict, summary: dict) -> list[str]:
    lines = render_summary(summary)
    if not data["groups"]:
        return lines + [S.dim(_("没有匹配的条目。"))]
    show_client = data["group_by"] != "client"
    name_width = min(40, max(display_width(row["name"]) for part in data["groups"] for row in part["entries"]))
    for part in data["groups"]:
        evidences = {row["evidence"] for row in part["entries"]}
        label = _("{0} · 证据 {1}").format(part["label"], _("、").join(evidence_text(item) for item in sorted(evidences, key=EVIDENCE_LEVELS.index)))
        lines.append(S.bold(label))
        previous = None
        for row in part["entries"]:
            section = (row["kind"], row["scope"], row["store"])
            if data["group_by"] == "client" and section != previous:
                previous = section
                count = sum((r["kind"], r["scope"], r["store"]) == section for r in part["entries"])
                lines.append(S.dim(_("  {0} · {1} · {2}  {3} 个").format(
                    _(KIND_LABEL[row["kind"]]), scope_text(row["scope"]), row["store"] or _("未知"), count)))
            lines += entry_line(row, name_width, show_client=show_client)
    lines.append(S.dim(_("证据：● file（读到文件）· ⚙ setting · ⌘ cli · ? 未知（不做结论）")))
    return lines


def render_dups(data: dict) -> tuple[list[str], list[dict]]:
    lines: list[str] = []
    numbered: list[dict] = []
    for kind, title in (("mcp", _("重复 MCP（{0} 组）")), ("skill", _("重复技能（{0} 组）"))):
        members = [group for group in data["groups"] if group["kind"] == kind]
        if not members:
            continue
        lines.append(S.bold(title.format(len(members))))
        for group in members:
            numbered.append(group)
            lines += render_group(len(numbered), group)
    if not numbered:
        lines.append(S.green(_("没有发现重复。")))
    return lines, numbered


def render_group(number: int, group: dict) -> list[str]:
    head = f" {S.bold(S.cyan(f'{number:>2}'))}  {S.bold(group['name'])}  {_(group['label'])}"
    if group["cross_client"]:
        head += S.dim(_("（跨客户端）"))
    lines = [head + S.dim(_("  分组 {0}").format(group["id"]))]
    for ref in group["members"]:
        lines.append(f"      {ref}")
    for text in group["effective"]["lines"]:
        lines.append("      " + (S.yellow(text) if not group["effective"]["known"] else text))
    if group["cross_client"] and group["class"] != "same-name-unknown":
        lines.append(S.dim(_("      跨客户端重复：各客户端加载各自的配置，可保留，不推荐退役")))
    return lines


def render_show(detail: dict) -> list[str]:
    def row(label: str, value: Any) -> str:
        text = _("未知") if value is None else (_("是") if value is True else _("否") if value is False else str(value))
        return f"  {pad(label, 18)}{text}"

    lines = [f"{S.bold(detail['name'])}  {S.dim(detail['ref'])}",
             row(_("类型"), _(KIND_LABEL[detail["kind"]])), row(_("客户端"), detail["client"]),
             row(_("作用域"), scope_text(detail["scope"])), row(_("存储"), detail["store"]),
             row(_("证据"), evidence_text(detail["evidence"]))]
    if detail["kind"] == "skill":
        origin = detail["origin"]
        lines += [row(_("SKILL.md 体积"), fmt_bytes(detail["skill_md_bytes"])),
                  row(_("SKILL.md 行数"), detail["skill_md_lines"]),
                  row(_("description 字节"), fmt_bytes(detail["description_bytes"])),
                  row(_("description 字符"), detail["description_chars"]),
                  row(_("目录体积 / 文件数"), f"{fmt_bytes(detail['dir_bytes'])} / {detail['file_count'] if detail['file_count'] is not None else _('未知')}"),
                  row(_("内容摘要（前 8 位）"), detail["content_sha8"]),
                  row(_("符号链接"), detail["is_symlink"]),
                  row(_("来源"), origin_value(detail)),
                  row(_("最新已知版本"), origin["latest_known"]), row(_("是否加载"), detail["loaded"])]
    else:
        lines += [row(_("传输"), detail["transport"]), row(_("指纹（前 8 位）"), detail["fingerprint8"]),
                  row(_("环境变量名"), _("、").join(detail["env_var_names"]) if detail["env_var_names"] else
                      (_("无") if detail["env_var_names"] == [] else None)),
                  row(_("含内联密钥值"), detail["has_inline_secret"]), row(_("远程主机"), detail["url_host"]),
                  S.dim(_("  不显示命令行参数、url 全文、环境变量值与 headers。"))]
    if detail.get("referenced_by"):
        lines.append(row(_("被引用"), _("被 {0} 条规则引用").format(len(detail["referenced_by"]))))
        lines += [f"      {item['rule_id']}" + (f"  {item['source']}" if item["source"] else "") for item in detail["referenced_by"]]
    if detail["tags"]:
        lines.append(row(_("机械发现"), " ".join(f"[{tag}]" for tag in detail["tags"])))
    for finding in detail["findings"]:
        lines.append(f"      {finding['fact']}" + (S.dim(_("（阈值：{0}；来源：{1}）").format(finding["threshold"], finding["source"]))
                                                     if finding.get("threshold") and finding.get("source") else ""))
    return lines


def render_check(data: dict) -> list[str]:
    lines = [S.bold(_("技能体检：只列机械事实与阈值；不打分、不排名、不建议删除"))]
    by_ref: dict[str, list[dict]] = {}
    for finding in data["findings"]:
        by_ref.setdefault(finding["ref"], []).append(finding)
    if by_ref:
        for ref, items in sorted(by_ref.items()):
            lines.append(f"  {S.bold(ref)}")
            for item in items:
                extra = S.dim(_("（阈值：{0}；来源：{1}）").format(item["threshold"], item["source"])) if item.get("threshold") and item.get("source") else ""
                lines.append(f"      · {item['fact']}{extra}")
    else:
        lines.append(S.green(_("没有机械发现。")))
    lines.append(S.bold(_("每会话 description 加载成本")))
    for row in data["cost"]:
        evidence = _("、").join(evidence_text(item) for item in row["evidence"]) or _("未知")
        if not row["counted"]:
            lines.append(_("  {0}：未知（没有可计入的条目；{1} 个未计入）").format(row["client"], row["uncounted"]))
            continue
        lines.append(_("  {0}：{1}（{2} 个已计入，{3} 个未计入；证据 {4}）").format(
            row["client"], fmt_bytes(row["resident_bytes"]), row["counted"], row["uncounted"], evidence))
        estimate = row["token_estimate"]
        lines.append(S.dim(_("      粗估 {0}–{1} token（{2}）").format(estimate["low"], estimate["high"], estimate["label"])))
        if row["capped"]:
            lines.append(S.dim(_("      其中 {0} 个按 Claude 的 {1} 字符截断上限折算").format(row["capped"], CLAUDE_DESCRIPTION_CAP_CHARS)))
    if not data["cost"]:
        lines.append(S.dim(_("  没有技能条目。")))
    if data["similarity"]["enabled"]:
        lines.append(S.bold(_("description 相似（机械判断，不代表功能重复；阈值 {0}，没有外部依据）").format(data["similarity"]["threshold"])))
        lines += [f"  {pair['refs'][0]}  ≈  {pair['refs'][1]}" for pair in data["similar"]] or [S.dim(_("  没有达到阈值的技能对。"))]
    lines += [S.dim(f"  {note}") for note in data["notes"]]
    return lines


# ---------------------------------------------------------------- provider 共用方法（真实与演示项目各自提供 _ext_load）

class ExtensionsMixin:
    """``extensions_*`` 方法：一次取数、会话内缓存、只读。子类实现 ``_ext_load(include_description)`` 返回 {state, reason, inventory}。"""

    def _ext_load(self, include_description: bool) -> dict:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def extensions_refresh(self) -> None:
        self.__dict__["_ext_cache"] = {}

    def _extensions(self, include_description: bool = False) -> dict:
        cache = self.__dict__.setdefault("_ext_cache", {})
        key = True if True in cache else bool(include_description)
        if key not in cache:
            result = self._ext_load(key)
            cache[key] = {**result, "analysis": analyse(result["inventory"])} if result["state"] == "available" else result
        return cache[key]

    def extensions_state(self) -> dict:
        result = self._extensions()
        return {"state": result["state"], "reason": result["reason"]}

    def extensions_summary(self) -> dict:
        result = self._extensions()
        return {**self.extensions_state(), **({"summary": result["analysis"]["summary"]} if result["state"] == "available" else {})}

    def extensions_list(self, kind: str | None = None, client: str | None = None, scope: str | None = None,
                        group: str = "client") -> dict:
        result = self._extensions()
        if result["state"] != "available":
            return self.extensions_state()
        analysis = result["analysis"]
        return {**self.extensions_state(), **listing(analysis, kind=kind, client=client, scope=scope, group=group),
                "filters": {"kind": kind, "client": client, "scope": scope}, "summary": analysis["summary"],
                "notes": [_("只含本项目与本机的已知存储；没在本机同步过的项目不可见")]}

    def extensions_dups(self, kind: str | None = None) -> dict:
        result = self._extensions()
        if result["state"] != "available":
            return self.extensions_state()
        analysis = result["analysis"]
        return {**self.extensions_state(), **dups(analysis, kind), "summary": analysis["summary"],
                "notes": [_("只含本项目与本机的已知存储；没在本机同步过的项目不可见")]}

    def extensions_show(self, token: str) -> dict:
        from project_base import EntryError  # noqa: PLC0415

        result = self._extensions()
        if result["state"] != "available":
            return self.extensions_state()
        analysis = result["analysis"]
        for group in analysis["groups"]:
            if group["id"] == token:
                return {**self.extensions_state(), "view": "group", "group": public_group(group)}
        found = find_entries(analysis, token)
        if not found:
            raise EntryError(_("没有这个条目：{0}").format(token), "not-found",
                             [_("用 extensions list 查看全部条目的引用（ref）")])
        if len(found) > 1:
            raise EntryError(_("“{0}”对应多个条目，请用完整引用").format(token), "ambiguous",
                             [entry["ref"] for entry in found])
        return {**self.extensions_state(), "view": "entry", "entry": show_entry(analysis, found[0])}

    def extensions_check(self, include_description: bool = False) -> dict:
        result = self._extensions(include_description)
        if result["state"] != "available":
            return self.extensions_state()
        return {**self.extensions_state(), **check(result["analysis"]["norm"], include_description)}
