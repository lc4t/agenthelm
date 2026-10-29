"""项目私有的候选、灰度与使用记录；只保存状态和证据快照，从不发布规则或修改 OPINION.md。"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import versioned
from i18n_opinion import _  # noqa: E402


DEFAULT_ROOTS = ("tasks", "cases", "reviews", "projects", "journal")
RESULTS = ("passed", "not-applicable", "violated")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
EMPTY_STATE = {"schema_version": 1, "candidates": {}, "rollouts": {}, "events": []}


class LifecycleError(ValueError):
    pass


def fail(message: str) -> None:
    raise LifecycleError(message)


def confined(root: Path, relative: str | Path) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        fail(_("路径必须是项目内的相对路径"))
    if any(item.is_symlink() for item in (path, *path.parents) if item != root.parent):
        fail(_("拒绝使用符号链接"))
    path.resolve().relative_to(root.resolve())
    return path


def current_lock(root: Path) -> dict[str, Any]:
    try:
        versioned.verify_project(root)
    except (versioned.OpinionVersionError, OSError, json.JSONDecodeError) as exc:
        raise LifecycleError(_("当前 Opinion 必须先通过 verify：{0}").format(exc)) from exc
    return versioned.read_object(root / "opinion.lock.json")


def occurrence(path: str, roots: tuple[str, ...]) -> str:
    parts = Path(path).parts
    if len(parts) < 2 or parts[0] not in roots:
        fail(_("occurrence 必须指向项目内的工作记录，根目录为：{0}").format(_('、').join(roots)))
    return "/".join(parts[:2])


def profile_ref(profile: dict[str, Any]) -> str:
    return f"{profile['id']}@{profile['version']}"


def rule_fingerprints(profile: dict[str, Any]) -> dict[str, str]:
    """有效规则的内容指纹；个人修订后的规则与原规则引用相同但指纹不同。"""
    return {f"{rule['id']}@{rule['version']}": versioned.digest(rule) for rule in versioned.effective_rules(profile).values()}


def rule_families(profile: dict[str, Any]) -> dict[str, str]:
    families: dict[str, str] = {}
    for source in profile["sources"]["templates"]:
        for ref in source["value"]["rules"]:
            families.setdefault(ref, source["ref"].split("/")[0])
    return families


def candidate_view(candidate: dict[str, Any]) -> dict[str, Any]:
    usable = [item for item in candidate["evidence"] if not item.get("fixture")]
    groups = {item["occurrence"] for item in usable}
    failed = any(item["validation"] == "failed" for item in usable)
    passed = any(item["validation"] == "passed" for item in usable)
    status = candidate["decision"] or ("needs-resolution" if failed else "ready-for-review" if passed else "waiting-verification")
    return {"id": candidate["id"], "content": candidate["content"], "content_sha256": versioned.digest(candidate["content"]),
            "status": status, "distinct_occurrences": len(groups),
            "recommendation_due": candidate["credit"] >= 1 and not candidate["decision"],
            "priority": len(groups), "evidence": candidate["evidence"], "writes_stable_rules": False}


def record_use(event: dict[str, Any], lock: dict[str, Any], roots: tuple[str, ...]) -> dict[str, Any]:
    profile = lock["profile"]
    known = {source["ref"] for source in profile["sources"]["rules"]}
    if not event.get("rules") or not set(event["rules"]) <= known:
        fail(_("use 必须记录适用规则的精确引用"))
    use = {key: event[key] for key in ("evidence", "evidence_sha256", "rules", "passed", "user_changed")}
    if type(use["passed"]) is not bool or type(use["user_changed"]) is not bool:
        fail(_("passed 与 user_changed 必须是明确的布尔值"))
    results = event.get("results")
    if results is not None:
        if not isinstance(results, dict) or set(results) != set(event["rules"]) or not set(results.values()) <= set(RESULTS):
            fail(_("results 必须为每条规则给出结果：{0}").format(_('、').join(RESULTS)))
        if use["passed"] and "violated" in results.values():
            fail(_("存在违反的规则时 passed 不能为 true"))
        use["results"] = results
    bundles = {bundle["family"] for bundle in profile["loading"]["bundles"]} if "loading" in profile else None
    for key in ("loaded_bundles", "routing_miss"):
        if key in event:
            if bundles is None:
                fail(_("当前 Profile 未声明分层加载，不能记录 {0}").format(key))
            if not isinstance(event[key], list) or not set(event[key]) <= bundles or len(event[key]) != len(set(event[key])):
                fail(_("{0} 必须是已声明规则束的不重复列表").format(key))
            use[key] = event[key]
    if "date" in event:
        if not isinstance(event["date"], str) or not DATE.fullmatch(event["date"]):
            fail(_("date 必须为 YYYY-MM-DD"))
        use["date"] = event["date"]
    use.update(occurrence=occurrence(event["occurrence"], roots), fixture=bool(event.get("fixture")))
    return use


def reduce(state: dict[str, Any], event: dict[str, Any], lock: dict[str, Any] | None = None,
           roots: tuple[str, ...] = DEFAULT_ROOTS) -> dict[str, Any]:
    result = deepcopy(state)
    event_hash = versioned.digest(event)
    if event_hash in result["events"]:
        return result
    kind = event["kind"]
    receipt = {key: event[key] for key in ("evidence", "evidence_sha256", "evidence_origin") if key in event}
    if "fixture" in event and type(event["fixture"]) is not bool:
        fail(_("fixture 必须是明确的布尔值"))
    if kind == "capture":
        identity, content = event["id"], event["content"]
        if not all(isinstance(content.get(key), str) and content[key].strip() for key in ("title", "text")):
            fail(_("候选需要完整的 title 与 text"))
        if not content.get("scopes") or not all(isinstance(item, str) and item.strip() for item in content["scopes"]):
            fail(_("候选需要明确的 scopes"))
        if identity in result["candidates"]:
            if result["candidates"][identity]["content"] != content:
                fail(_("候选内容变化时必须使用新的候选编号"))
        else:
            result["candidates"][identity] = {"id": identity, "content": content, "capture_evidence": receipt,
                                              "evidence": [], "presentations": [], "credit": 0.0, "decision": None}
    elif kind == "observe":
        candidate = result["candidates"][event["id"]]
        group = occurrence(event["occurrence"], roots)
        before = {item["occurrence"] for item in candidate["evidence"] if not item.get("fixture")}
        evidence = {key: event[key] for key in ("evidence", "evidence_sha256", "validation")}
        if evidence["validation"] not in ("pending", "passed", "failed"):
            fail(_("validation 必须为 pending、passed 或 failed"))
        evidence.update(occurrence=group, fixture=bool(event.get("fixture")))
        candidate["evidence"].append(evidence)
        if not evidence["fixture"] and group not in before:
            count = len(before) + 1
            candidate["credit"] += count / (count + 1)
    elif kind == "presented":
        candidate = result["candidates"][event["id"]]
        if event["content_sha256"] != versioned.digest(candidate["content"]):
            fail(_("展示的内容与候选不一致"))
        candidate["credit"] = max(0.0, candidate["credit"] - 1)
        candidate.setdefault("presentations", []).append(receipt)
    elif kind == "decision":
        candidate = result["candidates"][event["id"]]
        if event["content_sha256"] != versioned.digest(candidate["content"]):
            fail(_("人工裁决指向的候选内容不一致"))
        if event["decision"] not in ("accepted", "rejected", "held") or event.get("human_confirmed") is not True:
            fail(_("需要明确的人工裁决"))
        candidate["decision"] = event["decision"]
        candidate["decision_evidence"] = event["evidence"]
        candidate["decision_receipt"] = receipt
        # accepted 只允许据此准备 Profile，从不代表可以自动发布。
    elif kind == "adopt":
        ref = profile_ref(lock["profile"])
        if event.get("human_confirmed") is not True or event["profile_sha256"] != lock["profile_sha256"]:
            fail(_("采用需要人工批准当前精确的 Profile"))
        if ref in result["rollouts"] and result["rollouts"][ref]["profile_sha256"] != lock["profile_sha256"]:
            fail(_("不可变的 Profile 身份发生了变化"))
        result["rollouts"].setdefault(ref, {"state": "gray", "profile_sha256": lock["profile_sha256"],
                                           "opinion_sha256": lock["opinion_sha256"], "approval_evidence": event["evidence"],
                                           "approval_receipt": receipt, "uses": [], "blockers": []})
    elif kind == "use":
        rollout = result["rollouts"][profile_ref(lock["profile"])]
        if rollout["profile_sha256"] != lock["profile_sha256"] or rollout["opinion_sha256"] != lock["opinion_sha256"]:
            fail(_("use 指向的 Opinion 内容已经变化"))
        use = record_use(event, lock, roots)
        rollout["uses"].append(use)
        if not use["fixture"] and (not use["passed"] or use["user_changed"] or use.get("routing_miss")):
            rollout["blockers"].append(event_hash)
    elif kind == "formalize":
        rollout = result["rollouts"][profile_ref(lock["profile"])]
        if event.get("human_confirmed") is not True or event["profile_sha256"] != lock["profile_sha256"]:
            fail(_("复盘必须确认未变化的当前 Profile"))
        if rollout["profile_sha256"] != lock["profile_sha256"] or rollout["opinion_sha256"] != lock["opinion_sha256"]:
            fail(_("灰度内容已经变化；请采用新的已核验版本"))
        if rollout["blockers"]:
            fail(_("转正式前先在新版本中解决灰度失败"))
        rollout["state"] = "formal"
        rollout["review_evidence"] = event["evidence"]
        rollout["review_receipt"] = receipt
    else:
        fail(_("未知的生命周期事件"))
    result["events"].append(event_hash)
    return result


def evidence_valid(root: Path, state: dict[str, Any]) -> None:
    receipts: list[dict[str, Any]] = []
    for candidate in state["candidates"].values():
        receipts.extend(candidate["evidence"])
        receipts.extend(candidate.get("presentations", []))
        receipts.extend(candidate[key] for key in ("capture_evidence", "decision_receipt") if candidate.get(key))
    for rollout in state["rollouts"].values():
        receipts.extend(rollout["uses"])
        receipts.extend(rollout[key] for key in ("approval_receipt", "review_receipt") if rollout.get(key))
    for item in receipts:
        if hashlib.sha256(confined(root, item["evidence"]).read_bytes()).hexdigest() != item["evidence_sha256"]:
            fail(_("不可变的生命周期证据发生变化；修改状态前先重新核验"))


def successful(use: dict[str, Any]) -> bool:
    return not use["fixture"] and use["passed"] and not use["user_changed"] and not use.get("routing_miss")


def carried_uses(state: dict[str, Any], profile: dict[str, Any], profiles_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """同一 Profile 编号的旧版本中，所记录规则在当前版本指纹全部未变的使用记录。"""
    current, fingerprints = profile_ref(profile), rule_fingerprints(profile)
    carried: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for ref, rollout in state["rollouts"].items():
        identity, release = ref.split("@")
        if ref == current or identity != profile["id"]:
            continue
        path = profiles_root / identity / "versions" / f"{release}.json"
        try:
            previous = versioned.load_profile(path)
        except (versioned.OpinionVersionError, OSError, json.JSONDecodeError):
            skipped.append({"profile": ref, "reason": _("找不到可核验的旧 Profile")})
            continue
        if versioned.digest(previous) != rollout["profile_sha256"]:
            skipped.append({"profile": ref, "reason": _("旧 Profile 与灰度记录的指纹不一致")})
            continue
        before = rule_fingerprints(previous)
        for use in rollout["uses"]:
            if all(before.get(rule) is not None and before.get(rule) == fingerprints.get(rule) for rule in use["rules"]):
                carried.append({**use, "carried_from": ref})
    return carried, skipped


def coverage_view(state: dict[str, Any], profile: dict[str, Any], profiles_root: Path) -> dict[str, Any]:
    ref = profile_ref(profile)
    own = state["rollouts"].get(ref, {}).get("uses", [])
    carried, skipped = carried_uses(state, profile, profiles_root)
    uses = [*own, *carried]
    families = rule_families(profile)
    rules = []
    for rule in rule_fingerprints(profile):
        relevant = [use for use in uses if rule in use["rules"] and not use["fixture"]]
        passed = [use for use in relevant if successful(use) and use.get("results", {}).get(rule, "passed") == "passed"]
        dates = sorted(use["date"] for use in passed if use.get("date"))
        rules.append({"ref": rule, "family": families.get(rule), "distinct_successful_uses": len({use["occurrence"] for use in passed}),
                      "violations": sum(1 for use in relevant if use.get("results", {}).get(rule) == "violated"),
                      "last_used": dates[-1] if dates else None})
    result = {"profile": ref, "own_uses": len([use for use in own if not use["fixture"]]),
              "carried_uses": len([use for use in carried if not use["fixture"]]), "not_carried": skipped,
              "distinct_successful_uses": len({use["occurrence"] for use in uses if successful(use)}),
              "rules": rules, "unused_rules": [item["ref"] for item in rules if not item["distinct_successful_uses"]]}
    if "loading" in profile:
        bundles = []
        for bundle in profile["loading"]["bundles"]:
            family = bundle["family"]
            members = {rule for rule, owner in families.items() if owner == family}
            good = [use for use in uses if successful(use)]
            bundles.append({"family": family,
                            "loaded_in": len({use["occurrence"] for use in good if family in use.get("loaded_bundles", [])}),
                            "exercised_in": len({use["occurrence"] for use in good if members & set(use["rules"])}),
                            "routing_misses": sum(1 for use in uses if not use["fixture"] and family in use.get("routing_miss", []))})
        result["bundles"] = bundles
        result["unused_bundles"] = [item["family"] for item in bundles if not item["exercised_in"]]
    return result


def status_view(state: dict[str, Any], root: Path, profiles_root: Path) -> dict[str, Any]:
    value = deepcopy(state["rollouts"])
    for rollout in value.values():
        actual = [use for use in rollout["uses"] if not use["fixture"]]
        rollout["distinct_successful_uses"] = len({use["occurrence"] for use in actual if successful(use)})
        rollout["ready_for_review"] = not rollout["blockers"] and rollout["distinct_successful_uses"] > 1
        rollout["automatic_formalization"] = False
    payload: dict[str, Any] = {"ok": True, "status": "read-only", "value": value}
    try:
        profile = current_lock(root)["profile"]
    except LifecycleError as exc:
        payload["coverage"] = None
        payload["coverage_unavailable"] = str(exc)
        return payload
    coverage = coverage_view(state, profile, profiles_root)
    payload["coverage"] = coverage
    ref = profile_ref(profile)
    if ref in value:
        # 指纹未变的规则沿用旧版本的使用证据；失败记录不延续，但在覆盖视图中保留可见。
        value[ref]["distinct_successful_uses"] = coverage["distinct_successful_uses"]
        value[ref]["carried_uses"] = coverage["carried_uses"]
        value[ref]["ready_for_review"] = not value[ref]["blockers"] and coverage["distinct_successful_uses"] > 1
    return payload


def add_command(subparsers: Any) -> None:
    parser = subparsers.add_parser("lifecycle", help=_("项目私有的候选、灰度与使用记录；不发布规则"))
    parser.add_argument("action", choices=("queue", "status", "event"))
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--store", required=True, help=_("项目内保存状态与证据快照的相对目录"))
    parser.add_argument("--file", type=Path, help=_("event 的事件 JSON，必须位于项目内"))
    parser.add_argument("--profiles-root", help=_("保存历史 Profile 的项目内相对目录，默认 .opinion/profiles"))
    parser.add_argument("--occurrence-root", action="append", default=[], help=_("允许作为独立发生场景的顶层目录，可重复"))
    parser.add_argument("--titles", action="store_true", help=_("queue 只输出未裁决候选的标题与适用范围"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", help=_("预览返回的 confirmation_sha256"))
    parser.add_argument("--output", choices=("json",), default="json")


def run(args: argparse.Namespace) -> dict[str, Any]:
    root = args.project.resolve()
    roots = tuple(args.occurrence_root) or DEFAULT_ROOTS
    store = confined(root, args.store)
    profiles_root = confined(root, args.profiles_root or ".opinion/profiles")
    state_path = store / "lifecycle.json"
    raw = state_path.read_text(encoding="utf-8") if state_path.exists() else None
    state = json.loads(raw) if raw else deepcopy(EMPTY_STATE)
    evidence_valid(root, state)
    if args.action in ("queue", "status"):
        if args.apply:
            fail(_("只读命令不能使用 --apply"))
        if args.action == "status":
            return status_view(state, root, profiles_root)
        views = sorted((candidate_view(item) for item in state["candidates"].values()), key=lambda item: (-item["priority"], item["id"]))
        if args.titles:
            views = [{"id": item["id"], "title": item["content"]["title"], "scopes": item["content"]["scopes"],
                      "status": item["status"], "distinct_occurrences": item["distinct_occurrences"]}
                     for item in views if item["status"] not in ("accepted", "rejected", "held")]
        return {"ok": True, "status": "read-only", "value": views}
    if not args.file:
        fail(_("event 需要 --file"))
    event_path = confined(root, os.path.relpath(args.file.resolve(), root))
    event = json.loads(event_path.read_text(encoding="utf-8"))
    source = confined(root, event["evidence"])
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    if event.get("evidence_sha256") and event["evidence_sha256"] != source_hash:
        fail(_("事件证据已经变化"))
    event["evidence_sha256"] = source_hash
    event["evidence_origin"] = event["evidence"]
    snapshot = store / "evidence" / f"{source_hash}.blob"
    event["evidence"] = snapshot.relative_to(root).as_posix()
    lock = current_lock(root) if event["kind"] in ("adopt", "use", "formalize") else None
    after = reduce(state, event, lock, roots)
    fingerprint = versioned.digest({"before": state, "after": after, "event": event})
    if args.apply:
        if args.confirm != fingerprint:
            fail(_("确认指纹不一致"))
        store.mkdir(parents=True, exist_ok=True)
        with (store / ".lifecycle.lock").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            live = state_path.read_text(encoding="utf-8") if state_path.exists() else None
            if live != raw:
                fail(_("状态已经变化；请重新生成预览"))
            evidence_valid(root, state)
            if lock is not None and current_lock(root) != lock:
                fail(_("Opinion 在写入期间发生变化；请重新生成预览"))
            if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
                fail(_("证据在预览后发生变化"))
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            if snapshot.exists():
                if snapshot.read_bytes() != source_bytes:
                    fail(_("不可变的证据快照被改动"))
            else:
                with snapshot.open("xb") as handle:
                    handle.write(source_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
            descriptor, temporary = tempfile.mkstemp(prefix=".lifecycle-", dir=store)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(after, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, state_path)
            finally:
                if Path(temporary).exists():
                    Path(temporary).unlink()
    return {"ok": True, "status": "written" if args.apply else "preview", "confirmation_sha256": fingerprint,
            "event": event, "state": after, "writes_stable_rules": False}


def execute(args: argparse.Namespace) -> tuple[int, str]:
    try:
        return 0, versioned.pretty(run(args))
    except (LifecycleError, versioned.OpinionVersionError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        return 2, versioned.pretty({"ok": False, "error": str(exc)})
