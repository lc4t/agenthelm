#!/usr/bin/env python3
"""agenthelm：检查已安装的 agenthelm 插件与项目内容是否落后于云端发布。

``check`` 只读：比较各客户端已安装的插件版本、项目里由模板渲染的文件，
以及 Opinion 来源与云端 ``latest.json``。``--hook`` 供会话开始时静默调用：
每天至多联网一次、离线不报错、只在有更新时输出一行，可用环境变量
``AGENTHELM_NO_UPDATE_CHECK=1`` 或用户配置 ``{"update_check": false}`` 关闭。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MARKETPLACE = "agenthelm"
DEFAULT_LATEST_URLS = (
    "https://agenthelm.sakanano.moe/latest.json",
    "https://raw.githubusercontent.com/lc4t/skill/main/latest.json",
)
ENV_LATEST = "AGENTHELM_LATEST_URL"
ENV_DISABLE = "AGENTHELM_NO_UPDATE_CHECK"
CACHE_TTL_SECONDS = 24 * 3600
FETCH_TIMEOUT_SECONDS = 3
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


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- environment

@dataclass
class Env:
    home: Path
    variables: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> str | None:
        value = self.variables.get(name)
        return value if value else None

    @property
    def cache_dir(self) -> Path:
        base = self.get("XDG_CACHE_HOME")
        return (Path(base) if base else self.home / ".cache") / "agenthelm"

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

def _fetch(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "agenthelm-check"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict) or data.get("marketplace") != MARKETPLACE:
        raise CheckError(f"{url} is not an agenthelm latest.json")
    return data


def load_latest(env: Env, source: str | None = None, *, offline: bool = False,
                max_age: int = CACHE_TTL_SECONDS) -> tuple[dict, dict]:
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
    errors = []
    for url in urls:
        try:
            latest = _fetch(url)
        except (OSError, ValueError, CheckError, urllib.error.URLError) as exc:
            errors.append(f"{url}: {exc}")
            continue
        try:
            write_json_atomic(cache_file, {"url": url, "fetched_at": time.time(), "latest": latest})
        except OSError:
            pass
        return latest, {"source": url, "cached": False}
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
    steps = []
    for entry in latest.get("migrations", []):
        if entry.get("plugin") != plugin:
            continue
        to = str(entry.get("to"))
        if is_newer(to, current) and not is_newer(to, target):
            steps.append(entry)
    return sorted(steps, key=lambda entry: version_key(str(entry.get("to"))) or (0,))


def plugin_findings(latest: dict, installed: list[dict]) -> list[dict]:
    findings = []
    for item in installed:
        target = latest.get("plugins", {}).get(item["plugin"], {}).get("version")
        current = item.get("version") or ""
        if target is None:
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
                       "breaking": step.get("breaking"), "summary": step.get("summary")} for step in steps],
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
    sibling = PLUGIN_ROOT.parent / name
    if (sibling / "plugin.json").is_file():
        return sibling
    candidates = [Path(item["path"]) for item in installed if item["plugin"] == name]
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
        profile_file = Path(temporary) / "profile.json"
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


# ---------------------------------------------------------------- report

def run_check(env: Env, project: Path | None, *, latest_source: str | None = None, offline: bool = False,
              max_age: int = CACHE_TTL_SECONDS) -> dict:
    latest, origin = load_latest(env, latest_source, offline=offline, max_age=max_age)
    names = set(latest.get("plugins", {}))
    installed = installed_plugins(env, names)
    plugins = plugin_findings(latest, installed)
    content = content_findings(project, latest) if project else []
    opinion = opinion_findings(project, installed) if project else []
    behind = [item for item in plugins if item["status"] == "behind"]
    legacy = [item for item in plugins if item["client"] == "legacy"]
    content_updates = [item for item in content if item["status"] in {"update-available", "conflict"}]
    opinion_updates = [item for item in opinion if item["status"] == "update-available"]
    return {
        "ok": True,
        "latest": {"source": origin, "released_at": latest.get("released_at")},
        "plugins": plugins,
        "content": content,
        "opinion": opinion,
        "summary": {
            "plugins_behind": len(behind),
            "breaking": any(item["breaking"] for item in behind),
            "content_updates": len(content_updates),
            "content_conflicts": sum(1 for item in content if item["status"] == "conflict"),
            "opinion_updates": len(opinion_updates),
            "legacy_installs": len(legacy),
            "up_to_date": not (behind or content_updates or opinion_updates or legacy),
        },
    }


UPDATE_HINTS = {
    "claude": "claude plugin marketplace update agenthelm && claude plugin update {plugin}@agenthelm",
    "codex": "codex plugin marketplace upgrade agenthelm",
    "cursor": "agenthelm update --client cursor",
    "legacy": "agenthelm migrate",
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
                     + (f"（{summary['content_conflicts']} 个与本地修改冲突）" if summary["content_conflicts"] else ""))
    if summary["opinion_updates"]:
        parts.append("Opinion 有新版本模板或规则")
    if summary["legacy_installs"]:
        parts.append(f"{summary['legacy_installs']} 个旧方式安装待迁移")
    return "agenthelm：" + "，".join(parts) + "。运行 `agenthelm check` 查看详情；更新前会先预览，由你决定。"


def render_text(report: dict) -> str:
    lines = []
    origin = report["latest"]["source"]
    lines.append(f"云端版本源：{origin.get('source')}" + ("（缓存）" if origin.get("cached") else ""))
    if not report["plugins"]:
        lines.append("未发现已安装的 agenthelm 插件。")
    for item in report["plugins"]:
        label = {"behind": "可更新", "current": "最新", "ahead": "高于云端", "unknown": "版本未知"}[item["status"]]
        line = f"  [{item['client']}] {item['plugin']} {item.get('version')} → {item.get('latest')}  {label}"
        if item["status"] == "behind":
            kinds = {"code": "代码", "content": "仅模板/样例", "both": "代码与模板"}
            if item["change_kinds"]:
                line += "（" + "、".join(kinds[kind] for kind in item["change_kinds"]) + "）"
            if item["breaking"]:
                line += "（含不兼容变更）"
            hint = UPDATE_HINTS.get(item["client"])
            if hint:
                line += f"\n      更新：{hint.format(plugin=item['plugin'])}"
        elif item["client"] == "legacy":
            line += f"\n      旧方式安装：{UPDATE_HINTS['legacy']}"
        lines.append(line)
    for item in report["content"]:
        if item.get("pack") is None:
            lines.append(f"  内容锁：{item.get('error')}")
            continue
        label = {"current": "最新", "update-available": "可更新（本地未改动）",
                 "conflict": "可更新（与本地修改冲突）", "unknown": "云端未列出"}[item["status"]]
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
        report = run_check(env, project)
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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="agenthelm", description=__doc__.split("\n\n")[0])
    sub = result.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="只读检查插件、模板/样例与 Opinion 是否落后于云端")
    check.add_argument("--project", type=Path, default=None, help="同时检查该项目的模板与 Opinion；默认当前目录")
    check.add_argument("--no-project", action="store_true", help="只检查已安装插件")
    check.add_argument("--latest", help="latest.json 的 URL 或本地路径（测试、离线镜像）")
    check.add_argument("--offline", action="store_true", help="只用缓存，不联网")
    check.add_argument("--refresh", action="store_true", help="忽略一天的缓存，立即联网")
    check.add_argument("--hook", action="store_true", help="会话开始时静默调用：有更新才输出一行")
    check.add_argument("--output", choices=("text", "json", "claude-hook"), default="text")
    check.add_argument("--strict", action="store_true", help="有可更新项时退出码为 1（CI 用）")
    check.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    env = current_env(args.home)
    project = None if args.no_project else (args.project or Path.cwd()).resolve()
    if args.hook:
        return hook(env, project, args.output)
    try:
        report = run_check(env, project, latest_source=args.latest, offline=args.offline,
                           max_age=0 if args.refresh else CACHE_TTL_SECONDS)
    except (CheckError, OSError, ValueError) as exc:
        if args.output == "json":
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.output == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(render_text(report))
    return 1 if args.strict and not report["summary"]["up_to_date"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
