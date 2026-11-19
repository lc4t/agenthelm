"""项目私有的候选收件箱：低摩擦暂存 Agent 在工作中发现的观点，从不写入 OPINION.md、Profile 或锁。

收件箱记录的是“待用户裁决的候选”，不是规则。状态只有五种：pending（待确认）、trial（项目内试行）、
formal（用户已同意转正，正文仍须经 opinion-manager 的完整预览与确认写入）、held（暂缓）、rejected（丢弃）。
暂存（add）无需确认；任何状态变化（decide）都必须带 ``--confirmed``，表示用户已看过候选全文并明确表态。
``formal`` 只表示“用户已批准”，尚未写入 OPINION.md；``adopt`` 把“预览 → 用户确认 → 写入 → 读回 → 置 formal”合成一步确认，
内部只调用 versioned 的 profile / compose / verify（含各自的指纹校验），不另开写入路径。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import inbox_text as words
import inbox_relations
import versioned
from rule_lines import (GLOBAL_VALUES, RULE_WORDS, candidate_rule, custom_section_lines, is_global,  # noqa: F401
                        localized_global, parse_line, rule_qualifiers)


STORE = Path(".opinion") / "inbox.json"
STATUSES = ("pending", "trial", "formal", "held", "rejected")
DECIDABLE = ("pending", "trial", "formal", "held", "rejected")
OPEN_STATUSES = ("pending", "trial")
EMPTY = {"schema_version": 1, "candidates": {}}
MAX_TITLE, MAX_TEXT, MAX_QUOTE, MAX_FIELD = 120, 1200, 600, 300
# 本版软必填：缺 trigger / purpose 时 add 仍成功并返回 warnings。置 True 即变为严格拒绝（留给之后的 major）。
STRICT_REQUIRED = False
REQUIRED_FIELDS = ("trigger", "purpose")
OPTIONAL_FIELDS = ("exclude", "relation")
FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS
# 占位词：传入这些值等于没填，一律拒绝（比较前去掉空白与标点并转小写）。
PLACEHOLDERS = frozenset({"所有", "全部", "相关", "一切", "任何", "无", "tbd", "any", "all", "everything", "relevant", "anything", "na", "none"})
RUNTIME = Path(__file__).resolve().with_name("opinion_manager.py")

LANGUAGE = words.DEFAULT_LANGUAGE


class InboxError(ValueError):
    pass


def say(key: str, **fields: Any) -> str:
    return words.text(key, LANGUAGE, **fields)


def fail(key: str, **fields: Any) -> None:
    raise InboxError(say(key, **fields))


def now() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime | None = None) -> str:
    return (moment or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def days_since(value: str | None, moment: datetime | None = None) -> float:
    if not value:
        return 0.0
    return max(0.0, ((moment or now()) - parse_stamp(value)).total_seconds() / 86400)


def store_path(root: Path) -> Path:
    for item in (root / ".opinion", root / STORE):
        if item.is_symlink():
            fail("err_symlink", path=item)
    return root / STORE


def git_status(root: Path) -> str:
    """只读判断 .opinion/inbox.json 在 Git 里的状态；从不修改任何文件。"""
    def git(*arguments: str) -> int | None:
        try:
            return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=False, timeout=5).returncode
        except (OSError, subprocess.SubprocessError):
            return None

    target = STORE.as_posix()
    inside = git("rev-parse", "--is-inside-work-tree")
    if inside is None:
        return "unknown"
    if inside != 0:
        return "no-git"
    if git("ls-files", "--error-unmatch", "--", target) == 0:
        return "tracked"
    ignored = git("check-ignore", "-q", "--", target)
    return "ignored" if ignored == 0 else ("not-ignored" if ignored == 1 else "unknown")


def exposure(root: Path) -> dict[str, Any]:
    state = git_status(root)
    return {"ok": True, "status": "read-only", "git": state, "needs_notice": state != "ignored",
            "message": say("exposure_notice") + say("gap") + say(f"state_{state}")}


def normalized(title: str, text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", "", f"{title}{text}").lower().encode("utf-8")).hexdigest()


def load(root: Path) -> dict[str, Any]:
    path = store_path(root)
    if not path.exists():
        return json.loads(json.dumps(EMPTY))
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), dict):
        fail("err_format", path=path)
    return data


def save(root: Path, data: dict[str, Any]) -> None:
    path = store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".inbox-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


class Locked:
    """读改写一致：同一项目的多个会话同时暂存时不丢失。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def __enter__(self) -> dict[str, Any]:
        directory = store_path(self.root).parent
        directory.mkdir(parents=True, exist_ok=True)
        self.guard = (directory / ".inbox.lock").open("a")
        fcntl.flock(self.guard, fcntl.LOCK_EX)
        self.data = load(self.root)
        return self.data

    def __exit__(self, *_: object) -> None:
        fcntl.flock(self.guard, fcntl.LOCK_UN)
        self.guard.close()


def clean(value: str | None, label: str, limit: int, *, required: bool = True) -> str:
    text = (value or "").strip()
    if required and not text:
        fail("err_empty", label=say(label))
    if len(text) > limit:
        fail("err_too_long", label=say(label), limit=limit)
    return text


def field_value(value: str | None, name: str, limit: int = MAX_FIELD) -> str:
    """可选字段：未传入返回空串；传入了就必须有内容且不是占位词（relation 只要求非空）。"""
    if value is None:
        return ""
    text = clean(value, f"label_{name}", limit)
    if name != "relation" and re.sub(r"[\W_]+", "", text.lower()) in PLACEHOLDERS:
        fail("err_placeholder", label=say(f"label_{name}"), value=text)
    return text


def missing_fields(candidate: dict[str, Any]) -> list[str]:
    return [name for name in REQUIRED_FIELDS if not candidate.get(name)]


def next_identity(data: dict[str, Any], moment: datetime) -> str:
    prefix = f"cand-{moment:%Y%m%d}-"
    used = [int(key[len(prefix):]) for key in data["candidates"] if key.startswith(prefix) and key[len(prefix):].isdigit()]
    return f"{prefix}{max(used, default=0) + 1:03d}"


def add(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    title = clean(args.title, "label_title", MAX_TITLE)
    text = clean(args.text, "label_text", MAX_TEXT)
    quote = clean(args.quote, "label_quote", MAX_QUOTE, required=False)
    scopes = [clean(item, "label_scope", MAX_TITLE) for item in args.scope]
    if not scopes:
        fail("err_scope")
    extras = {name: field_value(getattr(args, name, None), name) for name in FIELDS}
    if is_global(extras["trigger"]) and not quote:
        fail("err_global_quote")
    absent = [name for name in REQUIRED_FIELDS if not extras[name]]
    if absent and STRICT_REQUIRED:
        fail("err_missing_fields", fields=say("sep").join(say(f"label_{name}") for name in absent))
    moment = now()
    first = not store_path(root).exists()
    with Locked(root) as data:
        digest = normalized(title, text)
        for existing in data["candidates"].values():
            if existing["digest"] == digest:
                if args.quote and args.quote.strip() and args.quote.strip() not in existing["quotes"]:
                    existing["quotes"].append(args.quote.strip())
                    existing["updated_at"] = stamp(moment)
                    save(root, data)
                return {"ok": True, "status": "duplicate", "id": existing["id"], "state": existing["status"]}
        identity = next_identity(data, moment)
        data["candidates"][identity] = {
            "id": identity, "title": title, "text": text, "scopes": scopes, "quotes": [quote] if quote else [],
            **extras, "status": "pending", "digest": digest, "client": args.client or "", "session": args.session or "",
            "created_at": stamp(moment), "updated_at": stamp(moment), "last_action_at": stamp(moment),
            "trial_since": None, "presented": 0, "presented_at": None,
            "history": [{"at": stamp(moment), "to": "pending", "note": "captured"}],
        }
        save(root, data)
    result = {"ok": True, "status": "added", "id": identity, "state": "pending", "writes_stable_rules": False,
              "complete": not absent, "warnings": [say("warn_missing_fields", fields=say("sep").join(say(f"label_{name}") for name in absent))] if absent else []}
    if first:
        notice = exposure(root)
        if notice["needs_notice"]:
            result["notice"] = notice["message"]
    return result


def revise(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    """修订候选内容。pending 且从未展示（presented==0）时是 Agent 自己的暂存修正，无需确认；其他状态必须 --confirmed。
    旧值写进 history，摘要重算，与其他候选撞摘要时报错；状态不变。"""
    if len(args.ids) != 1:
        fail("err_revise_one")
    changes: dict[str, Any] = {}
    if args.title is not None:
        changes["title"] = clean(args.title, "label_title", MAX_TITLE)
    if args.text is not None:
        changes["text"] = clean(args.text, "label_text", MAX_TEXT)
    if args.scope:
        changes["scopes"] = [clean(item, "label_scope", MAX_TITLE) for item in args.scope]
    for name in FIELDS:
        if getattr(args, name, None) is not None:
            changes[name] = field_value(getattr(args, name), name)
    quote = clean(args.quote, "label_quote", MAX_QUOTE, required=False)
    if not changes and not quote:
        fail("err_revise_nothing")
    moment = now()
    with Locked(root) as data:
        candidate = data["candidates"].get(args.ids[0])
        if candidate is None:
            fail("err_missing", id=args.ids[0])
        if not (candidate["status"] == "pending" and not candidate["presented"]) and not args.confirmed:
            fail("err_revise_confirm", status=candidate["status"])
        previous = {name: candidate.get(name, [] if name == "scopes" else "") for name, value in changes.items()
                    if candidate.get(name, [] if name == "scopes" else "") != value}
        quotes = list(candidate["quotes"]) + ([quote] if quote and quote not in candidate["quotes"] else [])
        if quotes != candidate["quotes"]:
            previous["quotes"] = list(candidate["quotes"])
        if not previous:
            return {"ok": True, "status": "unchanged", "id": candidate["id"], "changed": [], "candidate": view(candidate), "warnings": []}
        merged = {**candidate, **{name: value for name, value in changes.items() if name in previous}, "quotes": quotes}
        digest = normalized(merged["title"], merged["text"])
        for other in data["candidates"].values():
            if other["id"] != candidate["id"] and other["digest"] == digest:
                fail("err_revise_collision", id=other["id"])
        if is_global(merged.get("trigger") or "") and not quotes:
            fail("err_global_quote")
        if STRICT_REQUIRED and missing_fields(merged):
            fail("err_missing_fields", fields=say("sep").join(say(f"label_{name}") for name in missing_fields(merged)))
        candidate.update({name: merged[name] for name in previous if name != "quotes"})
        candidate["quotes"], candidate["digest"] = quotes, digest
        candidate["updated_at"] = stamp(moment)
        if args.confirmed:
            candidate["last_action_at"] = stamp(moment)
        candidate["history"].append({"at": stamp(moment), "to": candidate["status"], "note": "revised", "previous": previous})
        save(root, data)
        shown = with_relations(candidate, inbox_relations.targets(root, data))
    warnings = []
    if candidate["status"] == "formal":
        warnings.append(say("warn_revise_formal"))
    if missing_fields(candidate):
        warnings.append(say("warn_missing_fields", fields=say("sep").join(say(f"label_{name}") for name in missing_fields(candidate))))
    return {"ok": True, "status": "written", "id": candidate["id"], "changed": sorted(previous), "candidate": shown,
            "writes_stable_rules": False, "warnings": warnings}


def check(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    """只读：列出缺条件 / 目的的候选（pending、trial）与 OPINION.md 自定义行；认不出格式的行标「未结构化」。不写任何文件。"""
    data = load(root)
    candidates = [{"id": item["id"], "title": item["title"], "status": item["status"], "text": item["text"], "scopes": item["scopes"],
                   "trigger": item.get("trigger") or "", "purpose": item.get("purpose") or "",
                   "missing": missing_fields(item), "revise_command": revise_command(root, item["id"])}
                  for item in select(data, list(OPEN_STATUSES)) if missing_fields(item)]
    lines = []
    path = root / "OPINION.md"
    content = versioned.read_text(path) if path.exists() else ""
    for number, line in custom_section_lines(content):
        parsed = parse_line(line)
        if parsed is None:
            lines.append({"line": number, "format": "unstructured", "title": "", "missing": [], "text": inbox_relations.snippet(line)})
        elif missing_fields(parsed):
            lines.append({"line": number, "format": parsed["format"], "title": parsed["title"], "missing": missing_fields(parsed),
                          "text": inbox_relations.snippet(parsed["text"])})
    return {"ok": True, "status": "read-only", "candidates": candidates, "custom_lines": lines,
            "summary": {"candidates_incomplete": len(candidates), "custom_lines_incomplete": sum(1 for item in lines if item["format"] != "unstructured"),
                        "custom_lines_unstructured": sum(1 for item in lines if item["format"] == "unstructured")},
            "next_step": say("check_next") if candidates or lines else say("check_clean"), "writes_stable_rules": False}


def revise_command(root: Path, identity: str) -> str:
    return f"python3 {shlex.quote(str(RUNTIME))} inbox revise {shlex.quote(identity)} --project {shlex.quote(str(root))} --trigger \"<…>\" --purpose \"<…>\" --lang {LANGUAGE}"


def decide(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    if not args.confirmed:
        fail("err_confirm")
    if args.to not in DECIDABLE:
        fail("err_to", choices=say("sep").join(DECIDABLE))
    moment = now()
    changed = []
    with Locked(root) as data:
        for identity in args.ids:
            candidate = data["candidates"].get(identity)
            if candidate is None:
                fail("err_missing", id=identity)
        for identity in args.ids:
            candidate = data["candidates"][identity]
            if candidate["status"] == args.to:
                continue
            candidate["status"] = args.to
            candidate["updated_at"] = candidate["last_action_at"] = stamp(moment)
            candidate["trial_since"] = stamp(moment) if args.to == "trial" else None
            candidate["history"].append({"at": stamp(moment), "to": args.to, "note": (args.note or "").strip()})
            changed.append(identity)
        save(root, data)
    result = {"ok": True, "status": "written", "changed": changed, "to": args.to, "reminder": "",
              "writes_stable_rules": False, "candidates": [view(data["candidates"][identity]) for identity in args.ids]}
    if args.to == "formal":
        result["reminder"] = say("formal_reminder")
        result["in_opinion"] = False
        result["next_step"] = say("formal_next", command=adopt_command(root, list(args.ids)))
    return result


def adopt_command(root: Path, ids: list[str], confirm: str | None = None) -> str:
    tail = f" --apply --confirm {confirm}" if confirm else ""
    return f"python3 {shlex.quote(str(RUNTIME))} inbox adopt {' '.join(shlex.quote(item) for item in ids)} --project {shlex.quote(str(root))}{tail} --lang {LANGUAGE}"


DEFAULT_PROFILE = "private.my-opinion"


def fingerprint_input(item: dict[str, Any]) -> list[Any]:
    """采纳指纹里每条候选的输入；没有新字段的旧记录与旧版指纹一致。"""
    base = [item["title"], item["text"], item["scopes"]]
    extras = [item.get(name) or "" for name in ("trigger", "purpose", "exclude")]
    return [item["id"], versioned.digest(base + extras if any(extras) else base)]


def raw(path: Path) -> bytes | None:
    versioned.safe_path(path)
    return path.read_bytes() if path.exists() else None


def restore(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def adoptable(data: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
    missing = [item for item in ids if item not in data["candidates"]]
    if missing:
        fail("err_missing", id=say("sep").join(missing))
    done = [item for item in ids if data["candidates"][item]["status"] == "formal"]
    if done:
        fail("err_adopt_formal", ids=say("sep").join(done))
    return [data["candidates"][item] for item in ids]


def adopt_plan(root: Path, items: list[dict[str, Any]], workdir: Path) -> dict[str, Any]:
    """只读：与 profile / compose 的预览同一套函数，返回写入所需的全部输入与各自指纹。"""
    workdir = workdir.resolve()  # versioned.safe_path 拒绝含符号链接的路径；macOS 的临时目录在 /var -> /private/var 之下
    lock_path = root / "opinion.lock.json"
    current_lock = versioned.file_state(lock_path)
    previous = versioned.validate_lock(json.loads(current_lock))["profile"] if current_lock is not None else None
    language = versioned.project_language(root)
    custom = "\n\n".join(part for part in ((previous or {}).get("custom"), "\n".join(candidate_rule(item, language) for item in items)) if part)
    custom_file = workdir / "custom.md"
    custom_file.write_text(custom + "\n", encoding="utf-8")
    identity, release = (previous["id"], previous["version"]) if previous else (DEFAULT_PROFILE, "0.0.0")
    major, minor, patch = versioned.version(release)
    while True:
        patch += 1
        number = f"{major}.{minor}.{patch}" if previous else "1.0.0"
        target = root / ".opinion" / "profiles" / identity / "versions" / f"{number}.json"
        if not target.exists():
            break
    from_profile = None
    if previous:
        from_profile = workdir / "previous-profile.json"
        from_profile.write_text(versioned.pretty(previous), encoding="utf-8")
    profile_args = Namespace(project=root, id=identity, version=number, template=[], rule=[], remove_template=[], remove_rule=[],
                             custom_file=custom_file, from_profile=from_profile, overrides_file=None, resolutions_file=None,
                             loading_file=None, remove_loading=False, catalog=versioned.PUBLIC_CATALOG, apply=False, confirm=None)
    drafted = versioned.profile_command(profile_args)
    if drafted["unresolved"]:
        fail("err_adopt_unresolved", ids=say("sep").join(drafted["unresolved"]))
    preview_file = workdir / "profile.json"
    preview_file.write_text(versioned.pretty(drafted["profile"]), encoding="utf-8")
    compose_args = Namespace(project=root, profile=preview_file, template=[], rule=[], custom_file=None, replace=False,
                             accept_current=None, apply=False, confirm=None)
    composed = versioned.compose_command(compose_args)
    fingerprint = versioned.digest({"candidates": [fingerprint_input(item) for item in items],
                                    "profile": drafted["confirmation_sha256"], "compose": composed["confirmation_sha256"]})
    return {"profile_args": profile_args, "compose_args": compose_args, "drafted": drafted, "composed": composed,
            "fingerprint": fingerprint, "items": items, "target": target, "profile_id": f"{identity}@{number}", "preview_file": preview_file}


def adopt(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    ids = list(dict.fromkeys(args.ids))
    if not args.apply:
        with tempfile.TemporaryDirectory(prefix="opinion-adopt-") as workdir:
            plan = adopt_plan(root, adoptable(load(root), ids), Path(workdir))
        composed = plan["composed"]
        return {"ok": True, "status": "preview", "ids": ids, "profile": plan["profile_id"], "profile_path": str(plan["target"]),
                "path": composed["path"], "content": composed["content"], "blocked": composed["blocked"],
                "confirmation_sha256": plan["fingerprint"], "writes_stable_rules": False,
                "candidates": [view(item) for item in plan["items"]],
                "next_step": say("adopt_next", command=adopt_command(root, ids, plan["fingerprint"]))}
    if not args.confirm:
        fail("err_adopt_confirm")
    destination, lock_path = root / "OPINION.md", root / "opinion.lock.json"
    with Locked(root) as data, tempfile.TemporaryDirectory(prefix="opinion-adopt-") as workdir:
        items = adoptable(data, ids)
        plan = adopt_plan(root, items, Path(workdir))
        if plan["fingerprint"] != args.confirm:
            fail("err_adopt_stale", fingerprint=plan["fingerprint"])
        if plan["composed"]["blocked"]:
            raise InboxError(plan["composed"]["blocked"])
        before = {destination: raw(destination), lock_path: raw(lock_path)}
        target = plan["target"]
        created = []
        for directory in (target.parent, target.parent.parent, target.parent.parent.parent):
            if not directory.exists():
                created.append(directory)
        existed = target.exists()
        try:
            versioned.profile_command(Namespace(**{**vars(plan["profile_args"]), "apply": True, "confirm": plan["drafted"]["confirmation_sha256"]}))
            versioned.compose_command(Namespace(**{**vars(plan["compose_args"]), "profile": target, "apply": True,
                                                   "confirm": plan["composed"]["confirmation_sha256"]}))
            verified = versioned.verify_project(root)
            if destination.read_text(encoding="utf-8") != plan["composed"]["content"]:
                fail("err_adopt_readback")
            moment = now()
            for item in items:
                item["status"] = "formal"
                item["updated_at"] = item["last_action_at"] = stamp(moment)
                item["trial_since"] = None
                item["history"].append({"at": stamp(moment), "to": "formal", "note": f"adopted:{plan['profile_id']}"})
            save(root, data)
        except BaseException:
            for path, content in before.items():
                restore(path, content)
            if not existed:
                target.unlink(missing_ok=True)
            for directory in created:
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise
    return {"ok": True, "status": "written", "ids": ids, "changed": ids, "profile": plan["profile_id"], "path": str(destination),
            "verified": verified, "writes_stable_rules": True, "message": say("adopt_done", count=len(ids))}


def present(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    moment = now()
    marked = []
    with Locked(root) as data:
        for identity in args.ids:
            candidate = data["candidates"].get(identity)
            if candidate is not None:
                candidate["presented"] += 1
                candidate["presented_at"] = stamp(moment)
                marked.append(identity)
        save(root, data)
    pool = inbox_relations.targets(root, data) if marked else []
    return {"ok": True, "status": "written", "marked": marked, "candidates": [with_relations(data["candidates"][identity], pool) for identity in marked]}


def expire(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    """试行超过天数未转正则回到待确认。回退只改状态，仍保留 last_action_at，因此计入“久未处理”。"""
    if args.days < 1:
        fail("err_days")
    moment = now()
    reverted = []
    with Locked(root) as data:
        for candidate in data["candidates"].values():
            if candidate["status"] == "trial" and days_since(candidate["trial_since"], moment) >= args.days:
                candidate["status"] = "pending"
                candidate["trial_since"] = None
                candidate["updated_at"] = stamp(moment)
                candidate["history"].append({"at": stamp(moment), "to": "pending", "note": f"trial-expired:{args.days}d"})
                reverted.append(candidate["id"])
        if reverted:
            save(root, data)
    return {"ok": True, "status": "written" if reverted else "unchanged", "reverted": reverted}


def view(candidate: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "text", "scopes", "quotes", "status", "created_at", "updated_at", "last_action_at",
            "trial_since", "presented", "presented_at", "session", "client")
    shown = {key: candidate[key] for key in keys}
    shown.update({name: candidate.get(name) or "" for name in FIELDS})
    shown["complete"] = not missing_fields(candidate)
    shown["global"] = is_global(candidate.get("trigger") or "")
    return shown


def select(data: dict[str, Any], statuses: list[str] | None, session: str | None = None,
           unpresented: bool = False) -> list[dict[str, Any]]:
    items = [item for item in data["candidates"].values() if not statuses or item["status"] in statuses]
    if session:
        items = [item for item in items if item["session"] == session]
    if unpresented:
        items = [item for item in items if not item["presented"]]
    return sorted(items, key=lambda item: (item["created_at"], item["id"]))


def field_lines(item: dict[str, Any]) -> list[str]:
    """列表里的条件 / 目的 / 不适用 / 关系备注；缺必填项时标「不完整」，全局声明标「全局（请用户确认）」。"""
    lines = []
    for name in FIELDS:
        value = item.get(name) or ""
        if value:
            mark = say("global_mark") if name == "trigger" and is_global(value) else ""
            lines.append(say(f"list_{name}", value=value, mark=mark))
    missing = missing_fields(item)
    if missing:
        lines.append(say("list_incomplete", fields=say("sep").join(say(f"label_{name}") for name in missing)))
    return lines


def with_relations(item: dict[str, Any], pool: list[dict[str, str]]) -> dict[str, Any]:
    shown = view(item)
    shown["relations"] = [{**entry, "note": say("relations_note")} for entry in inbox_relations.relations_for(item, pool)]
    return shown


def relation_lines(relations: list[dict[str, Any]]) -> list[str]:
    return [say("list_relations", kind=say(f"kind_{entry['kind']}"), ref=entry["with"]["ref"], evidence=entry["evidence"]) for entry in relations]


def brief_qualifiers(item: dict[str, Any]) -> str:
    """试行条目括号内的适用范围，后接条件 / 目的 / 不适用；跟随界面语言。"""
    parts = [say("sep").join(item["scopes"])]
    for name in ("trigger", "purpose", "exclude"):
        if item.get(name):
            parts.append(say(f"brief_{name}", value=item[name]))
    return say("brief_join").join(parts)


def listing(root: Path, args: argparse.Namespace) -> dict[str, Any] | str:
    statuses = args.status.split(",") if args.status else None
    for item in statuses or []:
        if item not in STATUSES:
            fail("err_status", status=item)
    data = load(root)
    items = select(data, statuses, args.session, args.unpresented)
    pool = inbox_relations.targets(root, data) if items else []
    if args.output == "markdown":
        if not items:
            return say("list_empty") + "\n"
        lines = []
        for number, item in enumerate(items, 1):
            lines.append(f"{number}. **{item['title']}** ({item['id']} · {item['status']})")
            lines.append(say("list_rule", text=item["text"]))
            lines.append(say("list_scope", scopes=say("sep").join(item["scopes"])))
            lines.extend(field_lines(item))
            lines.extend(relation_lines(inbox_relations.relations_for(item, pool)))
            if item["quotes"]:
                lines.append(say("list_quote", quote=item["quotes"][-1]))
        return "\n".join(lines) + "\n"
    return {"ok": True, "status": "read-only", "value": [with_relations(item, pool) for item in items]}


def pending(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    """待确认项提供者：agent-pack 的会话开始检测按此契约汇总各来源（opinion 候选、SOP 待办等）。"""
    data = load(root)
    moment = now()
    open_items = select(data, list(OPEN_STATUSES))
    counts = {status: len(select(data, [status])) for status in STATUSES}
    waiting = [item for item in open_items if item["status"] == "pending"]
    command = f"python3 {shlex.quote(str(RUNTIME))} inbox list --project {shlex.quote(str(root))} --status pending,trial --output markdown"
    items = [{"id": item["id"], "source": "opinion", "kind": "candidate", "title": item["title"],
              "status": item["status"], "created_at": item["created_at"], "last_action_at": item["last_action_at"],
              "age_days": round(days_since(item["last_action_at"], moment), 1), "hint_command": command,
              **{name: item.get(name) or "" for name in FIELDS}, "complete": not missing_fields(item)}
             for item in open_items]
    session_items = select(data, ["pending"], args.session, True) if args.session else []
    last_actions = [item["last_action_at"] for item in data["candidates"].values()]
    return {"ok": True, "status": "read-only", "source": "opinion", "items": items, "counts": counts,
            "oldest_pending_days": round(max((days_since(item["last_action_at"], moment) for item in waiting), default=0.0), 1),
            "last_action_at": max(last_actions) if last_actions else None,
            "session_unpresented": [item["id"] for item in session_items]}


def brief(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    data = load(root)
    trial = select(data, ["trial"])
    protocol = say("protocol", runtime=RUNTIME, project=root, lang=LANGUAGE,
                   session=words.session_flag(args.session))
    lines = [protocol]
    used = len(protocol)
    shown = []
    if trial:
        lines.append(say("brief_trial_header"))
        for item in trial:
            entry = say("brief_trial_entry", id=item["id"], title=item["title"], text=item["text"],
                        scopes=brief_qualifiers(item))
            if used + len(entry) > args.max_chars:
                lines.append(say("brief_trial_more", count=len(trial) - len(shown)))
                break
            lines.append(entry)
            used += len(entry)
            shown.append(item["id"])
    waiting = len(select(data, ["pending"]))
    if waiting:
        lines.append(say("brief_pending", count=waiting))
    return {"ok": True, "status": "read-only", "text": "\n".join(lines), "trial_shown": shown,
            "pending": waiting, "trial": len(trial)}


def add_command(subparsers: Any) -> None:
    parser = subparsers.add_parser("inbox", help="Project-private candidate inbox: stash, decide and summarize pending items; only adopt --apply writes OPINION.md, through the full preview and fingerprint")
    parser.add_argument("action", choices=("add", "list", "decide", "adopt", "present", "expire", "pending", "brief", "exposure", "check", "revise"))
    parser.add_argument("ids", nargs="*", help="candidate ids for decide / adopt / present")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--title")
    parser.add_argument("--text")
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--quote", help="the user's own words, kept as evidence for the candidate (revise: appended)")
    parser.add_argument("--trigger", help="add: when the rule applies (required in spirit: omitting it only warns; placeholders such as 'all' are rejected)")
    parser.add_argument("--purpose", help="add: why the rule exists (omitting it only warns)")
    parser.add_argument("--exclude", help="add: when the rule does not apply")
    parser.add_argument("--relation", help="add: the agent's own note on how this relates to existing rules; stored and shown verbatim, never interpreted")
    parser.add_argument("--client", help="source client: claude / codex / cursor")
    parser.add_argument("--session", help="source session id; lets the end-of-task confirmation cover only this session's candidates")
    parser.add_argument("--status", help="list: comma-separated status filter")
    parser.add_argument("--unpresented", action="store_true", help="list: only candidates not yet shown to the user")
    parser.add_argument("--to", choices=DECIDABLE)
    parser.add_argument("--confirmed", action="store_true", help="the user has seen the full candidate and stated a decision explicitly")
    parser.add_argument("--note")
    parser.add_argument("--apply", action="store_true", help="adopt: write OPINION.md and the lock (needs --confirm); without it adopt only previews")
    parser.add_argument("--confirm", help="adopt: the confirmation_sha256 of the preview the user has reviewed")
    parser.add_argument("--days", type=int, help="expire: revert trials older than this many days to pending")
    parser.add_argument("--max-chars", type=int, default=6000, help="brief: size limit of the text")
    parser.add_argument("--output", choices=("json", "markdown"), default="json")
    parser.add_argument("--lang", choices=("auto", *words.LANGUAGES), help="message language: zh / en; by default AGENTHELM_LANG, then the system locale, then English")


def run(args: argparse.Namespace) -> dict[str, Any] | str:
    global LANGUAGE
    LANGUAGE = words.resolve_language(getattr(args, "lang", None))
    root = Path(os.path.abspath(args.project.expanduser()))
    if not root.is_dir():
        fail("err_project", path=root)
    if args.action == "add":
        return add(root, args)
    if args.action == "list":
        return listing(root, args)
    if args.action == "pending":
        return pending(root, args)
    if args.action == "brief":
        return brief(root, args)
    if args.action == "exposure":
        return exposure(root)
    if args.action == "check":
        return check(root, args)
    if args.action in ("decide", "adopt", "present", "revise") and not args.ids:
        fail("err_ids")
    if args.action == "adopt":
        return adopt(root, args)
    if args.action == "revise":
        return revise(root, args)
    if args.action == "decide":
        if not args.to:
            fail("err_need_to")
        return decide(root, args)
    if args.action == "present":
        return present(root, args)
    if args.days is None:
        fail("err_need_days")
    return expire(root, args)


def execute(args: argparse.Namespace) -> tuple[int, str]:
    try:
        result = run(args)
    except (InboxError, versioned.OpinionVersionError, OSError, json.JSONDecodeError) as exc:
        return 2, versioned.pretty({"ok": False, "error": str(exc)})
    return 0, result if isinstance(result, str) else versioned.pretty(result)
