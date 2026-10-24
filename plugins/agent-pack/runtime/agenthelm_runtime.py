"""agenthelm 用户级运行时：把四个插件的确定性工具装进用户数据目录，日常只需要 Python 3.11+。

布局（``$XDG_DATA_HOME`` 缺省为 ``~/.local/share``）::

    agenthelm/runtimes/<发布版本>/   index.json、plugins/<插件>/、bin/agenthelm、runtime.json
    agenthelm/current -> runtimes/<发布版本>

项目根启动器先找 ``current/bin/agenthelm``，所以装好后打开菜单不联网、不经过 npx。
同一版本装好后不再改写；新版本装在旁边并切换 ``current``，旧版本保留，可用 ``use`` 切回。
来源三选一：本命令所在的发布内容（npm 包、发布包或仓库检出）、``--from-release``
（联网下载发布包并核对摘要）、``--source``（本地目录或 .tar.gz，离线用）。
写操作默认只预览，``--apply`` 才执行。只在 macOS 与 Linux 上验证过。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PLUGINS = ("agent-pack", "agents-init", "opinion-manager", "project-orchestrator")
SKIP_PARTS = {"tests", "__pycache__", ".DS_Store", "node_modules"}
MANIFEST = "runtime.json"
SHIM = """#!/bin/sh
# agenthelm 运行时 {version}（agenthelm runtime install 生成）。只需要 Python 3.11+，不需要 AI 客户端。
self=$0
while [ -L "$self" ]; do  # a command link (--link-bin) points here; follow it to the runtime
  link=$(readlink "$self")
  case $link in /*) self=$link ;; *) self=$(dirname -- "$self")/$link ;; esac
done
here=$(CDPATH= cd -- "$(dirname -- "$self")/.." && pwd)
for candidate in "${{AGENTHELM_PYTHON:-}}" python3 python; do
  [ -n "$candidate" ] || continue
  command -v "$candidate" >/dev/null 2>&1 || continue
  if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    exec "$candidate" "$here/plugins/agent-pack/runtime/agenthelm.py" "$@"
  fi
done
echo "agenthelm 需要 Python 3.11 或更高版本：安装后重试，或设置 AGENTHELM_PYTHON。" >&2
exit 3
"""


class RuntimeInstallError(Exception):
    def __init__(self, message: str, next_steps: list[str] | None = None) -> None:
        super().__init__(message)
        self.next_steps = next_steps or []


# ---------------------------------------------------------------- locations

def data_root(env: Any) -> Path:
    base = env.get("XDG_DATA_HOME")
    return (Path(base) if base else env.home / ".local" / "share") / "agenthelm"


def runtimes_dir(env: Any) -> Path:
    return data_root(env) / "runtimes"


def current_link(env: Any) -> Path:
    return data_root(env) / "current"


def bundled_root() -> Path | None:
    """The release content this file ships in: npm package, release tarball or checkout."""
    root = Path(__file__).resolve().parents[3]
    return root if _is_release_root(root) else None


def _is_release_root(root: Path) -> bool:
    return (root / "index.json").is_file() and all((root / "plugins" / name / "plugin.json").is_file()
                                                    for name in PLUGINS)


def running_from(env: Any) -> str:
    here = Path(__file__).resolve()
    try:
        if data_root(env).resolve() in here.parents:
            return "runtime"
    except OSError:
        pass
    text = here.as_posix()
    if "/plugins/cache/" in text or "/.cursor/plugins/" in text:
        return "client-plugin"
    if "/node_modules/" in text or "/_npx/" in text:
        return "npm"
    return "checkout"


# ---------------------------------------------------------------- source description

def _shipped_files(root: Path) -> list[Path]:
    files = []
    for name in PLUGINS:
        base = root / "plugins" / name
        for path in sorted(base.rglob("*")):
            relative = path.relative_to(root)
            if path.is_file() and not path.is_symlink() and not (set(relative.parts) & SKIP_PARTS) \
                    and path.suffix != ".pyc":
                files.append(relative)
    return files


def describe(root: Path) -> dict:
    if not _is_release_root(root):
        raise RuntimeInstallError(f"{root} 不是 agenthelm 发布内容（缺少 index.json 或 plugins/<插件>）",
                            ["--source 指向仓库检出、解压后的发布包目录或 .tar.gz 发布包"])
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    version = str(index.get("plugin", {}).get("version") or "")
    if not version:
        raise RuntimeInstallError(f"{root}/index.json 没有发布版本号")
    digest = hashlib.sha256()
    files = _shipped_files(root)
    for relative in files:
        digest.update(relative.as_posix().encode("utf-8") + b"\0" + (root / relative).read_bytes() + b"\0")
    plugins = {name: json.loads((root / "plugins" / name / "plugin.json").read_text(encoding="utf-8")).get("version")
               for name in PLUGINS}
    return {"version": version, "plugins": plugins, "digest": digest.hexdigest(), "files": len(files)}


def _read_manifest(directory: Path) -> dict | None:
    try:
        data = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def installed(env: Any) -> list[dict]:
    base = runtimes_dir(env)
    found = []
    if base.is_dir():
        for directory in sorted(base.iterdir()):
            manifest = _read_manifest(directory) if directory.is_dir() and not directory.name.startswith(".") else None
            if manifest:
                found.append({**manifest, "path": str(directory)})
    return found


def current(env: Any) -> dict | None:
    link = current_link(env)
    if not link.is_symlink():
        return None
    manifest = _read_manifest(link)
    return {**manifest, "path": str(link.resolve())} if manifest else None


def release_version(latest: dict | None) -> str | None:
    tag = ((latest or {}).get("release") or {}).get("tag")
    return tag[1:] if isinstance(tag, str) and tag.startswith("v") else tag


def report(env: Any, latest: dict | None = None) -> dict:
    active = current(env)
    newest = release_version(latest)
    return {"current": active, "installed": installed(env), "data_root": str(data_root(env)),
            "running_from": running_from(env), "latest_release": newest,
            "update_available": bool(active and newest and _newer(newest, active["version"]))}


def _newer(candidate: str, existing: str) -> bool:
    def key(value: str) -> tuple:
        parts = value.split("-", 1)[0].split(".")
        return tuple(int(part) if part.isdigit() else 0 for part in parts)
    return key(candidate) > key(existing)


# ---------------------------------------------------------------- plan and apply

def plan(env: Any, root: Path, origin: str, *, link_bin: Path | None = None) -> dict:
    info = describe(root)
    target = runtimes_dir(env) / info["version"]
    existing = _read_manifest(target)
    active = current(env)
    if existing and existing.get("digest") != info["digest"]:
        raise RuntimeInstallError(f"运行时 {info['version']} 已安装，但内容与来源不同（来源：{origin}）",
                            [f"已装：{target}；同一版本不会被改写",
                             "确认要换掉时先手动移走该目录（例如移到 ~/.agent-pack/recovery/），再重新安装"])
    writes = []
    if not existing:
        writes.append({"path": str(target), "scope": "用户数据目录（新版本，旧版本保留）"})
    switch = not active or active.get("version") != info["version"]
    if switch:
        writes.append({"path": str(current_link(env)), "scope": f"切换当前版本 → {info['version']}"})
    link = None
    if link_bin is not None:
        link = link_bin.expanduser() / "agenthelm"
        expected = current_link(env) / "bin" / "agenthelm"
        if link.is_symlink() and Path(os.readlink(link)) == expected:
            link = None
        elif link.exists() or link.is_symlink():
            raise RuntimeInstallError(f"{link} 已存在且不是本运行时的链接，不会覆盖", ["换一个 --link-bin 目录，或手动处理该文件"])
        else:
            writes.append({"path": str(link), "scope": "命令链接（指向当前运行时）"})
    return {"version": info["version"], "plugins": info["plugins"], "digest": info["digest"], "source": origin,
            "target": str(target), "install": not existing, "switch": switch,
            "previous": active.get("version") if active else None, "link": str(link) if link else None,
            "writes": writes, "action": "install" if not existing else ("switch" if switch else "none")}


def apply(env: Any, planned: dict, root: Path) -> dict:
    base = runtimes_dir(env)
    base.mkdir(parents=True, exist_ok=True)
    target = Path(planned["target"])
    if planned["install"]:
        staging = Path(tempfile.mkdtemp(prefix=f".{planned['version']}-", dir=base))
        try:
            for relative in _shipped_files(root):
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / relative, destination)
            shutil.copy2(root / "index.json", staging / "index.json")
            if describe(staging)["digest"] != planned["digest"]:
                raise RuntimeInstallError("复制后的内容与来源摘要不一致，已放弃安装")
            shim = staging / "bin" / "agenthelm"
            shim.parent.mkdir()
            shim.write_text(SHIM.format(version=planned["version"]), encoding="utf-8")
            shim.chmod(0o755)
            manifest = {"schema_version": 1, "version": planned["version"], "plugins": planned["plugins"],
                        "digest": planned["digest"], "source": planned["source"],
                        "installed_at": datetime.now(UTC).replace(microsecond=0).isoformat()}
            (staging / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.rename(staging, target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    if planned["switch"]:
        _switch(env, target)
    if planned["link"]:
        link = Path(planned["link"])
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(current_link(env) / "bin" / "agenthelm")
    active = current(env)
    shim = current_link(env) / "bin" / "agenthelm"
    verified = bool(active and active["version"] == planned["version"] and os.access(shim, os.X_OK))
    return {"ok": verified, "version": planned["version"], "current": active,
            "verify": (f"重新读取：当前运行时 {planned['version']}，命令 {shim}" if verified
                       else "回读不一致：请运行 agenthelm runtime status 查看"),
            "rollback": f"agenthelm runtime use {planned['previous']} --apply" if planned["previous"]
            and planned["previous"] != planned["version"] else None}


def _switch(env: Any, target: Path) -> None:
    link = current_link(env)
    temporary = link.with_name(f".current.{os.getpid()}")
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    temporary.symlink_to(Path("runtimes") / target.name)
    os.replace(temporary, link)


def use(env: Any, version: str, *, apply_change: bool) -> dict:
    target = runtimes_dir(env) / version
    if not _read_manifest(target):
        versions = "、".join(item["version"] for item in installed(env)) or "（无）"
        raise RuntimeInstallError(f"没有安装运行时 {version}", [f"已安装：{versions}"])
    active = current(env)
    result = {"version": version, "previous": active.get("version") if active else None, "applied": False}
    if apply_change:
        _switch(env, target)
        result["applied"] = True
        result["current"] = current(env)
    return result


# ---------------------------------------------------------------- sources

def resolve_source(host: Any, env: Any, args: argparse.Namespace, workdir: Path, *, prefer_release: bool) -> tuple[Path, str]:
    if args.source is not None:
        source = args.source.expanduser().resolve()
        if source.is_file():
            extracted = workdir / "source"
            extracted.mkdir()
            host._safe_extract(source, extracted)
            roots = [path for path in extracted.iterdir() if path.is_dir()]
            if len(roots) != 1:
                raise RuntimeInstallError("发布包结构不符合预期")
            return roots[0], f"本地发布包 {source.name}"
        return source, f"本地目录 {source}"
    bundled = bundled_root()
    if bundled is not None and not prefer_release and not args.from_release:
        return bundled, f"随附内容（{running_from(env)}）"
    try:
        latest, _ = host.load_latest(env, args.latest, max_age=0)
        return host.release_source(latest, workdir), "GitHub Release（已核对摘要）"
    except (host.CheckError, OSError, ValueError) as exc:
        raise RuntimeInstallError(f"无法下载发布包：{exc}",
                            ["离线时用 --source 指向发布包（.tar.gz）或仓库检出",
                             "下载地址见 https://github.com/lc4t/agenthelm/releases"]) from exc


# ---------------------------------------------------------------- command line

def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="agenthelm runtime", description=__doc__.split("\n\n")[0])
    sub = result.add_subparsers(dest="action", required=True)
    for name, text in (("status", "当前运行时、已装版本与可用更新"),
                       ("install", "安装运行时（默认用本命令随附的内容；默认预览）"),
                       ("update", "联网安装最新发布版本并切换（默认预览）"),
                       ("use", "切换到已安装的某个版本（默认预览）")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--output", choices=("text", "json"), default="text")
        command.add_argument("--home", type=Path, help=argparse.SUPPRESS)
        command.add_argument("--latest", help="latest.json 的 URL 或本地路径")
        if name == "use":
            command.add_argument("version")
        if name in ("install", "update"):
            command.add_argument("--source", type=Path, help="本地目录或 .tar.gz 发布包（离线安装）")
            command.add_argument("--link-bin", type=Path, help="在这个目录建 agenthelm 命令链接（例如 ~/.local/bin）")
        if name == "install":
            command.add_argument("--from-release", action="store_true", help="联网下载发布包，而不用随附内容")
        if name != "status":
            command.add_argument("--apply", action="store_true")
        if name == "status":
            command.add_argument("--offline", action="store_true", help="只用缓存的版本信息")
    return result


def _print(payload: dict, output: str, lines: list[str]) -> None:
    if output == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))


def main(argv: list[str], host: Any) -> int:
    args = parser().parse_args(argv)
    env = host.current_env(args.home)
    if sys.platform == "win32":
        print("agenthelm runtime 目前只支持 macOS 与 Linux。", file=sys.stderr)
        return 4
    try:
        if args.action == "status":
            try:
                latest, _ = host.load_latest(env, args.latest, offline=args.offline)
            except (host.CheckError, OSError, ValueError):
                latest = None
            data = report(env, latest)
            active = data["current"]
            lines = [f"当前运行时：{active['version']}（{active['path']}）" if active else "当前运行时：未安装",
                     f"已安装：{'、'.join(item['version'] for item in data['installed']) or '（无）'}",
                     f"本次命令来自：{data['running_from']}"]
            if data["update_available"]:
                lines.append(f"有新版本 {data['latest_release']}：agenthelm runtime update 预览")
            if not active:
                lines.append("安装：agenthelm runtime install（预览）→ 加 --apply 执行")
            _print({"ok": True, **data}, args.output, lines)
            return 0
        if args.action == "use":
            result = use(env, args.version, apply_change=args.apply)
            lines = [f"{'已切换' if result['applied'] else '将切换'}：{result['previous']} → {result['version']}"]
            if not args.apply:
                lines.append("以上为预览；加 --apply 执行。")
            _print({"ok": True, **result}, args.output, lines)
            return 0
        with tempfile.TemporaryDirectory(prefix="agenthelm-runtime-") as temporary:
            root, origin = resolve_source(host, env, args, Path(temporary), prefer_release=args.action == "update")
            planned = plan(env, root, origin, link_bin=args.link_bin)
            lines = [f"运行时 {planned['version']}（来源：{origin}）"]
            lines += [f"  将写入：{item['path']}（{item['scope']}）" for item in planned["writes"]] or ["  已是当前版本，无需改动。"]
            if not args.apply or planned["action"] == "none" and not planned["link"]:
                if args.apply is False and planned["writes"]:
                    lines.append("以上为预览；加 --apply 执行。")
                _print({"ok": True, "applied": False, "plan": planned}, args.output, lines)
                return 0
            result = apply(env, planned, root)
        lines.append(("  " + result["verify"]))
        if result["rollback"]:
            lines.append(f"  回到旧版本：{result['rollback']}")
        if not os.environ.get("PATH", "").count(str(current_link(env) / "bin")) and not planned["link"]:
            lines.append(f"  直接用命令：把 {current_link(env) / 'bin'} 加入 PATH，或重新运行并加 --link-bin ~/.local/bin")
        _print({"ok": result["ok"], "applied": True, "plan": planned, **result}, args.output, lines)
        return 0 if result["ok"] else 6
    except RuntimeInstallError as exc:
        if args.output == "json":
            print(json.dumps({"ok": False, "error": str(exc), "next": exc.next_steps}, ensure_ascii=False, indent=2))
        else:
            print(f"出错：{exc}", file=sys.stderr)
            for step in exc.next_steps:
                print(f"  · {step}", file=sys.stderr)
        return 4 if "无法下载" in str(exc) else 2
