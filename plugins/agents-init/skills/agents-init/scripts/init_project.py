#!/usr/bin/env python3
"""使用 agents-init v6 中文模板安全创建或迁移项目骨架。"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
if str(Path(__file__).resolve().parent) not in sys.path:  # also loaded by path from other plugins
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n_init import N_, _, resolve_language  # noqa: E402


INITIALIZER_VERSION = "6.8.1"
PLUGIN_ROOT = Path(__file__).resolve().parents[3]
PROFILE_SCHEMA = "https://skill.sakanano.moe/skills/agents-init/project.schema.json"
PROFILE_PATH = Path(".agents/moe.sakanano.agent-pack/project.json")
# Baseline of the template-derived files this initializer wrote; `check`
# compares it with the files on disk and the newest content pack version.
CONTENT_LOCK_PATH = Path(".agents/moe.sakanano.agent-pack/content.lock.json")
CONTENT_LOCK_OWNER = "agenthelm"
LEGACY_PROFILE_PATH = Path(".agents/moe.sakanano.project-runtime/project.json")
RUNTIME_BLOCK = {
    "skill": "project-orchestrator",
    "plugin": "project-orchestrator",
    "capability_manager": "agent-pack",
    "distribution": "marketplace",
    "required": True,
    "commit_policy": "explicit",
    "push_policy": "explicit",
}
BUNDLED_PLUGINS = {
    "project-orchestrator": (Path("skills/project-orchestrator/SKILL.md"),),
    "agent-pack": (
        Path("skills/agent-pack/SKILL.md"),
        Path("runtime/agent_pack_config.py"),
        Path("runtime/mcp_server.py"),
    ),
    "opinion-manager": (
        Path("skills/opinion-manager/SKILL.md"),
        Path("runtime/opinion_manager.py"),
    ),
}
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "AGENT.template.md"
# 项目根目录的人用入口（启动器）：文件名由项目选择，内容与名称无关，随内容包更新。
LAUNCHER_TEMPLATE = Path(__file__).resolve().parents[1] / "launcher.sh"
DEFAULT_ENTRY = "menu"
ENTRY_API = 1
ENTRY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RESERVED_ENTRY_NAMES = {"AGENTS.md", "AGENT.md", "CLAUDE.md", "AGENT.RULES.md", "OPINION.md", "README.md",
                        "cases", "docs", "memory", "sops", "private", ".agents", ".git"}
# 私有目录：内容默认不进入 git（与 project-orchestrator 的 private_paths.IGNORE_TEXT 相同，测试核对一致）。
PRIVATE_IGNORE = "# agenthelm: 本目录内容默认不进入 git；读取前须经用户确认。\n*\n!.gitignore\n"
README_START = "<!-- agenthelm:entry -->"
README_END = "<!-- /agenthelm:entry -->"
# 内容锁里没有记录模板版本时（早于内容锁的项目），补登的条目使用这个版本，检查会提示刷新。
UNKNOWN_PACK_VERSION = "0.0.0"
TEMPLATE_PATTERN = re.compile(
    r"<!-- agents-init:template (?P<name>[^ ]+) -->\n"
    r"```markdown\n(?P<body>.*?)```\n"
    r"<!-- /agents-init:template -->",
    re.DOTALL,
)
REQUIRED_TEMPLATES = {
    "AGENTS.md",
    "AGENT.RULES.md",
    "OPINION.md",
    "ROUTE.md",
    "CLAUDE.md",
    "region/claude-import",
    "region/agents-contract",
    "region/privacy-contract",
    "region/privacy-contract.en",
    "privacy/declared",
    "privacy/undeclared",
    "privacy/declared.en",
    "privacy/undeclared.en",
    "privacy/rules-line",
    "privacy/rules-line.en",
    ".agent-doc/plan.md",
    ".agent-doc/progress.md",
    ".agent-doc/chat-summary.md",
    "docs/refs/README.md",
}


# 隐私约定相关的区块是 rc.2 才加入的；rc.1 及更早的历史版本包没有它们，渲染旧基线时按空文本处理。
PRIVACY_TEMPLATES = {name for name in REQUIRED_TEMPLATES if name.startswith(("privacy/", "region/privacy-contract"))}


class InitError(Exception):
    """Expected initialization failure."""

    def __init__(self, message: str, *, code: str = "invalid-request") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- recovery (backup of replaced files)

def default_recovery_dir(label: str) -> Path:
    """``~/.agent-pack/recovery/<UTC time>-agenthelm-<label>``: the place ``agenthelm update`` and ``migrate`` use."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return Path.home() / ".agent-pack" / "recovery" / f"{stamp}-agenthelm-{label}"


def prepare_recovery_dir(root: Path, recovery_dir: Path | None, label: str) -> tuple[Path, bool]:
    """The folder replaced files are copied to, ready for writing: ``(path, was_defaulted)``.

    An explicit ``--recovery-dir`` wins; otherwise the default location is used, so nobody has to invent a backup
    folder first. Either way it must be outside the project and writable. Callers resolve it *before* touching any
    project file, so a backup folder that cannot be used leaves the project unchanged instead of skipping the backup.
    """
    defaulted = recovery_dir is None
    target = (default_recovery_dir(label) if defaulted else recovery_dir.expanduser()).resolve()
    if target == root or root in target.parents:
        if defaulted:
            raise InitError(_("默认恢复目录 {0} 在项目目录内，请用 --recovery-dir 指定项目外的目录").format(target))
        raise InitError("--recovery-dir must be outside the project root")
    try:
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not target.is_dir():
            raise OSError(_("不是目录"))
        with tempfile.TemporaryFile(dir=target):
            pass
    except OSError as exc:
        raise InitError(_("恢复目录 {0} 无法创建或写入：{1}。项目文件没有改动；请检查权限，或用 --recovery-dir 指定其他项目外目录").format(target, exc),
                        code="recovery-unavailable") from exc
    return target, defaulted


def backup_into(root: Path, recovery: Path, relative: Path) -> None:
    """Copy ``root/relative`` to the same relative path under ``recovery`` (never overwriting an earlier copy)."""
    archived = recovery / relative
    if archived.exists():
        raise InitError(f"recovery collision: {relative}")
    archived.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    archived.write_bytes((root / relative).read_bytes())


def recovery_lines(result: dict) -> list[str]:
    """Text lines telling the user where the originals went and how to get them back."""
    recovery = result.get("recovery")
    if not recovery or not result.get("applied"):
        return []
    files = [str(path) for path in result.get("recovery_files") or []]
    lines = [_("被替换文件的原件已备份到：{0}").format(recovery)]
    lines += [f"  {path}" for path in files]
    lines.append(_("还原：把备份目录里的文件按相同的相对路径复制回项目目录（cp -p <备份目录>/<文件> <项目目录>/<文件>）。"))
    return lines


@dataclass(frozen=True)
class Inputs:
    project: Path
    name: str
    slug: str
    project_type: tuple[str, ...]
    vcs: str
    stack: tuple[str, ...]
    runtime: str
    agent_cli: tuple[str, ...]
    work_mode: str = "case-workspace"
    entry_launcher: str | None = None
    language: str = "zh"
    privacy_declared: bool = True  # Profile 的 privacy.forbidden_default_reads 非空；决定 AGENTS.md 的隐私约定写哪一版


def split_csv(value: str) -> tuple[str, ...]:
    items = tuple(part.strip() for part in value.split(",") if part.strip())
    if not items:
        raise argparse.ArgumentTypeError("expected at least one non-empty value")
    return items


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise InitError("--name needs an ASCII --slug override")
    return slug


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def load_templates(path: Path = TEMPLATE_PATH) -> dict[str, str]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InitError(_("无法读取中文模板：{0}").format(path), code="template-invalid") from exc
    return parse_templates(source)


def parse_templates(source: str, *, historical: bool = False) -> dict[str, str]:
    """Parse a template file. ``historical`` is for an earlier release's template (baseline rendering): the blocks added
    after it shipped (the privacy convention) may be missing and render as empty."""
    templates = {match.group("name"): match.group("body") for match in TEMPLATE_PATTERN.finditer(source)}
    missing = sorted(REQUIRED_TEMPLATES - set(templates) - (PRIVACY_TEMPLATES if historical else set()))
    if missing:
        raise InitError(_("中文模板缺少生成区块：") + ", ".join(missing), code="template-invalid")
    if historical:
        for name in PRIVACY_TEMPLATES - set(templates):
            templates[name] = ""
    return templates


def privacy_declared_in(profile: dict) -> bool:
    """The Profile declares private paths: ``privacy.forbidden_default_reads`` holds at least one non-empty string."""
    privacy = profile.get("privacy") if isinstance(profile.get("privacy"), dict) else {}
    reads = privacy.get("forbidden_default_reads")
    return isinstance(reads, list) and any(isinstance(item, str) and item.strip() for item in reads)


def localized(templates: dict[str, str], name: str, language: str) -> str:
    """Template ``name`` in the project's file language; ``<name>.en`` for English, the Chinese one when there is none."""
    return templates.get(f"{name}.en", templates[name]) if language == "en" else templates[name]


def privacy_section(templates: dict[str, str], language: str, declared: bool) -> str:
    """AGENTS.md's privacy convention: it names the Profile field instead of copying the paths."""
    return localized(templates, "privacy/declared" if declared else "privacy/undeclared", language).rstrip("\n")


def privacy_convention_state(text: str, templates: dict[str, str]) -> str:
    """How an AGENTS.md carries the privacy convention.

    ``declared`` / ``undeclared``: one of the template versions is in the text (either language, also inside the
    managed region); ``custom``: the user wrote their own rule naming the field; ``absent``: nothing about it.
    """
    for state, name in (("declared", "privacy/declared"), ("undeclared", "privacy/undeclared")):
        for language in ("zh", "en"):
            if privacy_section(templates, language, state == "declared").strip() in text:
                return state
    return "custom" if "privacy.forbidden_default_reads" in text else "absent"


def render_template(template: str, replacements: dict[str, str]) -> str:
    rendered = template
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    return rendered


def _is_regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _read_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InitError(_("Plugin manifest 无法解析：{0}").format(path), code="runtime-required") from exc
    if not isinstance(manifest, dict):
        raise InitError(_("Plugin manifest 根节点必须是对象：{0}").format(path), code="runtime-required")
    return manifest


def _version_key(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value.split("+", 1)[0])
    return tuple(int(number) for number in numbers) if numbers else ()


def _claude_installed_path(cache_market: Path, name: str) -> Path | None:
    """Claude Code keeps superseded version directories; its install record names the live one."""
    record = cache_market.parent.parent / "installed_plugins.json"
    try:
        installed = json.loads(record.read_text(encoding="utf-8")).get("plugins", {})
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    for entry in installed.get(f"{name}@{cache_market.name}", []):
        path = Path(str(entry.get("installPath", ""))).resolve()
        if path.parent == (cache_market / name).resolve() and (path / "plugin.json").is_file():
            return path
    return None


def locate_companion(agents_init_root: Path, name: str) -> Path | None:
    """Find a companion plugin installed next to this agents-init.

    Two layouts exist: ``<dir>/<plugin>`` (this repository, Cursor's local
    plugins, agent-pack bootstrap) and ``<marketplace>/<plugin>/<version>``
    (the Claude Code and Codex plugin caches).
    """
    flat = agents_init_root.parent / name
    if (flat / "plugin.json").is_file():
        return flat
    cache_market = agents_init_root.parent.parent
    versions_dir = cache_market / name
    if not versions_dir.is_dir():
        return None
    live = _claude_installed_path(cache_market, name)
    if live is not None:
        return live
    candidates = [path for path in versions_dir.iterdir() if (path / "plugin.json").is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (_version_key(path.name), path.stat().st_mtime))


def _agents_init_root(plugin_root: Path) -> Path:
    root = plugin_root.expanduser().resolve()
    # A repository checkout passes its root; plugins live under plugins/.
    nested = root / "plugins" / "agents-init"
    return nested if (nested / "plugin.json").is_file() else root


def validate_runtime(plugin_root: Path) -> dict[str, str | bool]:
    root = _agents_init_root(plugin_root)
    if not _is_regular_file(root / "plugin.json"):
        raise InitError(_("缺少 agents-init Plugin manifest：{0}").format(root / 'plugin.json'), code="runtime-required")
    if _read_manifest(root / "plugin.json").get("name") != "agents-init":
        raise InitError(_("Plugin manifest 必须声明 name=agents-init"), code="runtime-required")
    missing: list[str] = []
    versions: dict[str, str] = {}
    for name, files in BUNDLED_PLUGINS.items():
        companion = locate_companion(root, name)
        if companion is None:
            missing.append(f"{name}/plugin.json")
            continue
        missing.extend(f"{name}/{relative}" for relative in files if not _is_regular_file(companion / relative))
        manifest = _read_manifest(companion / "plugin.json")
        if manifest.get("name") != name:
            raise InitError(_("同包 Plugin manifest 必须声明 name={0}").format(name), code="runtime-required")
        versions[name] = str(manifest.get("version", ""))
    if missing:
        raise InitError(
            _("缺少必需的 project-orchestrator / agent-pack / opinion-manager 文件：")
            + ", ".join(missing),
            code="runtime-required",
        )
    return {
        "available": True,
        "source": "bundled" if root == PLUGIN_ROOT.resolve() else "external",
        "plugin": "agents-init",
        "lifecycle": f"project-orchestrator@{versions['project-orchestrator']}",
        "capabilities": f"agent-pack@{versions['agent-pack']}",
        "opinion": f"opinion-manager@{versions['opinion-manager']}",
    }


def read_legacy_profile(root: Path) -> dict | None:
    current = root
    last = len(LEGACY_PROFILE_PATH.parts) - 1
    for index, part in enumerate(LEGACY_PROFILE_PATH.parts):
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(metadata.st_mode):
            kind = "legacy profile" if index == last else "parent"
            raise InitError(f"refusing symlinked {kind}: {current.relative_to(root)}")
    if not stat.S_ISREG(metadata.st_mode):
        raise InitError(f"legacy profile is not a regular file: {LEGACY_PROFILE_PATH}")
    try:
        profile = json.loads(current.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InitError(_("legacy profile 无法解析：{0}").format(LEGACY_PROFILE_PATH), code="profile-invalid") from exc
    if not isinstance(profile, dict) or profile.get("schema_version") != "1.0":
        raise InitError(_("legacy profile 必须是 schema_version=1.0 的对象"), code="profile-invalid")
    return profile


def upgrade_legacy_profile(legacy: dict) -> dict:
    """保留项目自定义字段，只替换运行时归属与版本元数据。"""
    upgraded = {"$schema": PROFILE_SCHEMA, **legacy}
    upgraded["$schema"] = PROFILE_SCHEMA
    upgraded["initializer_version"] = INITIALIZER_VERSION
    runtime = dict(legacy.get("runtime") or {})
    for key in ("skill", "plugin", "capability_manager", "distribution"):
        runtime[key] = RUNTIME_BLOCK[key]
    for key in ("required", "commit_policy", "push_policy"):
        runtime.setdefault(key, RUNTIME_BLOCK[key])
    upgraded["runtime"] = runtime
    return upgraded


def configure_work_mode(profile: dict, mode: str | None) -> dict:
    if profile.get('work') is not None and not isinstance(profile['work'], dict):
        raise InitError(_('work 配置必须是对象'), code='profile-invalid')
    if mode is None:
        return profile
    work = dict(profile.get('work') or {})
    if any(work.get(key) is not None and not isinstance(work[key], dict) for key in ('task', 'case')):
        raise InitError(_('task/case 配置必须是对象或 null'), code='profile-invalid')
    if mode == 'case-workspace':
        if work.get('task'):
            work['legacy_task'] = {**work['task'], 'mode': 'migrate-only'}
        work['task'] = None
        work['case'] = {**(work.get('case') or {}),
                        'root': (work.get('case') or {}).get('root', 'cases'),
                        'artifact_mode': 'case-local'}
    work['mode'] = mode
    profile['work'] = work
    return profile


def validate_inline(label: str, value: str) -> str:
    if not value.strip():
        raise InitError(_("{0} 不能为空").format(label))
    if len(value) > 512 or any(ord(character) < 32 for character in value):
        raise InitError(_("{0} 只能包含单行可打印文本").format(label))
    return value.strip()


def validate_entry_name(name: str) -> str:
    if not ENTRY_NAME_RE.fullmatch(name) or name in RESERVED_ENTRY_NAMES:
        raise InitError(_("启动器名称不合法：{0}（只能用字母、数字、. _ -，且不能与骨架文件同名）").format(name))
    return name


def launcher_text() -> str:
    try:
        return LAUNCHER_TEMPLATE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InitError(_("无法读取启动器模板：{0}").format(LAUNCHER_TEMPLATE), code="template-invalid") from exc


def entry_command(launcher: str | None) -> str:
    return f"./{launcher}" if launcher else "agenthelm project"


def readme_block(launcher: str | None, language: str = "zh") -> str:
    """The README section that points at the project entry, written in the project's file language."""
    command = entry_command(launcher)
    if language == "en":
        return (f"{README_START}\n## How to use\n\nRun `{command}` in the project root to open the project menu: find files, see progress, "
                f"read the project guide, check and maintain. No AI client is needed, only Python 3.11+; if the first run says the runtime is "
                f"missing, install it once as prompted.\n\n"
                f"You can also skip the menu: `{command} find <words>`, `{command} progress`, `{command} --help`.\n{README_END}\n")
    return (f"{README_START}\n## 怎样使用\n\n在项目根目录运行 `{command}` 打开项目菜单：找资料、看进展、项目指南、检查与维护。"
            f"不需要 AI 客户端，只需要 Python 3.11+；第一次运行如果提示缺少运行时，按提示安装一次即可。\n\n"
            f"不进菜单也可以：`{command} find 关键词`、`{command} progress`、`{command} --help`。\n{README_END}\n")


def file_language(args: argparse.Namespace) -> str:
    """The language a new project's files are written in: --language, else the UI language at init time (migrate keeps zh)."""
    if getattr(args, "language", "auto") in ("zh", "en"):
        return args.language
    return "zh" if args.mode == "migrate" else resolve_language()


def project_language(profile: dict) -> str:
    """The language of a project's generated files: the Profile's ``language``, ``zh`` for projects that predate it."""
    return profile.get("language") if profile.get("language") in ("zh", "en") else "zh"


def tracked_paths(pack: dict | None, launcher: str | None) -> list[Path]:
    """Files the content lock follows: the pack's fixed list plus this project's launcher."""
    tracked = [Path(path) for path in (pack or {}).get("materializes", [])]
    return tracked + ([Path(launcher)] if launcher else [])


def files_for(inputs: Inputs, templates: dict[str, str] | None = None, launcher: str | None = None) -> dict[Path, str]:
    """``launcher`` is the launcher script text; omitted, the one shipped with this plugin."""
    templates = templates or load_templates()
    profile = {
        "$schema": PROFILE_SCHEMA,
        "schema_version": "1.0",
        "initializer_version": INITIALIZER_VERSION,
        "name": inputs.name,
        "language": inputs.language,
        "profile": {
            "project_type": list(inputs.project_type),
            "vcs": inputs.vcs,
            "stack": list(inputs.stack),
            "runtime": inputs.runtime,
            "agent_cli": list(inputs.agent_cli),
        },
        "runtime": dict(RUNTIME_BLOCK),
        "opinion": {
            "provider": "opinion-manager",
            "project_overlay": "OPINION.md",
            "strict_mode": "smart",
            "loading_mode": "tiered",
        },
        "memory": {
            "provider": "project-orchestrator",
            "root": "memory",
            "index_command": None,
            "check_command": None,
            "policy": None,
        },
        "sop": {"root": "sops"},
        "capabilities": {
            "plugin_roots": [".agents"],
            "plugin_dirs": [],
            "skill_roots": [".agents/skills"],
            "mcp_sources": [".agents/mcp.json"],
            "native_mcp_sources": [],
            "credential_env_file": None,
            "mcp_client_policy": {},
            "destination_skill_root": ".agents/skills",
            "destination_mcp": ".agents/mcp.json",
        },
        "work": {
            "mode": inputs.work_mode,
            "task": None,
            "case": {"root": "cases", "artifact_mode": "case-local"}
            if inputs.work_mode == "case-workspace" else None,
        },
        "privacy": {"forbidden_default_reads": ["private/"], "generated_outputs": []},
    }
    if inputs.entry_launcher:
        profile["entry"] = {"api": ENTRY_API, "launcher": inputs.entry_launcher}
    replacements = {
        "ENTRY_COMMAND": entry_command(inputs.entry_launcher),
        "PROJECT_NAME": inputs.name,
        "PROJECT_SLUG": inputs.slug,
        "PROJECT_TYPE": ", ".join(inputs.project_type),
        "VCS": inputs.vcs,
        "STACK": ", ".join(inputs.stack),
        "RUNTIME": inputs.runtime,
        "AGENT_CLI": ", ".join(inputs.agent_cli),
    }
    replacements["PRIVACY_SECTION"] = privacy_section(templates, inputs.language, inputs.privacy_declared)
    replacements["PRIVACY_RULES_LINE"] = localized(templates, "privacy/rules-line", inputs.language).rstrip("\n")
    agents = render_template(templates["AGENTS.md"], replacements)
    route = render_template(templates["ROUTE.md"], replacements)
    files = {
        Path("AGENTS.md"): agents,
        Path("AGENT.RULES.md"): render_template(templates["AGENT.RULES.md"], replacements),
        Path("OPINION.md"): render_template(templates["OPINION.md"], replacements),
        Path("CLAUDE.md"): render_template(templates["CLAUDE.md"], replacements),
        Path("AGENT.md"): route,
        Path(".agents/plugin.json"): json_text({
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": inputs.slug,
            "version": "0.1.0",
            "description": "项目本地的可移植 Agent 能力包。",
        }),
        Path(".agents/mcp.json"): json_text({
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {},
        }),
        PROFILE_PATH: json_text(profile),
        Path(".agents/skills/.gitkeep"): "",
        Path(".agent-doc/plan.md"): render_template(templates[".agent-doc/plan.md"], replacements),
        Path(".agent-doc/progress.md"): render_template(templates[".agent-doc/progress.md"], replacements),
        Path(".agent-doc/chat-summary.md"): render_template(templates[".agent-doc/chat-summary.md"], replacements),
        Path("memory/.gitkeep"): "",
        Path("sops/.gitkeep"): "",
        Path("private/.gitignore"): PRIVATE_IGNORE,
        Path("docs/refs/README.md"): render_template(templates["docs/refs/README.md"], replacements),
        Path("docs/drafts/.gitkeep"): "",
    }
    if inputs.work_mode == "case-workspace":
        files[Path("cases/.gitkeep")] = ""
    if inputs.entry_launcher:
        files[Path(inputs.entry_launcher)] = launcher if launcher is not None else launcher_text()
    return files


def validate_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise InitError(f"project directory does not exist: {root}")
    return root


def plan_writes(
    root: Path, files: dict[Path, str], *, skip_symlinked_parents: bool = False
) -> tuple[list[Path], list[Path], list[Path]]:
    """返回 (create, collisions, symlinked)；symlinked 只在迁移模式出现，永不写入。"""
    create: list[Path] = []
    collisions: list[Path] = []
    symlinked: list[Path] = []
    for relative in sorted(files, key=str):
        current = root
        linked = False
        for part in relative.parts[:-1]:
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                break
            if stat.S_ISLNK(metadata.st_mode):
                if not skip_symlinked_parents:
                    raise InitError(f"refusing symlinked parent: {current.relative_to(root)}")
                linked = True
                break
            if not stat.S_ISDIR(metadata.st_mode):
                raise InitError(f"parent is not a directory: {current.relative_to(root)}")
        if linked:
            symlinked.append(relative)
            continue
        destination = root / relative
        try:
            destination.lstat()
            exists = True
        except FileNotFoundError:
            exists = False
        (collisions if exists else create).append(relative)
    return create, collisions, symlinked


def _open_parent(root_fd: int, relative: Path, created_dirs: list[Path]) -> int:
    descriptor = os.dup(root_fd)
    traversed = Path()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        for part in relative.parts[:-1]:
            traversed /= part
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
                created_dirs.append(traversed)
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise InitError(f"unsafe parent path {traversed}: {exc}") from exc
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _write_exclusive(parent_fd: int, name: str, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o644, dir_fd=parent_fd)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o644)
    finally:
        os.close(descriptor)


def _open_existing_parent(root_fd: int, relative: Path) -> int:
    descriptor = os.dup(root_fd)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _unlink_relative(root_fd: int, relative: Path, *, directory: bool = False) -> None:
    parent_fd = _open_existing_parent(root_fd, relative)
    try:
        if directory:
            os.rmdir(relative.name, dir_fd=parent_fd)
        else:
            os.unlink(relative.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def apply_writes(root: Path, files: dict[Path, str], create: Iterable[Path]) -> None:
    planned = list(create)
    rendered = {relative: files[relative].encode("utf-8") for relative in planned}
    created_files: list[Path] = []
    created_dirs: list[Path] = []
    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        for relative in planned:
            parent_fd = _open_parent(root_fd, relative, created_dirs)
            try:
                _write_exclusive(parent_fd, relative.name, rendered[relative])
                created_files.append(relative)
            finally:
                os.close(parent_fd)
    except Exception as exc:
        cleanup_errors: list[str] = []
        for relative in reversed(created_files):
            try:
                _unlink_relative(root_fd, relative)
            except OSError as cleanup_exc:
                cleanup_errors.append(f"{relative}: {cleanup_exc}")
        for relative in reversed(created_dirs):
            try:
                _unlink_relative(root_fd, relative, directory=True)
            except OSError:
                # A concurrent writer may have added unknown content. Never remove it.
                pass
        detail = f"; rollback incomplete: {', '.join(cleanup_errors)}" if cleanup_errors else ""
        raise InitError(f"initialization rolled back after write failure: {exc}{detail}") from exc
    finally:
        os.close(root_fd)


def apply_migration(
    root: Path,
    files: dict[Path, str],
    create: Iterable[Path],
    replace: Iterable[Path],
    recovery_dir: Path | None,
    retire: Iterable[Path] = (),
) -> Path | None:
    """Write the migration; replaced and retired files are moved to the recovery folder first. Returns that folder."""
    create_paths = list(create)
    replace_paths = list(replace)
    retire_paths = list(retire)
    recovery: Path | None = None
    if replace_paths or retire_paths or recovery_dir is not None:
        recovery, _defaulted = prepare_recovery_dir(root, recovery_dir, "migrate")
        conflicts = [path for path in [*replace_paths, *retire_paths] if (recovery / path).exists()]
        if conflicts:
            raise InitError("recovery collision: " + ", ".join(str(path) for path in conflicts))

    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    created_files: list[Path] = []
    created_dirs: list[Path] = []
    moved: list[tuple[Path, Path]] = []
    try:
        for relative in [*replace_paths, *retire_paths]:
            assert recovery is not None
            original = root / relative
            archived = recovery / relative
            archived.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(original, archived)
            moved.append((archived, original))
        for relative in [*create_paths, *replace_paths]:
            parent_fd = _open_parent(root_fd, relative, created_dirs)
            try:
                _write_exclusive(parent_fd, relative.name, files[relative].encode("utf-8"))
                created_files.append(relative)
            finally:
                os.close(parent_fd)
    except Exception as exc:
        for relative in reversed(created_files):
            try:
                _unlink_relative(root_fd, relative)
            except OSError:
                pass
        for archived, original in reversed(moved):
            if archived.exists() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                os.replace(archived, original)
        for relative in reversed(created_dirs):
            try:
                _unlink_relative(root_fd, relative, directory=True)
            except OSError:
                pass
        raise InitError(f"migration rolled back after write failure: {exc}") from exc
    finally:
        os.close(root_fd)
    return recovery


def contract_pack(plugin_root: Path = PLUGIN_ROOT) -> dict | None:
    """The content pack whose rendered files this initializer tracks in the project."""
    try:
        content = json.loads((plugin_root / "content.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    for pack in content.get("packs", []):
        if pack.get("project_lock") == CONTENT_LOCK_OWNER:
            return pack
    return None


def content_baseline(pack: dict | None, files: dict[Path, str], written: Iterable[Path],
                     launcher: str | None = None) -> dict[str, str]:
    if pack is None:
        return {}
    tracked = set(tracked_paths(pack, launcher))
    return {
        path.as_posix(): hashlib.sha256(files[path].encode("utf-8")).hexdigest()
        for path in sorted(written, key=str)
        if path in tracked
    }


def render_inputs(inputs: Inputs) -> dict:
    """The inputs a project's template files were rendered with; with the pack version they rebuild the baseline."""
    return {"name": inputs.name, "slug": inputs.slug, "project_type": list(inputs.project_type), "vcs": inputs.vcs,
            "stack": list(inputs.stack), "runtime": inputs.runtime, "agent_cli": list(inputs.agent_cli),
            "work_mode": inputs.work_mode, "entry_launcher": inputs.entry_launcher, "language": inputs.language,
            "privacy_declared": inputs.privacy_declared}


def write_content_lock(root: Path, pack: dict, baseline: dict[str, str], *, inputs: Inputs | None = None,
                       regions: dict[str, dict[str, str]] | None = None) -> None:
    """Merge this run's baseline into the project's content lock, atomically.

    ``regions`` maps a file to ``{region id: body digest}`` for blocks this tool manages inside a file the user
    owns; ``render_inputs`` records what the templates were rendered with. Keys the lock already has stay.
    """
    path = root / CONTENT_LOCK_PATH
    for parent in [*reversed(CONTENT_LOCK_PATH.parents[:-1])]:
        if (root / parent).is_symlink():
            raise InitError(f"refusing symlinked parent: {parent}")
    if path.is_symlink():
        raise InitError(f"refusing symlinked content lock: {CONTENT_LOCK_PATH}")
    lock: dict = {"schema_version": 1, "packs": {}}
    if path.exists():
        try:
            lock = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InitError(_("content lock 无法解析：{0}").format(CONTENT_LOCK_PATH), code="profile-invalid") from exc
    entry = lock.setdefault("packs", {}).get(pack["id"], {})
    files = {**entry.get("files", {}), **baseline}
    updated = {**entry, "plugin": "agents-init", "version": pack["version"], "files": dict(sorted(files.items()))}
    if inputs is not None:
        updated["render_inputs"] = render_inputs(inputs)
    if regions:
        merged = {path_: dict(ids) for path_, ids in (entry.get("regions") or {}).items()}
        for path_, ids in regions.items():
            merged[path_] = {**merged.get(path_, {}), **ids}
        updated["regions"] = dict(sorted(merged.items()))
    # Whole-file baselines cannot describe a file owned only through regions. Older locks may contain both.
    for region_path, ids in updated.get("regions", {}).items():
        if ids:
            updated["files"].pop(region_path, None)
            updated.get("file_versions", {}).pop(region_path, None)
    lock["packs"][pack["id"]] = updated
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json_text(lock))


def _read_project_json(root: Path, relative: Path, label: str) -> dict:
    path = root / relative
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if root in parent.parents):
        raise InitError(f"refusing symlinked {label}", code="profile-invalid")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InitError(_("缺少 {0}").format(relative), code="refresh-unavailable") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InitError(_("{0} 无法解析：{1}").format(label, relative), code="profile-invalid") from exc
    if not isinstance(data, dict):
        raise InitError(_("{0} 根节点必须是对象").format(label), code="profile-invalid")
    return data


def inputs_from_profile(root: Path) -> Inputs:
    """Rebuild the initializer inputs a project was rendered with from its own Profile."""
    profile = _read_project_json(root, PROFILE_PATH, "Project Profile")
    details = profile.get("profile") or {}
    try:
        package = _read_project_json(root, Path(".agents/plugin.json"), "project plugin manifest")
        slug = str(package.get("name") or "")
    except InitError:
        slug = ""
    name = str(profile.get("name") or "")
    return Inputs(
        root, name, slug or slugify(name),
        tuple(details.get("project_type") or ()), str(details.get("vcs") or ""),
        tuple(details.get("stack") or ()), str(details.get("runtime") or ""),
        tuple(details.get("agent_cli") or ()),
        str((profile.get("work") or {}).get("mode") or "legacy"),
        _profile_launcher(profile),
        project_language(profile),
        privacy_declared_in(profile),
    )


def _profile_launcher(profile: dict) -> str | None:
    entry = profile.get("entry")
    launcher = entry.get("launcher") if isinstance(entry, dict) else None
    return validate_entry_name(launcher) if isinstance(launcher, str) else None


def _scaffold():
    """scaffold.py 与本文件同目录；绑定到正在运行的本模块，保证异常类一致。"""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import scaffold  # noqa: PLC0415

    scaffold.bind(sys.modules[__name__])
    return scaffold


def _unified_diff(relative: str, current: str, new: str, limit: int | None = 200) -> str:
    lines = list(difflib.unified_diff(
        current.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{relative}", tofile=f"b/{relative}",
    ))
    if limit is not None and len(lines) > limit:
        lines = lines[:limit] + [f"... ({len(lines) - limit} more diff lines)\n"]
    return "".join(lines)


# 保留本地内容、只追加带标记区块的文件 → 区块 ID（模板里的 region/<ID>）。
# AGENTS.md 的 agents-contract 区块已随 rc.1 发布，正文语义不变；隐私约定是独立的 privacy-contract 区块。
APPEND_REGIONS = {Path("AGENTS.md"): ("agents-contract", "privacy-contract"), Path("CLAUDE.md"): ("claude-import",)}
CLAUDE_IMPORT_RE = re.compile(r"^\s*@(?:\./)?AGENTS\.md\s*$", re.MULTILINE)


def line_delta(current: str, new: str) -> tuple[int, int]:
    """(lines the new text adds, lines of the current text it would drop)."""
    added = lost = 0
    matcher = difflib.SequenceMatcher(None, current.splitlines(), new.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            lost += i2 - i1
            added += j2 - j1
    return added, lost


def _regions():
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import regions  # noqa: PLC0415

    return regions


def _baseline():
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import baseline  # noqa: PLC0415

    return baseline


def _merge3():
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import merge3  # noqa: PLC0415

    return merge3


def baseline_providers() -> list:
    """Where historical content packs come from (runtime cache, local git tags, GitHub Release); tests replace this."""
    return _baseline().default_providers()


def _inputs_from_lock(root: Path, recorded: object, fallback: Inputs) -> Inputs:
    """The inputs recorded in the content lock; the Profile's when the lock has none or they do not fit."""
    if not isinstance(recorded, dict):
        return fallback
    try:
        return Inputs(root, str(recorded["name"]), str(recorded["slug"]), tuple(recorded["project_type"]), str(recorded["vcs"]),
                      tuple(recorded["stack"]), str(recorded["runtime"]), tuple(recorded["agent_cli"]),
                      str(recorded.get("work_mode") or "legacy"), recorded.get("entry_launcher") or None,
                      str(recorded.get("language") or "zh"),
                      recorded["privacy_declared"] if isinstance(recorded.get("privacy_declared"), bool) else fallback.privacy_declared)
    except (KeyError, TypeError):
        return fallback


def find_baseline(root: Path, relative: Path, digest: str, versions: list[str], recorded: object, current: Inputs,
                  providers: list, pack_id: str) -> dict | None:
    """Re-render ``relative`` as an earlier content pack did and return it only if it hashes to ``digest``.

    ``digest`` is what the content lock recorded when the file was written, so a match proves the text is exactly the
    baseline; a wrong guess (another pack version, other inputs) never matches and is skipped.
    """
    start = _inputs_from_lock(root, recorded, current)
    variants = [start, dataclasses.replace(start, privacy_declared=not start.privacy_declared)]
    seen: set[tuple[str, str]] = set()
    for provider in providers:
        try:
            for source in provider(pack_id, versions):
                if (source.kind, source.label) in seen:
                    continue
                seen.add((source.kind, source.label))
                try:
                    templates = parse_templates(source.template, historical=True)
                    for variant in variants:
                        text = files_for(variant, templates, source.launcher).get(relative)
                        if text is not None and hashlib.sha256(text.encode("utf-8")).hexdigest() == digest:
                            return {"text": text, "source": source.label, "kind": source.kind, "version": source.version}
                except (InitError, KeyError, ValueError):
                    continue
        except (OSError, ValueError):
            continue
    return None


# 内容刷新里的状态词与处置词：JSON 字段值保持英文标识，文本输出一律经这两个函数随界面语言显示
# （agent-pack 的 update --only content 与 project-orchestrator 的 fix update-content 各带一份同样的词表）。
STATE_LABELS = {"unchanged": N_("与新模板相同"), "clean": N_("没改过"), "modified": N_("你改过"), "missing": N_("目前不存在"),
                "local-only": N_("只有你的改动"), "kept-by-decision": N_("已决定保留"), "symlink": N_("符号链接")}
ACTION_LABELS = {"keep": N_("保持原样"), "replace": N_("替换"), "create": N_("新建"), "skip": N_("不新建"), "blocked": N_("阻塞"),
                 "merge": N_("三方合并"), "append": N_("追加区块")}


def state_label(state: str) -> str:
    return _(STATE_LABELS[state]) if state in STATE_LABELS else state


def action_label(action: str) -> str:
    return _(ACTION_LABELS[action]) if action in ACTION_LABELS else action


def display_width(text: str) -> int:
    return sum(2 if ord(char) > 0x2E7F else 1 for char in text)


def pad_label(text: str, width: int = 10) -> str:
    return text + " " * max(width - display_width(text), 0)


def file_choices(path: str) -> str:
    """The per-file commands for a file the user must decide on; every suggestion names the file."""
    options = [_("--keep {0} 保留你的版本（建议）").format(path)]
    if Path(path) in APPEND_REGIONS:
        options.append(_("--append {0} 保留并追加带标记的区块").format(path))
    options.append(_("--accept {0} 用新模板替换（先备份，会丢弃本地内容，需再加 --confirm-discard {0}）").format(path))
    options.append(_("--diff {0} 先看差异").format(path))
    return _("；").join(options)


def _refresh_notes(item: dict) -> list[str]:
    """Human lines for a file refresh cannot silently decide: why, what each choice costs, what to type."""
    path, reason = item["path"], item.get("reason")
    notes: list[str] = []
    if item.get("comparison") == "regions":
        return [_("只比较已登记的受管区块，区块外内容属于你；用 --mode region 查看并更新区块，整文件模板变化不再追问")]
    if reason == "baseline-unavailable":
        notes.append(_("找不到这个文件升级前的基线（旧版本包不在本机运行时缓存和本地 git tag 里，也没能从 GitHub Release 取得），无法三方合并；"
                       "没有改动任何文件，请改用 --keep / --append / --accept 选择"))
    elif reason == "merge-conflict":
        numbers = ", ".join(str(number) for number in item["merge"]["unresolved"])
        notes.append(_("有 {0} 处冲突需要你选择（第 {1} 块）：--pick {2}#<序号>=mine|theirs|both"
                       "（mine 保留你的、theirs 取新模板、both 两边都留）").format(len(item["merge"]["unresolved"]), numbers, path))
    elif reason == "no-baseline" and item.get("merge_requested"):
        notes.append(_("内容锁里没有 {0} 的基线摘要，无法三方合并：确认不了哪个历史版本是你的起点，也不猜基线（猜错会让合并悄悄吞掉改动）；"
                       "没有改动任何文件。请改选：--keep {0} / --append {0} / --accept {0}").format(path))
    if item["action"] == "merge":
        merge = item["merge"]
        notes.append(_("三方合并：新模板的变化已并入你的版本（自动合并 {0} 处，冲突 {1} 处均已选择；基线取自 {2}）；写入前先备份").format(
            merge["auto_hunks"], len(merge["conflicts"]), merge["source"]))
    if reason == "symlink":
        notes.append(_("{0} 是指向 {1} 的符号链接，refresh 不跟随链接改写，保留原样").format(path, item.get("link")))
        if item["action"] == "blocked":
            notes.append(_("确认后用 --keep {0} 记为保留（之后不再追问）；要让模板接管请先把它换成普通文件").format(path))
    elif reason == "no-baseline" and item["action"] == "blocked" and not item.get("merge_requested"):
        notes.append(_("内容锁里没有它的基线（早期版本初始化的项目），无法判断是你改过还是模板变了；建议先保留你的版本：--keep {0}").format(path))
    elif reason == "edited":
        notes.append(_("你改过这个文件，新模板也变了"))
        if item["action"] == "blocked" and not item.get("merge_requested"):
            notes.append(_("也可以三方合并（用旧版本包还原出升级前的基线，保留你的改动并带入新模板的变化）：--merge {0}").format(path))
    elif reason == "discard-unconfirmed":
        notes.append(_("--accept 会丢弃 {0} 行本地内容；确认后追加 --confirm-discard {1}").format(item["lines_lost"], path))
    if item["state"] == "kept-by-decision":
        notes.append(_("已按你的决定保留，不再追问。要让模板接管：--accept {0}（先备份，会丢弃本地内容，需再加 --confirm-discard {0}）；"
                       "要带入新模板的变化：--merge {0}（显式 --merge 是一次新的决定，覆盖此前的保留）").format(path) if reason != "no-baseline" else
                     _("已按你的决定保留，不再追问。内容锁里没有它的基线，无法三方合并；要让模板接管：--accept {0}"
                       "（先备份，会丢弃本地内容，需再加 --confirm-discard {0}）").format(path))
    if "local_lines" in item and item["action"] in {"blocked", "replace"}:
        notes.append(_("本地 {0} 行，新模板 {1} 行；采用新模板会新增 {2} 行、丢弃 {3} 行本地内容").format(
            item["local_lines"], item["template_lines"], item["lines_added"], item["lines_lost"]))
    if item["action"] == "blocked" and Path(path) in APPEND_REGIONS:
        notes.append(_("也可以保留你的内容并追加带标记的区块（原文不动；隐私约定只在项目声明了私有路径、且文件里还没有这条约定时才追加）：--append {0}").format(path)
                     if Path(path) == Path("AGENTS.md") else _("也可以保留你的内容并追加带标记的区块（原文不动）：--append {0}").format(path))
    if item["action"] == "blocked":
        notes.append(_("先看差异：--diff {0}").format(path))
    if item["action"] == "append":
        notes.append(_("保留你的内容，末尾追加带标记的区块：{0}（{1} 行，原文不动，先备份）").format(_("、").join(item["regions"]), item["append_lines"]))
    if reason == "already-present":
        notes.append(_("已经导入或带有这个区块，无需追加"))
    privacy = item.get("privacy_contract")
    if privacy == "appended":
        notes.append(_("项目声明了私有路径，且 {0} 里没有隐私约定：追加隐私约定区块（privacy-contract）").format(path))
    elif privacy == "undeclared-skipped":
        notes.append(_("未声明私有路径，不追加隐私约定（privacy.forbidden_default_reads 为空）"))
    elif privacy == "present-skipped":
        notes.append(_("{0} 里已有隐私约定（模板原文或你自己写的），不追加").format(path))
    return notes


def _symlink_item(path: Path, relative: Path, new: str, new_digest: str, keep: set[Path], kept_before: dict) -> dict:
    """A tracked file that is a symbolic link: never written through. Same text as the new render → unchanged;
    kept explicitly or by an earlier decision → keep; otherwise blocked until the user says --keep."""
    link = os.readlink(path)
    try:
        resolved = path.read_text(encoding="utf-8") if path.is_file() else None
    except (OSError, UnicodeDecodeError):
        resolved = None
    if resolved is not None and hashlib.sha256(resolved.encode("utf-8")).hexdigest() == new_digest:
        state, action, reason = "unchanged", "keep", None
    elif relative in keep or kept_before.get(relative.as_posix()) == new_digest:
        state, action, reason = "symlink", "keep", "symlink"
    else:
        state, action, reason = "symlink", "blocked", "symlink"
    item = {"path": relative.as_posix(), "state": state, "action": action, "new_sha256": new_digest, "link": link}
    if reason:
        item["reason"] = reason
    return item


def _parse_picks(values: list[str], merge: set[Path]) -> dict[Path, dict[int, str]]:
    """``--pick 文件#序号=mine|theirs|both`` → {文件: {序号: 选择}}。"""
    picks: dict[Path, dict[int, str]] = {}
    for value in values:
        target, _sep, choice = value.rpartition("=")
        name, _hash, number = target.rpartition("#")
        if not _sep or not _hash or not number.isdigit() or choice not in ("mine", "theirs", "both"):
            raise InitError(_("--pick 的写法是 <文件>#<冲突块序号>=mine|theirs|both：{0}").format(value))
        relative = Path(name)
        if relative not in merge:
            raise InitError(_("--pick 只能用于同时带 --merge 的文件：{0}").format(name))
        picks.setdefault(relative, {})[int(number)] = choice
    return picks


def refresh_project(args: argparse.Namespace, root: Path) -> tuple[int, dict]:
    """Re-render the tracked template files after a content pack update.

    Files still identical to their install-time baseline are replaced; files the user edited are only replaced
    with --accept (and, when that drops local lines, --confirm-discard), kept with --keep, or kept with a marked
    region appended to them with --append (AGENTS.md and CLAUDE.md). Every file the command changes is copied to
    the recovery folder first.
    """
    pack = contract_pack()
    if pack is None:
        raise InitError(_("当前 agents-init 未声明可刷新的内容包"), code="refresh-unavailable")
    try:
        lock = _read_project_json(root, CONTENT_LOCK_PATH, "content lock")
    except InitError as exc:
        if exc.code != "refresh-unavailable":
            raise
        lock = {"schema_version": 1, "packs": {}}
    # Without a baseline (projects initialised before content locks existed),
    # every file that differs from the new render counts as locally modified.
    entry = (lock.get("packs") or {}).get(pack["id"]) or {}
    baseline: dict[str, str] = dict(entry.get("files") or {})
    region_paths = {path for path, ids in (entry.get("regions") or {}).items() if ids}
    file_versions: dict[str, str] = dict(entry.get("file_versions") or {})  # 文件基线是哪个内容包版本渲染的（未更新的文件仍是旧版本）
    inputs = inputs_from_profile(root)
    rendered = files_for(inputs)
    accept = {Path(value) for value in args.accept}
    keep = {Path(value) for value in args.keep}
    append = {Path(value) for value in getattr(args, "append", [])}
    confirm = {Path(value) for value in getattr(args, "confirm_discard", [])}
    show_diff = {Path(value) for value in getattr(args, "diff", [])}
    merge = {Path(value) for value in getattr(args, "merge", [])}
    picks = _parse_picks(getattr(args, "pick", []), merge)
    tracked = tracked_paths(pack, inputs.entry_launcher)
    unknown = (accept | keep | append | confirm | show_diff | merge) - set(tracked)
    if unknown:
        raise InitError(_("不是内容包跟踪的文件：") + ", ".join(sorted(map(str, unknown))))
    chosen_twice = (accept & keep) | (accept & append) | (keep & append) | (merge & (accept | keep | append))
    if chosen_twice:
        raise InitError(_("同一文件只能选一种处理（--accept / --keep / --append / --merge）：") + ", ".join(sorted(map(str, chosen_twice))))
    not_appendable = append - set(APPEND_REGIONS)
    if not_appendable:
        raise InitError(_("只有 AGENTS.md 和 CLAUDE.md 可以追加受管区块：") + ", ".join(sorted(map(str, not_appendable))))
    if show_diff and args.apply:
        raise InitError(_("--diff 只用于查看，不能与 --apply 同用"))
    whole_file_picks = {path.as_posix() for path in (accept | merge)} & region_paths
    if whole_file_picks:
        raise InitError(_("这些文件只管理已登记的区块，请用 --mode region 处理，不能替换或合并整文件：{0}").format(", ".join(sorted(whole_file_picks))))
    templates = load_templates()
    regions = _regions()
    merge3 = _merge3() if merge else None
    providers = baseline_providers() if merge else []
    plan: list[dict] = []
    merged_texts: dict[Path, str] = {}
    kept_before = _scaffold().keep_fingerprints(root)
    for relative in tracked:
        new = rendered[relative]
        new_digest = hashlib.sha256(new.encode("utf-8")).hexdigest()
        path = root / relative
        if path.is_symlink():
            # 契约文件是符号链接（常见：CLAUDE.md → AGENTS.md）：refresh 不跟随链接改写，逐项处置，不拖垮其他文件。
            if relative in accept | append | merge:
                raise InitError(_("{0} 是指向 {1} 的符号链接，refresh 不跟随链接改写，不能用 --accept / --append / --merge；"
                                  "要保留请用 --keep {0}，要让模板接管请先把它换成普通文件").format(relative, os.readlink(path)))
            item = _symlink_item(path, relative, new, new_digest, keep, kept_before)
            if relative in show_diff:
                item["diff"] = ""
            item["note"] = _refresh_notes(item)
            plan.append(item)
            continue
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        current_digest = hashlib.sha256(current.encode("utf-8")).hexdigest() if current is not None else None
        if relative.as_posix() in region_paths and relative not in append:
            # Keep existing whole-file decisions without renewing their template fingerprints. The region table
            # owns future comparisons; even a changed full template must never prompt about the user's text.
            item = {"path": relative.as_posix(), "state": "kept-by-decision" if relative.as_posix() in kept_before else "local-only",
                    "action": "keep", "comparison": "regions"}
            if relative in show_diff:
                diffs = []
                for region_id in sorted(entry["regions"][relative.as_posix()]):
                    key = f"region/{region_id}"
                    if key not in templates:
                        continue
                    existing = regions.get(current, region_id) if current is not None else None
                    diffs.append(_unified_diff(f"{relative.as_posix()}#{region_id}", existing.body if existing else "",
                                               regions.normalize(localized(templates, key, inputs.language))))
                item["diff"] = "".join(diffs)
            item["note"] = _refresh_notes(item)
            plan.append(item)
            continue
        added, lost = line_delta(current or "", new)
        reason = None
        merge_info = None
        if current_digest == new_digest:
            state, action = "unchanged", "keep"
        elif current is None:
            state = "missing"
            action = "create" if relative in accept else "skip"
        elif new_digest == baseline.get(relative.as_posix()):
            # The new template renders this file exactly as before; the user's edits stay untouched.
            state, action = "local-only", "append" if relative in append else "keep"
        elif current_digest == baseline.get(relative.as_posix()):
            state, action = "clean", "keep" if relative in keep else "replace"
        else:
            state = "modified"
            decided = kept_before.get(relative.as_posix()) == new_digest  # 之前选过保留，且期望内容没变
            reason = "edited" if relative.as_posix() in baseline else "no-baseline"
            if relative in merge:
                action = "blocked"
                merge_info = {}
                found = None
                if reason == "edited":
                    wanted = [version for version in (file_versions.get(relative.as_posix()), entry.get("version"))
                              if version and version != UNKNOWN_PACK_VERSION]
                    found = find_baseline(root, relative, baseline[relative.as_posix()], list(dict.fromkeys(wanted)),
                                          entry.get("render_inputs"), inputs, providers, pack["id"])
                    if found is None:
                        reason = "baseline-unavailable"
                if found is not None:
                    merged = merge3.merge_text(found["text"], current, new)
                    chosen = picks.get(relative, {})
                    open_blocks = [block.number for block in merged.unresolved(chosen)]
                    merged_texts[relative] = merged.render(chosen, (_("你的版本"), _("基线"), _("新模板")))
                    merge_info = {"source": found["source"], "baseline_version": found["version"], "auto_hunks": merged.auto_hunks,
                                  "conflicts": [{"id": block.number, "mine": "".join(block.mine), "theirs": "".join(block.theirs),
                                                 "base": "".join(block.base), "pick": chosen.get(block.number)}
                                                for block in merged.conflicts],
                                  "unresolved": open_blocks}
                    if open_blocks:
                        reason = "merge-conflict"
                    else:
                        action, reason = "merge", None
            elif relative in accept:
                action = "replace"
                if lost and relative not in confirm:
                    action, reason = "blocked", "discard-unconfirmed"
            elif relative in append:
                action = "append"
            elif relative in keep or decided:
                action = "keep"
            else:
                action = "blocked"
            if decided and relative not in accept and relative not in merge:
                state = "kept-by-decision"
        item = {"path": relative.as_posix(), "state": state, "action": action, "new_sha256": new_digest}
        if reason:
            item["reason"] = reason
        if relative in merge:
            item["merge_requested"] = True
        if merge_info:
            item["merge"] = merge_info
        if current is not None and current_digest != new_digest:
            item.update(local_lines=len(current.splitlines()), template_lines=len(new.splitlines()),
                        lines_added=added, lines_lost=lost)
        appended_text = None
        if item["action"] == "append":
            wanted, appended = [], current or ""
            privacy = None
            for region_id in APPEND_REGIONS[relative]:
                if region_id == "privacy-contract" and (
                        not inputs.privacy_declared or privacy_convention_state(appended, templates) in ("declared", "custom")):
                    # 没有声明私有路径，或文件里已有这条约定（模板原文或用户自己写的）：不追加
                    privacy = "undeclared-skipped" if not inputs.privacy_declared else "present-skipped"
                    continue
                body = localized(templates, f"region/{region_id}", inputs.language)
                existing = regions.get(appended, region_id)
                if (existing is not None and existing.intact and existing.body == regions.normalize(body)) \
                        or (region_id == "claude-import" and CLAUDE_IMPORT_RE.search(appended)):
                    if region_id == "privacy-contract":
                        privacy = "present-skipped"
                    continue
                appended, region_state = regions.upsert(appended, region_id, body)
                if region_state == "modified":
                    raise InitError(_("{0} 里的受管区块 {1} 被你改过：用 --mode region 处理").format(relative, region_id))
                wanted.append((region_id, body))
                if region_id == "privacy-contract":
                    privacy = "appended"
            if privacy:
                item["privacy_contract"] = privacy
            if not wanted:
                item["action"], item["reason"] = "keep", "already-present"
            else:
                item.update(region=wanted[0][0], regions=[region_id for region_id, _body in wanted],
                            append_lines=sum(len(regions.render(region_id, body).splitlines()) for region_id, body in wanted))
                appended_text = appended
        if item["action"] in {"replace", "blocked", "create", "append"} or (state == "clean" and item["action"] == "keep") \
                or relative in show_diff or merge_info:
            shown = appended_text if appended_text is not None else merged_texts.get(relative, new)
            item["diff"] = _unified_diff(relative.as_posix(), current or "", shown,
                                         None if relative in show_diff else 200)
        item["note"] = _refresh_notes(item)
        plan.append(item)
    satisfied = {item["path"] for item in plan if item["action"] == "append" or item.get("reason") == "already-present"}
    unmet = sorted(path.as_posix() for path in append if path.as_posix() not in satisfied)
    if unmet:
        raise InitError(_("没有本地改动，不需要追加区块：") + ", ".join(unmet))
    not_modified = sorted(item["path"] for item in plan if Path(item["path"]) in merge and item["state"] != "modified")
    if not_modified:
        raise InitError(_("没有需要合并的本地改动（文件没改过，或已与新模板一致）：") + ", ".join(not_modified))
    blocked = [item["path"] for item in plan if item["action"] == "blocked"]
    symlinks = {item["path"] for item in plan if item.get("reason") == "symlink"}
    replacing = [Path(item["path"]) for item in plan if item["action"] in {"replace", "append", "merge"}]
    for relative, chosen in picks.items():
        item = next(item for item in plan if item["path"] == relative.as_posix())
        if "merge" not in item:
            continue  # 取不到基线：整个文件退回 --keep / --append / --accept 的选择
        known = {block["id"] for block in item["merge"]["conflicts"]}
        if set(chosen) - known:
            raise InitError(_("--pick 指向的冲突块不存在：{0}#{1}（先用 --merge {0} 查看冲突块序号）").format(
                relative, ", ".join(str(number) for number in sorted(set(chosen) - known) or [0])))
    result = {
        "ok": not blocked,
        "mode": "refresh",
        "applied": False,
        "pack": f"{pack['id']}@{pack['version']}",
        "from_version": entry.get("version"),
        "files": plan,
        "blocked": blocked,
    }
    if show_diff:
        result["diffs"] = {path.as_posix(): next(item["diff"] for item in plan if item["path"] == path.as_posix()) for path in sorted(show_diff)}
    if blocked:
        hints = []
        needs_choice = [item["path"] for item in plan
                        if item["action"] == "blocked" and item.get("reason") in {"no-baseline", "edited", "baseline-unavailable", "symlink"}]
        if needs_choice:
            hints.append(_("本地已改动的文件需逐个选择："))
            hints += [_("  {0}：{1}").format(path, file_choices(path) if path not in symlinks else _("--keep {0} 保留链接原样（建议）").format(path))
                      for path in needs_choice]
        for item in plan:
            if item.get("reason") == "discard-unconfirmed":
                hints.append(_("--accept 会丢弃本地内容：确认后追加 --confirm-discard {0}").format(item["path"]))
        result["hint"] = "\n".join(hints)
    if not args.apply or blocked or show_diff:
        return (0 if not blocked else 1), result
    if replacing:
        recovery, defaulted = prepare_recovery_dir(root, args.recovery_dir, "refresh")
        for relative in replacing:
            backup_into(root, recovery, relative)
        result.update(recovery=str(recovery), recovery_default=defaulted,
                      recovery_files=[path.as_posix() for path in replacing])
    new_regions: dict[str, dict[str, str]] = {}
    for item in plan:
        relative = Path(item["path"])
        if item["action"] in {"replace", "create"}:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, rendered[relative], 0o755 if relative == Path(inputs.entry_launcher or "\0") else None)
        elif item["action"] == "merge":
            target = root / relative
            _atomic_write(target, merged_texts[relative], os.stat(target).st_mode & 0o7777)
        elif item["action"] == "append":
            target = root / relative
            text = target.read_text(encoding="utf-8")
            for region_id in item["regions"]:
                body = localized(templates, f"region/{region_id}", inputs.language)
                text, _region_state = regions.upsert(text, region_id, body)
                new_regions.setdefault(item["path"], {})[region_id] = regions.digest(body)
            _atomic_write(target, text, os.stat(target).st_mode & 0o7777)
    files = dict(baseline)
    for item in plan:
        if item.get("comparison") == "regions":
            files.pop(item["path"], None)
            file_versions.pop(item["path"], None)
            continue
        if item["action"] in {"replace", "create", "merge"} or item["state"] == "unchanged":
            files[item["path"]] = item["new_sha256"]
            file_versions[item["path"]] = pack["version"]
        elif item["state"] == "local-only":
            file_versions[item["path"]] = pack["version"]  # 新模板渲染结果与基线相同
        elif item["path"] in baseline and item["path"] not in file_versions and entry.get("version") not in (None, UNKNOWN_PACK_VERSION):
            file_versions[item["path"]] = entry["version"]  # 没写这个文件：基线仍是升级前的版本
        # Files kept on purpose keep their old baseline: the tool did not write them.
    new_entry = {**entry, "plugin": "agents-init", "version": pack["version"],
                 "files": dict(sorted(files.items())), "render_inputs": render_inputs(inputs)}
    if file_versions:
        new_entry["file_versions"] = dict(sorted(file_versions.items()))
    if new_regions:
        merged = {path: dict(ids) for path, ids in (entry.get("regions") or {}).items()}
        for path, ids in new_regions.items():
            merged.setdefault(path, {}).update(ids)
        new_entry["regions"] = dict(sorted(merged.items()))
    for region_path, ids in new_entry.get("regions", {}).items():
        if ids:
            new_entry["files"].pop(region_path, None)
            new_entry.get("file_versions", {}).pop(region_path, None)
    lock.setdefault("packs", {})[pack["id"]] = new_entry
    lock_path = root / CONTENT_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(lock_path, json_text(lock))
    # 显式 --keep / --append 的选择记入决定文件：同样的模板内容以后不再追问，模板实质变化时才再问。
    for item in plan:
        if item.get("comparison") == "regions":
            continue
        chosen_keep = Path(item["path"]) in (keep | append) or item["action"] in {"append", "merge"}
        if chosen_keep and item["state"] in {"modified", "local-only", "kept-by-decision", "symlink"}:
            _scaffold().decide(root, f"content:{item['path']}", "keep",
                               reason=("refresh --append: kept the local file and appended a managed region" if item["action"] == "append"
                                       else "refresh --merge: merged the local edits with the new template" if item["action"] == "merge"
                                       else "refresh --keep: kept the local file"),
                               fingerprint=item["new_sha256"], scope="project", apply=True)
            result.setdefault("decisions", []).append(f"content:{item['path']}")
    result["applied"] = True
    return 0, result


def _adopt():
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import adopt  # noqa: PLC0415

    adopt.bind(sys.modules[__name__])
    return adopt


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Write ``text`` through a temporary file and ``os.replace``; a failure removes the temporary and raises InitError."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise InitError(_("写入 {0} 失败，临时文件已清理：{1}。已写好的文件与备份保持一致，排除原因后重新运行同一条命令即可继续").format(
            path.name, exc), code="write-failed") from exc


def readme_plan(root: Path, name: str, launcher: str | None, language: str = "zh") -> dict | None:
    """README.md: create it with the entry section, or append the section once (marked)."""
    path = root / "README.md"
    if path.is_symlink():
        raise InitError("refusing symlinked README.md")
    if not path.exists():
        return {"path": "README.md", "action": "create", "text": f"# {name}\n\n{readme_block(launcher, language)}"}
    current = path.read_text(encoding="utf-8")
    if README_START in current:
        return None
    return {"path": "README.md", "action": "append",
            "text": current.rstrip("\n") + "\n\n" + readme_block(launcher, language)}


def add_entry(args: argparse.Namespace, root: Path) -> tuple[int, dict]:
    """Give an initialised project its root launcher and Profile ``entry`` block.

    Only the Profile, the launcher, the content lock (and README.md with
    --readme-entry) are touched; other template files follow the normal refresh.
    An existing file with the launcher's name is the user's: it is kept or
    replaced only by explicit --keep / --accept, replacements backed up first.
    """
    if not args.entry_name:
        raise InitError(_("--mode entry 需要 --entry-name"))
    name = validate_entry_name(args.entry_name)
    profile = _read_project_json(root, PROFILE_PATH, "Project Profile")
    entry = profile.get("entry") if isinstance(profile.get("entry"), dict) else {}
    if entry.get("launcher") and entry["launcher"] != name:
        raise InitError(_("项目已声明入口 ./{0}；改名请同时修改 Profile 的 entry.launcher 与文件名").format(entry['launcher']))
    template = launcher_text()
    target = root / name
    if target.is_symlink():
        raise InitError(f"refusing symlinked launcher: {name}")
    current = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else None
    if target.exists() and not target.is_file():
        raise InitError(_("{0} 已存在且不是文件").format(name))
    chosen = {Path(value) for value in args.accept} | {Path(value) for value in args.keep}
    if chosen - {Path(name)}:
        raise InitError(_("--mode entry 只接受启动器文件的 --accept / --keep：") + name)
    if current is None:
        action = "create"
    elif current == template:
        action = "unchanged"
    elif name in args.accept:
        action = "replace"
    elif name in args.keep:
        action = "keep"
    else:
        action = "blocked"
    new_profile = {**profile, "entry": {**entry, "api": entry.get("api", ENTRY_API), "launcher": name}}
    profile_changed = new_profile != profile
    readme = readme_plan(root, str(profile.get("name") or root.name), name, project_language(profile)) if args.readme_entry else None
    writes = ([name] if action in ("create", "replace") else []) + ([str(PROFILE_PATH)] if profile_changed else []) \
        + ([readme["path"]] if readme else [])
    if action in ("create", "replace", "unchanged"):
        writes.append(str(CONTENT_LOCK_PATH))
    labels = {"create": _("新建启动器"), "replace": _("用模板替换（先备份）"), "unchanged": _("已是模板内容"),
              "keep": _("保留你的文件（不跟踪更新）"), "blocked": _("已存在同名文件，需要选择")}
    changes = [_("{0}：{1}").format(name, labels[action])]
    if profile_changed:
        changes.append(_("Project Profile：加入 entry（api {0}，launcher {1}）").format(new_profile['entry']['api'], name))
    if readme:
        changes.append(_("README.md：") + (_("新建并写入「怎样使用」") if readme["action"] == "create" else _("追加「怎样使用」一节")))
    result = {
        "ok": action != "blocked", "mode": "entry", "applied": False,
        "entry": {"name": name, "action": action, "writes": writes, "changes": changes,
                  "collisions": [name] if action == "blocked" else [],
                  "profile_diff": _unified_diff(str(PROFILE_PATH), json_text(profile), json_text(new_profile))
                  if profile_changed else ""},
        "blocked": [name] if action == "blocked" else [],
        "readme_snippet": readme_block(name, project_language(profile)) if not readme else "",
    }
    if action == "blocked":
        result["hint"] = _("{0} 已存在：--accept {1} 用模板替换（先备份到恢复目录），或 --keep {2} 保留你的文件").format(name, name, name)
    if not args.apply or action == "blocked":
        return (0 if action != "blocked" else 1), result
    if action == "replace":
        recovery, defaulted = prepare_recovery_dir(root, args.recovery_dir, "entry")
        backup_into(root, recovery, Path(name))
        result.update(recovery=str(recovery), recovery_default=defaulted, recovery_files=[name])
    if action in ("create", "replace"):
        _atomic_write(target, template, 0o755)
    if readme:
        _atomic_write(root / readme["path"], readme["text"])
    if profile_changed:
        _atomic_write(root / PROFILE_PATH, json_text(new_profile))
    if action in ("create", "replace", "unchanged"):
        pack = contract_pack()
        if pack is not None:
            lock_path = root / CONTENT_LOCK_PATH
            lock = _read_project_json(root, CONTENT_LOCK_PATH, "content lock") if lock_path.exists() \
                else {"schema_version": 1, "packs": {}}
            # 新条目只记录刚写入的启动器，它与当前模板一致，所以版本就是当前包版本；
            # 不再写占位的 0.0.0（那会让 check 永远提示“可更新”）。
            pack_entry = lock.setdefault("packs", {}).get(pack["id"]) or \
                {"plugin": "agents-init", "version": pack["version"], "files": {}}
            pack_entry["files"] = dict(sorted({**pack_entry.get("files", {}),
                                               name: hashlib.sha256(template.encode("utf-8")).hexdigest()}.items()))
            lock["packs"][pack["id"]] = pack_entry
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(lock_path, json_text(lock))
    result["applied"] = True
    return 0, result


def _adopt_text(result: dict) -> str:
    lines = []
    if result["mode"] == "scan":
        lines.append(_("Scan（只读）"))
    else:
        lines.append(_("{0}：{1}").format(_("已应用") if result.get("applied") else _("仅预览"), result["mode"]))
    for blocker in result.get("blockers", []):
        lines.append(_("  已阻塞 [{0}] {1}").format(blocker["code"], blocker["message"]))
    scan = result.get("scan", result)
    for item in scan.get("findings", []):
        lines.append(f"  [{item['id']}] {item['summary']}")
        for option in item["options"]:
            mark = _("（推荐）") if option["id"] == item["recommended"] else ""
            lines.append(f"      - {option['label']}{mark}" + (f"  {option['flag']}" if option["flag"] else ""))
    for key, title in (("create", _("新建")), ("preserve", _("保留"))):
        for path in result.get(key, []):
            lines.append(f"  {title} {path}")
    for item in result.get("modify", []):
        lines.append(f"  {item['action']} {item['path']}" + (_("（区块 {0}）").format(_("、").join(item.get("regions") or [item["region"]])) if item.get("region") else ""))
    for note in result.get("notes", []):
        lines.append(_("  说明：{0}").format(note))
    if result.get("needs_recovery_dir") and not result.get("applied"):
        lines.append("  " + _("改动已有文件前会先备份到 ~/.agent-pack/recovery/（也可用 --recovery-dir 指定项目外的目录）"))
    if scan.get("recommended_flags"):
        lines.append("  " + _("推荐选项：") + " ".join(scan["recommended_flags"]))
    if result.get("check_after"):
        lines.append("  " + _("接管后检查：") + (_("一致") if result["check_after"]["ok"] else _("仍有 ") + ", ".join(result["check_after"]["findings"])))
    lines.extend(recovery_lines(result))
    return "\n".join(lines) + "\n"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=_("使用 agents-init v6 中文模板安全创建或迁移项目骨架。"))
    result.add_argument("--project", type=Path, required=True)
    result.add_argument("--name")
    result.add_argument("--slug")
    result.add_argument("--project-type", type=split_csv)
    result.add_argument("--vcs")
    result.add_argument("--stack", type=split_csv)
    result.add_argument("--runtime")
    result.add_argument("--agent-cli", type=split_csv)
    result.add_argument("--mode", choices=("init", "migrate", "refresh", "entry", "check", "add-skeleton", "profile",
                                           "repair-lock", "decide", "case-naming", "scan", "adopt", "region", "language", "accept-current"),
                        default="init",
                        help=_("refresh：内容包更新后按 Profile 重新渲染已跟踪的模板文件；"
                             "entry：为已初始化的项目补上根目录启动器与 Profile 的 entry 段；"
                             "check：只读，对比期望骨架清单；add-skeleton：按模板补上缺失的骨架文件；"
                             "profile：把缺失或不符的受检字段改为模板值；repair-lock：修复内容锁的 0.0.0 版本；"
                             "decide：记录或撤销对某个发现的决定；case-naming：只输出 Case 目录改名计划与受影响的引用，不移动任何目录；"
                             "scan：只读，扫描已有内容与骨架的冲突并给出选项和推荐；"
                             "adopt：接管非空目录（已有文件原样保留，只补骨架；改动已有文件须显式选择并备份）；"
                             "region：更新受管区块（--accept / --keep <文件>#<区块ID>）；"
                             "language：只把 Profile 的 language 设为 --language zh|en（项目文件语言；默认预览，先备份，不改写任何已有文件）"))
    result.add_argument("--target", action="append", default=[],
                        help=_("add-skeleton：骨架路径；profile：字段；decide：发现 ID（可重复；省略则处理全部可处理项）"))
    result.add_argument("--decision", choices=("skip", "keep", "later", "unskip"), help=_("decide：决定；unskip 撤销"))
    result.add_argument("--reason", help=_("decide / accept-current：理由（建议写，随项目同步）"))
    result.add_argument("--fingerprint", help=_("decide：期望指纹（缺省取该发现当前的指纹）"))
    result.add_argument("--reprompt", action="store_true", help=_("check：忽略已记录的决定，重新评估全部发现"))
    result.add_argument("--required-only", action="store_true", dest="required_only",
                        help=_("check：退出码只看「需要处理」的发现（与 agenthelm check 一致）；缺省「可选」差异也使退出码为 1"))
    result.add_argument("--state-dir", type=Path, help=_("机器本地决定的状态目录（缺省 $XDG_STATE_HOME 或 ~/.local/state）"))
    result.add_argument("--entry-name", help=_("根目录启动器文件名（init 缺省 {0}；entry 模式必填）").format(DEFAULT_ENTRY))
    result.add_argument("--no-entry", action="store_true", help=_("init：不生成根目录启动器"))
    result.add_argument("--language", choices=("auto", "zh", "en"), default="auto",
                        help=_("init / migrate：项目文件语言（新建 Case 的小节名、README 追加的使用说明）；auto 在 init 时取当前界面语言，migrate 时保持中文"))
    result.add_argument("--readme-entry", action="store_true",
                        help=_("在已有 README.md 末尾追加「怎样使用」一节（带标记，只追加一次）；init 时 README 不存在会直接新建"))
    result.add_argument("--accept", action="append", default=[], help=_("refresh：用新模板替换这个本地已改动的文件"))
    result.add_argument("--keep", action="append", default=[], help=_("refresh：保留这个文件的本地内容"))
    result.add_argument("--append", action="append", default=[],
                        help=_("refresh：保留 AGENTS.md / CLAUDE.md 的本地内容，只在末尾追加带标记的区块（先备份）"))
    result.add_argument("--confirm-discard", action="append", default=[], dest="confirm_discard",
                        help=_("refresh：确认 --accept 会丢弃这个文件里的本地内容"))
    result.add_argument("--diff", action="append", default=[],
                        help=_("refresh：只查看这个文件与新模板的完整差异，不写入"))
    result.add_argument("--merge", action="append", default=[],
                        help=_("refresh：三方合并这个本地已改动的文件（用旧版本包还原基线，保留你的改动并带入新模板的变化；默认只预览，先备份）"))
    result.add_argument("--pick", action="append", default=[],
                        help=_("refresh --merge：逐块选择冲突，写法 <文件>#<冲突块序号>=mine|theirs|both（可重复）"))
    result.add_argument("--work-mode", choices=("case-workspace", "legacy"), default=None,
                        help=_("新项目默认 case-workspace；既有项目只在显式选择后更改模式"))
    result.add_argument("--replace", action="append", default=[], help="known skeleton path to replace in migrate mode")
    result.add_argument("--skip", action="append", default=[], help="known skeleton path the project does not want (migrate mode)")
    result.add_argument("--recovery-dir", type=Path,
                        help=_("被替换文件的备份目录（必须在项目外）；缺省 ~/.agent-pack/recovery/<UTC 时间>-agenthelm-<模式>/"))
    result.add_argument("--agents-policy", choices=("keep", "append", "split", "side-by-side"), default=None,
                        help=_("adopt：已有 AGENTS.md 怎么处理（缺省 keep，不改动）"))
    result.add_argument("--claude-policy", choices=("keep", "add-import"), default=None,
                        help=_("adopt：已有 CLAUDE.md 没导入 AGENTS.md 时是否追加导入区块（缺省 keep）"))
    result.add_argument("--route", action="append", default=[],
                        help=_("adopt split：<章节序号>=<agents|rules|user|opinion|sop>，未指定的章节留在 AGENTS.md"))
    result.add_argument("--register-root", action="append", default=[],
                        help=_("adopt：把已有的工作容器目录登记到 work.assets.roots（只读登记，不动内容）"))
    result.add_argument("--case-root", help=_("adopt：新 Case 放的目录（缺省 cases）"))
    result.add_argument("--memory-root", help=_("adopt：记忆条目放的目录（缺省 memory）"))
    result.add_argument(
        "--runtime-plugin-root",
        type=Path,
        default=PLUGIN_ROOT,
        help=(
            _("agents-init Plugin 根目录（同级查找 project-orchestrator、agent-pack、"
            "opinion-manager），或含 plugins/ 的仓库根目录")
        ),
    )
    result.add_argument("--apply", action="store_true", help="create the complete skeleton only when no collision exists")
    result.add_argument("--output", choices=("text", "json"), default="text")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        root = validate_root(args.project)
        if args.required_only and args.mode != "check":
            raise InitError(_("--required-only 只用于 --mode check"))
        if args.mode == "scan":
            placeholder = Inputs(root, args.name or root.name, "project", tuple(args.project_type or ("code",)),
                                 args.vcs or "git", tuple(args.stack or ("unknown",)), args.runtime or "local",
                                 tuple(args.agent_cli or ("codex",)), args.work_mode or "case-workspace",
                                 None if args.no_entry else validate_entry_name(args.entry_name or DEFAULT_ENTRY),
                                 file_language(args))
            adopt = _adopt()
            report = adopt.scan(root, placeholder, case_root=args.case_root or "cases",
                                memory_root=args.memory_root or "memory")
            result = {"ok": not report["blockers"], "mode": "scan", "applied": False, **report}
            print(json_text(result), end="") if args.output == "json" else print(_adopt_text(result), end="")
            return 0 if result["ok"] else 1
        if args.mode == "region":
            code, result = _scaffold().update_regions(root, accept=args.accept, keep=args.keep, apply=args.apply,
                                                      recovery_dir=args.recovery_dir, state_dir=args.state_dir)
            print(json_text(result), end="") if args.output == "json" else print(_scaffold().render_text(result), end="")
            return code
        if args.mode in ("check", "add-skeleton", "profile", "repair-lock", "decide", "case-naming", "language", "accept-current"):
            scaffold = _scaffold()
            code, result = scaffold.run_mode(args, root)
            print(json_text(result), end="") if args.output == "json" else print(scaffold.render_text(result), end="")
            return code
        if args.mode == "entry":
            code, result = add_entry(args, root)
            if args.output == "json":
                print(json_text(result), end="")
            else:
                print(_("{0}：入口 ./{1}").format(_("已应用") if result["applied"] else _("已阻塞") if result["blocked"] else _("仅预览"),
                                                 result["entry"]["name"]))
                for change in result["entry"]["changes"]:
                    print(f"  {change}")
                if result.get("hint"):
                    print(result["hint"])
                if result.get("readme_snippet"):
                    print(_("README 里可以加上：\n") + result["readme_snippet"])
                for line in recovery_lines(result):
                    print(line)
            return code
        if args.mode == "refresh":
            code, result = refresh_project(args, root)
            if args.output == "json":
                print(json_text(result), end="")
            else:
                label = _("已应用") if result["applied"] else (_("已阻塞") if result["blocked"] else _("仅预览"))
                print(_("{0}：{1}（来自 {2}）").format(label, result["pack"], result["from_version"]))
                for path, text in result.get("diffs", {}).items():  # --diff：只给差异，不再重复完整预览
                    print(text.rstrip("\n") if text else _("（{0} 与新模板没有差异）").format(path))
                for item in [] if result.get("diffs") else result["files"]:
                    print(f"  {pad_label(action_label(item['action']))}{item['path']} [{state_label(item['state'])}]")
                    for note in item.get("note", []):
                        print(f"      {note}")
                    for block in (item.get("merge") or {}).get("conflicts", []):
                        if block["pick"] is None:
                            print(_("      冲突块 {0}：").format(block["id"]))
                            for label, key in ((_("你的版本"), "mine"), (_("基线"), "base"), (_("新模板"), "theirs")):
                                print(f"        [{label}]")
                                print("".join(f"          {line}\n" for line in block[key].splitlines()) or "          \n", end="")
                if result.get("hint") and not result.get("diffs"):
                    print(result["hint"])
                for line in recovery_lines(result):
                    print(line)
            return code
        missing = [flag for flag, value in (("--name", args.name), ("--project-type", args.project_type),
                                            ("--vcs", args.vcs), ("--stack", args.stack),
                                            ("--runtime", args.runtime), ("--agent-cli", args.agent_cli))
                   if not value]
        if missing:
            raise InitError(_("init/migrate 需要：") + ", ".join(missing))
        runtime_status = validate_runtime(args.runtime_plugin_root)
        slug = args.slug or slugify(args.name)
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise InitError("--slug must be lowercase ASCII kebab-case")
        inputs = Inputs(
            root,
            validate_inline("--name", args.name),
            slug,
            tuple(validate_inline("--project-type", value) for value in args.project_type),
            validate_inline("--vcs", args.vcs),
            tuple(validate_inline("--stack", value) for value in args.stack),
            validate_inline("--runtime", args.runtime),
            tuple(validate_inline("--agent-cli", value) for value in args.agent_cli),
            args.work_mode or ('legacy' if args.mode == 'migrate' else 'case-workspace'),
            None if args.no_entry else validate_entry_name(args.entry_name) if args.entry_name
            else DEFAULT_ENTRY if args.mode in ("init", "adopt") else None,
            file_language(args),
        )
        if args.mode == "adopt":
            code, result = _adopt().run(args, root, inputs)
            print(json_text(result), end="") if args.output == "json" else print(_adopt_text(result), end="")
            return code
        files = files_for(inputs)
        if args.mode == "migrate" and (root / PROFILE_PATH).exists():
            profile_file = root / PROFILE_PATH
            if profile_file.is_symlink() or any(parent.is_symlink() for parent in profile_file.parents if parent != root):
                raise InitError("refusing symlinked Project Profile", code="profile-invalid")
            try:
                current_profile = json.loads(profile_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise InitError(_('Project Profile 无法解析'), code='profile-invalid') from exc
            if not isinstance(current_profile, dict) or current_profile.get("schema_version") != "1.0":
                raise InitError("unsupported Project Profile", code="profile-invalid")
            upgraded = configure_work_mode(upgrade_legacy_profile(current_profile), args.work_mode)
            if args.language in ("zh", "en"):
                upgraded["language"] = args.language
            files[PROFILE_PATH] = json_text(upgraded)
            if upgraded.get("work", {}).get("mode") != "case-workspace":
                files.pop(Path("cases/.gitkeep"), None)
        legacy_profile = read_legacy_profile(root)
        retire: list[Path] = []
        if legacy_profile is not None:
            if args.mode == "init":
                raise InitError(
                    _("检测到 v5 Project Profile（{0}），请改用 --mode migrate").format(LEGACY_PROFILE_PATH),
                    code="migrate-required",
                )
            if not (root / PROFILE_PATH).exists():
                upgraded = configure_work_mode(upgrade_legacy_profile(legacy_profile), args.work_mode)
                if args.language in ("zh", "en"):
                    upgraded["language"] = args.language
                files[PROFILE_PATH] = json_text(upgraded)
                if upgraded.get('work', {}).get('mode') != 'case-workspace':
                    files.pop(Path('cases/.gitkeep'), None)
                retire.append(LEGACY_PROFILE_PATH)
        output_profile = json.loads(files[PROFILE_PATH])
        files.pop(Path('cases/.gitkeep'), None)
        if output_profile.get('work', {}).get('mode') == 'case-workspace':
            case_root = (output_profile['work'].get('case') or {}).get('root', 'cases')
            if not isinstance(case_root, str) or not case_root or Path(case_root).is_absolute() or '..' in Path(case_root).parts or '\\' in case_root or re.match(r'^[A-Za-z]:', case_root) or case_root == '.' or set(Path(case_root).parts) & {'.git', '.agents', '.codex', '.cursor', '.claude', 'local-ai', 'confidential'}:
                raise InitError(_('Case root 必须是项目内明确相对目录'), code='profile-invalid')
            files[Path(case_root) / '.gitkeep'] = ''
        requested_skip = {Path(value) for value in args.skip}
        if requested_skip:
            if args.mode == "init":
                raise InitError("--skip is available only in migrate mode")
            unknown_skip = requested_skip - set(files)
            if unknown_skip:
                raise InitError("unknown skeleton path: " + ", ".join(str(path) for path in sorted(unknown_skip, key=str)))
            if PROFILE_PATH in requested_skip:
                raise InitError(f"{PROFILE_PATH} cannot be skipped")
            for path in requested_skip:
                del files[path]
        readme = None
        if args.mode == "init" and not (root / "README.md").exists():
            files[Path("README.md")] = f"# {inputs.name}\n\n{readme_block(inputs.entry_launcher, inputs.language)}"
        elif args.readme_entry:
            readme = readme_plan(root, inputs.name, inputs.entry_launcher, inputs.language)
        create, collisions, symlinked = plan_writes(
            root, files, skip_symlinked_parents=args.mode == "migrate"
        )
        requested_replace = {Path(value) for value in args.replace}
        if retire and PROFILE_PATH in symlinked:
            raise InitError(f"cannot upgrade legacy profile: {PROFILE_PATH} has a symlinked parent")
        linked_replace = requested_replace & set(symlinked)
        if linked_replace:
            raise InitError(
                "refusing to replace through symlinked parent: "
                + ", ".join(str(path) for path in sorted(linked_replace, key=str))
            )
        if args.mode == "init" and requested_replace:
            raise InitError("--replace is available only in migrate mode")
        unknown_replace = requested_replace - set(files) - set(symlinked)
        if unknown_replace:
            raise InitError("unknown skeleton path: " + ", ".join(str(path) for path in sorted(unknown_replace, key=str)))
        missing_replace = requested_replace - set(collisions)
        if missing_replace:
            raise InitError("--replace path does not exist: " + ", ".join(str(path) for path in sorted(missing_replace, key=str)))
        replace = [path for path in collisions if path in requested_replace]
        preserved = [path for path in collisions if path not in requested_replace]
        pack = contract_pack()
        baseline = content_baseline(pack, files, [*create, *replace], inputs.entry_launcher)
        recovery_used: Path | None = None
        if args.mode == "init":
            applied = bool(args.apply and not collisions)
            if applied:
                apply_writes(root, files, create)
            ok = not collisions
        else:
            applied = bool(args.apply)
            if applied:
                recovery_used = apply_migration(root, files, create, replace, args.recovery_dir, retire)
                if retire:
                    try:
                        (root / LEGACY_PROFILE_PATH.parent).rmdir()
                    except OSError:
                        pass
            ok = True
        if applied and inputs.entry_launcher and Path(inputs.entry_launcher) in [*create, *replace]:
            os.chmod(root / inputs.entry_launcher, 0o755)
        if applied and readme:
            _atomic_write(root / readme["path"], readme["text"])
        if applied and pack is not None and baseline:
            write_content_lock(root, pack, baseline, inputs=inputs)
        result = {
            "ok": ok,
            "mode": args.mode,
            "applied": applied,
            "create": [str(path) for path in create],
            "replace": [str(path) for path in replace],
            "preserved_collisions": [str(path) for path in preserved],
            "skipped_symlinked_parents": [str(path) for path in symlinked],
            "skipped_by_request": sorted(str(path) for path in requested_skip),
            "retire": [str(path) for path in retire],
            "recovery": str(recovery_used) if recovery_used is not None and (replace or retire) else None,
            "recovery_default": args.recovery_dir is None if recovery_used is not None else None,
            "recovery_files": [str(path) for path in [*replace, *retire]] if recovery_used is not None else [],
            "profile_upgraded_from": str(LEGACY_PROFILE_PATH) if retire else None,
            "runtime": runtime_status,
            "entry": {"launcher": inputs.entry_launcher, "command": entry_command(inputs.entry_launcher)},
            "readme": readme["action"] if readme else ("create" if Path("README.md") in create else "untouched"),
            "content_lock": {
                "path": str(CONTENT_LOCK_PATH),
                "pack": f"{pack['id']}@{pack['version']}",
                "files": sorted(baseline),
            } if pack is not None and baseline else None,
        }
        if args.output == "json":
            print(json_text(result), end="")
        else:
            label = _("已应用") if applied else (_("已阻塞") if args.mode == "init" and args.apply and collisions else _("仅预览"))
            print(_("{0}：新建 {1}，替换 {2}，保留冲突 {3}").format(label, len(create), len(replace), len(preserved)))
            for path in create:
                print(f"  {_('新建')} {path}")
            for path in replace:
                print(f"  {_('替换')} {path}")
            for path in preserved:
                print(f"  {_('保留')} {path}")
            for path in symlinked:
                print(_("  跳过 {0}（父目录是符号链接）").format(path))
            for path in retire:
                print(_("  退役 {0}（已升级进 {1}）").format(path, PROFILE_PATH))
            for line in recovery_lines(result):
                print(line)
        return 0 if ok else 1
    except InitError as error:
        if args.output == "json":
            print(json_text({"ok": False, "code": error.code, "error": str(error)}), end="")
        else:
            print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
