"""opinion_manager.py migrate：把用户现有的 Profile（2.0 / 2.1）迁移到带标签声明的 2.2，契约见 docs/design/opinion-progressive-loading.md 第 7 节。

默认只读预览（零写入，失败路径也一样）；``--apply --confirm <指纹>`` 才写入：先备份到 ``.opinion/backups/<时间戳>-migrate/``，
再复用 ``profile`` → ``compose`` → ``verify`` 的原子写与读回，任一步失败按备份逐字节还原；``--rollback <备份目录>`` 只在当前状态
等于迁移后状态时还原。用户的 ``custom`` 段与手工追加的内容一律保留：没有标签标记的行永远常驻，迁移不替用户猜标签。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import shlex
import shutil
import tempfile
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import inbox
import rule_lines
import tags
import versioned
from constants import KNOWN_PLACEHOLDERS
from i18n_opinion import _, N_  # noqa: E402


EXIT_OK, EXIT_ERROR, EXIT_CANNOT, EXIT_REFUSED, EXIT_FAILED = 0, 2, 3, 4, 5
BACKUPS = Path(".opinion") / "backups"
MANIFEST = "manifest.json"
RUNTIME = Path(__file__).resolve().with_name("opinion_manager.py")
EXCERPT = 80
# 状态：preview / migrated / unchanged / nothing-to-migrate / rolled-back 退出 0；cannot-migrate 为 3；refused 为 4；failed 为 5。
STATUS_EXIT = {"cannot-migrate": EXIT_CANNOT, "refused": EXIT_REFUSED, "failed": EXIT_FAILED}


class Stop(Exception):
    """迁移在某一步停下：状态码、原因代码、说明与下一步命令；除 failed 外不改动任何文件。"""

    def __init__(self, status: str, reason: str, message: str, steps: list[str] | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.reason, self.message, self.steps, self.extra = status, reason, message, steps or [], extra


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def command(root: Path, *words: str) -> str:
    return " ".join(["python3", shlex.quote(str(RUNTIME)), "migrate", "--project", shlex.quote(str(root)), *words])


def guarded(action: Any, reason: str, hint: str) -> Any:
    """读取阶段的 I/O 与格式错误统一为 cannot-migrate，附原因。"""
    try:
        return action()
    except (versioned.OpinionVersionError, OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise Stop("cannot-migrate", reason, _("{0}：{1}").format(hint, exc)) from exc


# ---------------------------------------------------------------- 读取（只读）

def read_state(root: Path) -> dict[str, Any]:
    """读取锁与正文；没有锁且正文空白（或初始化占位）返回 blank，其余无法迁移的形态抛 Stop。"""
    if not root.is_dir():
        raise Stop("cannot-migrate", "no-project", _("项目目录不存在：{0}").format(root))
    lock_path, opinion_path = root / "opinion.lock.json", root / "OPINION.md"
    has_lock = lock_path.exists() or lock_path.is_symlink()
    current = guarded(lambda: versioned.file_state(opinion_path), "opinion-unreadable", _("OPINION.md 不可读"))  # 含符号链接与非普通文件的拒绝
    raw = opinion_path.read_bytes() if current is not None else b""
    if not has_lock:
        if current is None or not current.strip() or current in KNOWN_PLACEHOLDERS:
            return {"blank": True, "current": current}
        raise Stop("cannot-migrate", "no-lock", _("项目没有 opinion.lock.json，OPINION.md 又含有内容，无法确定它来自哪个 Profile；迁移不会猜测"),
                   [_("用 profile 预览、确认后写入一个 Profile，再用 compose --profile 生成带锁的 OPINION.md（选择模板或把现有内容放进 --custom-file）；完成后重新运行 migrate")])
    guarded(lambda: versioned.safe_path(lock_path), "lock-unreadable", _("锁文件不可读"))
    lock = guarded(lambda: versioned.validate_lock(versioned.read_object(lock_path)), "lock-invalid", _("锁文件或 Profile 无效"))
    if current is None:
        raise Stop("cannot-migrate", "opinion-missing", _("OPINION.md 不存在，但项目有锁文件；先用锁里的 Profile 重建正文"),
                   [_("python3 {0} compose --project {1} --profile <锁里 Profile 的文件> --apply --confirm <预览指纹>").format(shlex.quote(str(RUNTIME)), shlex.quote(str(root)))])
    return {"blank": False, "lock": lock, "current": current, "raw": raw, "lock_language": lock.get("language", "zh"),
            "lock_bytes": lock_path.read_bytes()}


def summary(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """loading 的前后对照用摘要；完整声明见 changes.loading。"""
    loading = (profile or {}).get("loading")
    if loading is None:
        return None
    if tags.is_tag_profile(profile or {}):
        return {"kind": "tags", "always": len(loading["always"]), "tags": [item["id"] for item in loading["tags"]], "on_demand_rules": len(loading["members"])}
    return {"kind": "bundles", "core": list(loading["core"]), "bundles": [item["family"] for item in loading["bundles"]]}


# ---------------------------------------------------------------- 手工修改：只接受能放进 custom 的改动

def manual_edits(expected: str, current: str, profile: dict[str, Any], language: str) -> dict[str, Any]:
    """比较「按锁渲染的正文」与当前正文。自定义规则小节内的任何改动、其余位置的纯追加都并入新的 custom（保持原样，常驻）；
    改写或删除了生成内容（规则正文、元数据）无法放进 custom，抛 Stop。"""
    before, after = expected.splitlines(), current.splitlines()
    heading = versioned.FRAME[language]["custom"]
    start = None
    if profile["custom"]:
        candidate = len(before) - len(profile["custom"].split("\n"))
        if candidate >= 2 and before[candidate - 2] == heading and before[candidate - 1] == "":
            start = candidate
    inside: list[str] = []
    extra: list[list[str]] = []
    rejected: list[tuple[int, str]] = []
    edited = 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if op == "equal":
            if start is not None and i2 > start:
                inside.extend(before[max(i1, start):i2])
            continue
        in_custom = start is not None and i1 >= start
        if op == "insert" and not in_custom:
            extra.append(after[j1:j2])
        elif in_custom:
            inside.extend(after[j1:j2])
            edited += sum(1 for line in after[j1:j2] if line.strip())
        else:
            rejected.append((i1 + 1, before[i1] if i1 < len(before) else ""))
    if rejected:
        shown = _("；").join(_("第 {0} 行：{1}").format(number, text[:EXCERPT]) for number, text in rejected[:3])
        raise Stop("cannot-migrate", "manual-edit-conflict",
                   _("OPINION.md 与锁不一致，且改动落在生成内容里（改写或删除了规则正文或元数据），无法并入自定义规则：{0}").format(shown),
                   [_("把改动写成自定义规则（放在“用户自定义规则”小节，或用 inbox add / adopt），或还原被改动的生成内容，再重新运行 migrate；规则本身的修订用 profile --overrides-file")],
                   conflicts=[{"line": number, "text": text} for number, text in rejected])
    appended = [_trim(hunk) for hunk in extra if any(line.strip() for line in hunk)]
    parts = [_trim(inside)] if any(line.strip() for line in inside) else []
    custom = "\n\n".join(["\n".join(part) for part in parts + appended]).strip() or None
    lines = [line for hunk in appended for line in hunk if line.strip()]
    return {"custom": custom, "appended_lines": len(lines), "edited_in_custom": edited, "appended": lines}


def _trim(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


# ---------------------------------------------------------------- 分类

def classify_custom(profile: dict[str, Any], content: str, language: str) -> dict[str, Any]:
    """custom 段逐行归类：tagged（已带有效标签）/ suggest（有条件的结构化行，可手工加标签）/ resident（常驻，不改）。"""
    if not profile["custom"]:
        return {"lines": [], "tagged": 0, "suggested": 0, "resident": 0, "available_tags": []}
    vocabulary = [item["id"] for item in profile["loading"]["tags"]] if tags.is_tag_profile(profile) else []
    owners = {}
    if vocabulary:
        for index, (_line, tag_set) in enumerate(tags.split_custom(profile)[0]):
            owners[index] = tag_set
    rendered = content.splitlines()
    first = None
    heading = versioned.FRAME[language]["custom"]
    for number, line in enumerate(rendered):
        if line == heading:
            first = number + 2
    entries: list[dict[str, Any]] = []
    for index, line in enumerate(profile["custom"].split("\n")):
        if not line.strip():
            continue
        parsed = rule_lines.parse_line(line)
        owned = owners.get(index, ())
        if owned:
            category = "tagged"
        elif vocabulary and parsed and parsed.get("trigger") and not parsed.get("tags") and not parsed.get("tags_error"):
            category = "suggest"
        else:
            category = "resident"
        entry = {"line": (first + index + 1) if first is not None else None, "category": category, "text": line[:EXCERPT]}
        if owned:
            entry["tags"] = list(owned)
        if category == "resident" and parsed and (parsed.get("tags") or parsed.get("tags_error")):
            entry["note"] = "invalid-tags"
        entries.append(entry)
    count = lambda name: sum(1 for item in entries if item["category"] == name)  # noqa: E731
    return {"lines": entries, "tagged": count("tagged"), "suggested": count("suggest"), "resident": count("resident"), "available_tags": vocabulary}


def inbox_report(root: Path) -> dict[str, Any]:
    """收件箱只读：列出缺 trigger / purpose 的旧候选（不改写、不阻断），并给出文件指纹供确认指纹使用。"""
    path = root / inbox.STORE
    try:
        raw = path.read_bytes() if path.exists() and not path.is_symlink() else None
        data = inbox.load(root)
    except (inbox.InboxError, OSError, ValueError, UnicodeDecodeError) as exc:
        return {"readable": False, "error": str(exc), "incomplete": [], "sha256": None}
    incomplete = [{"id": identity, "title": item.get("title", ""), "status": item.get("status"), "missing": inbox.missing_fields(item)}
                  for identity, item in sorted(data["candidates"].items()) if item.get("status") != "formal" and inbox.missing_fields(item)]
    return {"readable": True, "incomplete": incomplete, "sha256": sha(raw) if raw is not None else None}


# ---------------------------------------------------------------- 计划（只读）

def plan_for(root: Path, args: argparse.Namespace, workdir: Path) -> dict[str, Any]:
    state = read_state(root)
    if state["blank"]:
        return {"result": {"status": "nothing-to-migrate", "reason": "blank", "message": _("没有可迁移的内容：OPINION.md 不存在或只是初始化出来的空白文件")}, "actionable": False}
    lock, current = state["lock"], state["current"]
    old, language = lock["profile"], versioned.project_language(root)
    base: dict[str, Any] = {
        "profile": {"id": old["id"], "version": old["version"], "schema_version": old["schema_version"], "templates": old["templates"], "rules": old["rules"]},
        "inbox": inbox_report(root),
        "loading": {"before": summary(old), "after": summary(old)},
        "opinion_md": {"modified": versioned.text_digest(current) != lock["opinion_sha256"], "sha256": versioned.text_digest(current), "lock_sha256": lock["opinion_sha256"],
                       "crlf": b"\r\n" in state["raw"]},
        "notes": [],
    }
    if state["raw"] and b"\r\n" in state["raw"]:
        base["notes"].append(_("OPINION.md 使用 CRLF 行尾；迁移后的正文统一为 LF，回滚会逐字节还原原文件"))
    old_file = workdir / "old-profile.json"
    old_file.write_text(versioned.pretty(old), encoding="utf-8")
    refs = updates_for(old_file, old, args)
    base["updates"] = refs["entries"]
    base["custom"] = classify_custom(old, versioned.render(old, state["lock_language"]), state["lock_language"])
    schema = old["schema_version"]
    if schema == "2.2":
        return {"result": {**base, "status": "unchanged", "reason": "already-tagged", "message": _("已经是带标签声明的 Profile（schema 2.2），无需迁移")}, "actionable": False}
    if schema == "2.1" and not args.tags:
        return {"result": {**base, "status": "unchanged", "reason": "bundles-kept",
                           "message": _("Profile 使用规则束声明（schema 2.1），按约定保持不动，context bundle 继续可用"),
                           "next": [_("如要改用标签，先预览：{0}").format(command(root, "--tags"))]}, "actionable": False}
    if schema == "2.1":
        base["notes"].append(_("改用标签会把规则束声明（core / signals / bundles）整体替换为标签声明；新旧声明互斥，改写后不再有 bundles，context bundle 不再可用"))
    elif schema == "2.0":
        base["notes"].append(_("若此前用 --no-tags 有意保持完整读取，迁移会重新生成标签声明；不迁移则保持原样，迁移后可用 --rollback 或 profile --remove-loading 恢复完整读取"))
    return draft(root, args, workdir, state, base, refs, old_file)


def updates_for(old_file: Path, old: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """沿用 updates 的预览：可升级的模板与规则；--skip 的项保持当前版本。"""
    found = guarded(lambda: versioned.updates_command(Namespace(profile=old_file, catalog=versioned.PUBLIC_CATALOG))["updates"], "catalog-conflict", _("公开 catalog 与 Profile 记录的来源不一致"))
    entries, templates, rules = [], [], []
    known = {versioned.parse_ref(item["current"], item["kind"])[0] for item in found}
    unknown = sorted(set(args.skip) - known)
    if unknown:
        versioned.fail(_("--skip 引用了没有可升级版本的项：{0}；可用的项：{1}").format(", ".join(unknown), ", ".join(sorted(known)) or "-"))
    for item in found:
        identity = versioned.parse_ref(item["current"], item["kind"])[0]
        target = item["available"][-1]
        skipped = identity in args.skip
        entries.append({"kind": item["kind"], "id": identity, "current": item["current"], "target": target, "action": "skip" if skipped else "upgrade"})
        if not skipped:
            (templates if item["kind"] == "templates" else rules).append(target)
    return {"entries": entries, "templates": templates, "rules": rules}


def draft(root: Path, args: argparse.Namespace, workdir: Path, state: dict[str, Any], base: dict[str, Any], refs: dict[str, Any], old_file: Path) -> dict[str, Any]:
    lock, current = state["lock"], state["current"]
    old, language = lock["profile"], versioned.project_language(root)
    custom_file = None
    manual = None
    if base["opinion_md"]["modified"]:
        manual = manual_edits(versioned.render(old, state["lock_language"]), current, old, state["lock_language"])
        if manual["custom"] is None and old["custom"]:
            raise Stop("cannot-migrate", "manual-edit-conflict", _("OPINION.md 删除了全部自定义规则，迁移无法表示这种改动；迁移不会让已删除的行复活"),
                       [_("先用 profile 派生一个不含这些自定义规则的新版本，或还原被删除的行，再重新运行 migrate")])
        base["opinion_md"]["manual"] = {"appended_lines": manual["appended_lines"], "edited_in_custom": manual["edited_in_custom"], "appended": manual["appended"]}
        if manual["custom"] and manual["custom"] != (old["custom"] or ""):
            custom_file = workdir / "custom.md"
            custom_file.write_text(manual["custom"] + "\n", encoding="utf-8")
    major, minor, patch = versioned.version(old["version"])
    while True:
        patch += 1
        number = f"{major}.{minor}.{patch}"
        target = root / ".opinion" / "profiles" / old["id"] / "versions" / f"{number}.json"
        if not target.exists():
            break

    def namespace(**override: Any) -> Namespace:
        values = dict(project=root, id=old["id"], version=number, template=refs["templates"], rule=refs["rules"], remove_template=[], remove_rule=[], custom_file=custom_file,
                      from_profile=old_file, overrides_file=None, resolutions_file=args.resolutions_file, loading_file=None, remove_loading=False, no_tags=False,
                      catalog=versioned.PUBLIC_CATALOG, apply=False, confirm=None)
        values.update(override)
        return Namespace(**values)

    # 先只取升级后的来源（不带声明），再由所选模板合并出标签声明，作为 --loading-file 显式写入，不依赖 profile 对“模板选择是否变化”的判断。
    first = guarded(lambda: versioned.profile_command(namespace(remove_loading=True)), "draft-failed", _("无法生成新的 Profile 预览"))
    proposed = guarded(lambda: versioned.template_loading(first["profile"]["sources"]), "tag-conflict", _("所选模板的标签声明无法合并"))
    if proposed is None:
        return {"result": {**base, "status": "nothing-to-migrate", "reason": "no-tag-declaration",
                           "message": _("所选的公开模板没有可物化的标签声明（没有选用公开模板，或模板版本不带标签），保持完整读取，没有可迁移的内容"),
                           "updates": [{**item, "action": "available"} for item in base["updates"]]}, "actionable": False}
    loading_file = workdir / "loading.json"
    loading_file.write_text(versioned.pretty(proposed), encoding="utf-8")
    drafted = guarded(lambda: versioned.profile_command(namespace(loading_file=loading_file)), "draft-failed", _("无法生成新的 Profile 预览"))
    profile = drafted["profile"]
    preview_file = workdir / "profile.json"
    preview_file.write_text(versioned.pretty(profile), encoding="utf-8")
    accept = versioned.text_digest(current) if base["opinion_md"]["modified"] else None
    compose_args = Namespace(project=root, profile=preview_file, template=[], rule=[], custom_file=None, replace=False, accept_current=accept, apply=False, confirm=None)
    composed = guarded(lambda: versioned.compose_command(compose_args), "compose-failed", _("无法生成新的 OPINION.md 预览"))
    changes = drafted["changes"]
    base["loading"] = {"before": summary(old), "after": summary(profile), "source": "template"}
    base["custom"] = classify_custom(profile, composed["content"], language)
    base["changes"] = {key: changes[key] for key in ("added", "removed", "changed", "custom_changed", "templates", "direct_rules", "loading_changed", "loading")} | {"conflicts": [item["id"] for item in changes["conflicts"]]}
    unified = list(difflib.unified_diff(current.splitlines(), composed["content"].splitlines(), "OPINION.md", "OPINION.md (migrated)", lineterm="", n=0))
    base["diff"] = {"added_lines": sum(1 for line in unified if line.startswith("+") and not line.startswith("+++")),
                    "removed_lines": sum(1 for line in unified if line.startswith("-") and not line.startswith("---"))}
    base["target"] = {"profile": f"{old['id']}@{number}", "schema_version": profile["schema_version"], "path": str(target)}
    unresolved = list(drafted["unresolved"])
    classification = {key: base[key] for key in ("profile", "target", "updates", "loading", "opinion_md", "custom", "inbox", "changes")}
    fingerprint = versioned.digest({"profile": drafted["confirmation_sha256"], "compose": composed["confirmation_sha256"], "classification": classification})
    steps = []
    if unresolved:
        steps.append(_("先处理升级冲突：为 {0} 写 resolutions 文件（每项 keep-local 或 use-upstream），再加 --resolutions-file 重新预览").format(", ".join(unresolved)))
    elif composed["blocked"]:
        steps.append(composed["blocked"])
    else:
        steps.append(_("审阅后确认：{0}").format(command(root, *(["--tags"] if args.tags else []), *[item for skip in args.skip for item in ("--skip", shlex.quote(skip))],
                                                          *(["--resolutions-file", shlex.quote(str(args.resolutions_file))] if args.resolutions_file else []), "--apply", "--confirm", fingerprint)))
    result = {**base, "status": "preview", "unresolved": unresolved, "blocked": composed["blocked"], "confirmation_sha256": fingerprint, "content": composed["content"], "next": steps,
              **{key: composed[key] for key in ("untranslated", "warnings") if key in composed}}
    return {"result": result, "actionable": True, "fingerprint": fingerprint, "unresolved": unresolved, "blocked": composed["blocked"], "target": target, "number": number,
            "drafted": drafted, "composed": composed, "namespace": namespace(loading_file=loading_file), "compose_args": compose_args, "content": composed["content"],
            "old": old, "old_file": old_file, "preview_file": preview_file, "accept": accept}


# ---------------------------------------------------------------- 备份、写入、读回、还原

def backup_files(root: Path, old: dict[str, Any]) -> list[tuple[str, Path, bool]]:
    """备份的相对路径、源文件、回滚时是否还原。收件箱仅作留底，迁移不改写它，回滚也不还原它。"""
    profile = Path(".opinion") / "profiles" / old["id"] / "versions" / f"{old['version']}.json"
    items = [("OPINION.md", root / "OPINION.md", True), ("opinion.lock.json", root / "opinion.lock.json", True), (profile.as_posix(), root / profile, True), (inbox.STORE.as_posix(), root / inbox.STORE, False)]
    return [item for item in items if item[1].exists()]


def write_backup(root: Path, plan: dict[str, Any], directory: Path) -> list[dict[str, Any]]:
    entries = []
    for relative, source, restore in backup_files(root, plan["old"]):
        raw = source.read_bytes()
        destination = directory / "files" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        entries.append({"path": relative, "sha256": sha(raw), "restore": restore})
    manifest = {"schema": 1, "kind": "opinion-migrate", "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "confirmation_sha256": plan["fingerprint"],
                "before": f"{plan['old']['id']}@{plan['old']['version']}", "before_consistent": not plan["result"]["opinion_md"]["modified"],
                "files": entries, "after": None}
    (directory / MANIFEST).write_text(versioned.pretty(manifest), encoding="utf-8")
    return entries


def write_new(root: Path, plan: dict[str, Any]) -> None:
    versioned.profile_command(Namespace(**{**vars(plan["namespace"]), "apply": True, "confirm": plan["drafted"]["confirmation_sha256"]}))
    versioned.compose_command(Namespace(**{**vars(plan["compose_args"]), "profile": plan["target"], "apply": True, "confirm": plan["composed"]["confirmation_sha256"]}))


def read_back(root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    verified = versioned.verify_project(root)
    if (root / "OPINION.md").read_text(encoding="utf-8") != plan["content"]:
        versioned.fail(_("写入后读回的 OPINION.md 与预览不一致"))
    if versioned.read_object(plan["target"]) != plan["drafted"]["profile"]:
        versioned.fail(_("写入后读回的 Profile 与预览不一致"))
    return verified


def restore_all(root: Path, directory: Path, manifest: dict[str, Any], target: Path | None) -> None:
    for item in manifest["files"]:
        if item["restore"]:
            inbox.restore(root / item["path"], (directory / "files" / item["path"]).read_bytes())
    if target is not None:
        target.unlink(missing_ok=True)


def unique_directory(root: Path) -> Path:
    moment = datetime.now(UTC)
    while True:
        candidate = root / BACKUPS / f"{moment.strftime('%Y%m%dT%H%M%SZ')}-migrate"
        if not candidate.exists():
            return candidate
        moment = datetime.fromtimestamp(moment.timestamp() + 1, UTC)


def created_directories(root: Path, target: Path) -> list[Path]:
    chain = [root / ".opinion", root / BACKUPS, target.parent.parent.parent, target.parent.parent, target.parent]
    return [path for path in chain if not path.exists()]


def remove_created(paths: list[Path]) -> None:
    for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def apply(root: Path, plan: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    result = plan["result"]
    if plan["unresolved"]:
        raise Stop("refused", "unresolved-conflicts", _("升级存在未确认的冲突：{0}").format(", ".join(plan["unresolved"])), result["next"])
    if plan["blocked"]:
        raise Stop("refused", "blocked", plan["blocked"])
    if not args.confirm:
        raise Stop("refused", "confirm-required", _("写入需要 --apply --confirm <预览指纹>；先审阅预览"), result["next"])
    if args.confirm != plan["fingerprint"]:
        raise Stop("refused", "stale-confirmation", _("确认指纹与当前预览不一致：预览之后 Profile、OPINION.md、收件箱或 catalog 有变化；请重新预览并审阅"),
                   [command(root, *(["--tags"] if args.tags else []))])
    directory = unique_directory(root)
    created = created_directories(root, plan["target"])
    manifest: dict[str, Any] = {}
    phase = "backup"
    try:
        manifest = {"files": write_backup(root, plan, directory)}
        phase = "write"
        write_new(root, plan)
        phase = "read-back"
        verified = read_back(root, plan)
        phase = "manifest"
        finalize_manifest(root, directory, plan)
    except BaseException as exc:
        restored, detail = True, ""
        try:
            if manifest:
                restore_all(root, directory, manifest, plan["target"])
            else:
                plan["target"].unlink(missing_ok=True)
            shutil.rmtree(directory, ignore_errors=True)
            remove_created(created)
        except BaseException as inner:  # 还原本身失败：保留备份目录，给出手工还原方法
            restored, detail = False, str(inner)
        if not isinstance(exc, Exception):
            raise
        message = _("迁移在“{0}”阶段失败：{1}").format(phase, exc)
        if restored:
            raise Stop("failed", "apply-failed", message + _("；已按备份逐字节还原，项目与迁移前相同"), phase=phase, restored=True) from exc
        raise Stop("failed", "restore-failed", message + _("；自动还原也失败了（{0}），备份目录已保留：{1}").format(detail, directory),
                   [command(root, "--rollback", shlex.quote(str(directory)))], phase=phase, restored=False, backup=str(directory)) from exc
    return {**result, "status": "migrated", "applied": True, "backup": str(directory), "verified": verified,
            "message": _("已迁移到 {0}（schema {1}），备份在 {2}").format(plan["result"]["target"]["profile"], plan["result"]["target"]["schema_version"], directory),
            "next": [_("不满意时回滚：{0}").format(command(root, "--rollback", shlex.quote(str(directory))))]}


def finalize_manifest(root: Path, directory: Path, plan: dict[str, Any]) -> None:
    manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    manifest["after"] = {"profile": plan["result"]["target"]["profile"], "profile_path": plan["target"].relative_to(root).as_posix(), "profile_sha256": sha(plan["target"].read_bytes()),
                         "opinion_sha256": sha((root / "OPINION.md").read_bytes()), "lock_sha256": sha((root / "opinion.lock.json").read_bytes())}
    (directory / MANIFEST).write_text(versioned.pretty(manifest), encoding="utf-8")


# ---------------------------------------------------------------- 回滚

def rollback(root: Path, argument: Path) -> dict[str, Any]:
    directory = Path(argument if argument.is_absolute() else Path.cwd() / argument)
    directory = Path(versioned.safe_path(directory))
    parent = Path(versioned.safe_path(root / BACKUPS))
    if directory.parent != parent or not directory.name.endswith("-migrate") or not directory.is_dir():
        raise Stop("refused", "not-a-backup", _("回滚目录必须是本项目 .opinion/backups/ 下的 *-migrate 目录：{0}").format(directory))
    manifest = guarded(lambda: json.loads((directory / MANIFEST).read_text(encoding="utf-8")), "manifest-invalid", _("备份 manifest 不可读"))
    if not isinstance(manifest, dict) or manifest.get("kind") != "opinion-migrate" or not isinstance(manifest.get("files"), list):
        raise Stop("refused", "manifest-invalid", _("备份 manifest 格式无效"))
    if not manifest.get("after"):
        raise Stop("refused", "incomplete-backup", _("备份没有迁移后的状态记录（迁移未完成），无法确认是否可以安全回滚；该目录只是失败现场的备份"))
    for item in manifest["files"]:
        stored = directory / "files" / item["path"]
        if not stored.is_file() or sha(stored.read_bytes()) != item["sha256"]:
            raise Stop("refused", "backup-damaged", _("备份文件缺失或指纹不符：{0}").format(item["path"]))
    after = manifest["after"]
    target = root / after["profile_path"]

    def current(path: Path) -> str | None:
        return sha(path.read_bytes()) if path.is_file() else None

    restorable = [item for item in manifest["files"] if item["restore"]]
    before_state = all(current(root / item["path"]) == item["sha256"] for item in restorable) and not target.exists()
    after_state = (current(root / "OPINION.md") == after["opinion_sha256"] and current(root / "opinion.lock.json") == after["lock_sha256"] and current(target) == after["profile_sha256"])
    if before_state:
        return {"ok": True, "status": "unchanged", "reason": "already-restored", "message": _("当前状态已经等于迁移前，无需回滚"), "backup": str(directory)}
    if not after_state:
        changed = [name for name, value in (("OPINION.md", current(root / "OPINION.md") == after["opinion_sha256"]), ("opinion.lock.json", current(root / "opinion.lock.json") == after["lock_sha256"]),
                                            (after["profile_path"], current(target) == after["profile_sha256"])) if not value]
        raise Stop("refused", "state-changed", _("当前状态不等于迁移后的状态（迁移之后这些文件又被修改：{0}），回滚会丢失后续修改，已拒绝；需要时手工从备份目录取回文件").format(", ".join(changed)),
                   changed=changed, backup=str(directory))
    snapshot = {path: path.read_bytes() if path.exists() else None for path in (root / "OPINION.md", root / "opinion.lock.json", target)}
    try:
        restore_all(root, directory, manifest, target)
        # 迁移前正文被手工修改过时，还原后 verify 按设计不通过，只做逐字节核对
        verified = versioned.verify_project(root) if manifest.get("before_consistent", True) else None
        for item in restorable:
            if current(root / item["path"]) != item["sha256"]:
                versioned.fail(_("还原后核对失败：{0}").format(item["path"]))
    except BaseException as exc:
        for path, content in snapshot.items():
            inbox.restore(path, content)
        if not isinstance(exc, Exception):
            raise
        raise Stop("failed", "rollback-failed", _("回滚失败：{0}；已恢复到回滚前的状态").format(exc), restored=True) from exc
    return {"ok": True, "status": "rolled-back", "backup": str(directory), "verified": verified, "message": _("已按备份还原 Profile、OPINION.md 与锁，并删除迁移写入的 Profile {0}").format(after["profile"])}


# ---------------------------------------------------------------- 输出与入口

def text_of(result: dict[str, Any]) -> str:
    """可读文字：框架文字随界面语言；用户自己的内容原样引用。"""
    names = {"preview": N_("预览（未写入任何文件）"), "migrated": N_("已迁移"), "unchanged": N_("无需改动"), "nothing-to-migrate": N_("没有可迁移的内容"), "rolled-back": N_("已回滚"),
             "cannot-migrate": N_("无法迁移（未写入任何文件）"), "refused": N_("已拒绝（未写入任何文件）"), "failed": N_("失败")}
    lines = [_("# Opinion 迁移"), "", _("- 状态：{0}").format(_(names[result["status"]]))]
    if result.get("message"):
        lines.append(_("- 说明：{0}").format(result["message"]))
    if result.get("reason"):
        lines.append(_("- 原因代码：{0}").format(result["reason"]))
    profile = result.get("profile")
    if profile:
        lines.append(_("- 当前 Profile：`{0}@{1}`（schema {2}）").format(profile["id"], profile["version"], profile["schema_version"]))
    if result.get("target"):
        lines.append(_("- 迁移后 Profile：`{0}`（schema {1}）").format(result["target"]["profile"], result["target"]["schema_version"]))
    if result.get("updates"):
        lines.extend(("", _("## 模板与规则升级")))
        for item in result["updates"]:
            lines.append(_("- `{0}` → `{1}`（{2}）").format(item["current"], item["target"], _({"upgrade": N_("升级"), "skip": N_("按 --skip 保持当前版本"), "available": N_("可升级，本次未采用")}[item["action"]])))
    if result.get("loading") and (result["loading"]["before"] or result["loading"]["after"]):
        lines.extend(("", _("## 加载声明"), _("- 迁移前：{0}").format(json.dumps(result["loading"]["before"], ensure_ascii=False)), _("- 迁移后：{0}").format(json.dumps(result["loading"]["after"], ensure_ascii=False))))
    if result.get("opinion_md"):
        info = result["opinion_md"]
        lines.extend(("", _("## OPINION.md"), _("- 是否被手工修改：{0}").format(_("是") if info["modified"] else _("否"))))
        if info.get("manual"):
            lines.append(_("- 手工内容并入自定义规则（原样保留、常驻）：追加 {0} 行，自定义小节内改动 {1} 行").format(info["manual"]["appended_lines"], info["manual"]["edited_in_custom"]))
    if result.get("diff"):
        lines.append(_("- 新旧正文差异：+{0} / -{1} 行（完整新正文见 --output json 的 content）").format(result["diff"]["added_lines"], result["diff"]["removed_lines"]))
    custom = result.get("custom")
    if custom and custom["lines"]:
        lines.extend(("", _("## 自定义规则"), _("- 已带标签：{0}；常驻：{1}；可手工加标签的建议：{2}").format(custom["tagged"], custom["resident"], custom["suggested"])))
        for item in custom["lines"]:
            if item["category"] == "suggest":
                lines.append(_("  - 第 {0} 行可在行尾追加 `[tags 标签]`（可用标签：{1}）：{2}").format(item["line"], ", ".join(custom["available_tags"]), item["text"]))
    if result.get("inbox", {}).get("incomplete"):
        lines.extend(("", _("## 收件箱中缺字段的旧候选（只列出，不改写，不影响迁移）")))
        for item in result["inbox"]["incomplete"]:
            lines.append(_("- `{0}` {1}：缺 {2}").format(item["id"], item["title"], ", ".join(item["missing"])))
    for note in result.get("notes", []):
        lines.append(_("- 注意：{0}").format(note))
    for warning in result.get("warnings", []):
        lines.append(_("- 警告：{0}").format(warning))
    if result.get("confirmation_sha256"):
        lines.extend(("", _("确认指纹：`{0}`").format(result["confirmation_sha256"])))
    if result.get("next"):
        lines.extend(("", _("## 下一步"), *[f"- {step}" for step in result["next"]]))
    return "\n".join(lines) + "\n"


def emit(result: dict[str, Any], output: str) -> tuple[int, str]:
    code = STATUS_EXIT.get(result["status"], EXIT_OK)
    result = {"ok": code == EXIT_OK, **{key: value for key, value in result.items() if key != "ok"}}
    return code, (versioned.pretty(result) if output == "json" else text_of(result))


def execute(args: argparse.Namespace) -> tuple[int, str]:
    root = versioned.safe_path(args.project)
    try:
        if args.rollback is not None:
            if args.apply or args.confirm or args.tags or args.skip or args.resolutions_file:
                versioned.fail(_("--rollback 不能与 --apply、--confirm、--tags、--skip、--resolutions-file 同用"))
            return emit({"project": str(root), **rollback(root, args.rollback)}, args.output)
        if args.confirm and not args.apply:
            versioned.fail(_("--confirm 需要和 --apply 一起使用"))
        with tempfile.TemporaryDirectory(prefix="opinion-migrate-") as workdir:
            plan = plan_for(root, args, Path(workdir).resolve())
            result = plan["result"]
            if args.apply and plan["actionable"]:
                result = apply(root, plan, args)
    except Stop as stop:
        payload = {"status": stop.status, "reason": stop.reason, "message": stop.message, "project": str(root), "next": stop.steps, **stop.extra, "applied": False}
        return emit(payload, args.output)
    return emit({"project": str(root), "applied": bool(args.apply and result["status"] == "migrated"), **result}, args.output)


def add_command(subparsers: Any) -> None:
    parser = subparsers.add_parser("migrate", help=_("把现有 Opinion 迁移到标签选择加载：默认预览，--apply --confirm 才写入，可回滚"))
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help=_("写入迁移（需要 --confirm）"))
    parser.add_argument("--confirm", help=_("预览返回的确认指纹"))
    parser.add_argument("--rollback", type=Path, help=_("还原到迁移前：.opinion/backups/ 下的备份目录"))
    parser.add_argument("--tags", action="store_true", help=_("2.1（规则束声明）的项目改用标签声明；默认保持 2.1 不动"))
    parser.add_argument("--skip", action="append", default=[], help=_("不升级这个模板（family/variant）或规则编号，可重复"))
    parser.add_argument("--resolutions-file", type=Path, help=_("升级冲突的处理：编号到 keep-local 或 use-upstream 的 JSON"))
    parser.add_argument("--output", choices=("json", "markdown"), default="markdown")
