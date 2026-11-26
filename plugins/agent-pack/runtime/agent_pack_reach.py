#!/usr/bin/env python3
"""Impact and reachability of one Skill / MCP disposition (design #184 section 3).

``impact`` is a pure function: it gets one disposition (which entries are archived, which copies are added) and the file
facts of every client that reads the entry, and returns the per-client verdict ``unchanged | lost | replaced | gained |
uncertain | unknown`` plus the warnings and text derived from it. It never touches the file system. ``read_facts`` (the
last section) builds those facts from an inventory observation and the file system.

Everything about a client comes from its driver declaration (``stores``, ``compat_reads``, ``approval_required``,
``trust_required``); this module names no client. The verdicts are file-level: whether a client really loads a copy depends
on settings keys that inventory does not read (``loaded`` is always ``unknown``), and the output says so.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))
import agent_pack_clients as registry  # noqa: E402
import agent_pack_ext_common as ext  # noqa: E402
from i18n_agent_pack import _, N_  # noqa: E402

SCHEMA = 1
MAX_HOPS = 8   # a symlink chain longer than this is reported as ``link-chain-too-deep``
VERDICTS = ("unchanged", "lost", "replaced", "gained", "uncertain", "unknown")
GATES = {"approval": "project-approval-unknown", "trust": "project-trust-unknown"}

REASON_TEXT = {
    "evidence-unknown": N_("有副本没有文件级证据"),
    "store-undetermined": N_("该客户端的读取位置尚未确定"),
    "compat-copy-present": N_("兼容读取位置里有同名副本，该客户端是否读取未核实"),
    "project-approval-unknown": N_("项目级 .mcp.json 需要逐项目批准，批准状态未读取"),
    "project-trust-unknown": N_("项目级配置只对受信任项目生效，信任状态未读取"),
    "link-chain-too-deep": N_("符号链接层数过多或成环"),
}
KIND_TEXT = {"skill": N_("技能"), "mcp": N_("MCP")}
OTHER_MACHINES = N_("若该条目随项目仓库同步，迁到用户级后其他机器上的该项目不再带有它；工具不检查它是否已提交")


# ------------------------------------------------------------------ the pure computation

def _is_under(path: str | None, directory: str) -> bool:
    return path is not None and (path == directory or path.startswith(directory.rstrip("/") + "/"))


def destroyed_how(entry: dict, retired: set[str], retired_dirs: list[str]) -> str | None:
    """``"direct"`` (the entry itself is archived), ``"link"`` (a hop of its chain, or the directory it resolves into, is
    archived) or ``None`` (it survives)."""
    hops = entry["hops"]
    if hops[0]["phys"] in retired:
        return "direct"
    for hop in hops:
        if hop["phys"] in retired or any(_is_under(hop["phys"], d) or _is_under(hop.get("real"), d) for d in retired_dirs):
            return "link"
    return None


def _gate_reasons(entries: list[dict]) -> list[str]:
    return [GATES[gate] for gate in GATES if any(entry.get("gate") == gate for entry in entries)]


def _client_row(names: list[str], client: dict, entries: list[dict], adds: list[dict], retired: set[str],
                retired_dirs: list[str]) -> tuple[dict | None, list[dict]]:
    """The verdict row of one client (``None`` when it has no copy before and none after) and its link-destroyed entries."""
    mine = [entry for entry in entries if entry["client"] == client["id"]]
    state = {id(entry): destroyed_how(entry, retired, retired_dirs) for entry in mine}
    survivors = [entry for entry in mine if state[id(entry)] is None and not entry.get("problem")]
    reads = {read["path"]: read for read in client["reads"]}
    arriving = []
    for add in adds:
        if add.get("store"):
            place = reads.get(add["store"])
        elif add["client"] == client["id"]:
            place = next((read for read in client["reads"] if read["scope"] == add["scope"]), None)
        else:
            place = None
        if place is not None:
            arriving.append({"ref": add["ref_for"](client["id"]), "digest": add["digest"], "store": place["path"],
                             "gate": place.get("gate"), "evidence": "file"})
    after = survivors + arriving
    link_hit = [entry for entry in mine if state[id(entry)] == "link" and not entry.get("problem")]
    if not mine and not after:
        return None, link_hit
    before_digests = {entry["digest"] for entry in mine}
    after_digests = {entry["digest"] for entry in after}
    reasons: list[str] = []
    unknown: list[str] = []
    if client.get("undetermined"):
        unknown.append("store-undetermined")
    if any(entry["evidence"] != "file" or entry.get("problem") in ("dangling",) for entry in mine):
        unknown.append("evidence-unknown")
    if any(entry.get("problem") in ("too-deep", "loop") for entry in mine):
        unknown.append("link-chain-too-deep")
    reliable_before = [entry for entry in mine if not entry.get("gate")]
    reliable_after = [entry for entry in after if not entry.get("gate")]
    if unknown:
        verdict, reasons = "unknown", unknown
    elif not mine:
        verdict = "gained"
    elif before_digests == after_digests and after:
        verdict = "unchanged"
    elif not after:
        verdict = "lost"
    else:
        verdict = "replaced"
    if verdict == "lost":
        compat = [entry for entry in client.get("compat", []) if entry["name"] in names
                  and destroyed_how(entry, retired, retired_dirs) is None and not entry.get("problem")]
        if compat:
            verdict, reasons = "uncertain", ["compat-copy-present"]
        elif not reliable_before:
            verdict, reasons = "uncertain", _gate_reasons(mine)
        else:
            if link_hit:
                reasons.append("link-target-retired")
            if any(state[id(entry)] == "direct" for entry in mine):
                reasons += ["entry-retired", "no-other-copy"]
    elif verdict in ("unchanged", "replaced") and reliable_before and not reliable_after:
        verdict, reasons = "uncertain", _gate_reasons(after)
    elif verdict == "gained" and not reliable_after:
        verdict, reasons = "uncertain", _gate_reasons(after)
    row: dict[str, Any] = {
        "detected": bool(client.get("detected")), "reads": [read["path"] for read in client["reads"]],
        "before": sorted(entry["ref"] for entry in mine), "after": sorted(entry["ref"] for entry in after),
        "after_stores": sorted({entry["store"] for entry in after}),
        "verdict": verdict, "evidence": "unknown" if verdict == "unknown" else "file", "reasons": reasons}
    if link_hit:
        row["via_before"] = [{"ref": entry["ref"], "path": entry["path"], "chain": [hop["shown"] for hop in entry["hops"][1:]],
                              "resolved": entry.get("resolved")} for entry in link_hit]
    return row, link_hit


def impact(disposition: dict, facts: dict) -> dict:
    """The ``impact`` object of a plan (design section 3.7). ``disposition`` is ``{kind, name, names?, retire[], add[],
    other_machines}`` (``names``: the entry names the disposition concerns when ``name`` is only a label); ``retire[]`` items are ``{phys, is_link, real, scope}`` and ``add[]`` items ``{client | store, scope,
    digest, ref_for}``; ``facts`` is ``{clients[], entries[]}`` (see ``read_facts``)."""
    kind, name = disposition["kind"], disposition["name"]
    retired = {item["phys"] for item in disposition["retire"]}
    retired_dirs = [item["real"] for item in disposition["retire"] if not item["is_link"] and item.get("real")]
    names = disposition.get("names") or [name]
    entries = [entry for entry in facts["entries"] if entry["name"] in names]
    clients: dict[str, dict] = {}
    dangling: set[str] = set()
    for client in facts["clients"]:
        row, link_hit = _client_row(names, client, entries, disposition["add"], retired, retired_dirs)
        dangling.update(entry["path"] for entry in link_hit)
        if row is not None:
            clients[client["id"]] = row
    notes = []
    if any(item.get("scope") == "user" for item in disposition["retire"]):
        notes.append("affects-all-projects")
    if disposition.get("other_machines"):
        notes.append("other-machines-lose-it")
    result = {
        "schema": SCHEMA, "kind": kind, "name": name, "basis": "file", "clients": clients,
        "lost_clients": [c for c, row in clients.items() if row["verdict"] == "lost"],
        "uncertain_clients": [c for c, row in clients.items() if row["verdict"] == "uncertain"],
        "unknown_clients": [c for c, row in clients.items() if row["verdict"] == "unknown"],
        "dangling_after": sorted(dangling), "scope_notes": sorted(notes),
    }
    return result


# ------------------------------------------------------------------ warnings and text

def _display(client: str) -> str:
    return (registry.get(client) or {}).get("display", client)


def _reason_phrases(reasons: list[str]) -> str:
    return "；".join(_(REASON_TEXT[code]) for code in reasons if code in REASON_TEXT) or "-"


def warnings(result: dict) -> list[dict]:
    """The ``impact-*`` warnings (``message`` follows the interface language, everything else is data)."""
    out = []
    kind_label = _(KIND_TEXT.get(result["kind"], result["kind"]))
    for client, row in result["clients"].items():
        if row["verdict"] == "lost":
            out.append(ext.warning("impact-lost-client", _("影响面：退役后 {0} 将失去 {1}「{2}」").format(
                _display(client), kind_label, result["name"]), client=client, ref=row["before"][0]))
        elif row["verdict"] in ("uncertain", "unknown"):
            out.append(ext.warning("impact-uncertain", _("影响面：无法确定 {0} 的结果（{1}）；工具只保证文件改动，不保证客户端行为").format(
                _display(client), _reason_phrases(row["reasons"])), client=client, reasons=row["reasons"]))
    if result["dangling_after"]:
        out.append(ext.warning("impact-dangling-link", _("影响面：这些符号链接将悬空：{0}").format(", ".join(result["dangling_after"])),
                               paths=result["dangling_after"]))
    if "other-machines-lose-it" in result["scope_notes"]:
        out.append(ext.warning("impact-other-machines", _(OTHER_MACHINES)))
    return out


def render_lines(result: dict) -> list[str]:
    """The text block of the preview (design section 3.8)."""
    kind_label = _(KIND_TEXT.get(result["kind"], result["kind"]))
    name = result["name"]
    lines: list[str] = []
    for client, row in result["clients"].items():
        shown = _display(client)
        where = ", ".join(row["after_stores"]) or "-"
        verdict = row["verdict"]
        if verdict == "lost":
            lines.append(_("影响面：退役后 {0} 将失去 {1}「{2}」").format(shown, kind_label, name))
            for via in row.get("via_before", []):
                lines.append(_("  原因：它的符号链接 {0} 指向将被归档的目录，归档后链接悬空").format(via["path"]))
            if "no-other-copy" in row["reasons"]:
                lines.append(_("  原因：{0} 读取的位置里没有其他同名副本（读取位置：{1}）").format(shown, ", ".join(row["reads"])))
            continue
        if verdict == "unchanged":
            line = _("影响面：{0} 不受影响（仍可从 {1} 读取）").format(shown, where)
        elif verdict == "replaced":
            line = _("影响面：{0} 将改为读取内容不同的另一份（{1}）").format(shown, where)
        elif verdict == "gained":
            line = _("影响面：{0} 将新增 {1}「{2}」（读取位置：{3}）").format(shown, kind_label, name, where)
        else:
            line = _("影响面：无法确定 {0} 的结果（{1}）；工具只保证文件改动，不保证客户端行为").format(shown, _reason_phrases(row["reasons"]))
        if not row["detected"]:
            line += " " + _("（本机未检测到该客户端，仅按其声明计算）")
        lines.append(line)
    if result["dangling_after"]:
        lines.append(_("影响面：这些符号链接将悬空：{0}").format(", ".join(result["dangling_after"])))
    if "affects-all-projects" in result["scope_notes"]:
        lines.append(_("用户级条目影响所有项目；本机项目清单未提供"))
    if "other-machines-lose-it" in result["scope_notes"]:
        lines.append(_(OTHER_MACHINES))
    lines.append(_("影响面按各客户端读取的文件位置计算；是否真正加载取决于客户端设置，本命令不读取"))
    return lines


def attach(plan: dict, disposition: dict, facts: dict) -> None:
    """Compute the impact of ``disposition``, store ``plan["impact"]`` and append the ``impact-*`` warnings."""
    result = impact(disposition, facts)
    plan["impact"] = result
    plan["warnings"].extend(warnings(result))


# ------------------------------------------------------------------ reading the facts (the only part that touches files)

def physical(path: Path) -> Path:
    """Parent directories fully resolved, the last component not: where ``path`` itself lives (a move of it moves that)."""
    return Path(os.path.realpath(path.parent)) / path.name


def chain(path: Path, shown: Any) -> tuple[list[dict], str | None, str | None]:
    """``(hops, problem, resolved)`` of an entry path: each hop is ``{phys, real, shown}`` (hop 0 is the entry itself); a
    problem is ``dangling`` | ``too-deep`` | ``loop``; ``resolved`` is the final directory (display form) when it resolves."""
    hops: list[dict] = []
    seen: set[str] = set()
    current = path    # followed physically (parents resolved), so a ``..`` in a link target means what the OS means
    lexical = path    # the same hop spelled the way the links spell it, for display
    problem = None
    while True:
        phys = physical(current)
        hops.append({"phys": str(phys), "real": os.path.realpath(current), "shown": shown(lexical)})
        if not current.is_symlink():
            break
        if str(phys) in seen:
            problem = "loop"
            break
        seen.add(str(phys))
        if len(hops) > MAX_HOPS:
            problem = "too-deep"
            break
        try:
            target = os.readlink(current)
        except OSError:
            problem = "dangling"
            break
        lexical = Path(os.path.normpath(os.path.join(os.path.dirname(lexical), target)))
        current = Path(os.path.normpath(os.path.join(os.path.dirname(phys), target)))
    if problem is None and not (hops and Path(hops[-1]["real"]).is_dir()):
        problem = "dangling"
    return hops, problem, shown(Path(hops[-1]["real"])) if problem is None else None


def read_facts(kind: str, names: list[str], home: Path, project: Path, report: dict, getenv=None, find_cli=None) -> dict:
    """The facts ``impact`` needs, from an observation ``report`` of every client (``clients=all``) and the file system."""
    import agent_pack_inventory as inv  # noqa: PLC0415 - shipped next to this file

    getenv = getenv or os.environ.get
    find_cli = find_cli or shutil.which
    store_kind = "skills" if kind == "skill" else "mcp"

    def shown(path: Path | str) -> str:
        return inv.display_path(path, home, project)

    items = report["skills"] if kind == "skill" else report["mcps"]
    entries = []
    for item in items:
        if item["name"] not in names:
            continue
        gate = _gate(item["client"], store_kind, item["store"])
        entry = {"ref": item["ref"], "client": item["client"], "name": item["name"], "scope": item["scope"], "store": item["store"],
                 "evidence": item["evidence"], "gate": gate,
                 "digest": item.get("content_sha256") if kind == "skill" else item.get("fingerprint")}
        if kind == "skill":
            root = inv.resolve_store(item["store"], home, project)
            path = root / item["name"]
            hops, problem, resolved = chain(path, shown)
            entry.update(path=item["path"], hops=hops, problem=problem, resolved=resolved)
        else:
            key = f"{item['store']}#{item['name']}"
            entry.update(path=item["store"], hops=[{"phys": key, "real": None, "shown": item["store"]}], problem=None, resolved=None)
        entries.append(entry)
    clients = []
    for client in registry.all_ids():
        driver = registry.get(client)
        stores = registry.stores(client, store_kind)
        undetermined = len(registry.stores(client, store_kind, with_undetermined=True)) != len(stores)
        if not stores and not undetermined:
            continue
        detected = registry.detect(driver, home, getenv, find_cli)["detected"]
        if client not in registry.default_ids() and not detected:
            continue
        row = {"id": client, "detected": detected, "undetermined": undetermined,
               "reads": [{"scope": s["scope"], "path": s["path"], "gate": _gate_of(s)} for s in stores], "compat": []}
        if kind == "skill":
            for place in registry.compat_reads(client, store_kind):
                root = inv.resolve_store(place["path"], home, project)
                for name in names:
                    path = root / name if root is not None else None
                    if path is None or not (path.exists() or path.is_symlink()):
                        continue
                    hops, problem, _resolved = chain(path, shown)
                    row["compat"].append({"name": name, "client": client, "path": shown(path), "hops": hops, "problem": problem})
        clients.append(row)
    return {"clients": clients, "entries": entries}


def facts_for(kind: str, names: list[str], home: Path, project: Path) -> dict:
    """Observe every registered client (a client outside the default set only counts when detected) and read the facts."""
    import agent_pack_inventory as inv  # noqa: PLC0415

    report = inv.observe(home, project, ["all"], "skills" if kind == "skill" else "mcp", "all")
    return read_facts(kind, names, home, project, report)


def _gate_of(store: dict) -> str | None:
    return "approval" if store.get("approval_required") else ("trust" if store.get("trust_required") else None)


def _gate(client: str, kind: str, store: str) -> str | None:
    declared = next((s for s in registry.stores(client, kind) if s["path"] == store), None)
    return _gate_of(declared) if declared else None


def retire_item(path: Path, scope: str) -> dict:
    """A ``retire[]`` item for an archived Skill directory or link."""
    link = path.is_symlink()
    return {"phys": str(physical(path)), "is_link": link, "real": None if link else os.path.realpath(path), "scope": scope}


def retire_mcp_item(store: str, name: str, scope: str) -> dict:
    return {"phys": f"{store}#{name}", "is_link": True, "real": None, "scope": scope}


def add_item(kind: str, name: str, client: str, scope: str, store: str | None, digest: str | None) -> dict:
    """An ``add[]`` item: a copy that appears in ``store`` (visible to every client that reads it) or, for an MCP entry,
    in ``client``'s ``scope``."""
    import agent_pack_inventory as inv  # noqa: PLC0415

    def ref_for(reader: str) -> str:
        return inv.entry_ref({"name": name, "client": reader, "scope": scope, "store": store or ""}, kind)

    return {"client": client, "scope": scope, "store": store, "digest": digest, "ref_for": ref_for}
