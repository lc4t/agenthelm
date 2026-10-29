#!/usr/bin/env python3
"""脚手架一致性：把项目和 agents-init 发布的期望骨架清单对比，并保存用户的决定。

- ``check``：只读。骨架文件缺失、被内容锁跟踪的模板文件与当前模板不同、Profile 受检字段缺失或不对、
  内容锁版本未知；被用户跳过的项单独列出。
- ``add_skeleton`` / ``fix_profile`` / ``repair_lock``：默认预览，``apply`` 才写入；替换前备份。
- ``decide`` / ``undecide``：把“跳过 / 保留 / 稍后”记入决定文件。项目级决定随项目同步
  （``.agents/moe.sakanano.agent-pack/check-decisions.json``）；只和这台机器有关的发现（id 以 ``local:`` 开头）
  存在用户状态目录，不进项目。

比较的对象只有脚手架。Opinion、Profile 里的项目自有字段与审计字段不参与比较，也不进入指纹。
本模块由 ``init_project.py``（命令行）和 agent-pack（``agenthelm check``）共同使用。
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
if str(Path(__file__).resolve().parent) not in sys.path:  # also loaded by path from other plugins
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n_init import _  # noqa: E402

HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE.parent / "skeleton.json"
SCHEMA_PATH = HERE.parent / "project.schema.json"
SKELETON_PACK = "agents-init/project-skeleton"
DECISIONS_PATH = Path(".agents/moe.sakanano.agent-pack/check-decisions.json")
DECISION_VALUES = ("skip", "keep", "later")
LOCAL_PREFIX = "local:"
# macOS iCloud 未下载文件：不读内容，避免触发下载。
SF_DATALESS = 0x40000000

init: Any = None  # init_project 模块；由 bind() 或首次使用时加载


def _load_default() -> Any:
    name = "agenthelm_init_project"
    path = HERE / "init_project.py"
    loaded = sys.modules.get(name)
    if loaded is not None and Path(getattr(loaded, "__file__", "")).resolve() == path:
        return loaded
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def bind(module: Any) -> None:
    """Use the init_project module that is already running, so its exception classes are the ones callers catch."""
    global init
    init = module


def _init() -> Any:
    global init
    if init is None:
        init = _load_default()
    return init


def sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _fail(message: str, code: str = "invalid-request") -> Exception:
    return _init().InitError(message, code=code)


# ---------------------------------------------------------------- manifest

def load_manifest() -> dict:
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail(_("无法读取期望骨架清单：{0}").format(MANIFEST_PATH), "template-invalid") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise _fail(_("期望骨架清单格式不对"), "template-invalid")
    return manifest


def pack_versions() -> dict[str, str]:
    """Versions of the content packs the expectation comes from (part of the fingerprint)."""
    module = _init()
    versions = {}
    try:
        content = json.loads((module.PLUGIN_ROOT / "content.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return versions
    for pack in content.get("packs", []):
        if pack.get("id") in (SKELETON_PACK, "agents-init/project-contract"):
            versions[pack["id"]] = str(pack.get("version", ""))
    return versions


def _relative_ok(value: Any) -> bool:
    if not isinstance(value, str) or not value or value == ".":
        return False
    path = Path(value)
    return not (path.is_absolute() or ".." in path.parts or "\\" in value or re.match(r"^[A-Za-z]:", value)
                or set(path.parts) & {".git", ".agents", ".codex", ".cursor", ".claude"})


def _get(data: Any, dotted: str) -> tuple[bool, Any]:
    current = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _set(data: dict, dotted: str, value: Any) -> None:
    current = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = copy.deepcopy(value)


class Context:
    """Everything derived from one project: its Profile, the inputs it was rendered with, the expected files."""

    def __init__(self, root: Path) -> None:
        module = _init()
        self.root = root
        self.profile = module._read_project_json(root, module.PROFILE_PATH, "Project Profile")
        self.inputs = self._inputs()
        work = self.profile.get("work") if isinstance(self.profile.get("work"), dict) else {}
        case = work.get("case") if isinstance(work.get("case"), dict) else {}
        memory = self.profile.get("memory") if isinstance(self.profile.get("memory"), dict) else {}
        self.case_root = case.get("root") if _relative_ok(case.get("root")) else "cases"
        self.memory_root = memory.get("root") if _relative_ok(memory.get("root")) else "memory"
        self.rendered = self._rendered()
        self.fresh = json.loads(self.rendered[module.PROFILE_PATH])
        self.pack = module.contract_pack()
        self.tracked = {path.as_posix() for path in module.tracked_paths(self.pack, self.inputs.entry_launcher)}
        self.manifest = load_manifest()

    def _inputs(self) -> Any:
        module = _init()
        try:
            return module.inputs_from_profile(self.root)
        except module.InitError:
            # 中文项目名没有可推导的 slug；检查只需要渲染出可比较的模板，不影响 Profile。
            details = self.profile.get("profile") or {}
            work = self.profile.get("work") if isinstance(self.profile.get("work"), dict) else {}
            return module.Inputs(
                self.root, str(self.profile.get("name") or self.root.name), "project",
                tuple(details.get("project_type") or ()), str(details.get("vcs") or ""),
                tuple(details.get("stack") or ()), str(details.get("runtime") or ""),
                tuple(details.get("agent_cli") or ()), str(work.get("mode") or "legacy"),
                module._profile_launcher(self.profile),
            )

    def _rendered(self) -> dict[Path, str]:
        files = _init().files_for(self.inputs)
        files.pop(Path("cases/.gitkeep"), None)
        files.pop(Path("memory/.gitkeep"), None)
        if self.inputs.work_mode == "case-workspace":
            files[Path(self.case_root) / ".gitkeep"] = ""
        files[Path(self.memory_root) / ".gitkeep"] = ""
        return files

    def entries(self) -> list[dict]:
        """The manifest's file entries resolved for this project (tokens filled, inapplicable ones dropped)."""
        resolved = []
        for entry in self.manifest["files"]:
            when = entry.get("when")
            if when == "case-workspace" and self.inputs.work_mode != "case-workspace":
                continue
            if when == "entry" and not self.inputs.entry_launcher:
                continue
            path = (entry["path"].replace("{case_root}", self.case_root).replace("{memory_root}", self.memory_root)
                    .replace("{launcher}", self.inputs.entry_launcher or ""))
            text = self.rendered.get(Path(path))
            resolved.append({**entry, "path": path, "fingerprint": sha(text if text is not None else path)})
        return resolved

    def expected_text(self, path: str) -> str | None:
        return self.rendered.get(Path(path))


# ---------------------------------------------------------------- reading project state

def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _file_digest(path: Path) -> str | None:
    """sha256 of a regular file; None when it is not a readable regular file or is still in iCloud."""
    metadata = _lstat(path)
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        return None
    if sys.platform == "darwin" and getattr(metadata, "st_flags", 0) & SF_DATALESS:
        return None
    try:
        return sha(path.read_bytes())
    except OSError:
        return None


def read_lock(root: Path) -> tuple[dict | None, str | None]:
    module = _init()
    try:
        return module._read_project_json(root, module.CONTENT_LOCK_PATH, "content lock"), None
    except module.InitError as exc:
        return None, None if exc.code == "refresh-unavailable" else str(exc)


def _skeleton_state(ctx: Context, entry: dict, baseline: dict[str, str]) -> str:
    target = ctx.root / entry["path"]
    compare = entry["compare"]
    if compare == "opinion":
        return "ignored"
    if compare == "dir":
        if _lstat(target) is not None or (ctx.root / Path(entry["path"]).parent).is_dir():
            return "ok"
        return "missing"
    if _lstat(target) is None:
        return "missing"
    if compare == "lock" and entry["path"] in ctx.tracked and entry["path"] not in baseline:
        current = _file_digest(target)
        expected = ctx.expected_text(entry["path"])
        if current is not None and expected is not None and current != sha(expected):
            return "differs"
    return "ok"


def _check_profile(ctx: Context) -> list[dict]:
    work_mode = ctx.inputs.work_mode
    rows = []
    for rule in ctx.manifest.get("profile", []):
        if rule.get("when") == "case-workspace" and work_mode != "case-workspace":
            continue
        present, value = _get(ctx.profile, rule["path"])
        _found, default = _get(ctx.fresh, rule["path"])
        mode = rule["mode"]
        if mode == "default" and default is None:
            mode = "manual"
        expected = default if mode in ("default", "equal") else None
        kinds = {"string": str, "boolean": bool, "array": list}
        kind = kinds[rule["type"]]
        if not present:
            state = "missing"
        elif value is None and mode == "default":
            state = "ok"
        elif not isinstance(value, kind) or (kind is str and rule.get("enum") and value not in rule["enum"]) \
                or (mode == "equal" and value != expected):
            state = "invalid"
        else:
            state = "ok"
        rows.append({"field": rule["path"], "state": state, "mode": mode, "expected": expected,
                     "value": value if state == "invalid" else None,
                     "fingerprint": sha(json.dumps({"field": rule["path"], "expected": expected,
                                                    "enum": rule.get("enum")}, sort_keys=True, ensure_ascii=False))})
    return rows


def _project_fields(ctx: Context) -> list[str]:
    """Profile fields the scaffold does not know: the project's own, never compared."""
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    known = set(ctx.manifest.get("audit_fields", [])) | set(ctx.manifest.get("opinion_fields", [])) \
        | {rule["path"] for rule in ctx.manifest.get("profile", [])}
    extras: list[str] = []

    def walk(value: dict, props: dict, prefix: str) -> None:
        for key, child in value.items():
            dotted = f"{prefix}{key}"
            declared = props.get(key)
            if declared is None:
                if not any(item == dotted or item.startswith(dotted + ".") for item in known):
                    extras.append(dotted)
            elif isinstance(child, dict) and isinstance(declared.get("properties"), dict):
                walk(child, declared["properties"], dotted + ".")

    walk(ctx.profile, schema.get("properties") or {}, "")
    return sorted(extras)


# ---------------------------------------------------------------- decisions

def local_decisions_file(state_dir: Path, root: Path) -> Path:
    return state_dir / "agenthelm" / "decisions" / f"{sha(str(root))[:16]}.json"


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    return Path(base) if base else Path.home() / ".local" / "state"


def read_decisions(path: Path) -> tuple[dict[str, dict], str | None, str | None]:
    """(decisions, sha256 of the file text or None, error)."""
    if path.is_symlink():
        return {}, None, _("拒绝符号链接：{0}").format(path.name)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None, None
    except (OSError, UnicodeDecodeError) as exc:
        return {}, None, _("决定文件无法读取：{0}").format(exc)
    try:
        data = json.loads(text)
    except ValueError as exc:
        return {}, sha(text), _("决定文件不是合法 JSON：{0}").format(exc)
    table = data.get("decisions") if isinstance(data, dict) else None
    if not isinstance(table, dict) or not all(isinstance(item, dict) and item.get("decision") in DECISION_VALUES
                                              for item in table.values()):
        return {}, sha(text), _("决定文件格式不对（需要 decisions 对象，decision 为 skip / keep / later）")
    return table, sha(text), None


def conflict_copies(root: Path) -> list[str]:
    """Copies a sync service leaves when two machines wrote the decisions file at once."""
    directory = (root / DECISIONS_PATH).parent
    stem = DECISIONS_PATH.stem
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir()
                  if p.name != DECISIONS_PATH.name and p.name.startswith(stem) and p.suffix == ".json")


def load_decisions(root: Path, state_dir: Path | None) -> dict:
    project, project_sha, project_error = read_decisions(root / DECISIONS_PATH)
    local_path = local_decisions_file(state_dir or default_state_dir(), root)
    local, local_sha, local_error = read_decisions(local_path)
    return {"project": project, "project_sha": project_sha, "local": local, "local_sha": local_sha,
            "local_path": str(local_path), "errors": [e for e in (project_error, local_error) if e],
            "conflict_copies": conflict_copies(root)}


def partition(findings: list[dict], decisions: dict, *, reprompt: bool = False) -> tuple[list[dict], list[dict]]:
    """Split findings into those still to show and those the user decided on (and whose expectation is unchanged)."""
    active, skipped = [], []
    for finding in findings:
        table = decisions["local"] if finding.get("scope") == "machine" else decisions["project"]
        decision = table.get(finding["id"])
        item = dict(finding)
        if decision is not None:
            if reprompt:
                item["previous_decision"] = decision
            elif decision["decision"] in ("skip", "keep") and decision.get("fingerprint") == finding["fingerprint"]:
                skipped.append({**item, "decision": decision})
                continue
            elif decision["decision"] == "later":
                item["deferred"] = decision
            else:
                item["changed_since_decision"] = decision
        active.append(item)
    return active, skipped


def keep_fingerprints(root: Path) -> dict[str, str]:
    """Files the user chose to keep, with the expected-content fingerprint of that moment (refresh honours them)."""
    table, _decisions, error = read_decisions(root / DECISIONS_PATH)
    if error:
        return {}
    return {key[len("content:"):]: item["fingerprint"] for key, item in table.items()
            if key.startswith("content:") and item["decision"] == "keep" and item.get("fingerprint")}


# ---------------------------------------------------------------- case directory naming

CASE_STYLES = {
    "case-id": r"CASE-[0-9]{8}-[0-9]{3}-[a-z0-9]+(?:-[a-z0-9]+)*",
    "date-topic": r"[0-9]{8}-.+",
}
TEXT_SUFFIXES = {".md", ".json", ".txt", ".yaml", ".yml", ".csv", ".html", ".toml"}
SKIPPED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", "dist", "build", "target"}


def case_naming_spec(profile: dict) -> dict:
    """The effective naming rule: ``work.case.directory_naming`` if declared, else the default Case id."""
    work = profile.get("work") if isinstance(profile.get("work"), dict) else {}
    case = work.get("case") if isinstance(work.get("case"), dict) else {}
    declared = case.get("directory_naming")
    if declared is None:
        return {"style": "case-id", "pattern": CASE_STYLES["case-id"], "declared": False, "error": None}
    if not isinstance(declared, dict):
        return {"style": None, "pattern": None, "declared": True, "error": _("directory_naming 必须是对象")}
    pattern = declared.get("pattern")
    style = declared.get("style") or ("custom" if pattern else "case-id")
    if style not in (*CASE_STYLES, "custom"):
        return {"style": style, "pattern": None, "declared": True,
                "error": _("style 只能是 case-id / date-topic / custom")}
    if pattern is not None and not isinstance(pattern, str):
        return {"style": style, "pattern": None, "declared": True, "error": _("pattern 必须是字符串（正则，匹配整个目录名）")}
    if style == "custom" and not pattern:
        return {"style": style, "pattern": None, "declared": True, "error": _("style 为 custom 时需要 pattern")}
    pattern = pattern or CASE_STYLES[style]
    try:
        re.compile(pattern)
    except re.error as exc:
        return {"style": style, "pattern": pattern, "declared": True, "error": _("pattern 不是合法正则：{0}").format(exc)}
    return {"style": style, "pattern": pattern, "declared": True, "error": None}


def _case_directories(ctx: Context) -> list[str]:
    base = ctx.root / ctx.case_root
    if not base.is_dir() or base.is_symlink():
        return []
    return sorted(p.name for p in base.iterdir()
                  if p.is_dir() and not p.is_symlink() and not p.name.startswith((".", "_")) and (p / "CASE.md").is_file())


def check_case_naming(ctx: Context) -> dict:
    spec = case_naming_spec(ctx.profile)
    names = _case_directories(ctx)
    mismatched = []
    if spec["error"] is None and ctx.inputs.work_mode == "case-workspace":
        compiled = re.compile(spec["pattern"])
        mismatched = [name for name in names if not compiled.fullmatch(name)]
    return {**spec, "root": ctx.case_root, "directories": len(names), "mismatched": mismatched[:200],
            "mismatched_count": len(mismatched)}


def _suggest_name(name: str, style: str, taken: set[str]) -> str | None:
    """A rename that fits ``case-id`` or ``date-topic`` when the old name carries a date; otherwise None (you decide)."""
    match = re.match(r"^(?:CASE-)?([0-9]{8})-(?:[0-9]{3}-)?(.+)$", name)
    if not match or style not in CASE_STYLES:
        return None
    date, topic = match.groups()
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    if not slug:
        return None
    if style == "date-topic":
        return f"{date}-{slug}"
    sequence = 1
    while f"CASE-{date}-{sequence:03d}-{slug}" in taken:
        sequence += 1
    return f"CASE-{date}-{sequence:03d}-{slug}"


def _references(root: Path, needles: dict[str, str], limit_files: int = 3000) -> dict[str, list[str]]:
    """Text files that mention a Case directory name; read-only and never touching iCloud placeholders."""
    found: dict[str, list[str]] = {name: [] for name in needles}
    seen = 0
    for current, directories, files in os.walk(root):
        directories[:] = [d for d in directories if d not in SKIPPED_DIRS and not (Path(current) / d).is_symlink()]
        for filename in files:
            path = Path(current) / filename
            if path.suffix.lower() not in TEXT_SUFFIXES or path.is_symlink():
                continue
            seen += 1
            if seen > limit_files:
                return found
            try:
                metadata = path.lstat()
                if metadata.st_size > 512 * 1024 or (sys.platform == "darwin" and getattr(metadata, "st_flags", 0) & SF_DATALESS):
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for name in needles:
                if name in text and len(found[name]) < 20:
                    found[name].append(path.relative_to(root).as_posix())
    return found


def case_naming_plan(root: Path) -> dict:
    """What would have to change for the case directories to follow the declared naming. Moves nothing."""
    ctx = Context(root)
    naming = check_case_naming(ctx)
    result = {"ok": True, "mode": "case-naming", "applied": False, "spec": {k: naming[k] for k in ("style", "pattern", "declared")},
              "error": naming["error"], "renames": [], "declaration_hint": None, "moves_directories": False}
    if naming["error"]:
        return result
    names = _case_directories(ctx)
    taken = set(names)
    mismatched = naming["mismatched"]
    refs = _references(root, {name: name for name in mismatched})
    for name in mismatched:
        suggestion = _suggest_name(name, naming["style"], taken)
        if suggestion:
            taken.add(suggestion)
        result["renames"].append({"from": f"{ctx.case_root}/{name}", "to": f"{ctx.case_root}/{suggestion}" if suggestion else None,
                                  "references": refs.get(name, []),
                                  "note": "" if suggestion else _("名称里没有日期或主题，需要你自己决定新名字"),
                                  })
    if not naming["declared"] and mismatched:
        names_style = "date-topic" if all(re.fullmatch(CASE_STYLES["date-topic"], n) for n in mismatched) else "custom"
        result["declaration_hint"] = {
            "if_this_is_your_convention": {"work": {"case": {"directory_naming": (
                {"style": "date-topic"} if names_style == "date-topic" else {"style": "custom", "pattern": _("<匹配全部目录名的正则>")})}}},
            "note": _("目录名是你的约定的话，在 Profile 里声明它（fix profile 不会替你写这一项），之后 check 按声明检查")}
    return result


# ---------------------------------------------------------------- fingerprint

def fingerprint(report: dict) -> dict:
    """One hash over the scaffold state: expected-list versions, skeleton state, checked Profile fields, lock.

    Skipped items, Opinion, audit fields and the project's own fields are left out, so two machines that agree on
    the scaffold get the same value no matter what the project adds on top.
    """
    skipped = {item["id"] for item in report["skipped"]}
    detail: dict[str, str] = {}
    for pack, version in sorted(report["packs"].items()):
        detail[f"expected:{pack}"] = version
    for row in report["skeleton"]:
        if row["state"] == "ignored" or f"skel:{row['path']}" in skipped or f"content:{row['path']}" in skipped:
            continue
        detail[f"skeleton:{row['path']}"] = row["state"]
    for row in report["profile"]:
        if f"profile:{row['field']}" not in skipped:
            detail[f"profile:{row['field']}"] = row["state"]
    lock = report["lock"]
    if lock["pack"] and f"lock:{lock['pack']}" not in skipped:
        detail["lock:version"] = f"{lock['status']}@{lock['version']}"
    naming = report["case_naming"]
    if "case-naming" not in skipped:
        detail["case-naming"] = f"{naming['style']}:{naming['pattern']}:" + ("error" if naming["error"] else
                                                                           "mismatch" if naming["mismatched_count"] else "ok")
    digest = sha(json.dumps(detail, sort_keys=True, ensure_ascii=False))
    return {"fingerprint": f"sha256:{digest}", "detail": detail}


def explain(mine: dict[str, str], theirs: dict[str, str]) -> list[str]:
    """Human-readable differences between two fingerprint details (this machine vs another)."""
    lines = []
    for key in sorted(set(mine) | set(theirs)):
        if mine.get(key) == theirs.get(key):
            continue
        if key not in theirs:
            lines.append(_("{0}：只有这台有（{1}）").format(key, mine[key]))
        elif key not in mine:
            lines.append(_("{0}：只有对方有（{1}）").format(key, theirs[key]))
        elif key.startswith("expected:"):
            lines.append(_("{0}：期望清单版本不同——这台 {1}，对方 {2}（先把两边的 agents-init 更新到同一版本）").format(key, mine[key], theirs[key]))
        else:
            lines.append(_("{0}：这台 {1}，对方 {2}").format(key, mine[key], theirs[key]))
    return lines


# ---------------------------------------------------------------- check

def _finding(identifier: str, kind: str, title: str, reason: str, fingerprint: str, fix: str | None,
             **extra: Any) -> dict:
    return {"id": identifier, "kind": kind, "level": "suggest", "scope": "project", "title": title,
            "reason": reason, "fingerprint": fingerprint, "fix": fix, **extra}


def check(root: Path, *, state_dir: Path | None = None, reprompt: bool = False) -> dict:
    """Read-only comparison of the project with the expected scaffold."""
    module = _init()
    ctx = Context(root)
    lock, lock_error = read_lock(root)
    pack_entry = ((lock or {}).get("packs") or {}).get(ctx.pack["id"]) if ctx.pack else None
    baseline = dict((pack_entry or {}).get("files") or {})
    versions = pack_versions()
    findings: list[dict] = []

    skeleton = []
    for entry in ctx.entries():
        state = _skeleton_state(ctx, entry, baseline)
        skeleton.append({"path": entry["path"], "compare": entry["compare"], "required": entry["required"],
                         "state": state, "fingerprint": entry["fingerprint"]})
        if state == "missing" and entry["required"]:
            findings.append(_finding(f"skel:{entry['path']}", "skeleton-missing", _("缺少脚手架文件 {0}").format(entry['path']),
                                     _("期望骨架清单里有，项目里没有"), entry["fingerprint"], "add-skeleton",
                                     path=entry["path"]))
        elif state == "differs":
            findings.append(_finding(f"content:{entry['path']}", "content-differs", _("{0} 与当前模板不同").format(entry['path']),
                                     _("内容锁里没有它的基线，无法判断是你改过还是模板变了"),
                                     entry["fingerprint"], "update-content", path=entry["path"]))

    profile_rows = _check_profile(ctx)
    for row in profile_rows:
        if row["state"] == "ok":
            continue
        fixable = row["mode"] in ("default", "equal")
        what = _("缺少") if row["state"] == "missing" else _("不符合期望")
        findings.append(_finding(f"profile:{row['field']}", f"profile-{row['state']}",
                                 _("Profile {0}：{1}").format(what, row['field']),
                                 _("期望 {0}").format(json.dumps(row['expected'], ensure_ascii=False)) if fixable else _("需要你自己填写"),
                                 row["fingerprint"], "profile" if fixable else None, field=row["field"]))

    lock_state = {"pack": ctx.pack["id"] if ctx.pack else None, "version": (pack_entry or {}).get("version"),
                  "latest": ctx.pack["version"] if ctx.pack else None, "error": lock_error, "status": "ok"}
    if pack_entry is not None and pack_entry.get("version") == module.UNKNOWN_PACK_VERSION:
        repairable, _plan = _lock_repair_plan(ctx, lock)
        lock_state["status"] = "unknown-version"
        findings.append(_finding(
            f"lock:{ctx.pack['id']}", "lock-version", _("内容锁的模板版本未知（0.0.0）"),
            _("旧版本写入的占位版本") + (_("；基线与当前模板一致，可自动修复") if repairable else _("；基线与当前模板不同，需要逐文件选择")),
            sha(f"{ctx.pack['id']}@{module.UNKNOWN_PACK_VERSION}"), "repair-lock" if repairable else "update-content"))
    if lock_error:
        lock_state["status"] = "error"

    naming = check_case_naming(ctx)
    if naming["error"]:
        findings.append(_finding("profile:work.case.directory_naming", "profile-invalid",
                                 _("Profile 的 work.case.directory_naming 无法使用"), naming["error"],
                                 sha(json.dumps({"field": "work.case.directory_naming", "error": naming["error"]})), None,
                                 field="work.case.directory_naming"))
    elif naming["mismatched_count"]:
        shown = _("、").join(naming["mismatched"][:3]) + ("……" if naming["mismatched_count"] > 3 else "")
        source = _("Profile 声明的") if naming["declared"] else _("默认的 CASE-日期-序号-主题")
        findings.append(_finding("case-naming", "case-naming",
                                 _("{0} 个 Case 目录的命名与{1}规则不符").format(naming['mismatched_count'], source),
                                 _("例如 {0}；规则 {1}").format(shown, naming['pattern']),
                                 sha(json.dumps({"style": naming["style"], "pattern": naming["pattern"]})), "case-naming"))
    extras = [{"path": p, "kind": "legacy"} for p in ctx.manifest.get("legacy_paths", []) if _lstat(root / p)]
    extras += [{"path": f, "kind": "project-field"} for f in _project_fields(ctx)]

    decisions = load_decisions(root, state_dir)
    active, skipped = partition(findings, decisions, reprompt=reprompt)
    return {"ok": not active, "mode": "check", "packs": versions, "skeleton": skeleton, "profile": profile_rows,
            "lock": lock_state, "case_naming": naming, "extras": extras, "findings": active, "skipped": skipped,
            "decisions": {"path": DECISIONS_PATH.as_posix(), "errors": decisions["errors"],
                          "conflict_copies": decisions["conflict_copies"], "local_path": decisions["local_path"]}}


# ---------------------------------------------------------------- shared write helpers

def _backup(root: Path, recovery_dir: Path | None, relative: Path) -> str:
    if recovery_dir is None:
        raise _fail(_("替换文件前需要 --recovery-dir"))
    recovery = recovery_dir.expanduser().resolve()
    if recovery == root or root in recovery.parents:
        raise _fail("--recovery-dir must be outside the project root")
    archived = recovery / relative
    if archived.exists():
        raise _fail(f"recovery collision: {relative}")
    archived.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    archived.write_bytes((root / relative).read_bytes())
    return str(recovery)


def _record_baseline(root: Path, pack: dict, additions: dict[str, str]) -> bool:
    """Add baselines for files this tool just wrote, but only where the lock's version is the current one."""
    module = _init()
    if not additions:
        return False
    path = root / module.CONTENT_LOCK_PATH
    lock = module._read_project_json(root, module.CONTENT_LOCK_PATH, "content lock") if path.exists() \
        else {"schema_version": 1, "packs": {}}
    entry = lock.setdefault("packs", {}).get(pack["id"])
    if entry is None:
        entry = {"plugin": "agents-init", "version": pack["version"], "files": {}}
    elif entry.get("version") != pack["version"]:
        return False
    entry["files"] = dict(sorted({**entry.get("files", {}), **additions}.items()))
    lock["packs"][pack["id"]] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    module._atomic_write(path, module.json_text(lock))
    return True


# ---------------------------------------------------------------- add-skeleton

def add_skeleton(root: Path, targets: list[str] | None, *, apply: bool = False, state_dir: Path | None = None) -> dict:
    """Create missing skeleton files from the templates; existing files are never touched."""
    module = _init()
    ctx = Context(root)
    report = check(root, state_dir=state_dir, reprompt=True)
    known = {item["path"]: item for item in report["skeleton"] if item["state"] != "ignored"}
    if targets:
        unknown = [t for t in targets if t not in known]
        if unknown:
            raise _fail(_("不在期望骨架清单里：") + _("、").join(unknown))
        wanted = list(dict.fromkeys(targets))
    else:
        wanted = [path for path, item in known.items() if item["state"] == "missing" and item["required"]]
    missing = [p for p in wanted if known[p]["state"] == "missing"]
    unchanged = [p for p in wanted if known[p]["state"] != "missing"]
    files = {Path(p): ctx.rendered.get(Path(p), "") for p in missing}
    create: list[Path] = []
    if files:
        create, collisions, _extra = module.plan_writes(root, files)
        unchanged += [str(p) for p in collisions]
    result = {"ok": True, "mode": "add-skeleton", "applied": False,
              "create": [{"path": str(p), "bytes": len(files[p].encode("utf-8"))} for p in create],
              "unchanged": sorted(unchanged)}
    if not apply or not create:
        return result
    module.apply_writes(root, files, create)
    launcher = ctx.inputs.entry_launcher
    if launcher and Path(launcher) in create:
        os.chmod(root / launcher, 0o755)
    if ctx.pack:
        _record_baseline(root, ctx.pack, {str(p): sha(files[p]) for p in create if str(p) in ctx.tracked})
    result["applied"] = True
    return result


# ---------------------------------------------------------------- profile

def fix_profile(root: Path, fields: list[str] | None, *, apply: bool = False, recovery_dir: Path | None = None,
                state_dir: Path | None = None) -> dict:
    """Set the selected missing/invalid Profile fields to the template value; nothing else changes."""
    module = _init()
    report = check(root, state_dir=state_dir, reprompt=True)
    rows = {row["field"]: row for row in report["profile"] if row["state"] != "ok" and row["mode"] in ("default", "equal")}
    if fields:
        unknown = [f for f in fields if f not in rows]
        if unknown:
            raise _fail(_("这些字段不需要或不能自动修复：") + _("、").join(unknown))
        selected = list(dict.fromkeys(fields))
    else:
        selected = list(rows)
    profile = module._read_project_json(root, module.PROFILE_PATH, "Project Profile")
    updated = copy.deepcopy(profile)
    for field in selected:
        _set(updated, field, rows[field]["expected"])
    changed = updated != profile
    result = {"ok": True, "mode": "profile", "applied": False, "fields": selected if changed else [],
              "diff": module._unified_diff(module.PROFILE_PATH.as_posix(), module.json_text(profile),
                                           module.json_text(updated)) if changed else ""}
    if not apply or not changed:
        return result
    if module._read_project_json(root, module.PROFILE_PATH, "Project Profile") != profile:
        raise _fail(_("Profile 刚被改过，请重新预览"), "conflict")
    result["recovery"] = _backup(root, recovery_dir, module.PROFILE_PATH)
    module._atomic_write(root / module.PROFILE_PATH, module.json_text(updated))
    result["applied"] = True
    return result


# ---------------------------------------------------------------- content lock

def _lock_repair_plan(ctx: Context, lock: dict | None) -> tuple[bool, dict]:
    """(can the 0.0.0 version be replaced truthfully, the lock entry as it would become)."""
    module = _init()
    if ctx.pack is None or lock is None:
        return False, {}
    entry = copy.deepcopy((lock.get("packs") or {}).get(ctx.pack["id"]) or {})
    files = dict(entry.get("files") or {})
    comparable = True
    for path, digest in files.items():
        text = ctx.expected_text(path)
        if text is None or sha(text) != digest:
            comparable = False
    if entry.get("version") == module.UNKNOWN_PACK_VERSION and comparable:
        entry["version"] = ctx.pack["version"]
    return comparable, entry


def repair_lock(root: Path, *, apply: bool = False) -> dict:
    """Replace a 0.0.0 lock version by the real one and record baselines for tracked files equal to the template.

    The version is only replaced when every recorded baseline equals what the current template renders: then the
    baselines really are this template version. Otherwise nothing is guessed; ``update-content`` decides per file.
    """
    module = _init()
    ctx = Context(root)
    lock, error = read_lock(root)
    result = {"ok": True, "mode": "repair-lock", "applied": False, "changes": [], "blocked": "", "diff": ""}
    if ctx.pack is None or lock is None:
        result["blocked"] = error or _("项目没有内容锁")
        return result
    entry = (lock.get("packs") or {}).get(ctx.pack["id"])
    if entry is None:
        result["blocked"] = _("内容锁里没有项目契约模板包")
        return result
    comparable, new_entry = _lock_repair_plan(ctx, lock)
    if entry.get("version") == module.UNKNOWN_PACK_VERSION and not comparable:
        result["blocked"] = _("记录的基线与当前模板不同，无法确定真实版本：用 update-content 逐文件选择保留或采用")
        return result
    if new_entry["version"] != entry.get("version"):
        result["changes"].append(_("模板版本 {0} → {1}").format(entry.get('version'), new_entry['version']))
    if new_entry.get("version") == ctx.pack["version"]:
        files = dict(new_entry.get("files") or {})
        for path in sorted(ctx.tracked):
            text = ctx.expected_text(path)
            current = _file_digest(root / path)
            if path not in files and text is not None and current == sha(text):
                files[path] = current
                result["changes"].append(_("记录基线：{0}").format(path))
        new_entry["files"] = dict(sorted(files.items()))
    if not result["changes"]:
        return result
    new_lock = copy.deepcopy(lock)
    new_lock["packs"][ctx.pack["id"]] = new_entry
    result["diff"] = module._unified_diff(module.CONTENT_LOCK_PATH.as_posix(), module.json_text(lock),
                                          module.json_text(new_lock))
    if not apply:
        return result
    fresh, _lock = read_lock(root)
    if fresh != lock:
        raise _fail(_("内容锁刚被改过，请重新预览"), "conflict")
    module._atomic_write(root / module.CONTENT_LOCK_PATH, module.json_text(new_lock))
    result["applied"] = True
    return result


# ---------------------------------------------------------------- recording decisions

def _decision_file(root: Path, state_dir: Path | None, scope: str) -> Path:
    return local_decisions_file(state_dir or default_state_dir(), root) if scope == "machine" else root / DECISIONS_PATH


def _store(path: Path, table: dict, expect_sha: str | None, root: Path, scope: str) -> None:
    module = _init()
    if scope == "project":
        for parent in [path.parent, *path.parents]:
            if parent == root:
                break
            if parent.is_symlink():
                raise _fail(_("拒绝符号链接：{0}").format(parent.relative_to(root)))
    current_sha = None
    try:
        current_sha = sha(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError):
        pass
    if current_sha != expect_sha:
        raise _fail(_("决定文件刚被别处改过（可能是另一台机器同步过来），请重新预览"), "conflict")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if scope == "machine" else 0o755)
    text = module.json_text({"schema_version": 1, "decisions": dict(sorted(table.items()))})
    module._atomic_write(path, text)


def decide(root: Path, finding_id: str, decision: str, *, reason: str = "", fingerprint: str | None = None,
           scope: str | None = None, apply: bool = False, state_dir: Path | None = None) -> dict:
    """Record skip / keep / later for a finding. ``fingerprint`` defaults to the finding's current expectation."""
    if decision not in DECISION_VALUES:
        raise _fail(_("决定只能是 skip / keep / later"))
    scope = scope or ("machine" if finding_id.startswith(LOCAL_PREFIX) else "project")
    if fingerprint is None:
        report = check(root, state_dir=state_dir, reprompt=True)
        match = next((f for f in [*report["findings"], *report["skipped"]] if f["id"] == finding_id), None)
        if match is None:
            raise _fail(_("现在没有这个发现：{0}（用 check 查看当前的发现 ID）").format(finding_id), "not-found")
        fingerprint = match["fingerprint"]
    path = _decision_file(root, state_dir, scope)
    table, current_sha, error = read_decisions(path)
    if error:
        raise _fail(error, "profile-invalid")
    entry = {"decision": decision, "fingerprint": fingerprint, "decided_at": now_iso()}
    if reason:
        entry["reason"] = reason
    table = {**table, finding_id: entry}
    result = {"ok": True, "mode": "decide", "applied": False, "id": finding_id, "scope": scope, "entry": entry,
              "file": DECISIONS_PATH.as_posix() if scope == "project" else str(path)}
    if apply:
        _store(path, table, current_sha, root, scope)
        result["applied"] = True
    return result


def undecide(root: Path, finding_id: str, *, apply: bool = False, state_dir: Path | None = None) -> dict:
    scopes = ["machine"] if finding_id.startswith(LOCAL_PREFIX) else ["project", "machine"]
    for scope in scopes:
        path = _decision_file(root, state_dir, scope)
        table, current_sha, error = read_decisions(path)
        if error:
            raise _fail(error, "profile-invalid")
        if finding_id in table:
            remaining = {k: v for k, v in table.items() if k != finding_id}
            result = {"ok": True, "mode": "undecide", "applied": False, "id": finding_id, "scope": scope,
                      "removed": table[finding_id],
                      "file": DECISIONS_PATH.as_posix() if scope == "project" else str(path)}
            if apply:
                _store(path, remaining, current_sha, root, scope)
                result["applied"] = True
            return result
    raise _fail(_("没有关于 {0} 的决定").format(finding_id), "not-found")


# ---------------------------------------------------------------- command line (called by init_project)

def run_mode(args: Any, root: Path) -> tuple[int, dict]:
    """Handle ``--mode check | add-skeleton | profile | repair-lock | decide | case-naming`` of init_project.py."""
    state_dir = args.state_dir
    targets = list(args.target) or None
    if args.mode == "check":
        report = check(root, state_dir=state_dir, reprompt=args.reprompt)
        report["fingerprint"] = fingerprint(report)
        return (0 if report["ok"] else 1), report
    if args.mode == "case-naming":
        return 0, case_naming_plan(root)
    if args.mode == "add-skeleton":
        return 0, add_skeleton(root, targets, apply=args.apply, state_dir=state_dir)
    if args.mode == "profile":
        return 0, fix_profile(root, targets, apply=args.apply, recovery_dir=args.recovery_dir, state_dir=state_dir)
    if args.mode == "repair-lock":
        result = repair_lock(root, apply=args.apply)
        return (1 if result["blocked"] else 0), result
    if not args.target or len(args.target) != 1:
        raise _fail(_("--mode decide 需要且只需要一个 --target <发现 ID>"))
    if args.decision == "unskip":
        return 0, undecide(root, args.target[0], apply=args.apply, state_dir=state_dir)
    return 0, decide(root, args.target[0], args.decision or "skip", reason=args.reason or "",
                     fingerprint=args.fingerprint, apply=args.apply, state_dir=state_dir)


def render_text(result: dict) -> str:
    lines = []
    mode = result.get("mode")
    if mode == "check":
        for item in result["findings"]:
            lines.append(_("  [{0}] {1}：{2}").format(item['id'], item['title'], item['reason']) + (_("（修复：{0}）").format(item['fix']) if item["fix"] else ""))
        for item in result["skipped"]:
            reason = item["decision"].get("reason") or _("（未写理由）")
            lines.append(_("  [已跳过 {0}] {1}").format(item['id'], reason))
        lines.append(_("脚手架与期望一致。") if result["ok"] else _("{0} 项差异。").format(len(result['findings'])))
    else:
        label = _("已执行") if result.get("applied") else _("预览")
        lines.append(_("{0}：{1}").format(label, mode))
        for item in result.get("renames", []):
            lines.append(f"  {item['from']} → {item['to'] or _('（需要你决定）')}"
                         + (_("；被 {0} 个文件提到").format(len(item['references'])) if item["references"] else ""))
        for item in result.get("create", []):
            lines.append(f"  create {item['path']}")
        for change in result.get("changes", []):
            lines.append(f"  {change}")
        if result.get("diff"):
            lines.append(result["diff"].rstrip())
        if result.get("blocked"):
            lines.append(result["blocked"])
    return "\n".join(lines) + "\n"
