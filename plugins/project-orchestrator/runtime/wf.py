#!/usr/bin/env python3
"""wf：人用的文件查找入口，基于交付物台账（runtime/assets.py），无需 Agent。

    wf                       交互模式
    wf 周报 9月1号             直接查（日期、同义词自动识别）
    wf -o 周报 9月1号          查到后直接打开最新一个
    wf recent                最近改动的交付物
    wf cases [词]            Case 列表（进行中在前）
    wf refs <路径|序号> [层数]  谁引用它 / 它引用谁
    wf o|f|c|p|v <序号>       打开 / 在文件管理器中显示 / 复制路径 / 打印路径 / 列出版本（序号指上一次结果）
    wf check                 台账巡检摘要
    wf rebuild               强制重建台账

--project <目录> 与 --output json 可写在任意位置；缺省从当前目录向上查找 Project Profile。
打开、显示与复制只在 macOS 上调用系统命令，其他平台打印绝对路径。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assets  # noqa: E402
from assets import AssetError, Project  # noqa: E402

ACTIONS = {"o": "open", "f": "reveal", "c": "copy", "v": "versions", "r": "refs", "p": "path"}
STATUS_LABEL = {"active": "进行中", "waiting": "等待", "done": "已完成", "captured": "待澄清",
                "suspended": "挂起", "abandoned": "放弃"}


# ---------------------------------------------------------------- display

class Style:
    def __init__(self) -> None:
        self.on = sys.stdout.isatty() and "NO_COLOR" not in os.environ

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)


S = Style()


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(character) in "WF" else 1 for character in text)


def clip(text: str, width: int) -> str:
    if display_width(text) <= width:
        return text
    out, used = "", 0
    for character in text:
        size = 2 if unicodedata.east_asian_width(character) in "WF" else 1
        if used + size > width - 1:
            break
        out += character
        used += size
    return out + "…"


def term_width() -> int:
    return max(60, min(shutil.get_terminal_size((100, 24)).columns, 120))


def status_text(item: dict) -> str:
    status = item.get("status", "")
    if not status:
        return "project" if item.get("container_kind") == "project" else "-"
    label = STATUS_LABEL.get(status, status)
    return S.green(label) if status == "active" else S.yellow(label) if status == "waiting" else S.dim(label)


def render_groups(groups: list[dict]) -> None:
    width = term_width()
    for index, item in enumerate(groups, 1):
        label = item["title"] or item["name"]
        date = (item["modified"] or "")[:10]
        left = f" {S.bold(S.cyan(f'{index:>2}'))}  {S.bold(clip(label, width - 22))}"
        visible = display_width(re.sub(r"\033\[[0-9;]*m", "", left))
        print(left + " " * max(1, width - visible - len(date)) + S.dim(date))
        owner = item["case_title"] or item["container"]
        git = item["git"]
        git_text = S.dim(git) if git.startswith(("tracked", "none")) else S.yellow(git)
        print(f"     {item['name']} · {clip(owner, width - 50)} ({status_text(item)}) · {git_text}")
        print(S.dim(f"     {clip(item['path'], width - 6)}"))
        if item.get("versions"):
            names = " · ".join(version["name"] for version in item["versions"][:4])
            more = f" 等 {len(item['versions'])} 个" if len(item["versions"]) > 4 else ""
            print(S.dim(f"     └ 同系列另有 {len(item['versions'])} 个：{clip(names, width - 30)}{more}"))


# ---------------------------------------------------------------- last results

def save_last(project: Project, groups: list[dict]) -> None:
    """Remember the last result list (paths and versions) in the cache, never in the project."""
    try:
        path = project.state_dir(create=True) / assets.LAST_NAME
        payload = [{"path": group["path"], "name": group["name"], "title": group["title"],
                    "modified": group["modified"], "versions": group.get("versions", [])} for group in groups]
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)
    except (OSError, AssetError):
        pass


def load_last(project: Project) -> list[dict]:
    try:
        data = json.loads((project.state_dir() / assets.LAST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, AssetError):
        return []
    return [item for item in data if isinstance(item, dict) and isinstance(item.get("path"), str)] \
        if isinstance(data, list) else []


def resolve_target(token: str, last: list[dict]) -> dict | None:
    if token.isdigit():
        index = int(token) - 1
        if 0 <= index < len(last):
            return last[index]
        print(S.red(f"没有第 {token} 项（上一次结果共 {len(last)} 项）"))
        return None
    return {"path": token, "name": Path(token).name, "title": "", "modified": "", "versions": []}


# ---------------------------------------------------------------- actions

def do_action(action: str, relative: str, project: Project, platform: str | None = None) -> str:
    """Open / reveal / copy on macOS; elsewhere (or without the commands) print the absolute path."""
    target = str(project.root / relative)
    mac = (platform or sys.platform) == "darwin"
    if action == "open" and mac and shutil.which("open"):
        subprocess.run(["open", target], check=False)
        print(S.green("已打开 ") + relative)
    elif action == "reveal" and mac and shutil.which("open"):
        subprocess.run(["open", "-R", target], check=False)
        print(S.green("已在 Finder 中显示 ") + relative)
    elif action == "copy" and mac and shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=target.encode("utf-8"), check=False)
        print(S.green("已复制绝对路径 ") + target)
    else:
        print(target)
        return "printed"
    return action


def show_versions(group: dict) -> None:
    print(S.bold(group.get("title") or group.get("name", "")))
    print(f"  {(group.get('modified') or '')[:16].replace('T', ' ')}  {group['path']}  {S.green('最新')}")
    for version in group.get("versions", []):
        print(f"  {(version.get('modified') or '')[:16].replace('T', ' ')}  {version['path']}  "
              f"{S.dim(version.get('git', ''))}")


def show_refs(project: Project, path: str, depth: int = 1) -> dict:
    result = assets.refs(project, path, depth=depth)
    print(S.bold(result["path"]))
    if result["inbound"]:
        print(S.cyan("  被引用："))
        for item in result["inbound"][:25]:
            via = "" if item["via"] == "file" else S.dim("（目录级）")
            print(f"    ← {item['src']}{via}")
    else:
        print(S.dim("  没有被任何 Case 或扫描根里的文件引用"))
    if result["outbound"]:
        print(S.cyan("  引用了："))
        for item in result["outbound"][:25]:
            print(f"    → {item['dst']}")
    if result["affected_containers"]:
        print(S.yellow("  改动会波及：") + "、".join(result["affected_containers"][:8]))
    return result


def do_search(project: Project, terms: list[str], *, limit: int = 10, **filters: str) -> list[dict]:
    if assets.status(project)["status"] != "fresh":
        print(S.dim("（索引更新中…）"))
    result = assets.find(project, terms, limit=limit, **filters)
    query = result["query"]
    description = " ".join(query["terms"]) or "全部"
    if query["dates"]:
        description += " · 日期 " + ",".join(date.replace("*-", "") for date in query["dates"])
    print(S.dim(f"查找：{description}"))
    if not result["results"]:
        print(S.yellow("没有找到。") + S.dim("试试更短的词、去掉日期，或 `wf cases` 先看 Case 列表。"))
        return []
    render_groups(result["results"])
    tail = f"共 {result['total']} 组"
    if result["total"] > result["returned"]:
        tail += f"，只显示前 {result['returned']} 组（加关键词收窄）"
    print(S.dim(tail + "。序号可接 o 打开 / f 显示 / c 复制 / v 版本 / r 引用，例：o1"))
    save_last(project, result["results"])
    return result["results"]


def do_cases(project: Project, term: str | None, limit: int = 30) -> dict:
    result = assets.cases(project, term, limit=limit)
    width = term_width()
    for item in result["results"]:
        status = STATUS_LABEL.get(item["status"], item["status"] or "-")
        mark = S.green(f"{status:<4}") if item["status"] == "active" else S.dim(f"{status:<4}")
        print(f" {mark} {S.bold(clip(item['title'] or item['id'], width - 52))}  {S.dim(clip(item['id'], 44))}  "
              f"{S.dim(str(item['files']) + ' 个文件')}")
    print(S.dim(f"共 {result['total']} 个 Case" + ("；用 `wf <Case 关键词>` 查其文件" if result["total"] else "")))
    return result


def do_recent(project: Project, limit: int) -> dict:
    result = assets.recent(project, limit=limit)
    render_groups(result["results"])
    save_last(project, result["results"])
    return result


def do_check(project: Project) -> dict:
    report = assets.check(project)
    totals, tiers, counts = report["totals"], report["claim_tiers"], report["broken_counts"]
    git = totals["git"]
    print(S.bold("台账巡检"))
    print(f"  文件 {totals['files']} · 引用 {totals['edges']} · Git 已入库 {git.get('tracked', 0)} / "
          f"不入库 {git.get('ignored', 0)} / 新建未提交 {git.get('untracked', 0)} / 非 Git {git.get('none', 0)}")
    print(f"  交付物：文件级被引用 {tiers.get('file', 0)} · 仅目录覆盖 {tiers.get('directory', 0)} · "
          f"无人认领 {S.yellow(str(tiers.get('none', 0)))}")
    print(f"  断链：真断链 {S.yellow(str(counts.get('real-broken', 0)))} · "
          f"说明文字 {counts.get('prose-not-path', 0)} · 主题链接 {counts.get('wikilink-topic', 0)}")
    for container, paths in list(report["unclaimed"].items())[:5]:
        print(S.dim(f"  无人认领 · {container}: {paths[0]}"))
    return report


# ---------------------------------------------------------------- entry

HELP = f"""{S.bold('用法')}
  直接输入关键词查找；可带日期（9月1号 / 0901 / 2026-09-01），同义词按项目的 asset-synonyms.json 展开。
  {S.cyan('o1')} 打开第 1 项   {S.cyan('f1')} 在文件管理器中显示   {S.cyan('c1')} 复制绝对路径   {S.cyan('p1')} 打印路径
  {S.cyan('v1')} 查看全部版本   {S.cyan('r1')} 谁引用它（r1 2 为两层）   {S.cyan('1')} 直接打开
  {S.cyan('recent')} 最近改动   {S.cyan('cases [词]')} Case 列表   {S.cyan('check')} 巡检   {S.cyan('q')} 退出
  过滤：{S.cyan('ext:xlsx')}  {S.cyan('kind:deliverable')}（也可 draft/source/data）  {S.cyan('in:片段')}（限定 Case/目录）"""


def parse_filters(tokens: list[str]) -> tuple[list[str], dict]:
    terms, filters = [], {}
    for token in tokens:
        match = re.match(r"^(ext|kind|in):(.+)$", token)
        if match:
            filters[{"ext": "ext", "kind": "kind", "in": "container"}[match.group(1)]] = match.group(2)
        else:
            terms.append(token)
    return terms, filters


def split_globals(argv: list[str]) -> tuple[list[str], Path | None, str]:
    """Pull ``--project`` and ``--output`` out of any position."""
    rest, project, output = [], None, "text"
    index = 0
    while index < len(argv):
        token = argv[index]
        name, _, inline = token.partition("=")
        if name in ("--project", "--output"):
            if inline:
                value = inline
            elif index + 1 < len(argv):
                index += 1
                value = argv[index]
            else:
                raise AssetError(f"{name} 需要一个值")
            if name == "--project":
                project = Path(value)
            elif value in ("text", "json"):
                output = value
            else:
                raise AssetError("--output 只能是 text 或 json")
        else:
            rest.append(token)
        index += 1
    return rest, project, output


def interactive(project: Project) -> int:
    try:
        import readline  # noqa: F401  line editing and history
    except ImportError:
        pass
    current = assets.status(project)
    if current["status"] != "fresh":
        print(S.dim("（索引更新中…）"))
        assets.rebuild(project)
        current = assets.status(project)
    print(S.bold(S.cyan("交付物台账")) + S.dim(f"  ·  {current.get('file_count', '?')} 个文件  ·  索引 "
                                             f"{current.get('built_at', '')[11:16]}"))
    print(S.dim("输入关键词查找；? 帮助；q 退出"))
    groups: list[dict] = []
    while True:
        try:
            line = input(S.bold(S.cyan("\n查找 › "))).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("q", "quit", "exit", "退出"):
            return 0
        if line in ("?", "help", "帮助"):
            print(HELP)
            continue
        try:
            match = re.match(r"^([ofcvrp])\s*(\d+)(?:\s+(\d+))?$", line) or re.match(r"^()(\d+)()$", line)
            if match:
                key, number, extra = match.group(1) or "o", match.group(2), match.group(3)
                target = resolve_target(number, groups or load_last(project))
                if target is None:
                    continue
                if key == "v":
                    show_versions(target)
                elif key == "r":
                    show_refs(project, target["path"], depth=int(extra or 1))
                else:
                    do_action(ACTIONS[key], target["path"], project)
                continue
            if line in ("recent", "最近"):
                groups = do_recent(project, 15)["results"]
                continue
            if line == "cases" or line.startswith("cases "):
                do_cases(project, line.split(None, 1)[1] if " " in line else None)
                continue
            if line == "check":
                do_check(project)
                continue
            terms, filters = parse_filters(line.split())
            groups = do_search(project, terms, **filters)
        except AssetError as exc:
            print(S.red(f"出错：{exc}"))


def command(project: Project, argv: list[str], output: str) -> tuple[int, dict | None]:
    head = argv[0]

    def number(position: int, default: int) -> int:
        return int(argv[position]) if len(argv) > position and argv[position].isdigit() else default

    if output == "json":
        if head in ("recent", "最近"):
            return 0, assets.recent(project, limit=number(1, 15))
        if head in ("cases", "case"):
            return 0, assets.cases(project, " ".join(argv[1:]) or None)
        if head == "check":
            return 0, assets.check(project)
        if head == "rebuild":
            return 0, assets.rebuild(project, force=True)
        if head in ("refs", "r") and len(argv) > 1:
            target = resolve_target(argv[1], load_last(project))
            return (1, None) if target is None else (0, assets.refs(project, target["path"], depth=number(2, 1)))
        terms, filters = parse_filters(argv[1:] if head in ("-o", "-f", "-c") else argv)
        result = assets.find(project, terms, **filters)
        save_last(project, result["results"])
        return (0 if result["results"] else 1), result
    if head in ("recent", "最近"):
        do_recent(project, number(1, 15))
        return 0, None
    if head in ("cases", "case"):
        do_cases(project, " ".join(argv[1:]) or None)
        return 0, None
    if head == "check":
        do_check(project)
        return 0, None
    if head == "rebuild":
        result = assets.rebuild(project, force=True)
        print(f"{result['action']}：{result['file_count']} 个文件")
        return 0, None
    if (head in ACTIONS and len(argv) > 1 and (argv[1].isdigit() or head == "r")) or (head == "refs" and len(argv) > 1):
        target = resolve_target(argv[1], load_last(project))
        if target is None:
            return 1, None
        if head in ("r", "refs"):
            show_refs(project, target["path"], depth=number(2, 1))
        elif head == "v":
            show_versions(target)
        else:
            do_action(ACTIONS[head], target["path"], project)
        return 0, None
    flags = {"-o": "open", "-f": "reveal", "-c": "copy"}
    action = flags.get(head)
    terms, filters = parse_filters(argv[1:] if action else argv)
    groups = do_search(project, terms, **filters)
    if action and groups:
        do_action(action, groups[0]["path"], project)
    return (0 if groups else 1), None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    output = "text"
    try:
        argv, start, output = split_globals(argv)
        if argv and argv[0] in ("-h", "--help", "help"):
            print(__doc__)
            return 0
        project = Project.discover(start, explicit=start is not None)
        if not argv:
            if output == "json":
                raise AssetError("交互模式不支持 --output json")
            return interactive(project)
        code, result = command(project, argv, output)
        if output == "json":
            print(json.dumps(result if result is not None else {"ok": False, "code": "not-found",
                                                                  "error": "没有这一项"}, ensure_ascii=False, indent=2))
        return code
    except AssetError as exc:
        if output == "json":
            print(json.dumps({"ok": False, "code": exc.code, "error": str(exc)}, ensure_ascii=False))
        else:
            print(S.red(f"出错：{exc}"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
