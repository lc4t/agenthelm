#!/usr/bin/env python3
"""local-only：只属于这台机器的目录（依赖、缓存）不进入云同步盘。

项目放在 iCloud / Dropbox / OneDrive 等同步目录里时，``.venv``、``node_modules`` 这类可重建的大目录
会拖慢同步、产生冲突副本。本模块做三件事：

* ``check``：只读。识别同步服务、找出落在同步目录里的本机专属目录，核对已外置条目的软链接状态、
  HOME 是否与项目约定一致、同步范围内目录的体积是否接近阈值，并给出可对比的“本机环境明细”。
* ``fix``：默认只预览，``--apply`` 才写。把目录移到 ``~/.local/share/agenthelm/local-only/<project_id>/``
  并在原位置建软链接；新设备上补建目标目录；按用户的选择提高阈值。移动失败整体回滚。
* ``config``：显示生效的配置与文件位置。

约定（实测依据见 references/local-only.md）：

* 链接文本必须在所有设备上相同（软链接本身会被同步），所以目标路径由“项目里记录的 HOME + project_id
  + 项目内相对路径”拼成，各设备的 HOME 必须一致；不一致时拒绝写入并报告。
* npm 会把软链接的 ``node_modules`` 换成真实目录，pnpm 会失败：这两个只报告，不外置；yarn（经典版）、
  bun、uv 与普通 venv 实测可以。
* 缓存类（``__pycache__``、``.ruff_cache`` 等）优先用工具自带的环境变量 / 选项外置，本模块只给出写法。

只用标准库；不联网；不读取文件内容（``package.json`` 除外，且跳过 iCloud 未下载的占位文件）。
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from i18n_agent_pack import _, N_  # noqa: E402

CONFIG_PATH = Path(".agents/moe.sakanano.agent-pack/local-only.json")
SCHEMA_VERSION = 1
STORE_PARTS = (".local", "share", "agenthelm", "local-only")
SF_DATALESS = 0x40000000  # macOS: iCloud 未下载的文件（st_flags）
MB = 1024 * 1024

# (id, 显示名, 路径中的目录名)。路径识别只看目录名，不读文件。
SYNC_SERVICES = (
    ("icloud", "iCloud Drive", re.compile(r"^(com~apple~CloudDocs|Mobile Documents|iCloud Drive|iCloudDrive)$")),
    ("dropbox", "Dropbox", re.compile(r"^Dropbox( \(.*\)|-.*)?$")),
    ("onedrive", "OneDrive", re.compile(r"^OneDrive( - .*|-.*)?$")),
    ("google-drive", "Google Drive", re.compile(r"^(GoogleDrive(-.*)?|Google Drive|My Drive)$")),
)
DECLARED_SERVICES = {"icloud": "iCloud Drive", "dropbox": "Dropbox", "onedrive": "OneDrive",
                     "google-drive": "Google Drive", "syncthing": "Syncthing", "other": N_("其他同步服务")}
NOT_SYNCED = "none"

VCS_DIRS = {".git", ".svn", ".hg"}
DEPENDENCY_NAMES = {".tox", ".nox", ".terraform", ".gradle"}
CACHE_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".cache",
               ".eslintcache", ".parcel-cache", ".turbo"}
# 默认保持同步（构建产物与本机工具状态按用户决定同步）；项目可在配置里改。
DEFAULT_KEEP_SYNCED = ("dist", "build", "out", ".claude", ".cursor", ".idea", ".vscode")
DEFAULT_LIMITS = {"warn_ratio": 0.8, "dir_mb": 200, "dir_files": 20000}
RUNTIME_SUFFIXES = ("-wal", "-shm", ".pid", ".sock")
CONFLICT_RE = re.compile(r"\(.*(conflicted copy|Case Conflict).*\)", re.IGNORECASE)
ICLOUD_PLACEHOLDER_RE = re.compile(r"^\..+\.icloud$")
LOCKFILES = (("bun.lockb", "bun"), ("bun.lock", "bun"), ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
             ("package-lock.json", "npm"), ("npm-shrinkwrap.json", "npm"))
# 实测（Linux 容器）：yarn 1.22.22 与 bun 1.4.4 保留软链接并装进目标；npm 10.9.4 把链接换成真实目录；
# pnpm 10.28.0 失败。yarn 2+ 未测。
PM_VERDICT = {"npm": "avoid", "pnpm": "avoid", "yarn": "ok", "bun": "ok"}
PM_ADVICE = {"npm": N_("npm 会把软链接的 node_modules 换成真实目录（实测 10.9.4），依赖会回到同步盘里"),
             "pnpm": N_("pnpm 对软链接的 node_modules 会安装失败并删掉链接目标（实测 10.28.0）")}
CACHE_HINTS = {
    "__pycache__": N_("环境变量 PYTHONPYCACHEPREFIX=<本机目录>"),
    ".ruff_cache": N_("环境变量 RUFF_CACHE_DIR=<本机目录>"),
    ".mypy_cache": N_("环境变量 MYPY_CACHE_DIR=<本机目录>"),
    ".pytest_cache": N_("pytest 选项 -o cache_dir=<本机目录>（或在 pytest 配置里写 cache_dir）"),
}
# 外置后用什么命令在这台机器上重建。
REBUILD_BY_LOCK = (("uv.lock", ["uv", "sync"]), ("poetry.lock", ["poetry", "install"]),
                   ("bun.lockb", ["bun", "install", "--frozen-lockfile"]),
                   ("bun.lock", ["bun", "install", "--frozen-lockfile"]),
                   ("yarn.lock", ["yarn", "install", "--frozen-lockfile"]))
# 时间预算（秒）：会话开始钩子只有 10 秒。
BUDGETS = {"quick": 1.5, "standard": 6.0, "full": 120.0}
QUICK_DEPTH = 2


class LocalOnlyError(Exception):
    def __init__(self, message: str, code: str = "usage") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 小工具

def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_dataless(stat: os.stat_result) -> bool:
    return bool(getattr(stat, "st_flags", 0) & SF_DATALESS)


def _human(size: float) -> str:
    return f"{size / MB:.0f} MB" if size >= MB else f"{size / 1024:.0f} KB"


def store_root(home: str | Path) -> str:
    """外置目录的根（文本形式）：<HOME>/.local/share/agenthelm/local-only。不看 XDG_DATA_HOME。"""
    return "/".join([str(home).rstrip("/"), *STORE_PARTS])


def expected_link(home: str, project_id: str, relative: str) -> str:
    return f"{store_root(home)}/{project_id}/{relative}"


# ---------------------------------------------------------------- 配置

@dataclass
class Config:
    path: Path
    exists: bool = False
    errors: list[str] = field(default_factory=list)
    project_id: str | None = None
    home: str | None = None
    service: str | None = None
    items: list[dict] = field(default_factory=list)
    limits: dict = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    overrides: dict[str, dict] = field(default_factory=dict)
    keep_synced: list[str] = field(default_factory=lambda: list(DEFAULT_KEEP_SYNCED))
    ignore: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def limit_for(self, bucket: str) -> tuple[float, int]:
        override = self.overrides.get(bucket, {})
        return float(override.get("dir_mb", self.limits["dir_mb"])), int(override.get("dir_files", self.limits["dir_files"]))

    @property
    def complete(self) -> bool:
        return bool(self.project_id and self.home)


def _apply_limits(target: dict, source: Any, label: str, errors: list[str]) -> None:
    if not isinstance(source, dict):
        errors.append(_("{0}：limits 必须是对象").format(label))
        return
    for key, kind in (("warn_ratio", float), ("dir_mb", float), ("dir_files", int)):
        if key not in source:
            continue
        value = source[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 \
                or (key == "warn_ratio" and value > 1):
            errors.append(_("{0}：limits.{1} 必须是正数").format(label, key) + (_("且不超过 1") if key == "warn_ratio" else ""))
            continue
        target[key] = kind(value)


def _string_list(source: Mapping, key: str, label: str, errors: list[str]) -> list[str] | None:
    if key not in source:
        return None
    value = source[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        errors.append(_("{0}：{1} 必须是字符串数组").format(label, key))
        return None
    return list(value)


def load_config(root: Path, user_config: Path | None = None) -> Config:
    """优先级：内置默认 < 用户配置（~/.config/agenthelm/config.json 的 local_only） < 项目配置。

    项目配置赢过用户配置：阈值影响各设备得出的结论，应当随项目同步，而不是每台机器各一套。
    """
    config = Config(path=root / CONFIG_PATH)
    if user_config is not None:
        try:
            section = _read_json(user_config).get("local_only")
        except (OSError, ValueError, AttributeError):
            section = None
        if isinstance(section, dict):
            if "limits" in section:
                _apply_limits(config.limits, section["limits"], _("用户配置 {0}").format(user_config), config.errors)
            for key, attribute in (("keep_synced", "keep_synced"), ("ignore", "ignore")):
                value = _string_list(section, key, _("用户配置 {0}").format(user_config), config.errors)
                if value is not None:
                    setattr(config, attribute, value)
    path = config.path
    if path.is_symlink() or not path.is_file():
        return config
    try:
        raw = _read_json(path)
        if not isinstance(raw, dict):
            raise ValueError(_("顶层必须是对象"))
    except (OSError, ValueError) as exc:
        config.errors.append(_("{0} 无法读取：{1}").format(CONFIG_PATH.as_posix(), exc))
        return config
    config.exists, config.raw = True, raw
    label = CONFIG_PATH.as_posix()
    if raw.get("schema_version") != SCHEMA_VERSION:
        config.errors.append(_("{0}：schema_version 必须是 {1}").format(label, SCHEMA_VERSION))
    for key in ("project_id", "home"):
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            config.errors.append(_("{0}：{1} 必须是非空字符串").format(label, key))
        elif value:
            setattr(config, key, value)
    service = raw.get("sync_service")
    if service is not None:
        if service != NOT_SYNCED and service not in DECLARED_SERVICES:
            config.errors.append(_("{0}：sync_service 只能是 {1}").format(label, _('、').join([*DECLARED_SERVICES, NOT_SYNCED])))
        else:
            config.service = service
    items = raw.get("items", [])
    if not isinstance(items, list) or not all(
            isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"] for item in items):
        config.errors.append(_("{0}：items 必须是 [{{\"path\": …, \"kind\": …}}] 数组").format(label))
    else:
        config.items = [{"path": item["path"], "kind": str(item.get("kind") or "declared")} for item in items]
        for item in config.items:
            problem = _unsafe_relative(item["path"])
            if problem:
                config.errors.append(_("{0}：items 里的 {1} {2}").format(label, item['path'], problem))
    if "limits" in raw:
        _apply_limits(config.limits, raw["limits"], label, config.errors)
        overrides = raw["limits"].get("overrides") if isinstance(raw["limits"], dict) else None
        if isinstance(overrides, dict):
            for bucket, values in overrides.items():
                probe: dict = {}
                _apply_limits(probe, values, _("{0} 的 limits.overrides.{1}").format(label, bucket), config.errors)
                if probe:
                    config.overrides[bucket] = probe
    for key, attribute in (("keep_synced", "keep_synced"), ("ignore", "ignore"), ("extra_local_only", "extra")):
        value = _string_list(raw, key, label, config.errors)
        if value is not None:
            setattr(config, attribute, value)
    return config


def _unsafe_relative(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or value in {".", ""}:
        return _("必须是项目内的相对路径")
    if path.parts[0] in VCS_DIRS:
        return _("不能在版本库目录里")
    return ""


def _config_document(config: Config, **changes: Any) -> dict:
    """要写盘的配置：保留用户写的其余字段，只更新这里管理的字段。"""
    document = dict(config.raw)
    document["schema_version"] = SCHEMA_VERSION
    for key in ("project_id", "home", "sync_service", "items"):
        value = changes.get(key, {"project_id": config.project_id, "home": config.home, "sync_service": config.service,
                                  "items": config.items}[key])
        if value is not None:
            document[key] = value
    if "limit_overrides" in changes:
        limits = dict(document.get("limits") if isinstance(document.get("limits"), dict) else {})
        overrides = dict(limits.get("overrides") if isinstance(limits.get("overrides"), dict) else {})
        for bucket, values in changes["limit_overrides"].items():
            overrides[bucket] = {**(overrides.get(bucket) if isinstance(overrides.get(bucket), dict) else {}), **values}
        limits["overrides"] = overrides
        document["limits"] = limits
    return document


# ---------------------------------------------------------------- 同步服务

def detect_service(root: Path, declared: str | None) -> dict | None:
    """已声明的优先；否则按路径里的目录名识别（macOS 的“桌面与文稿同步”路径上没有标记，只能声明）。"""
    if declared == NOT_SYNCED:
        return None
    if declared:
        return {"id": declared, "name": _(DECLARED_SERVICES[declared]), "source": "declared"}
    resolved = root.resolve()
    for identifier, name, pattern in SYNC_SERVICES:
        if any(pattern.match(part) for part in resolved.parts):
            return {"id": identifier, "name": name, "source": "path"}
    return None


# ---------------------------------------------------------------- 扫描

@dataclass
class Scan:
    candidates: list[dict] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)
    package_json: dict[str, dict] = field(default_factory=dict)
    runtime_files: list[str] = field(default_factory=list)
    conflict_copies: list[str] = field(default_factory=list)
    dataless: int = 0
    buckets: dict[str, dict] = field(default_factory=dict)
    truncated: bool = False


def _classify(name: str, rel: str, path: Path, config: Config) -> str | None:
    if rel in config.keep_synced or name in config.keep_synced:
        return None
    if rel in config.extra:
        return "declared"
    if name == "node_modules":
        return "node_modules"
    if name in DEPENDENCY_NAMES:
        return "dependency"
    if name in CACHE_NAMES:
        return "cache"
    if (path / "pyvenv.cfg").is_file():
        return "venv"
    if (path / "CACHEDIR.TAG").is_file():
        return "cache"
    return None


def _package_manager(directory: Path, names: set[str], relative: str) -> dict:
    declared, version = None, ""
    try:
        stat = (directory / "package.json").lstat()
        if not _is_dataless(stat) and stat.st_size <= MB:
            value = _read_json(directory / "package.json").get("packageManager")
            if isinstance(value, str):
                declared = value.split("@", 1)[0].lower()
                version = value.split("@", 1)[1] if "@" in value else ""
    except (OSError, ValueError, AttributeError):
        pass
    locks = {manager for lock, manager in LOCKFILES if lock in names}
    if declared in PM_VERDICT:
        manager, source = declared, "packageManager"
    elif len(locks) == 1:
        manager, source = next(iter(locks)), "lockfile"
    else:
        manager, source = None, "unknown"
    verdict = PM_VERDICT.get(manager or "", "unknown")
    if manager == "yarn":
        berry = ".yarnrc.yml" in names or (declared == "yarn" and version[:1].isdigit() and int(version.split(".")[0]) >= 2)
        if berry:
            verdict = "unverified"
    return {"dir": relative, "pm": manager, "source": source, "verdict": verdict}


def scan_tree(root: Path, config: Config, *, quick: bool, deadline: float) -> Scan:
    scan = Scan()
    stack: list[tuple[Path, str, int]] = [(root, "", 0)]
    while stack:
        if time.monotonic() > deadline:
            scan.truncated = True
            break
        directory, relative, depth = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            continue
        names = {entry.name for entry in entries}
        if "package.json" in names:
            scan.package_json[relative or "."] = _package_manager(directory, names, relative or ".")
        for entry in entries:
            child = f"{relative}/{entry.name}" if relative else entry.name
            if child in config.ignore or entry.name in config.ignore:
                continue
            try:
                if entry.is_symlink():
                    scan.links[child] = os.readlink(entry.path)
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if entry.name in VCS_DIRS:
                    continue
                kind = _classify(entry.name, child, Path(entry.path), config)
                if kind:
                    scan.candidates.append({"path": child, "kind": kind, "name": entry.name})
                elif not quick or depth < QUICK_DEPTH:
                    stack.append((Path(entry.path), child, depth + 1))
                continue
            name = entry.name
            if name.endswith(RUNTIME_SUFFIXES):
                scan.runtime_files.append(child)
            if CONFLICT_RE.search(name):
                scan.conflict_copies.append(child)
            if ICLOUD_PLACEHOLDER_RE.match(name):
                scan.dataless += 1
            if quick:
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if _is_dataless(stat):
                scan.dataless += 1
            bucket = scan.buckets.setdefault(relative.split("/", 1)[0] if relative else ".", {"bytes": 0, "files": 0})
            bucket["bytes"] += stat.st_size
            bucket["files"] += 1
    return scan


def measure(path: Path, deadline: float) -> dict:
    """目录的字节数与文件数；超过时间预算就停，结果标记为下限。"""
    total, files, truncated = 0, 0, False
    stack = [path]
    while stack:
        if time.monotonic() > deadline:
            truncated = True
            break
        try:
            with os.scandir(stack.pop()) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return {"bytes": total, "files": files, "truncated": truncated}


# ---------------------------------------------------------------- 评估

def rebuild_command(root: Path, relative: str) -> list[str] | None:
    """在条目所在目录里用锁文件推断重建命令；找不到就返回 None。"""
    parent = (root / relative).parent
    for lock, command in REBUILD_BY_LOCK:
        if (parent / lock).is_file():
            return list(command)
    return None


def _item_state(root: Path, relative: str, expected: str) -> tuple[str, str | None]:
    path = root / relative
    if path.is_symlink():
        text = os.readlink(path)
        if text != expected:
            return "wrong-target", text
        return ("ok" if Path(expected).is_dir() else "not-installed"), text
    if not os.path.lexists(path):
        return "absent", None
    return ("reverted" if path.is_dir() else "conflict"), None


def _linkable(candidate: dict, managers: dict[str, dict]) -> tuple[bool, str]:
    """(能否由 fix 外置, 原因)。只有实测过的种类才默认外置。"""
    kind = candidate["kind"]
    if kind == "venv":
        return True, ""
    if kind == "node_modules":
        parent = candidate["path"].rpartition("/")[0] or "."
        info = managers.get(parent) or {}
        if info.get("verdict") == "avoid":
            return False, _(PM_ADVICE[info["pm"]])
        if info.get("verdict") == "unverified":
            return False, _("yarn 2 及以上（PnP / node-modules 链接方式）未测试")
        return True, ""
    if kind == "declared":
        return True, ""
    return False, _("这类目录没有实测过软链接外置：确认后可用 --externalize 指定")


def _finding(identifier: str, kind: str, title: str, reason: str, impact: str, fingerprint: str, *,
             attention: bool = False, options: list[dict] | None = None, data: dict | None = None) -> dict:
    options = options or []
    return {"id": identifier, "kind": kind, "level": "suggest", "scope": "machine", "title": title, "reason": reason,
            "impact": impact, "fingerprint": fingerprint, "fix": "local-only", "attention": attention,
            "options": options, "next": options[0]["label"] if options else "", "data": data or {}}


def _command(*parts: str) -> str:
    return " ".join(["agenthelm", "local-only", *parts])


def check(root: Path, *, home: str | Path, environ: Mapping[str, str] | None = None, mode: str = "standard",
          user_config: Path | None = None) -> dict:
    """只读评估。没有同步服务也没有配置的项目：直接返回空发现，不扫描。"""
    environ = environ or {}
    root = root.resolve()
    config = load_config(root, user_config)
    service = detect_service(root, config.service)
    home_text = str(home)
    report: dict = {
        "root": str(root), "mode": mode, "truncated": False, "service": service,
        "config": {"path": str(config.path), "exists": config.exists, "project_id": config.project_id,
                   "home": config.home, "errors": list(config.errors)},
        "home": {"expected": config.home, "actual": home_text, "ok": config.home in (None, home_text)},
        "items": [], "candidates": [], "package_managers": [], "sizes": [], "notes": [], "findings": [],
        "limits": {**config.limits},
    }
    findings: list[dict] = report["findings"]
    if config.errors:
        findings.append(_finding("local:local-only-config", "config", _("local-only 配置有问题"), _("；").join(config.errors),
                                 _("有问题的配置项不生效，其余按默认处理"), _sha("\n".join(config.errors)),
                                 options=[{"label": _("修正 {0} 后重新检查").format(CONFIG_PATH.as_posix()), "command": ""}]))
    if service is None and not config.items:
        if sys.platform == "darwin":
            documents = [home_text + "/Documents", home_text + "/Desktop"]
            if any(str(root) == p or str(root).startswith(p + "/") for p in documents):
                report["notes"].append(_("项目在 ~/Documents 或 ~/Desktop 下：如果开了 iCloud“桌面与文稿”同步，路径上看不出来，"
                                       "请在配置里声明 sync_service（fix --service icloud）"))
        return _finish(report, config)
    quick = mode == "quick"
    deadline = time.monotonic() + BUDGETS.get(mode, BUDGETS["standard"])
    scan = scan_tree(root, config, quick=quick, deadline=deadline)
    report["truncated"] = scan.truncated
    managers = {entry["dir"]: entry for entry in scan.package_json.values()}
    report["package_managers"] = sorted(managers.values(), key=lambda entry: entry["dir"])
    mismatch = not report["home"]["ok"]
    if mismatch:
        findings.append(_finding(
            "local:home-mismatch", "home-mismatch", _("这台机器的 HOME 与项目约定不一致"),
            _("项目记录的是 {0}，这台是 {1}").format(config.home, home_text), _("软链接的文本必须在所有设备上相同；不一致时 fix 拒绝建立或修改链接，"
            "已有链接在这台机器上也可能悬空"), _sha(f"{config.home}|{home_text}"), attention=True,
            options=[{"label": _("让各台设备的 HOME 路径一致（例如相同的用户名），或在配置里核对 home 字段"), "command": ""}]))
    # 已声明的外置条目
    item_states: dict[str, str] = {}
    if config.complete:
        for item in config.items:
            relative = item["path"]
            expected = expected_link(config.home, config.project_id, relative)
            state, actual = ("home-mismatch", None) if mismatch else _item_state(root, relative, expected)
            item_states[relative] = state
            command = rebuild_command(root, relative)
            report["items"].append({"path": relative, "kind": item["kind"], "state": state, "expected": expected,
                                    "actual": actual, "rebuild": command})
            findings.extend(_item_findings(item, state, expected, actual, command))
    elif config.items:
        report["notes"].append(_("配置里有外置条目，但缺少 project_id 或 home：运行 fix --apply 补全"))
    declared = set(item_states)
    # 落在同步目录里的本机专属目录
    synced = service is not None
    for candidate in scan.candidates:
        linkable, why = _linkable(candidate, managers)
        entry = {**candidate, "linkable": linkable, "reason": why}
        if candidate["path"] in declared:
            continue  # 已声明的条目由 local:link:<路径> 说明
        report["candidates"].append(entry)
    if synced:
        _candidate_findings(root, report, config, quick)
    # 包管理器
    if synced:
        for entry in report["package_managers"]:
            if entry["verdict"] == "avoid":
                findings.append(_finding(
                    f"local:pm:{entry['dir']}", "package-manager", _("同步目录里用 {0}（{1}）").format(entry['pm'], entry['dir']),
                    _(PM_ADVICE[entry["pm"]]), _("node_modules 无法外置到本机，会随项目同步"), _sha(f"pm:{entry['pm']}"),
                    options=[{"label": _("尽量改用 yarn（经典版）或 bun，它们实测可以把 node_modules 外置；"
                                       "换包管理器会改 lock 文件，由你决定"), "command": ""}]))
    # 体积
    if not quick and synced:
        _size_findings(report, scan, config)
    # 其他同步问题
    if synced and scan.runtime_files:
        sample = _("、").join(scan.runtime_files[:5]) + (_(" 等 {0} 个").format(len(scan.runtime_files)) if len(scan.runtime_files) > 5 else "")
        findings.append(_finding(
            "local:runtime-files", "runtime-files", _("同步目录里有只属于运行中进程的文件"), sample,
            _("SQLite 的 -wal/-shm、pid、socket 文件被多台设备同时同步会损坏数据库或误导进程"),
            _sha("\n".join(sorted(scan.runtime_files))),
            options=[{"label": _("把数据库、pid、socket 放到项目外（例如 ~/.local/state），或在同步服务里排除这些文件"), "command": ""}]))
    if synced and scan.conflict_copies:
        sample = _("、").join(scan.conflict_copies[:5]) + (_(" 等 {0} 个").format(len(scan.conflict_copies)) if len(scan.conflict_copies) > 5 else "")
        findings.append(_finding(
            "local:conflict-copies", "conflict-copies", _("同步服务留下了冲突副本"), sample,
            _("两台设备改了同一个文件，其中一份只剩在副本里"), _sha("\n".join(sorted(scan.conflict_copies))),
            options=[{"label": _("比对后合并进原文件并删除副本（命名规则按 Dropbox 的“conflicted copy”，iCloud 的未核对）"),
                      "command": ""}]))
    if synced and scan.dataless:
        report["notes"].append(_("有 {0} 个文件尚未下载到本机（iCloud 占位文件）：读取它们会触发下载，agenthelm 的扫描不会读取这些文件").format(scan.dataless))
    if scan.truncated:
        report["notes"].append(_("扫描超过时间预算，结果只覆盖了已扫描的部分：用 local-only check --mode full 做完整扫描"))
    store_env = environ.get("XDG_DATA_HOME")
    if store_env and store_env.rstrip("/") != home_text.rstrip("/") + "/.local/share":
        report["notes"].append(_("XDG_DATA_HOME={0}：外置目录固定在 {1}，不跟随这个变量（链接文本要各设备一致）").format(store_env, store_root(home_text)))
    return _finish(report, config)


def _item_findings(item: dict, state: str, expected: str, actual: str | None, command: list[str] | None) -> list[dict]:
    relative = item["path"]
    rebuild = _("然后在项目里运行 {0}").format(' '.join(command)) if command else _("然后按项目文档重新安装依赖")
    if state in ("ok", "home-mismatch"):
        return []
    identifier = f"local:link:{relative}"
    fix = {"label": _("建立本机目录与链接（{0}）；{1}").format(_command('fix', '--apply'), rebuild), "command": _command("fix", "--apply")}
    if state == "not-installed":
        return [_finding(identifier, "link", _("这台机器还没有安装 {0}").format(relative),
                         _("链接 {0} 指向 {1}，本机上没有这个目录").format(relative, expected),
                         _("不装好就无法使用 {0}；此时直接运行 uv sync 之类的命令会报“File exists”").format(relative),
                         _sha(f"not-installed:{expected}"), options=[fix])]
    if state == "absent":
        return [_finding(identifier, "link", _("项目声明了外置的 {0}，这台机器上还没有").format(relative),
                         _("{0} 既不是链接也不存在（链接可能还没同步过来，或同步服务不同步软链接）").format(relative),
                         _("这台机器上用到它的命令会因为缺少依赖失败"), _sha(f"absent:{expected}"), options=[fix])]
    if state == "reverted":
        return [_finding(identifier, "link", _("{0} 又变回了真实目录").format(relative),
                         _("本应是指向 {0} 的链接；常见原因是 npm install 或手工重装把链接换掉了").format(expected),
                         _("依赖回到了同步盘里"), _sha(f"reverted:{expected}"), attention=True,
                         options=[{"label": _("重新外置（{0}）；目标目录已有内容时需要你先处理").format(_command('fix', '--externalize', relative, '--apply')), "command":
                                   _command("fix", "--externalize", relative, "--apply")}])]
    if state == "wrong-target":
        return [_finding(identifier, "link", _("{0} 的链接指向了别处").format(relative), _("期望 {0}，实际 {1}").format(expected, actual),
                         _("各设备的链接文本不同会造成同步冲突或悬空"), _sha(f"wrong:{actual}"), attention=True,
                         options=[{"label": _("这是你自己的链接就保持不动并在配置里去掉该条目；否则手动改回期望的目标"), "command": ""}])]
    return [_finding(identifier, "link", _("{0} 既不是目录也不是链接").format(relative), _("路径被一个普通文件占用"),
                     _("无法外置"), _sha(f"conflict:{relative}"), attention=True,
                     options=[{"label": _("先处理这个文件"), "command": ""}])]


def _candidate_findings(root: Path, report: dict, config: Config, quick: bool) -> None:
    findings = report["findings"]
    caches: list[dict] = []
    measuring = time.monotonic() + 8  # 所有目录共用的体积统计预算
    for candidate in report["candidates"]:
        kind, relative = candidate["kind"], candidate["path"]
        if kind == "cache":
            caches.append(candidate)
            continue
        size = {"bytes": 0, "files": 0, "truncated": True} if quick else measure(root / relative, measuring)
        candidate["size"] = size
        amount = "" if quick else _("，约 {0}{1}、{2} 个文件").format('≥' if size['truncated'] else '', _human(size['bytes']), size['files'])
        if candidate["linkable"]:
            options = [{"label": _("外置到本机并在原位置建链接（{0}）").format(_command('fix', '--externalize', relative, '--apply')),
                        "command": _command("fix", "--externalize", relative, "--apply")}]
            impact = _("整个目录随项目同步：同步很慢，不同系统或架构的二进制还会互相覆盖")
        else:
            options = [{"label": candidate["reason"], "command": ""}]
            impact = _("整个目录随项目同步，但这一类目前没有已验证的外置办法")
        findings.append(_finding(f"local:real:{relative}", "real-in-cloud", _("同步目录里有本机专属目录 {0}").format(relative),
                                 _("{0}（{1}）是真实目录{2}").format(relative, kind, amount), impact, _sha(f"real:{kind}:{candidate['linkable']}"),
                                 attention=True, options=options, data={"kind": kind, "linkable": candidate["linkable"]}))
    if caches:
        lines = []
        for candidate in caches:
            hint = _(CACHE_HINTS[candidate["name"]]) if candidate["name"] in CACHE_HINTS else ""
            lines.append(candidate["path"] + (_("（{0}）").format(hint) if hint else ""))
        findings.append(_finding(
            "local:cache", "cache", _("同步目录里有 {0} 个工具缓存目录").format(len(caches)), _("；").join(lines[:8]) + (_(" 等") if len(lines) > 8 else ""),
            _("缓存会不断变化，每次变化都触发同步"),
            _sha("\n".join(sorted(entry["path"] for entry in caches))),
            options=[{"label": _("用工具自带的环境变量把缓存放到 {0}/…（实测 PYTHONPYCACHEPREFIX、RUFF_CACHE_DIR、MYPY_CACHE_DIR 与 pytest -o cache_dir 均有效）").format(store_root(config.home or '~')), "command": ""}]))


def _limit_text(megabytes: float | None, files: int | None) -> str:
    parts = ([f"{megabytes:g} MB"] if megabytes else []) + ([_("{0} 个文件").format(files)] if files else [])
    return _(" 或 ").join(parts)


def _size_findings(report: dict, scan: Scan, config: Config) -> None:
    warn = config.limits["warn_ratio"]
    for bucket, usage in sorted(scan.buckets.items()):
        limit_mb, limit_files = config.limit_for(bucket)
        ratio = max(usage["bytes"] / MB / limit_mb, usage["files"] / limit_files)
        state = "exceeded" if ratio >= 1 else "approaching" if ratio >= warn else None
        entry = {"path": bucket, "bytes": usage["bytes"], "files": usage["files"], "ratio": round(ratio, 2),
                 "state": state, "limit_mb": limit_mb, "limit_files": limit_files}
        report["sizes"].append(entry)
        if state is None:
            continue
        target = bucket if bucket != "." else _("（项目根的散文件）")
        mb = max(limit_mb * 2, round(usage["bytes"] / MB * 1.25))
        count = max(limit_files * 2, round(usage["files"] * 1.25))
        quoted = bucket if bucket != "." else "."
        raise_to = f"{quoted}={mb:g}/{count}"
        options = [
            {"label": _("确认这个体积正常，把阈值提到 {0:g} MB 或 {1} 个文件（{2}）").format(mb, count, _command('fix', '--raise-limit', raise_to, '--apply')),
             "command": _command("fix", "--raise-limit", raise_to, "--apply")},
            {"label": _("不再提示（agenthelm fix --skip local:size:{0} --reason …；只记在这台机器上）").format(bucket), "command": ""},
            {"label": _("在同步服务里把它设为不同步，或移出同步目录（本工具不替你做；普通内容外置会让其他设备看不到）"), "command": ""},
        ]
        words = _("已超过") if state == "exceeded" else _("接近")
        report["findings"].append(_finding(
            f"local:size:{bucket}", "size", _("{0} 的体积{1}阈值").format(target, words),
            _("{0}、{1} 个文件；阈值 {2}（达到 {3:.0%} 开始提示）").format(_human(usage['bytes']), usage['files'], _limit_text(limit_mb, limit_files), warn),
            _("同步范围太大会拖慢同步，也容易产生冲突副本"), _sha(f"size:{state}:{limit_mb}:{limit_files}"),
            attention=state == "exceeded", options=options,
            data={"bucket": bucket, "state": state, "ratio": entry["ratio"]}))


def _finish(report: dict, config: Config) -> dict:
    detail: dict[str, str] = {}
    if config.exists:
        stable = {"project_id": config.project_id, "service": config.service or "",
                  "items": sorted((item["path"], item["kind"]) for item in config.items)}
        detail["config"] = _sha(json.dumps(stable, sort_keys=True, ensure_ascii=False))
        detail["home"] = report["home"]["actual"]
        for item in report["items"]:
            detail[f"link:{item['path']}"] = f"{item['state']}|{item['expected']}"
    report["fingerprint"] = {"fingerprint": _sha(json.dumps(detail, sort_keys=True)) if detail else "", "detail": detail}
    report["summary"] = {"findings": len(report["findings"]),
                         "attention": sum(1 for finding in report["findings"] if finding["attention"])}
    return report


def explain(mine: Mapping[str, str], theirs: Mapping[str, str]) -> list[str]:
    """两台机器的本机环境明细差在哪里。"""
    lines = []
    for key in sorted(set(mine) | set(theirs)):
        if mine.get(key) == theirs.get(key):
            continue
        if key == "home":
            lines.append(_("HOME 不同：这台 {0}，对方 {1}（外置链接要求一致）").format(mine.get('home'), theirs.get('home')))
        elif key == "config":
            lines.append(_("local-only 配置不同（project_id / 同步服务 / 外置条目）：等项目文件同步完成后再比较"))
        elif key.startswith("link:"):
            path = key[len("link:"):]
            ours, other = mine.get(key), theirs.get(key)
            if ours is None or other is None:
                lines.append(_("外置条目 {0}：只有{1}声明了").format(path, _('这台') if other is None else _('对方')))
                continue
            (state_a, text_a), (state_b, text_b) = ours.split("|", 1), other.split("|", 1)
            if text_a != text_b:
                lines.append(_("外置条目 {0}：链接文本不同（{1} ≠ {2}）").format(path, text_a, text_b))
            if state_a != state_b:
                lines.append(_("外置条目 {0}：这台 {1}，对方 {2}").format(path, state_a, state_b))
        else:
            lines.append(_("{0}：这台 {1}，对方 {2}").format(key, mine.get(key), theirs.get(key)))
    return lines


# ---------------------------------------------------------------- fix

def _same_device(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def _tree_stats(path: Path) -> tuple[int, int]:
    files = size = 0
    for current, _dirs, names in os.walk(path):
        for name in names:
            files += 1
            try:
                size += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                pass
    return files, size


def _move_tree(source: Path, destination: Path, undo: list) -> None:
    """同设备用 rename；跨设备先复制并核对文件数与字节数，核对通过才删原目录。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, destination)
        undo.append(lambda: os.rename(destination, source))
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    shutil.copytree(source, destination, symlinks=True)
    undo.append(lambda: shutil.rmtree(destination, ignore_errors=True))
    if _tree_stats(source) != _tree_stats(destination):
        raise LocalOnlyError(_("复制到本机目录后文件数或字节数对不上，已撤销，原目录没有动"), "failed")
    shutil.rmtree(source)
    # 原目录已删除；之后的步骤失败时，把本机目录里的内容复制回原位置。
    undo.append(lambda: (shutil.copytree(destination, source, symlinks=True), shutil.rmtree(destination, ignore_errors=True)))


def _validate_relative(root: Path, relative: str) -> Path:
    problem = _unsafe_relative(relative)
    if problem:
        raise LocalOnlyError(f"{relative} {problem}")
    current = root
    parts = Path(relative).parts
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise LocalOnlyError(_("{0} 的上级 {1} 是符号链接，拒绝处理").format(relative, part))
    return root / relative


def _document_digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None


def plan_fix(root: Path, *, home: str | Path, environ: Mapping[str, str] | None = None, user_config: Path | None = None,
             externalize: list[str] | None = None, raise_limit: dict[str, dict] | None = None,
             service: str | None = None, rebuild: bool = False) -> dict:
    """生成修复计划（不写任何东西）。``externalize`` 为空时处理所有“推荐”的条目。"""
    root = root.resolve()
    home_text = str(home)
    raise_limit = raise_limit or {}
    config = load_config(root, user_config)
    blocking = [error for error in config.errors if not error.startswith("用户配置")]
    if blocking and (config.exists or os.path.lexists(config.path)):
        raise LocalOnlyError(_("项目配置有问题，先修正再处理（不会覆盖它）：") + _("；").join(blocking), "config-invalid")
    if config.home and config.home != home_text:
        raise LocalOnlyError(_("这台机器的 HOME（{0}）与项目约定（{1}）不一致：拒绝建立或修改链接").format(home_text, config.home), "home-mismatch")
    if os.name == "nt":
        raise LocalOnlyError(_("Windows 上只支持检查，不支持外置（建立软链接需要特权）"), "unsupported")
    if service is not None and service != NOT_SYNCED and service not in DECLARED_SERVICES:
        raise LocalOnlyError(_("同步服务只能是 {0}").format(_('、').join([*DECLARED_SERVICES, NOT_SYNCED])))
    report = check(root, home=home_text, environ=environ, mode="standard", user_config=user_config)
    # 由 HOME 与项目路径派生：预览与执行得到同一个值；两台设备同时初始化也写出相同内容，不会产生冲突副本。
    project_id = config.project_id or "p-" + _sha(f"{home_text}\0{root}")[:10]
    steps: list[dict] = []
    blocked: list[str] = []
    items = {item["path"]: dict(item) for item in config.items}
    wanted: list[tuple[str, str]] = []  # (相对路径, 种类)
    if externalize:
        for relative in externalize:
            relative = relative.rstrip("/")
            _validate_relative(root, relative)
            known = next((entry for entry in report["candidates"] if entry["path"] == relative), None)
            wanted.append((relative, known["kind"] if known else items.get(relative, {}).get("kind", "declared")))
    else:
        for entry in report["candidates"]:
            if entry["linkable"] and entry["kind"] in ("venv", "node_modules"):
                wanted.append((entry["path"], entry["kind"]))
            elif entry["kind"] != "cache" and not entry["linkable"]:
                blocked.append(_("{0}：{1}").format(entry['path'], entry['reason']))
        for item in report["items"]:
            if item["state"] in ("absent", "not-installed", "reverted"):
                wanted.append((item["path"], item["kind"]))
    for relative, kind in dict.fromkeys(wanted):
        path = _validate_relative(root, relative)
        target_text = expected_link(home_text, project_id, relative)
        target = Path(target_text)
        if kind == "node_modules":
            manager = next((m for m in report["package_managers"] if m["dir"] == (relative.rpartition("/")[0] or ".")), None)
            if manager and manager["verdict"] == "avoid":
                blocked.append(_("{0}：{1}；不外置（改用 yarn 经典版或 bun 后再来）").format(relative, _(PM_ADVICE[manager['pm']])))
                continue
        action, detail = None, ""
        if path.is_symlink():
            if os.readlink(path) != target_text:
                blocked.append(_("{0}：已经是指向别处的链接（{1}），不替你改").format(relative, os.readlink(path)))
                continue
            if not target.is_dir():
                action, detail = "make-target", _("建立本机目录 {0}（链接已存在）").format(target_text)
        elif path.is_dir():
            if target.exists() and not (target.is_dir() and not any(target.iterdir())):
                blocked.append(_("{0}：本机目标 {1} 已有内容，先确认哪一份要保留").format(relative, target_text))
                continue
            files, size = _tree_stats(path)
            action = "externalize"
            detail = _("把 {0}（{1} 个文件，{2}）移到 {3}，原位置建软链接").format(relative, files, _human(size), target_text)
            if kind == "declared":
                detail += _("。注意：这不是已知可重建的目录，其他设备将看不到它的内容")
        elif not os.path.lexists(path):
            action, detail = "link", _("建立本机目录 {0} 和软链接 {1}").format(target_text, relative)
        else:
            blocked.append(_("{0}：被一个普通文件占用").format(relative))
            continue
        items[relative] = {"path": relative, "kind": kind}
        if action:
            steps.append({"action": action, "path": relative, "kind": kind, "target": target_text, "detail": detail})
        if rebuild:
            command = rebuild_command(root, relative)
            if command is None:
                blocked.append(_("{0}：找不到锁文件，无法推断重建命令（不重建）").format(relative))
            elif shutil.which(command[0]) is None:
                blocked.append(_("{0}：本机没有 {1}，不重建").format(relative, command[0]))
            else:
                cwd = str((root / relative).parent)
                steps.append({"action": "rebuild", "path": relative, "kind": kind, "command": command, "cwd": cwd,
                              "detail": _("在 {0} 运行 {1}").format(cwd, ' '.join(command))})
    for bucket, limits in raise_limit.items():
        steps.append({"action": "raise-limit", "path": bucket, "limits": limits,
                      "detail": _("把 {0} 的阈值提到 {1}（写入项目配置）").format(bucket, _limit_text(limits.get('dir_mb'), limits.get('dir_files')))})
    new_items = sorted(items.values(), key=lambda entry: entry["path"])
    config_needed = new_items != sorted(config.items, key=lambda entry: entry["path"]) or service is not None or bool(raise_limit)
    changes: dict[str, Any] = {}
    if config_needed:
        changes["items"] = new_items
        if not config.complete:
            changes.update(project_id=project_id, home=home_text)
        if service is not None:
            changes["sync_service"] = service
        verb = _("更新") if config.exists else _("创建")
        steps.insert(0, {"action": "write-config", "path": CONFIG_PATH.as_posix(),
                         "detail": _("{0}项目配置 {1}（随项目同步；project_id={2}，home={3}）").format(verb, CONFIG_PATH.as_posix(), project_id, home_text)})
    return {"root": str(root), "project_id": project_id, "config": str(config.path), "steps": steps, "blocked": blocked,
            "config_changes": changes, "limit_overrides": dict(raise_limit), "empty": not steps}


def apply_fix(plan: dict, *, home: str | Path, environ: Mapping[str, str] | None = None, user_config: Path | None = None,
              recovery_root: Path | None = None) -> dict:
    """执行计划。任何一步失败都撤销本次已做的移动、链接和配置写入。"""
    root = Path(plan["root"])
    config = load_config(root, user_config)
    undo: list = []
    done: list[str] = []
    backup = None
    try:
        for step in plan["steps"]:
            action = step["action"]
            if action == "write-config":
                if config.path.is_file():
                    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
                    backup = (recovery_root or Path(home) / ".agent-pack" / "recovery") / f"{stamp}-agenthelm-local-only" / "local-only.json"
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(config.path, backup)
                    previous = config.path.read_bytes()
                    undo.append(lambda previous=previous: config.path.write_bytes(previous))
                else:
                    undo.append(lambda: config.path.unlink(missing_ok=True))
                changes = plan["config_changes"]
                document = _config_document(
                    config, **{key: changes[key] for key in ("project_id", "home", "sync_service", "items") if key in changes},
                    **({"limit_overrides": plan["limit_overrides"]} if plan["limit_overrides"] else {}))
                _write_json_atomic(config.path, document)
            elif action in ("externalize", "make-target", "link"):
                path, target_text = root / step["path"], step["target"]
                target = Path(target_text)
                _validate_relative(root, step["path"])
                if action == "externalize":
                    if target.exists() and any(target.iterdir()):
                        raise LocalOnlyError(_("本机目标目录 {0} 已有内容").format(target_text), "conflict")
                    if target.exists():
                        target.rmdir()
                    _move_tree(path, target, undo)
                else:
                    created = not target.exists()
                    target.mkdir(parents=True, exist_ok=True)
                    if created:
                        undo.append(lambda target=target: shutil.rmtree(target, ignore_errors=True))
                if not path.is_symlink():
                    os.symlink(target_text, path, target_is_directory=True)
                    undo.append(lambda path=path: path.unlink(missing_ok=True))
                if os.readlink(path) != target_text or not target.is_dir():
                    raise LocalOnlyError(_("{0} 建链接后核对失败").format(step['path']), "failed")
            elif action == "rebuild":
                completed = subprocess.run(step["command"], cwd=step["cwd"], capture_output=True, text=True,
                                           check=False, timeout=1800)
                if completed.returncode != 0:
                    done.append(_("重建失败（{0}，退出码 {1}）：").format(' '.join(step['command']), completed.returncode)
                                + (completed.stderr or "").strip()[-300:])
                    continue
            done.append(step["detail"])
    except Exception:
        for action in reversed(undo):
            try:
                action()
            except OSError:
                pass
        raise
    return {"applied": done, "config_backup": str(backup) if backup else ""}


# ---------------------------------------------------------------- 文本输出

LEVEL_MARK = {True: N_("需要处理"), False: N_("建议")}
PM_LABEL = {"ok": N_("可外置"), "avoid": N_("同步目录里应避免"), "unverified": N_("未测试"), "unknown": N_("未知")}


def render_text(report: dict) -> str:
    lines = []
    service = report["service"]
    lines.append(_("云同步：") + (_("{0}（{1}）").format(service['name'], _('已声明') if service['source'] == 'declared' else _('按路径识别'))
                               if service else _("未识别到同步服务——不需要外置，用什么都行")))
    config = report["config"]
    lines.append(_("配置：") + (config["path"] if config["exists"] else _("（还没有；fix --apply 会创建）")))
    if config["exists"]:
        lines.append(_("HOME：{0}（项目 {1}，本机 {2}）").format(_('一致') if report['home']['ok'] else _('不一致'), report['home']['expected'], report['home']['actual']))
    for item in report["items"]:
        lines.append(_("外置条目 {0}：{1} → {2}").format(item['path'], item['state'], item['expected']))
    for entry in report["package_managers"]:
        if report["service"] and entry["pm"]:
            lines.append(_("包管理器 {0}：{1}（{2}）").format(entry['dir'], entry['pm'], _(PM_LABEL[entry['verdict']])))
    for size in report["sizes"]:
        if size["state"]:
            lines.append(_("体积 {0}：{1}、{2} 个文件（{3:.0%} 的阈值）").format(size['path'], _human(size['bytes']), size['files'], size['ratio']))
    for finding in report["findings"]:
        lines.append(_("[{0}] {1}\n    原因：{2}\n    影响：{3}").format(_(LEVEL_MARK[finding['attention']]), finding['title'], finding['reason'], finding['impact']))
        for option in finding["options"]:
            lines.append(f"    · {option['label']}")
        lines.append(f"    ID {finding['id']}")
    lines += [_("说明：{0}").format(note) for note in report["notes"]]
    if not report["findings"]:
        lines.append(_("没有发现需要处理的项。"))
    return "\n".join(lines) + "\n"


def render_plan(plan: dict, *, applied: dict | None = None) -> str:
    lines = []
    if plan["empty"] and not plan["blocked"]:
        return _("没有需要处理的项。\n")
    for number, step in enumerate(plan["steps"], 1):
        lines.append(f" {number}. {step['detail']}")
    for message in plan["blocked"]:
        lines.append(_(" 不处理：{0}").format(message))
    if applied is None:
        lines.append(_("以上为预览；确认后追加 --apply 执行。移动失败会整体撤销。"))
    else:
        if applied.get("config_backup"):
            lines.append(_("旧配置已备份到 {0}").format(applied['config_backup']))
        lines.append(_("已执行。重新运行 local-only check 核对；在其他设备上同步完成后同样运行 fix --apply 建立它们各自的本机目录。"))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 命令行

def _parse_limits(values: list[str]) -> dict[str, dict]:
    """``目录=MB`` 或 ``目录=MB/文件数``。"""
    result: dict[str, dict] = {}
    for value in values:
        bucket, separator, amount = value.rpartition("=")
        megabytes, _unused, files = amount.partition("/")
        try:
            limits = {"dir_mb": float(megabytes)}
            if files:
                limits["dir_files"] = int(files)
        except ValueError:
            limits = {"dir_mb": 0}
        if not separator or not bucket or limits["dir_mb"] <= 0 or limits.get("dir_files", 1) <= 0:
            raise LocalOnlyError(_("--raise-limit 的格式是 <目录>=<MB> 或 <目录>=<MB>/<文件数>：{0}").format(value))
        result[bucket.rstrip("/") or "."] = limits
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agenthelm local-only", description=_("只属于这台机器的目录不进云同步盘"))
    sub = parser.add_subparsers(dest="action", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--project", type=Path, help=_("项目目录（默认当前目录）"))
        command.add_argument("--home", type=Path, help=_("把这个目录当作 HOME（测试或隔离用）"))
        command.add_argument("--output", choices=("text", "json"), default="text")

    check_parser = sub.add_parser("check", help=_("只读检查（云同步、外置链接、HOME、体积、包管理器）"))
    common(check_parser)
    check_parser.add_argument("--mode", choices=("quick", "standard", "full"), default="full",
                              help=_("quick 只看浅层且不统计体积；full 完整扫描（默认）"))
    check_parser.add_argument("--strict", action="store_true", help=_("有需要处理的项时退出码为 1"))
    check_parser.add_argument("--reprompt", action="store_true", help=_("忽略已记录的决定"))
    check_parser.add_argument("--fingerprint", action="store_true", help=_("输出本机环境指纹与明细（用于跨设备对比）"))
    check_parser.add_argument("--explain", metavar=_("对方的 JSON"), help=_("与 --fingerprint 合用：和另一台机器的输出比较"))

    fix = sub.add_parser("fix", help=_("外置、补建目标目录、调整阈值（默认预览，--apply 才写）"))
    common(fix)
    fix.add_argument("--externalize", action="append", default=[], metavar=_("路径"), help=_("外置这个项目内路径（可重复）"))
    fix.add_argument("--raise-limit", action="append", default=[], metavar=_("目录=MB"), help=_("提高某个目录的体积阈值（可重复）；可写 目录=MB/文件数"))
    fix.add_argument("--service", help=_("声明同步服务：{0}").format(_('、').join([*DECLARED_SERVICES, NOT_SYNCED])))
    fix.add_argument("--rebuild", action="store_true", help=_("外置后在本机运行重建命令（uv sync / yarn install 等）"))
    fix.add_argument("--apply", action="store_true")

    config = sub.add_parser("config", help=_("显示生效的配置与文件位置"))
    common(config)
    return parser


def main(argv: list[str] | None = None, host: Any = None) -> int:
    args = build_parser().parse_args(argv)
    root = (args.project or Path.cwd()).resolve()
    if host is not None:
        env = host.current_env(args.home)
        home, environ, user_config = env.home, env.variables, env.config_file
    else:
        home = args.home or Path(os.path.expanduser("~"))
        environ, user_config = dict(os.environ), home / ".config" / "agenthelm" / "config.json"
    if host is not None and hasattr(host, "profile_root"):
        root = host.profile_root(root) or root
    emit = (lambda payload, text: print(json.dumps(payload, ensure_ascii=False, indent=2))
            if args.output == "json" else sys.stdout.write(text))
    try:
        if args.action == "config":
            config = load_config(root, user_config)
            payload = {"ok": not config.errors, "path": str(config.path), "exists": config.exists,
                       "project_id": config.project_id, "home": config.home, "sync_service": config.service,
                       "items": config.items, "limits": config.limits, "overrides": config.overrides,
                       "keep_synced": config.keep_synced, "ignore": config.ignore, "extra_local_only": config.extra,
                       "errors": config.errors, "user_config": str(user_config), "store": store_root(home)}
            text = "\n".join([_("项目配置：{0}（{1}）").format(config.path, _('存在') if config.exists else _('不存在')), _("用户配置：{0} 的 local_only 段").format(user_config),
                              _("外置目录根：{0}").format(store_root(home)), _("阈值：{0}").format(json.dumps(config.limits, ensure_ascii=False)),
                              _("默认保持同步：{0}").format(_('、').join(config.keep_synced)), *config.errors]) + "\n"
            emit(payload, text)
            return 0 if not config.errors else 4
        if args.action == "check":
            report = host.local_only_report(host.current_env(args.home), root, mode=args.mode, reprompt=args.reprompt) \
                if host is not None and hasattr(host, "local_only_report") \
                else check(root, home=home, environ=environ, mode=args.mode, user_config=user_config)
            if args.fingerprint or args.explain:
                payload = {"ok": True, "local_only_fingerprint": report["fingerprint"]}
                differences: list[str] = []
                lines = [_("本机环境指纹 {0}").format(report['fingerprint']['fingerprint'] or _('（没有 local-only 配置）'))]
                if args.explain:
                    try:
                        other = _read_json(Path(args.explain).expanduser())
                        theirs = other["local_only_fingerprint"]["detail"]
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        print(_("读不了对方的输出（需要对方运行 local-only check --fingerprint --output json）：{0}").format(exc), file=sys.stderr)
                        return 2
                    differences = explain(report["fingerprint"]["detail"], theirs)
                    payload["same"], payload["differences"] = not differences, differences
                    lines.append(_("两台机器的本机环境一致。") if not differences else _("有 {0} 处不同：").format(len(differences)))
                    lines += [f"  {line}" for line in differences]
                else:
                    lines += [f"  {key} = {value}" for key, value in report["fingerprint"]["detail"].items()]
                emit(payload, "\n".join(lines) + "\n")
                return 1 if differences else 0
            emit(report, render_text(report))
            return 1 if args.strict and report["summary"]["attention"] else 0
        plan = plan_fix(root, home=home, environ=environ, user_config=user_config, externalize=args.externalize,
                        raise_limit=_parse_limits(args.raise_limit), service=args.service, rebuild=args.rebuild)
        applied = apply_fix(plan, home=home, environ=environ, user_config=user_config) if args.apply and not plan["empty"] else None
        emit({"ok": True, "applied": applied is not None, "plan": plan, "result": applied}, render_plan(plan, applied=applied))
        return 0
    except LocalOnlyError as exc:
        if args.output == "json":
            print(json.dumps({"ok": False, "error": str(exc), "code": exc.code}, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        if args.output == "json":
            print(json.dumps({"ok": False, "error": str(exc), "code": "os-error"}, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
