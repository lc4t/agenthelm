"""技能与 MCP 的写入动作：move / dedupe / scope（默认预览，``--apply`` 才写）。

- move、dedupe 全部转发给 agent-pack 的 ``agenthelm relocate`` / ``agenthelm dedupe``（参数列表形式的子进程，``--output json``），
  不 import agent-pack。它们的 plan（含 ``plan_digest``、``blocked``、``promise``、``confirmations``）原样展示；
  ``--apply`` 时先重新预览并比对摘要，不一致就拒绝（没有任何写入），一致才用 ``--expect-plan`` 回传同一摘要。
- scope 把「生效范围」写成 Project Profile 的 ``capabilities.mcp_scope`` / ``skill_scope`` 声明（agent-pack 的 sync / reconcile 读取它）：
  预览给 diff 与可粘贴片段；``--apply`` 先把 Profile 备份到恢复目录，再原子写入，写后读回。
- 输出里不含 env 值、headers、令牌：转发来的 plan 经过一次递归清洗，只留白名单之外的键被丢弃。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from i18n_orchestrator import _, N_  # noqa: E402
from wf import S
import project_extensions as ext
from project_base import PROFILE, EntryError, read_profile

ACTION_TIMEOUT_SECONDS = 180
SECRET_KEYS = frozenset({"env", "headers", "token", "secret", "password", "authorization", "args", "command_line"})
REF_RE = re.compile(r"^(skill|mcp):([^@\s]+)@([a-z0-9-]+):([a-z]+)(#[a-z0-9-]+)?$")
SCOPE_KEY = {"skill": "skill_scope", "mcp": "mcp_scope"}
NO_WRITE_GROUP = N_("此类重复（跨客户端或证据不足）本版没有写入动作")
EXT_HINT = N_("更新 agent-pack 或运行时（需要 agenthelm relocate / dedupe）后再试")
DISPOSITIONS = ("retire-others", "keep-all", "ignore", "unignore")


# ---------------------------------------------------------------- 子进程与清洗

def scrub(value: Any) -> Any:
    """递归去掉可能携带值的键：env、headers、令牌类。agent-pack 本身不输出它们，这里是第二道防线。"""
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items() if str(key).lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def run_agenthelm(host: Any, tail: list[str]) -> tuple[int, dict]:
    command = ext.agenthelm_command(host)
    if command is None:
        raise EntryError(_("未找到 agent-pack 的 agenthelm 命令"), "unavailable", [_(EXT_HINT)])
    try:
        code, out, err = ext._run([*command, *tail], ACTION_TIMEOUT_SECONDS)
    except OSError as exc:
        raise EntryError(_("无法运行 agenthelm：{0}").format(exc), "unavailable") from exc
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        if code == 2:
            raise EntryError(_("当前的 agenthelm 不认识这个命令或参数（需要更新 agent-pack）：{0}").format(err or tail[0]), "unavailable")
        raise EntryError(_("agenthelm {0} 没有返回可解析的结果：{1}").format(tail[0], err or _("输出不是 JSON")), "failed")
    return code, scrub(data)


def _blocked(plan: dict) -> bool:
    return bool(plan.get("blocked")) or plan.get("status") == "blocked"


def _forward(host: Any, root: Path, tail: list[str], *, apply: bool, expect: str | None) -> dict:
    """预览一次；--apply 时重算摘要并比对，一致才转发 --apply --expect-plan。返回 {mode, plan, applied, refused?}。"""
    base = [*tail, "--project", str(root), "--output", "json"]
    _code, preview = run_agenthelm(host, base)
    plan = preview.get("plan")
    if not isinstance(plan, dict):
        raise EntryError(_("agenthelm 没有给出计划：{0}").format(preview.get("error") or _("无详情")), "failed")
    result = {"mode": "preview", "plan": plan, "applied": None, "blocked": _blocked(plan)}
    if not apply:
        return result
    if not expect:
        raise EntryError(_("--apply 需要 --expect-plan <预览里的 plan_digest>"), "usage",
                         [_("先不加 --apply 运行一次预览，复制其中的 plan_digest")])
    result["mode"] = "apply"
    if plan.get("plan_digest") != expect:
        return {**result, "applied": {"status": "refused", "reason": "plan-changed"}, "refused": "plan-changed"}
    if _blocked(plan):
        return {**result, "applied": {"status": "refused", "reason": "blocked"}, "refused": "blocked"}
    code, done = run_agenthelm(host, [*base, "--apply", "--expect-plan", expect])
    applied = done.get("applied")
    if not isinstance(applied, dict):
        applied = {"status": "failed", "reason": str(done.get("error") or code)}
    result["applied"] = applied
    if isinstance(done.get("plan"), dict):
        result["plan"] = done["plan"]
    return result


# ---------------------------------------------------------------- move / dedupe

def move(host: Any, root: Path, ref: str, to: str, *, mode: str | None = None, keep_source: bool = False,
         allow_widen: bool = False, confirm_content: bool = False, apply: bool = False, expect: str | None = None) -> dict:
    parsed = REF_RE.match(ref)
    if parsed is None:
        raise EntryError(_("条目引用不合法：{0}").format(ref), "usage", [_("形如 skill:<名>@<客户端>:<作用域>；用 extensions list 查看")])
    tail = ["relocate", ref, "--to", to]
    if mode:
        tail += ["--mode", mode]
    for flag, on in (("--keep-source", keep_source), ("--allow-widen", allow_widen), ("--confirm-project-content", confirm_content)):
        if on:
            tail.append(flag)
    result = _forward(host, root, tail, apply=apply, expect=expect)
    result["action"] = "move"
    return result


def dedupe_groups(host: Any, root: Path) -> list[dict] | None:
    """dedupe --list 的分组；旧版 agenthelm 或失败时返回 None（交给 dedupe 自己判断）。"""
    try:
        _code, data = run_agenthelm(host, ["dedupe", "--list", "--project", str(root), "--output", "json"])
    except EntryError:
        return None
    groups = data.get("groups")
    return [group for group in groups if isinstance(group, dict)] if isinstance(groups, list) else None


def unsupported_group(group_id: str) -> dict:
    return {"mode": "preview", "action": "dedupe", "plan": None, "applied": None, "blocked": True, "group": group_id,
            "unsupported": True, "message": _(NO_WRITE_GROUP)}


def dedupe(host: Any, root: Path, group_id: str, *, keep: str | None = None, retire_others: bool = False,
           keep_all: bool = False, note: str | None = None, ignore: bool = False, unignore: bool = False,
           apply: bool = False, expect: str | None = None) -> dict:
    chosen = sum(bool(item) for item in (retire_others, keep_all, ignore, unignore))
    if chosen != 1:
        raise EntryError(_("请选择且只选择一种处置：--keep <ref> --retire-others、--keep-all --note …、--ignore、--unignore"), "usage")
    if retire_others and not keep:
        raise EntryError(_("--retire-others 需要 --keep <ref>"), "usage")
    if keep_all and not note:
        raise EntryError(_("--keep-all 需要 --note 说明原因"), "usage")
    groups = dedupe_groups(host, root)
    if groups is not None and group_id not in {group.get("id") for group in groups}:
        return unsupported_group(group_id)
    tail = ["dedupe", group_id]
    if retire_others:
        tail += ["--keep", keep, "--retire-others"]
    elif keep_all:
        tail += ["--keep-all", "--note", note or ""]
    elif ignore:
        tail.append("--ignore")
    else:
        tail.append("--unignore")
    result = _forward(host, root, tail, apply=apply, expect=expect)
    result["action"] = "dedupe"
    return result


# ---------------------------------------------------------------- 读回（再次取 inventory，与计划逐项比对）

def _inventory_entries(host: Any, root: Path, kind: str) -> list[dict] | None:
    loaded = ext.load(host, root)
    if loaded["state"] != "available":
        return None
    items = loaded["inventory"].get("skills" if kind == "skill" else "mcps")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _find(entries: list[dict], name: str, client: str, scope: str) -> dict | None:
    return next((item for item in entries if (item.get("name"), item.get("client"), item.get("scope")) == (name, client, scope)), None)


def readback_move(host: Any, root: Path, result: dict) -> dict:
    """relocate 之后：目标存在且 content_sha256 与源一致；需要退役时源不再出现在原作用域。"""
    plan = result["plan"]
    status = (result.get("applied") or {}).get("status")
    if status == "native":
        return {"ok": True, "checks": [], "note": "native"}
    source, target = plan.get("source") or {}, plan.get("target") or {}
    entries = _inventory_entries(host, root, plan.get("kind") or "skill")
    if entries is None:
        return {"ok": False, "checks": [{"id": "inventory", "ok": False, "message": _("读回时 inventory 不可用")}]}
    checks = []
    found = _find(entries, source.get("name", ""), target.get("client", ""), target.get("scope", ""))
    checks.append({"id": "target-present", "ok": found is not None, "message": _("目标条目存在")})
    if found is not None and source.get("content_sha256"):
        same = found.get("content_sha256") == source["content_sha256"]
        checks.append({"id": "target-digest", "ok": same, "message": _("目标内容摘要与源一致")})
    if plan.get("mode") != "link" and plan.get("retire_source") and (source.get("client"), source.get("scope")) != \
            (target.get("client"), target.get("scope")):
        gone = _find(entries, source.get("name", ""), source.get("client", ""), source.get("scope", "")) is None
        checks.append({"id": "source-retired", "ok": gone, "message": _("源条目已归档")})
    return {"ok": all(check["ok"] for check in checks), "checks": checks}


def readback_dedupe(host: Any, root: Path, group_id: str, result: dict) -> dict:
    plan = result["plan"]
    disposition = plan.get("disposition")
    groups = dedupe_groups(host, root)
    if groups is None:
        return {"ok": False, "checks": [{"id": "list", "ok": False, "message": _("读回时 dedupe --list 不可用")}]}
    group = next((item for item in groups if item.get("id") == group_id), None)
    if disposition in ("keep-all", "ignore"):
        ok = bool(group and group.get("decision"))
        return {"ok": ok, "checks": [{"id": "decision", "ok": ok, "message": _("决定已记录")}]}
    if disposition == "unignore":
        ok = not (group and group.get("decision"))
        return {"ok": ok, "checks": [{"id": "decision", "ok": ok, "message": _("决定已撤销")}]}
    if disposition == "retire-others" and (result.get("applied") or {}).get("status") == "applied":
        retired = [item.get("ref") if isinstance(item, dict) else item for item in plan.get("retire") or []]
        left = {member.get("ref") for member in (group or {}).get("members", [])}
        ok = not (set(retired) & left)
        return {"ok": ok, "checks": [{"id": "retired", "ok": ok, "message": _("被退役的副本不再出现")}]}
    return {"ok": True, "checks": []}


def finish(provider: Any, view: str, result: dict, group_id: str | None = None) -> bool:
    """应用之后读回并写进 result["readback"]；返回是否需要关注（blocked、拒绝、回滚、失败、读回不一致）。"""
    attention = bool(result.get("blocked")) or bool(result.get("refused"))
    if result["mode"] != "apply" or result.get("refused") or result.get("unsupported"):
        return attention
    status = (result.get("applied") or {}).get("status")
    if view != "scope" and status in ("applied", "recorded"):
        result["readback"] = provider.extensions_readback(view, result, group_id)
        attention = attention or not result["readback"]["ok"]
    return attention or status in ("rolled-back", "failed", "refused")


# ---------------------------------------------------------------- scope：写 Project Profile 的声明

def _profile_text(profile: dict) -> str:
    return json.dumps(profile, ensure_ascii=False, indent=2) + "\n"


def scope_plan(host: Any, root: Path, ref: str, effective: str, *, allow_widen: bool = False) -> dict:
    parsed = REF_RE.match(ref)
    if parsed is None:
        raise EntryError(_("条目引用不合法：{0}").format(ref), "usage", [_("形如 skill:<名>@<客户端>:<作用域>；用 extensions list 查看")])
    if effective not in ("project", "user"):
        raise EntryError(_("--effective 只能是 project 或 user"), "usage")
    kind, name, client, scope = parsed.group(1), parsed.group(2), parsed.group(3), parsed.group(4)
    key = SCOPE_KEY[kind]
    profile = read_profile(root)
    capabilities = profile.get("capabilities")
    blocked: list[dict] = []
    if not isinstance(capabilities, dict):
        blocked.append({"code": "profile-no-capabilities", "message": _("Profile 没有 capabilities 对象，不自动创建")})
        capabilities = {}
    current_map = capabilities.get(key) or {}
    if not isinstance(current_map, dict):
        blocked.append({"code": "scope-map-invalid", "message": _("Profile 的 {0} 不是对象").format(key)})
        current_map = {}
    default = capabilities.get("default_scope", "user")
    if default not in ("user", "project"):
        blocked.append({"code": "default-scope-invalid", "message": _("Profile 的 default_scope 取值非法：{0}").format(default)})
    current = current_map.get(name, default)
    if current not in ("user", "project"):
        blocked.append({"code": "scope-value-invalid", "message": _("Profile 里 {0}.{1} 的取值非法").format(key, name)})
    entries = _inventory_entries(host, root, kind)
    entry = _find(entries, name, client, scope) if entries is not None else None
    if entries is not None and entry is None:
        blocked.append({"code": "entry-not-found", "message": _("inventory 里没有这个条目：{0}").format(ref)})
    if entry is not None and entry.get("evidence") == "unknown":
        blocked.append({"code": "evidence-unknown", "message": _("条目的证据为未知：只读，不写声明")})
    direction = "same" if current == effective else ("narrow" if effective == "project" else "widen")
    if direction == "widen" and not allow_widen:
        blocked.append({"code": "widen-needs-flag", "message": _("放宽（项目→用户级）需要 --allow-widen：此后所有项目都会看到它")})
    updated = json.loads(json.dumps(profile))
    if direction != "same" and not blocked:
        scopes = updated["capabilities"].setdefault(key, {})
        scopes[name] = effective
    before, after = _profile_text(profile), _profile_text(updated)
    diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), PROFILE.as_posix(), PROFILE.as_posix()))
    status = "blocked" if blocked else ("unchanged" if direction == "same" else "ready")
    plan = {"ref": ref, "kind": kind, "name": name, "key": key, "effective": effective, "current": current,
            "direction": direction, "status": status, "blocked": blocked, "profile": PROFILE.as_posix(), "diff": diff,
            "snippet": {"capabilities": {key: {name: effective}}},
            "steps": [{"id": "s1", "action": "backup-profile", "promise": "certain"},
                      {"id": "s2", "action": "write-profile-declaration", "promise": "certain"},
                      {"id": "s3", "action": "verify-readback", "promise": "certain"},
                      {"id": "s4", "action": "preview-sync", "promise": "cannot-promise"}],
            "warnings": [{"code": "declaration-only", "message": _("只写声明；条目要等 sync / reconcile 预览并应用后才真正移动，客户端行为工具不承诺")}],
            "next": {"sync": _("agent-pack 的 sync / reconcile 预览（不加 --apply）：看这条声明会让哪些文件变化")}}
    if kind == "mcp":
        plan["warnings"].append({"code": "mcp-declaration-only", "message": _("MCP 的实际写入由 agent-pack 的 sync 负责；本命令不编辑任何客户端配置")})
    plan["plan_digest"] = hashlib.sha256(json.dumps(
        {"ref": ref, "effective": effective, "direction": direction, "before": hashlib.sha256(before.encode()).hexdigest(),
         "after": hashlib.sha256(after.encode()).hexdigest(), "blocked": [item["code"] for item in blocked]},
        sort_keys=True).encode()).hexdigest()
    plan["_updated"] = after
    return plan


def scope_apply(provider: Any, root: Path, plan: dict, recovery: str) -> dict:
    """备份 Profile → 原子写入 → 读回。写前再确认 Profile 没有变过。"""
    path = root / PROFILE
    updated = plan["_updated"]
    backup_dir = Path(recovery)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_dir / PROFILE.name
    shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(prefix=".project-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except OSError as exc:
        Path(tmp).unlink(missing_ok=True)
        return {"status": "failed", "reason": str(exc), "recovery_dir": str(backup_dir)}
    ok = read_profile(root).get("capabilities", {}).get(plan["key"], {}).get(plan["name"]) == plan["effective"]
    return {"status": "applied" if ok else "failed", "recovery_dir": str(backup_dir),
            "restore": "cp {0} {1}".format(backup, path), "readback": {"ok": ok}}


def public_scope_plan(plan: dict) -> dict:
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def scope(provider: Any, ref: str, effective: str, *, allow_widen: bool = False, apply: bool = False,
          expect: str | None = None) -> dict:
    root = provider.root
    plan = scope_plan(provider.host, root, ref, effective, allow_widen=allow_widen)
    blocked = plan["status"] == "blocked"
    result = {"mode": "preview", "action": "scope", "plan": public_scope_plan(plan), "applied": None, "blocked": blocked}
    if not apply:
        return result
    if not expect:
        raise EntryError(_("--apply 需要 --expect-plan <预览里的 plan_digest>"), "usage",
                         [_("先不加 --apply 运行一次预览，复制其中的 plan_digest")])
    result["mode"] = "apply"
    if plan["plan_digest"] != expect:
        return {**result, "applied": {"status": "refused", "reason": "plan-changed"}, "refused": "plan-changed"}
    if blocked:
        return {**result, "applied": {"status": "refused", "reason": "blocked"}, "refused": "blocked"}
    if plan["status"] == "unchanged":
        return {**result, "applied": {"status": "unchanged"}}
    from project_maintain import _recovery_dir  # noqa: PLC0415  延迟导入，避免循环依赖

    result["applied"] = scope_apply(provider, root, plan, _recovery_dir(provider, "scope"))
    return result


# ---------------------------------------------------------------- 文字渲染

PROMISE_LABEL = {"certain": N_("确定"), "best-effort": N_("尽力而为（需在客户端里确认）"), "cannot-promise": N_("无法承诺（只保证文件改动，不保证客户端行为）")}
DIRECTION_LABEL = {"narrow": N_("收窄"), "same": N_("不变"), "widen": N_("放宽：此后所有项目都会看到它")}
APPLIED_LABEL = {"applied": N_("已应用"), "native": N_("无需写入（客户端已原生加载）"), "manual-only": N_("只给手工步骤，没有写入"),
                 "rolled-back": N_("失败，已回滚"), "refused": N_("已拒绝，没有写入"), "failed": N_("失败"),
                 "unchanged": N_("没有变化"), "recorded": N_("已记录")}
REFUSED_REASON = {"plan-changed": N_("预览之后内容有变化（摘要不一致）：请重新预览，再用新的 plan_digest 确认"),
                  "blocked": N_("计划有 blocked 项，不可执行")}


def _line_items(items: Any, key: str = "message") -> list[str]:
    out = []
    for item in items or []:
        out.append(f"  - {item.get(key) or item.get('code')}" if isinstance(item, dict) else f"  - {item}")
    return out


def render_plan(plan: dict) -> list[str]:
    lines = [S.bold(plan.get("ref") or plan.get("group") or "")]
    direction = plan.get("direction")
    if direction:
        label = _(DIRECTION_LABEL.get(direction, direction))
        lines.append(_("方向：{0}").format(S.red(label) if direction == "widen" else label))
    if plan.get("status"):
        lines.append(_("状态：{0}").format(plan["status"]))
    for step in plan.get("steps") or []:
        lines.append(f"  {step.get('id', '')}  {step.get('action', '')}  [{_(PROMISE_LABEL.get(step.get('promise'), str(step.get('promise'))))}]")
    if plan.get("blocked"):
        lines += [S.red(_("blocked（不可执行）："))] + _line_items(plan["blocked"])
    if plan.get("warnings"):
        lines += [S.yellow(_("提示："))] + _line_items(plan["warnings"])
    if plan.get("confirmations"):
        lines += [S.yellow(_("需要确认："))] + ["  - " + _("{0}（{1}）").format(item.get("text"), item.get("flag")) if isinstance(item, dict) else f"  - {item}"
                                              for item in plan["confirmations"]]
    for item in plan.get("manual") or []:
        lines.append("  $ " + str(item.get("command") if isinstance(item, dict) else item))
    if plan.get("diff"):
        lines += [_("Profile 将发生的改动："), *plan["diff"].rstrip("\n").splitlines()]
    if plan.get("snippet"):
        lines += [_("可粘贴的片段（合并进 Profile）："), *json.dumps(plan["snippet"], ensure_ascii=False, indent=2).splitlines()]
    nxt = plan.get("next")
    if isinstance(nxt, dict):
        for key, value in nxt.items():
            if key == "apply":
                continue
            lines.append(S.dim(_("下一步：{0}").format(value)))
    if plan.get("plan_digest"):
        lines.append(_("plan_digest：{0}").format(plan["plan_digest"]))
    return lines


def render_action(result: dict) -> list[str]:
    if result.get("unsupported"):
        return [S.yellow(result["message"])]
    lines = render_plan(result["plan"])
    applied = result.get("applied")
    if result["mode"] == "preview":
        lines.append(S.dim(_("以上为预览，没有写入。确认后加 --apply --expect-plan <plan_digest>。")))
        return lines
    status = (applied or {}).get("status", "failed")
    lines.append(S.bold(_("结果：{0}").format(_(APPLIED_LABEL.get(status, status)))))
    if result.get("refused"):
        lines.append(S.yellow(_(REFUSED_REASON.get(result["refused"], result["refused"]))))
    for key in ("recovery_dir", "restore"):
        if (applied or {}).get(key):
            value = applied[key]
            lines.append(f"  {key}: " + ("; ".join(map(str, value)) if isinstance(value, list) else str(value)))
    readback = result.get("readback")
    if readback:
        lines.append(_("读回：{0}").format(_("一致") if readback["ok"] else S.red(_("读回失败"))))
        lines += [f"  {'✓' if item['ok'] else '✗'} {item['message']}" for item in readback.get("checks", [])]
    return lines
