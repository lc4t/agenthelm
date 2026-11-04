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
from i18n_agent_pack import _, N_  # noqa: E402

MARKETPLACE = "agenthelm"
NPX = "npx @sakanano-moe/agenthelm"


def cli_command(root: Path | None = None) -> str:
    """The command printed in hints. A stable runtime keeps the plain ``npx`` form, which resolves to npm's
    ``latest``. A pre-release runtime (``6.11.0-rc.2``) pins ``@<that version>``: an untagged npx would fetch the
    older stable release, and ``@next`` may already point at a newer candidate. The npm package version equals the
    suite version (release.yml sets it from the tag), so the pinned command runs exactly this runtime's code."""
    try:
        import agenthelm_runtime  # noqa: PLC0415 - shipped next to this file
        label = agenthelm_runtime.running_label(root if root is not None else Path(__file__).resolve().parents[3])
    except Exception:  # noqa: BLE001 - a hint must never break the command
        return NPX
    return f"{NPX}@{label}" if label and "-" in label else NPX


CLI = cli_command()
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


class Unavailable(CheckError):
    """A client cannot be handled on this run for a reason outside the update itself (not a failed update)."""


def version_key(value: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", value or "")
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(candidate: str, current: str) -> bool:
    new, old = version_key(candidate), version_key(current)
    return new is not None and old is not None and new > old


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: Any) -> None:
    # A symlinked dotfile (hooks.json, settings.json) keeps being a symlink: write to its target, temp file beside it.
    path = Path(os.path.realpath(path))
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
    """Return (latest, origin). A local path is read directly; URLs go through a daily cache.

    The cache is used only when it came from one of the sources this call would fetch: an explicit
    ``--latest`` / ``AGENTHELM_LATEST_URL`` that differs from the cached source ignores the cache (and the
    origin says so), it is never answered with another source's content.
    """
    source = source or env.get(ENV_LATEST)
    if source and not re.match(r"https?://", source):
        return read_json(Path(source).expanduser()), {"source": source, "cached": False}
    urls = (source,) if source else DEFAULT_LATEST_URLS
    cache_file = env.cache_dir / "latest.json"
    cached = None
    ignored_cache = None
    try:
        cached = read_json(cache_file)
    except (OSError, ValueError):
        pass
    if not isinstance(cached, dict) or "latest" not in cached:
        cached = None
    if cached and cached.get("url") not in urls:
        ignored_cache, cached = cached.get("url"), None
    extra = {"ignored_cache": ignored_cache} if ignored_cache else {}
    if cached and (offline or time.time() - cached.get("fetched_at", 0) < max_age):
        return cached["latest"], {"source": cached.get("url"), "cached": True, "fetched_at": cached.get("fetched_at")}
    if offline:
        if ignored_cache:
            raise CheckError(_("离线，且缓存来自另一个来源（{0}），不能代替 {1}").format(ignored_cache, urls[0]))
        raise CheckError(_("离线且没有缓存的 latest.json"))
    failure_file = env.cache_dir / "last_failure.json"
    if backoff:
        try:
            if time.time() - read_json(failure_file).get("at", 0) < FAILURE_BACKOFF_SECONDS:
                if cached:
                    return cached["latest"], {"source": cached.get("url"), "cached": True, "stale": True,
                                              "fetched_at": cached.get("fetched_at")}
                raise CheckError(_("最近联网失败，稍后再试"))
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
        return latest, {"source": url, "cached": False, **extra}
    try:
        write_json_atomic(failure_file, {"at": time.time()})
    except OSError:
        pass
    if cached:
        return cached["latest"], {"source": cached.get("url"), "cached": True, "stale": True,
                                  "fetched_at": cached.get("fetched_at")}
    raise CheckError(_("无法获取 latest.json：") + "; ".join(errors))


def origin_line(origin: dict) -> str:
    """The version source a run actually used, and whether it came from the cache."""
    line = _("云端版本源：{0}").format(origin.get("source"))
    if origin.get("cached"):
        when = ""
        if origin.get("fetched_at"):
            when = _("，获取于 {0}").format(datetime.fromtimestamp(origin["fetched_at"], UTC).strftime("%Y-%m-%d %H:%M UTC"))
        line += (_("（缓存，已过期，联网失败{0}）") if origin.get("stale") else _("（缓存{0}）")).format(when)
    if origin.get("ignored_cache"):
        line += _("（已忽略来自 {0} 的缓存）").format(origin["ignored_cache"])
    return line


# ---------------------------------------------------------------- installed plugins

def _manifest_version(path: Path) -> str | None:
    try:
        return str(read_json(path / "plugin.json").get("version"))
    except (OSError, ValueError, AttributeError):
        return None


def _claude_records(env: Env) -> dict:
    try:
        record = read_json(env.claude_dir / "plugins" / "installed_plugins.json").get("plugins", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return record if isinstance(record, dict) else {}


def _local_directory_marketplace(env: Env) -> bool:
    """True when Claude Code's own record says the ``agenthelm`` marketplace comes from a local directory or file.

    Unverified against a real client: this relies on ``plugins/known_marketplaces.json`` keeping a ``source`` object
    whose ``source`` field is ``directory`` or ``file`` for such marketplaces."""
    try:
        record = read_json(env.claude_dir / "plugins" / "known_marketplaces.json")[MARKETPLACE]
        return str(record["source"]["source"]) in {"directory", "file"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _dev_reason(path: Path, *, local_marketplace: bool = False) -> str | None:
    """Why an install under the ``agenthelm`` marketplace is a development install (None: it is a normal one)."""
    if path.is_symlink():
        return "symlink"
    if local_marketplace:
        return "local-directory-marketplace"
    return None


def installed_plugins(env: Env, names: set[str]) -> list[dict]:
    """Installs the ``agenthelm`` marketplace manages. A symlinked install, or one from a local-directory marketplace,
    carries ``dev`` (the reason): it is reported and left alone. Installs from other marketplaces: see ``other_installs``."""
    found: list[dict] = []
    local_marketplace = _local_directory_marketplace(env)
    for key, entries in sorted(_claude_records(env).items()):
        plugin, _sep, market = key.partition("@")
        if market != MARKETPLACE or plugin not in names or not isinstance(entries, list):
            continue
        for entry in entries:
            path = Path(str(entry.get("installPath", "")))
            # A git-SHA version means the entry had no version; fall back to the manifest.
            version = entry.get("version") if version_key(str(entry.get("version"))) else _manifest_version(path)
            item = {"client": "claude", "plugin": plugin, "version": version, "path": str(path),
                    "scope": entry.get("scope")}
            dev = _dev_reason(path, local_marketplace=local_marketplace)
            if dev:
                item["dev"] = dev
            found.append(item)
    codex_cache = env.codex_dir / "plugins" / "cache" / MARKETPLACE
    for plugin in sorted(names):
        directories = [path for path in (codex_cache / plugin).glob("*") if (path / "plugin.json").is_file()]
        if directories:
            newest = max(directories, key=lambda path: version_key(path.name) or (0,))
            item = {"client": "codex", "plugin": plugin, "version": _manifest_version(newest), "path": str(newest)}
            if _dev_reason(newest) or _dev_reason(codex_cache / plugin):
                item["dev"] = "symlink"
            found.append(item)
    for client, base in (("cursor", env.home / ".cursor" / "plugins" / "local"),
                         ("legacy", env.home / ".agents" / "plugins" / "plugins"),
                         ("legacy", env.home / ".claude" / "agent-pack" / "plugins")):
        for plugin in sorted(names):
            path = base / plugin
            if (path / "plugin.json").is_file():
                item = {"client": client, "plugin": plugin, "version": _manifest_version(path), "path": str(path)}
                # A symlinked Cursor entry points at a working copy; a real directory is what `install` writes.
                if client == "cursor" and path.is_symlink():
                    item["dev"] = "symlink"
                found.append(item)
    return found


def legacy_owner(env: Env, item: dict) -> str | None:
    """The client a legacy (pre-marketplace) entry belongs to, judged from its recorded path; None when unknown.

    ``~/.claude/...`` is Claude Code's; ``~/.agents/...`` is the Codex personal marketplace the old bootstrap wrote
    (``[plugins."<name>@personal"]`` in Codex's config). Anything else, or a path that cannot be read, has no owner.
    """
    try:
        path = Path(str(item.get("path", ""))).expanduser().resolve()
        roots = {"claude": [env.home / ".claude", env.claude_dir], "codex": [env.home / ".agents", env.codex_dir]}
        for client, candidates in roots.items():
            if any(path == root.resolve() or root.resolve() in path.parents for root in candidates):
                return client
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def other_installs(env: Env, names: set[str]) -> list[dict]:
    """Installs of agenthelm plugins that no ``agenthelm`` marketplace manages: another marketplace name (a fork, a
    local-directory marketplace under its own name). Not found here: a Claude Code ``--plugin-dir`` session (it
    leaves no record we know of — unverified) and how Codex / Cursor record local development installs (unverified)."""
    found: list[dict] = []
    for key, entries in sorted(_claude_records(env).items()):
        plugin, _sep, market = key.partition("@")
        if not market or market == MARKETPLACE or plugin not in names or not isinstance(entries, list):
            continue
        for entry in entries:
            path = Path(str(entry.get("installPath", "")))
            version = entry.get("version") if version_key(str(entry.get("version"))) else _manifest_version(path)
            found.append({"client": "claude", "plugin": plugin, "version": version, "path": str(path),
                          "scope": entry.get("scope"), "marketplace": market, "dev": "other-marketplace"})
    codex_cache = env.codex_dir / "plugins" / "cache"
    for market_dir in sorted(path for path in codex_cache.glob("*") if path.is_dir()) if codex_cache.is_dir() else []:
        if market_dir.name in {MARKETPLACE, "personal"}:  # "personal" is the legacy bootstrap, handled as legacy
            continue
        for plugin in sorted(names):
            directories = [path for path in (market_dir / plugin).glob("*") if (path / "plugin.json").is_file()]
            if directories:
                newest = max(directories, key=lambda path: version_key(path.name) or (0,))
                found.append({"client": "codex", "plugin": plugin, "version": _manifest_version(newest),
                              "path": str(newest), "marketplace": market_dir.name, "dev": "other-marketplace"})
    return found


def declared_marketplaces(env: Env, project: Path | None) -> list[dict]:
    """The ``extraKnownMarketplaces.agenthelm`` declarations in Claude Code's user and project settings files.
    The tool only reads them; it never edits a settings file."""
    files = [("user", env.claude_dir / "settings.json")]
    if project is not None:
        files += [("project", project / ".claude" / "settings.json"), ("local", project / ".claude" / "settings.local.json")]
    found = []
    for scope, path in files:
        try:
            declared = read_json(path).get("extraKnownMarketplaces", {}).get(MARKETPLACE)
        except (OSError, ValueError, AttributeError):
            continue
        if declared is not None:
            found.append({"scope": scope, "file": str(path),
                          "source": declared.get("source") if isinstance(declared, dict) and "source" in declared and isinstance(declared.get("source"), dict) else declared})
    return found


def market_sync_problem(env: Env, declared: list[dict]) -> dict | None:
    """Why Claude Code cannot update right now: ``settings.json`` declares the agenthelm market, but the market Claude
    Code has registered (``plugins/known_marketplaces.json``) is missing or came from another source / ref. Claude Code
    only registers a declared market at start-up, so the update fails with "Marketplace 'agenthelm' not found" until it
    restarts. None: no declaration, or both agree. Unverified against a real client: the record layout is assumed."""
    if not declared:
        return None
    path = env.claude_dir / "plugins" / "known_marketplaces.json"
    wanted = declared_source_text(declared[0]["source"])
    registered = None
    try:
        record = read_json(path).get(MARKETPLACE)
        registered = declared_source_text(record["source"]) if isinstance(record, dict) and "source" in record else None
        code = None if registered is not None and all(
            registered == declared_source_text(item["source"]) for item in declared) else (
            "not-registered" if registered is None else "source-differs")
    except FileNotFoundError:
        code = "not-registered"
    except (OSError, ValueError, AttributeError):
        code = "unreadable"
    if code is None:
        return None
    if code == "source-differs":
        wanted = next(declared_source_text(item["source"]) for item in declared
                      if declared_source_text(item["source"]) != registered)
    reason = {"not-registered": _("Claude Code 还没有登记 agenthelm 市场（{0} 里没有它），而 settings.json 已声明 {1}").format(path, wanted),
              "unreadable": _("读不到 Claude Code 的已登记市场（{0} 无法解析），而 settings.json 已声明 {1}").format(path, wanted),
              "source-differs": _("settings.json 声明的市场是 {0}，Claude Code 已登记的是 {1}").format(wanted, registered)}[code]
    return {"status": "blocked", "reason_code": code, "reason": reason, "declared": wanted, "registered": registered,
            "next": _("重启 Claude Code 让它按 settings.json 登记市场；或手动执行：claude plugin marketplace add {0}").format(wanted)}


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
        elif item.get("dev"):
            status = "dev"  # a development / local install: reported, never updated or migrated
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


# ---------------------------------------------------------------- install state (#31)

INSTALL_STATES = ("none", "partial", "marketplace", "dev", "legacy", "mixed", "market-conflict")
STATE_LABELS = {"none": N_("未安装"), "partial": N_("部分安装"), "marketplace": N_("市场安装"), "dev": N_("开发版/本地安装"),
                "legacy": N_("旧方式安装"), "mixed": N_("多处安装"), "market-conflict": N_("市场冲突")}
ORIGIN_LABELS = {"symlink": N_("指向别处的软链"), "local-directory-marketplace": N_("本地目录市场"),
                 "other-marketplace": N_("其他市场")}
# Where the client keeps what a replacement moves away ("" = nothing is replaced).
RECOVERY_HINTS = {
    "claude": N_("Claude Code 升级后把旧版本目录留在 ~/.claude/plugins/cache/agenthelm/<插件>/<旧版本>/（客户端行为，agenthelm 不清理）"),
    "codex": N_("由 Codex 管理，agenthelm 不备份；需要回退时用 codex 重新安装指定版本"),
    "cursor": N_("被替换的目录备份在 ~/.agent-pack/recovery/<时间>-cursor-<插件>/replaced-plugin/<插件>/"),
    "legacy": N_("旧安装移入 ~/.agent-pack/recovery/<时间>-agenthelm-migrate/（含 migration.json 清单，可按清单恢复）"),
}


def declared_source_text(source: Any) -> str:
    """A declared marketplace source in the form ``--marketplace-source`` takes: ``owner/repo[#ref]``, a URL or a path."""
    if not isinstance(source, dict):
        return str(source)
    kind = source.get("source")
    target = source.get("repo") or source.get("url") or source.get("path") or ""
    ref = source.get("ref")
    return f"{target}#{ref}" if ref and kind in {"github", "git", "url"} else str(target)


def release_tag(latest: dict) -> str | None:
    """The tag of the release ``latest`` describes (None: latest.json carries no release block)."""
    release = latest.get("release")
    tag = release.get("tag") if isinstance(release, dict) else None
    return str(tag) if tag else None


def market_pin(latest: dict, declared: list[dict]) -> dict | None:
    """A declaration of the agenthelm repository pinned to a ref, with whether that ref is the release ``latest`` describes."""
    tag = release_tag(latest)
    for item in declared:
        source = item["source"]
        if isinstance(source, dict) and source.get("ref") and not market_conflict(latest, [{**item, "source": {k: v for k, v in source.items() if k != "ref"}}]):
            return {"scope": item["scope"], "file": item["file"], "ref": source["ref"], "target": tag,
                    "at_target": bool(tag) and source["ref"] == tag}
    return None


def market_conflict(latest: dict, declared: list[dict]) -> dict | None:
    """Claude Code refuses ``marketplace add`` for a name whose declared source differs (issue #31 comment, measured on
    macOS): flag a declaration that points elsewhere, or that pins a ref (a later `marketplace add` without it is refused)."""
    try:
        slug = github_slug(latest)
    except CheckError:
        slug = ""
    for item in declared:
        source = item["source"]
        if not isinstance(source, dict):
            return {**item, "reason": "points-elsewhere", "declared": declared_source_text(source), "ref": None}
        target = str(source.get("repo") or source.get("url") or source.get("path") or "")
        same = source.get("source") == "github" and target == slug or (
            source.get("source") in {"git", "url"} and slug and slug in target)
        if not same:
            return {**item, "reason": "points-elsewhere", "declared": declared_source_text(source), "ref": source.get("ref")}
        if source.get("ref"):
            if release_tag(latest) == source.get("ref"):
                continue  # pinned to the very release being checked: nothing to resolve
            return {**item, "reason": "pinned", "declared": declared_source_text(source), "ref": source.get("ref")}
    return None


def _install_record(item: dict) -> dict:
    kind = "legacy" if item["client"] == "legacy" else "dev" if item.get("dev") else "marketplace"
    origin = (f"other-marketplace:{item['marketplace']}" if item.get("dev") == "other-marketplace"
              else item.get("dev") or MARKETPLACE)
    return {"plugin": item["plugin"], "version": item.get("version"), "path": item["path"], "kind": kind,
            "origin": origin, "scope": item.get("scope"), "status": item.get("status")}


def _origin_text(record: dict) -> str:
    base, _sep, extra = record["origin"].partition(":")
    text = _(ORIGIN_LABELS[base]) if base in ORIGIN_LABELS else _("市场安装")
    return f"{text} {extra}" if extra else text


def diagnose_client(client: str, latest: dict, findings: list[dict], others: list[dict], conflict: dict | None,
                    pin: dict | None = None) -> dict:
    names = sorted(latest.get("plugins", {}))
    legacy = [item for item in findings if item["client"] == "legacy"] if client in {"claude", "codex"} else []
    records = [_install_record(item) for item in findings if item["client"] == client]
    records += [_install_record({**item, "status": "dev"}) for item in others if item["client"] == client]
    records += [_install_record(item) for item in legacy]
    present = sorted({item["plugin"] for item in records if item["kind"] != "legacy"})
    missing = [name for name in names if name not in present]
    managed = [item for item in records if item["kind"] == "marketplace"]
    dev = [item for item in records if item["kind"] == "dev"]
    old = [item for item in records if item["kind"] == "legacy"]
    # Several places for one plugin only clash when they come from different sources (two scopes of the same market are fine).
    clashes = []
    for plugin in sorted({item["plugin"] for item in records}):
        mine = [item for item in records if item["plugin"] == plugin]
        if len({(item["kind"], item["origin"]) for item in mine}) > 1:
            clashes.append({"plugin": plugin, "installs": mine,
                            "versions_differ": len({item["version"] for item in mine}) > 1})
    sub = None
    if managed:
        statuses = {item["status"] for item in managed}
        sub = "behind" if "behind" in statuses else "ahead" if "ahead" in statuses else \
            "current" if statuses == {"current"} else "unknown"
    if clashes:
        base = "mixed"
    elif dev:
        base = "dev"
    elif old and not managed:
        base = "legacy"
    elif not records:
        base = "none"
    elif missing:
        base = "partial"
    else:
        base = "marketplace"
    state = "market-conflict" if conflict and base in {"none", "partial", "marketplace", "legacy"} else base
    diag = {"client": client, "state": state, "base_state": base, "sub_status": sub, "installed": present,
            "missing": missing, "installs": records,
            "conflicts": [{"plugin": c["plugin"], "versions_differ": c["versions_differ"],
                           "effective": None} for c in clashes],
            "market_conflict": {k: conflict[k] for k in ("scope", "file", "reason", "declared", "ref")} if conflict else None,
            "untouched": [{"plugin": item["plugin"], "path": item["path"], "origin": item["origin"]} for item in dev],
            "market_pin": pin}
    diag.update(_advice(diag, client))
    if pin and pin["at_target"]:
        diag["summary"] += _("settings 把 agenthelm 市场固定在目标版本 {0}，无需改动。").format(pin["ref"])
    return diag


def _advice(diag: dict, client: str) -> dict:
    """The one-sentence conclusion, the next command (a preview), whether it changes an existing install, and where originals go."""
    name = CLIENT_NAMES[client]
    state, base = diag["state"], diag["base_state"]
    missing = _("、").join(diag["missing"])
    install = f"{CLI} install --client {client}"
    update = f"{CLI} update --client {client}"
    recovery = None
    changes = False
    command: str | None
    if state == "none":
        summary, command = _("{0} 里没有任何 agenthelm 插件。").format(name), install
    elif state == "partial":
        summary = _("{0} 只装了一部分，缺 {1}。").format(name, missing)
        if "agents-init" in diag["installed"]:
            summary += _("agents-init 依赖其余插件，缺失时它无法完整工作。")
        command = install
        if client == "cursor":  # Cursor's install rewrites every plugin it installs
            changes, recovery = True, _(RECOVERY_HINTS["cursor"])
    elif state == "marketplace":
        sub = diag["sub_status"]
        if sub == "behind":
            summary, command, changes = _("{0} 通过 agenthelm 市场安装，有插件落后于云端。").format(name), update, True
            recovery = _(RECOVERY_HINTS[client])
        elif sub == "ahead":
            summary, command = _("{0} 通过 agenthelm 市场安装，版本高于云端（可能是预发布）。").format(name), None
        elif sub == "current":
            summary, command = _("{0} 通过 agenthelm 市场安装，已是最新。").format(name), None
        else:
            summary, command = _("{0} 通过 agenthelm 市场安装，但读不到版本，无法比较。").format(name), None
    elif state == "dev":
        where = _("；").join(_("{0}（{1}：{2}）").format(item['plugin'], _origin_text(item), item['path']) for item in diag["untouched"])
        summary = _("{0} 有开发版/本地安装：{1}。update / migrate 默认不改动它们，由你决定是否纳入市场管理。").format(name, where)
        command = update  # the preview names the untouched installs
        changes = any(item["status"] == "behind" for item in diag["installs"] if item["kind"] == "marketplace")
        if missing:
            summary += _("还缺 {0}，可用 install 补装（不会改动已有的安装）。").format(missing)
    elif state == "legacy":
        summary = _("{0} 只有旧方式安装（复制 + 软链），不会自动更新。").format(name)
        command, changes, recovery = f"{CLI} migrate --install", True, _(RECOVERY_HINTS["legacy"])
    elif state == "mixed":
        plugins = _("、").join(c["plugin"] for c in diag["conflicts"])
        skew = any(c["versions_differ"] for c in diag["conflicts"])
        summary = _("{0} 里 {1} 有多处安装{2}；哪一处生效由客户端决定（未确认），建议保留一处。").format(
            name, plugins, _("，版本不一致") if skew else "")
        legacy_involved = any(item["kind"] == "legacy" for item in diag["installs"])
        command = f"{CLI} migrate" if legacy_involved else f"{CLI} doctor"
        if legacy_involved:
            changes, recovery = True, _(RECOVERY_HINTS["legacy"])
    else:  # market-conflict
        conflict = diag["market_conflict"]
        if conflict["reason"] == "pinned":
            summary = _("{0} 的 settings 把 agenthelm 市场固定在 {1}（{2}）：不带它的 marketplace add 会被拒绝，update 也只会跟随这个 ref。").format(
                name, conflict["ref"], conflict["file"])
        else:
            summary = _("{0} 的 settings 声明 agenthelm 市场指向 {1}（{2}），与 agenthelm 默认来源不同：用默认来源 marketplace add 会被拒绝。").format(
                name, conflict["declared"], conflict["file"])
        summary += _("agenthelm 不会修改 settings.json：要么改那处声明，要么让 --marketplace-source 与它一致。")
        command = f'{install} --marketplace-source "{conflict["declared"]}"'
        if base == "legacy":
            changes, recovery = True, _(RECOVERY_HINTS["legacy"])
    result = {"summary": summary, "next_command": command, "modifies_existing": changes,
              "modifies_note": (_("会改动已有的安装") if changes else _("不会改动已有的安装")) if command else None,
              "recovery": recovery}
    if diag["untouched"] and result["modifies_note"]:
        result["modifies_note"] += _("；开发版/本地安装不会改动")
    return result


def install_state(env: Env, latest: dict, findings: list[dict], others: list[dict], declared: list[dict],
                  clients: list[str]) -> dict:
    """Per selected client: none / partial / marketplace(current|behind|ahead) / dev / legacy / mixed / market-conflict."""
    detected = set(detect_clients(env))
    conflict = market_conflict(latest, declared)
    pin = market_pin(latest, declared)
    result: dict = {"clients": {}, "unconfirmed": [
        "claude marketplace add on a same-named marketplace beyond the measured declared-source mismatch",
        "claude --plugin-dir leaves no record in installed_plugins.json",
        "codex and cursor local development install forms, and a Codex-side marketplace declaration"]}
    for client in clients:
        mine = [item for item in [*findings, *others] if item["client"] in {client, "legacy"}]
        if client not in detected and not any(item["client"] == client for item in mine):
            continue
        # A legacy entry belongs to the client its path says (same rule as the summary count); unknown owner: both.
        scoped = [item for item in findings
                  if item["client"] != "legacy" or legacy_owner(env, item) in {client, None}]
        result["clients"][client] = diagnose_client(client, latest, scoped, others,
                                                    conflict if client == "claude" else None,
                                                    pin if client == "claude" else None)
    return result


# ---------------------------------------------------------------- project content

def content_findings(project: Path, latest: dict) -> list[dict]:
    lock_path = project / CONTENT_LOCK
    if not lock_path.is_file():
        return []
    try:
        lock = read_json(lock_path)
    except (OSError, ValueError) as exc:
        return [{"pack": None, "status": "error", "error": _("{0} 无法解析：{1}").format(CONTENT_LOCK, exc)}]
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


def locate_companion(name: str, installed: list[dict], *, excluded: set[Path] | None = None) -> Path | None:
    """The newest copy of a companion plugin: next to this one or in any client install."""
    candidates = [PLUGIN_ROOT.parent / name, *(Path(item["path"]) for item in installed if item["plugin"] == name)]
    candidates = [path for path in candidates if path not in (excluded or set()) and (path / "plugin.json").is_file()]
    return max(candidates, key=lambda path: version_key(_manifest_version(path) or "") or (0,)) if candidates else None


def opinion_findings(project: Path, installed: list[dict]) -> list[dict]:
    """Delegate to opinion-manager: newer catalog versions of the templates and rules the project locked."""
    lock_path = project / OPINION_LOCK
    if not lock_path.is_file():
        return []
    manager = locate_companion("opinion-manager", installed)
    if manager is None:
        return [{"status": "unknown", "error": _("未找到已安装的 opinion-manager")}]
    try:
        profile = read_json(lock_path)["profile"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [{"status": "error", "error": _("{0} 无法解析：{1}").format(OPINION_LOCK, exc)}]
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
        return [{"status": "error", "error": result.get("error", _("opinion-manager updates 失败"))}]
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
        return {**base, "error": _("已安装的 agents-init 没有脚手架检查（更新 agents-init 后再检查）")}
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
                         "title": _("这台机器还没有安装用户级运行时"), "reason": text["reason"], "impact": text["impact"],
                         "fingerprint": "runtime-missing", "fix": "install-runtime", "next": text["label"]})
    where = sync_folder(root)
    if where is not None:
        extra = _("；其中有可重建目录：{0}").format(_('、').join(where['rebuildable'][:6])) if where["rebuildable"] else ""
        findings.append({"id": "local:sync-folder", "kind": "sync-folder", "level": "suggest", "scope": "machine",
                         "title": _("项目位于同步盘（{0}）").format(where['service']),
                         "reason": f"{where['path']}{extra}",
                         "impact": _("多台机器共用同一份项目时：项目文件随同步盘走，但运行时、缓存与 wf 的台账是每台机器各一份；"
                                   "可重建目录（依赖、构建产物）同步很慢，也容易产生冲突副本"),
                         "fingerprint": sha256_text(where["service"]), "fix": "sync-folder",
                         "next": _("运行 agenthelm fix sync-folder 查看指引（只给方案，不改文件、不删内容）；不需要就 fix --skip local:sync-folder"),
                         "sync": where})
    # local-only：同步盘里的本机专属目录、外置链接、HOME、体积……没有同步服务也没有配置时不扫描。
    try:
        engine = _local_only().check(root, home=env.home, environ=env.variables, mode="quick" if quick else "standard",
                                     user_config=env.config_file)
        findings.extend(engine["findings"])
    except Exception as exc:  # noqa: BLE001 - 本机目录检查出错不能拖垮插件与脚手架检查
        findings.append({"id": "local:local-only-error", "kind": "error", "level": "suggest", "scope": "machine",
                         "title": _("无法检查本机专属目录"), "reason": str(exc), "impact": _("只影响同步盘相关提示"),
                         "fingerprint": sha256_text(str(exc)), "fix": None, "attention": False, "options": [],
                         "next": _("运行 agenthelm local-only check 查看详情")})
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

def baseline_less_differences(env: Env, project: Path | None) -> list[str]:
    """Project files that differ from the template with no baseline in the content lock (check's content-differs findings)."""
    try:
        report = scaffold_report(env, project)
    except Exception:  # noqa: BLE001 - a hint only; never let it break update
        return []
    return [item["path"] for item in (report or {}).get("findings", []) if item.get("kind") == "content-differs" and item.get("path")]


def scaffold_fingerprint(env: Env, scaffold: dict | None) -> dict | None:
    module = scaffold_module(env)
    if scaffold is None or module is None or not scaffold.get("available"):
        return None
    return module.fingerprint(scaffold)


CLIENT_NAMES = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"}
CLIENT_KEYS = ("claude", "codex", "cursor")


def ignored_clients(env: Env) -> list[str]:
    """The clients the user chose to ignore (user config ``ignored_clients``); unknown names are dropped."""
    try:
        value = read_json(env.config_file).get("ignored_clients", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [name for name in CLIENT_KEYS if isinstance(value, list) and name in value]


def select_clients(env: Env, explicit: list[str] | None) -> dict:
    """Which clients a run covers. ``--client`` wins over the ignore setting; without it, ignored ones are left out."""
    ignored = ignored_clients(env)
    if explicit:
        chosen = [name for name in CLIENT_KEYS if name in explicit]
        return {"explicit": chosen, "active": chosen, "ignored": [], "excluded": [n for n in CLIENT_KEYS if n not in chosen]}
    return {"explicit": None, "active": [n for n in CLIENT_KEYS if n not in ignored], "ignored": ignored, "excluded": []}


def active_clients(env: Env, explicit: list[str] | None = None) -> list[str]:
    """Detected clients minus ignored ones (an explicit ``--client`` list is used as given)."""
    if explicit:
        return list(explicit)
    return [name for name in detect_clients(env) if name not in ignored_clients(env)]


def unused_client_hints(env: Env, plugins: list[dict], ignored: list[str]) -> list[str]:
    """Detected clients that look unused: with several clients around, one that has no agenthelm plugin installed or
    whose agenthelm plugins are all behind. The caller shows each hint once."""
    detected = [name for name in detect_clients(env)]
    if len(detected) < 2:
        return []
    hints = []
    for name in detected:
        if name in ignored:
            continue
        mine = [item for item in plugins if item["client"] == name]
        if not mine or all(item["status"] == "behind" for item in mine):
            hints.append(name)
    return hints


def run_check(env: Env, project: Path | None, *, latest_source: str | None = None, offline: bool = False,
              max_age: int = CACHE_TTL_SECONDS, hook_mode: bool = False, reprompt: bool = False,
              clients: list[str] | None = None, source: Path | None = None) -> dict:
    latest, origin = load_latest(
        env, latest_source, offline=offline, max_age=max_age,
        timeout=HOOK_FETCH_TIMEOUT_SECONDS if hook_mode else FETCH_TIMEOUT_SECONDS, backoff=hook_mode,
    )
    names = set(latest.get("plugins", {}))
    installed = installed_plugins(env, names)
    selection = select_clients(env, clients)
    everything = plugin_findings(latest, installed)
    # A legacy copy belongs to no client: it stays visible whichever clients are selected.
    plugins = [item for item in everything if item["client"] == "legacy" or item["client"] in selection["active"]]
    ignored_plugins = [item for item in everything if item["client"] in selection["ignored"]]
    others = other_installs(env, names)
    state = install_state(env, latest, everything, others, declared_marketplaces(env, project), selection["active"])
    content = content_findings(project, latest) if project else []
    opinion = opinion_findings(project, installed) if project else []
    scaffold = scaffold_report(env, project, reprompt=reprompt)
    machine = machine_findings(env, project, reprompt=reprompt, quick=hook_mode)
    local_attention = sum(1 for item in machine["findings"] if item.get("attention"))
    scaffold_active = len(scaffold["findings"]) if scaffold else 0
    # 只有“需要处理”的脚手架发现（level must）参与 up_to_date、--strict 与会话开始提示；旧版 agents-init 没有分级时按可选处理。
    scaffold_numbers = scaffold_counts(scaffold)
    scaffold_required = scaffold_numbers["required"]
    behind = [item for item in plugins if item["status"] == "behind"]
    # Cursor is installed from a release tarball: when update would find none (no tag / digest, no --source) the user
    # cannot clear the finding, so it is listed as unavailable instead of counted as behind. Same decision as update.
    unavailable: list[dict] = []
    for item in plugins:
        item["unavailable"] = None
    if any(item["client"] == "cursor" for item in behind):
        reason = cursor_unavailable(latest, source, timeout=HOOK_FETCH_TIMEOUT_SECONDS if hook_mode else FETCH_TIMEOUT_SECONDS)
        for item in behind:
            if reason and item["client"] == "cursor":
                item["unavailable"] = reason
        unavailable = [item for item in behind if item.get("unavailable")]
        behind = [item for item in behind if not item.get("unavailable")]
    # With --client, a legacy entry stays listed (it is a migration risk) but counts toward up_to_date only when its
    # path says it belongs to a selected client; an entry whose owner cannot be told is listed and not counted.
    for item in plugins:
        if item["client"] == "legacy":
            item["owner"] = legacy_owner(env, item)
    legacy = [item for item in plugins if item["client"] == "legacy"
              and (not selection["explicit"] or item["owner"] in selection["active"])]
    legacy_unattributed = sum(1 for item in plugins if item["client"] == "legacy") - len(legacy)
    by_client = {name: {"behind": sum(1 for item in behind if item["client"] == name),
                        "unavailable": sum(1 for item in unavailable if item["client"] == name),
                        "total": sum(1 for item in plugins if item["client"] == name)}
                 for name in selection["active"] if any(item["client"] == name for item in plugins)}
    content_updates = [item for item in content if item["status"] in {"update-available", "conflict"}]
    content_errors = [item for item in content if item["status"] == "error"]  # 内容锁无法解析：无法判断，不能当作“已是最新”
    opinion_updates = [item for item in opinion if item["status"] == "update-available"]
    settings = user_settings_module()
    language = settings.report_section(env.variables, settings.read_settings(env.config_file)[0], scaffold)
    return {
        "ok": True,
        "latest": {"source": origin, "released_at": latest.get("released_at")},
        "_latest": latest,
        "plugins": plugins,
        "ignored_plugins": ignored_plugins,
        "install_state": state,
        "clients": {"selected": selection["explicit"], "ignored": selection["ignored"],
                    "excluded": selection["excluded"],
                    "unused": [] if (hook_mode or selection["explicit"])
                    else unused_client_hints(env, everything, selection["ignored"])},
        "content": content,
        "opinion": opinion,
        "scaffold": scaffold,
        "machine": machine,
        "language": language,
        "summary": {
            "scaffold_findings": scaffold_active,
            "scaffold_required": scaffold_required,
            "scaffold_optional": scaffold_numbers["optional"],
            "scaffold_info": scaffold_numbers["info"],
            "profile_notes": scaffold_numbers["profile_notes"],
            "scaffold_skipped": len(scaffold["skipped"]) if scaffold else 0,
            "local_only_attention": local_attention,
            "plugins_behind": len(behind),
            "plugins_unavailable": len(unavailable),
            "by_client": by_client,
            "breaking": any(item["breaking"] for item in behind),
            "content_updates": len(content_updates),
            "content_errors": len(content_errors),
            "content_conflicts": sum(1 for item in content if item["status"] == "conflict"),
            "opinion_updates": len(opinion_updates),
            "legacy_installs": len(legacy),
            "legacy_other_clients": legacy_unattributed,
            "install_states": {name: item["state"] for name, item in state["clients"].items()},
            "up_to_date": not (behind or content_updates or content_errors or opinion_updates or legacy or scaffold_required
                               or local_attention),
        },
    }


def update_hint(client: str | None) -> str | None:
    """The command that updates a client's plugins; built when asked so it follows the current ``CLI``."""
    return {
        "claude": "claude plugin marketplace update agenthelm && claude plugin update {plugin}@agenthelm",
        "codex": "codex plugin marketplace upgrade agenthelm",
        "cursor": f"{CLI} update",
        "legacy": f"{CLI} migrate --install",
    }.get(client or "")


def _client_state(name: str, item: dict) -> str:
    if item["behind"]:
        text = _("{0}：{1} 个可更新").format(CLIENT_NAMES[name], item["behind"])
    elif item.get("unavailable"):
        return _("{0}：{1} 个不可用，需 --source 本地源").format(CLIENT_NAMES[name], item["unavailable"])
    else:
        return _("{0}：已是最新").format(CLIENT_NAMES[name])
    if item.get("unavailable"):
        text += _("，另有 {0} 个不可用（需 --source 本地源）").format(item["unavailable"])
    return text


def profile_note_counts(scaffold: dict) -> tuple[int, int]:
    """(conflicting values, undeclared fields): the two kinds of Profile information shown as ``[脚手架·信息]`` lines."""
    return (len(scaffold.get("profile_conflicts") or []), int((scaffold.get("profile_extras") or {}).get("total") or 0))


def scaffold_counts(scaffold: dict | None) -> dict[str, int]:
    """The one place that counts scaffold items; ``summary`` (JSON) and the text both read it.
    ``info`` is everything the text prints as 脚手架·信息: info-level findings plus the Profile notes."""
    if not scaffold:
        return {"required": 0, "optional": 0, "info": 0, "profile_notes": 0}
    findings = scaffold.get("findings") or []
    notes = sum(profile_note_counts(scaffold))
    return {"required": sum(1 for item in findings if item.get("level") == "must"),
            "optional": sum(1 for item in findings if item.get("level", "suggest") == "suggest"),
            "info": sum(1 for item in findings if item.get("level") == "info") + notes,
            "profile_notes": notes}


def one_line(report: dict) -> str | None:
    summary = report["summary"]
    if summary["up_to_date"]:
        return None
    parts = []
    if summary["plugins_behind"]:
        text = _("{0} 个插件可更新").format(summary['plugins_behind']) + (_("（含不兼容变更）") if summary["breaking"] else "")
        per_client = summary.get("by_client") or {}
        if per_client:
            text += _("（") + _("；").join(_client_state(name, item) for name, item in per_client.items()) + _("）")
        parts.append(text)
    if summary["content_updates"]:
        parts.append(_("{0} 个模板/样例包可更新").format(summary['content_updates'])
                     + (_("（{0} 个本地有改动，更新时逐文件确认）").format(summary['content_conflicts']) if summary["content_conflicts"] else ""))
    if summary.get("content_errors"):
        parts.append(_("项目内容锁无法解析，没能检查模板/样例包"))
    if summary["opinion_updates"]:
        parts.append(_("Opinion 有新版本模板或规则"))
    if summary["legacy_installs"]:
        parts.append(_("{0} 个旧方式安装待迁移").format(summary['legacy_installs']))
    if summary["scaffold_required"]:
        parts.append(_("项目脚手架有 {0} 项需要处理的差异").format(summary['scaffold_required']))
    if summary.get("local_only_attention"):
        parts.append(_("同步目录里有 {0} 项本机专属目录需要处理").format(summary['local_only_attention']))
    return _("agenthelm：") + _("，").join(parts) + _("。运行 `{0} check` 查看详情；更新前会先预览，由你决定。").format(CLI)


def render_profile_notes(scaffold: dict, show_extras: bool) -> list[str]:
    """Information only (never a finding, never the exit code): values that no longer fit the schema, and fields it does not declare."""
    lines = []
    for item in scaffold.get("profile_conflicts") or []:
        lines.append(_("  [脚手架·信息] {0}").format(item["message"]))
    summary = dict(scaffold.get("profile_extras") or {}, total=profile_note_counts(scaffold)[1])
    if not summary.get("total"):
        return lines
    parts = [_("项目自有 {0}").format(summary["own"])]
    if summary.get("suspected_typo"):
        parts.append(_("疑似拼写错误 {0}").format(summary["suspected_typo"]))
    if summary.get("read_by_plugin"):
        parts.append(_("已被其他插件读取、schema 尚未声明 {0}").format(summary["read_by_plugin"]))
    lines.append(_("  [脚手架·信息] Profile 中有 {0} 个 schema 未声明的字段（{1}；工具不读取、不比较）{2}").format(
        summary["total"], _("，").join(parts), "" if show_extras else _("：check --show-extras 展开")))
    if show_extras:
        for item in scaffold.get("extras") or []:
            if item.get("kind") != "project-field":
                continue
            if item["category"] == "suspected-typo":
                note = _("疑似拼写错误，是不是想写 {0}？").format(item["similar_to"])
            elif item["category"] == "read-by-plugin":
                note = _("{0} 会读取它，但 schema 尚未声明").format(item["plugin"])
            else:
                note = _("项目自有")
            lines.append(_("      - {0}（{1}）").format(item['path'], note))
    return lines


def level_tag(level: str) -> str:
    return {"must": _("需要处理"), "suggest": _("可选"), "info": _("信息")}.get(level, level)


CATEGORY_COMMANDS = {
    "skeleton": ("fix add-skeleton", None), "content": ("fix update-content", None), "region": ("fix update-region", None),
    "profile": ("fix profile", None), "case-naming": ("fix case-naming", None), "lock": ("fix repair-lock", None),
}


def _scaffold_item_lines(item: dict) -> str:
    mark = _("（稍后）") if item.get("deferred") else _("（期望内容已变化，重新提示）") if item.get("changed_since_decision") else ""
    fix = _("  处理：fix {0}").format(item['fix']) if item.get("fix") else _("  需要你自己处理")
    return _("  [脚手架] {0}{1}：{2}\n      ID {3}{4}").format(item['title'], mark, item['reason'], item['id'], fix)


def render_scaffold_groups(scaffold: dict, details: bool) -> list[str]:
    """需要处理的逐项列出；可选与信息性的按类别各合并一行（--details 时逐项展开）。"""
    lines = []
    groups = {(row["level"], row["category"]): row for row in scaffold.get("categories", [])}
    by_key: dict[tuple[str, str], list[dict]] = {}
    for item in scaffold["findings"]:
        by_key.setdefault((item.get("level", "suggest"), item.get("category") or item["kind"]), []).append(item)
    for key, members in by_key.items():
        level, category = key
        if level == "must" or details or key not in groups:
            lines += [_scaffold_item_lines(item) for item in members]
            continue
        verb = CATEGORY_COMMANDS.get(category)
        handle = _("；处理：{0}").format(f"{CLI} {verb[0]}") if verb else ""
        lines.append(_("  [脚手架·{0}] {1}（查看：{2} check --details{3}）").format(
            level_tag(level), groups[key]["label"], CLI, handle))
    optional = sum(len(m) for k, m in by_key.items() if k[0] != "must")
    if optional:
        lines.append(_("  这 {0} 项都不需要？一次接受现状：{1} fix --accept-current --reason \"<原因>\"（先预览，--apply 写入；之后只提示新出现的差异）").format(optional, CLI))
    return lines


def render_scaffold(report: dict, show_skipped: bool, details: bool = False, show_extras: bool = False) -> list[str]:
    scaffold = report.get("scaffold")
    lines = []
    if scaffold is None:
        return lines
    if scaffold.get("error"):
        lines.append(_("  [脚手架] 无法检查：{0}").format(scaffold['error']))
        return lines
    lines += render_scaffold_groups(scaffold, details)
    for message in scaffold["decisions"]["errors"]:
        lines.append(_("  [脚手架] 决定文件有问题：{0}").format(message))
    if scaffold["decisions"]["conflict_copies"]:
        lines.append(_("  [脚手架] 同步服务留下了决定文件的冲突副本：")
                     + _("、").join(scaffold["decisions"]["conflict_copies"]) + _("（合并到 check-decisions.json 后删除）"))
    if not scaffold["findings"] and not scaffold["decisions"]["errors"]:
        lines.append(_("  [脚手架] 与期望一致") + (_("（{0} 项已跳过）").format(len(scaffold['skipped'])) if scaffold["skipped"] else ""))
    lines += render_profile_notes(scaffold, show_extras)
    skipped = [*scaffold["skipped"], *report.get("machine", {}).get("skipped", [])]
    if show_skipped:
        for item in skipped:
            decision = item["decision"]
            lines.append(_("  [已跳过·{0}] {1}：{2}  {3}").format(decision['decision'], item['id'], decision.get('reason') or _('（未写理由）'), decision.get('decided_at', '')))
        if not skipped:
            lines.append(_("  没有被跳过的项。"))
    elif skipped:
        lines.append(_("  （另有 {0} 项已被你跳过：check --show-skipped 查看）").format(len(skipped)))
    return lines


def render_machine(report: dict) -> list[str]:
    lines = []
    for item in report.get("machine", {}).get("findings", []):
        lines.append(_("  [本机] {0}：{1}\n      影响：{2}\n      处理：{3}\n      ID {4}（只记在这台机器上；fix --skip {5} --reason …）").format(item['title'], item['reason'], item['impact'], item['next'], item['id'], item['id']))
    return lines


def render_install_state(state: dict) -> list[str]:
    """The install-state diagnosis at the head of ``check``: a conclusion first, then the next command and its reach."""
    clients = state.get("clients") or {}
    if not clients:
        return []
    lines = [_("安装状态：")]
    for name, diag in clients.items():
        label = _(STATE_LABELS[diag["state"]])
        if diag["state"] == "marketplace" and diag.get("sub_status"):
            label += "·" + {"current": _("最新"), "behind": _("落后"), "ahead": _("高于云端"), "unknown": _("版本未知")}[diag["sub_status"]]
        lines.append(f"  [{CLIENT_NAMES[name]}·{label}] {diag['summary']}")
        for conflict in diag["conflicts"]:
            for record in [r for r in diag["installs"] if r["plugin"] == conflict["plugin"]]:
                lines.append(_("      {0} {1}：{2} {3}（{4}）").format(
                    record["plugin"], _("旧方式安装") if record["kind"] == "legacy" else _origin_text(record),
                    record.get("version"), record["path"], record["scope"] or "-"))
        if diag["next_command"]:
            lines.append(_("      下一步（预览）：{0}").format(diag["next_command"]))
            lines.append(f"      {diag['modifies_note']}")
        if diag["recovery"]:
            lines.append(_("      原件恢复位置：{0}").format(diag["recovery"]))
    return lines


def render_text(report: dict, *, show_skipped: bool = False, details: bool = False, show_extras: bool = False) -> str:
    lines = []
    origin = report["latest"]["source"]
    lines.append(origin_line(origin))
    clients = report.get("clients") or {}
    if clients.get("selected"):
        lines.append(_("只处理所选客户端：{0}").format(_("、").join(CLIENT_NAMES[name] for name in clients["selected"])))
    for name in clients.get("ignored", []):
        shown = [item for item in report.get("ignored_plugins", []) if item["client"] == name]
        lines.append(_("  [已忽略] {0}（用户配置 ignored_clients；不参与检查与更新）").format(CLIENT_NAMES[name]))
        if show_skipped:
            for item in shown:
                lines.append(_("      {0} {1} → {2}（未检查）").format(item["plugin"], item.get("version"), item.get("latest")))
    lines += render_install_state(report.get("install_state") or {})
    if not report["plugins"]:
        lines.append(_("未发现已安装的 agenthelm 插件。"))
    shown_breaking: dict[tuple[str, str], str] = {}  # (plugin, target version) -> client whose line carries the notes
    for item in report["plugins"]:
        label = {"behind": _("可更新"), "current": _("最新"), "ahead": _("高于云端"), "unknown": _("版本未知"),
                 "legacy": _("旧方式安装，待迁移"), "dev": _("开发版/本地安装，不会被 update / migrate 改动")}[item["status"]]
        if item["status"] == "legacy":
            lines.append(_("  [旧方式] {0} {1}  {2}").format(item['plugin'], item.get('version'), label))
            continue
        if item["status"] == "dev":
            lines.append(_("  [{0}] {1} {2}  {3}（{4}）").format(item['client'], item['plugin'], item.get('version'), label, item['path']))
            continue
        if item.get("unavailable"):
            label = _("不可用（需 --source 本地源，不计入“可更新”）")
        line = f"  [{item['client']}] {item['plugin']} {item.get('version')} → {item.get('latest')}  {label}"
        if item["status"] == "behind":
            kinds = {"code": _("代码"), "content": _("仅模板/样例"), "both": _("代码与模板")}
            if item["change_kinds"]:
                line += _("（") + _("、").join(kinds[kind] for kind in item["change_kinds"]) + _("）")
            if item["breaking"]:
                line += _("（含不兼容变更）")
            for step in item["steps"]:
                if step.get("breaking"):
                    key = (item["plugin"], str(step["to"]))
                    if key in shown_breaking:  # same plugin, same step: print the notes once, on the first client
                        line += _("\n      {0} 不兼容：同上（见 {1}）").format(step['to'], shown_breaking[key])
                        continue
                    shown_breaking[key] = item["client"]
                    line += _("\n      {0} 不兼容：{1}").format(step['to'], step.get('summary'))
                    line += "".join(f"\n        - {action}" for action in step.get("actions") or [])
            hint = update_hint(item["client"])
            if item.get("unavailable"):
                line += _("\n      {0}\n      有本地仓库时：{1} update --source <本地仓库>；不使用 Cursor 时：{1} config ignore-client cursor --apply").format(
                    item["unavailable"], CLI)
            elif hint:
                line += _("\n      更新：{0}").format(hint.format(plugin=item['plugin']))
        lines.append(line)
    for item in report["content"]:
        if item.get("pack") is None:
            lines.append(_("  内容锁：{0}").format(item.get('error')))
            continue
        label = {"current": _("最新"), "update-available": _("可更新（本地未改动）"),
                 "conflict": _("可更新（本地有改动，更新时逐文件确认）"), "unknown": _("云端未列出")}[item["status"]]
        line = _("  [内容] {0} {1} → {2}  {3}").format(item['pack'], item['version'], item.get('latest'), label)
        if item["locally_changed"]:
            line += _("\n      本地已改动：") + _("、").join(item["locally_changed"])
        lines.append(line)
    for item in report["opinion"]:
        if item["status"] == "update-available":
            refs = _("；").join(f"{update['current']} → {update['available'][-1]}" for update in item["updates"])
            lines.append(_("  [Opinion] 有新版本：{0}（用 opinion-manager 预览后确认）").format(refs))
        elif item["status"] in {"error", "unknown"}:
            lines.append(f"  [Opinion] {item.get('error')}")
    lines += render_scaffold(report, show_skipped, details, show_extras)
    lines += render_machine(report)
    lines += [_("  [语言] {0}").format(hint) for hint in (report.get("language") or {}).get("hints", [])]
    if not report["plugins"]:
        lines.append(_("运行 `{0} install` 预览安装。").format(CLI))
    else:
        if any(item["status"] == "legacy" for item in report["plugins"]):
            lines.append(_("旧方式安装可用 `{0}` 预览迁移。").format(update_hint('legacy')))
        if report["summary"].get("legacy_other_clients"):
            lines.append(_("其中 {0} 个旧方式安装不属于所选客户端（或无法判断归属），已列出但未计入“是否最新”。").format(
                report["summary"]["legacy_other_clients"]))
        lines.append(one_line(report) or _("全部为最新。"))
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


# ---------------------------------------------------------------- last hook error (diagnosability)

HOOK_ERROR_LIMIT = 5
HOOK_ERROR_TEXT_CHARS = 160


def hook_error_file(env: Env) -> Path:
    return env.state_dir / "agenthelm" / "hook-errors.json"


def summarize_error(env: Env, error: BaseException | str) -> tuple[str, str]:
    """Exception type and a one-line, truncated message; the home directory is shortened to ``~``. Never user input."""
    kind = type(error).__name__ if isinstance(error, BaseException) else "Error"
    text = str(error) if error is not None else ""
    text = next((line.strip() for line in text.splitlines() if line.strip()), "")
    text = text.replace(str(env.home), "~")
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return kind, text[:HOOK_ERROR_TEXT_CHARS]


def record_hook_error(env: Env, hook_name: str, error: BaseException | str) -> None:
    """Remember the latest hook failures (newest last, at most HOOK_ERROR_LIMIT) in the user state dir.
    Hooks must stay silent, so every failure here is swallowed too."""
    try:
        kind, message = summarize_error(env, error)
        path = hook_error_file(env)
        try:
            data = read_json(path)
            entries = data.get("errors") if isinstance(data, dict) else []
            entries = [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []
        except (OSError, ValueError):
            entries = []
        entries.append({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "hook": str(hook_name)[:40],
                        "type": kind[:60], "message": message})
        write_json_atomic(path, {"errors": entries[-HOOK_ERROR_LIMIT:]})
    except Exception:  # noqa: BLE001 - recording a failure must never itself fail a session
        pass


def read_hook_errors(env: Env) -> list[dict]:
    try:
        data = read_json(hook_error_file(env))
    except (OSError, ValueError):
        return []
    entries = data.get("errors") if isinstance(data, dict) else None
    return [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []


def last_hook_error(env: Env) -> dict | None:
    """Stable shape for status/doctor JSON: {at, hook, type, message, count} or None."""
    entries = read_hook_errors(env)
    if not entries:
        return None
    last = entries[-1]
    return {"at": str(last.get("at", "")), "hook": str(last.get("hook", "")), "type": str(last.get("type", "")),
            "message": str(last.get("message", "")), "count": len(entries)}


def clear_hook_errors(env: Env) -> bool:
    try:
        hook_error_file(env).unlink()
        return True
    except OSError:
        return False


def hook(env: Env, project: Path | None, output: str) -> int:
    """Session-start entry: silent unless something is behind; never fails the session."""
    if not update_check_enabled(env):
        return 0
    try:
        report = run_check(env, project, hook_mode=True)
    except Exception as exc:  # noqa: BLE001 - an update notice must never break a session
        record_hook_error(env, "update-check", exc)
        return 0
    message = "\n".join(part for part in (one_line(report), (report.get("language") or {}).get("session_hint")) if part) or None
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
        raise CheckError(_("latest.json 的 repository 不是 GitHub 仓库：{0}").format(latest.get('repository')))
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
                raise CheckError(_("发布包含越界路径：{0}").format(member.name))
            if not (member.isfile() or member.isdir()):
                raise CheckError(_("发布包含不支持的条目：{0}").format(member.name))
        try:
            bundle.extractall(destination, filter="data")  # noqa: S202 - members validated above
        except TypeError:  # Python without extraction filters
            bundle.extractall(destination)  # noqa: S202


def _plugin_versions(latest: dict) -> dict:
    return {name: item.get("version") for name, item in latest.get("plugins", {}).items()}


def github_release(latest: dict, timeout: float = FETCH_TIMEOUT_SECONDS) -> dict:
    """Release details straight from GitHub, used when latest.json came from the raw fallback.

    Accepted only when that tag ships exactly the plugin versions ``latest`` lists,
    the same rule the site applies before attaching a release.
    """
    try:
        slug = github_slug(latest)
        release = _fetch_json(f"https://github.com/{slug}/releases/latest/download/release.json", timeout)
        tagged = _fetch_json(f"https://raw.githubusercontent.com/{slug}/{release['tag']}/latest.json", timeout)
    except (OSError, ValueError, KeyError, TypeError, CheckError, urllib.error.URLError):
        return {}
    return release if _plugin_versions(tagged) == _plugin_versions(latest) else {}


def release_problem(release: dict) -> str | None:
    """Why the release cannot be downloaded (None when it can). The one rule behind Cursor's “unavailable”:
    ``update`` / ``install`` raise it as :class:`Unavailable`, ``check`` uses it to keep Cursor out of the “behind” count."""
    if release.get("tag") and release.get("tarball_sha256"):
        return None
    return _("云端 latest.json 没有发布 tag 与摘要；请用 --source 指定本地仓库")


def cursor_unavailable(latest: dict, source: Path | None = None, *, timeout: float = FETCH_TIMEOUT_SECONDS) -> str | None:
    """Why Cursor cannot be updated on this run (None when it can): the same decision :func:`release_source` makes.

    A local ``--source`` is always usable (it is validated when the update runs); otherwise the release needs a tag
    and a digest, from latest.json or, failing that, from GitHub."""
    if source is not None:
        return None
    return release_problem(latest.get("release") or github_release(latest, timeout))


def release_source(latest: dict, workdir: Path, source: Path | None = None) -> Path:
    """A directory with plugins/<name>: a local checkout, or the tagged release tarball."""
    if source is not None:
        root = source.expanduser().resolve()
        if not (root / "plugins").is_dir():
            raise CheckError(_("{0} 不含 plugins/ 目录").format(root))
        return root
    release = latest.get("release") or github_release(latest)
    tag, digest = release.get("tag"), release.get("tarball_sha256")
    problem = release_problem(release)
    if problem:
        raise Unavailable(problem)
    url = release.get("tarball_url") or f"https://github.com/{github_slug(latest)}/releases/download/{tag}/agenthelm-{tag}.tar.gz"
    archive = workdir / "release.tar.gz"
    request = urllib.request.Request(url, headers={"User-Agent": "agenthelm"})
    with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as handle:  # noqa: S310
        shutil.copyfileobj(response, handle)
    if sha256_file(archive) != digest:
        raise CheckError(_("发布包摘要不符：{0}").format(url))
    extracted = workdir / "release"
    extracted.mkdir()
    _safe_extract(archive, extracted)
    roots = [path for path in extracted.iterdir() if path.is_dir()]
    if len(roots) != 1 or not (roots[0] / "plugins").is_dir():
        raise CheckError(_("发布包结构不符合预期"))
    return roots[0]


def _agent_pack_config():
    sys.path.insert(0, str(RUNTIME_DIR))
    import agent_pack_config  # noqa: PLC0415 - shipped next to this file

    return agent_pack_config


@dataclass
class Step:
    """One planned action; ``command`` runs a client CLI, ``action`` runs in-process.

    ``verify`` lists the plugins whose installed version is read back before and after the step (``targets`` holds
    the version each is expected to reach); ``skipped`` marks a step that has nothing to do and why.
    """

    kind: str
    client: str | None
    description: str
    command: list[str] | None = None
    action: Any = None
    blocked: str | None = None
    verify: list[str] | None = None
    targets: dict[str, str | None] = field(default_factory=dict)
    scope: str | None = None
    skipped: str | None = None
    note: str | None = None
    untouched: bool = False  # an install the tool recognises but leaves alone (development / local)
    pinned: str | None = None  # the ref Claude's marketplace declaration is pinned to (updates only follow it)
    blocker: str | None = None  # machine-readable cause of ``blocked`` that the user has to resolve (not a missing CLI)
    next: str | None = None

    def view(self) -> dict:
        data = {"kind": self.kind, "client": self.client, "description": self.description}
        if self.command:
            data["command"] = " ".join(self.command)
        if self.blocked:
            data["blocked"] = self.blocked
        if self.blocker:
            data["blocker"] = self.blocker
            data["next"] = self.next
        if self.skipped:
            data["skipped"] = self.skipped
        if self.note:
            data["note"] = self.note
        if self.untouched:
            data["untouched"] = True
        return data


def untouched_steps(env: Env, names: set[str], clients: list[str], kind: str = "update") -> list[Step]:
    """Steps that only say "this install is not touched": development / local installs of agenthelm plugins."""
    steps = []
    found = [item for item in installed_plugins(env, names) if item.get("dev")] + other_installs(env, names)
    for item in found:
        if item["client"] not in clients:
            continue
        source = item.get("marketplace")
        origin = _origin_text(_install_record(item))
        steps.append(Step(kind, item["client"], _("{0} {1} {2}（{3}：{4}）").format(
            CLIENT_NAMES[item["client"]], item["plugin"], item.get("version") or "?", origin, item["path"]) + (f" [{source}]" if source else ""),
            skipped=_("不会改动：开发版/本地安装默认不被 update / migrate 改动；想纳入市场管理，请自行卸载这处安装后运行 `{0} install`").format(CLI),
            untouched=True))
    return steps


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
    return [found or name, *args], (None if found else _("未找到 {0} 命令（已查 PATH 与常见安装位置；可用 AGENTHELM_{1} 指定）").format(name, name.upper()))


def installed_versions(env: Env, client: str | None, plugins: list[str], scope: str | None = None) -> dict[str, str | None]:
    """The version of each plugin installed in ``client`` right now (None: not installed or unreadable)."""
    found: dict[str, str | None] = {}
    for item in installed_plugins(env, set(plugins)):
        if item["client"] != client or (scope and item.get("scope") and item["scope"] != scope):
            continue
        kept = found.get(item["plugin"])
        if item["plugin"] not in found or (version_key(item.get("version") or "") or (0,)) > (version_key(kept or "") or (0,)):
            found[item["plugin"]] = item.get("version")
    return {plugin: found.get(plugin) for plugin in plugins}


def already_installed_reason(version: str | None, target: str | None) -> str:
    if version and target and is_newer(target, version):
        return _("已安装 {0}，云端是 {1}：install 不会升级已安装的插件，请用 `{2} update`").format(version, target, CLI)
    if version and target and is_newer(version, target):
        return _("已安装 {0}（高于云端的 {1}）：install 不会改动已安装的插件").format(version, target)
    return _("已安装 {0}，已是最新：install 不会改动已安装的插件").format(version)


def install_steps(env: Env, latest: dict, clients: list[str], plugins: list[str], source: Path | None,
                  home: Path, marketplace_source: str | None = None) -> list[Step]:
    slug = marketplace_source or github_slug(latest)
    targets = {plugin: latest.get("plugins", {}).get(plugin, {}).get("version") for plugin in plugins}
    steps: list[Step] = []
    foreign = {(item["client"], item["plugin"]) for item in other_installs(env, set(plugins))}
    for client in clients:
        present = installed_versions(env, client, plugins)
        if client in ("claude", "codex"):
            name = CLIENT_NAMES[client]
            command, blocked = _cli(env, client, "plugin", "marketplace", "add", slug)
            steps.append(Step("install", client, _("{0} 添加 agenthelm 市场（{1}）").format(name, slug), command, blocked=blocked))
            for plugin in plugins:
                add = ("plugin", "install") if client == "claude" else ("plugin", "add")
                command, blocked = _cli(env, client, *add, f"{plugin}@{MARKETPLACE}")
                keep_foreign = (client, plugin) in foreign and not present[plugin]  # nothing runs, so a missing CLI is irrelevant
                steps.append(Step("install", client, _("{0} 安装 {1}").format(name, plugin), command,
                                  blocked=None if keep_foreign else blocked,
                                  verify=[plugin], targets={plugin: targets[plugin]},
                                  skipped=(already_installed_reason(present[plugin], targets[plugin]) if present[plugin] else
                                           _("不会改动：已有来自其他市场的开发版/本地安装，不再重复安装（想改用 agenthelm 市场，请自行卸载后重试）")
                                           if (client, plugin) in foreign else None),
                                  untouched=keep_foreign))
        elif client == "cursor":
            steps.append(cursor_step(latest, plugins, source, home, _("安装"), env=env))
    return steps


def cursor_step(latest: dict, plugins: list[str], source: Path | None, home: Path, verb: str,
                env: Env | None = None) -> Step:
    def run(apply: bool, recovery_root: Path | None) -> list[dict]:
        config = _agent_pack_config()
        with tempfile.TemporaryDirectory(prefix="agenthelm-") as temporary:
            root = release_source(latest, Path(temporary), source)
            return [config.install_plugin(root / "plugins" / plugin, "cursor", home=home, apply=apply,
                                          replace=True, recovery_root=recovery_root)
                    for plugin in plugins]
    note = None
    if env is not None:
        present = {plugin: version for plugin, version in installed_versions(env, "cursor", plugins).items() if version}
        if present:
            note = _("已安装 {0}；会用发布包里的版本覆盖它们").format(
                _("、").join(f"{plugin} {version}" for plugin, version in present.items()))
    return Step("install", "cursor", _("Cursor {0} {1} 到 ~/.cursor/plugins/local（重启 Cursor 生效）").format(verb, _('、').join(plugins)),
                action=run, verify=list(plugins),
                targets={plugin: latest.get("plugins", {}).get(plugin, {}).get("version") for plugin in plugins}, note=note)


def claude_market_sync(env: Env, report: dict, project: Path | None, only: str | None) -> dict | None:
    """A1: the market problem that would make Claude's update steps fail, found before anything runs."""
    if only not in (None, "plugins") or not any(item["status"] == "behind" and item["client"] == "claude"
                                                 for item in report["plugins"]):
        return None
    return market_sync_problem(env, declared_marketplaces(env, project))


def codex_pin_advice(latest: dict) -> str:
    """A9: pin Codex's agenthelm marketplace to the release. ``--ref`` and ``owner/repo@ref`` are in the Codex plugin docs
    (developers.openai.com/codex/plugins/build); whether ``add`` replaces an existing same-named market is unconfirmed."""
    try:
        slug = github_slug(latest)
    except CheckError:
        return _("固定 Codex 市场的方法见 INSTALL.md 第 4 节")
    tag = release_tag(latest) or "<tag>"
    return _("codex plugin marketplace add {0} --ref {1}（若提示该市场已存在，先 codex plugin marketplace remove {2}；"
             "移除对已装插件的影响未确认）；详见 INSTALL.md 第 4 节").format(slug, tag, MARKETPLACE)


def update_steps(env: Env, report: dict, project: Path | None, source: Path | None, home: Path,
                 accept: list[str], keep: list[str], recovery_dir: Path | None,
                 only: str | None, *, content_flags: list[str] | None = None) -> list[Step]:
    latest = report["_latest"]
    steps: list[Step] = []
    behind = [item for item in report["plugins"] if item["status"] == "behind"]
    if only in (None, "plugins"):
        claude = sorted({item["plugin"] for item in behind if item["client"] == "claude"})
        if claude:
            command, blocked = _cli(env, "claude", "plugin", "marketplace", "update", MARKETPLACE)
            pin = ((report.get("install_state") or {}).get("clients", {}).get("claude") or {}).get("market_conflict")
            pinned = str(pin["ref"]) if pin and pin.get("reason") == "pinned" and pin.get("ref") else None
            note = None
            if pinned:
                newest = max((entry["latest"] for entry in behind if entry["client"] == "claude" and entry.get("latest")),
                             key=lambda text: version_key(text) or (0,), default="?")
                note = _("提示：Claude 的 settings 把 agenthelm 市场固定在 {0}，更新只会跟随这个 ref，版本可能不会变（最新版本 {1}）。"
                         "要让它跟随新版本：把 settings.json 里 agenthelm 市场声明的 ref 改成目标 tag，或去掉 ref 跟随 main（agenthelm 不会替你改 settings.json）。"
                         ).format(pinned, newest)
                if release_tag(latest):
                    note += _("目标 tag：{0}。").format(release_tag(latest))
            sync = report.get("market_sync")
            block = dict(blocked=sync["reason"], blocker="market-not-synced", next=sync["next"]) if sync else {}
            steps.append(Step("update", "claude", _("刷新 Claude Code 的 agenthelm 市场"), command, **{"blocked": blocked, **block},
                              note=note, pinned=pinned))
            for item in [entry for entry in behind if entry["client"] == "claude"]:
                extra = ["--scope", item["scope"]] if item.get("scope") and item["scope"] != "user" else []
                command, blocked = _cli(env, "claude", "plugin", "update", f"{item['plugin']}@{MARKETPLACE}", *extra)
                steps.append(Step("update", "claude", _("Claude Code 更新 {0} → {1}").format(item['plugin'], item['latest']),
                                  command, verify=[item["plugin"]], **{"blocked": blocked, **block},
                                  targets={item["plugin"]: item["latest"]}, scope=item.get("scope"), pinned=pinned))
        codex = [item for item in behind if item["client"] == "codex"]
        if codex:
            command, blocked = _cli(env, "codex", "plugin", "marketplace", "upgrade", MARKETPLACE)
            steps.append(Step("update", "codex", _("Codex 升级 agenthelm 市场与已装插件"), command, blocked=blocked,
                              verify=sorted({item["plugin"] for item in codex}),
                              targets={item["plugin"]: item["latest"] for item in codex}))
        cursor = sorted({item["plugin"] for item in behind if item["client"] == "cursor"})
        if cursor:
            steps.append(cursor_step(latest, cursor, source, home, _("更新"), env=env))
            steps[-1].targets = {item["plugin"]: item["latest"] for item in behind if item["client"] == "cursor"}
    if only in (None, "plugins"):
        steps += untouched_steps(env, set(latest.get("plugins", {})), list((report.get("install_state") or {}).get("clients", {})))
    if only in (None, "content") and project is not None:
        # 用户点名了文件（--merge / --diff / --keep …）时，即使内容锁的版本已是最新（例如先 --keep 过，锁已升到新版）
        # 也要把这些文件交给 refresh 处理；否则 --keep 之后再 --merge 只会得到“没有需要更新的内容”。
        named = bool(accept or keep or content_flags)
        for item in report["content"]:
            if item.get("status") not in {"update-available", "conflict"} and not (named and item.get("status") in {"current", "unknown"}):
                continue
            steps.append(content_step(env, item, report, project, accept, keep, recovery_dir, content_flags=content_flags))
    return steps


def content_companion(env: Env, report: dict) -> tuple[Path | None, list[dict]]:
    """Use the same client selection for planning and execution, retaining an independent runtime sibling."""
    installed = installed_plugins(env, {"agents-init"})
    selection = report.get("clients") or select_clients(env, None)
    selected = selection.get("selected") or selection.get("explicit")
    excluded = set(selection.get("ignored") or []) | set(selection.get("excluded") or [])
    available = []
    for entry in installed:
        owner = legacy_owner(env, entry) if entry["client"] == "legacy" else entry["client"]
        if owner is None or (owner not in excluded and (not selected or owner in selected)):
            available.append(entry)
    # A sibling can itself be an ignored client install (e.g. Cursor's plugins/local).
    # Only independent runtime siblings bypass client selection.
    excluded_paths = {Path(entry["path"]) for entry in installed} - {Path(entry["path"]) for entry in available}
    return locate_companion("agents-init", available, excluded=excluded_paths), available


def content_step(env: Env, item: dict, report: dict, project: Path, accept: list[str], keep: list[str],
                 recovery_dir: Path | None, *, content_flags: list[str] | None = None) -> Step:
    if item["plugin"] != "agents-init":
        return Step("content", None, _("{0}：没有可用的刷新工具").format(item['pack']), blocked="unsupported")
    # Use the same newest available copy that actually runs refresh. A stale copy in
    # another client (including an unavailable Cursor install) must not veto it.
    manager, installed = content_companion(env, report)
    required = report["_latest"].get("plugins", {}).get("agents-init", {}).get("version")
    current = _manifest_version(manager) if manager is not None else None
    stale = bool(manager is not None and required and (not current or is_newer(required, current)))
    description = _("重新渲染 {0} {1} → {2}").format(item['pack'], item['version'], item['latest']) \
        if item.get("status") in {"update-available", "conflict"} else _("按当前模板处理指定的文件：{0} {1}").format(item['pack'], item['version'])

    def run(apply: bool, _recovery_root: Path | None) -> dict:
        manager, _installed = content_companion(env, report)
        if manager is None:
            raise CheckError(_("未找到已安装的 agents-init"))
        command = [sys.executable, str(manager / "skills/agents-init/scripts/init_project.py"),
                   "--project", str(project), "--mode", "refresh", "--output", "json"]
        for path in accept:
            command += ["--accept", path]
        for path in keep:
            command += ["--keep", path]
        command += content_flags or []  # --append / --confirm-discard / --diff
        if recovery_dir is not None:
            command += ["--recovery-dir", str(recovery_dir)]
        if apply:
            command.append("--apply")
        completed = subprocess.run(command, capture_output=True, text=True, check=False,
                                   env=env.child_environment())
        try:
            outcome = json.loads(completed.stdout)
        except ValueError as exc:
            raise CheckError((completed.stderr or completed.stdout).strip()[:300]) from exc
        if completed.returncode not in (0, 1) and isinstance(outcome, dict) and outcome.get("error"):
            # Exit 1 is a normal "blocked, choose per file"; anything else (e.g. no usable backup folder) is a failure.
            raise CheckError(str(outcome["error"]))
        return outcome

    blocked, advice, note = None, None, None
    older = [entry for entry in report["plugins"]
             if entry["plugin"] == "agents-init" and entry["status"] in {"behind", "unavailable"}]
    if stale:
        owner = next((entry["client"] for entry in installed if Path(entry["path"]) == manager), None)
        client = CLIENT_NAMES.get(owner, _("随运行时提供的副本"))
        updatable = [entry["client"] for entry in installed if entry["client"] in {"claude", "codex"}]
        update_client = owner if owner in updatable else next(iter(updatable), None)
        advice = (f"{CLI} update --only plugins --client {update_client}" if update_client
                  else f"{CLI} runtime update --apply")
        blocked = _("先更新 agents-init：实际选用 {0} {1}（{2}），需要 {3}；下一步：{4}").format(
            client, current or "?", manager, required, advice)
    elif older:
        copies = _("、").join(f"{CLIENT_NAMES.get(entry['client'], entry['client'])} {entry.get('version') or '?'}"
                              for entry in older)
        note = _("其他客户端的 agents-init 较旧：{0}；建议分别升级；内容刷新使用 {1}（{2}），不受这些副本阻塞。").format(
            copies, current or "?", manager)
    return Step("content", None, description, action=run, blocked=blocked, note=note,
                blocker="outdated-agents-init" if stale else None, next=advice)


STDERR_LINES = 20

# Failure texts the client CLIs are known to print: (pattern, what it means, what to do next).
KNOWN_FAILURES = (
    (re.compile(r"network source differs|differs from the one declared", re.I),
     N_("市场来源与 settings.json 里的声明不一致：Claude Code 要求这个市场名下的来源，与 settings 里 extraKnownMarketplaces 为它声明的来源一致。"),
     N_("把 --marketplace-source 改成与 ~/.claude/settings.json 里 extraKnownMarketplaces.agenthelm 声明相同的来源（含 ref），"
        "或先修改那处声明，再重试。")),
)


def explain_failure(stderr: str) -> tuple[str, str] | None:
    for pattern, reason, advice in KNOWN_FAILURES:
        if pattern.search(stderr):
            return _(reason), _(advice)
    return None


def tail_lines(text: str, limit: int = STDERR_LINES) -> str:
    lines = text.strip().splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    return _("…（前面省略 {0} 行）").format(len(lines) - limit) + "\n" + "\n".join(lines[-limit:])


UNCHANGED_REASONS = {
    "claude": N_("该客户端的市场条目 version 没有变（Claude Code 只看市场条目的 version 判断是否有更新），或市场没有固定到这个版本"),
    "codex": N_("该客户端的市场没有固定到此版本（可能在跟随默认分支，升级到的是那里的版本）"),
    "cursor": N_("发布包里的版本与已装的相同，文件已按发布包重写"),
}


def verify_step(step: Step, record: dict, before: dict, after: dict) -> None:
    """Compare the versions read back around a step that ran fine; say whether it took effect and why not."""
    changes = []
    for plugin in step.verify or []:
        target = step.targets.get(plugin)
        entry = {"plugin": plugin, "before": before.get(plugin), "after": after.get(plugin), "target": target}
        entry["changed"] = entry["before"] != entry["after"]
        changes.append(entry)
    record["changes"] = changes
    if len(changes) == 1:
        record["before"], record["after"] = changes[0]["before"], changes[0]["after"]
    unchanged = [entry for entry in changes if not entry["changed"]]
    if not unchanged:
        return
    record["status"] = "unchanged"
    if all(entry["after"] is None for entry in unchanged):
        record["reason"] = _("命令成功，但读不到该客户端里这个插件的版本，无法确认是否生效")
    elif all(entry["target"] and entry["after"] == entry["target"] for entry in unchanged):
        record["reason"] = _("已经是目标版本")
    elif step.pinned:
        record["reason"] = _("Claude 的 settings 把 agenthelm 市场固定在 {0}，更新只会跟随这个 ref，所以版本没变；"
                             "把声明里的 ref 改成目标 tag，或去掉 ref 跟随 main。").format(step.pinned)
    else:
        record["reason"] = _(UNCHANGED_REASONS.get(step.client or "", N_("版本没有变化，原因未知")))


def execute_steps(steps: list[Step], *, apply: bool, recovery_root: Path | None,
                  child_env: dict[str, str] | None = None, env: Env | None = None) -> list[dict]:
    """Run (or preview) the steps. After a failure the same client's remaining steps are not run, other clients' are.

    With ``env`` the installed versions of the plugins a step changes are read back around it, so ``applied`` means the
    version really moved and ``unchanged`` means the command succeeded but nothing changed.
    """
    results = []
    failed_clients: dict[str | None, str] = {}
    for step in steps:
        record = step.view()
        if apply and step.client is not None and step.client in failed_clients and not step.blocked:
            record["status"] = "not_run"
            record["reason"] = _("同一客户端前面的步骤失败（{0}），这一步没有执行").format(failed_clients[step.client])
        elif step.skipped:
            # Nothing would run, so a missing client command is irrelevant: skipped wins over blocked.
            record["status"] = "skipped"
            record.pop("blocked", None)
        elif step.blocked:
            record["status"] = "blocked"
        else:
            before = installed_versions(env, step.client, step.verify, step.scope) if apply and env and step.verify else {}
            if step.action is not None:
                try:
                    record["result"] = step.action(apply, recovery_root)
                    record["status"] = "applied" if apply else "preview"
                    outcome = record["result"]
                    if apply and isinstance(outcome, dict) and outcome.get("applied") is False and outcome.get("blocked"):
                        # Files still wait for a choice: nothing was written, so this is not "done".
                        record["status"], record["needs_choice"] = "blocked", True
                except Unavailable as exc:
                    record["status"], record["error"] = "unavailable", str(exc)
                    if step.client == "cursor":
                        record["next"] = _("用 --source <本地仓库> 指向本地检出；不使用 Cursor 时用 --client 只选其他客户端，"
                                           "或长期忽略：{0} config ignore-client cursor --apply").format(CLI)
                except Exception as exc:  # noqa: BLE001 - report every step, stop nothing silently
                    record["status"], record["error"] = "failed", str(exc)
            elif apply:
                try:
                    completed = subprocess.run(step.command, capture_output=True, text=True, check=False, env=child_env)
                except OSError as exc:
                    record.update({"status": "failed", "exit_code": None, "stderr": "", "error": str(exc)})
                else:
                    record["status"] = "applied" if completed.returncode == 0 else "failed"
                    record["exit_code"] = completed.returncode
                    output = (completed.stdout + completed.stderr).strip()
                    if output and completed.returncode == 0:
                        record["output"] = output[-600:]
                    if completed.returncode != 0:
                        record["stderr"] = tail_lines(completed.stderr)
                        last = next((line.strip() for line in reversed(completed.stderr.splitlines()) if line.strip()), "")
                        record["error"] = _("命令退出码 {0}").format(completed.returncode) + (_("：{0}").format(last) if last else "")
                        known = explain_failure(completed.stderr)
                        if known:
                            record["reason"], record["next"] = known
            else:
                record["status"] = "preview"
            if apply and env and step.verify and record["status"] == "applied":
                verify_step(step, record, before, installed_versions(env, step.client, step.verify, step.scope))
        results.append(record)
        if apply and record["status"] == "failed":
            failed_clients.setdefault(step.client, step.description)
    return results


CLIENT_ORDER = {name: index for index, name in enumerate(CLIENT_KEYS)}


def step_outcome_ok(results: list[dict], *, blockers: bool = False) -> bool:
    """False when a step failed, ran without effect, or still waits for a choice (``blockers``: or is held back by
    a cause the user has to resolve, e.g. Claude Code has not registered the declared market yet)."""
    return not any(r["status"] in {"failed", "unchanged"} or r.get("needs_choice")
                   or (blockers and r["status"] == "blocked" and r.get("blocker")) for r in results)


def outcome_summary(results: list[dict]) -> dict:
    """Per-client outcome of an applied run, so one client's trouble neither hides nor taints the others."""
    rank = {"failed": 0, "unchanged": 1, "unavailable": 2, "blocked": 3, "ok": 4, "skipped": 5}
    clients: dict[str, dict] = {}
    for record in results:
        name = record.get("client") or "content"
        status = {"applied": "ok", "failed": "failed", "unchanged": "unchanged", "unavailable": "unavailable",
                  "blocked": "blocked", "skipped": "skipped", "not_run": "failed"}.get(record["status"], "ok")
        entry = clients.setdefault(name, {"status": "skipped", "steps": 0, "applied": 0, "failed": 0, "unchanged": 0,
                                          "not_run": 0})
        entry["steps"] += 1
        entry["applied"] += record["status"] == "applied"
        entry["failed"] += record["status"] == "failed"
        entry["unchanged"] += record["status"] == "unchanged"
        entry["not_run"] += record["status"] == "not_run"
        if rank[status] < rank[entry["status"]]:
            entry["status"] = status
        if status in {"unavailable"} and record.get("error"):
            entry["reason"] = record["error"]
        if status == "blocked" and record.get("blocker"):
            entry["blocker"] = record["blocker"]
            entry["reason"] = record["blocked"]
    return {"ok": step_outcome_ok(results, blockers=True), "clients": clients}


def render_outcome(results: list[dict]) -> list[str]:
    """The closing lines of an applied run: what took effect, what did not, and which clients are unaffected."""
    summary = outcome_summary(results)
    names = {**CLIENT_NAMES, "content": _("项目内容")}
    lines = [_("汇总：")]
    labels = {"ok": _("已完成"), "failed": _("失败"), "unchanged": _("命令成功但版本没有变化"),
              "unavailable": _("不可用"), "blocked": _("未执行（阻塞）"), "skipped": _("无需操作")}
    ordered = sorted(summary["clients"].items(), key=lambda item: CLIENT_ORDER.get(item[0], 9))
    for name, entry in ordered:
        text = _("  {0}：{1}").format(names.get(name, name), labels[entry['status']])
        if entry.get("reason"):
            text += _("（{0}）").format(entry["reason"])
        if entry["not_run"]:
            text += _("；其后 {0} 步没有执行").format(entry["not_run"])
        lines.append(text)
    bad = [names.get(n, n) for n, e in ordered if e["status"] in {"failed", "unchanged"} or e.get("blocker")]
    good = [names.get(n, n) for n, e in ordered if e["status"] == "ok"]
    if bad:
        lines.append(_("整体：没有全部成功——{0} 需要处理；{1}").format(
            _("、").join(bad), (_("其他已完成：{0}。").format(_("、").join(good)) if good else _("没有其他客户端完成。"))))
    elif any(r.get("needs_choice") for r in results):
        lines.append(_("整体：还有文件等你选择，尚未完成。"))
    elif summary["ok"] and good:
        lines.append(_("整体：已完成。"))
    return lines


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


INFORMATIONAL_ISSUES = ("update-check-disabled", "hook-error")


def hook_error_line(failure: dict) -> str:
    return f"{failure['at']} [{failure['hook']}] {failure['type']}: {failure['message']}".rstrip(": ")


def doctor(env: Env, report: dict) -> dict:
    issues = []
    by_plugin: dict[str, list[dict]] = {}
    for item in report["plugins"]:
        by_plugin.setdefault(item["plugin"], []).append(item)
    for plugin, items in sorted(by_plugin.items()):
        clients = [item["client"] for item in items]
        if "legacy" in clients and any(client in {"claude", "codex"} for client in clients):
            issues.append({"plugin": plugin, "issue": "duplicate",
                           "detail": _("同时存在市场安装与旧方式安装，运行 {0} migrate").format(CLI)})
        versions = {item.get("version") for item in items if item["client"] != "legacy"}
        if len(versions) > 1:
            issues.append({"plugin": plugin, "issue": "version-skew",
                           "detail": _("各客户端版本不一致：") + _("、").join(f"{i['client']}={i.get('version')}" for i in items)})
    names = set(report["_latest"].get("plugins", {}))
    installed_names = {item["plugin"] for item in report["plugins"] if item["client"] != "legacy"}
    if installed_names and installed_names != names:
        issues.append({"plugin": None, "issue": "incomplete",
                       "detail": _("缺少：") + _("、").join(sorted(names - installed_names))})
    if not update_check_enabled(env):
        issues.append({"plugin": None, "issue": "update-check-disabled", "detail": _("会话内更新检查已关闭")})
    failure = last_hook_error(env)
    if failure:
        issues.append({"plugin": None, "issue": "hook-error", "detail": _("最近一次钩子错误：{0}").format(hook_error_line(failure))
                       + _("；运行 {0} nudge status --clear-errors 清除").format(CLI)})
    return {"ok": not [issue for issue in issues if issue["issue"] not in INFORMATIONAL_ISSUES],
            "issues": issues, "legacy": legacy_artifacts(env, names), "summary": report["summary"],
            "last_hook_error": failure}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="agenthelm", description=_("agenthelm：在 Claude Code、Codex、Cursor 间安装、检查与更新 agenthelm 插件。"))
    sub = result.add_subparsers(dest="command", required=True)

    def refresh(command: argparse.ArgumentParser) -> None:
        command.add_argument("--refresh", action="store_true", help=_("忽略一天的缓存，立即联网"))

    def client_filter(command: argparse.ArgumentParser, help_text: str | None = None) -> None:
        command.add_argument("--client", action="append", choices=CLIENTS,
                             help=help_text or _("只处理这个客户端（可重复）；默认处理检测到的全部客户端（用户配置 ignored_clients 里的除外）"))

    def common(command: argparse.ArgumentParser, *, project: bool = True) -> None:
        command.add_argument("--latest", help=_("latest.json 的 URL 或本地路径（测试、离线镜像）"))
        command.add_argument("--output", choices=("text", "json"), default="text")
        command.add_argument("--home", type=Path, help=argparse.SUPPRESS)
        if project:
            command.add_argument("--project", type=Path, default=None, help=_("项目目录；默认当前目录"))
            command.add_argument("--no-project", action="store_true", help=_("不检查项目内容"))

    check = sub.add_parser("check", help=_("只读检查插件、模板/样例与 Opinion 是否落后于云端"))
    common(check)
    check.add_argument("--offline", action="store_true", help=_("只用缓存，不联网"))
    refresh(check)
    client_filter(check)
    check.add_argument("--source", type=Path, help=_("Cursor 更新用的本地仓库：给出后 Cursor 按可更新计（与 update --source 一致）"))
    check.add_argument("--hook", action="store_true", help=_("会话开始时静默调用：有更新才输出一行"))
    check.add_argument("--hook-format", choices=("text", "claude-hook"), default="text")
    check.add_argument("--strict", action="store_true", help=_("有可更新项或必须处理的脚手架差异时退出码为 1（CI 用；可选与信息性的差异不算）"))
    check.add_argument("--show-skipped", action="store_true", help=_("列出被你跳过的项及理由"))
    check.add_argument("--show-extras", action="store_true", help=_("展开 Profile 中 schema 未声明的字段（项目自有、疑似拼写错误、已被其他插件读取）"))
    check.add_argument("--details", action="store_true", help=_("脚手架差异逐项展开（默认同类合并为一行）"))
    check.add_argument("--reprompt", action="store_true", help=_("忽略已记录的决定，重新评估全部脚手架差异"))
    check.add_argument("--fingerprint", action="store_true",
                       help=_("输出脚手架指纹（骨架状态 + 受检 Profile 字段 + 内容锁 + 命名规则；不含 Opinion、自有字段、已跳过项）"))
    check.add_argument("--explain", nargs="?", const="-", metavar=_("对方的 JSON"),
                       help=_("与 --fingerprint 合用：不带参数时逐项列出这台的明细；带另一台机器 check --fingerprint "
                            "--output json 的输出文件时，指出两台差在哪里（有差异退出码 1）"))

    install = sub.add_parser("install", help=_("经各客户端的原生市场安装 agenthelm 插件（默认预览）"))
    common(install, project=False)
    client_filter(install, _("只安装到这个客户端（可重复）；默认安装到检测到的全部客户端（用户配置 ignored_clients 里的除外）"))
    refresh(install)
    install.add_argument("--plugin", action="append", help=_("默认安装全部插件"))
    install.add_argument("--source", type=Path, help=_("Cursor 安装用的本地仓库（默认下载发布包）"))
    install.add_argument("--marketplace-source", help=_("市场来源（默认 latest.json 的 GitHub 仓库；可用镜像或本地路径）"))
    install.add_argument("--apply", action="store_true")

    update = sub.add_parser("update", help=_("更新落后的插件与模板/样例（默认预览，确认后 --apply）"))
    common(update)
    client_filter(update)
    refresh(update)
    update.add_argument("--only", choices=("plugins", "content"))
    update.add_argument("--accept", action="append", default=[], help=_("内容更新：用新模板替换这个本地已改动的文件"))
    update.add_argument("--keep", action="append", default=[], help=_("内容更新：保留这个文件的本地内容"))
    update.add_argument("--append", action="append", default=[],
                        help=_("内容更新：保留 AGENTS.md / CLAUDE.md 的本地内容，只在末尾追加带标记的区块（先备份）"))
    update.add_argument("--confirm-discard", action="append", default=[], dest="confirm_discard",
                        help=_("内容更新：确认 --accept 会丢弃这个文件里的本地内容"))
    update.add_argument("--diff", action="append", default=[], help=_("内容更新：只查看这个文件与新模板的完整差异，不写入"))
    update.add_argument("--merge", action="append", default=[],
                        help=_("内容更新：三方合并这个文件（用旧版本包还原出升级前的基线，保留你的改动并带入新模板的变化；--apply 前先备份）"))
    update.add_argument("--pick", action="append", default=[], metavar=_("文件#块=mine|theirs|both"),
                        help=_("内容更新：为 --merge 的冲突块选择 mine（保留你的）、theirs（取新模板）或 both（两边都留）"))
    update.add_argument("--recovery-dir", type=Path, help=_("被替换文件的备份目录（项目外），缺省 ~/.agent-pack/recovery/"))
    update.add_argument("--source", type=Path, help=_("Cursor 更新用的本地仓库（默认下载发布包）"))
    update.add_argument("--apply", action="store_true")

    migrate = sub.add_parser("migrate", help=_("把旧方式安装（agent-pack 复制 + 软链）迁到市场安装（默认预览）"))
    common(migrate, project=False)
    migrate.add_argument("--install", action="store_true", help=_("先经市场安装到检测到的客户端"))
    client_filter(migrate)
    refresh(migrate)
    migrate.add_argument("--source", type=Path)
    migrate.add_argument("--marketplace-source")
    migrate.add_argument("--apply", action="store_true")

    doctor_parser = sub.add_parser("doctor", help=_("报告重复安装、版本不一致、旧方式残留与检查开关"))
    common(doctor_parser)

    sub.add_parser("fix", add_help=False,
                   help=_("修复项目脚手架差异与更新（等同 project fix；默认预览，--apply 写入；--skip/--unskip 记录决定）"))
    sub.add_parser("local-only", add_help=False,
                   help=_("只属于这台机器的目录（.venv、node_modules、缓存）不进云同步盘：check / fix / config（详见 local-only --help）"))
    sub.add_parser("runtime", add_help=False,
                   help=_("用户级运行时：install / update / use / status（日常只需要 Python，不经过 npx）"))
    sub.add_parser("config", add_help=False,
                   help=_("用户级设置：界面语言、语言提示、项目文件语言（config show / language / language-hint / project-language；默认只预览）"))
    sub.add_parser("project", add_help=False,
                   help=_("项目自助入口：找资料、看进展、项目指南、检查（不需要 AI Agent；详见 project --help）"))
    locate_parser = sub.add_parser("locate", help=_("输出插件的已安装位置（最新优先），供下游脚本查找"))
    locate_parser.add_argument("plugin")
    locate_parser.add_argument("--version", help=_("只要这个版本"))
    locate_parser.add_argument("--include-legacy", action="store_true",
                               help=_("也列出旧方式安装（默认不列，避免清理后才失效）"))
    locate_parser.add_argument("--output", choices=("text", "json"), default="json")
    locate_parser.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    return result


# 内容刷新的状态词与处置词（JSON 字段值保持英文标识；agents-init、project-orchestrator 各带同样的词表）。
CONTENT_STATE_LABELS = {"unchanged": N_("与新模板相同"), "clean": N_("没改过"), "modified": N_("你改过"), "missing": N_("目前不存在"),
                        "local-only": N_("只有你的改动"), "kept-by-decision": N_("已决定保留"), "symlink": N_("符号链接")}
CONTENT_ACTION_LABELS = {"keep": N_("保持原样"), "replace": N_("替换"), "create": N_("新建"), "skip": N_("不新建"), "blocked": N_("阻塞"),
                         "merge": N_("三方合并"), "append": N_("追加区块")}


def content_state_label(state: str) -> str:
    return _(CONTENT_STATE_LABELS[state]) if state in CONTENT_STATE_LABELS else state


def content_action_label(action: str) -> str:
    return _(CONTENT_ACTION_LABELS[action]) if action in CONTENT_ACTION_LABELS else action


def _pad(text: str, width: int = 10) -> str:
    return text + " " * max(width - sum(2 if ord(char) > 0x2E7F else 1 for char in text), 0)


def _print_steps(results: list[dict], apply: bool) -> None:
    labels = {"preview": _("预览"), "applied": _("完成"), "blocked": _("阻塞"), "failed": _("失败"),
              "unchanged": _("完成，无变化"), "skipped": _("跳过"), "unavailable": _("不可用"), "not_run": _("未执行")}
    for record in results:
        line = f"  [{labels.get(record['status'], record['status'])}] {record['description']}"
        if record.get("command"):
            line += f"\n      $ {record['command']}"
        if record.get("note"):
            line += f"\n      {record['note']}"
        if record.get("blocked"):
            line += f"\n      {record['blocked']}"
        if record.get("blocker") and record.get("next"):
            line += _("\n      下一步：{0}").format(record["next"])
        if record.get("skipped"):
            line += f"\n      {record['skipped']}"
        if record["status"] in {"failed", "unavailable"}:
            if record.get("exit_code") is not None:
                line += _("\n      退出码：{0}").format(record["exit_code"])
            if record.get("error"):
                line += _("\n      错误：{0}").format(record["error"])
            if record.get("reason"):
                line += _("\n      原因：{0}").format(record["reason"])
            if record.get("next"):
                line += _("\n      下一步：{0}").format(record["next"])
            if record.get("stderr"):
                line += _("\n      客户端 stderr（最多 {0} 行）：").format(STDERR_LINES)
                line += "".join(f"\n        {text}" for text in record["stderr"].splitlines())
        elif record.get("error"):
            line += f"\n      {record['error']}"
        if record["status"] == "not_run":
            line += f"\n      {record['reason']}"
        for change in record.get("changes", []) if record["status"] in {"applied", "unchanged"} else []:
            prefix = f"{change['plugin']}: " if len(record["changes"]) > 1 else ""
            if change["changed"]:
                line += f"\n      {prefix}{change['before'] or _('（未安装）')} → {change['after']}" + (
                    _("（目标 {0}）").format(change["target"]) if change.get("target") and change["after"] != change["target"] else "")
            else:
                line += _("\n      {0}仍是 {1}（目标 {2}）").format(prefix, change["after"] or _("（读不到）"), change.get("target") or "?")
        if record["status"] == "unchanged":
            line += _("\n      原因：{0}").format(record["reason"])
            if record.get("next"):
                line += _("\n      下一步：{0}").format(record["next"])
        result = record.get("result")
        if isinstance(result, dict) and result.get("mode") == "refresh":
            diffs = result.get("diffs") or {}
            for item in [] if diffs else result.get("files", []):  # --diff 只输出差异，不再重复完整预览
                line += f"\n      {_pad(content_action_label(item['action']))}{item['path']} [{content_state_label(item['state'])}]"
                for note in item.get("note", []):
                    line += f"\n          {note}"
            for path, diff in diffs.items():
                line += "\n" + (diff.rstrip("\n") if diff else _("（{0} 与新模板没有差异）").format(path))
            if result.get("hint") and not diffs:
                line += "\n" + "\n".join(f"      {hint}" for hint in result["hint"].splitlines())
        print(line)
    if apply and results:
        print("\n".join(render_outcome(results)))
    if not apply and results:
        print(_("以上为预览；确认后追加 --apply 执行。"))


def run_project(argv: list[str]) -> int:
    """``agenthelm project``: hand over to project-orchestrator's project entry, which owns it."""
    installed = installed_plugins(current_env(), {"project-orchestrator"})
    copies = [PLUGIN_ROOT.parent / "project-orchestrator", *(Path(item["path"]) for item in installed)]
    # Older copies predate the project entry; only those that ship it count.
    usable = [path for path in copies if (path / "runtime" / "project_cli.py").is_file()]
    if not usable:
        stale = any((path / "plugin.json").is_file() for path in copies)
        found = _("（已安装的 project-orchestrator 版本过旧）") if stale else ""
        print(_("agenthelm project 需要 project-orchestrator 的项目入口，但没有找到{0}。\n  · 更新插件：{1} update，或从发布包安装运行时（见 INSTALL.md）").format(found, CLI), file=sys.stderr)
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


def user_settings_module():
    sys.path.insert(0, str(RUNTIME_DIR))
    import user_settings  # noqa: PLC0415 - shipped next to this file

    return user_settings


def run_config(argv: list[str]) -> int:
    """``agenthelm config``: interface language, the language hint and the project file language."""
    return user_settings_module().main(argv, sys.modules[__name__])


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
        reason = (report.get("scaffold") or {}).get("error") or _("当前目录不在 agenthelm 项目里")
        print(json.dumps({"ok": False, "error": reason}, ensure_ascii=False) if args.output == "json"
              else _("无法计算脚手架指纹：{0}").format(reason), file=sys.stderr if args.output != "json" else sys.stdout)
        return 4
    payload: dict = {"ok": True, "fingerprint": result["fingerprint"], "detail": result["detail"]}
    lines = [_("脚手架指纹 {0}").format(result['fingerprint'])]
    mine_local: dict = {}
    if project is not None:
        # 本机环境（外置链接、HOME）单独成段：没有 local-only 配置的项目，输出与以前完全一样。
        mine_local = local_only_report(env, project, mode="quick")["fingerprint"]
        if mine_local["detail"]:
            payload["local_only"] = mine_local
            lines.append(_("本机环境指纹 {0}").format(mine_local['fingerprint']))
    differences: list[str] = []
    if args.explain and args.explain != "-":
        try:
            other = read_json(Path(args.explain).expanduser())
        except (OSError, ValueError) as exc:
            print(_("读不了对方的输出：{0}").format(exc), file=sys.stderr)
            return 2
        theirs = other.get("detail") or (other.get("scaffold_fingerprint") or {}).get("detail") or other.get("fingerprint", {}).get("detail")
        if not isinstance(theirs, dict):
            print(_("对方的文件里没有指纹明细：请在对方机器运行 agenthelm check --fingerprint --output json 并保存输出"), file=sys.stderr)
            return 2
        module = scaffold_module(env)
        differences = module.explain(result["detail"], theirs)
        their_local = (other.get("local_only") or {}).get("detail") or {}
        if mine_local.get("detail") or their_local:
            if not their_local:
                differences.append(_("本机环境：对方没有 local-only 明细（对方的 agent-pack 较旧或项目还没配置）"))
            elif not mine_local.get("detail"):
                differences.append(_("本机环境：这台没有 local-only 配置，对方有"))
            else:
                differences += _local_only().explain(mine_local["detail"], their_local)
        payload["same"] = not differences
        payload["differences"] = differences
        lines.append(_("两台机器的脚手架一致。") if not differences else _("有 {0} 处不同：").format(len(differences)))
        lines += [f"  {item}" for item in differences]
    elif args.explain:
        lines += [f"  {key} = {value}" for key, value in result["detail"].items()]
        lines += [f"  local-only {key} = {value}" for key, value in mine_local.get("detail", {}).items()]
    if args.output == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))
    return 1 if differences else 0


def client_hint_text(env: Env, report: dict) -> str:
    """One-time hint for a detected client that looks unused, with the command that ignores it. Recorded once shown."""
    pending = (report.get("clients") or {}).get("unused") or []
    state_file = env.state_dir / "agenthelm" / "client-hints.json"
    try:
        seen = read_json(state_file).get("shown", [])
    except (OSError, ValueError, AttributeError):
        seen = []
    fresh = [name for name in pending if name not in seen]
    if not fresh:
        return ""
    try:
        write_json_atomic(state_file, {"shown": sorted({*seen, *fresh})})
    except OSError:
        pass
    return "".join(_("提示：检测到 {0}，但没有看到你在用它（没有装 agenthelm 插件，或插件都落后）。不使用的话可以忽略它，之后 check / update / "
                     "会话开始提示都不再统计：{1} config ignore-client {2}（预览；加 --apply 写入）。这条提示只显示一次。\n").format(
        CLIENT_NAMES[name], CLI, name) for name in fresh)


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
    if argv[:1] == ["config"]:
        return run_config(argv[1:])
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
                               max_age=0 if args.refresh else CACHE_TTL_SECONDS, reprompt=args.reprompt,
                               clients=args.client, source=args.source)
            report.pop("_latest")
            if args.fingerprint or args.explain:
                return print_fingerprint(env, report, args, project)
            if args.output == "json":
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                sys.stdout.write(render_text(report, show_skipped=args.show_skipped, details=args.details, show_extras=args.show_extras))
                sys.stdout.write(client_hint_text(env, report))
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
                print(_("没有发现问题。") if result["ok"] and not result["legacy"] else "")
            return 0 if result["ok"] else 1
        # install / update / migrate change things, so they always fetch fresh data; --refresh is accepted for symmetry.
        latest, origin = load_latest(env, args.latest, max_age=0)
        if args.command == "install":
            clients = active_clients(env, args.client)
            plugins = args.plugin or sorted(latest.get("plugins", {}))
            steps = install_steps(env, latest, clients, plugins, args.source, home, args.marketplace_source)
            results = execute_steps(steps, apply=args.apply, recovery_root=None,
                                    child_env=env.child_environment(), env=env)
            payload: dict = {"ok": step_outcome_ok(results), "applied": args.apply, "latest": origin,
                             "clients": clients, "steps": results}
        elif args.command == "update":
            report = run_check(env, project, latest_source=args.latest, max_age=0, clients=args.client)
            origin = report["latest"]["source"]
            recovery = args.recovery_dir
            if args.apply and recovery is None and project is not None:
                recovery = recovery_dir_for(env, "content")
            if args.diff and args.apply:
                raise CheckError(_("--diff 只用于查看，不能与 --apply 同用"))
            flags = [value for flag, paths in (("--append", args.append), ("--confirm-discard", args.confirm_discard),
                                               ("--diff", args.diff), ("--merge", args.merge), ("--pick", args.pick))
                     for path in paths for value in (flag, path)]
            report["market_sync"] = claude_market_sync(env, report, project, args.only)
            steps = update_steps(env, report, project, args.source, home, args.accept, args.keep,
                                 recovery, args.only, content_flags=flags)
            results = execute_steps(steps, apply=args.apply, recovery_root=None,
                                    child_env=env.child_environment(), env=env)
            for record in results:  # Codex "no change": say how to pin its marketplace
                if record["status"] == "unchanged" and record.get("client") == "codex":
                    record["next"] = codex_pin_advice(latest)
            payload = {"ok": step_outcome_ok(results, blockers=args.apply), "applied": args.apply, "latest": origin,
                       "steps": results, "summary": report["summary"], "market_sync": report["market_sync"]}
            backups = [record["result"] for record in results
                       if isinstance(record.get("result"), dict) and record["result"].get("recovery")]
            if args.apply and backups:
                payload["recovery"] = backups[0]["recovery"]
                payload["recovery_files"] = [path for item in backups for path in item.get("recovery_files", [])]
            if not steps:
                content_errors = [item for item in report["content"] if item["status"] == "error"] if args.only != "plugins" else []
                chosen = args.accept or args.keep or args.append or args.confirm_discard or args.diff or args.merge or args.pick
                if content_errors:  # 内容锁坏了不是“没有需要更新的内容”：说明原因并以非零退出
                    payload["ok"] = False
                    payload["message"] = _("项目内容没有检查：{0}。修复或移走这个文件后重试；没有改动任何文件。").format(content_errors[0]["error"])
                elif chosen and args.only != "plugins":  # 用户点名了文件，却没有任何待更新内容：不能装作处理过
                    payload["message"] = _("项目没有内容锁（或内容锁里没有可刷新的内容包），--accept / --keep / --append / --diff / --merge 指定的文件没有被处理；没有改动任何文件。"
                                           "没有内容锁的项目用 {0} fix update-content。").format(CLI)
                else:
                    payload["message"] = _("没有需要更新的内容。")
                    differing = baseline_less_differences(env, project) if args.only != "plugins" else []
                    if differing:  # check lists them (no baseline in the lock); update has nothing to render for them
                        payload["message"] += _("有 {0} 项无基线差异（内容与模板不同，内容锁里没有基线），"
                                                "请用 {1} fix update-content（或 update --only content --keep {2}）逐个处理。").format(len(differing), CLI, differing[0])
        else:  # migrate
            names = set(latest.get("plugins", {}))
            artifacts = legacy_artifacts(env, names)
            selected = active_clients(env, args.client)
            steps = install_steps(env, latest, selected, sorted(names),
                                  args.source, home, args.marketplace_source) if args.install else []
            steps += untouched_steps(env, names, selected, kind="migrate")
            results = execute_steps(steps, apply=args.apply, recovery_root=None,
                                    child_env=env.child_environment(), env=env)
            payload = {"ok": step_outcome_ok(results), "applied": args.apply, "latest": origin,
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
        print(origin_line(payload["latest"]))
        if payload.get("message"):
            print(payload["message"])
        _print_steps(payload["steps"], args.apply)
        for item in payload.get("legacy", []):
            print(_("  [旧安装] {0} {1}: {2}").format(item['kind'], item['name'], item['path']) + (_("（需手动处理）") if item.get("manual") else ""))
        if payload.get("recovery") and args.command == "update":
            print(_("被替换文件的原件已备份到：{0}").format(payload['recovery']))
            for path in payload.get("recovery_files", []):
                print(f"  {path}")
            print(_("还原：把备份目录里的文件按相同的相对路径复制回项目目录（cp -p <备份目录>/<文件> <项目目录>/<文件>）。"))
        elif payload.get("recovery"):
            print(_("旧安装已移入：{0}（含 migration.json 清单，可按清单恢复）").format(payload['recovery']))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
