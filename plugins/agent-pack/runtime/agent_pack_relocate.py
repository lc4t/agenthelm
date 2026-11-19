#!/usr/bin/env python3
"""``agenthelm relocate``: move or copy one Skill to another client / scope (preview by default, ``--apply`` writes).

Flow: plan (pure, reads only) -> ``plan_digest`` -> apply (recomputes the plan and refuses on any difference).
Write order: recovery directory -> copy beside the target under a temporary name -> read back (relative paths and
content digests, ``SKILL.md`` included) -> rename into place -> archive the source (a move, never a delete).
Any failure undoes only what this run created and puts archived sources back; an existing target is never touched.

Only Skills are handled here; MCP entries are refused (their writes belong to a later change). Plans and outputs
never contain file contents, only names, paths and digests.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))
import agent_pack_ext_common as ext  # noqa: E402
import agent_pack_inventory as inv  # noqa: E402
from i18n_agent_pack import _, N_  # noqa: E402

CLIENTS = ("claude", "codex", "cursor")
# Where a Skill for ``client:scope`` is written. ``codex:user`` / ``cursor:project`` use the shared ``.agents/skills``
# (decision D-CB11); a Skill already sitting in a store that client reads natively is a zero-write ``native`` plan.
TARGET_STORE = {
    ("claude", "user"): "~/.claude/skills", ("claude", "project"): f"{inv.PROJECT}/.claude/skills",
    ("codex", "user"): "~/.agents/skills", ("codex", "project"): f"{inv.PROJECT}/.agents/skills",
    ("cursor", "user"): "~/.cursor/skills", ("cursor", "project"): f"{inv.PROJECT}/.agents/skills",
}
WIDEN_WARNING = N_("放宽：这个 Skill 之后在所有项目里可见")
RANK = {"project": 0, "user": 1}  # scope breadth: moving to a higher rank widens


def parse_ref(ref: str) -> tuple[str, str, str, str] | None:
    """``skill:<name>@<client>:<scope>[#agents]`` -> (kind, name, client, scope); ``None`` when malformed."""
    base = ref.split("#", 1)[0]
    kind, sep, rest = base.partition(":")
    name, at, where = rest.rpartition("@")
    client, colon, scope = where.partition(":")
    if not (sep and at and colon and kind and name and client and scope):
        return None
    return kind, name, client, scope


def parse_target(text: str) -> tuple[str, str] | None:
    client, colon, scope = text.partition(":")
    if colon and client in CLIENTS and scope in RANK:
        return client, scope
    return None


def _candidates(report: dict, name: str) -> list[str]:
    return sorted(item["ref"] for item in report["skills"] if item["name"] == name)


def build_plan(home: Path, project: Path, ref: str, to: str, mode: str = "copy", allow_widen: bool = False,
               keep_source: bool = False, recovery_override: Path | None = None) -> tuple[dict, dict]:
    """The plan (JSON-safe) and a private context with absolute paths for apply. Reads only, never writes."""
    home = home.expanduser().resolve()
    project = project.expanduser().resolve()
    plan: dict[str, Any] = {
        "ref": ref, "kind": "skill", "source": None, "target": None, "mode": mode, "retire_source": False,
        "direction": None, "status": "blocked", "steps": [], "blocked": [], "warnings": [], "confirmations": [],
        "recovery_base": ext.display(ext.recovery_base(home, recovery_override), home, project),
    }
    ctx: dict[str, Any] = {}
    blocks = plan["blocked"]

    parsed = parse_ref(ref)
    target = parse_target(to)
    if parsed is None:
        blocks.append(ext.blocked("invalid-ref", _("引用格式应为 skill:<名>@<客户端>:<作用域>")))
    elif parsed[0] != "skill":
        blocks.append(ext.blocked("unsupported-kind", _("本版本只处理 Skill；MCP 的写入不在此命令内")))
    if target is None:
        blocks.append(ext.blocked("invalid-target", _("目标格式应为 <客户端>:<作用域>，客户端为 claude|codex|cursor，作用域为 user|project")))
    if blocks:
        return _finish(plan), ctx
    assert parsed is not None and target is not None
    _kind, name, _src_client, _src_scope = parsed
    target_client, target_scope = target

    report = inv.observe(home, project, None, "skills", "all")
    item = next((entry for entry in report["skills"] if entry["ref"] == ref), None)
    if item is None:
        near = _candidates(report, name)
        blocks.append(ext.blocked("source-not-found", _("没有找到这个 Skill 引用"), candidates=near))
        return _finish(plan), ctx
    source_root = inv.resolve_store(item["store"], home, project)
    source_path = source_root / item["name"] if source_root else None
    plan["source"] = {"ref": ref, "name": name, "client": item["client"], "scope": item["scope"], "store": item["store"],
                      "path": item["path"], "is_symlink": item["is_symlink"], "content_sha256": item["content_sha256"]}
    if source_path is None:
        blocks.append(ext.blocked("project-not-found", _("需要项目目录，请用 --project 指定")))
        return _finish(plan), ctx
    if not project.is_dir() and (target_scope == "project" or item["scope"] == "project"):
        blocks.append(ext.blocked("project-not-found", _("需要项目目录，请用 --project 指定")))
        return _finish(plan), ctx
    if item["evidence"] != "file" or not item["content_sha256"]:
        blocks.append(ext.blocked("source-not-file-evidence", _("源条目没有文件级证据（目录损坏或没有可读的 SKILL.md），只读，不能迁移")))
    store = TARGET_STORE[(target_client, target_scope)]
    target_root = inv.resolve_store(store, home, project)
    target_path = target_root / name
    plan["target"] = {"client": target_client, "scope": target_scope, "store": store, "path": f"{store}/{name}"}
    direction = "same" if item["scope"] == target_scope else ("widen" if RANK[target_scope] > RANK[item["scope"]] else "narrow")
    plan["direction"] = direction

    native = [s for sc, s in inv.SKILL_STORES[target_client] if sc == target_scope]
    if item["store"] in native and not blocks:
        plan["status"] = "native"
        plan["steps"].append(ext.step("s1", "native-noop", "certain", to=plan["source"]["path"]))
        plan["warnings"].append(ext.warning("native", _("这个客户端已经原生读取该存储，无需写入")))
        return _finish(plan, status="native"), ctx
    if direction == "widen" and not allow_widen:
        blocks.append(ext.blocked("widen-needs-flag", _("项目级 → 用户级会放宽作用域（所有项目都会看到），需要 --allow-widen")))
    if direction == "widen":
        plan["warnings"].append(ext.warning("widen", _(WIDEN_WARNING)))

    manifest = ext.tree_manifest(source_path) if not blocks else None
    if not blocks:
        if manifest is None:
            blocks.append(ext.blocked("dir-too-large", _("Skill 目录文件过多（超过 {0} 个），不迁移").format(ext.MAX_TREE_FILES)))
        else:
            outside = ext.external_links(source_path, manifest)
            if outside:
                blocks.append(ext.blocked("source-has-external-symlink", _("Skill 目录内有指向目录外的符号链接，复制后会失效"), paths=outside))
    effective_mode = mode
    if mode == "link":
        if item["scope"] != target_scope:
            blocks.append(ext.blocked("link-cross-scope", _("跨作用域（家目录 ↔ 项目）不能用符号链接，只能复制")))
        else:
            hint = ext.sync_folder_hint(target_path) or ext.sync_folder_hint(source_path)
            if hint:
                effective_mode = "copy"
                plan["warnings"].append(ext.warning("sync-folder-demoted", _("检测到同步盘位置（{0}）：同步盘对符号链接的行为没有一手文档，已降级为复制").format(hint), folder=hint))
                plan["sync_folder"] = hint
    plan["mode"] = effective_mode
    if effective_mode == "link" and not keep_source:
        plan["warnings"].append(ext.warning("link-keeps-source", _("符号链接模式必然保留源（目标指向它）；两处内容同步变化")))
    plan["retire_source"] = effective_mode == "copy" and not keep_source

    if target_path.exists() or target_path.is_symlink():
        if target_path.is_symlink() and not target_path.exists():
            relation = "different"
        else:
            existing = ext.tree_manifest(target_path) if target_path.is_dir() else None
            relation = "identical" if manifest is not None and existing == manifest else "different"
        blocks.append(ext.blocked("target-exists-" + relation, _("目标已存在同名条目（内容{0}），不覆盖、不改名、不合并").format(
            _("相同") if relation == "identical" else _("不同")), target=plan["target"]["path"]))
    if os.path.realpath(source_path) == os.path.realpath(target_path):
        blocks.append(ext.blocked("target-is-source", _("目标就是源本身")))

    if target_scope == "project" and item["scope"] == "user":
        relative = f"{store[len(inv.PROJECT) + 1:]}/{name}"
        plan["confirmations"].append({
            "id": "project-content",
            "text": _("将进入项目目录：{0}（可能被提交到版本库）；请自行确认其许可证与是否含私有内容，工具无法判断").format(relative),
            "flag": "--confirm-project-content"})

    if not blocks:
        steps = plan["steps"]
        steps.append(ext.step("s1", "create-recovery-dir", "certain", to=plan["recovery_base"]))
        if effective_mode == "link":
            steps.append(ext.step("s2", "create-link", "best-effort", to=plan["target"]["path"], target=plan["source"]["path"]))
        else:
            steps.append(ext.step("s2", "copy-tree", "certain", from_=plan["source"]["path"], to=plan["target"]["path"],
                                  files=len(manifest or []), tree_digest=ext.tree_digest(manifest or [])))
        steps.append(ext.step("s3", "verify-readback", "certain", to=plan["target"]["path"]))
        steps.append(ext.step("s4", "archive-source" if plan["retire_source"] else "keep-source", "certain",
                              from_=plan["source"]["path"]))
        steps.append(ext.step("s5", "confirm-in-client", "cannot-promise" if plan.get("sync_folder") else "best-effort",
                              client=target_client))
    plan["status"] = "blocked" if blocks else "ready"
    ctx.update(source=source_path, target=target_path, manifest=manifest, home=home, project=project,
               recovery_override=recovery_override)
    return _finish(plan), ctx


def _finish(plan: dict, status: str | None = None) -> dict:
    for entry in plan["steps"]:
        if "from_" in entry:
            entry["from"] = entry.pop("from_")
    if status:
        plan["status"] = status
    plan["plan_digest"] = ext.plan_digest(plan)
    return plan


def next_command(plan: dict, args_text: str) -> str:
    return f"agenthelm relocate {args_text} --apply --expect-plan {plan['plan_digest']}" + (
        " --confirm-project-content" if plan["confirmations"] else "")


# ------------------------------------------------------------------ apply

def apply_plan(plan: dict, ctx: dict, confirm_project: bool) -> dict:
    """Execute a ready plan. Never raises for expected failures; returns the ``applied`` record."""
    home, project = ctx["home"], ctx["project"]
    source: Path = ctx["source"]
    target: Path = ctx["target"]
    manifest = ctx["manifest"]
    if plan["status"] == "native":
        return {"status": "native", "written": False}
    if plan["confirmations"] and not confirm_project:
        return {"status": "refused", "reason": "project-content-unconfirmed", "written": False,
                "message": _("需要确认：{0}（加 --confirm-project-content）").format(plan["confirmations"][0]["text"])}
    base = ext.recovery_base(home, ctx["recovery_override"])
    moved: list[tuple[Path, Path]] = []
    created: list[Path] = []
    made_dirs: list[Path] = []
    recovery: Path | None = None
    staging: Path | None = None
    try:
        recovery = ext.new_recovery_dir(base, f"relocate-{plan['source']['name']}")
        ext.write_private_json(recovery / "relocate.json", {
            "schema_version": ext.SCHEMA_VERSION, "ref": plan["ref"], "source": plan["source"]["path"],
            "target": plan["target"]["path"], "mode": plan["mode"], "plan_digest": plan["plan_digest"]})
        if target.exists() or target.is_symlink():
            raise ext.ExtError("target appeared after the preview")
        made_dirs = ext.make_parents(target.parent)
        if plan["mode"] == "link":
            relative = os.path.relpath(source, target.parent)
            os.symlink(relative, target, target_is_directory=True)
            created.append(target)
            if os.readlink(target) != relative or os.path.realpath(target) != os.path.realpath(source):
                raise ext.ExtError("link read-back mismatch")
        else:
            staging = target.parent / f".{target.name}.agenthelm-tmp-{os.getpid()}"
            ext.copy_tree(source, staging)
            created.append(staging)
            if not ext.readback_matches(manifest, staging):
                raise ext.ExtError("copy read-back mismatch")
            if target.exists() or target.is_symlink():
                raise ext.ExtError("target appeared after the preview")
            os.rename(staging, target)
            created.remove(staging)
            created.append(target)
            if not ext.readback_matches(manifest, target):
                raise ext.ExtError("installed copy read-back mismatch")
        archived = None
        if plan["retire_source"]:
            archived = ext.archive(source, recovery, "skills", plan["source"]["name"])
            moved.append((archived, source))
    except Exception as exc:  # noqa: BLE001 - every failure must roll back the same way
        ext.rollback_moves(moved)
        for path in reversed(created):
            ext.remove_created(path)
        ext.remove_empty_parents(made_dirs)
        return {"status": "rolled-back", "written": False, "reason": type(exc).__name__,
                "message": _("失败，已回滚：{0}").format(str(exc)),
                "recovery_dir": ext.display(recovery, home, project) if recovery else None}
    applied: dict[str, Any] = {
        "status": "applied", "written": True, "mode": plan["mode"],
        "target": plan["target"]["path"], "readback": {"files": len(manifest), "ok": True,
                                                       "skill_md_sha256": plan["source"]["content_sha256"]},
        "recovery_dir": ext.display(recovery, home, project), "archived": None, "restore": [],
    }
    if archived is not None:
        applied["archived"] = ext.display(archived, home, project)
        applied["restore"] = [ext.restore_line(archived, source, home)]
        applied["restore_note"] = _("恢复：把归档目录移回原位（本版没有 restore 命令）")
    return applied


# ------------------------------------------------------------------ text

ACTION_TEXT = {
    "native-noop": N_("无需写入：{to} 已被该客户端原生读取"),
    "create-recovery-dir": N_("在 {to} 下建恢复目录（权限 0700）"),
    "copy-tree": N_("复制 {from} → {to}（{files} 个条目，目录内符号链接保持为链接）"),
    "create-link": N_("建立相对符号链接 {to} → {target}"),
    "verify-readback": N_("读回校验 {to}：逐文件比较相对路径与内容摘要，SKILL.md 摘要与源一致"),
    "archive-source": N_("校验通过后把源 {from} 归档到恢复目录（归档不是删除）"),
    "keep-source": N_("保留源 {from}"),
    "confirm-in-client": N_("下一步：重启 {client} 或重新进入项目，确认它出现（工具不执行客户端命令）"),
}


def render_text(plan: dict, applied: dict | None, command: str, color: bool = False) -> str:
    lines = []
    preview = applied is None
    lines.append(_("agenthelm relocate：预览（未写入任何文件）") if preview else _("agenthelm relocate：已执行"))
    lines.append(_("来源：{0}").format(plan["ref"]))
    if plan["target"]:
        lines.append(_("目标：{0}:{1}  {2}").format(plan["target"]["client"], plan["target"]["scope"], plan["target"]["path"]))
        direction = {"narrow": _("收窄"), "same": _("同级"), "widen": _("放宽")}[plan["direction"]]
        text = _("方式：{0}；方向：{1}").format(plan["mode"], direction)
        lines.append(ext.red(text, color) if plan["direction"] == "widen" else text)
    for entry in plan["steps"]:
        template = ACTION_TEXT[entry["action"]]
        body = _(template).format(**entry)
        lines.append(f"  {entry['id']} [{entry['promise']}] {body}")
    for warning in plan["warnings"]:
        text = _("注意：{0}").format(warning["message"])
        lines.append(ext.red(text, color) if warning["code"] == "widen" else text)
    for entry in plan["confirmations"]:
        lines.append(_("需要确认（{0}）：{1}").format(entry["flag"], entry["text"]))
    for entry in plan["blocked"]:
        lines.append(_("已阻止 [{0}]：{1}").format(entry["code"], entry["message"]))
        for candidate in entry.get("candidates", []):
            lines.append(f"  - {candidate}")
    if preview:
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
    """Entry for ``agenthelm relocate``. Exit codes: 0 ok / preview without blocks, 1 blocked or failed, 2 usage."""
    if args.lang and args.lang != "auto":
        os.environ["AGENTHELM_LANG"] = args.lang
    if sys.platform not in ("darwin",) and not sys.platform.startswith("linux"):
        sys.stderr.write(_("只支持 macOS 与 Linux\n"))
        return 2
    if args.apply and not args.expect_plan:
        sys.stderr.write(_("--apply 必须带 --expect-plan <计划指纹>（先预览取得）\n"))
        return 2
    project = (args.project or Path.cwd()).resolve()
    recovery_override = getattr(args, "recovery_root", None)
    plan, ctx = build_plan(home, project, args.ref, args.to, args.mode, args.allow_widen, args.keep_source, recovery_override)
    args_text = _command_text(args)
    command = next_command(plan, args_text)
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
            applied = apply_plan(plan, ctx, args.confirm_project_content)
            code = 0 if applied["status"] in ("applied", "native") else 1
    if args.output == "json":
        document = {"schema_version": ext.SCHEMA_VERSION, "command": "relocate",
                    "mode": "apply" if args.apply else "preview", "plan": plan, "applied": applied}
        print(json.dumps(document, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(render_text(plan, applied, command, ext.use_color()))
    return code


def _command_text(args: Any) -> str:
    import shlex  # noqa: PLC0415
    parts = [shlex.quote(args.ref), "--to", shlex.quote(args.to)]
    if args.mode != "copy":
        parts += ["--mode", args.mode]
    if args.project:
        parts += ["--project", shlex.quote(str(args.project))]
    if args.allow_widen:
        parts.append("--allow-widen")
    if args.keep_source:
        parts.append("--keep-source")
    return " ".join(parts)
