#!/usr/bin/env python3
"""MCP entry moves and retirements for a ``native-package`` client (``agenthelm relocate`` mcp refs, later ``dedupe``).

The one place that edits MCP entries in the client's user-level MCP file (and its ``local`` section under the project's
path) and the project's ``.mcp.json``. It reuses the readers and helpers of ``agent_pack_native_package`` (the same ones the
sync uses), writes atomically, restores every touched file byte for byte on a failure, and never prints a value:
plans, outputs and the metadata this module writes carry names, scopes, paths and digests only. The only files that hold
the user's own values are the recovery fragments (``mcp/<name>@<scope>.json``) and the byte-exact backups
(``claude.json.before`` / ``project-mcp.json.before``), all mode 0600.

Only entries the project's agent-pack state owns are ever removed by the tool; any other entry gets the official manual
command and a backup reminder (``MANUAL_REMOVE`` / ``MANUAL_BACKUP``), flagged as not archivable by the tool.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import stat
import sys
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))
import agent_pack_clients as registry  # noqa: E402
import agent_pack_config as core  # noqa: E402
import agent_pack_ext_common as ext  # noqa: E402
import agent_pack_mcp_json as mcp_json  # noqa: E402
import agent_pack_inventory as inv  # noqa: E402
from i18n_agent_pack import _, N_  # noqa: E402

# Writers as data: the clients whose MCP stores this module knows how to edit are the ones whose driver declares a
# ``native-package`` sync, with the scopes their ``stores.mcp`` declare. Whether a client supports project-level writes is
# decided only by ``agent_pack_config.project_write_supported``.
MCP_WRITERS = {client: tuple(store["scope"] for store in registry.stores(client, "mcp"))
               for client in registry.ids_with_sync("native-package")}
RANK = {"local": 0, "project": 0, "user": 1}   # breadth: a higher rank widens; local <-> project is lateral
ROOT_KEY = core.mcp_root_key(core.native_package_client())   # the key holding the servers in those stores
_USER_STORE = registry.user_mcp_store(core.native_package_client())["path"]
STORE_PATH = {"user": _USER_STORE, "local": _USER_STORE + "#projects[" + inv.PROJECT + "]",
              "project": inv.PROJECT + "/.mcp.json"}
PLACE = {"local": "project:local", "project": "project:file"}


def manual_remove(client: str) -> str | None:
    """The documented command that removes an entry by hand (``mcp.manual_remove``), with ``{scope}`` and ``{name}`` left."""
    argv = (registry.mcp_spec(client) or {}).get("manual_remove")
    return " ".join(argv) if argv else None


def manual_backup(client: str) -> str:
    """A backup command for the user-level MCP file the manual command edits."""
    path = registry.user_mcp_store(client)["path"]
    return f"cp {path} {path}.bak"


WIDEN_WARNING = N_("放宽：这个 MCP 之后在所有项目里可见")

ACTION_TEXT = {
    "write-mcp-entry": N_("写入 {to}：条目 {name}（原样复制，不显示任何值）"),
    "verify-mcp-readback": N_("读回校验 {to}：条目 {name} 与源逐字段一致"),
    "retire-mcp-entry": N_("校验通过后从 {from} 移除源条目 {name}；原片段存入恢复目录 mcp/（0600），配置文件先整份逐字节备份"),
    "update-state": N_("更新 agent-pack 的归属记录：{name} → {place}"),
    "manual-remove-mcp": N_("{ref}：不可由工具归档，见下面的手工命令"),
}


def entry_hash(entry: Any) -> str:
    """The hash the Claude sync records for an entry (``state`` ownership), so both agree."""
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode("utf-8")).hexdigest()


def _short(entry: Any) -> str:
    return entry_hash(entry)[:16]


def _native():
    import agent_pack_native_package  # noqa: PLC0415 - shipped next to this file
    return agent_pack_native_package


# ------------------------------------------------------------------ reading

def _root_and_package(project: Path) -> tuple[Path, str | None, Any]:
    """(project root, package name or ``None``, layout or ``None``). A project without a usable profile is fine."""
    try:
        layout = core.load_project_layout(project)
        return layout.root, core._package_name(layout.root), layout
    except core.AgentPackError:
        return project, None, None


def read_entry(home: Path, root: Path, scope: str, name: str) -> tuple[Any, str | None]:
    """(raw entry or ``None``, problem). ``problem`` is a short code when the store cannot be read as expected."""
    native = _native()
    try:
        if scope == "project":
            servers, problem = native._read_project_file(native._project_file(root))
            return servers.get(name), problem
        config = native._read_config(home)
        if scope == "user":
            return (config.get(ROOT_KEY) or {}).get(name), None
        _key, servers, problem = native.local_section(config, root)
        return servers.get(name), problem
    except core.AgentPackError:
        return None, "config-unreadable"


def ownership(home: Path, package: str | None, scope: str, name: str) -> dict:
    """Whether the project's agent-pack state owns the entry at that scope, and the hash it recorded.

    Only the state of this project's package counts; an entry owned by another package's state (or by none) is unmanaged.
    """
    native = _native()
    state = native._read_state(home, package) if package else {}
    if scope == "user":
        mcps = state.get("mcps") if isinstance(state.get("mcps"), dict) else {}
        recorded = mcps.get(name)
        places = native._state_scopes(state, "mcps")
        owned = name in mcps and places.get(name, "user") == "user"
        return {"managed": owned, "recorded_hash": recorded if owned else None, "package": package}
    owned = native._state_scopes(state, "mcps").get(name) == PLACE[scope]
    hashes = native._project_hashes(state)
    return {"managed": owned, "recorded_hash": hashes.get(name) if owned else None, "package": package}


# ------------------------------------------------------------------ plan

def manual_entry(item_ref: str, client: str, scope: str, store: str, name: str) -> dict:
    entry: dict[str, Any] = {"ref": item_ref, "client": client, "scope": scope, "store": store, "name": name,
                             "not_archivable_by_tool": True, "promise": "cannot-promise"}
    if manual_remove(client) is not None:
        entry["command"] = manual_remove(client).format(scope=scope, name=shlex.quote(name))
        entry["backup"] = manual_backup(client)
    else:
        entry["instruction"] = _("手工删除 {0} 里名为 {1} 的 MCP 条目（先自己备份该文件）").format(store, name)
    return entry


def _finish(plan: dict, status: str | None = None) -> dict:
    if status:
        plan["status"] = status
    plan["plan_digest"] = ext.plan_digest(plan)
    return plan


def parse_target(text: str) -> tuple[str, str] | None:
    client, colon, scope = text.partition(":")
    return (client, scope) if colon and scope in ("user", "local", "project") else None


def build_plan(home: Path, project: Path, ref: str, to: str, allow_widen: bool = False, keep_source: bool = False,
               recovery_override: Path | None = None, mode: str = "copy") -> tuple[dict, dict]:
    """The plan of moving one MCP entry between Claude scopes (JSON-safe, never a value) and the private apply context."""
    home = home.expanduser().resolve()
    project = project.expanduser().resolve()
    root, package, layout = _root_and_package(project)
    plan: dict[str, Any] = {
        "ref": ref, "kind": "mcp", "source": None, "target": None, "mode": "copy", "retire_source": False,
        "direction": None, "status": "blocked", "steps": [], "blocked": [], "warnings": [], "confirmations": [],
        "manual": [], "recovery_base": ext.display(ext.recovery_base(home, recovery_override), home, root),
    }
    ctx: dict[str, Any] = {}
    blocks = plan["blocked"]
    from agent_pack_relocate import parse_ref  # noqa: PLC0415 - one ref grammar for both kinds
    parsed = parse_ref(ref)
    target = parse_target(to)
    if parsed is None or parsed[0] != "mcp":
        blocks.append(ext.blocked("invalid-ref", _("引用格式应为 mcp:<名>@<客户端>:<作用域>")))
    if target is None:
        blocks.append(ext.blocked("invalid-target", _("MCP 的目标格式应为 <客户端>:<作用域>，作用域为 user|local|project")))
    if blocks:
        return _finish(plan), ctx
    assert parsed is not None and target is not None
    _kind, name, src_client, src_scope = parsed
    target_client, target_scope = target

    report = inv.observe(home, root, inv.clients_with(src_client), "mcp", "all")
    item = next((entry for entry in report["mcps"] if entry["ref"] == ref), None)
    if item is None:
        near = sorted(entry["ref"] for entry in report["mcps"] if entry["name"] == name)
        blocks.append(ext.blocked("source-not-found", _("没有找到这个 MCP 引用"), candidates=near))
        return _finish(plan), ctx
    store = STORE_PATH.get(item["scope"], item["store"])
    plan["source"] = {"ref": ref, "name": name, "client": item["client"], "scope": item["scope"], "store": store,
                      "path": store, "managed": False}
    plan["target"] = {"client": target_client, "scope": target_scope, "store": STORE_PATH[target_scope],
                      "path": STORE_PATH[target_scope]}
    direction = "same" if RANK[item["scope"]] == RANK[target_scope] and item["scope"] != target_scope else (
        "widen" if RANK[target_scope] > RANK[item["scope"]] else "narrow")
    plan["direction"] = direction

    if src_client != target_client:
        blocks.append(ext.blocked("cross-client-unsupported", _("本版本只在同一客户端的作用域之间迁移 MCP；跨客户端用 transfer 或 sync")))
    for client in dict.fromkeys((src_client, target_client)):
        if client not in MCP_WRITERS:
            blocks.append(ext.blocked("mcp-client-unsupported", _("{0} 的 MCP 配置本工具不写入").format(client), client=client))
        elif (item["scope"] != "user" or target_scope != "user") and not core.project_write_supported(client):
            blocks.append(ext.blocked("project-write-unsupported", _("{0} 不支持项目级写入").format(client), client=client))
    if mode != "copy":
        blocks.append(ext.blocked("link-unsupported", _("MCP 条目只能复制，不支持 --mode link")))
    if item["scope"] == target_scope:
        blocks.append(ext.blocked("target-is-source", _("目标作用域就是源本身")))
    if not root.is_dir():
        blocks.append(ext.blocked("project-not-found", _("需要项目目录，请用 --project 指定")))
    if item["evidence"] != "file":
        blocks.append(ext.blocked("source-not-file-evidence", _("源条目没有文件级证据，只读，不能迁移")))
    if direction == "widen" and not allow_widen:
        blocks.append(ext.blocked("widen-needs-flag", _("项目级 → 用户级会放宽作用域（所有项目都会看到），需要 --allow-widen")))
    if direction == "widen":
        plan["warnings"].append(ext.warning("widen", WIDEN_WARNING))
    if blocks:
        return _finish(plan), ctx

    raw, problem = read_entry(home, root, item["scope"], name)
    if raw is None or problem or not isinstance(raw, dict):
        blocks.append(ext.blocked("source-unreadable", _("读不到源条目（配置文件结构不被识别或已变化），不迁移")))
        return _finish(plan), ctx
    owner = ownership(home, package, item["scope"], name)
    current_hash = entry_hash(raw)
    plan["source"].update(managed=owner["managed"], entry_digest=current_hash[:16])
    if owner["managed"] and owner["recorded_hash"] and owner["recorded_hash"] != current_hash:
        plan["warnings"].append(ext.warning("modified-since-sync", _("源条目自上次同步后被改过：仍按受管处理，原片段会完整存入恢复目录")))

    # target side
    existing, target_problem = read_entry(home, root, target_scope, name)
    native = _native()
    if target_problem:
        blocks.append(ext.blocked("target-unreadable", _("目标存储的结构不被识别，不写入"), problem=target_problem))
    elif existing is not None:
        relation = "identical" if entry_hash(existing) == current_hash else "different"
        blocks.append(ext.blocked("target-exists-" + relation, _("目标已存在同名条目（内容{0}），不覆盖、不改名、不合并").format(
            _("相同") if relation == "identical" else _("不同")), target=plan["target"]["path"]))
    elif not native._writable(native.user_config(home) if target_scope != "project" else native._project_file(root)):
        blocks.append(ext.blocked("target-not-writable", _("目标文件不可写")))
    if target_scope == "project":
        if item.get("has_inline_secret"):
            blocks.append(ext.blocked("inline-secret-in-project-file", _("条目含内联密钥值，不能写入项目文件 .mcp.json；请先改成 ${VAR} 引用（工具不改写条目）")))
        fields = native.machine_specific(raw, home, root)
        if fields:
            blocks.append(ext.blocked("machine-specific-in-project-file", _("条目含本机路径（{0}），不能写入项目文件 .mcp.json").format(", ".join(fields)), fields=fields))
        plan["confirmations"].append({
            "id": "project-content", "flag": "--confirm-project-content",
            "text": _("将进入项目文件 .mcp.json（可能被提交到版本库）；请自行确认其内容适合共享")})
    if layout is not None and package:
        declared = core.declared_scope(layout, "mcp", name)
        landing = "user" if target_scope == "user" else "project"
        explicit = name in layout.mcp_scope or layout.default_scope != "user"   # only a declaration the project made
        if explicit and declared != landing:
            plan["warnings"].append(ext.warning("declaration-differs", _("项目 Profile 把 {0} 声明为 {1} 级：下次 sync 会按声明处理它，请同步更新声明").format(name, declared)))

    plan["retire_source"] = owner["managed"] and not keep_source
    if not blocks:
        steps = plan["steps"]
        steps.append(ext.step("s1", "create-recovery-dir", "certain", to=plan["recovery_base"]))
        steps.append(ext.step("s2", "write-mcp-entry", "certain", to=plan["target"]["path"], name=name))
        steps.append(ext.step("s3", "verify-mcp-readback", "certain", to=plan["target"]["path"], name=name))
        if plan["retire_source"]:
            steps.append(ext.step("s4", "retire-mcp-entry", "certain", name=name, **{"from": plan["source"]["path"]}))
            steps.append(ext.step("s5", "update-state", "certain", name=name,
                                  place="user" if target_scope == "user" else PLACE[target_scope]))
        elif not owner["managed"]:
            plan["manual"].append(manual_entry(ref, item["client"], item["scope"], store, name))
            steps.append(ext.step("s4", "manual-remove-mcp", "cannot-promise", ref=ref))
            plan["warnings"].append(ext.warning("mcp-not-archivable", _("未受管条目不可由工具归档：只复制到新位置，原位置不动；要移除请按下面的手工命令（先自己备份）")))
        else:
            steps.append(ext.step("s4", "keep-source", "certain", **{"from": plan["source"]["path"]}))
        steps.append(ext.step(f"s{len(steps) + 1}", "confirm-in-client", "best-effort", client=target_client))
    plan["status"] = "blocked" if blocks else "ready"
    ctx.update(home=home, project=root, recovery_override=recovery_override, entry=raw, package=package,
               source_scope=item["scope"], target_scope=target_scope, name=name)
    return _finish(plan), ctx


# ------------------------------------------------------------------ writing

def _write_state(path: Path, data: dict) -> None:
    core._write_json_atomic(path, data, mode=0o600)


def state_after(state: dict, name: str, from_scope: str, to_scope: str | None, new_hash: str | None) -> dict:
    """The agent-pack state with ``name`` moved from ``from_scope`` to ``to_scope`` (or dropped when ``None``).

    The pre-existing ``mcps`` key stays user-level ownership only (an older runtime reads it that way); where each
    entry sits is recorded only under ``scopes``, completed from that key when it was absent.
    """
    new = copy.deepcopy(state)
    mcps = new.get("mcps") if isinstance(new.get("mcps"), dict) else {}
    scopes = new.get("scopes") if isinstance(new.get("scopes"), dict) else {}
    places = scopes.get("mcps") if isinstance(scopes.get("mcps"), dict) else {}
    hashes = scopes.get("project_hashes") if isinstance(scopes.get("project_hashes"), dict) else {}
    for known in mcps:
        places.setdefault(known, "user")
    mcps.pop(name, None)
    places.pop(name, None)
    hashes.pop(name, None)
    if to_scope == "user":
        mcps[name] = new_hash
        places[name] = "user"
    elif to_scope is not None:
        places[name] = PLACE[to_scope]
        hashes[name] = new_hash
    scopes.setdefault("skills", {})
    scopes.update(mcps=places, project_hashes=hashes)
    new.update(mcps=mcps, scopes=scopes)
    return new


def _fragment_name(name: str, scope: str) -> str:
    return f"{name}@{scope}.json"


def apply_edits(home: Path, root: Path, recovery: Path, edits: list[dict], state_package: str | None = None,
                state_change: dict | list[dict] | None = None) -> dict:
    """Apply ``edits`` (``{"op": "set"|"drop", "scope", "name", "entry"?}``) and the optional state change in one
    transaction. Config and project-file writes are atomic; any failure puts every touched file back byte for byte and
    re-raises. Returns the recovery fragment paths of the dropped entries."""
    native = _native()
    config_path = native.user_config(home)
    project_file = native._project_file(root)
    state_path = native.state_path(home, state_package) if state_package else None
    snapshots: dict[Path, tuple[bytes | None, int]] = {}
    for path in (config_path, project_file, state_path):
        if path is not None:
            snapshots[path] = (path.read_bytes() if path.is_file() else None,
                               stat.S_IMODE(path.stat().st_mode) if path.is_file() else 0o600)
    config = native._read_config(home)
    config_before = json.loads(json.dumps(config))
    fragments: list[Path] = []
    file_data: dict[str, Any] | None = None
    touched: list[Path] = []
    try:
        for edit in edits:
            if edit["op"] == "drop":
                path = recovery / "mcp" / _fragment_name(edit["name"], edit["scope"])
                ext.write_private_json(path, {"schema_version": ext.SCHEMA_VERSION, "name": edit["name"],
                                              "scope": edit["scope"], "store": STORE_PATH[edit["scope"]],
                                              "entry": edit["entry"]})
                os.chmod(path, 0o600)
                os.chmod(path.parent, 0o700)
                fragments.append(path)
        if any(e["scope"] != "project" for e in edits):
            _backup(recovery, "claude.json.before", snapshots[config_path][0])
        if any(e["scope"] == "project" for e in edits):
            _backup(recovery, "project-mcp.json.before", snapshots[project_file][0])
            file_data = mcp_json.read_config(project_file) if snapshots[project_file][0] is not None else {}
        for edit in edits:
            servers = _servers_for(native, config, file_data, edit["scope"], root, create=edit["op"] == "set")
            if edit["op"] == "set":
                servers[edit["name"]] = edit["entry"]
            elif servers is not None:
                servers.pop(edit["name"], None)
        if config != config_before:
            touched.append(config_path)
            core._write_json_atomic(config_path, config, mode=snapshots[config_path][1] if snapshots[config_path][0] else 0o600)
        if file_data is not None:
            touched.append(project_file)
            core._write_json_atomic(project_file, file_data, mode=snapshots[project_file][1] if snapshots[project_file][0] else 0o644)
        if state_package and state_change is not None and state_path is not None:
            after = native._read_state(home, state_package)
            for change in ([state_change] if isinstance(state_change, dict) else state_change):
                after = state_after(after, **change)
            touched.append(state_path)
            _write_state(state_path, after | {"updated_at": ext_stamp()})
        _verify(home, root, edits)
    except Exception:
        for path in reversed(touched):
            data, mode = snapshots[path]
            native._restore_bytes(path, data, mode)
        raise
    return {"fragments": fragments}


def ext_stamp() -> str:
    from datetime import UTC, datetime  # noqa: PLC0415
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def _backup(recovery: Path, name: str, data: bytes | None) -> None:
    if data is None:
        return
    path = recovery / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def _servers_for(native, config: dict, file_data: dict | None, scope: str, root: Path, create: bool) -> dict | None:
    if scope == "user":
        return config.setdefault(ROOT_KEY, {}) if create else config.get(ROOT_KEY, {})
    if scope == "project":
        assert file_data is not None
        return file_data.setdefault(ROOT_KEY, {})
    key = native.local_section(config, root)[0]
    if key is None:
        raise core.AgentPackError("claude local MCP store changed shape since the preview")
    if not create and key not in (config.get("projects") or {}):
        return {}
    return config.setdefault("projects", {}).setdefault(key, {}).setdefault(ROOT_KEY, {})


def _verify(home: Path, root: Path, edits: list[dict]) -> None:
    for edit in edits:
        found, problem = read_entry(home, root, edit["scope"], edit["name"])
        if problem:
            raise ext.ExtError("read-back failed: " + problem)
        if edit["op"] == "set" and found != edit["entry"]:
            raise ext.ExtError("read-back mismatch")
        if edit["op"] == "drop" and found is not None:
            raise ext.ExtError("entry still present after removal")


def restore_lines(recovery: Path, home: Path, fragments: list[Path], edits: list[dict]) -> list[str]:
    lines = []
    for edit, fragment in zip((e for e in edits if e["op"] == "drop"), fragments):
        lines.append(_("把 {0} 里的 entry 手工放回 {1} 的 mcpServers.{2}；或整份还原 {3}（逐字节备份，会丢掉之后的其他改动）").format(
            ext.display(fragment, home, None), STORE_PATH[edit["scope"]], edit["name"],
            ext.display(recovery / ("project-mcp.json.before" if edit["scope"] == "project" else "claude.json.before"), home, None)))
    return lines


def retire_group(home: Path, root: Path, recovery: Path, package: str | None, items: list[dict]) -> dict:
    """Remove managed entries (``{"scope", "name", "entry"}``) in one transaction (``dedupe --retire-others``): fragments
    and byte-exact backups first, atomic edits, state records dropped, byte-exact rollback on failure."""
    edits = [{"op": "drop", **item} for item in items]
    changes = [{"name": item["name"], "from_scope": item["scope"], "to_scope": None, "new_hash": None} for item in items]
    result = apply_edits(home, root, recovery, edits, package, changes)
    return {"fragments": result["fragments"], "edits": edits}


def apply_plan(plan: dict, ctx: dict, confirm_project: bool) -> dict:
    """Execute a ready MCP move plan; failures roll back and are reported, never raised."""
    home, root = ctx["home"], ctx["project"]
    if plan["confirmations"] and not confirm_project:
        return {"status": "refused", "reason": "project-content-unconfirmed", "written": False,
                "message": _("需要确认：{0}（加 --confirm-project-content）").format(plan["confirmations"][0]["text"])}
    recovery: Path | None = None
    name, source_scope, target_scope = ctx["name"], ctx["source_scope"], ctx["target_scope"]
    edits = [{"op": "set", "scope": target_scope, "name": name, "entry": ctx["entry"]}]
    state_change = None
    if plan["retire_source"]:
        edits.append({"op": "drop", "scope": source_scope, "name": name, "entry": ctx["entry"]})
        state_change = {"name": name, "from_scope": source_scope, "to_scope": target_scope, "new_hash": entry_hash(ctx["entry"])}
    try:
        recovery = ext.new_recovery_dir(ext.recovery_base(home, ctx["recovery_override"]), f"relocate-mcp-{name}")
        ext.write_private_json(recovery / "relocate.json", {
            "schema_version": ext.SCHEMA_VERSION, "ref": plan["ref"], "source": plan["source"]["path"],
            "target": plan["target"]["path"], "retire_source": plan["retire_source"], "plan_digest": plan["plan_digest"]})
        result = apply_edits(home, root, recovery, edits, ctx["package"] if plan["retire_source"] else None, state_change)
    except Exception as exc:  # noqa: BLE001 - every failure rolls back the same way
        return {"status": "rolled-back", "written": False, "reason": type(exc).__name__,
                "message": _("失败，已回滚：{0}").format(str(exc)),
                "recovery_dir": ext.display(recovery, home, root) if recovery else None}
    applied: dict[str, Any] = {
        "status": "applied", "written": True, "mode": "copy", "target": plan["target"]["path"],
        "readback": {"ok": True}, "recovery_dir": ext.display(recovery, home, root),
        "retired": plan["retire_source"], "restore": [], "manual": plan["manual"]}
    if plan["retire_source"]:
        applied["restore"] = restore_lines(recovery, home, result["fragments"], edits)
    return applied
