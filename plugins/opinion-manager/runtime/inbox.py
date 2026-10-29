"""项目私有的候选收件箱：低摩擦暂存 Agent 在工作中发现的观点，从不写入 OPINION.md、Profile 或锁。

收件箱记录的是“待用户裁决的候选”，不是规则。状态只有五种：pending（待确认）、trial（项目内试行）、
formal（用户已同意转正，正文仍须经 opinion-manager 的完整预览与确认写入）、held（暂缓）、rejected（丢弃）。
暂存（add）无需确认；任何状态变化（decide）都必须带 ``--confirmed``，表示用户已看过候选全文并明确表态。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import inbox_text as words
import versioned


STORE = Path(".opinion") / "inbox.json"
STATUSES = ("pending", "trial", "formal", "held", "rejected")
DECIDABLE = ("pending", "trial", "formal", "held", "rejected")
OPEN_STATUSES = ("pending", "trial")
EMPTY = {"schema_version": 1, "candidates": {}}
MAX_TITLE, MAX_TEXT, MAX_QUOTE = 120, 1200, 600
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
            "status": "pending", "digest": digest, "client": args.client or "", "session": args.session or "",
            "created_at": stamp(moment), "updated_at": stamp(moment), "last_action_at": stamp(moment),
            "trial_since": None, "presented": 0, "presented_at": None,
            "history": [{"at": stamp(moment), "to": "pending", "note": "captured"}],
        }
        save(root, data)
    result = {"ok": True, "status": "added", "id": identity, "state": "pending", "writes_stable_rules": False}
    if first:
        notice = exposure(root)
        if notice["needs_notice"]:
            result["notice"] = notice["message"]
    return result


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
    reminder = say("formal_reminder") if args.to == "formal" else ""
    return {"ok": True, "status": "written", "changed": changed, "to": args.to, "reminder": reminder,
            "writes_stable_rules": False}


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
    return {"ok": True, "status": "written", "marked": marked}


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
    return {key: candidate[key] for key in keys}


def select(data: dict[str, Any], statuses: list[str] | None, session: str | None = None,
           unpresented: bool = False) -> list[dict[str, Any]]:
    items = [item for item in data["candidates"].values() if not statuses or item["status"] in statuses]
    if session:
        items = [item for item in items if item["session"] == session]
    if unpresented:
        items = [item for item in items if not item["presented"]]
    return sorted(items, key=lambda item: (item["created_at"], item["id"]))


def listing(root: Path, args: argparse.Namespace) -> dict[str, Any] | str:
    statuses = args.status.split(",") if args.status else None
    for item in statuses or []:
        if item not in STATUSES:
            fail("err_status", status=item)
    items = select(load(root), statuses, args.session, args.unpresented)
    if args.output == "markdown":
        if not items:
            return say("list_empty") + "\n"
        lines = []
        for number, item in enumerate(items, 1):
            lines.append(f"{number}. **{item['title']}** ({item['id']} · {item['status']})")
            lines.append(say("list_rule", text=item["text"]))
            lines.append(say("list_scope", scopes=say("sep").join(item["scopes"])))
            if item["quotes"]:
                lines.append(say("list_quote", quote=item["quotes"][-1]))
        return "\n".join(lines) + "\n"
    return {"ok": True, "status": "read-only", "value": [view(item) for item in items]}


def pending(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    """待确认项提供者：agent-pack 的会话开始检测按此契约汇总各来源（opinion 候选、SOP 待办等）。"""
    data = load(root)
    moment = now()
    open_items = select(data, list(OPEN_STATUSES))
    counts = {status: len(select(data, [status])) for status in STATUSES}
    waiting = [item for item in open_items if item["status"] == "pending"]
    command = f"python3 {RUNTIME} inbox list --project {root} --status pending,trial --output markdown"
    items = [{"id": item["id"], "source": "opinion", "kind": "candidate", "title": item["title"],
              "status": item["status"], "created_at": item["created_at"], "last_action_at": item["last_action_at"],
              "age_days": round(days_since(item["last_action_at"], moment), 1), "hint_command": command}
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
                   session=f" --session {args.session}" if args.session else "")
    lines = [protocol]
    used = len(protocol)
    shown = []
    if trial:
        lines.append(say("brief_trial_header"))
        for item in trial:
            entry = say("brief_trial_entry", id=item["id"], title=item["title"], text=item["text"],
                        scopes=say("sep").join(item["scopes"]))
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
    parser = subparsers.add_parser("inbox", help="项目私有的候选收件箱：暂存、裁决与待确认汇总；不写入规则")
    parser.add_argument("action", choices=("add", "list", "decide", "present", "expire", "pending", "brief", "exposure"))
    parser.add_argument("ids", nargs="*", help="decide / present 的候选编号")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--title")
    parser.add_argument("--text")
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--quote", help="用户原话，作为候选的证据")
    parser.add_argument("--client", help="来源客户端：claude / codex / cursor")
    parser.add_argument("--session", help="来源会话编号；用于任务结束时只确认本次新增的候选")
    parser.add_argument("--status", help="list 的状态过滤，逗号分隔")
    parser.add_argument("--unpresented", action="store_true", help="list 只列还没向用户展示过的")
    parser.add_argument("--to", choices=DECIDABLE)
    parser.add_argument("--confirmed", action="store_true", help="用户已看过候选全文并明确表态")
    parser.add_argument("--note")
    parser.add_argument("--days", type=int, help="expire：试行超过多少天回到待确认")
    parser.add_argument("--max-chars", type=int, default=6000, help="brief 的正文上限")
    parser.add_argument("--output", choices=("json", "markdown"), default="json")
    parser.add_argument("--lang", choices=("auto", *words.LANGUAGES), help="提示语言：zh / en；缺省按 AGENTHELM_LANG、系统 locale 决定，最后用中文")


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
    if args.action in ("decide", "present") and not args.ids:
        fail("err_ids")
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
