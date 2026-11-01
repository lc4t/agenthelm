#!/usr/bin/env python3
"""交付物台账：登记 Case 与额外扫描根下的文件，按标题、日期、同义词查找，追踪版本与引用。

这是 agenthelm 的官方样例实现，只读：台账是一份可随时重建的 SQLite 投影，放在用户
缓存目录（``$XDG_CACHE_HOME/agenthelm/assets/``，缺省 ``~/.cache``），项目里的文件与
各 CASE.md 仍是权威，本工具不写回任何项目文件。

扫描范围来自 Project Profile：Case 根取 ``work.case.root``（缺省 ``cases``），额外扫描根取
``work.assets.roots``（缺省无）。同义词来自项目可选文件
``.agents/moe.sakanano.agent-pack/asset-synonyms.json``（缺省为空）。凭据、密钥与证书
无论是否入库都不登记；嵌套的 SVN/Git 外部检出与纯缓存目录不展开。Case 的 sensitivity 不影响登记：台账只是本机缓存里的索引，
private 的 Case 也能被找到（读取其内容前是否需要确认，由私有目录的约定管，不在这里过滤）。
"""
from __future__ import annotations

import argparse
import bisect
import collections
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote
from i18n_orchestrator import _  # noqa: E402

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

PROFILE = Path(".agents/moe.sakanano.agent-pack/project.json")
SYNONYMS = Path(".agents/moe.sakanano.agent-pack/asset-synonyms.json")
ASSET_SCHEMA_VERSION = 2  # 2: 非 shared 的 Case 也登记；旧台账自动重建
DB_NAME = "assets.sqlite3"
LOCK_NAME = "assets.lock"
LAST_NAME = "wf-last.json"
# 生成的派生视图（Dashboard 等）在开头带此标记，不作为引用来源。
DERIVED_MARKER = "<!-- derived:"

SKIP_NAMES = {".DS_Store", ".gitkeep", ".localized", "Thumbs.db", "desktop.ini"}
SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".svn", ".hg", ".venv", ".obsidian", ".idea", ".vscode"}
# 目录里出现这些标记时视为嵌套的外部检出，整体不登记。
EXTERNAL_MARKERS = (".svn", ".git")
CACHE_DIR_RE = re.compile(r"^(\.cache|cache|.*[-_]cache(?:[-_].*)?)$", re.IGNORECASE)
# 被 .gitignore 排除的文件只在属于业务文档、表格或压缩包时登记（仅元数据）。
BUSINESS_EXT = {".xlsx", ".xls", ".csv", ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz",
                ".docx", ".doc", ".pptx", ".ppt", ".pdf", ".numbers", ".pages"}
# 凭据、密钥、证书：无论是否入库都不登记。
DENY_NAME_RE = re.compile(r"(?i)(credential|password|passwd|secret|token|cookie|\.env|id_rsa|id_ed25519|"
                          r"private[-_]?key)")
DENY_EXT = {".pem", ".key", ".p12", ".pfx", ".cer", ".crt", ".mobileprovision", ".keystore", ".jks"}
# macOS iCloud 未下载文件（st_flags 的 SF_DATALESS）：不读内容、不算哈希，避免触发下载。
SF_DATALESS = 0x40000000
HASH_LIMIT = 200 * 1024 * 1024
TEXT_EXT = {".md", ".csv", ".txt", ".json", ".jsonl", ".html", ".htm", ".py", ".yaml", ".yml", ".sql"}
CONTENT_LIMIT = 1024 * 1024
DECLARED_KEYS = ("deliverables", "evidence")
STATUS_ORDER = {"active": 0, "waiting": 1, "captured": 2, "suspended": 3, "done": 4, "abandoned": 5}
ROLE_SCORE = {"deliverable": 2, "draft": 1, "source": -1, "process": -1, "image": -2, "case-record": -1}

MD_LINK = re.compile(r"\]\((<[^>]+>|[^)\n]+)\)")
WIKI = re.compile(r"\[\[([^\]\n|#]+)(?:#[^\]\n|]*)?(?:\|[^\]\n]*)?\]\]")
TICK = re.compile(r"`([^`\n]{3,300})`")
# 只匹配正文里的 Case 编号；路径片段（前后带 /）由路径解析处理。
CASE_ID = re.compile(r"(?<![/\w-])CASE-\d{8}-\d{3}(?:-[a-z0-9]+)*(?![/\w])")
LEGACY_TASK = re.compile(r"\btasks/(\d{8}-[\w.\-]+)")

CREATE_SQL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE containers (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
    sensitivity TEXT NOT NULL, goal TEXT NOT NULL, tags TEXT NOT NULL, aliases TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE files (
    path TEXT PRIMARY KEY, name TEXT NOT NULL, ext TEXT NOT NULL, container TEXT NOT NULL,
    container_kind TEXT NOT NULL, role TEXT NOT NULL, size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL, sha256 TEXT, git_state TEXT NOT NULL, git_dirty INTEGER NOT NULL,
    last_commit TEXT, dataless INTEGER NOT NULL, title TEXT NOT NULL, family TEXT NOT NULL, dates TEXT NOT NULL
);
CREATE TABLE edges (src TEXT NOT NULL, dst TEXT NOT NULL, type TEXT NOT NULL, raw TEXT NOT NULL,
                    PRIMARY KEY (src, dst));
CREATE TABLE broken (src TEXT NOT NULL, raw TEXT NOT NULL, type TEXT NOT NULL, class TEXT NOT NULL,
                     PRIMARY KEY (src, raw));
CREATE INDEX files_family ON files(family);
CREATE INDEX files_container ON files(container);
CREATE INDEX edges_dst ON edges(dst);
"""


class AssetError(Exception):
    def __init__(self, message: str, code: str = "invalid-request") -> None:
        super().__init__(message)
        self.code = code


def _iso(seconds: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(seconds)) if seconds else None


def _now() -> str:
    return _iso(time.time()) or ""


# ---------------------------------------------------------------- project

class Project:
    """Scan layout from the Project Profile; every field is optional."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        if not self.root.is_dir():
            raise AssetError(_("项目目录不存在：{0}").format(root), "project-missing")
        profile_path = self.root / PROFILE
        self.profile_found = profile_path.is_file()
        profile: dict = {}
        if self.profile_found:
            try:
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise AssetError(_("Project Profile 无法解析：{0}").format(exc), "profile-invalid") from exc
        work = profile.get("work") if isinstance(profile, dict) else None
        work = work if isinstance(work, dict) else {}
        case = work.get("case") if isinstance(work.get("case"), dict) else {}
        assets = work.get("assets") if isinstance(work.get("assets"), dict) else {}
        self.case_root = self._relative(case.get("root") or "cases", "work.case.root")
        extra = assets.get("roots") or []
        if not isinstance(extra, list) or not all(isinstance(item, str) for item in extra):
            raise AssetError(_("work.assets.roots 必须是字符串列表"), "profile-invalid")
        roots = [self.case_root]
        for value in extra:
            relative = self._relative(value, "work.assets.roots")
            if relative not in roots:
                roots.append(relative)
        for first in roots:
            for second in roots:
                if first != second and second.startswith(first + "/"):
                    raise AssetError(_("扫描根互相嵌套：{0} 与 {1}").format(first, second), "profile-invalid")
        self.roots = roots

    @classmethod
    def discover(cls, start: Path | None, explicit: bool) -> "Project":
        """``--project`` is taken as given; otherwise walk up from cwd to the nearest Profile."""
        base = (start or Path.cwd()).expanduser().resolve()
        if not explicit:
            for candidate in (base, *base.parents):
                if (candidate / PROFILE).is_file():
                    return cls(candidate)
        return cls(base)

    def _relative(self, value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise AssetError(_("{0} 必须是项目内相对路径").format(label), "profile-invalid")
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise AssetError(_("{0} 必须是项目内相对路径").format(label), "profile-invalid")
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise AssetError(_("{0} 不能经过符号链接").format(label), "symlink")
        return relative.as_posix()

    def state_dir(self, create: bool = False) -> Path:
        base = os.environ.get("XDG_CACHE_HOME")
        cache = (Path(base) if base else Path.home() / ".cache") / "agenthelm" / "assets"
        key = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:16]
        directory = cache / key
        resolved = directory.resolve()
        if resolved == self.root or self.root in resolved.parents:
            raise AssetError(_("缓存目录不能位于项目内（检查 XDG_CACHE_HOME）"), "state-inside-project")
        if create:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        for path in (directory, directory / DB_NAME, directory / LOCK_NAME, directory / LAST_NAME):
            if path.is_symlink():
                raise AssetError(_("缓存路径不能是符号链接：{0}").format(path.name), "symlink")
        return directory

    def synonyms(self) -> list[list[str]]:
        path = self.root / SYNONYMS
        if not path.is_file() or path.is_symlink():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AssetError(_("同义词文件无法解析：{0}").format(exc), "synonyms-invalid") from exc
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, list):
            raise AssetError(_("同义词文件需要 groups 列表"), "synonyms-invalid")
        return [[str(word).lower() for word in group if str(word).strip()]
                for group in groups if isinstance(group, list) and len(group) > 1]

    def container_of(self, rel: str) -> tuple[str, str, str]:
        """(container id, kind, role) for a project-relative path."""
        for root in self.roots:
            if not rel.startswith(root + "/"):
                continue
            inner = rel[len(root) + 1:].split("/")
            if len(inner) < 2:
                return root, "other", "other"
            if root == self.case_root:
                return inner[0], "case", _role_of(inner[1:])
            return f"{root}/{inner[0]}", "project", "project-file"
        return rel.split("/", 1)[0], "other", "other"


def _role_of(parts: list[str]) -> str:
    """Role inside a Case, following the case-workspace layout."""
    if parts == ["CASE.md"]:
        return "case-record"
    top = parts[0]
    if top == "docs":
        sub = parts[1] if len(parts) > 2 else ""
        return {"drafts": "draft", "refs": "source", "images": "image"}.get(sub, "deliverable")
    return {"refs": "source", "data": "data", "images": "image", "scripts": "script",
            ".agent-doc": "process", "work-items": "work-item"}.get(top, "other")


def parse_frontmatter(text: str) -> dict:
    """Tolerant top-level reader (scalars and one-level lists); project Schemas vary."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    data: dict[str, Any] = {}
    key = None
    for line in text[3:end].splitlines():
        match = re.match(r"^([A-Za-z_]\w*):\s*(.*)$", line)
        if match:
            key, value = match.group(1), match.group(2).strip()
            data[key] = [] if value in ("", "[]") else value.strip('"')
            continue
        match = re.match(r"^\s+-\s+(.*)$", line)
        if match and key is not None:
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(match.group(1).strip().strip('"'))
    return data


# ---------------------------------------------------------------- titles, dates, versions

_TITLE_H1 = re.compile(r"(?m)^#\s+(.+?)\s*$")
_TITLE_HTML = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_DATE_FULL = (
    re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(20\d{2})[-_./](\d{1,2})[-_./](\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?"),
)
_DATE_MD = (
    re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?"),
    re.compile(r"(?<![\d.])(\d{1,2})[./](\d{1,2})(?![\d.])"),
    re.compile(r"(?<!\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"),
)
_VERSION_TOKEN = re.compile(
    r"(?i)[-_ ]?(v\d+(?:\.\d+)*|final|draft|web|test\d*|copy|终稿|定稿|草稿|评审稿|讨论稿|副本|\(\d+\))"
)
_TRAILING_DATE = re.compile(r"[-_ ]*\d{8,10}$")


def read_title(path: Path, ext: str) -> str:
    """Best-effort document title; cheap, bounded read, never raises."""
    if ext not in (".md", ".html", ".htm"):
        return ""
    try:
        with open(path, "rb") as handle:
            head = handle.read(16384).decode("utf-8", "replace")
    except OSError:
        return ""
    if ext == ".md":
        front = parse_frontmatter(head)
        if isinstance(front.get("title"), str) and front["title"]:
            return front["title"][:200]
        body = head
        if head.startswith("---"):
            end = head.find("\n---", 3)
            body = head[end + 4:] if end > 0 else head
        match = _TITLE_H1.search(body)
        return match.group(1)[:200] if match else ""
    match = _TITLE_HTML.search(head)
    return re.sub(r"\s+", " ", match.group(1)).strip()[:200] if match else ""


def extract_dates(*texts: str) -> str:
    """Normalized date tokens: ``yyyymmdd`` when the year is known, plus ``mmdd``."""
    found = set()
    for text in texts:
        if not text:
            continue
        for pattern in _DATE_FULL:
            for match in pattern.finditer(text):
                month, day = int(match.group(2)), int(match.group(3))
                if 1 <= month <= 12 and 1 <= day <= 31:
                    found.add(f"{match.group(1)}{month:02d}{day:02d}")
                    found.add(f"{month:02d}{day:02d}")
        for pattern in _DATE_MD:
            for match in pattern.finditer(text):
                month, day = int(match.group(1)), int(match.group(2))
                if 1 <= month <= 12 and 1 <= day <= 31:
                    found.add(f"{month:02d}{day:02d}")
    return " ".join(sorted(found))


def family_key(container: str, name: str) -> str:
    """Files that differ only by version, draft marker or trailing date share a family."""
    stem, ext = os.path.splitext(name)
    stem = _VERSION_TOKEN.sub("", stem)
    stem = _TRAILING_DATE.sub("", stem).strip(" -_") or stem
    return f"{container}|{stem.lower()}{ext.lower()}"


def version_rank(name: str) -> tuple:
    """Explicit vN and a trailing date outrank mtime, which sync clients rewrite."""
    stem = os.path.splitext(name)[0]
    match = re.search(r"(?i)v(\d+(?:\.\d+)*)", stem)
    numbers = tuple(int(part) for part in match.group(1).split(".")) if match else ()
    date = re.search(r"(\d{8,10})$", stem)
    return numbers, int(date.group(1)) if date else 0


def parse_query(terms: list[str]) -> tuple[list[str], list[tuple[str | None, str]]]:
    """Split free text into (text terms, [(year or None, 'mmdd')])."""
    text = " ".join(terms)
    dates: list[tuple[str | None, str]] = []
    for pattern in _DATE_FULL:
        for match in pattern.finditer(text):
            month, day = int(match.group(2)), int(match.group(3))
            if 1 <= month <= 12 and 1 <= day <= 31:
                dates.append((match.group(1), f"{month:02d}{day:02d}"))
        text = pattern.sub(" ", text)
    for pattern in _DATE_MD:
        for match in pattern.finditer(text):
            month, day = int(match.group(1)), int(match.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                dates.append((None, f"{month:02d}{day:02d}"))
        text = pattern.sub(" ", text)
    return [term for term in text.split() if term], dates


def expand_terms(terms: list[str], groups: list[list[str]]) -> list[list[str]]:
    """Each term splits into known synonym words plus leftovers; each part gains its alternatives."""
    keys = sorted({word for group in groups for word in group}, key=len, reverse=True)
    out = []
    for term in terms:
        lowered = term.lower()
        parts: list[str] = []
        rest = lowered
        while rest:
            for key in keys:
                if rest.startswith(key):
                    parts.append(key)
                    rest = rest[len(key):]
                    break
            else:
                # Unknown characters glue together into one sub-term.
                if parts and parts[-1] not in keys:
                    parts[-1] += rest[0]
                else:
                    parts.append(rest[0])
                rest = rest[1:]
        if not any(part in keys for part in parts):
            parts = [lowered]
        for part in parts:
            alternatives = {part}
            for group in groups:
                if part in group:
                    alternatives.update(group)
            out.append(sorted(alternatives))
    return out


# ---------------------------------------------------------------- scanning

def _walk(project: Project, skipped: list | None = None,
          checkouts: list | None = None) -> Iterator[tuple[str, os.stat_result]]:
    """Yield (relative path, stat) for regular files under the scan roots; symlinks are never followed."""
    root = project.root
    stack = [root / base for base in project.roots if (root / base).is_dir()]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        if checkouts is not None and any(
                entry.name in EXTERNAL_MARKERS and entry.is_dir(follow_symlinks=False) for entry in entries):
            checkouts.append(directory.relative_to(root).as_posix())
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in SKIP_DIRS:
                        continue
                    if CACHE_DIR_RE.match(entry.name):
                        if skipped is not None:
                            skipped.append({"reason": "cache", "path": Path(entry.path).relative_to(root).as_posix()})
                        continue
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    if entry.name in SKIP_NAMES or entry.name.endswith(".pyc"):
                        continue
                    yield Path(entry.path).relative_to(root).as_posix(), entry.stat(follow_symlinks=False)
            except OSError:
                continue


def listing_fingerprint(project: Project) -> tuple[str, int]:
    digest = hashlib.sha256()
    for root in project.roots:
        digest.update(f"root\0{root}\n".encode("utf-8"))
    count = 0
    for rel, stat in sorted(_walk(project), key=lambda item: item[0]):
        digest.update(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8"))
        count += 1
    return digest.hexdigest(), count


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes | None:
    """git output, or None when git is missing or the project is not a work tree."""
    try:
        result = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=root, input=input_bytes,
                                capture_output=True, check=False, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode in (0, 1) else None


def _git_state(project: Project, paths: list[str]) -> tuple[bool, set, dict, set, set]:
    root = project.root
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if not inside or inside.strip() != b"true":
        return False, set(), {}, set(), set()
    roots = list(project.roots)
    # log and status print paths from the repository top; the project may be a subdirectory.
    prefix = (_git(root, "rev-parse", "--show-prefix") or b"").decode("utf-8", "replace").strip()
    tracked = {item.decode("utf-8", "replace")
               for item in (_git(root, "ls-files", "-z", "--", *roots) or b"").split(b"\0") if item}
    last: dict[str, int] = {}
    stamp = None
    log = (_git(root, "log", "--format=@%ct", "--name-only", "--", *roots) or b"").decode("utf-8", "replace")
    for line in log.splitlines():
        if line.startswith("@"):
            stamp = int(line[1:])
        elif line.startswith(prefix) and line[len(prefix):] not in last and stamp is not None:
            last[line[len(prefix):]] = stamp
    dirty = set()
    for record in (_git(root, "status", "--porcelain", "-z", "--", *roots) or b"").split(b"\0"):
        path = record[3:].decode("utf-8", "replace")
        if len(record) > 3 and path.startswith(prefix):
            dirty.add(path[len(prefix):])
    candidates = [path for path in paths if path not in tracked]
    ignored: set = set()
    if candidates:
        output = _git(root, "check-ignore", "--stdin", "-z", input_bytes="\0".join(candidates).encode("utf-8"))
        ignored = {item for item in (output or b"").decode("utf-8", "replace").split("\0") if item}
    return True, tracked, last, dirty, ignored


def _ignored_dir(root: Path, relative: str) -> bool:
    try:
        return subprocess.run(["git", "check-ignore", "-q", "--", relative + "/"], cwd=root,
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _checkout_roots(project: Project, checkouts: list[str]) -> set[str]:
    """Top-most git-ignored ancestor of each nested checkout, never above its scan root."""
    found = set()
    for directory in checkouts:
        current = directory
        while True:
            parent = os.path.dirname(current)
            if not parent or parent in project.roots or not _ignored_dir(project.root, parent):
                break
            current = parent
        found.add(current)
    return found


def _is_dataless(stat: os.stat_result) -> bool:
    return sys.platform == "darwin" and bool(getattr(stat, "st_flags", 0) & SF_DATALESS)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_containers(project: Project) -> dict[str, dict]:
    containers = {}
    case_root = project.root / project.case_root
    if not case_root.is_dir():
        return containers
    for directory in sorted(case_root.iterdir()):
        record = directory / "CASE.md"
        if directory.is_symlink() or not directory.is_dir() or record.is_symlink() or not record.is_file():
            continue
        try:
            front = parse_frontmatter(record.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue

        def text(key: str, default: str = "") -> str:
            value = front.get(key)
            return value if isinstance(value, str) and value else default

        def as_list(key: str) -> list:
            value = front.get(key)
            return value if isinstance(value, list) else []

        containers[directory.name] = {
            "id": directory.name, "kind": "case", "title": text("title"), "status": text("status"),
            "sensitivity": text("sensitivity", "shared"), "goal": text("goal"), "tags": as_list("tags"),
            "aliases": as_list("aliases"), "legacy_ids": as_list("legacy_ids"), "updated_at": text("updated_at"),
        }
    return containers


def scan(project: Project, previous: dict | None = None) -> dict:
    """Scan the project into an in-memory registry; nothing is written."""
    previous = previous or {}
    root = project.root
    containers = _load_containers(project)
    skipped: list[dict] = []
    checkouts: list[str] = []
    raw = []
    for rel, stat in _walk(project, skipped, checkouts):
        raw.append((rel, stat))
    external = _checkout_roots(project, checkouts)
    for directory in sorted(external):
        skipped.append({"reason": "external-checkout", "path": directory})
    if external:
        raw = [(rel, stat) for rel, stat in raw if not any(rel.startswith(item + "/") for item in external)]
    has_git, tracked, last_commit, dirty, ignored = _git_state(project, [rel for rel, _stat in raw])

    files = {}
    denied = 0
    for rel, stat in raw:
        name = rel.rsplit("/", 1)[-1]
        ext = Path(name).suffix.lower()
        if DENY_NAME_RE.search(name) or ext in DENY_EXT:
            denied += 1
            continue
        if not has_git:
            git_state = "none"
        elif rel in ignored:
            git_state = "ignored"
            if ext not in BUSINESS_EXT:
                continue
        elif rel in tracked:
            git_state = "tracked"
        else:
            git_state = "untracked"
        container, kind, role = project.container_of(rel)
        dataless = _is_dataless(stat)
        digest = None
        if not dataless and stat.st_size <= HASH_LIMIT:
            cached = previous.get(rel)
            if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
                digest = cached[2]
            else:
                try:
                    digest = _sha256_file(root / rel)
                except OSError:
                    digest = None
        title = "" if dataless else read_title(root / rel, ext)
        files[rel] = {
            "path": rel, "name": name, "ext": ext, "container": container, "container_kind": kind, "role": role,
            "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest, "git_state": git_state,
            "git_dirty": 1 if rel in dirty else 0, "last_commit": _iso(last_commit.get(rel)),
            "dataless": 1 if dataless else 0, "title": title, "family": family_key(container, name),
            "dates": extract_dates(name, title),
        }
    for item in files.values():
        if item["container_kind"] == "project" and item["container"] not in containers:
            containers[item["container"]] = {
                "id": item["container"], "kind": "project", "title": item["container"].split("/")[-1],
                "status": "", "sensitivity": "shared", "goal": "", "tags": [], "aliases": [],
                "legacy_ids": [], "updated_at": "",
            }
    edges, broken = _extract_edges(project, files, containers)
    return {"files": files, "containers": containers, "edges": edges, "broken": broken,
            "skipped": skipped, "denied": denied, "git": has_git}


def _prefixes(project: Project) -> tuple[str, ...]:
    """Bare path mentions are recognised when they start at a top-level project directory."""
    names = set(project.roots)
    try:
        names.update(entry.name for entry in os.scandir(project.root)
                     if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."))
    except OSError:
        pass
    return tuple(sorted(name + "/" for name in names))


def _extract_edges(project: Project, files: dict, containers: dict) -> tuple[dict, dict]:
    root, case_root = project.root, project.case_root
    prefixes = _prefixes(project)
    bare = re.compile(r"(?<![\w/.])((?:" + "|".join(re.escape(prefix) for prefix in prefixes)
                      + r")[^\s`\"'<>)\]|，。；、]+)") if prefixes else None
    directories = set()
    for rel in files:
        parent = os.path.dirname(rel)
        while parent:
            directories.add(parent)
            parent = os.path.dirname(parent)
    by_base: dict[str, list[str]] = collections.defaultdict(list)
    for rel in files:
        by_base[os.path.basename(rel)].append(rel)
        if rel.endswith(".md"):
            by_base[os.path.basename(rel)[:-3]].append(rel)
    sorted_paths = sorted(files)
    legacy = {str(legacy_id): f"{case_root}/{cid}/CASE.md"
              for cid, item in containers.items() for legacy_id in item.get("legacy_ids", [])}
    case_ids = sorted(cid for cid, item in containers.items() if item["kind"] == "case")

    def exists_any(rel: str) -> str | None:
        if rel in files:
            return rel
        if rel in directories:
            # A bare top-level directory carries no locating value.
            return rel + "/" if "/" in rel else None
        if rel != "." and not rel.startswith("../") and (root / rel).exists():
            return rel
        return None

    def prefix_unique(candidate: str) -> str | None:
        if len(candidate) < 12:
            return None
        index = bisect.bisect_left(sorted_paths, candidate)
        hits = []
        while index < len(sorted_paths) and sorted_paths[index].startswith(candidate):
            hits.append(sorted_paths[index])
            if len(hits) > 1:
                return None
            index += 1
        return hits[0] if hits else None

    def resolve(raw: str, src: str, mode: str) -> tuple[str | None, bool]:
        raw = unquote(raw.strip().strip("<>")).strip().split("#", 1)[0].rstrip(".,:;)")
        if not raw or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", raw) or raw.startswith(("/", "~")) or len(raw) > 260:
            return None, False
        candidates = [os.path.normpath(os.path.join(os.path.dirname(src), raw))]
        if "/" in raw:
            candidates.append(os.path.normpath(raw))
        parts = src.split("/")
        depth = len(case_root.split("/"))
        if src.startswith(case_root + "/") and len(parts) > depth + 1:
            candidates.append(os.path.normpath("/".join(parts[:depth + 1]) + "/" + raw))
        if mode == "wiki":
            candidates += [candidate + ".md" for candidate in candidates if not candidate.endswith(".md")]
        candidates = [candidate for candidate in candidates if not candidate.startswith("..")]
        for candidate in candidates:
            hit = exists_any(candidate)
            if hit:
                return hit, True
        if mode != "wiki":
            for candidate in candidates:
                hit = prefix_unique(candidate)
                if hit:
                    return hit, True
        if mode == "wiki":
            base = os.path.basename(raw)
            options = by_base.get(base) or by_base.get(base + ".md") or []
            if len(options) == 1:
                return options[0], True
        looks = "/" in raw and (raw.startswith(prefixes) or "." in os.path.basename(raw))
        return None, looks

    edges: dict[tuple[str, str], tuple[str, str]] = {}
    broken: dict[tuple[str, str], str] = {}

    def add_edge(src: str, dst: str, kind: str, raw: str) -> None:
        if dst.rstrip("/") != src and (src, dst) not in edges:
            edges[(src, dst)] = (kind, raw)

    sources = [rel for rel, item in files.items() if item["ext"] == ".md" and item["size"] < 2_000_000
               and not item["dataless"] and item["git_state"] != "ignored"]
    for src in sources:
        try:
            text = (root / src).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if DERIVED_MARKER in text[:600]:
            continue
        seen = set()
        if src.startswith(case_root + "/") and src.endswith("/CASE.md") and src.count("/") == case_root.count("/") + 2:
            front = parse_frontmatter(text)
            for key in DECLARED_KEYS:
                values = front.get(key)
                for value in values if isinstance(values, list) else []:
                    dst, looks = resolve(value, src, "path")
                    if dst:
                        add_edge(src, dst, "declared:" + key, value)
                    elif looks:
                        broken.setdefault((src, value), "declared:" + key)
                    seen.add(value)
        for match in MD_LINK.finditer(text):
            dst, looks = resolve(match.group(1), src, "link")
            if dst:
                add_edge(src, dst, "link", match.group(1))
            elif looks and match.group(1) not in seen:
                broken.setdefault((src, match.group(1)), "link")
        for match in WIKI.finditer(text):
            dst, _kind = resolve(match.group(1), src, "wiki")
            if dst:
                add_edge(src, dst, "wikilink", match.group(1))
            else:
                broken.setdefault((src, match.group(1)), "wikilink")
        for match in TICK.finditer(text):
            raw = match.group(1)
            if raw in seen or (" " in raw.strip() and not raw.startswith(prefixes)):
                continue
            if "/" not in raw and not re.search(r"\.\w{1,5}$", raw):
                continue
            dst, looks = resolve(raw, src, "path")
            if dst:
                add_edge(src, dst, "mention", raw)
            elif looks and raw.startswith(prefixes):
                broken.setdefault((src, raw), "mention")
        for match in bare.finditer(text) if bare else ():
            raw = match.group(1).rstrip(".,:;")
            if "*" in raw or raw.endswith("/"):
                continue
            dst, looks = resolve(raw, src, "path")
            if dst:
                add_edge(src, dst, "mention", raw)
            elif looks:
                broken.setdefault((src, raw), "mention")
        for match in CASE_ID.finditer(text):
            hits = [cid for cid in case_ids if cid == match.group(0) or cid.startswith(match.group(0) + "-")]
            if len(hits) == 1:
                add_edge(src, f"{case_root}/{hits[0]}/CASE.md", "case-id", match.group(0))
        for match in LEGACY_TASK.finditer(text):
            target = legacy.get(match.group(1))
            if target:
                add_edge(src, target, "legacy-id", match.group(0))
                for key in [key for key in broken if key[0] == src and key[1].startswith(match.group(0))]:
                    del broken[key]
    return edges, broken


def classify_broken(raw: str, kind: str, prefixes: tuple[str, ...] = ()) -> str:
    if re.match(r"^tasks/\d{8}", raw):
        return "legacy-task-id"
    if kind.startswith("declared") and (
            (" " in raw.strip() and not raw.startswith(prefixes)) or "：" in raw or "http" in raw):
        return "prose-not-path"
    if kind == "wikilink" and "/" not in raw and "." not in raw:
        return "wikilink-topic"
    return "real-broken"


# ---------------------------------------------------------------- storage

class _Lock:
    """Exclusive rebuild lock; advisory where the platform offers it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_Lock":
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise AssetError(_("无法打开台账锁：{0}").format(exc), "state-lock") from exc
        try:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            raise AssetError(_("台账正在被另一个进程重建，请稍后重试"), "index-locked") from exc
        return self

    def __exit__(self, *_: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)  # closing releases flock / msvcrt locks
            self.fd = None


def _previous_hashes(db_path: Path) -> dict:
    if not db_path.is_file():
        return {}
    try:
        connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        try:
            return {row[0]: (row[1], row[2], row[3]) for row in connection.execute(
                "SELECT path, size, mtime_ns, sha256 FROM files WHERE sha256 IS NOT NULL")}
        finally:
            connection.close()
    except sqlite3.Error:
        return {}


def _write_database(path: Path, registry: dict, fingerprint: str, project: Project) -> None:
    temporary = path.parent / f".assets.{os.getpid()}.{time.time_ns()}.tmp"
    prefixes = _prefixes(project)
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(CREATE_SQL)
        connection.execute(f"PRAGMA user_version = {ASSET_SCHEMA_VERSION}")
        meta = {"schema_version": str(ASSET_SCHEMA_VERSION), "built_at": _now(), "fingerprint": fingerprint,
                "project": str(project.root), "roots": json.dumps(project.roots, ensure_ascii=False),
                "file_count": str(len(registry["files"])), "edge_count": str(len(registry["edges"])),
                "denied": str(registry["denied"]), "git": "1" if registry["git"] else "0",
                "skipped": json.dumps(registry["skipped"], ensure_ascii=False)}
        connection.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
        connection.executemany(
            "INSERT INTO containers VALUES (?,?,?,?,?,?,?,?,?)",
            [(item["id"], item["kind"], item["title"], item["status"], item["sensitivity"], item["goal"],
              json.dumps(item["tags"], ensure_ascii=False), json.dumps(item["aliases"], ensure_ascii=False),
              item["updated_at"]) for item in registry["containers"].values()])
        connection.executemany(
            "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(item["path"], item["name"], item["ext"], item["container"], item["container_kind"], item["role"],
              item["size"], item["mtime_ns"], item["sha256"], item["git_state"], item["git_dirty"],
              item["last_commit"], item["dataless"], item["title"], item["family"], item["dates"])
             for item in registry["files"].values()])
        connection.executemany("INSERT INTO edges VALUES (?,?,?,?)",
                               [(src, dst, kind, raw) for (src, dst), (kind, raw) in registry["edges"].items()])
        connection.executemany("INSERT INTO broken VALUES (?,?,?,?)",
                               [(src, raw, kind, classify_broken(raw, kind, prefixes))
                                for (src, raw), kind in registry["broken"].items()])
        connection.commit()
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    connection.close()
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _open(project: Project) -> sqlite3.Connection:
    path = project.state_dir() / DB_NAME
    if not path.is_file():
        raise AssetError(_("台账尚未建立，先运行 rebuild（或去掉 --no-refresh）"), "index-missing")
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as exc:
        raise AssetError(_("台账无法读取，请运行 rebuild --force"), "index-corrupt") from exc
    if version != ASSET_SCHEMA_VERSION:
        connection.close()
        raise AssetError(_("台账格式与当前工具不一致，请运行 rebuild --force"), "index-incompatible")
    return connection


def _meta(connection: sqlite3.Connection) -> dict:
    return {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}


def rebuild(project: Project, force: bool = False) -> dict:
    state = project.state_dir(create=True)
    db_path = state / DB_NAME
    with _Lock(state / LOCK_NAME):
        fingerprint, count = listing_fingerprint(project)
        if not force and db_path.is_file():
            try:
                connection = _open(project)
                try:
                    meta = _meta(connection)
                finally:
                    connection.close()
                if meta.get("fingerprint") == fingerprint:
                    return {"ok": True, "action": "no-op", "file_count": int(meta.get("file_count", 0)),
                            "built_at": meta.get("built_at", ""), "state_dir": str(state)}
            except AssetError:
                pass
        registry = scan(project, _previous_hashes(db_path))
        _write_database(db_path, registry, fingerprint, project)
        return {"ok": True, "action": "rebuilt", "file_count": len(registry["files"]),
                "edge_count": len(registry["edges"]), "broken_count": len(registry["broken"]),
                "denied": registry["denied"], "built_at": _now(), "state_dir": str(state)}


def status(project: Project) -> dict:
    base = {"ok": True, "project": str(project.root), "profile": "found" if project.profile_found else "missing",
            "roots": project.roots, "state_dir": str(project.state_dir())}
    try:
        connection = _open(project)
    except AssetError as exc:
        return {**base, "status": exc.code.replace("index-", "")}
    try:
        meta = _meta(connection)
    finally:
        connection.close()
    fingerprint, count = listing_fingerprint(project)
    return {**base, "status": "fresh" if meta.get("fingerprint") == fingerprint else "stale",
            "file_count": int(meta.get("file_count", 0)), "edge_count": int(meta.get("edge_count", 0)),
            "built_at": meta.get("built_at", ""), "current_file_count": count}


def _connect(project: Project, refresh: bool) -> sqlite3.Connection:
    if refresh:
        rebuild(project)
    return _open(project)


# ---------------------------------------------------------------- queries

def _containers(connection: sqlite3.Connection) -> dict[str, dict]:
    return {row["id"]: dict(row) for row in connection.execute("SELECT * FROM containers")}


def _row_info(row: sqlite3.Row, info: dict, inbound: collections.Counter) -> dict:
    container = info.get(row["container"], {})
    return {"path": row["path"], "name": row["name"], "title": row["title"], "role": row["role"],
            "container": row["container"], "container_kind": row["container_kind"],
            "case_title": container.get("title", ""), "status": container.get("status", ""),
            "sensitivity": container.get("sensitivity", "shared"),
            "modified": _iso(row["mtime_ns"] / 1e9), "size": row["size"],
            "git": row["git_state"] + ("+dirty" if row["git_dirty"] else ""),
            "dataless": bool(row["dataless"]), "referenced_by": inbound.get(row["path"], 0)}


def _group_families(rows: list, info: dict, inbound: collections.Counter, limit: int) -> tuple[list, int]:
    """rows: [(score, mtime_ns, row)] -> family groups, best first; the newest version leads each group."""
    groups: dict[str, list] = collections.OrderedDict()
    for score, mtime, row in rows:
        groups.setdefault(row["family"], []).append((score, mtime, row))
    out = []
    for members in groups.values():
        members.sort(key=lambda member: (version_rank(member[2]["name"]), member[1]), reverse=True)
        head = _row_info(members[0][2], info, inbound)
        head["versions"] = [{"path": member[2]["path"], "name": member[2]["name"],
                             "modified": _iso(member[1] / 1e9), "git": member[2]["git_state"]}
                            for member in members[1:]]
        out.append((max(member[0] for member in members), members[0][1], head))
    out.sort(key=lambda group: (group[0], group[1]), reverse=True)
    return [group[2] for group in out[:limit]], len(out)


def _inbound_counts(connection: sqlite3.Connection) -> collections.Counter:
    return collections.Counter(row[0] for row in connection.execute("SELECT dst FROM edges"))


def find(project: Project, terms: list[str], *, kind: str | None = None, ext: str | None = None,
         container: str | None = None, content: bool = False, limit: int = 10, refresh: bool = True) -> dict:
    connection = _connect(project, refresh)
    try:
        info, inbound = _containers(connection), _inbound_counts(connection)
        text_terms, query_dates = parse_query(terms)
        expanded = expand_terms(text_terms, project.synonyms())
        ext_value = ("." + ext.lstrip(".").lower()) if ext else None
        scored = []
        for row in connection.execute("SELECT * FROM files"):
            if (kind and row["role"] != kind) or (ext_value and row["ext"] != ext_value):
                continue
            if container and container.lower() not in row["container"].lower():
                continue
            if query_dates:
                file_dates = set(row["dates"].split())
                if not any((year + md in file_dates) if year else (md in file_dates) for year, md in query_dates):
                    continue
            owner = info.get(row["container"], {})
            name, title = row["name"].lower(), row["title"].lower()
            head = " ".join([name, title, row["path"].lower()])
            meta = " ".join([owner.get("title", ""), owner.get("goal", ""), owner.get("tags", ""),
                             owner.get("aliases", "")]).lower()
            score, hit, body = 0, True, None
            for alternatives in expanded:
                if any(word in name or word in title for word in alternatives):
                    score += 5
                elif any(word in head for word in alternatives):
                    score += 3
                elif any(word in meta for word in alternatives):
                    score += 1
                elif content and row["ext"] in TEXT_EXT and row["size"] <= CONTENT_LIMIT \
                        and not row["dataless"] and row["git_state"] != "ignored":
                    if body is None:
                        try:
                            body = (project.root / row["path"]).read_text(encoding="utf-8", errors="replace").lower()
                        except OSError:
                            body = ""
                    if any(word in body for word in alternatives):
                        score += 2
                    else:
                        hit = False
                else:
                    hit = False
                if not hit:
                    break
            if not hit:
                continue
            score += (4 if query_dates else 0) + ROLE_SCORE.get(row["role"], 0)
            score += {"active": 2, "waiting": 1}.get(owner.get("status", ""), 0)
            scored.append((score, row["mtime_ns"], row))
        groups, total = _group_families(scored, info, inbound, limit)
        return {"ok": True, "total": total, "files": len(scored), "returned": len(groups), "results": groups,
                "query": {"terms": text_terms, "dates": [f"{year or '*'}-{md}" for year, md in query_dates]}}
    finally:
        connection.close()


def recent(project: Project, *, limit: int = 15, refresh: bool = True) -> dict:
    connection = _connect(project, refresh)
    try:
        info, inbound = _containers(connection), _inbound_counts(connection)
        rows = connection.execute("SELECT * FROM files WHERE role IN ('deliverable','draft','project-file') "
                                  "ORDER BY mtime_ns DESC LIMIT 400").fetchall()
        groups, _total = _group_families([(0, row["mtime_ns"], row) for row in rows], info, inbound, limit)
        return {"ok": True, "results": groups, "returned": len(groups)}
    finally:
        connection.close()


def cases(project: Project, term: str | None = None, *, limit: int = 30, refresh: bool = True) -> dict:
    connection = _connect(project, refresh)
    try:
        counts = {row[0]: row[1] for row in connection.execute("SELECT container, COUNT(*) FROM files GROUP BY container")}
        groups = project.synonyms()
        rows = []
        for row in connection.execute("SELECT * FROM containers WHERE kind = 'case'"):
            haystack = " ".join([row["id"], row["title"], row["goal"], row["tags"], row["aliases"]]).lower()
            if term:
                words, _dates = parse_query([term])
                if not all(any(word in haystack for word in alternatives)
                           for alternatives in expand_terms(words, groups)):
                    continue
            rows.append({"id": row["id"], "title": row["title"], "status": row["status"],
                         "updated_at": row["updated_at"], "files": counts.get(row["id"], 0)})
        rows.sort(key=lambda item: item["updated_at"], reverse=True)
        rows.sort(key=lambda item: STATUS_ORDER.get(item["status"], 6))
        return {"ok": True, "total": len(rows), "results": rows[:limit]}
    finally:
        connection.close()


def _inbound_rows(connection: sqlite3.Connection, path: str) -> list[dict]:
    out = [{"src": row["src"], "type": row["type"], "via": "file"}
           for row in connection.execute("SELECT src, type FROM edges WHERE dst = ?", (path,))]
    parent = os.path.dirname(path)
    while parent:
        for row in connection.execute("SELECT src, type FROM edges WHERE dst = ?", (parent + "/",)):
            out.append({"src": row["src"], "type": row["type"], "via": "directory:" + parent + "/"})
        parent = os.path.dirname(parent)
    return out


def refs(project: Project, path: str, *, depth: int = 1, refresh: bool = True) -> dict:
    connection = _connect(project, refresh)
    try:
        path = path.strip()
        while path.startswith("./"):
            path = path[2:]
        if not connection.execute("SELECT 1 FROM files WHERE path = ?", (path,)).fetchone() and \
                not connection.execute("SELECT 1 FROM edges WHERE dst = ? OR dst = ?",
                                       (path, path.rstrip("/") + "/")).fetchone():
            raise AssetError(_("台账里没有这个路径：{0}").format(path), "unknown-path")
        outbound = [{"dst": row["dst"], "type": row["type"]}
                    for row in connection.execute("SELECT dst, type FROM edges WHERE src = ? ORDER BY dst", (path,))]
        direct, seen_src = [], set()
        for row in _inbound_rows(connection, path):
            if row["src"] not in seen_src:
                seen_src.add(row["src"])
                direct.append(row)
        layers, seen = [direct], {path} | seen_src
        frontier = [row["src"] for row in direct]
        for _depth in range(max(depth, 1) - 1):
            following = []
            for node in frontier:
                for row in _inbound_rows(connection, node):
                    if row["src"] not in seen:
                        seen.add(row["src"])
                        following.append(row)
            if not following:
                break
            layers.append(following)
            frontier = [row["src"] for row in following]
        owner = connection.execute("SELECT container FROM files WHERE path = ?", (path,)).fetchone()
        affected = sorted({project.container_of(row["src"])[0] for layer in layers for row in layer}
                          - ({owner["container"]} if owner else set()))
        return {"ok": True, "path": path, "outbound": outbound,
                "inbound": [dict(row, depth=index + 1) for index, layer in enumerate(layers) for row in layer],
                "affected_containers": affected}
    finally:
        connection.close()


def check(project: Project, *, limit: int = 50, refresh: bool = True) -> dict:
    connection = _connect(project, refresh)
    try:
        info = _containers(connection)
        files = {row["path"]: dict(row) for row in connection.execute("SELECT * FROM files")}
        edges = [(row["src"], row["dst"], row["type"]) for row in connection.execute("SELECT src, dst, type FROM edges")]
        inbound: dict[str, set] = collections.defaultdict(set)
        declared: dict[str, set] = collections.defaultdict(set)
        reference_dirs = set()
        docs_dir = re.compile(r"^" + re.escape(project.case_root) + r"/[^/]+/docs(/|$)")
        for src, dst, kind in edges:
            inbound[dst.rstrip("/")].add(src)
            if kind.startswith("declared"):
                declared[project.container_of(src)[0]].add(dst.rstrip("/"))
            if dst.endswith("/") and docs_dir.match(dst):
                reference_dirs.add(dst.rstrip("/"))
        tiers: collections.Counter = collections.Counter()
        unclaimed: dict[str, list] = collections.defaultdict(list)
        directory_only: dict[str, list] = collections.defaultdict(list)
        for path, item in files.items():
            if item["container_kind"] != "case" or item["role"] not in ("deliverable", "draft") \
                    or item["name"].startswith("~$"):
                continue
            if path in declared[item["container"]] or any(src != path for src in inbound.get(path, ())):
                tiers["file"] += 1
            elif any(path.startswith(directory + "/") for directory in reference_dirs):
                tiers["directory"] += 1
                directory_only[item["container"]].append(path)
            else:
                tiers["none"] += 1
                unclaimed[item["container"]].append(path)
        broken: dict[str, list] = collections.defaultdict(list)
        for row in connection.execute("SELECT src, raw, type, class FROM broken ORDER BY src, raw"):
            broken[row["class"]].append({"src": row["src"], "raw": row["raw"], "type": row["type"]})
        by_hash: dict[str, list] = collections.defaultdict(list)
        for path, item in files.items():
            if item["sha256"] and item["size"] > 10 * 1024:
                by_hash[item["sha256"]].append(path)
        duplicates = [sorted(paths) for paths in by_hash.values()
                      if len(paths) > 1 and len({files[path]["container"] for path in paths}) > 1]
        upstream_newer = []
        for src, dst, kind in edges:
            source, target = files.get(src), files.get(dst)
            if not source or not target or source["container"] == target["container"] or kind == "case-id":
                continue
            if target["mtime_ns"] > source["mtime_ns"] and target["container_kind"] == source["container_kind"] == "case":
                upstream_newer.append({"src": src, "dst": dst, "dst_modified": _iso(target["mtime_ns"] / 1e9),
                                       "src_modified": _iso(source["mtime_ns"] / 1e9)})
        meta = _meta(connection)
        return {
            "ok": True, "built_at": meta.get("built_at", ""),
            "totals": {"files": len(files), "edges": len(edges), "denied": int(meta.get("denied", 0)),
                       "git": dict(collections.Counter(item["git_state"] for item in files.values()))},
            "claim_tiers": dict(tiers),
            "unclaimed": {key: value[:limit] for key, value in sorted(unclaimed.items())},
            "directory_only": {key: len(value) for key, value in sorted(directory_only.items())},
            "broken": {key: value[:limit] for key, value in broken.items()},
            "broken_counts": {key: len(value) for key, value in broken.items()},
            "duplicates": duplicates[:limit],
            "upstream_newer": upstream_newer[:limit],
            "untracked_new": sorted(path for path, item in files.items() if item["git_state"] == "untracked")[:limit],
            "skipped": json.loads(meta.get("skipped", "[]"))[:limit],
            "case_status": dict(collections.Counter(item["status"] for item in info.values() if item["kind"] == "case")),
        }
    finally:
        connection.close()


# ---------------------------------------------------------------- cli

def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=_("交付物台账：登记 Case 与额外扫描根下的文件，按标题、日期、同义词查找，追踪版本与引用。"))
    result.add_argument("--project", type=Path, default=None)
    result.add_argument("--output", choices=("text", "json"), default="text")
    # The same options are accepted after the subcommand, where people tend to type them.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--project", type=Path, default=argparse.SUPPRESS)
    shared.add_argument("--output", choices=("text", "json"), default=argparse.SUPPRESS)
    sub = result.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, refresh: bool = True) -> argparse.ArgumentParser:
        item = sub.add_parser(name, parents=[shared], help=help_text)
        if refresh:
            item.add_argument("--no-refresh", action="store_true", help=_("不先增量重建，直接读现有台账"))
        return item

    build = command("rebuild", _("重建台账（文件清单未变时为 no-op）"), refresh=False)
    build.add_argument("--force", action="store_true")
    command("status", _("台账是否最新、位置与扫描根"), refresh=False)
    search = command("find", _("按标题、文件名、日期与同义词查找，同一系列的版本归为一组"))
    search.add_argument("terms", nargs="*")
    search.add_argument("--kind", help=_("角色：deliverable / draft / source / data / image / script / process / project-file"))
    search.add_argument("--ext")
    search.add_argument("--in", dest="container", help=_("限定 Case 或目录（片段匹配）"))
    search.add_argument("--content", action="store_true", help=_("名称不命中时再查文本正文（≤1 MiB）"))
    search.add_argument("--limit", type=int, default=10)
    latest = command("recent", _("最近改动的交付物"))
    latest.add_argument("--limit", type=int, default=15)
    listing = command("cases", _("Case 列表（进行中在前）与各自登记的文件数"))
    listing.add_argument("term", nargs="?")
    listing.add_argument("--limit", type=int, default=30)
    reference = command("refs", _("谁引用它、它引用谁、改动会波及哪些 Case"))
    reference.add_argument("path")
    reference.add_argument("--depth", type=int, default=1)
    inspect = command("check", _("巡检：无人认领的交付物、断链、跨容器重复、上游更新"))
    inspect.add_argument("--limit", type=int, default=50)
    return result


def render_text(command: str, result: dict) -> str:
    if command in ("find", "recent"):
        lines = []
        for index, item in enumerate(result["results"], 1):
            owner = item["case_title"] or item["container"]
            lines.append(f"{index:>2}. {item['title'] or item['name']}  [{owner}] {item['git']}  {item['path']}")
            lines += [_("      版本 {0}  {1}").format(version['name'], version['path']) for version in item["versions"]]
        return "\n".join(lines) or _("没有找到。")
    if command == "cases":
        return "\n".join(_("{0:9} {1}  {2}  ({3} 个文件)").format(item['status'] or '-', item['id'], item['title'], item['files'])
                         for item in result["results"]) or _("没有 Case。")
    if command == "refs":
        lines = [result["path"]]
        lines += [f"  ← {item['src']}" + ("" if item["via"] == "file" else f"  ({item['via']})")
                  for item in result["inbound"]]
        lines += [f"  → {item['dst']}" for item in result["outbound"]]
        if result["affected_containers"]:
            lines.append(_("  改动会波及：") + _("、").join(result["affected_containers"]))
        return "\n".join(lines)
    if command == "check":
        totals, tiers, counts = result["totals"], result["claim_tiers"], result["broken_counts"]
        git = totals["git"]
        lines = [_("文件 {0} · 引用 {1} · 拒绝登记的凭据类文件 {2}").format(totals['files'], totals['edges'], totals['denied']),
                 _("Git：已入库 {0} / 被忽略 {1} / 未提交 {2} / 非 Git {3}").format(git.get('tracked', 0), git.get('ignored', 0), git.get('untracked', 0), git.get('none', 0)),
                 _("交付物：文件级被引用 {0} · 仅目录覆盖 {1} · 无人认领 {2}").format(tiers.get('file', 0), tiers.get('directory', 0), tiers.get('none', 0)),
                 _("断链：{0} · 说明文字 {1} · 旧 Task 路径 {2} · 主题链接 {3}").format(counts.get('real-broken', 0), counts.get('prose-not-path', 0), counts.get('legacy-task-id', 0), counts.get('wikilink-topic', 0))]
        lines += [_("  断链 {0} → {1}").format(item['src'], item['raw']) for item in result["broken"].get("real-broken", [])]
        lines += [_("  无人认领 {0}").format(path) for paths in result["unclaimed"].values() for path in paths]
        lines += [_("  重复 ") + " ≡ ".join(group) for group in result["duplicates"]]
        return "\n".join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)


def run(project: Project, args: argparse.Namespace) -> dict:
    refresh = not getattr(args, "no_refresh", False)
    if args.command == "rebuild":
        return rebuild(project, force=args.force)
    if args.command == "status":
        return status(project)
    if args.command == "find":
        return find(project, args.terms, kind=args.kind, ext=args.ext, container=args.container,
                    content=args.content, limit=args.limit, refresh=refresh)
    if args.command == "recent":
        return recent(project, limit=args.limit, refresh=refresh)
    if args.command == "cases":
        return cases(project, args.term, limit=args.limit, refresh=refresh)
    if args.command == "refs":
        return refs(project, args.path, depth=args.depth, refresh=refresh)
    return check(project, limit=args.limit, refresh=refresh)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        project = Project.discover(args.project, explicit=args.project is not None)
        result = run(project, args)
    except AssetError as exc:
        payload = {"ok": False, "code": exc.code, "error": str(exc)}
        if args.output == "json":
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.output == "json"
          else render_text(args.command, result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
