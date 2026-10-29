#!/usr/bin/env python3
"""agenthelm：在 Claude Code、Codex、Cursor 间安装、检查与更新 agenthelm 插件。

``project`` 打开不需要 AI Agent 的项目自助入口（由 project-orchestrator 实现）；``runtime``
管理用户级运行时；``nudge`` 是各客户端 hook 调用的观点候选采集与未决事项提示（见 references/nudge.md）。``check`` 只读：比较各客户端已安装的插件版本、项目里由模板渲染的文件，
以及 Opinion 来源与云端 ``latest.json``。``install``、``update``、``migrate``
默认只预览，``--apply`` 才执行。通过 ``npx @sakanano-moe/agenthelm <命令>`` 或
``python3 <agent-pack>/runtime/agenthelm.py <命令>`` 运行。``--hook`` 供会话开始时静默调用：
每天至多联网一次、离线不报错、只在有更新时输出一行，可用环境变量
``AGENTHELM_NO_UPDATE_CHECK=1`` 或用户配置 ``{"update_check": false}`` 关闭。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MARKETPLACE = "agenthelm"
CLI = "npx @sakanano-moe/agenthelm"
DEFAULT_LATEST_URLS = (
    "https://agenthelm.sakanano.moe/latest.json",
    "https://raw.githubusercontent.com/lc4t/agenthelm/main/latest.json",
)
ENV_LATEST = "AGENTHELM_LATEST_URL"
ENV_DISABLE = "AGENTHELM_NO_UPDATE_CHECK"
CACHE_TTL_SECONDS = 24 * 3600
# Interactive commands wait longer: some networks reach the site slowly.
FETCH_TIMEOUT_SECONDS = 10
HOOK_FETCH_TIMEOUT_SECONDS = 2
FAILURE_BACKOFF_SECONDS = 6 * 3600
CONTENT_LOCK = Path(".agents/moe.sakanano.agent-pack/content.lock.json")
OPINION_LOCK = Path("opinion.lock.json")
RUNTIME_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = RUNTIME_DIR.parent


class CheckError(Exception):
    pass


def version_key(value: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", value or "")
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(candidate: str, current: str) -> bool:
    new, old = version_key(candidate), version_key(current)
    return new is not None and old is not None and new > old


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- environment

@dataclass
class Env:
    home: Path
    variables: dict[str, str] = field(default_factory=dict)

    def child_environment(self) -> dict[str, str]:
        """Environment for client CLIs: they must see the same home this run inspects."""
        child = dict(self.variables)
        child["HOME"] = str(self.home)
        return child

    def get(self, name: str) -> str | None:
        value = self.variables.get(name)
        return value if value else None

    @property
    def cache_dir(self) -> Path:
        base = self.get("XDG_CACHE_HOME")
        return (Path(base) if base else self.home / ".cache") / "agenthelm"

    @property
    def state_dir(self) -> Path:
        base = self.get("XDG_STATE_HOME")
        return Path(base) if base else self.home / ".local" / "state"

    @property
    def config_file(self) -> Path:
        base = self.get("XDG_CONFIG_HOME")
        return (Path(base) if base else self.home / ".config") / "agenthelm" / "config.json"

    @property
    def claude_dir(self) -> Path:
        configured = self.get("CLAUDE_CONFIG_DIR")
        return Path(configured) if configured else self.home / ".claude"

    @property
    def codex_dir(self) -> Path:
        configured = self.get("CODEX_HOME")
        return Path(configured) if configured else self.home / ".codex"


def current_env(home: Path | None = None) -> Env:
    variables = dict(os.environ)
    if home is not None:
        # An explicit home must not inherit locations that point back at the real one.
        for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
            variables.pop(name, None)
    return Env(home=(home or Path.home()), variables=variables)


def update_check_enabled(env: Env) -> bool:
    if env.get(ENV_DISABLE) not in (None, "0", "false"):
        return False
    try:
        return read_json(env.config_file).get("update_check", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


# ---------------------------------------------------------------- cloud source

def _fetch_json(url: str, timeout: float = FETCH_TIMEOUT_SECONDS) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "agenthelm-check"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _fetch(url: str, timeout: float = FETCH_TIMEOUT_SECONDS) -> dict:
    data = _fetch_json(url, timeout)
    if not isinstance(data, dict) or data.get("marketplace") != MARKETPLACE:
        raise CheckError(f"{url} is not an agenthelm latest.json")
    return data


def load_latest(env: Env, source: str | None = None, *, offline: bool = False,
                max_age: int = CACHE_TTL_SECONDS, timeout: float = FETCH_TIMEOUT_SECONDS,
                backoff: bool = False) -> tuple[dict, dict]:
    """Return (latest, origin). A local path is read directly; URLs go through a daily cache."""
    source = source or env.get(ENV_LATEST)
    if source and not re.match(r"https?://", source):
        return read_json(Path(source).expanduser()), {"source": source, "cached": False}
    urls = (source,) if source else DEFAULT_LATEST_URLS
    cache_file = env.cache_dir / "latest.json"
    cached = None
    try:
        cached = read_json(cache_file)
    except (OSError, ValueError):
        pass
    if cached and (offline or time.time() - cached.get("fetched_at", 0) < max_age):
        return cached["latest"], {"source": cached.get("url"), "cached": True, "fetched_at": cached.get("fetched_at")}
    if offline:
        raise CheckError("离线且没有缓存的 latest.json")
    failure_file = env.cache_dir / "last_failure.json"
    if backoff:
        try:
            if time.time() - read_json(failure_file).get("at", 0) < FAILURE_BACKOFF_SECONDS:
                if cached:
                    return cached["latest"], {"source": cached.get("url"), "cached": True, "stale": True}
                raise CheckError("最近联网失败，稍后再试")
        except (OSError, ValueError, AttributeError):
            pass
    errors = []
    for url in urls:
        try:
            latest = _fetch(url, timeout)
        except (OSError, ValueError, CheckError, urllib.error.URLError) as exc:
            errors.append(f"{url}: {exc}")
            continue
        try:
            write_json_atomic(cache_file, {"url": url, "fetched_at": time.time(), "latest": latest})
        except OSError:
            pass
        return latest, {"source": url, "cached": False}
    try:
        write_json_atomic(failure_file, {"at": time.time()})
    except OSError:
        pass
    if cached:
        return cached["latest"], {"source": cached.get("url"), "cached": True, "stale": True,
                                  "fetched_at": cached.get("fetched_at")}
    raise CheckError("无法获取 latest.json：" + "; ".join(errors))


# ---------------------------------------------------------------- installed plugins

def _manifest_version(path: Path) -> str | None:
    try:
        return str(read_json(path / "plugin.json").get("version"))
    except (OSError, ValueError, AttributeError):
        return None


def installed_plugins(env: Env, names: set[str]) -> list[dict]:
    found: list[dict] = []
    try:
        record = read_json(env.claude_dir / "plugins" / "installed_plugins.json").get("plugins", {})
    except (OSError, ValueError, AttributeError):
        record = {}
    for key, entries in sorted(record.items()):
        plugin, _, market = key.partition("@")
        if market != MARKETPLACE or plugin not in names:
            continue
        for entry in entries:
            path = Path(str(entry.get("installPath", "")))
            # A git-SHA version means the entry had no version; fall back to the manifest.
            version = entry.get("version") if version_key(str(entry.get("version"))) else _manifest_version(path)
            found.append({"client": "claude", "plugin": plugin, "version": version, "path": str(path),
                          "scope": entry.get("scope")})
    codex_cache = env.codex_dir / "plugins" / "cache" / MARKETPLACE
    for plugin in sorted(names):
        directories = [path for path in (codex_cache / plugin).glob("*") if (path / "plugin.json").is_file()]
        if directories:
            newest = max(directories, key=lambda path: version_key(path.name) or (0,))
            found.append({"client": "codex", "plugin": plugin, "version": _manifest_version(newest),
                          "path": str(newest)})
    for client, base in (("cursor", env.home / ".cursor" / "plugins" / "local"),
                         ("legacy", env.home / ".agents" / "plugins" / "plugins"),
                         ("legacy", env.home / ".claude" / "agent-pack" / "plugins")):
        for plugin in sorted(names):
            path = base / plugin
            if (path / "plugin.json").is_file():
                found.append({"client": client, "plugin": plugin, "version": _manifest_version(path),
                              "path": str(path)})
    return found


def migrations_between(latest: dict, plugin: str, current: str, target: str) -> list[dict]:
    """The steps from ``current`` to ``target``; when two entries end at the same version (a span kept for the
    release gate and the single steps inside it) the narrower one, with the newer ``from``, is shown."""
    steps: dict[str, dict] = {}
    for entry in latest.get("migrations", []):
        if entry.get("plugin") != plugin:
            continue
        to = str(entry.get("to"))
        if is_newer(to, current) and not is_newer(to, target):
            kept = steps.get(to)
            if kept is None or is_newer(str(entry.get("from")), str(kept.get("from"))):
                steps[to] = entry
    return sorted(steps.values(), key=lambda entry: version_key(str(entry.get("to"))) or (0,))


def plugin_findings(latest: dict, installed: list[dict]) -> list[dict]:
    findings = []
    for item in installed:
        target = latest.get("plugins", {}).get(item["plugin"], {}).get("version")
        current = item.get("version") or ""
        if item["client"] == "legacy":
            status = "legacy"  # a pre-marketplace copy: it gets migrated, not updated
        elif target is None:
            status = "unknown"
        elif is_newer(target, current):
            status = "behind"
        elif is_newer(current, target):
            status = "ahead"
        elif version_key(current) is None:
            status = "unknown"
        else:
            status = "current"
        steps = migrations_between(latest, item["plugin"], current, target) if status == "behind" else []
        kinds = sorted({step.get("change_kind") for step in steps if step.get("change_kind")})
        findings.append({
            **item,
            "latest": target,
            "status": status,
            "change_kinds": kinds,
            "breaking": any(step.get("breaking") for step in steps),
            "steps": [{"to": step.get("to"), "change_kind": step.get("change_kind"),
                       "breaking": step.get("breaking"), "summary": step.get("summary"),
                       "actions": step.get("actions", [])} for step in steps],
        })
    return findings


# ---------------------------------------------------------------- project content

def content_findings(project: Path, latest: dict) -> list[dict]:
    lock_path = project / CONTENT_LOCK
    if not lock_path.is_file():
        return []
    try:
        lock = read_json(lock_path)
    except (OSError, ValueError) as exc:
        return [{"pack": None, "status": "error", "error": f"{CONTENT_LOCK} 无法解析：{exc}"}]
    findings = []
    for pack_id, entry in sorted(lock.get("packs", {}).items()):
        plugin = entry.get("plugin") or pack_id.split("/", 1)[0]
        target = latest.get("plugins", {}).get(plugin, {}).get("content", {}).get(pack_id, {}).get("version")
        files = []
        for relative, digest in sorted(entry.get("files", {}).items()):
            path = project / relative
            if not path.is_file():
                state = "missing"
            elif sha256_file(path) == digest:
                state = "clean"
            else:
                state = "modified"
            files.append({"path": relative, "state": state})
        changed = [item["path"] for item in files if item["state"] != "clean"]
        if target is None:
            status = "unknown"
        elif is_newer(target, entry.get("version", "")):
            status = "conflict" if changed else "update-available"
        else:
            status = "current"
        findings.append({"pack": pack_id, "plugin": plugin, "version": entry.get("version"),
                         "latest": target, "status": status, "locally_changed": changed, "files": files})
    return findings


def locate_companion(name: str, installed: list[dict]) -> Path | None:
    """The newest copy of a companion plugin: next to this one or in any client install."""
    candidates = [PLUGIN_ROOT.parent / name, *(Path(item["path"]) for item in installed if item["plugin"] == name)]
    candidates = [path for path in candidates if (path / "plugin.json").is_file()]
    return max(candidates, key=lambda path: version_key(_manifest_version(path) or "") or (0,)) if candidates else None


def opinion_findings(project: Path, installed: list[dict]) -> list[dict]:
    """Delegate to opinion-manager: newer catalog versions of the templates and rules the project locked."""
    lock_path = project / OPINION_LOCK
    if not lock_path.is_file():
        return []
    manager = locate_companion("opinion-manager", installed)
    if manager is None:
        return [{"status": "unknown", "error": "未找到已安装的 opinion-manager"}]
    try:
        profile = read_json(lock_path)["profile"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [{"status": "error", "error": f"{OPINION_LOCK} 无法解析：{exc}"}]
    with tempfile.TemporaryDirectory() as temporary:
        # macOS temporary paths live under /var, a symlink opinion-manager refuses.
        profile_file = Path(temporary).resolve() / "profile.json"
        profile_file.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(manager / "runtime" / "opinion_manager.py"), "updates",
             "--profile", str(profile_file), "--catalog", str(manager / "catalog")],
            capture_output=True, text=True, check=False, timeout=30,
        )
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        return [{"status": "error", "error": (completed.stderr or completed.stdout).strip()[:300]}]
    if not result.get("ok", False):
        return [{"status": "error", "error": result.get("error", "opinion-manager updates 失败")}]
    return [{"status": "update-available" if result["updates"] else "current", "updates": result["updates"]}]


# ---------------------------------------------------------------- project scaffold

PROFILE_FILE = Path(".agents/moe.sakanano.agent-pack/project.json")
_SCAFFOLD_CACHE: dict[str, Any] = {}


def profile_root(project: Path | None) -> Path | None:
    """The project root that holds the Project Profile, searching upwards from ``project``."""
    if project is None:
        return None
    base = project.expanduser().resolve()
    return next((path for path in [base, *base.parents] if (path / PROFILE_FILE).is_file()), None)


def scaffold_module(env: Env) -> Any | None:
    """agents-init's scaffold module (the sibling copy first, then the newest installed one); None when too old."""
    installed = installed_plugins(env, {"agents-init"})
    copies = [PLUGIN_ROOT.parent / "agents-init", *(Path(item["path"]) for item in installed)]
    usable = [path for path in copies if (path / "skills/agents-init/scripts/scaffold.py").is_file()]
    if not usable:
        return None
    sibling = PLUGIN_ROOT.parent / "agents-init"
    root = sibling if sibling in usable else max(usable, key=lambda p: version_key(_manifest_version(p) or "") or (0,))
    scripts = root / "skills/agents-init/scripts"
    key = str(scripts)
    if key not in _SCAFFOLD_CACHE:
        import importlib.util
        spec = importlib.util.spec_from_file_location(f"agenthelm_scaffold_{len(_SCAFFOLD_CACHE)}", scripts / "scaffold.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        _SCAFFOLD_CACHE[key] = module
    return _SCAFFOLD_CACHE[key]


def scaffold_report(env: Env, project: Path | None, *, reprompt: bool = False) -> dict | None:
    """Read-only comparison of the project with the expected scaffold; None outside a project."""
    root = profile_root(project)
    if root is None:
        return None
    base = {"available": False, "root": str(root), "findings": [], "skipped": [], "error": ""}
    module = scaffold_module(env)
    if module is None:
        return {**base, "error": "已安装的 agents-init 没有脚手架检查（更新 agents-init 后再检查）"}
    try:
        return {**module.check(root, state_dir=env.state_dir, reprompt=reprompt), "available": True,
                "root": str(root), "error": ""}
    except Exception as exc:  # noqa: BLE001 - a broken Profile must not break plugin and content checks
        return {**base, "error": str(exc)}


def _local_only() -> Any:
    """local_only 模块（与本文件同目录，只用标准库）。"""
    if str(RUNTIME_DIR) not in sys.path:
        sys.path.insert(0, str(RUNTIME_DIR))
    import local_only  # noqa: PLC0415 - shipped next to this file

    return local_only


SYNC_SERVICES = tuple((name, pattern) for _identifier, name, pattern in _local_only().SYNC_SERVICES)
REBUILDABLE_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".cache", "dist", "build", "target",
                    ".tox", ".next", ".gradle"}


def sync_folder(root: Path) -> dict | None:
    """The sync service whose folder holds the project (by path), and the rebuildable directories inside it."""
    resolved = root.resolve()
    service = next((name for name, pattern in SYNC_SERVICES if any(pattern.match(part) for part in resolved.parts)), None)
    if service is None:
        return None
    found: list[str] = []
    for current, directories, _files in os.walk(resolved):
        depth = len(Path(current).relative_to(resolved).parts)
        descend = []
        for name in directories:
            if name in REBUILDABLE_DIRS:
                found.append((Path(current) / name).relative_to(resolved).as_posix())
            elif name != ".git" and depth < 1:  # 只看根目录和它的下一层
                descend.append(name)
        directories[:] = descend
    return {"service": service, "path": str(resolved), "rebuildable": sorted(found)}


def machine_findings(env: Env, project: Path | None, *, reprompt: bool = False, quick: bool = False) -> dict:
    """Findings about this machine only (no user-level runtime ...); their decisions live in the user state directory."""
    root = profile_root(project)
    result: dict = {"findings": [], "skipped": []}
    if root is None:
        return result
    import agenthelm_runtime  # noqa: PLC0415 - shipped next to this file

    findings = []
    data = agenthelm_runtime.report(env, None)
    if data["current"] is None:
        text = agenthelm_runtime.missing_text(data, agenthelm_runtime.command_status())
        findings.append({"id": "local:runtime-missing", "kind": "runtime-missing", "level": "suggest", "scope": "machine",
                         "title": "这台机器还没有安装用户级运行时", "reason": text["reason"], "impact": text["impact"],
                         "fingerprint": "runtime-missing", "fix": "install-runtime", "next": text["label"]})
    where = sync_folder(root)
    if where is not None:
        extra = f"；其中有可重建目录：{'、'.join(where['rebuildable'][:6])}" if where["rebuildable"] else ""
        findings.append({"id": "local:sync-folder", "kind": "sync-folder", "level": "suggest", "scope": "machine",
                         "title": f"项目位于同步盘（{where['service']}）",
                         "reason": f"{where['path']}{extra}",
                         "impact": "多台机器共用同一份项目时：项目文件随同步盘走，但运行时、缓存与 wf 的台账是每台机器各一份；"
                                   "可重建目录（依赖、构建产物）同步很慢，也容易产生冲突副本",
                         "fingerprint": sha256_text(where["service"]), "fix": "sync-folder",
                         "next": "运行 agenthelm fix sync-folder 查看指引（只给方案，不改文件、不删内容）；不需要就 fix --skip local:sync-folder",
                         "sync": where})
    # local-only：同步盘里的本机专属目录、外置链接、HOME、体积……没有同步服务也没有配置时不扫描。
    try:
        engine = _local_only().check(root, home=env.home, environ=env.variables, mode="quick" if quick else "standard",
                                     user_config=env.config_file)
        findings.extend(engine["findings"])
    except Exception as exc:  # noqa: BLE001 - 本机目录检查出错不能拖垮插件与脚手架检查
        findings.append({"id": "local:local-only-error", "kind": "error", "level": "suggest", "scope": "machine",
                         "title": "无法检查本机专属目录", "reason": str(exc), "impact": "只影响同步盘相关提示",
                         "fingerprint": sha256_text(str(exc)), "fix": None, "attention": False, "options": [],
                         "next": "运行 agenthelm local-only check 查看详情"})
    module = scaffold_module(env)
    if module is None or not findings:
        result["findings"] = findings
        return result
    decisions = module.load_decisions(root, env.state_dir)
    result["findings"], result["skipped"] = module.partition(findings, decisions, reprompt=reprompt)
    return result


def local_only_report(env: Env, project: Path | None, *, mode: str = "full", reprompt: bool = False) -> dict:
    """local-only 的完整检查结果（供 ``agenthelm local-only check`` 与 MCP 工具），已套用这台机器上记录的决定。"""
    module = _local_only()
    base = (project or Path.cwd()).resolve()
    root = profile_root(base) or base
    report = module.check(root, home=env.home, environ=env.variables, mode=mode, user_config=env.config_file)
    report["skipped"] = []
    scaffold = scaffold_module(env)
    if scaffold is not None and report["findings"]:
        decisions = scaffold.load_decisions(root, env.state_dir)
        report["findings"], skipped = scaffold.partition(report["findings"], decisions, reprompt=reprompt)
        report["skipped"] = [{"id": item["id"], "decision": item["decision"]} for item in skipped]
        report["summary"] = {"findings": len(report["findings"]),
                             "attention": sum(1 for item in report["findings"] if item.get("attention"))}
    return report


def local_only_plan(env: Env, project: Path | None, **options: Any) -> dict:
    """``agenthelm local-only fix`` 的预览（不写任何东西）；原因不能处理时抛出 LocalOnlyError。"""
    base = (project or Path.cwd()).resolve()
    return _local_only().plan_fix(profile_root(base) or base, home=env.home, environ=env.variables,
                                  user_config=env.config_file, **options)


# ---------------------------------------------------------------- report

def scaffold_fingerprint(env: Env, scaffold: dict | None) -> dict | None:
    module = scaffold_module(env)
    if scaffold is None or module is None or not scaffold.get("available"):
        return None
    return module.fingerprint(scaffold)


def run_check(env: Env, project: Path | None, *, latest_source: str | None = None, offline: bool = False,
              max_age: int = CACHE_TTL_SECONDS, hook_mode: bool = False, reprompt: bool = False) -> dict:
    latest, origin = load_latest(
        env, latest_source, offline=offline, max_age=max_age,
        timeout=HOOK_FETCH_TIMEOUT_SECONDS if hook_mode else FETCH_TIMEOUT_SECONDS, backoff=hook_mode,
    )
    names = set(latest.get("plugins", {}))
    installed = installed_plugins(env, names)
    plugins = plugin_findings(latest, installed)
    content = content_findings(project, latest) if project else []
    opinion = opinion_findings(project, installed) if project else []
    scaffold = scaffold_report(env, project, reprompt=reprompt)
    machine = machine_findings(env, project, reprompt=reprompt, quick=hook_mode)
    local_attention = sum(1 for item in machine["findings"] if item.get("attention"))
    scaffold_active = len(scaffold["findings"]) if scaffold else 0
    behind = [item for item in plugins if item["status"] == "behind"]
    legacy = [item for item in plugins if item["client"] == "legacy"]
    content_updates = [item for item in content if item["status"] in {"update-available", "conflict"}]
    opinion_updates = [item for item in opinion if item["status"] == "update-available"]
    return {
        "ok": True,
        "latest": {"source": origin, "released_at": latest.get("released_at")},
        "_latest": latest,
        "plugins": plugins,
        "content": content,
        "opinion": opinion,
        "scaffold": scaffold,
        "machine": machine,
        "summary": {
            "scaffold_findings": scaffold_active,
            "scaffold_skipped": len(scaffold["skipped"]) if scaffold else 0,
            "local_only_attention": local_attention,
            "plugins_behind": len(behind),
            "breaking": any(item["breaking"] for item in behind),
            "content_updates": len(content_updates),
            "content_conflicts": sum(1 for item in content if item["status"] == "conflict"),
            "opinion_updates": len(opinion_updates),
            "legacy_installs": len(legacy),
            "up_to_date": not (behind or content_updates or opinion_updates or legacy or scaffold_active
                               or local_attention),
        },
    }


UPDATE_HINTS = {
    "claude": "claude plugin marketplace update agenthelm && claude plugin update {plugin}@agenthelm",
    "codex": "codex plugin marketplace upgrade agenthelm",
    "cursor": f"{CLI} update",
    "legacy": f"{CLI} migrate --install",
}


def one_line(report: dict) -> str | None:
    summary = report["summary"]
    if summary["up_to_date"]:
        return None
    parts = []
    if summary["plugins_behind"]:
        parts.append(f"{summary['plugins_behind']} 个插件可更新" + ("（含不兼容变更）" if summary["breaking"] else ""))
    if summary["content_updates"]:
        parts.append(f"{summary['content_updates']} 个模板/样例包可更新"
                     + (f"（{summary['content_conflicts']} 个本地有改动，更新时逐文件确认）" if summary["content_conflicts"] else ""))
    if summary["opinion_updates"]:
        parts.append("Opinion 有新版本模板或规则")
    if summary["legacy_installs"]:
        parts.append(f"{summary['legacy_installs']} 个旧方式安装待迁移")
    if summary["scaffold_findings"]:
        parts.append(f"项目脚手架有 {summary['scaffold_findings']} 项差异")
    if summary.get("local_only_attention"):
        parts.append(f"同步目录里有 {summary['local_only_attention']} 项本机专属目录需要处理")
    return "agenthelm：" + "，".join(parts) + f"。运行 `{CLI} check` 查看详情；更新前会先预览，由你决定。"


def render_scaffold(report: dict, show_skipped: bool) -> list[str]:
    scaffold = report.get("scaffold")
    lines = []
    if scaffold is None:
        return lines
    if scaffold.get("error"):
        lines.append(f"  [脚手架] 无法检查：{scaffold['error']}")
        return lines
    for item in scaffold["findings"]:
        mark = "（稍后）" if item.get("deferred") else "（期望内容已变化，重新提示）" if item.get("changed_since_decision") else ""
        fix = f"  处理：fix {item['fix']}" if item.get("fix") else "  需要你自己处理"
        lines.append(f"  [脚手架] {item['title']}{mark}：{item['reason']}\n      ID {item['id']}{fix}")
    for message in scaffold["decisions"]["errors"]:
        lines.append(f"  [脚手架] 决定文件有问题：{message}")
    if scaffold["decisions"]["conflict_copies"]:
        lines.append("  [脚手架] 同步服务留下了决定文件的冲突副本："
                     + "、".join(scaffold["decisions"]["conflict_copies"]) + "（合并到 check-decisions.json 后删除）")
    if not scaffold["findings"] and not scaffold["decisions"]["errors"]:
        lines.append("  [脚手架] 与期望一致" + (f"（{len(scaffold['skipped'])} 项已跳过）" if scaffold["skipped"] else ""))
    skipped = [*scaffold["skipped"], *report.get("machine", {}).get("skipped", [])]
    if show_skipped:
        for item in skipped:
            decision = item["decision"]
            lines.append(f"  [已跳过·{decision['decision']}] {item['id']}：{decision.get('reason') or '（未写理由）'}"
                         f"  {decision.get('decided_at', '')}")
        if not skipped:
            lines.append("  没有被跳过的项。")
    elif skipped:
        lines.append(f"  （另有 {len(skipped)} 项已被你跳过：check --show-skipped 查看）")
    return lines


def render_machine(report: dict) -> list[str]:
    lines = []
    for item in report.get("machine", {}).get("findings", []):
        lines.append(f"  [本机] {item['title']}：{item['reason']}\n      影响：{item['impact']}\n      处理：{item['next']}"
                     f"\n      ID {item['id']}（只记在这台机器上；fix --skip {item['id']} --reason …）")
    return lines


def render_text(report: dict, *, show_skipped: bool = False) -> str:
    lines = []
    origin = report["latest"]["source"]
    lines.append(f"云端版本源：{origin.get('source')}" + ("（缓存）" if origin.get("cached") else ""))
    if not report["plugins"]:
        lines.append("未发现已安装的 agenthelm 插件。")
    for item in report["plugins"]:
        label = {"behind": "可更新", "current": "最新", "ahead": "高于云端", "unknown": "版本未知",
                 "legacy": "旧方式安装，待迁移"}[item["status"]]
        if item["status"] == "legacy":
            lines.append(f"  [旧方式] {item['plugin']} {item.get('version')}  {label}")
            continue
        line = f"  [{item['client']}] {item['plugin']} {item.get('version')} → {item.get('latest')}  {label}"
        if item["status"] == "behind":
            kinds = {"code": "代码", "content": "仅模板/样例", "both": "代码与模板"}
            if item["change_kinds"]:
                line += "（" + "、".join(kinds[kind] for kind in item["change_kinds"]) + "）"
            if item["breaking"]:
                line += "（含不兼容变更）"
            for step in item["steps"]:
                if step.get("breaking"):
                    line += f"\n      {step['to']} 不兼容：{step.get('summary')}"
                    line += "".join(f"\n        - {action}" for action in step.get("actions") or [])
            hint = UPDATE_HINTS.get(item["client"])
            if hint:
                line += f"\n      更新：{hint.format(plugin=item['plugin'])}"
        lines.append(line)
    for item in report["content"]:
        if item.get("pack") is None:
            lines.append(f"  内容锁：{item.get('error')}")
            continue
        label = {"current": "最新", "update-available": "可更新（本地未改动）",
                 "conflict": "可更新（本地有改动，更新时逐文件确认）", "unknown": "云端未列出"}[item["status"]]
        line = f"  [内容] {item['pack']} {item['version']} → {item.get('latest')}  {label}"
        if item["locally_changed"]:
            line += "\n      本地已改动：" + "、".join(item["locally_changed"])
        lines.append(line)
    for item in report["opinion"]:
        if item["status"] == "update-available":
            refs = "；".join(f"{update['current']} → {update['available'][-1]}" for update in item["updates"])
            lines.append(f"  [Opinion] 有新版本：{refs}（用 opinion-manager 预览后确认）")
        elif item["status"] in {"error", "unknown"}:
            lines.append(f"  [Opinion] {item.get('error')}")
    lines += render_scaffold(report, show_skipped)
    lines += render_machine(report)
    if not report["plugins"]:
        lines.append(f"运行 `{CLI} install` 预览安装。")
    else:
        if any(item["status"] == "legacy" for item in report["plugins"]):
            lines.append(f"旧方式安装可用 `{UPDATE_HINTS['legacy']}` 预览迁移。")
        lines.append(one_line(report) or "全部为最新。")
    return "\n".join(lines) + "\n"


def notice_due(env: Env, message: str) -> bool:
    """Show the same notice at most once per day."""
    state_file = env.cache_dir / "last_notice.json"
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    try:
        state = read_json(state_file)
        if state.get("digest") == digest and time.time() - state.get("at", 0) < CACHE_TTL_SECONDS:
            return False
    except (OSError, ValueError, AttributeError):
        pass
    try:
        write_json_atomic(state_file, {"digest": digest, "at": time.time()})
    except OSError:
        pass
    return True


def hook(env: Env, project: Path | None, output: str) -> int:
    """Session-start entry: silent unless something is behind; never fails the session."""
    if not update_check_enabled(env):
        return 0
    try:
        report = run_check(env, project, hook_mode=True)
    except Exception:  # noqa: BLE001 - an update notice must never break a session
        return 0
    message = one_line(report)
    if message is None or not notice_due(env, message):
        return 0
    if output == "claude-hook":
        print(json.dumps({"systemMessage": message, "hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": message}}, ensure_ascii=False))
    else:
        print(message)
    return 0


# ---------------------------------------------------------------- install / update / migrate

CLIENTS = ("claude", "codex", "cursor")


def github_slug(latest: dict) -> str:
    match = re.match(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", str(latest.get("repository", "")))
    if not match:
        raise CheckError(f"latest.json 的 repository 不是 GitHub 仓库：{latest.get('repository')}")
    return match.group(1)


def detect_clients(env: Env) -> list[str]:
    found = []
    if find_cli(env, "claude") or env.claude_dir.is_dir():
        found.append("claude")
    if find_cli(env, "codex") or env.codex_dir.is_dir():
        found.append("codex")
    if (env.home / ".cursor").is_dir():
        found.append("cursor")
    return found


def _safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise CheckError(f"发布包含越界路径：{member.name}")
            if not (member.isfile() or member.isdir()):
                raise CheckError(f"发布包含不支持的条目：{member.name}")
        try:
            bundle.extractall(destination, filter="data")  # noqa: S202 - members validated above
        except TypeError:  # Python without extraction filters
            bundle.extractall(destination)  # noqa: S202


def _plugin_versions(latest: dict) -> dict:
    return {name: item.get("version") for name, item in latest.get("plugins", {}).items()}


def github_release(latest: dict) -> dict:
    """Release details straight from GitHub, used when latest.json came from the raw fallback.

    Accepted only when that tag ships exactly the plugin versions ``latest`` lists,
    the same rule the site applies before attaching a release.
    """
    try:
        slug = github_slug(latest)
        release = _fetch_json(f"https://github.com/{slug}/releases/latest/download/release.json")
        tagged = _fetch_json(f"https://raw.githubusercontent.com/{slug}/{release['tag']}/latest.json")
    except (OSError, ValueError, KeyError, TypeError, CheckError, urllib.error.URLError):
        return {}
    return release if _plugin_versions(tagged) == _plugin_versions(latest) else {}


def release_source(latest: dict, workdir: Path, source: Path | None = None) -> Path:
    """A directory with plugins/<name>: a local checkout, or the tagged release tarball."""
    if source is not None:
        root = source.expanduser().resolve()
        if not (root / "plugins").is_dir():
            raise CheckError(f"{root} 不含 plugins/ 目录")
        return root
    release = latest.get("release") or github_release(latest)
    tag, digest = release.get("tag"), release.get("tarball_sha256")
    if not tag or not digest:
        raise CheckError("云端 latest.json 没有发布 tag 与摘要；请用 --source 指定本地仓库")
    url = release.get("tarball_url") or f"https://github.com/{github_slug(latest)}/releases/download/{tag}/agenthelm-{tag}.tar.gz"
    archive = workdir / "release.tar.gz"
    request = urllib.request.Request(url, headers={"User-Agent": "agenthelm"})
    with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as handle:  # noqa: S310
        shutil.copyfileobj(response, handle)
    if sha256_file(archive) != digest:
        raise CheckError(f"发布包摘要不符：{url}")
    extracted = workdir / "release"
    extracted.mkdir()
    _safe_extract(archive, extracted)
    roots = [path for path in extracted.iterdir() if path.is_dir()]
    if len(roots) != 1 or not (roots[0] / "plugins").is_dir():
        raise CheckError("发布包结构不符合预期")
    return roots[0]


def _agent_pack_config():
    sys.path.insert(0, str(RUNTIME_DIR))
    import agent_pack_config  # noqa: PLC0415 - shipped next to this file

    return agent_pack_config


@dataclass
class Step:
    """One planned action; ``command`` runs a client CLI, ``action`` runs in-process."""

    kind: str
    client: str | None
    description: str
    command: list[str] | None = None
    action: Any = None
    blocked: str | None = None

    def view(self) -> dict:
        data = {"kind": self.kind, "client": self.client, "description": self.description}
        if self.command:
            data["command"] = " ".join(self.command)
        if self.blocked:
            data["blocked"] = self.blocked
        return data


def known_cli_locations(env: Env, name: str) -> list[Path]:
    """Where a client's command line lives when it is not on PATH (GUI apps, per-user installs)."""
    home = env.home
    if name == "codex":
        bundle = "ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
        return [Path("/Applications") / bundle, home / "Applications" / bundle,
                home / ".local/bin/codex", Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex")]
    return [home / ".local/bin" / name, Path("/opt/homebrew/bin") / name, Path("/usr/local/bin") / name]


def find_cli(env: Env, name: str) -> str | None:
    """A client's command line: ``AGENTHELM_<NAME>`` override, then PATH, then the usual app locations."""
    override = env.get(f"AGENTHELM_{name.upper()}")
    if override and os.access(override, os.X_OK):
        return override
    on_path = shutil.which(name, path=env.get("PATH") or "")
    if on_path:
        return on_path
    for candidate in known_cli_locations(env, name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _cli(env: Env, name: str, *args: str) -> tuple[list[str], str | None]:
    """The command (using the resolved executable) and why it cannot run, if it cannot."""
    found = find_cli(env, name)
    return [found or name, *args], (None if found else f"未找到 {name} 命令（已查 PATH 与常见安装位置；可用 AGENTHELM_{name.upper()} 指定）")


def install_steps(env: Env, latest: dict, clients: list[str], plugins: list[str], source: Path | None,
                  home: Path, marketplace_source: str | None = None) -> list[Step]:
    slug = marketplace_source or github_slug(latest)
    steps: list[Step] = []
    for client in clients:
        if client == "claude":
            command, blocked = _cli(env, "claude", "plugin", "marketplace", "add", slug)
            steps.append(Step("install", "claude", f"Claude Code 添加 agenthelm 市场（{slug}）", command, blocked=blocked))
            for plugin in plugins:
                command, blocked = _cli(env, "claude", "plugin", "install", f"{plugin}@{MARKETPLACE}")
                steps.append(Step("install", "claude", f"Claude Code 安装 {plugin}", command, blocked=blocked))
        elif client == "codex":
            command, blocked = _cli(env, "codex", "plugin", "marketplace", "add", slug)
            steps.append(Step("install", "codex", f"Codex 添加 agenthelm 市场（{slug}）", command, blocked=blocked))
            for plugin in plugins:
                command, blocked = _cli(env, "codex", "plugin", "add", f"{plugin}@{MARKETPLACE}")
                steps.append(Step("install", "codex", f"Codex 安装 {plugin}", command, blocked=blocked))
        elif client == "cursor":
            steps.append(cursor_step(latest, plugins, source, home, "安装"))
    return steps


def cursor_step(latest: dict, plugins: list[str], source: Path | None, home: Path, verb: str) -> Step:
    def run(apply: bool, recovery_root: Path | None) -> list[dict]:
        config = _agent_pack_config()
        with tempfile.TemporaryDirectory(prefix="agenthelm-") as temporary:
            root = release_source(latest, Path(temporary), source)
            return [config.install_plugin(root / "plugins" / plugin, "cursor", home=home, apply=apply,
                                          replace=True, recovery_root=recovery_root)
                    for plugin in plugins]
    return Step("install", "cursor", f"Cursor {verb} {'、'.join(plugins)} 到 ~/.cursor/plugins/local（重启 Cursor 生效）",
                action=run)


def update_steps(env: Env, report: dict, project: Path | None, source: Path | None, home: Path,
                 accept: list[str], keep: list[str], recovery_dir: Path | None,
                 only: str | None) -> list[Step]:
    latest = report["_latest"]
    steps: list[Step] = []
    behind = [item for item in report["plugins"] if item["status"] == "behind"]
    if only in (None, "plugins"):
        claude = sorted({item["plugin"] for item in behind if item["client"] == "claude"})
        if claude:
            command, blocked = _cli(env, "claude", "plugin", "marketplace", "update", MARKETPLACE)
            steps.append(Step("update", "claude", "刷新 Claude Code 的 agenthelm 市场", command, blocked=blocked))
            for item in [entry for entry in behind if entry["client"] == "claude"]:
                extra = ["--scope", item["scope"]] if item.get("scope") and item["scope"] != "user" else []
                command, blocked = _cli(env, "claude", "plugin", "update", f"{item['plugin']}@{MARKETPLACE}", *extra)
                steps.append(Step("update", "claude", f"Claude Code 更新 {item['plugin']} → {item['latest']}",
                                  command, blocked=blocked))
        if any(item["client"] == "codex" for item in behind):
            command, blocked = _cli(env, "codex", "plugin", "marketplace", "upgrade", MARKETPLACE)
            steps.append(Step("update", "codex", "Codex 升级 agenthelm 市场与已装插件", command, blocked=blocked))
        cursor = sorted({item["plugin"] for item in behind if item["client"] == "cursor"})
        if cursor:
            steps.append(cursor_step(latest, cursor, source, home, "更新"))
    if only in (None, "content") and project is not None:
        for item in report["content"]:
            if item.get("status") not in {"update-available", "conflict"}:
                continue
            steps.append(content_step(env, item, report, project, accept, keep, recovery_dir))
    return steps


def content_step(env: Env, item: dict, report: dict, project: Path, accept: list[str], keep: list[str],
                 recovery_dir: Path | None) -> Step:
    if item["plugin"] != "agents-init":
        return Step("content", None, f"{item['pack']}：没有可用的刷新工具", blocked="unsupported")
    stale = any(entry["plugin"] == "agents-init" and entry["status"] == "behind" for entry in report["plugins"])
    description = f"重新渲染 {item['pack']} {item['version']} → {item['latest']}"

    def run(apply: bool, _recovery_root: Path | None) -> dict:
        installed = installed_plugins(env, {"agents-init"})
        manager = locate_companion("agents-init", installed)
        if manager is None:
            raise CheckError("未找到已安装的 agents-init")
        command = [sys.executable, str(manager / "skills/agents-init/scripts/init_project.py"),
                   "--project", str(project), "--mode", "refresh", "--output", "json"]
        for path in accept:
            command += ["--accept", path]
        for path in keep:
            command += ["--keep", path]
        if recovery_dir is not None:
            command += ["--recovery-dir", str(recovery_dir)]
        if apply:
            command.append("--apply")
        completed = subprocess.run(command, capture_output=True, text=True, check=False,
                                   env=env.child_environment())
        try:
            return json.loads(completed.stdout)
        except ValueError as exc:
            raise CheckError((completed.stderr or completed.stdout).strip()[:300]) from exc

    blocked = "先更新 agents-init 插件，再用新模板预览" if stale else None
    return Step("content", None, description, action=run, blocked=blocked)


def execute_steps(steps: list[Step], *, apply: bool, recovery_root: Path | None,
                  child_env: dict[str, str] | None = None) -> list[dict]:
    results = []
    for step in steps:
        record = step.view()
        if step.blocked:
            record["status"] = "blocked"
        elif step.action is not None:
            try:
                record["result"] = step.action(apply, recovery_root)
                record["status"] = "applied" if apply else "preview"
            except Exception as exc:  # noqa: BLE001 - report every step, stop nothing silently
                record["status"], record["error"] = "failed", str(exc)
        elif apply:
            completed = subprocess.run(step.command, capture_output=True, text=True, check=False, env=child_env)
            record["status"] = "applied" if completed.returncode == 0 else "failed"
            output = (completed.stdout + completed.stderr).strip()
            if output:
                record["output"] = output[-600:]
        else:
            record["status"] = "preview"
        results.append(record)
        if apply and record["status"] == "failed":
            break
    return results


# ---------------------------------------------------------------- legacy migration

def _personal_marketplace_entries(env: Env, names: set[str]) -> list[str]:
    try:
        data = read_json(env.home / ".agents/plugins/marketplace.json")
    except (OSError, ValueError):
        return []
    return [entry.get("name") for entry in data.get("plugins", []) if entry.get("name") in names]


def _codex_personal_sections(env: Env, names: set[str]) -> list[str]:
    try:
        text = (env.codex_dir / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return []
    return [name for name in sorted(names) if f'[plugins."{name}@personal"]' in text]


def _legacy_roots(env: Env) -> list[Path]:
    return [env.home / ".agents/plugins/plugins", env.home / ".claude/agent-pack/plugins"]


def _points_into(path: Path, roots: list[Path]) -> bool:
    try:
        target = path.resolve()
    except OSError:
        return False
    return any(root.resolve() == target or root.resolve() in target.parents for root in roots if root.exists())


def _is_plugin_skill_dir(path: Path, names: set[str]) -> bool:
    """True for <plugin>/skills/<skill> of an agenthelm plugin, wherever that plugin lives."""
    try:
        target = path.resolve()
        manifest = read_json(target.parent.parent / "plugin.json")
    except (OSError, ValueError):
        return False
    return target.parent.name == "skills" and manifest.get("name") in names


def legacy_artifacts(env: Env, names: set[str]) -> list[dict]:
    """Everything the pre-marketplace agent-pack bootstrap left behind for agenthelm plugins."""
    artifacts: list[dict] = []
    for root in _legacy_roots(env):
        for name in sorted(names):
            path = root / name
            if path.exists() or path.is_symlink():
                artifacts.append({"kind": "plugin-copy" if not path.is_symlink() else "plugin-record",
                                  "name": name, "path": str(path)})
    skills_dir = env.claude_dir / "skills"
    roots = _legacy_roots(env)
    for name in sorted(names):
        link = skills_dir / name
        if link.is_symlink() and (_points_into(link, roots) or _is_plugin_skill_dir(link, names)):
            artifacts.append({"kind": "claude-skill-link", "name": name, "path": str(link)})
        elif link.is_dir() and not link.is_symlink():
            artifacts.append({"kind": "claude-skill-copy", "name": name, "path": str(link), "manual": True})
    for name in _personal_marketplace_entries(env, names):
        artifacts.append({"kind": "codex-personal-entry", "name": name,
                          "path": str(env.home / ".agents/plugins/marketplace.json")})
    for name in _codex_personal_sections(env, names):
        artifacts.append({"kind": "codex-config-section", "name": name, "path": str(env.codex_dir / "config.toml")})
    for name in sorted(names):
        cache = env.codex_dir / "plugins" / "cache" / "personal" / name
        if cache.is_dir() and not cache.is_symlink():
            artifacts.append({"kind": "codex-personal-cache", "name": name, "path": str(cache)})
    try:
        servers = read_json(env.home / ".claude.json").get("mcpServers", {})
    except (OSError, ValueError, AttributeError):
        servers = {}
    for server, config in sorted(servers.items()):
        text = json.dumps(config)
        if any(str(root) in text for root in roots) and server in names:
            artifacts.append({"kind": "claude-user-mcp", "name": server, "path": str(env.home / ".claude.json")})
    return artifacts


def _remove_toml_sections(text: str, headers: set[str]) -> str:
    kept, skipping = [], False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped in headers
        if not skipping:
            kept.append(line)
    return "".join(kept)


def retire_legacy(env: Env, artifacts: list[dict], recovery: Path) -> list[dict]:
    """Move or edit each legacy artifact, keeping an exact copy under ``recovery``."""
    recovery.mkdir(parents=True, mode=0o700)
    done: list[dict] = []
    backed_up: set[str] = set()

    def inside_recovery(path: Path) -> Path:
        """The path below the recovery folder: home-relative, or absolute for locations outside home."""
        try:
            return path.relative_to(env.home)
        except ValueError:
            return Path("external") / str(path).lstrip("/")

    def backup(path: Path) -> None:
        if str(path) in backed_up:
            return
        target = recovery / "files" / inside_recovery(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        backed_up.add(str(path))

    for item in artifacts:
        path = Path(item["path"])
        if item.get("manual"):
            done.append({**item, "status": "manual"})
            continue
        if item["kind"] in {"plugin-copy", "plugin-record", "claude-skill-link", "codex-personal-cache"}:
            target = recovery / "moved" / inside_recovery(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, target)
            done.append({**item, "status": "moved", "to": str(target)})
        elif item["kind"] == "codex-personal-entry":
            backup(path)
            data = read_json(path)
            data["plugins"] = [entry for entry in data.get("plugins", []) if entry.get("name") != item["name"]]
            write_json_atomic(path, data)
            done.append({**item, "status": "edited"})
        elif item["kind"] == "codex-config-section":
            backup(path)
            text = path.read_text(encoding="utf-8")
            path.write_text(_remove_toml_sections(text, {f'[plugins."{item["name"]}@personal"]'}), encoding="utf-8")
            done.append({**item, "status": "edited"})
        elif item["kind"] == "claude-user-mcp":
            backup(path)
            data = read_json(path)
            data.get("mcpServers", {}).pop(item["name"], None)
            mode = path.stat().st_mode & 0o777
            write_json_atomic(path, data)
            os.chmod(path, mode)
            done.append({**item, "status": "edited"})
    write_json_atomic(recovery / "migration.json", {"at": datetime.now(UTC).isoformat(), "artifacts": done})
    return done


def recovery_dir_for(env: Env, label: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return env.home / ".agent-pack" / "recovery" / f"{stamp}-agenthelm-{label}"


def locate(env: Env, plugin: str, version: str | None = None, *, include_legacy: bool = False) -> dict:
    """Where a plugin is installed, newest first: the stable lookup for downstream wrappers.

    Legacy copies are left out unless asked for, so a wrapper that works today
    keeps working after ``migrate`` retires them.
    """
    found = [item for item in installed_plugins(env, {plugin})
             if (version is None or item.get("version") == version)
             and (include_legacy or item["client"] != "legacy")]
    found.sort(key=lambda item: (version_key(item.get("version") or "") or (0,), item["client"] != "legacy"),
               reverse=True)
    return {"ok": bool(found), "plugin": plugin, "version": version,
            "path": found[0]["path"] if found else None, "installs": found}


def doctor(env: Env, report: dict) -> dict:
    issues = []
    by_plugin: dict[str, list[dict]] = {}
    for item in report["plugins"]:
        by_plugin.setdefault(item["plugin"], []).append(item)
    for plugin, items in sorted(by_plugin.items()):
        clients = [item["client"] for item in items]
        if "legacy" in clients and any(client in {"claude", "codex"} for client in clients):
            issues.append({"plugin": plugin, "issue": "duplicate",
                           "detail": f"同时存在市场安装与旧方式安装，运行 {CLI} migrate"})
        versions = {item.get("version") for item in items if item["client"] != "legacy"}
        if len(versions) > 1:
            issues.append({"plugin": plugin, "issue": "version-skew",
                           "detail": "各客户端版本不一致：" + "、".join(f"{i['client']}={i.get('version')}" for i in items)})
    names = set(report["_latest"].get("plugins", {}))
    installed_names = {item["plugin"] for item in report["plugins"] if item["client"] != "legacy"}
    if installed_names and installed_names != names:
        issues.append({"plugin": None, "issue": "incomplete",
                       "detail": "缺少：" + "、".join(sorted(names - installed_names))})
    if not update_check_enabled(env):
        issues.append({"plugin": None, "issue": "update-check-disabled", "detail": "会话内更新检查已关闭"})
    return {"ok": not [issue for issue in issues if issue["issue"] != "update-check-disabled"],
            "issues": issues, "legacy": legacy_artifacts(env, names), "summary": report["summary"]}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="agenthelm", description=__doc__.split("\n\n")[0])
    sub = result.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser, *, project: bool = True) -> None:
        command.add_argument("--latest", help="latest.json 的 URL 或本地路径（测试、离线镜像）")
        command.add_argument("--output", choices=("text", "json"), default="text")
        command.add_argument("--home", type=Path, help=argparse.SUPPRESS)
        if project:
            command.add_argument("--project", type=Path, default=None, help="项目目录；默认当前目录")
            command.add_argument("--no-project", action="store_true", help="不检查项目内容")

    check = sub.add_parser("check", help="只读检查插件、模板/样例与 Opinion 是否落后于云端")
    common(check)
    check.add_argument("--offline", action="store_true", help="只用缓存，不联网")
    check.add_argument("--refresh", action="store_true", help="忽略一天的缓存，立即联网")
    check.add_argument("--hook", action="store_true", help="会话开始时静默调用：有更新才输出一行")
    check.add_argument("--hook-format", choices=("text", "claude-hook"), default="text")
    check.add_argument("--strict", action="store_true", help="有可更新项或脚手架差异时退出码为 1（CI 用）")
    check.add_argument("--show-skipped", action="store_true", help="列出被你跳过的项及理由")
    check.add_argument("--reprompt", action="store_true", help="忽略已记录的决定，重新评估全部脚手架差异")
    check.add_argument("--fingerprint", action="store_true",
                       help="输出脚手架指纹（骨架状态 + 受检 Profile 字段 + 内容锁 + 命名规则；不含 Opinion、自有字段、已跳过项）")
    check.add_argument("--explain", nargs="?", const="-", metavar="对方的 JSON",
                       help="与 --fingerprint 合用：不带参数时逐项列出这台的明细；带另一台机器 check --fingerprint "
                            "--output json 的输出文件时，指出两台差在哪里（有差异退出码 1）")

    install = sub.add_parser("install", help="经各客户端的原生市场安装 agenthelm 插件（默认预览）")
    common(install, project=False)
    install.add_argument("--client", action="append", choices=CLIENTS, help="默认安装到检测到的全部客户端")
    install.add_argument("--plugin", action="append", help="默认安装全部插件")
    install.add_argument("--source", type=Path, help="Cursor 安装用的本地仓库（默认下载发布包）")
    install.add_argument("--marketplace-source", help="市场来源（默认 latest.json 的 GitHub 仓库；可用镜像或本地路径）")
    install.add_argument("--apply", action="store_true")

    update = sub.add_parser("update", help="更新落后的插件与模板/样例（默认预览，确认后 --apply）")
    common(update)
    update.add_argument("--only", choices=("plugins", "content"))
    update.add_argument("--accept", action="append", default=[], help="内容更新：用新模板替换这个本地已改动的文件")
    update.add_argument("--keep", action="append", default=[], help="内容更新：保留这个文件的本地内容")
    update.add_argument("--recovery-dir", type=Path, help="被替换文件的备份目录（项目外）")
    update.add_argument("--source", type=Path, help="Cursor 更新用的本地仓库（默认下载发布包）")
    update.add_argument("--apply", action="store_true")

    migrate = sub.add_parser("migrate", help="把旧方式安装（agent-pack 复制 + 软链）迁到市场安装（默认预览）")
    common(migrate, project=False)
    migrate.add_argument("--install", action="store_true", help="先经市场安装到检测到的客户端")
    migrate.add_argument("--client", action="append", choices=CLIENTS)
    migrate.add_argument("--source", type=Path)
    migrate.add_argument("--marketplace-source")
    migrate.add_argument("--apply", action="store_true")

    doctor_parser = sub.add_parser("doctor", help="报告重复安装、版本不一致、旧方式残留与检查开关")
    common(doctor_parser)

    sub.add_parser("fix", add_help=False,
                   help="修复项目脚手架差异与更新（等同 project fix；默认预览，--apply 写入；--skip/--unskip 记录决定）")
    sub.add_parser("local-only", add_help=False,
                   help="只属于这台机器的目录（.venv、node_modules、缓存）不进云同步盘：check / fix / config（详见 local-only --help）")
    sub.add_parser("runtime", add_help=False,
                   help="用户级运行时：install / update / use / status（日常只需要 Python，不经过 npx）")
    sub.add_parser("project", add_help=False,
                   help="项目自助入口：找资料、看进展、项目指南、检查（不需要 AI Agent；详见 project --help）")
    locate_parser = sub.add_parser("locate", help="输出插件的已安装位置（最新优先），供下游脚本查找")
    locate_parser.add_argument("plugin")
    locate_parser.add_argument("--version", help="只要这个版本")
    locate_parser.add_argument("--include-legacy", action="store_true",
                               help="也列出旧方式安装（默认不列，避免清理后才失效）")
    locate_parser.add_argument("--output", choices=("text", "json"), default="json")
    locate_parser.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    return result


def _print_steps(results: list[dict], apply: bool) -> None:
    labels = {"preview": "预览", "applied": "完成", "blocked": "阻塞", "failed": "失败"}
    for record in results:
        line = f"  [{labels.get(record['status'], record['status'])}] {record['description']}"
        if record.get("command"):
            line += f"\n      $ {record['command']}"
        if record.get("blocked"):
            line += f"\n      {record['blocked']}"
        if record.get("error"):
            line += f"\n      {record['error']}"
        result = record.get("result")
        if isinstance(result, dict) and result.get("mode") == "refresh":
            for item in result.get("files", []):
                line += f"\n      {item['action']:8} {item['path']} [{item['state']}]"
            if result.get("hint"):
                line += f"\n      {result['hint']}"
        print(line)
    if not apply and results:
        print("以上为预览；确认后追加 --apply 执行。")


def run_project(argv: list[str]) -> int:
    """``agenthelm project``: hand over to project-orchestrator's project entry, which owns it."""
    installed = installed_plugins(current_env(), {"project-orchestrator"})
    copies = [PLUGIN_ROOT.parent / "project-orchestrator", *(Path(item["path"]) for item in installed)]
    # Older copies predate the project entry; only those that ship it count.
    usable = [path for path in copies if (path / "runtime" / "project_cli.py").is_file()]
    if not usable:
        stale = any((path / "plugin.json").is_file() for path in copies)
        found = "（已安装的 project-orchestrator 版本过旧）" if stale else ""
        print(f"agenthelm project 需要 project-orchestrator 的项目入口，但没有找到{found}。\n"
              f"  · 更新插件：{CLI} update，或从发布包安装运行时（见 INSTALL.md）", file=sys.stderr)
        return 3
    # Prefer the copy shipped next to this agent-pack (same release), then the newest installed one.
    sibling = PLUGIN_ROOT.parent / "project-orchestrator"
    newest = sibling if sibling in usable else \
        max(usable, key=lambda path: version_key(_manifest_version(path) or "") or (0,))
    sys.path.insert(0, str(newest / "runtime"))
    import project_cli  # noqa: PLC0415  located at run time

    project_cli.HOST = sys.modules[__name__]
    return project_cli.main(argv)


def run_nudge(argv: list[str]) -> int:
    """``agenthelm nudge``: hooks that collect opinion candidates and surface unconfirmed items."""
    sys.path.insert(0, str(RUNTIME_DIR))
    import agent_pack_nudge  # noqa: PLC0415 - shipped next to this file

    return agent_pack_nudge.main(argv, sys.modules[__name__])


def run_local_only(argv: list[str]) -> int:
    """``agenthelm local-only``: keep rebuildable directories out of cloud-synced folders."""
    return _local_only().main(argv, sys.modules[__name__])


def run_runtime(argv: list[str]) -> int:
    """``agenthelm runtime``: the user-level runtime that project launchers use without any AI client."""
    sys.path.insert(0, str(RUNTIME_DIR))
    import agenthelm_runtime  # noqa: PLC0415 - shipped next to this file

    return agenthelm_runtime.main(argv, sys.modules[__name__])


def print_fingerprint(env: Env, report: dict, args: argparse.Namespace, project: Path | None = None) -> int:
    """``check --fingerprint [--explain [file]]``: the scaffold fingerprint, and where two machines differ."""
    result = scaffold_fingerprint(env, report.get("scaffold"))
    if result is None:
        reason = (report.get("scaffold") or {}).get("error") or "当前目录不在 agenthelm 项目里"
        print(json.dumps({"ok": False, "error": reason}, ensure_ascii=False) if args.output == "json"
              else f"无法计算脚手架指纹：{reason}", file=sys.stderr if args.output != "json" else sys.stdout)
        return 4
    payload: dict = {"ok": True, "fingerprint": result["fingerprint"], "detail": result["detail"]}
    lines = [f"脚手架指纹 {result['fingerprint']}"]
    mine_local: dict = {}
    if project is not None:
        # 本机环境（外置链接、HOME）单独成段：没有 local-only 配置的项目，输出与以前完全一样。
        mine_local = local_only_report(env, project, mode="quick")["fingerprint"]
        if mine_local["detail"]:
            payload["local_only"] = mine_local
            lines.append(f"本机环境指纹 {mine_local['fingerprint']}")
    differences: list[str] = []
    if args.explain and args.explain != "-":
        try:
            other = read_json(Path(args.explain).expanduser())
        except (OSError, ValueError) as exc:
            print(f"读不了对方的输出：{exc}", file=sys.stderr)
            return 2
        theirs = other.get("detail") or (other.get("scaffold_fingerprint") or {}).get("detail") or other.get("fingerprint", {}).get("detail")
        if not isinstance(theirs, dict):
            print("对方的文件里没有指纹明细：请在对方机器运行 agenthelm check --fingerprint --output json 并保存输出", file=sys.stderr)
            return 2
        module = scaffold_module(env)
        differences = module.explain(result["detail"], theirs)
        their_local = (other.get("local_only") or {}).get("detail") or {}
        if mine_local.get("detail") or their_local:
            if not their_local:
                differences.append("本机环境：对方没有 local-only 明细（对方的 agent-pack 较旧或项目还没配置）")
            elif not mine_local.get("detail"):
                differences.append("本机环境：这台没有 local-only 配置，对方有")
            else:
                differences += _local_only().explain(mine_local["detail"], their_local)
        payload["same"] = not differences
        payload["differences"] = differences
        lines.append("两台机器的脚手架一致。" if not differences else f"有 {len(differences)} 处不同：")
        lines += [f"  {item}" for item in differences]
    elif args.explain:
        lines += [f"  {key} = {value}" for key, value in result["detail"].items()]
        lines += [f"  local-only {key} = {value}" for key, value in mine_local.get("detail", {}).items()]
    if args.output == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))
    return 1 if differences else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["project"]:
        return run_project(argv[1:])
    if argv[:1] == ["fix"]:
        return run_project(["fix", *argv[1:]])
    if argv[:1] == ["runtime"]:
        return run_runtime(argv[1:])
    if argv[:1] == ["nudge"]:
        return run_nudge(argv[1:])
    if argv[:1] == ["local-only"]:
        return run_local_only(argv[1:])
    args = parser().parse_args(argv)
    env = current_env(args.home)
    home = env.home
    project = None
    if hasattr(args, "no_project") and not args.no_project:
        project = (args.project or Path.cwd()).resolve()
    if args.command == "check" and args.hook:
        return hook(env, project, args.hook_format)
    if args.command == "locate":
        found = locate(env, args.plugin, args.version, include_legacy=args.include_legacy)
        if args.output == "json":
            print(json.dumps(found, ensure_ascii=False, indent=2))
        elif found["path"]:
            print(found["path"])
        return 0 if found["ok"] else 1
    try:
        if args.command == "check":
            report = run_check(env, project, latest_source=args.latest, offline=args.offline,
                               max_age=0 if args.refresh else CACHE_TTL_SECONDS, reprompt=args.reprompt)
            report.pop("_latest")
            if args.fingerprint or args.explain:
                return print_fingerprint(env, report, args, project)
            if args.output == "json":
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                sys.stdout.write(render_text(report, show_skipped=args.show_skipped))
            return 1 if args.strict and not report["summary"]["up_to_date"] else 0
        if args.command == "doctor":
            result = doctor(env, run_check(env, project, latest_source=args.latest))
            if args.output == "json":
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                for issue in result["issues"]:
                    print(f"  [{issue['issue']}] {issue.get('plugin') or ''} {issue['detail']}".rstrip())
                for item in result["legacy"]:
                    print(f"  [legacy] {item['kind']} {item['name']}: {item['path']}")
                print("没有发现问题。" if result["ok"] and not result["legacy"] else "")
            return 0 if result["ok"] else 1
        latest, _ = load_latest(env, args.latest, max_age=0)
        if args.command == "install":
            clients = args.client or detect_clients(env)
            plugins = args.plugin or sorted(latest.get("plugins", {}))
            steps = install_steps(env, latest, clients, plugins, args.source, home, args.marketplace_source)
            results = execute_steps(steps, apply=args.apply, recovery_root=None,
                                    child_env=env.child_environment())
            payload: dict = {"ok": all(r["status"] != "failed" for r in results), "applied": args.apply,
                             "clients": clients, "steps": results}
        elif args.command == "update":
            report = run_check(env, project, latest_source=args.latest, max_age=0)
            recovery = args.recovery_dir
            if args.apply and recovery is None and project is not None:
                recovery = recovery_dir_for(env, "content")
            steps = update_steps(env, report, project, args.source, home, args.accept, args.keep,
                                 recovery, args.only)
            results = execute_steps(steps, apply=args.apply, recovery_root=None,
                                    child_env=env.child_environment())
            payload = {"ok": all(r["status"] != "failed" for r in results), "applied": args.apply,
                       "steps": results, "summary": report["summary"]}
            if not steps:
                payload["message"] = "没有需要更新的内容。"
        else:  # migrate
            names = set(latest.get("plugins", {}))
            artifacts = legacy_artifacts(env, names)
            steps = install_steps(env, latest, args.client or detect_clients(env), sorted(names),
                                  args.source, home, args.marketplace_source) if args.install else []
            results = execute_steps(steps, apply=args.apply, recovery_root=None,
                                    child_env=env.child_environment())
            payload = {"ok": all(r["status"] != "failed" for r in results), "applied": args.apply,
                       "steps": results, "legacy": artifacts}
            if args.apply and payload["ok"] and artifacts:
                recovery = recovery_dir_for(env, "migrate")
                payload["retired"] = retire_legacy(env, artifacts, recovery)
                payload["recovery"] = str(recovery)
    except (CheckError, OSError, ValueError) as exc:
        if args.output == "json":
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.output == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if payload.get("message"):
            print(payload["message"])
        _print_steps(payload["steps"], args.apply)
        for item in payload.get("legacy", []):
            print(f"  [旧安装] {item['kind']} {item['name']}: {item['path']}" + ("（需手动处理）" if item.get("manual") else ""))
        if payload.get("recovery"):
            print(f"旧安装已移入：{payload['recovery']}（含 migration.json 清单，可按清单恢复）")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
