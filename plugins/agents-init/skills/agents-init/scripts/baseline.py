"""取得内容包的历史版本，用来重新渲染升级前的基线正文。

来源按这个顺序找，前一个没有就找下一个，全部取不到时返回空（调用方退回“保留 / 采用 / 追加”的选择）：

1. 本机运行时缓存：``<数据目录>/agenthelm/runtimes/<版本>/plugins/agents-init/``；
2. 本地 git tag：随检出带来的 ``v*`` tag 里的 ``plugins/agents-init/``；
3. GitHub Release：发布包 ``agenthelm-<tag>.tar.gz``，下载后核对 ``release.json`` 里的 SHA-256。

每个来源都要通过内容包自己的摘要校验（``content.json`` 里每个源文件的 SHA-256）；渲染出的基线是否可信，由调用方拿内容锁
里记录的文件摘要再核对一次。网络访问只经 ``fetch`` 注入点，测试用本地夹具替换；``AGENTHELM_NO_UPDATE_CHECK=1`` 关闭联网。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

REPOSITORY = "lc4t/agenthelm"
PLUGIN = "agents-init"
TEMPLATE_NAME = "AGENT.template.md"
LAUNCHER_NAME = "launcher.sh"
MAX_RELEASES = 20  # 联网最多看最近这么多个发布
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024

Fetch = Callable[[str], bytes]  # 抓取失败抛 OSError


@dataclass(frozen=True)
class PackSource:
    label: str  # 给人看的来源，如 “git tag v6.8.0”
    kind: str  # cache | git-tag | release
    version: str  # 内容包版本（content.json 里的 version）
    template: str
    launcher: str | None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verified(content: object, pack_id: str, read: Callable[[str], bytes | None], label: str, kind: str) -> PackSource | None:
    """content.json 里的内容包 + 它声明的源文件；任何一个文件缺失或摘要不符都丢弃这个来源。"""
    if not isinstance(content, dict):
        return None
    pack = next((item for item in content.get("packs", []) if isinstance(item, dict) and item.get("id") == pack_id), None)
    if not pack or not pack.get("version"):
        return None
    texts: dict[str, bytes] = {}
    for source in pack.get("sources", []):
        data = read(source)
        expected = (pack.get("files") or {}).get(source)
        if data is None or not expected or sha256(data) != expected:
            return None
        texts[Path(source).name] = data
    if TEMPLATE_NAME not in texts:
        return None
    try:
        launcher = texts[LAUNCHER_NAME].decode("utf-8") if LAUNCHER_NAME in texts else None
        return PackSource(label, kind, str(pack["version"]), texts[TEMPLATE_NAME].decode("utf-8"), launcher)
    except UnicodeDecodeError:
        return None


def _json(data: bytes | None) -> object:
    try:
        return json.loads(data.decode("utf-8")) if data is not None else None
    except (UnicodeDecodeError, ValueError):
        return None


def _order(sources: Iterator[PackSource], wanted: list[str]) -> Iterator[PackSource]:
    """想要的版本先给，其余的随后（调用方靠摘要核对，所以其余版本也可能命中）。"""
    rest = []
    for source in sources:
        if source.version in wanted:
            yield source
        else:
            rest.append(source)
    yield from rest


# ---------------------------------------------------------------- 1. runtime cache

def runtime_cache_dir(environ: dict | None = None) -> Path:
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "agenthelm" / "runtimes"


def from_runtime_cache(pack_id: str, wanted: list[str], directory: Path | None = None) -> Iterator[PackSource]:
    directory = directory or runtime_cache_dir()

    def sources() -> Iterator[PackSource]:
        try:
            versions = sorted((item for item in directory.iterdir() if item.is_dir() and not item.name.startswith(".")),
                              key=lambda item: item.name, reverse=True)
        except OSError:
            return
        for version in versions:
            root = version / "plugins" / PLUGIN
            if root.is_symlink():
                continue

            def read(source: str, root=root) -> bytes | None:
                path = root / source
                try:
                    return path.read_bytes() if path.is_file() and not path.is_symlink() else None
                except OSError:
                    return None

            found = _verified(_json(read("content.json")), pack_id, read, f"runtime cache {version.name}", "cache")
            if found:
                yield found

    yield from _order(sources(), wanted)


# ---------------------------------------------------------------- 2. local git tags

def _git(repo: Path, *args: str) -> bytes | None:
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def from_git_tags(pack_id: str, wanted: list[str], repo: Path | None = None) -> Iterator[PackSource]:
    repo = repo or Path(__file__).resolve().parents[3]
    prefix = _git(repo, "rev-parse", "--show-prefix")
    tags = _git(repo, "tag", "--list", "v*", "--sort=-v:refname")
    if prefix is None or tags is None:
        return
    base = prefix.decode("utf-8", "replace").strip()  # 如 plugins/agents-init/
    if not base:
        return

    def sources() -> Iterator[PackSource]:
        for tag in tags.decode("utf-8", "replace").split():
            def read(source: str, tag=tag) -> bytes | None:
                return _git(repo, "show", f"{tag}:{base}{source}")

            found = _verified(_json(read("content.json")), pack_id, read, f"git tag {tag}", "git-tag")
            if found:
                yield found

    yield from _order(sources(), wanted)


# ---------------------------------------------------------------- 3. GitHub Release

def urlopen_fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "agenthelm"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - https URL built from REPOSITORY
        data = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise OSError("response too large")
    return data


def _tar_reader(archive: bytes, root: str) -> Callable[[str], bytes | None]:
    """只读发布包里 ``<顶层目录>/plugins/agents-init/<文件>``，不解压到磁盘。"""
    def read(source: str) -> bytes | None:
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as handle:
                for member in handle:
                    parts = Path(member.name).parts
                    if member.isfile() and len(parts) >= 3 and parts[1:3] == ("plugins", PLUGIN) \
                            and "/".join(parts[3:]) == source and ".." not in parts:
                        extracted = handle.extractfile(member)
                        return extracted.read() if extracted else None
        except (tarfile.TarError, OSError, EOFError):
            return None
        return None

    return read


def from_github_release(pack_id: str, wanted: list[str], fetch: Fetch | None = None,
                        repository: str = REPOSITORY, environ: dict | None = None) -> Iterator[PackSource]:
    """只下载 content.json 的版本在 ``wanted`` 里的发布；发布包摘要来自同一发布的 release.json。"""
    environ = os.environ if environ is None else environ
    if environ.get("AGENTHELM_NO_UPDATE_CHECK") == "1" or not wanted:
        return
    fetch = fetch or urlopen_fetch
    try:
        releases = json.loads(fetch(f"https://api.github.com/repos/{repository}/releases?per_page={MAX_RELEASES}"))
    except (OSError, ValueError):
        return
    for release in releases if isinstance(releases, list) else []:
        tag = release.get("tag_name") if isinstance(release, dict) else None
        if not isinstance(tag, str) or not tag:
            continue
        try:
            peek = _json(fetch(f"https://raw.githubusercontent.com/{repository}/{tag}/plugins/{PLUGIN}/content.json"))
            pack = next((item for item in (peek or {}).get("packs", []) if isinstance(item, dict) and item.get("id") == pack_id), None) \
                if isinstance(peek, dict) else None
            if not pack or str(pack.get("version")) not in wanted:
                continue
            info = json.loads(fetch(f"https://github.com/{repository}/releases/download/{tag}/release.json"))
            archive = fetch(info.get("tarball_url") or f"https://github.com/{repository}/releases/download/{tag}/agenthelm-{tag}.tar.gz")
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(info, dict) or sha256(archive) != info.get("tarball_sha256"):
            continue
        found = _verified(_json(_tar_reader(archive, tag)("content.json")), pack_id,
                          _tar_reader(archive, tag), f"GitHub Release {tag}", "release")
        if found:
            yield found


def default_providers(fetch: Fetch | None = None) -> list[Callable[[str, list[str]], Iterator[PackSource]]]:
    """缓存 → git tag → GitHub Release；每个提供者接收 ``(内容包 id, 想要的版本)``。"""
    return [from_runtime_cache, from_git_tags, lambda pack_id, wanted: from_github_release(pack_id, wanted, fetch)]
