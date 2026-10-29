#!/usr/bin/env python3
"""使用 agents-init v6 中文模板安全创建或迁移项目骨架。"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


INITIALIZER_VERSION = "6.8.0"
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
                        "cases", "docs", "memory", ".agents", ".git"}
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
    ".agent-doc/plan.md",
    ".agent-doc/progress.md",
    ".agent-doc/chat-summary.md",
    "docs/refs/README.md",
}


class InitError(Exception):
    """Expected initialization failure."""

    def __init__(self, message: str, *, code: str = "invalid-request") -> None:
        super().__init__(message)
        self.code = code


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
        raise InitError(f"无法读取中文模板：{path}", code="template-invalid") from exc
    templates = {match.group("name"): match.group("body") for match in TEMPLATE_PATTERN.finditer(source)}
    missing = sorted(REQUIRED_TEMPLATES - set(templates))
    if missing:
        raise InitError("中文模板缺少生成区块：" + ", ".join(missing), code="template-invalid")
    return templates


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
        raise InitError(f"Plugin manifest 无法解析：{path}", code="runtime-required") from exc
    if not isinstance(manifest, dict):
        raise InitError(f"Plugin manifest 根节点必须是对象：{path}", code="runtime-required")
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
        raise InitError(f"缺少 agents-init Plugin manifest：{root / 'plugin.json'}", code="runtime-required")
    if _read_manifest(root / "plugin.json").get("name") != "agents-init":
        raise InitError("Plugin manifest 必须声明 name=agents-init", code="runtime-required")
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
            raise InitError(f"同包 Plugin manifest 必须声明 name={name}", code="runtime-required")
        versions[name] = str(manifest.get("version", ""))
    if missing:
        raise InitError(
            "缺少必需的 project-orchestrator / agent-pack / opinion-manager 文件："
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
        raise InitError(f"legacy profile 无法解析：{LEGACY_PROFILE_PATH}", code="profile-invalid") from exc
    if not isinstance(profile, dict) or profile.get("schema_version") != "1.0":
        raise InitError("legacy profile 必须是 schema_version=1.0 的对象", code="profile-invalid")
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
        raise InitError('work 配置必须是对象', code='profile-invalid')
    if mode is None:
        return profile
    work = dict(profile.get('work') or {})
    if any(work.get(key) is not None and not isinstance(work[key], dict) for key in ('task', 'case')):
        raise InitError('task/case 配置必须是对象或 null', code='profile-invalid')
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
        raise InitError(f"{label} 不能为空")
    if len(value) > 512 or any(ord(character) < 32 for character in value):
        raise InitError(f"{label} 只能包含单行可打印文本")
    return value.strip()


def validate_entry_name(name: str) -> str:
    if not ENTRY_NAME_RE.fullmatch(name) or name in RESERVED_ENTRY_NAMES:
        raise InitError(f"启动器名称不合法：{name}（只能用字母、数字、. _ -，且不能与骨架文件同名）")
    return name


def launcher_text() -> str:
    try:
        return LAUNCHER_TEMPLATE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InitError(f"无法读取启动器模板：{LAUNCHER_TEMPLATE}", code="template-invalid") from exc


def entry_command(launcher: str | None) -> str:
    return f"./{launcher}" if launcher else "agenthelm project"


def readme_block(launcher: str | None) -> str:
    command = entry_command(launcher)
    return (f"{README_START}\n## 怎样使用\n\n在项目根目录运行 `{command}` 打开项目菜单：找资料、看进展、项目指南、检查与维护。"
            f"不需要 AI 客户端，只需要 Python 3.11+；第一次运行如果提示缺少运行时，按提示安装一次即可。\n\n"
            f"不进菜单也可以：`{command} find 关键词`、`{command} progress`、`{command} --help`。\n{README_END}\n")


def tracked_paths(pack: dict | None, launcher: str | None) -> list[Path]:
    """Files the content lock follows: the pack's fixed list plus this project's launcher."""
    tracked = [Path(path) for path in (pack or {}).get("materializes", [])]
    return tracked + ([Path(launcher)] if launcher else [])


def files_for(inputs: Inputs, templates: dict[str, str] | None = None) -> dict[Path, str]:
    templates = templates or load_templates()
    profile = {
        "$schema": PROFILE_SCHEMA,
        "schema_version": "1.0",
        "initializer_version": INITIALIZER_VERSION,
        "name": inputs.name,
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
        "privacy": {"forbidden_default_reads": [], "generated_outputs": []},
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
        Path("docs/refs/README.md"): render_template(templates["docs/refs/README.md"], replacements),
        Path("docs/drafts/.gitkeep"): "",
    }
    if inputs.work_mode == "case-workspace":
        files[Path("cases/.gitkeep")] = ""
    if inputs.entry_launcher:
        files[Path(inputs.entry_launcher)] = launcher_text()
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
) -> None:
    create_paths = list(create)
    replace_paths = list(replace)
    retire_paths = list(retire)
    if (replace_paths or retire_paths) and recovery_dir is None:
        raise InitError("--recovery-dir is required when --replace is used or a legacy profile is retired")
    recovery = recovery_dir.expanduser().resolve() if recovery_dir is not None else None
    if recovery is not None:
        if recovery == root or root in recovery.parents:
            raise InitError("--recovery-dir must be outside the project root")
        recovery.mkdir(parents=True, exist_ok=True, mode=0o700)
        if recovery.is_symlink() or not recovery.is_dir():
            raise InitError("--recovery-dir must be a real directory")
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
            "work_mode": inputs.work_mode, "entry_launcher": inputs.entry_launcher}


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
            raise InitError(f"content lock 无法解析：{CONTENT_LOCK_PATH}", code="profile-invalid") from exc
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
    lock["packs"][pack["id"]] = updated
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json_text(lock), encoding="utf-8")
    os.replace(temporary, path)


def _read_project_json(root: Path, relative: Path, label: str) -> dict:
    path = root / relative
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if root in parent.parents):
        raise InitError(f"refusing symlinked {label}", code="profile-invalid")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InitError(f"缺少 {relative}", code="refresh-unavailable") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InitError(f"{label} 无法解析：{relative}", code="profile-invalid") from exc
    if not isinstance(data, dict):
        raise InitError(f"{label} 根节点必须是对象", code="profile-invalid")
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


def _unified_diff(relative: str, current: str, new: str, limit: int = 200) -> str:
    lines = list(difflib.unified_diff(
        current.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{relative}", tofile=f"b/{relative}",
    ))
    if len(lines) > limit:
        lines = lines[:limit] + [f"... ({len(lines) - limit} more diff lines)\n"]
    return "".join(lines)


def refresh_project(args: argparse.Namespace, root: Path) -> tuple[int, dict]:
    """Re-render the tracked template files after a content pack update.

    Files still identical to their install-time baseline are replaced; files the
    user edited are only replaced with --accept or kept with --keep. Every
    replaced file is copied to --recovery-dir first.
    """
    pack = contract_pack()
    if pack is None:
        raise InitError("当前 agents-init 未声明可刷新的内容包", code="refresh-unavailable")
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
    inputs = inputs_from_profile(root)
    rendered = files_for(inputs)
    accept = {Path(value) for value in args.accept}
    keep = {Path(value) for value in args.keep}
    tracked = tracked_paths(pack, inputs.entry_launcher)
    unknown = (accept | keep) - set(tracked)
    if unknown:
        raise InitError("不是内容包跟踪的文件：" + ", ".join(sorted(map(str, unknown))))
    if accept & keep:
        raise InitError("同一文件不能同时 --accept 与 --keep：" + ", ".join(sorted(map(str, accept & keep))))
    plan: list[dict] = []
    kept_before = _scaffold().keep_fingerprints(root)
    for relative in tracked:
        new = rendered[relative]
        new_digest = hashlib.sha256(new.encode("utf-8")).hexdigest()
        path = root / relative
        if path.is_symlink():
            raise InitError(f"refusing symlinked file: {relative}")
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        current_digest = hashlib.sha256(current.encode("utf-8")).hexdigest() if current is not None else None
        if current_digest == new_digest:
            state, action = "unchanged", "keep"
        elif current is None:
            state = "missing"
            action = "create" if relative in accept else "skip"
        elif new_digest == baseline.get(relative.as_posix()):
            # The new template renders this file exactly as before; the user's edits stay untouched.
            state, action = "local-only", "keep"
        elif current_digest == baseline.get(relative.as_posix()):
            state, action = "clean", "keep" if relative in keep else "replace"
        else:
            state = "modified"
            decided = kept_before.get(relative.as_posix()) == new_digest  # 之前选过保留，且期望内容没变
            action = "replace" if relative in accept else "keep" if relative in keep or decided else "blocked"
            if decided and relative not in accept:
                state = "kept-by-decision"
        item = {"path": relative.as_posix(), "state": state, "action": action, "new_sha256": new_digest}
        if action in {"replace", "blocked", "create"} or (state == "clean" and action == "keep"):
            item["diff"] = _unified_diff(relative.as_posix(), current or "", new)
        plan.append(item)
    blocked = [item["path"] for item in plan if item["action"] == "blocked"]
    replacing = [Path(item["path"]) for item in plan if item["action"] == "replace"]
    result = {
        "ok": not blocked,
        "mode": "refresh",
        "applied": False,
        "pack": f"{pack['id']}@{pack['version']}",
        "from_version": entry.get("version"),
        "files": plan,
        "blocked": blocked,
    }
    if blocked:
        result["hint"] = "本地已改动的文件需逐个选择：--accept <文件> 用新模板替换（先备份），或 --keep <文件> 保留本地"
    if not args.apply or blocked:
        return (0 if not blocked else 1), result
    if replacing and args.recovery_dir is None:
        raise InitError("--recovery-dir is required when refresh replaces files")
    if replacing:
        recovery = args.recovery_dir.expanduser().resolve()
        if recovery == root or root in recovery.parents:
            raise InitError("--recovery-dir must be outside the project root")
        for relative in replacing:
            archived = recovery / relative
            if archived.exists():
                raise InitError(f"recovery collision: {relative}")
            archived.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            archived.write_bytes((root / relative).read_bytes())
        result["recovery"] = str(recovery)
    for item in plan:
        relative = Path(item["path"])
        if item["action"] in {"replace", "create"}:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            temporary.write_text(rendered[relative], encoding="utf-8")
            if relative == Path(inputs.entry_launcher or "\0"):
                temporary.chmod(0o755)
            os.replace(temporary, target)
    files = dict(baseline)
    for item in plan:
        if item["action"] in {"replace", "create"} or item["state"] == "unchanged":
            files[item["path"]] = item["new_sha256"]
        # Files kept on purpose keep their old baseline: the tool did not write them.
    lock.setdefault("packs", {})[pack["id"]] = {**entry, "plugin": "agents-init", "version": pack["version"],
                                               "files": dict(sorted(files.items())), "render_inputs": render_inputs(inputs)}
    lock_path = root / CONTENT_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = lock_path.with_name(f".{lock_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json_text(lock), encoding="utf-8")
    os.replace(temporary, lock_path)
    # 显式 --keep 的选择记入决定文件：同样的模板内容以后不再追问，模板实质变化时才再问。
    for item in plan:
        if Path(item["path"]) in keep and item["state"] in {"modified", "local-only", "kept-by-decision"}:
            _scaffold().decide(root, f"content:{item['path']}", "keep", reason="refresh --keep",
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
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    if mode is not None:
        temporary.chmod(mode)
    os.replace(temporary, path)


def readme_plan(root: Path, name: str, launcher: str | None) -> dict | None:
    """README.md: create it with the entry section, or append the section once (marked)."""
    path = root / "README.md"
    if path.is_symlink():
        raise InitError("refusing symlinked README.md")
    if not path.exists():
        return {"path": "README.md", "action": "create", "text": f"# {name}\n\n{readme_block(launcher)}"}
    current = path.read_text(encoding="utf-8")
    if README_START in current:
        return None
    return {"path": "README.md", "action": "append",
            "text": current.rstrip("\n") + "\n\n" + readme_block(launcher)}


def add_entry(args: argparse.Namespace, root: Path) -> tuple[int, dict]:
    """Give an initialised project its root launcher and Profile ``entry`` block.

    Only the Profile, the launcher, the content lock (and README.md with
    --readme-entry) are touched; other template files follow the normal refresh.
    An existing file with the launcher's name is the user's: it is kept or
    replaced only by explicit --keep / --accept, replacements backed up first.
    """
    if not args.entry_name:
        raise InitError("--mode entry 需要 --entry-name")
    name = validate_entry_name(args.entry_name)
    profile = _read_project_json(root, PROFILE_PATH, "Project Profile")
    entry = profile.get("entry") if isinstance(profile.get("entry"), dict) else {}
    if entry.get("launcher") and entry["launcher"] != name:
        raise InitError(f"项目已声明入口 ./{entry['launcher']}；改名请同时修改 Profile 的 entry.launcher 与文件名")
    template = launcher_text()
    target = root / name
    if target.is_symlink():
        raise InitError(f"refusing symlinked launcher: {name}")
    current = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else None
    if target.exists() and not target.is_file():
        raise InitError(f"{name} 已存在且不是文件")
    chosen = {Path(value) for value in args.accept} | {Path(value) for value in args.keep}
    if chosen - {Path(name)}:
        raise InitError("--mode entry 只接受启动器文件的 --accept / --keep：" + name)
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
    readme = readme_plan(root, str(profile.get("name") or root.name), name) if args.readme_entry else None
    writes = ([name] if action in ("create", "replace") else []) + ([str(PROFILE_PATH)] if profile_changed else []) \
        + ([readme["path"]] if readme else [])
    if action in ("create", "replace", "unchanged"):
        writes.append(str(CONTENT_LOCK_PATH))
    labels = {"create": "新建启动器", "replace": "用模板替换（先备份）", "unchanged": "已是模板内容",
              "keep": "保留你的文件（不跟踪更新）", "blocked": "已存在同名文件，需要选择"}
    changes = [f"{name}：{labels[action]}"]
    if profile_changed:
        changes.append(f"Project Profile：加入 entry（api {new_profile['entry']['api']}，launcher {name}）")
    if readme:
        changes.append("README.md：" + ("新建并写入「怎样使用」" if readme["action"] == "create" else "追加「怎样使用」一节"))
    result = {
        "ok": action != "blocked", "mode": "entry", "applied": False,
        "entry": {"name": name, "action": action, "writes": writes, "changes": changes,
                  "collisions": [name] if action == "blocked" else [],
                  "profile_diff": _unified_diff(str(PROFILE_PATH), json_text(profile), json_text(new_profile))
                  if profile_changed else ""},
        "blocked": [name] if action == "blocked" else [],
        "readme_snippet": readme_block(name) if not readme else "",
    }
    if action == "blocked":
        result["hint"] = f"{name} 已存在：--accept {name} 用模板替换（先备份到 --recovery-dir），或 --keep {name} 保留你的文件"
    if not args.apply or action == "blocked":
        return (0 if action != "blocked" else 1), result
    if action == "replace":
        if args.recovery_dir is None:
            raise InitError("--recovery-dir is required when replacing the launcher")
        recovery = args.recovery_dir.expanduser().resolve()
        if recovery == root or root in recovery.parents:
            raise InitError("--recovery-dir must be outside the project root")
        archived = recovery / name
        if archived.exists():
            raise InitError(f"recovery collision: {name}")
        archived.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        archived.write_bytes(target.read_bytes())
        result["recovery"] = str(recovery)
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
        lines.append("Scan（只读）")
    else:
        lines.append(f"{'Applied' if result.get('applied') else 'Dry-run'}: {result['mode']}")
    for blocker in result.get("blockers", []):
        lines.append(f"  blocked [{blocker['code']}] {blocker['message']}")
    scan = result.get("scan", result)
    for item in scan.get("findings", []):
        lines.append(f"  [{item['id']}] {item['summary']}")
        for option in item["options"]:
            mark = "（推荐）" if option["id"] == item["recommended"] else ""
            lines.append(f"      - {option['label']}{mark}" + (f"  {option['flag']}" if option["flag"] else ""))
    for key, title in (("create", "create"), ("preserve", "preserve")):
        for path in result.get(key, []):
            lines.append(f"  {title} {path}")
    for item in result.get("modify", []):
        lines.append(f"  {item['action']} {item['path']}" + (f"（区块 {item['region']}）" if item.get("region") else ""))
    for note in result.get("notes", []):
        lines.append(f"  note: {note}")
    if result.get("needs_recovery_dir") and not result.get("applied"):
        lines.append("  改动已有文件需要 --recovery-dir（项目外的备份目录）")
    if scan.get("recommended_flags"):
        lines.append("  推荐选项：" + " ".join(scan["recommended_flags"]))
    if result.get("check_after"):
        lines.append("  接管后检查：" + ("一致" if result["check_after"]["ok"] else "仍有 " + "、".join(result["check_after"]["findings"])))
    return "\n".join(lines) + "\n"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--project", type=Path, required=True)
    result.add_argument("--name")
    result.add_argument("--slug")
    result.add_argument("--project-type", type=split_csv)
    result.add_argument("--vcs")
    result.add_argument("--stack", type=split_csv)
    result.add_argument("--runtime")
    result.add_argument("--agent-cli", type=split_csv)
    result.add_argument("--mode", choices=("init", "migrate", "refresh", "entry", "check", "add-skeleton", "profile",
                                           "repair-lock", "decide", "case-naming", "scan", "adopt", "region"),
                        default="init",
                        help="refresh：内容包更新后按 Profile 重新渲染已跟踪的模板文件；"
                             "entry：为已初始化的项目补上根目录启动器与 Profile 的 entry 段；"
                             "check：只读，对比期望骨架清单；add-skeleton：按模板补上缺失的骨架文件；"
                             "profile：把缺失或不符的受检字段改为模板值；repair-lock：修复内容锁的 0.0.0 版本；"
                             "decide：记录或撤销对某个发现的决定；case-naming：只输出 Case 目录改名计划与受影响的引用，不移动任何目录；"
                             "scan：只读，扫描已有内容与骨架的冲突并给出选项和推荐；"
                             "adopt：接管非空目录（已有文件原样保留，只补骨架；改动已有文件须显式选择并备份）；"
                             "region：更新受管区块（--accept / --keep <文件>#<区块ID>）")
    result.add_argument("--target", action="append", default=[],
                        help="add-skeleton：骨架路径；profile：字段；decide：发现 ID（可重复；省略则处理全部可处理项）")
    result.add_argument("--decision", choices=("skip", "keep", "later", "unskip"), help="decide：决定；unskip 撤销")
    result.add_argument("--reason", help="decide：理由（建议写，随项目同步）")
    result.add_argument("--fingerprint", help="decide：期望指纹（缺省取该发现当前的指纹）")
    result.add_argument("--reprompt", action="store_true", help="check：忽略已记录的决定，重新评估全部发现")
    result.add_argument("--state-dir", type=Path, help="机器本地决定的状态目录（缺省 $XDG_STATE_HOME 或 ~/.local/state）")
    result.add_argument("--entry-name", help=f"根目录启动器文件名（init 缺省 {DEFAULT_ENTRY}；entry 模式必填）")
    result.add_argument("--no-entry", action="store_true", help="init：不生成根目录启动器")
    result.add_argument("--readme-entry", action="store_true",
                        help="在已有 README.md 末尾追加「怎样使用」一节（带标记，只追加一次）；init 时 README 不存在会直接新建")
    result.add_argument("--accept", action="append", default=[], help="refresh：用新模板替换这个本地已改动的文件")
    result.add_argument("--keep", action="append", default=[], help="refresh：保留这个文件的本地内容")
    result.add_argument("--work-mode", choices=("case-workspace", "legacy"), default=None,
                        help="新项目默认 case-workspace；既有项目只在显式选择后更改模式")
    result.add_argument("--replace", action="append", default=[], help="known skeleton path to replace in migrate mode")
    result.add_argument("--skip", action="append", default=[], help="known skeleton path the project does not want (migrate mode)")
    result.add_argument("--recovery-dir", type=Path, help="outside-project backup directory required for replacements")
    result.add_argument("--agents-policy", choices=("keep", "append", "split", "side-by-side"), default=None,
                        help="adopt：已有 AGENTS.md 怎么处理（缺省 keep，不改动）")
    result.add_argument("--claude-policy", choices=("keep", "add-import"), default=None,
                        help="adopt：已有 CLAUDE.md 没导入 AGENTS.md 时是否追加导入区块（缺省 keep）")
    result.add_argument("--route", action="append", default=[],
                        help="adopt split：<章节序号>=<agents|rules|user|opinion|sop>，未指定的章节留在 AGENTS.md")
    result.add_argument("--register-root", action="append", default=[],
                        help="adopt：把已有的工作容器目录登记到 work.assets.roots（只读登记，不动内容）")
    result.add_argument("--case-root", help="adopt：新 Case 放的目录（缺省 cases）")
    result.add_argument("--memory-root", help="adopt：记忆条目放的目录（缺省 memory）")
    result.add_argument(
        "--runtime-plugin-root",
        type=Path,
        default=PLUGIN_ROOT,
        help=(
            "agents-init Plugin 根目录（同级查找 project-orchestrator、agent-pack、"
            "opinion-manager），或含 plugins/ 的仓库根目录"
        ),
    )
    result.add_argument("--apply", action="store_true", help="create the complete skeleton only when no collision exists")
    result.add_argument("--output", choices=("text", "json"), default="text")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        root = validate_root(args.project)
        if args.mode == "scan":
            placeholder = Inputs(root, args.name or root.name, "project", tuple(args.project_type or ("code",)),
                                 args.vcs or "git", tuple(args.stack or ("unknown",)), args.runtime or "local",
                                 tuple(args.agent_cli or ("codex",)), args.work_mode or "case-workspace",
                                 None if args.no_entry else validate_entry_name(args.entry_name or DEFAULT_ENTRY))
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
        if args.mode in ("check", "add-skeleton", "profile", "repair-lock", "decide", "case-naming"):
            scaffold = _scaffold()
            code, result = scaffold.run_mode(args, root)
            print(json_text(result), end="") if args.output == "json" else print(scaffold.render_text(result), end="")
            return code
        if args.mode == "entry":
            code, result = add_entry(args, root)
            if args.output == "json":
                print(json_text(result), end="")
            else:
                print(("Applied" if result["applied"] else "Blocked" if result["blocked"] else "Dry-run")
                      + f": entry ./{result['entry']['name']}")
                for change in result["entry"]["changes"]:
                    print(f"  {change}")
                if result.get("hint"):
                    print(result["hint"])
                if result.get("readme_snippet"):
                    print("README 里可以加上：\n" + result["readme_snippet"])
            return code
        if args.mode == "refresh":
            code, result = refresh_project(args, root)
            if args.output == "json":
                print(json_text(result), end="")
            else:
                label = "Applied" if result["applied"] else ("Blocked" if result["blocked"] else "Dry-run")
                print(f"{label}: {result['pack']} (from {result['from_version']})")
                for item in result["files"]:
                    print(f"  {item['action']:8} {item['path']} [{item['state']}]")
                if result.get("hint"):
                    print(result["hint"])
            return code
        missing = [flag for flag, value in (("--name", args.name), ("--project-type", args.project_type),
                                            ("--vcs", args.vcs), ("--stack", args.stack),
                                            ("--runtime", args.runtime), ("--agent-cli", args.agent_cli))
                   if not value]
        if missing:
            raise InitError("init/migrate 需要：" + ", ".join(missing))
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
                raise InitError('Project Profile 无法解析', code='profile-invalid') from exc
            if not isinstance(current_profile, dict) or current_profile.get("schema_version") != "1.0":
                raise InitError("unsupported Project Profile", code="profile-invalid")
            upgraded = configure_work_mode(upgrade_legacy_profile(current_profile), args.work_mode)
            files[PROFILE_PATH] = json_text(upgraded)
            if upgraded.get("work", {}).get("mode") != "case-workspace":
                files.pop(Path("cases/.gitkeep"), None)
        legacy_profile = read_legacy_profile(root)
        retire: list[Path] = []
        if legacy_profile is not None:
            if args.mode == "init":
                raise InitError(
                    f"检测到 v5 Project Profile（{LEGACY_PROFILE_PATH}），请改用 --mode migrate",
                    code="migrate-required",
                )
            if not (root / PROFILE_PATH).exists():
                upgraded = configure_work_mode(upgrade_legacy_profile(legacy_profile), args.work_mode)
                files[PROFILE_PATH] = json_text(upgraded)
                if upgraded.get('work', {}).get('mode') != 'case-workspace':
                    files.pop(Path('cases/.gitkeep'), None)
                retire.append(LEGACY_PROFILE_PATH)
        output_profile = json.loads(files[PROFILE_PATH])
        files.pop(Path('cases/.gitkeep'), None)
        if output_profile.get('work', {}).get('mode') == 'case-workspace':
            case_root = (output_profile['work'].get('case') or {}).get('root', 'cases')
            if not isinstance(case_root, str) or not case_root or Path(case_root).is_absolute() or '..' in Path(case_root).parts or '\\' in case_root or re.match(r'^[A-Za-z]:', case_root) or case_root == '.' or set(Path(case_root).parts) & {'.git', '.agents', '.codex', '.cursor', '.claude', 'local-ai', 'confidential'}:
                raise InitError('Case root 必须是项目内明确相对目录', code='profile-invalid')
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
            files[Path("README.md")] = f"# {inputs.name}\n\n{readme_block(inputs.entry_launcher)}"
        elif args.readme_entry:
            readme = readme_plan(root, inputs.name, inputs.entry_launcher)
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
        if args.mode == "init":
            applied = bool(args.apply and not collisions)
            if applied:
                apply_writes(root, files, create)
            ok = not collisions
        else:
            applied = bool(args.apply)
            if applied:
                apply_migration(root, files, create, replace, args.recovery_dir, retire)
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
            label = "Applied" if applied else ("Blocked" if args.mode == "init" and args.apply and collisions else "Dry-run")
            print(label + f": {len(create)} create, {len(replace)} replace, {len(preserved)} preserved collision(s)")
            for path in create:
                print(f"  create {path}")
            for path in replace:
                print(f"  replace {path}")
            for path in preserved:
                print(f"  preserve {path}")
            for path in symlinked:
                print(f"  skip {path} (symlinked parent)")
            for path in retire:
                print(f"  retire {path} (upgraded into {PROFILE_PATH})")
        return 0 if ok else 1
    except InitError as error:
        if args.output == "json":
            print(json_text({"ok": False, "code": error.code, "error": str(error)}), end="")
        else:
            print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
