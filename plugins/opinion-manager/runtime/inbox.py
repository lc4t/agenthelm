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

import versioned


STORE = Path(".opinion") / "inbox.json"
STATUSES = ("pending", "trial", "formal", "held", "rejected")
DECIDABLE = ("pending", "trial", "formal", "held", "rejected")
OPEN_STATUSES = ("pending", "trial")
EMPTY = {"schema_version": 1, "candidates": {}}
MAX_TITLE, MAX_TEXT, MAX_QUOTE = 120, 1200, 600
RUNTIME = Path(__file__).resolve().with_name("opinion_manager.py")

PROTOCOL = """[Opinion 协议 · agenthelm]
1. 实施前读取项目 OPINION.md（如有）并按其要求工作；下列「试行」候选同样要遵循，交付时说明是否适用。
2. 用户在对话里表达长期偏好、纠正或规则（如“以后都…”“不要再…”“必须…”“记住…”）时，先静默暂存为候选，不打断当前工作：
   python3 {runtime} inbox add --project {project} --title "<标题>" --text "<完整规则>" --scope "<适用范围>" --quote "<用户原话>"{session}
   一次性的任务要求、项目事实和命令不是候选。暂存不需要确认，也不会改变任何规则。
3. 任务完成或即将完成时，若本次暂存了候选，用编号列表一次性请用户裁决：正式采纳 / 项目内试行 / 暂缓 / 丢弃。
   用户明确表态后记录：python3 {runtime} inbox decide <编号…> --project {project} --to trial|formal|held|rejected --confirmed
   采纳为正式只改状态；写入 OPINION.md 须走 opinion-manager 的完整预览与确认。
4. 未经用户确认，不得把候选写入 OPINION.md，也不得把一次出现当作确认。
5. inbox add 的结果带 notice（首次暂存时出现）时，用一句话如实转告用户。收件箱保存用户原话，可能含敏感信息；
   是否公开仓库、是否排除 .opinion/ 由用户决定，不要自行修改 .gitignore、删除或改写内容。"""


EXPOSURE_NOTICE = ("收件箱会把你的原话保存在 .opinion/inbox.json，其中可能含敏感信息。启用本功能的项目建议保持私有；"
                   "若要公开仓库或让他人看到，是否排除 .opinion/ 及其中内容由你决定。本工具不会替你决定，也不会修改 .gitignore。")
EXPOSURE_STATES = {
    "ignored": "当前已被 Git 忽略。",
    "tracked": "当前已被 Git 跟踪，提交和推送会包含它。",
    "not-ignored": "当前未被 Git 忽略，提交时会被包含。",
    "no-git": "当前目录不是 Git 仓库；放在同步盘或共享目录里同样会被他人看到。",
    "unknown": "无法判断 Git 状态。",
}


class InboxError(ValueError):
    pass


def fail(message: str) -> None:
    raise InboxError(message)


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
            fail(f"拒绝使用符号链接：{item}")
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
            "message": f"{EXPOSURE_NOTICE}{EXPOSURE_STATES[state]}"}


def normalized(title: str, text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", "", f"{title}{text}").lower().encode("utf-8")).hexdigest()


def load(root: Path) -> dict[str, Any]:
    path = store_path(root)
    if not path.exists():
        return json.loads(json.dumps(EMPTY))
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), dict):
        fail(f"收件箱格式不正确：{path}")
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
        fail(f"{label}不能为空")
    if len(text) > limit:
        fail(f"{label}超过 {limit} 个字符；请拆成多条候选或精简")
    return text


def next_identity(data: dict[str, Any], moment: datetime) -> str:
    prefix = f"cand-{moment:%Y%m%d}-"
    used = [int(key[len(prefix):]) for key in data["candidates"] if key.startswith(prefix) and key[len(prefix):].isdigit()]
    return f"{prefix}{max(used, default=0) + 1:03d}"


def add(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    title = clean(args.title, "标题", MAX_TITLE)
    text = clean(args.text, "规则正文", MAX_TEXT)
    quote = clean(args.quote, "用户原话", MAX_QUOTE, required=False)
    scopes = [clean(item, "适用范围", MAX_TITLE) for item in args.scope]
    if not scopes:
        fail("候选需要至少一个 --scope（适用范围）")
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
            "history": [{"at": stamp(moment), "to": "pending", "note": "暂存"}],
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
        fail("状态变化需要用户明确表态：先向用户展示候选全文，用户确认后加 --confirmed")
    if args.to not in DECIDABLE:
        fail(f"--to 必须是 {'、'.join(DECIDABLE)} 之一")
    moment = now()
    changed = []
    with Locked(root) as data:
        for identity in args.ids:
            candidate = data["candidates"].get(identity)
            if candidate is None:
                fail(f"没有这个候选：{identity}")
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
    reminder = "正式采纳只记录状态；写入 OPINION.md 须经 opinion-manager 的完整预览与确认。" if args.to == "formal" else ""
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
        fail("--days 必须是正整数")
    moment = now()
    reverted = []
    with Locked(root) as data:
        for candidate in data["candidates"].values():
            if candidate["status"] == "trial" and days_since(candidate["trial_since"], moment) >= args.days:
                candidate["status"] = "pending"
                candidate["trial_since"] = None
                candidate["updated_at"] = stamp(moment)
                candidate["history"].append({"at": stamp(moment), "to": "pending", "note": f"试行超过 {args.days} 天未转正，回退"})
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
            fail(f"未知状态：{item}")
    items = select(load(root), statuses, args.session, args.unpresented)
    if args.output == "markdown":
        if not items:
            return "没有符合条件的候选。\n"
        lines = []
        for number, item in enumerate(items, 1):
            lines.append(f"{number}. **{item['title']}**（{item['id']} · {item['status']}）")
            lines.append(f"   - 规则：{item['text']}")
            lines.append(f"   - 适用：{'、'.join(item['scopes'])}")
            if item["quotes"]:
                lines.append(f"   - 用户原话：{item['quotes'][-1]}")
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
    protocol = PROTOCOL.format(runtime=RUNTIME, project=root,
                               session=f" --session {args.session}" if args.session else "")
    lines = [protocol]
    used = len(protocol)
    shown = []
    if trial:
        lines.append("\n[试行候选 · 用户已同意在本项目试用，尚未正式；按其工作并在交付时说明是否适用]")
        for item in trial:
            entry = f"- {item['id']} {item['title']}：{item['text']}（适用：{'、'.join(item['scopes'])}）"
            if used + len(entry) > args.max_chars:
                lines.append(f"- …另有 {len(trial) - len(shown)} 条试行候选未展示：运行 inbox list --status trial 查看")
                break
            lines.append(entry)
            used += len(entry)
            shown.append(item["id"])
    waiting = len(select(data, ["pending"]))
    if waiting:
        lines.append(f"\n[待确认候选 {waiting} 条，尚未生效；任务结束前可向用户集中确认]")
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


def run(args: argparse.Namespace) -> dict[str, Any] | str:
    root = Path(os.path.abspath(args.project.expanduser()))
    if not root.is_dir():
        fail(f"项目目录不存在：{root}")
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
        fail("需要至少一个候选编号")
    if args.action == "decide":
        if not args.to:
            fail("decide 需要 --to")
        return decide(root, args)
    if args.action == "present":
        return present(root, args)
    if args.days is None:
        fail("expire 需要 --days")
    return expire(root, args)


def execute(args: argparse.Namespace) -> tuple[int, str]:
    try:
        result = run(args)
    except (InboxError, versioned.OpinionVersionError, OSError, json.JSONDecodeError) as exc:
        return 2, versioned.pretty({"ok": False, "error": str(exc)})
    return 0, result if isinstance(result, str) else versioned.pretty(result)
