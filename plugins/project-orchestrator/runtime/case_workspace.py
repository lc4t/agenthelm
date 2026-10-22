#!/usr/bin/env python3
"""Case 目录盘点、检索与已审阅映射的无损迁移；不代替项目状态工具。"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


BLOCKED = {"local-ai", "confidential", ".git", ".agents", "credential"}


class WorkspaceError(ValueError):
    pass


def rename_exclusive(source: Path, target: Path) -> None:
    """原子拒绝覆盖，避免 exists/rename 之间的并发目标碰撞。"""
    if os.name == 'nt':
        os.rename(source, target)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        function, args = libc.renamex_np, (os.fsencode(source), os.fsencode(target), 4)
        function.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        function = libc.renameat2
        args = (-100, os.fsencode(source), -100, os.fsencode(target), 1)
        function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    else:
        raise WorkspaceError('当前平台缺少原子禁止覆盖的 rename')
    if function(*args) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise WorkspaceError('目标在移动时出现，拒绝覆盖')
        raise OSError(code, os.strerror(code))


def safe_path(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise WorkspaceError("非法相对路径")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or "." == value or re.match(r"^[A-Za-z]:", value):
        raise WorkspaceError("路径必须处于项目内")
    if set(relative.parts) & BLOCKED:
        raise WorkspaceError("迁移范围包含受保护路径")
    path = root
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            raise WorkspaceError("拒绝符号链接")
    return path


def case_root_path(root: Path) -> Path:
    profile = root / '.agents/moe.sakanano.agent-pack/project.json'
    for parent in (profile, profile.parent, profile.parent.parent):
        if parent.is_symlink():
            raise WorkspaceError('Project Profile 不允许符号链接')
    name = 'cases'
    if profile.exists():
        if not profile.is_file() or profile.stat().st_size > 512 * 1024:
            raise WorkspaceError('非法 Project Profile')
        fd = os.open(profile, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise WorkspaceError('Project Profile 必须是对象')
        work = data.get('work') or {}
        if not isinstance(work, dict) or not isinstance(work.get('case') or {}, dict):
            raise WorkspaceError('非法 Case 配置')
        name = (work.get('case') or {}).get('root', name)
    return safe_path(root, name)


def opaque(path: Path) -> bool:
    name = path.name.lower()
    return (name == ".env" or name.startswith(".env.") or name in {"id_rsa", "id_ed25519", "credentials"}
            or any(token in name for token in ("secret", "credential", "cookie", "token", "private-key"))
            or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"})


def digest_file(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise WorkspaceError("拒绝非普通文件")
        h = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
        return h.hexdigest()


def inventory(path: Path) -> dict:
    if path.is_symlink():
        raise WorkspaceError("拒绝符号链接")
    if not path.exists() or not (path.is_dir() or path.is_file()):
        raise WorkspaceError("源目录或文件不存在")
    files = []
    directories = []
    traversal = os.walk(path, followlinks=False) if path.is_dir() else [(path.parent, [], [path.name])]
    for parent, dirs, names in traversal:
        for name in sorted(dirs):
            child = Path(parent) / name
            if child.is_symlink():
                raise WorkspaceError("目录中存在符号链接")
            directories.append(child.relative_to(path).as_posix())
        for name in sorted(names):
            child = Path(parent) / name
            meta = child.lstat()
            if not stat.S_ISREG(meta.st_mode):
                raise WorkspaceError("目录中存在非普通文件")
            item = {"path": child.relative_to(path).as_posix() if path.is_dir() else '.', "bytes": meta.st_size}
            private_ancestor = any(opaque(part) or part.name in BLOCKED for part in (child, *child.parents) if part != path.parent and path.parent in part.parents)
            if private_ancestor or opaque(path):
                item.update(opaque=True, inode=meta.st_ino, device=meta.st_dev)
            else:
                item["sha256"] = digest_file(child)
            files.append(item)
    files.sort(key=lambda item: item["path"])
    payload = {"files": files, "directories": sorted(directories)}
    return {**payload, "file_count": len(files), "bytes": sum(item["bytes"] for item in files),
            "opaque_files": sum(item.get("opaque", False) for item in files),
            "fingerprint": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}


def plan_migration(root: Path, mapping: dict) -> dict:
    if not isinstance(mapping, dict) or mapping.get("schema_version") != "1.0":
        raise WorkspaceError("映射必须声明 schema_version=1.0")
    moves = mapping.get("moves")
    records = mapping.get("records", [])
    if not isinstance(moves, list) or not moves or not isinstance(records, list):
        raise WorkspaceError("映射需要非空 moves 数组")
    source_paths, target_paths, planned = [], [], []
    case_parts = case_root_path(root).relative_to(root).parts
    for move in moves:
        if not isinstance(move, dict):
            raise WorkspaceError("非法映射项")
        source = safe_path(root, move.get("source", ""))
        target = safe_path(root, move.get("target", ""))
        relative = target.relative_to(root)
        if len(source.relative_to(root).parts) < 2 or len(relative.parts) < len(case_parts) + 1 or relative.parts[:len(case_parts)] != case_parts:
            raise WorkspaceError("源需要明确子目录，目标需要位于 cases 的工作目录")
        if target.exists():
            raise WorkspaceError("目标已存在，拒绝合并或覆盖")
        manifest = inventory(source)
        if move.get("fingerprint") and move["fingerprint"] != manifest["fingerprint"]:
            raise WorkspaceError("预演后源文件发生变化")
        source_paths.append(source)
        target_paths.append(target)
        planned.append({"source": move["source"], "target": move["target"], "manifest": manifest})
    for collection in (source_paths, target_paths):
        for index, path in enumerate(collection):
            for other in collection[index + 1:]:
                if path == other or path in other.parents or other in path.parents:
                    raise WorkspaceError("映射存在重复或嵌套目录")
    for source in source_paths:
        for target in target_paths:
            if source == target or source in target.parents or target in source.parents:
                raise WorkspaceError("源与目标重叠")
    record_paths = []
    for record in records:
        path = safe_path(root, record.get("path", ""))
        parts = path.relative_to(root).parts
        content = record.get("content")
        if len(parts) != len(case_parts) + 2 or parts[:len(case_parts)] != case_parts or parts[-1] != "CASE.md" or not isinstance(content, str):
            raise WorkspaceError("新增记录只能是完整 CASE.md")
        if path.exists() or path in record_paths or len(content.encode()) > 2 * 1024 * 1024:
            raise WorkspaceError("新增记录冲突或超出大小限制")
        if path.parent not in target_paths:
            raise WorkspaceError("新增 CASE.md 必须属于本次迁移目录")
        source = source_paths[target_paths.index(path.parent)]
        if not source.is_dir() or (source / "CASE.md").exists():
            raise WorkspaceError("源已有 CASE.md，请使用项目工具维护现有记录")
        record_paths.append(path)
    return {"ok": True, "applied": False, "moves": planned, "records": [r["path"] for r in records],
            "file_count": sum(item["manifest"]["file_count"] for item in planned),
            "bytes": sum(item["manifest"]["bytes"] for item in planned),
            "opaque_files": sum(item["manifest"]["opaque_files"] for item in planned)}


def migrate(root: Path, mapping: dict, *, apply: bool = False) -> dict:
    result = plan_migration(root, mapping)
    if not apply:
        return result
    moved, created, directories = [], [], []
    try:
        for move in result["moves"]:
            source = safe_path(root, move["source"])
            target = safe_path(root, move["target"])
            # 再核对源指纹，防止预演后并发变动被静默接纳。
            if inventory(source) != move["manifest"]:
                raise WorkspaceError("源目录在应用前发生变化")
            missing = []
            parent = target.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for parent in reversed(missing):
                parent.mkdir()
                directories.append(parent)
            if target.exists():
                raise WorkspaceError("目标在应用前出现")
            if source.stat().st_dev != target.parent.stat().st_dev:
                raise WorkspaceError("跨文件系统移动不受支持")
            rename_exclusive(source, target)
            moved.append((source, target))
            if inventory(target) != move["manifest"]:
                raise WorkspaceError("移动后内容核验失败")
        for record in mapping.get("records", []):
            path = safe_path(root, record["path"])
            with path.open("x", encoding="utf-8") as stream:
                created.append(path)
                stream.write(record["content"])
                stream.flush()
                os.fsync(stream.fileno())
        result["applied"] = True
        return result
    except BaseException:
        for path in reversed(created):
            path.unlink()
        for source, target in reversed(moved):
            if source.exists():
                raise WorkspaceError("回滚源路径冲突，请按映射人工恢复")
            rename_exclusive(target, source)
        for directory in reversed(directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise


def metadata(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise WorkspaceError("非法或过大的 Case 记录")
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'r', encoding='utf-8') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise WorkspaceError('Case 记录必须是普通文件')
        source = stream.read(2 * 1024 * 1024 + 1)
    if len(source.encode()) > 2 * 1024 * 1024:
        raise WorkspaceError('Case 记录过大')
    if not source.startswith("---\n") or "\n---" not in source[4:]:
        raise WorkspaceError("Case 缺少 Frontmatter")
    header = source.split("\n---", 1)[0][4:]
    data, key = {}, None
    for line in header.splitlines():
        if line.startswith("  - ") and key:
            raw = line[4:].strip()
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw.strip("\"'")
            if not isinstance(data[key], list):
                raise WorkspaceError("非法数组字段")
            data[key].append(value)
        elif re.match(r"^[a-z_]+:", line):
            key, raw = line.split(":", 1)
            if key in data:
                raise WorkspaceError("Case 存在重复字段")
            raw = raw.strip()
            try:
                data[key] = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                data[key] = raw.strip("\"'")
    if not all(isinstance(data.get(k), str) and data[k] for k in ("id", "title", "status")):
        raise WorkspaceError("Case 缺少身份与状态")
    return data


def inspect_cases(root: Path, terms: list[str] | None = None) -> dict:
    case_root = case_root_path(root)
    cases, invalid = [], []
    if not case_root.exists():
        return {"ok": True, "cases": [], "invalid": []}
    words = [term.casefold() for term in terms or []]
    for directory in sorted(case_root.iterdir()):
        if directory.is_symlink():
            invalid.append({"path": directory.relative_to(root).as_posix(), "error": "符号链接"})
            continue
        if not directory.is_dir():
            continue
        if opaque(directory) or directory.name in BLOCKED:
            invalid.append({'path': directory.relative_to(root).as_posix(), 'error': '隐私路径'})
            continue
        try:
            record = directory / "CASE.md"
            data = metadata(record)
            if data['id'] != directory.name or data.get('sensitivity', 'shared') != 'shared':
                raise WorkspaceError('Case 身份或隐私位置不一致')
            files = []
            size = 0
            for parent, dirs, names in os.walk(directory, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink() and name not in BLOCKED and not opaque(Path(name))]
                for name in names:
                    path = Path(parent) / name
                    if path.is_symlink() or not path.is_file() or opaque(path):
                        continue
                    files.append(path.relative_to(directory).as_posix())
                    size += path.stat().st_size
            haystack = " ".join(str(data.get(key, "")) for key in ("title", "id", "aliases", "legacy_ids", "tags", "goal", "related_projects"))
            haystack = (haystack + " " + " ".join(files)).casefold()
            matches = [word for word in words if word in haystack]
            if words and len(matches) != len(words):
                continue
            cases.append({"path": record.relative_to(root).as_posix(),
                          **{key: data.get(key, []) for key in ("id", "title", "status", "legacy_ids", "aliases", "tags", "deliverables")},
                          "file_count": len(files), "bytes": size, "matched_terms": matches})
        except (OSError, ValueError) as error:
            invalid.append({"path": directory.relative_to(root).as_posix(), "error": type(error).__name__})
    return {"ok": not invalid, "cases": cases, "invalid": invalid}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "search", "migrate"))
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--map", type=Path)
    parser.add_argument("--term", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.project.is_symlink() or not args.project.is_dir():
            raise WorkspaceError("项目根目录必须是实际目录")
        root = args.project.resolve()
        if args.command == "migrate":
            if not args.map or args.map.is_symlink():
                raise WorkspaceError("迁移需要普通映射文件")
            result = migrate(root, json.loads(args.map.read_text(encoding="utf-8")), apply=args.apply)
        else:
            if args.apply or args.map:
                raise WorkspaceError("只读命令不支持 apply 或 map")
            result = inspect_cases(root, args.term if args.command == "search" else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
