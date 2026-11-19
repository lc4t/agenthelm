#!/usr/bin/env python3
"""``agenthelm dedupe``: handle one duplicate group reported by ``agenthelm inventory`` (preview by default).

Dispositions:
* ``--keep <ref> --retire-others``: Skill copies are archived (a move into the recovery directory, never a delete);
  MCP copies are *never* edited here - the plan only prints the official manual command and a backup reminder.
* ``--keep-all --note "..."`` / ``--ignore`` / ``--unignore``: write only the machine-level decision file
  (``~/.config/agenthelm/ext-decisions.json``, mode 0600, no values); no configuration is changed.
* ``--desktop-snippet``: read-only paste-ready ``claude_desktop_config.json`` fragment for the kept MCP entry.

A copy whose evidence is ``unknown`` is read-only: it can never be retired. Group ids are stable
(``kind|client|name-or-fingerprint``), so they do not change when other entries appear.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))
import agent_pack_desktop as desktop  # noqa: E402
import agent_pack_ext_common as ext  # noqa: E402
import agent_pack_inventory as inv  # noqa: E402
from i18n_agent_pack import _, N_  # noqa: E402

DECISIONS_SCHEMA = 1
NOTE_LIMIT = 200
SECRET_LIKE = re.compile(r"(sk-[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|xox[abp]-[A-Za-z0-9-]{8,}|Bearer\s+\S{8,}"
                         r"|[A-Za-z0-9+/_-]{32,}|://[^/\s:@]+:[^/\s@]+@)")


# Manual-removal commands per client, as data (no per-client branches in the code): a client without an entry only
# gets an instruction to delete the entry in its file by hand. ``{scope}`` and ``{name}`` are filled in.
MANUAL_REMOVE = {"claude": "claude mcp remove --scope {scope} {name}"}
MANUAL_BACKUP = {"claude": "cp ~/.claude.json ~/.claude.json.bak"}


def decisions_path(home: Path) -> Path:
    return home / ".config" / "agenthelm" / "ext-decisions.json"


# ------------------------------------------------------------------ groups

def group_id(kind: str, client: str, key: str) -> str:
    return "g-" + hashlib.sha256(f"{kind}|{client}|{key}".encode("utf-8")).hexdigest()[:10]


def list_groups(report: dict) -> list[dict]:
    """Every duplicate group of the inventory as ``{id, kind, group_kind, client, key, members[], relation, ...}``."""
    groups = []
    for dup in report["duplicates"]["skills"]:
        members = [item for item in report["skills"] if item["client"] == dup["client"] and item["name"] == dup["name"]
                   and item["path"] in dup["stores"]]
        groups.append({"id": group_id("skill", dup["client"], dup["name"]), "kind": "skill", "group_kind": "same-name",
                       "client": dup["client"], "key": dup["name"], "relation": dup["relation"],
                       "effective": dup.get("effective"), "members": members})
    for dup in report["duplicates"]["mcps"]:
        if dup["kind"] == "same-name":
            members = [item for item in report["mcps"] if item["client"] == dup["client"] and item["name"] == dup["names"][0]
                       and item["store"] in dup["stores"]]
            key = dup["names"][0]
        else:
            members = [item for item in report["mcps"] if item["client"] == dup["client"]
                       and item.get("fingerprint") == dup.get("fingerprint")]
            key = dup["fingerprint"]
        groups.append({"id": group_id("mcp", dup["client"], key), "kind": "mcp", "group_kind": dup["kind"],
                       "client": dup["client"], "key": key if dup["kind"] == "same-name" else "|".join(dup["names"]),
                       "relation": dup["relation"], "effective": dup.get("effective"), "members": members})
    return groups


def members_digest(group: dict) -> str:
    return hashlib.sha256(ext.canonical(sorted(item["ref"] for item in group["members"])).encode("utf-8")).hexdigest()[:16]


def read_decisions(home: Path) -> dict:
    path = decisions_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": DECISIONS_SCHEMA, "decisions": {}}
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), dict):
        return {"schema_version": DECISIONS_SCHEMA, "decisions": {}}
    return data


def _summary(group: dict, decisions: dict) -> dict:
    decision = decisions["decisions"].get(group["id"])
    state = None
    if decision:
        state = {"decision": decision.get("decision"), "note": decision.get("note", ""), "at": decision.get("at"),
                 "stale": decision.get("members_digest") != members_digest(group)}
    return {"id": group["id"], "kind": group["kind"], "group_kind": group["group_kind"], "client": group["client"],
            "key": group["key"], "relation": group["relation"],
            "members": [{"ref": item["ref"], "path": item.get("path") or item.get("store"), "evidence": item["evidence"],
                         "precedence": item["precedence"]} for item in group["members"]],
            "decision": state}


def cross_client_hint(report: dict, group: dict) -> list[dict]:
    """D-CB4: the same MCP fingerprint in several clients is intentional by default and is never recommended for retirement."""
    if group["kind"] != "mcp":
        return []
    prints = {item["fingerprint"] for item in group["members"] if item.get("fingerprint")}
    others = sorted({item["ref"] for item in report["mcps"] if item["client"] != group["client"]
                     and item.get("fingerprint") in prints})
    return [ext.warning("cross-client-keep-recommended",
                        _("其他客户端里有同指纹的条目：跨客户端的相同 MCP 不冲突，建议保留，不推荐退役"), refs=others)] if others else []


# ------------------------------------------------------------------ plan

def build_plan(home: Path, project: Path, group: str, keep: str | None, retire_others: bool, keep_all: bool,
               ignore: bool, unignore: bool, note: str | None, recovery_override: Path | None = None) -> tuple[dict, dict]:
    home = home.expanduser().resolve()
    project = project.expanduser().resolve()
    report = inv.observe(home, project, None, "all", "all")
    decisions = read_decisions(home)
    plan: dict[str, Any] = {
        "group": group, "kind": None, "disposition": None, "status": "blocked", "members": [], "steps": [],
        "blocked": [], "warnings": [], "manual": [], "keep": keep, "retire": [],
        "recovery_base": ext.display(ext.recovery_base(home, recovery_override), home, project),
    }
    ctx: dict[str, Any] = {"home": home, "project": project, "recovery_override": recovery_override, "report": report}
    blocks = plan["blocked"]
    chosen = [flag for flag, on in (("retire-others", retire_others), ("keep-all", keep_all), ("ignore", ignore),
                                    ("unignore", unignore)) if on]
    if len(chosen) != 1:
        blocks.append(ext.blocked("choose-one-disposition", _("必须且只能选一个：--keep <ref> --retire-others、--keep-all、--ignore、--unignore")))
        return _finish(plan), ctx
    plan["disposition"] = chosen[0]
    found = next((item for item in list_groups(report) if item["id"] == group), None)
    if found is None:
        blocks.append(ext.blocked("group-not-found", _("没有这个重复分组；先用 --list 查看"),
                                  candidates=[item["id"] for item in list_groups(report)]))
        return _finish(plan), ctx
    plan["kind"] = found["kind"]
    plan["members"] = _summary(found, decisions)["members"]
    plan["members_digest"] = members_digest(found)
    plan["existing_decision"] = (decisions["decisions"].get(group) or {}).get("decision")
    plan["warnings"].extend(cross_client_hint(report, found))
    ctx["group"] = found

    if chosen[0] in ("keep-all", "ignore", "unignore"):
        _plan_decision(plan, ctx, found, chosen[0], note, decisions)
    else:
        _plan_retire(plan, ctx, found, keep, home, project)
    plan["status"] = "blocked" if blocks else ("manual-only" if plan["manual"] and not plan["retire"] else "ready")
    return _finish(plan), ctx


def _plan_decision(plan: dict, ctx: dict, group: dict, which: str, note: str | None, decisions: dict) -> None:
    blocks = plan["blocked"]
    if which == "keep-all":
        if not note or not note.strip():
            blocks.append(ext.blocked("note-required", _("--keep-all 需要 --note 写明保留理由")))
        elif len(note) > NOTE_LIMIT:
            blocks.append(ext.blocked("note-too-long", _("备注最长 {0} 个字符").format(NOTE_LIMIT)))
        elif SECRET_LIKE.search(note):
            blocks.append(ext.blocked("note-looks-like-secret", _("备注疑似含有令牌或口令，不写入；决定文件不保存任何凭据")))
    if which == "unignore" and plan["existing_decision"] != "ignore":
        blocks.append(ext.blocked("not-ignored", _("这个分组当前不是 ignore 状态")))
    if blocks:
        return
    decision = {"keep-all": "keep-all", "ignore": "ignore", "unignore": None}[which]
    plan["decision"] = {"decision": decision, "note": (note or "").strip() if which == "keep-all" else "",
                        "members_digest": plan["members_digest"]}
    plan["steps"].append(ext.step("s1", "write-decision" if decision else "remove-decision", "certain",
                                  to="~/.config/agenthelm/ext-decisions.json"))
    plan["warnings"].append(ext.warning("config-untouched", _("只写决定文件，不改任何客户端配置")))
    ctx["decisions"] = decisions


def _plan_retire(plan: dict, ctx: dict, group: dict, keep: str | None, home: Path, project: Path) -> None:
    blocks = plan["blocked"]
    members = group["members"]
    kept = next((item for item in members if item["ref"] == keep), None)
    if keep is None:
        blocks.append(ext.blocked("keep-required", _("--retire-others 需要 --keep <ref>"), candidates=[m["ref"] for m in members]))
        return
    if kept is None:
        blocks.append(ext.blocked("keep-not-in-group", _("--keep 的引用不在这个分组里"), candidates=[m["ref"] for m in members]))
        return
    others = [item for item in members if item is not kept]
    for item in members:
        if item["evidence"] != "file":
            blocks.append(ext.blocked("evidence-unknown", _("{0} 没有文件级证据：只读，不能退役").format(item["ref"]), ref=item["ref"]))
    if blocks:
        return
    if group["kind"] == "mcp":
        _plan_mcp_manual(plan, group, kept, others, home)
        return
    kept_real = os.path.realpath(_entry_path(kept, home, project))
    for item in others:
        path = _entry_path(item, home, project)
        real = os.path.realpath(path)
        if not path.is_symlink() and (kept_real == real or kept_real.startswith(real + os.sep)):
            blocks.append(ext.blocked("keep-depends-on-retired", _("保留的条目依赖要退役的目录：{0}").format(item["ref"]), ref=item["ref"]))
    if blocks:
        return
    if group["relation"] == "different":
        plan["warnings"].append(ext.warning("content-differs", _("各副本内容不同：退役的副本只是归档，之后可手工移回")))
    steps = plan["steps"]
    steps.append(ext.step("s1", "create-recovery-dir", "certain", to=plan["recovery_base"]))
    several = len(others) > 1
    for index, item in enumerate(others, start=1):
        bucket_name = item["name"] if not several else f"{item['name']}@" + re.sub(r"[^A-Za-z0-9]+", "-", item["store"]).strip("-")
        plan["retire"].append({"ref": item["ref"], "path": item["path"], "bucket_name": bucket_name,
                               "content_sha256": item["content_sha256"], "is_symlink": item["is_symlink"]})
        steps.append(ext.step(f"s{index + 1}", "archive-copy", "certain", **{"from": item["path"], "bucket_name": bucket_name}))
        if item["origin"]["type"] in ("plugin", "marketplace"):
            plan["warnings"].append(ext.warning("managed-origin", _("{0} 由插件管理：退役后下次同步可能又会出现").format(item["ref"]), ref=item["ref"]))
    steps.append(ext.step(f"s{len(others) + 2}", "confirm-in-client", "best-effort", client=group["client"]))
    ctx["kept_path"] = kept_real


def _plan_mcp_manual(plan: dict, group: dict, kept: dict, others: list[dict], home: Path) -> None:
    """MCP copies are never edited: only the official manual command, flagged as not archivable by this tool."""
    for item in others:
        entry: dict[str, Any] = {"ref": item["ref"], "client": item["client"], "scope": item["scope"], "store": item["store"],
                                 "name": item["name"], "not_archivable_by_tool": True, "promise": "cannot-promise"}
        if item["client"] in MANUAL_REMOVE:
            entry["command"] = MANUAL_REMOVE[item["client"]].format(scope=item["scope"], name=shlex.quote(item["name"]))
            entry["backup"] = MANUAL_BACKUP[item["client"]]
        else:
            entry["instruction"] = _("手工删除 {0} 里名为 {1} 的 MCP 条目（先自己备份该文件）").format(item["store"], item["name"])
        plan["manual"].append(entry)
        plan["steps"].append(ext.step(f"s{len(plan['steps']) + 1}", "manual-remove-mcp", "cannot-promise", ref=item["ref"]))
    plan["warnings"].append(ext.warning("mcp-not-archivable", _("MCP 副本不可由工具归档：配置文件可能含本地凭据，本工具不编辑它，只给手工命令")))
    if group["client"] in MANUAL_BACKUP:
        plan["warnings"].append(ext.warning("backup-first", _("先自己备份 ~/.claude.json，再执行下面的命令")))
    else:
        plan["warnings"].append(ext.warning("backup-first", _("先自己备份相关配置文件，再手工修改")))
    if (group.get("effective") or {}).get("evidence") != "file":
        plan["warnings"].append(ext.warning("precedence-unknown", _("哪一份生效官方文档没有给出（或需要读设置），请自行确认后再执行")))


def _entry_path(item: dict, home: Path, project: Path) -> Path:
    root = inv.resolve_store(item["store"], home, project)
    return root / item["name"]


def _finish(plan: dict) -> dict:
    plan["plan_digest"] = ext.plan_digest(plan)
    return plan


# ------------------------------------------------------------------ apply

def apply_plan(plan: dict, ctx: dict) -> dict:
    home, project = ctx["home"], ctx["project"]
    if plan["disposition"] in ("keep-all", "ignore", "unignore"):
        return _apply_decision(plan, ctx)
    if not plan["retire"]:
        return {"status": "manual-only", "written": False, "manual": plan["manual"]}
    moved: list[tuple[Path, Path]] = []
    recovery: Path | None = None
    try:
        recovery = ext.new_recovery_dir(ext.recovery_base(home, ctx["recovery_override"]), f"dedupe-{plan['group']}")
        ext.write_private_json(recovery / "dedupe.json", {
            "schema_version": ext.SCHEMA_VERSION, "group": plan["group"], "keep": plan["keep"], "plan_digest": plan["plan_digest"],
            "retired": [{"ref": item["ref"], "path": item["path"], "archived_as": item["bucket_name"]} for item in plan["retire"]]})
        for item in plan["retire"]:
            report_item = next(entry for entry in ctx["group"]["members"] if entry["ref"] == item["ref"])
            path = _entry_path(report_item, home, project)
            moved.append((ext.archive(path, recovery, "skills", item["bucket_name"]), path))
        if not _kept_still_intact(ctx):
            raise ext.ExtError("the kept copy changed while retiring")
    except Exception as exc:  # noqa: BLE001
        ext.rollback_moves(moved)
        return {"status": "rolled-back", "written": False, "reason": type(exc).__name__,
                "message": _("失败，已回滚：{0}").format(str(exc)),
                "recovery_dir": ext.display(recovery, home, project) if recovery else None}
    return {"status": "applied", "written": True, "recovery_dir": ext.display(recovery, home, project),
            "archived": [ext.display(a, home, project) for a, _o in moved],
            "restore": [ext.restore_line(a, o, home) for a, o in moved],
            "restore_note": _("恢复：把归档目录移回原位（本版没有 restore 命令）")}


def _kept_still_intact(ctx: dict) -> bool:
    return Path(ctx["kept_path"]).is_dir() and (Path(ctx["kept_path"]) / "SKILL.md").is_file()


def _apply_decision(plan: dict, ctx: dict) -> dict:
    home = ctx["home"]
    path = decisions_path(home)
    data = read_decisions(home)
    decision = plan["decision"]
    try:
        if decision["decision"] is None:
            data["decisions"].pop(plan["group"], None)
        else:
            group = ctx["group"]
            data["decisions"][plan["group"]] = {
                "decision": decision["decision"], "note": decision["note"], "kind": group["kind"], "client": group["client"],
                "key": group["key"], "members_digest": decision["members_digest"],
                "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
        data["schema_version"] = DECISIONS_SCHEMA
        ext.write_private_json(path, data)
    except OSError as exc:
        return {"status": "failed", "written": False, "message": _("失败：{0}").format(type(exc).__name__)}
    return {"status": "applied", "written": True, "decision_file": "~/.config/agenthelm/ext-decisions.json",
            "decision": decision["decision"]}


# ------------------------------------------------------------------ text

ACTION_TEXT = {
    "create-recovery-dir": N_("在 {to} 下建恢复目录（权限 0700）"),
    "archive-copy": N_("归档 {from} 到恢复目录（归档不是删除）"),
    "confirm-in-client": N_("下一步：重启 {client} 或重新进入项目，确认结果（工具不执行客户端命令）"),
    "manual-remove-mcp": N_("{ref}：不可由工具归档，见下面的手工命令"),
    "write-decision": N_("写入决定文件 {to}（权限 0600，不含任何值）"),
    "remove-decision": N_("从决定文件 {to} 移除这个分组的决定"),
}


def render_list(groups: list[dict], decisions: dict) -> str:
    if not groups:
        return _("没有发现重复分组。") + "\n"
    lines = [_("重复分组（用 id 调用 dedupe）：")]
    for group in groups:
        info = _summary(group, decisions)
        decision = ""
        if info["decision"]:
            decision = "  " + _("[已决定：{0}{1}]").format(info["decision"]["decision"], _("，过期") if info["decision"]["stale"] else "")
        lines.append(f"- {group['id']}  {group['kind']}/{group['group_kind']}  {group['client']}  {group['key']}  ({group['relation']}){decision}")
        for member in info["members"]:
            lines.append(f"    {member['ref']}  {member['path']}  [{member['evidence']}]")
    return "\n".join(lines) + "\n"


def render_text(plan: dict, applied: dict | None, command: str) -> str:
    lines = [_("agenthelm dedupe：预览（未写入任何文件）") if applied is None else _("agenthelm dedupe：已执行")]
    lines.append(_("分组：{0}（{1}）").format(plan["group"], plan["kind"] or "-"))
    for member in plan["members"]:
        lines.append(f"  {member['ref']}  {member['path']}  [{member['evidence']}]")
    for entry in plan["steps"]:
        lines.append(f"  {entry['id']} [{entry['promise']}] " + _(ACTION_TEXT[entry["action"]]).format(**entry))
    for entry in plan["manual"]:
        lines.append(_("手工（不可由工具归档）：{0}").format(entry.get("command") or entry.get("instruction")))
        if entry.get("backup"):
            lines.append(_("  先备份：{0}").format(entry["backup"]))
    for warning in plan["warnings"]:
        lines.append(_("注意：{0}").format(warning["message"]))
    for entry in plan["blocked"]:
        lines.append(_("已阻止 [{0}]：{1}").format(entry["code"], entry["message"]))
        for candidate in entry.get("candidates", []):
            lines.append(f"  - {candidate}")
    if applied is None:
        lines.append(_("计划指纹：{0}").format(plan["plan_digest"]))
        if plan["status"] == "ready":
            lines.append(_("确认后执行：{0}").format(command))
    else:
        lines.append(_("结果：{0}").format(applied["status"]))
        if applied.get("message"):
            lines.append(applied["message"])
        if applied.get("recovery_dir"):
            lines.append(_("恢复目录：{0}").format(applied["recovery_dir"]))
        for line in applied.get("restore", []):
            lines.append(_("手工恢复：{0}").format(line))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ CLI

def run(args: Any, home: Path) -> int:
    """Exit codes: 0 ok / preview without blockers, 1 blockers or failure, 2 usage."""
    if args.lang and args.lang != "auto":
        os.environ["AGENTHELM_LANG"] = args.lang
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        sys.stderr.write(_("只支持 macOS 与 Linux\n"))
        return 2
    if args.apply and not args.expect_plan:
        sys.stderr.write(_("--apply 必须带 --expect-plan <计划指纹>（先预览取得）\n"))
        return 2
    project = (args.project or Path.cwd()).resolve()
    home = home.expanduser().resolve()
    if args.list or args.group is None:
        report = inv.observe(home, project, None, "all", "all")
        groups = list_groups(report)
        decisions = read_decisions(home)
        if args.output == "json":
            print(json.dumps({"schema_version": ext.SCHEMA_VERSION, "command": "dedupe", "mode": "list",
                              "groups": [_summary(group, decisions) for group in groups]}, ensure_ascii=False, indent=2))
        else:
            sys.stdout.write(render_list(groups, decisions))
        return 0
    if args.desktop_snippet:
        return _run_snippet(args, home, project)
    plan, ctx = build_plan(home, project, args.group, args.keep, args.retire_others, args.keep_all, args.ignore,
                           args.unignore, args.note, getattr(args, "recovery_root", None))
    command = _command_text(args, plan)
    plan["next"] = {"apply": command}
    applied = None
    code = 1 if plan["blocked"] else 0
    if args.apply:
        if plan["plan_digest"] != args.expect_plan:
            applied = {"status": "refused", "reason": "plan-digest-mismatch", "written": False,
                       "message": _("计划指纹与预览不一致（源、目标或参数已变化），未写入；请重新预览。当前指纹：{0}").format(plan["plan_digest"])}
            code = 1
        elif plan["blocked"]:
            applied = {"status": "refused", "reason": "blocked", "written": False}
        else:
            applied = apply_plan(plan, ctx)
            code = 0 if applied["status"] in ("applied", "manual-only") else 1
    if args.output == "json":
        print(json.dumps({"schema_version": ext.SCHEMA_VERSION, "command": "dedupe",
                          "mode": "apply" if args.apply else "preview", "plan": plan, "applied": applied},
                         ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(render_text(plan, applied, command))
    return code


def _run_snippet(args: Any, home: Path, project: Path) -> int:
    report = inv.observe(home, project, None, "mcp", "all")
    group = next((item for item in list_groups(report) if item["id"] == args.group), None)
    entry = None
    if group is not None and args.keep:
        entry = next((item for item in group["members"] if item["ref"] == args.keep), None)
    if entry is None:
        document = {"ref": args.keep, "path": desktop.config_path(), "snippet": None, "notes": [], "promise": "cannot-promise",
                    "blocked": [ext.blocked("keep-required", _("--desktop-snippet 需要有效的分组 id 和 --keep <mcp 引用>"),
                                            candidates=[m["ref"] for m in group["members"]] if group else [])]}
    else:
        document = desktop.build(entry)
    if args.output == "json":
        print(json.dumps({"schema_version": ext.SCHEMA_VERSION, "command": "dedupe", "mode": "desktop-snippet",
                          "desktop": document}, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(desktop.render_text(document))
    return 1 if document["blocked"] else 0


def _command_text(args: Any, plan: dict) -> str:
    parts = [args.group]
    if args.keep:
        parts += ["--keep", shlex.quote(args.keep)]
    for flag, on in (("--retire-others", args.retire_others), ("--keep-all", args.keep_all), ("--ignore", args.ignore),
                     ("--unignore", args.unignore)):
        if on:
            parts.append(flag)
    if args.note:
        parts += ["--note", shlex.quote(args.note)]
    if args.project:
        parts += ["--project", shlex.quote(str(args.project))]
    return f"agenthelm dedupe {' '.join(parts)} --apply --expect-plan {plan['plan_digest']}"
