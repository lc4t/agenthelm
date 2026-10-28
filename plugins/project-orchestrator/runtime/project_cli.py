#!/usr/bin/env python3
"""项目入口：不需要 AI Agent 的项目自助菜单与命令。

    agenthelm project                  交互菜单（终端里）；非终端时打印概览后退出，不等待输入
    agenthelm project find <词…>        找资料：关键词、日期（2025-12、10月）、--type、--in；--open N 打开第 N 项
    agenthelm project open <路径>       打开资料（--reveal 显示所在目录，--copy 复制路径）
    agenthelm project refs <路径>       谁引用它 / 它引用谁
    agenthelm project progress [词]     看进展：按状态分组；--status 只看一种，--all 含已完成
    agenthelm project show <事项>       事项详情：编号、旧编号、别名或标题片段都可以
    agenthelm project guide            项目指南
    agenthelm project health           检查项目与环境（含脚手架是否与最新版一致）；有必须处理的项时退出码为 1
    agenthelm project fix [项…]         修复与更新：默认只预览，--apply 执行；--keep / --accept 选择定制文件
    agenthelm project fix add-skeleton [路径…]   按模板补上缺少的骨架文件（已有文件不动）
    agenthelm project fix profile [字段…]        只改所选 Profile 字段，预览里有 diff
    agenthelm project fix --skip <ID> --reason …  永久跳过某个发现（记入随项目同步的决定文件）；--unskip <ID> 撤销
    agenthelm project record <事项|new> 记录事项：默认只预览，--apply 写入
    agenthelm project tools [工具]      项目自行声明的工具；--run --apply 执行
    agenthelm project sop <子命令>      流程（SOP）：list / index / show / new / interview / defaults / preflight /
                                       activate / run / step / status / due / validate …；写入默认只预览，--apply 执行
    agenthelm project capabilities     各项能力当前是否可用、为什么

项目根目录的启动器（缺省 ./menu）等同于 agenthelm project --project <项目根目录>。
全局选项可写在任意位置：--project <目录>、--output text|json、--no-input、--offline。
--demo study|personal|research|blank 使用内置的虚构项目体验，可加 --demo-fault
index-broken|offline|runtime-old|conflict；演示结果都标注「演示」，不读写真实文件。

退出码：0 成功；1 没有结果或有必须处理的项；2 用法错误（含 --skip 了现在不存在的发现 ID）；
3 运行时与项目入口不兼容；4 能力当前不可用（含决定文件损坏）；5 写入冲突（别人刚改过，
含决定文件刚被另一台机器同步更新）；6 执行失败；130 中断。
脚手架差异只算“建议处理”，不会让 health 退出码变成 1。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_demo  # noqa: E402
from project_base import LAUNCHER_API, RUNTIME_API, EntryError  # noqa: E402
from project_demo import DemoProvider  # noqa: E402
from wf import S, clip, display_width, term_width  # noqa: E402

Provider = Any  # DemoProvider 或 project_real.RealProvider：方法与返回结构相同
# 经 ``agenthelm project`` 启动时，agent-pack 把自己的模块放在这里，供检查与更新使用。
HOST: Any = None

EXIT_OK, EXIT_ATTENTION, EXIT_USAGE, EXIT_INCOMPATIBLE, EXIT_UNAVAILABLE, EXIT_CONFLICT = 0, 1, 2, 3, 4, 5
EXIT_FAILED, EXIT_INTERRUPTED = 6, 130
EXIT_BY_CODE = {"usage": EXIT_USAGE, "not-found": EXIT_ATTENTION, "ambiguous": EXIT_ATTENTION,
                "runtime-incompatible": EXIT_INCOMPATIBLE, "unavailable": EXIT_UNAVAILABLE,
                "index-broken": EXIT_UNAVAILABLE, "not-a-project": EXIT_UNAVAILABLE,
                "profile-invalid": EXIT_UNAVAILABLE, "offline": EXIT_UNAVAILABLE,
                "conflict": EXIT_CONFLICT, "failed": EXIT_FAILED}
GROUPS = (("active", "进行中"), ("waiting", "等待"), ("captured", "待澄清"), ("suspended", "挂起"),
          ("done", "已完成"), ("abandoned", "放弃"), ("other", "其他状态"))
GROUP_LABEL = dict(GROUPS)
CLOSED = {"done", "abandoned"}
LEVELS = (("must", "必须处理"), ("suggest", "建议处理"), ("update", "可选更新"), ("unavailable", "当前不可用"))
LEVEL_LABEL = dict(LEVELS)
DEMO_BANNER = "【演示数据 · 不读写真实文件】"


def hhmm(value: str | None) -> str:
    """2026-10-24T21:30:00+08:00 -> 10-24 21:30"""
    return (value or "")[5:16].replace("T", " ")


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


# ---------------------------------------------------------------- provider

def open_provider(args: argparse.Namespace) -> Provider:
    if args.demo:
        return DemoProvider(args.demo, args.demo_fault)
    import project_real  # noqa: PLC0415 - only real runs need the project tools

    return project_real.RealProvider(args.project, args.project is not None, offline=args.offline,
                                     host=HOST, via_launcher=args.via_launcher)


def check_entry_api(provider: Provider) -> None:
    low, high = RUNTIME_API
    if provider.entry_api > high:
        raise EntryError(f"这个项目的入口需要接口版本 {provider.entry_api}，当前 agenthelm 运行时只支持到 {high}",
                         "runtime-incompatible",
                         ["更新运行时：agenthelm runtime update --apply（联网）",
                          "离线：agenthelm runtime install --source <新版发布包> --apply，见 INSTALL.md「离线安装」",
                          "项目文件没有被改动"])
    if provider.entry_api < low:
        raise EntryError(f"这个项目的入口较旧（接口版本 {provider.entry_api}），当前运行时最低支持 {low}",
                         "runtime-incompatible", ["更新项目入口：agenthelm update --only content，按提示保留或采用"])


# ---------------------------------------------------------------- renderers (text)

def header(provider: Provider, title: str) -> list[str]:
    banner = S.yellow(DEMO_BANNER) if provider.simulated else ""
    return [f"{S.bold(provider.name)} · {title}  {banner}".rstrip()]


def render_overview(provider: Provider) -> list[str]:
    overview = provider.overview()
    index = overview["index"]
    if index["status"] == "broken":
        index_text = S.red("资料索引损坏（可重建）")
    elif index["status"] == "missing":
        index_text = "资料索引尚未建立（首次查找时建立）"
    else:
        index_text = f"资料 {index['file_count']} 个（索引 {hhmm(index['built_at'])}）"
    counts = overview["items"]
    items_text = " · ".join(f"{label} {counts[key]}" for key, label in GROUPS if counts.get(key) and key not in CLOSED)
    health = overview["health"]
    health_text = " · ".join(f"{health[key]} 项{label}" for key, label in LEVELS if health.get(key)) or "未发现问题"
    if not overview.get("health_checked", True):
        health_text = "尚未检查（首页第 4 项）"
    banner = S.yellow(DEMO_BANNER) if provider.simulated else ""
    return [f"{S.bold(S.cyan(overview['name']))}  {S.dim(overview['kind_label'])}  {banner}".rstrip(),
            f"{index_text} · 事项 {items_text or '暂无'}",
            f"检查：{health_text}"]


def render_find(result: dict) -> list[str]:
    width = term_width()
    lines = []
    if not result["results"]:
        return [S.yellow("没有找到。") + S.dim("试试更短的词、去掉日期，或先在「看进展」里找到事项。")]
    for number, item in enumerate(result["results"], 1):
        label = item["title"] or item["name"]
        date = hhmm(item["modified"])
        left = f" {S.bold(S.cyan(f'{number:>2}'))}  {S.bold(clip(label, width - 24))}"
        lines.append(pad(left, width - len(date)) + S.dim(date))
        owner = item["case_title"] or ("项目根目录" if item["container"] == "project" else f"目录 {item['container']}/")
        status = GROUP_LABEL.get(item["status"], item["status"]) if item["status"] else ""
        owner_text = f"{owner}（{status}）" if status else owner
        refs = f" · 被引用 {item['referenced_by']} 处" if item["referenced_by"] else ""
        lines.append(f"     归属：{clip(owner_text, width - 30)}{refs}")
        lines.append(S.dim(f"     {clip(item['path'], width - 6)}"))
        if item["versions"]:
            lines.append(S.dim(f"     └ 同系列另有 {len(item['versions'])} 个版本（v{number} 查看）"))
    index = result["index"]
    lines.append(S.dim(f"共 {result['total']} 项 · 索引 {hhmm(index['built_at'])}，{index['file_count']} 个文件"))
    return lines


def render_versions(item: dict) -> list[str]:
    lines = [S.bold(item["title"] or item["name"]),
             f"  {hhmm(item['modified'])}  {item['path']}  {S.green('最新')}"]
    lines += [f"  {hhmm(version['modified'])}  {version['path']}" for version in item["versions"]]
    return lines


def render_refs(result: dict) -> list[str]:
    lines = [S.bold(result["path"])]
    lines += [S.cyan("  被引用：")] + [f"    ← {path}" for path in result["inbound"]] if result["inbound"] \
        else [S.dim("  没有被其他资料或事项引用")]
    if result["outbound"]:
        lines += [S.cyan("  引用了：")] + [f"    → {path}" for path in result["outbound"]]
    return lines


def render_items(result: dict, *, expand_closed: bool = False) -> tuple[list[str], list[dict]]:
    """Grouped list; returns the lines and the numbered rows (closed groups only when expanded)."""
    width = term_width()
    lines, numbered = [], []
    rows = result["items"]
    for key, label in GROUPS:
        members = [item for item in rows if item["group"] == key]
        if not members:
            continue
        if key in CLOSED and not expand_closed:
            lines.append(S.dim(f"{label}（{len(members)}）  输入 all 展开"))
            continue
        lines.append(S.bold(f"{label}（{len(members)}）"))
        for item in members:
            numbered.append(item)
            date = hhmm(item["updated_at"])
            left = f" {S.bold(S.cyan(f'{len(numbered):>2}'))}  {clip(item['title'], 26)}"
            if item["waiting_for"]:
                hint = f"等待：{item['waiting_for']}"
            elif item["next_action"] and key not in CLOSED:
                hint = f"下一步：{item['next_action']}"
            else:
                hint = (item["last_record"] or {}).get("text", "") or item["goal"]
            line = pad(left, 36) + S.dim(clip(hint, width - 52))
            lines.append(pad(line, width - len(date)) + S.dim(date))
    if not rows:
        lines.append(S.yellow("还没有事项。") + S.dim("可以在「记录事项」里新建第一个。"))
    lines.append(S.dim(f"来源：{result['source']} · 读取于 {hhmm(result['read_at'])}"))
    return lines, numbered


def render_item(item: dict) -> list[str]:
    def row(label: str, value: str) -> str:
        return f"  {pad(label, 12)}{value or S.dim('—')}"

    group = GROUP_LABEL.get(item["group"], item["status"])
    status = item["status"] if item["status"] != item["group"] else group
    last = item["last_record"]
    lines = [f"{S.bold(item['title'])}  [{status}]", row("编号", item["id"]), row("目标", item["goal"]),
             row("最后记录", f"{hhmm(last['at'])}  {last['text']}" if last else ""),
             row("下一动作", item["next_action"]), row("等待", item["waiting_for"]),
             row("恢复条件", item["resume_when"])]
    if item["deliverables"]:
        lines.append(row("交付物", f"d1  {item['deliverables'][0]}"))
        lines += [f"  {pad('', 12)}d{number}  {path}" for number, path in enumerate(item["deliverables"][1:], 2)]
    else:
        lines.append(row("交付物", ""))
    names = [*item["aliases"], *item["legacy_ids"]]
    if names:
        lines.append(row("别名/旧编号", "、".join(names)))
    lines.append(row("权威记录", f"{item['path']} · 更新于 {hhmm(item['updated_at'])}"))
    lines.append(S.dim("  只显示已记录的内容，不推断优先级、重要性或是否完成。"))
    return lines


def render_guide(guide: dict) -> list[str]:
    lines = [S.bold("这个项目做什么"), f"  {guide['about']}", "", S.bold("资料在哪")]
    lines += [f"  {pad(label, 18)}{path}" for label, path in guide["where"]]
    lines += ["", S.bold("怎样开始")] + [f"  {number}. {step}" for number, step in enumerate(guide["start"], 1)]
    lines += ["", S.bold("常用操作")]
    command = guide.get("command") or f"./{guide['launcher']}"
    for goal, menu, cli in guide["examples"]:
        lines.append(f"  · {goal}：菜单 {menu}；命令 {command} {cli}")
    lines += ["", S.bold("现在能用的功能")]
    for capability in guide["capabilities"]:
        mark = S.green("可用") if capability["state"] == "available" else S.yellow("不可用")
        offline = "，断网可用" if capability["offline"] else "，需要联网"
        reason = f"：{capability['reason']}" if capability["reason"] else ""
        lines.append(f"  {pad(capability['title'], 16)}{mark}{reason}{S.dim(offline)}")
    lines += ["", S.dim(f"这份指南在本地生成，断网可读。随时运行 {command} 打开本菜单。")]
    if guide.get("readme_mentions_entry") is False:
        lines.append(S.yellow("README 还没有提到这个入口：在 README 写一句“运行 " + command + " 打开项目菜单”，新接手的人就能找到。"))
    return lines


def render_health(health: dict, show_skipped: bool = False) -> tuple[list[str], list[dict]]:
    lines, numbered = [], []
    source = health["update_source"]
    if source["status"] == "online":
        online = "联网检查：最新"
    elif source["status"] == "cache":
        online = f"联网检查：使用 {hhmm(source['cached_at'])} 的缓存（一天内）"
    elif source["status"] == "unavailable":
        online = S.yellow("联网检查：不可用（没有 agent-pack）")
    elif source.get("cached_at"):
        online = S.yellow(f"联网检查：当前离线，使用 {hhmm(source['cached_at'])} 的缓存")
    else:
        online = S.yellow("联网检查：当前离线，也没有缓存的版本信息")
    lines.append(S.dim(f"读取于 {hhmm(health['read_at'])} · ") + online)
    for key, label in LEVELS:
        members = [finding for finding in health["findings"] if finding["level"] == key]
        if not members:
            continue
        colour = S.red if key == "must" else S.yellow if key in ("suggest", "update") else S.dim
        lines.append(colour(S.bold(f"{label}（{len(members)}）")))
        for finding in members:
            numbered.append(finding)
            lines.append(f" {S.bold(S.cyan(f'{len(numbered):>2}'))}  [{finding['area']}] {S.bold(finding['title'])}")
            lines.append(f"     原因：{finding['reason']}")
            lines.append(f"     影响：{finding['impact']}")
            for step in finding["next"]:
                lines.append(f"     下一步：{step['label']}" + S.dim(command_hint(step["action"])))
            if finding.get("decision"):
                where = "只记在这台机器上" if finding["decision"]["scope"] == "machine" else "记入项目，所有机器生效"
                lines.append(S.dim(f"     不需要？fix --skip {finding['decision']['id']} --reason …（{where}）"))
    skipped = health.get("skipped") or []
    if show_skipped:
        lines.append(S.bold(f"已跳过（{len(skipped)}）") if skipped else S.dim("没有被跳过的项。"))
        for item in skipped:
            decision = item["decision"]
            lines.append(f"     {item['id']}：{decision.get('reason') or '（未写理由）'} "
                         + S.dim(f"[{decision['decision']} {decision.get('decided_at', '')}]"))
    elif skipped:
        lines.append(S.dim(f"另有 {len(skipped)} 项已被跳过：health --show-skipped 查看"))
    if health["ok"]:
        lines.append(S.green("正常：") + "、".join(health["ok"]))
    return lines, numbered


def command_hint(action: str) -> str:
    """fix:rebuild-index -> （命令：fix rebuild-index）; empty for advice-only steps."""
    verb, _, target = action.partition(":")
    return f"（命令：{verb} {target}）" if verb in ("fix", "open") and target else ""


def render_plan(plan: dict, choices: dict[str, str] | None = None) -> list[str]:
    lines = []
    for number, step in enumerate(plan["steps"], 1):
        lines.append(f" {S.bold(S.cyan(str(number)))}  {S.bold(step['title'])}"
                     + (S.red("  含不兼容变更") if step["breaking"] else ""))
        lines += [f"     变化：{change}" for change in step["changes"]]
        for write in step["writes"]:
            lines.append(f"     将写入：{write['path']}（{write['scope']}）")
        for item in step["custom"]:
            chosen = (choices or {}).get(item["path"])
            decision = chosen or item["default"]
            if item.get("options"):
                label = ("" if chosen else "默认") + {"accept": "采用（按模板处理）", "skip": "永久跳过（记入决定文件）",
                                                    "later": "稍后（本次不处理）"}.get(decision, decision)
            elif item["path"] == "README.md":
                label = ("" if chosen else "默认") + ("不改" if decision == "keep" else "追加「怎样使用」")
            else:
                label = ("" if chosen else "默认") + ("保留你的版本" if decision == "keep" else "采用新模板")
            lines.append(S.yellow(f"     定制文件：{item['path']}（{item['state']}）→ {label}"))
        lines.append(f"     联网：{'需要' if step['network'] else '不需要'} · 恢复：{step['recovery']}")
        if step["blocked"]:
            lines.append(S.yellow(f"     暂不能执行：{step['blocked']}"))
    if not plan["steps"]:
        lines.append(S.green("没有需要处理的项。"))
    return lines


def render_apply(result: dict) -> list[str]:
    prefix = "【演示】" if result.get("simulated") else ""
    lines = []
    for item in result["results"]:
        if item["status"] == "skipped":
            lines.append(S.yellow(f"  跳过 {item['id']}：{item['detail']}"))
            continue
        lines.append(S.green(f"  {prefix}已处理 {item['id']}"))
        if item["written"]:
            lines.append(f"    写入：{'、'.join(item['written'])}")
        if item["kept"]:
            lines.append(f"    保留：{'、'.join(item['kept'])}（未改动）")
        if item["accepted"]:
            lines.append(f"    采用新模板：{'、'.join(item['accepted'])}")
        lines.append(f"    验证：{item['verify']}")
        lines.append(f"    恢复位置：{item['recovery']}")
    return lines


def render_preview(preview: dict) -> list[str]:
    verb = "将新建" if preview.get("kind") == "new" or preview.get("target") is None else "将修改"
    lines = [S.bold(f"{verb} {preview['path']}")]
    for line in preview["lines"]:
        lines.append(S.green(line) if line.startswith("+") else S.red(line) if line.startswith("-") else S.dim(line))
    if preview["expect_updated_at"]:
        lines.append(S.dim(f"写入前会核对 updated_at 仍为 {preview['expect_updated_at']}，别人改过就不写入"))
    return lines


def render_tools(tools: list[dict]) -> list[str]:
    if not tools:
        return [S.dim("这个项目没有声明专用工具。项目可在 .agents/moe.sakanano.agent-pack/tools.json 声明。")]
    lines = []
    for number, tool in enumerate(tools, 1):
        mark = S.green("可用") if tool["state"] == "available" else S.yellow(f"不可用：{tool['reason']}")
        lines.append(f" {S.bold(S.cyan(str(number)))}  {S.bold(tool['title'])}  {mark}")
        if tool["description"]:
            lines.append(f"     {tool['description']}")
        lines.append(S.dim(f"     命令：{' '.join(tool['command'])}"))
        account = f" · 账号：{tool['account']}" if tool["account"] else ""
        lines.append(S.dim(f"     联网：{'需要' if tool['network'] else '不需要'}{account} · 写入：{'、'.join(tool['writes']) or '无'}"))
    return lines


def render_capabilities(capabilities: list[dict]) -> list[str]:
    return [f"  {pad(item['title'], 16)}{S.green('可用') if item['state'] == 'available' else S.yellow('不可用')}"
            + (f"：{item['reason']}" if item["reason"] else "") for item in capabilities]


def render_error(error: EntryError) -> list[str]:
    lines = [S.red(f"出错：{error}")]
    lines += [f"  · {step}" for step in error.next_steps]
    return lines


# ---------------------------------------------------------------- commands

def _act_target(result: dict, number: int) -> dict:
    if not 1 <= number <= len(result["results"]):
        raise EntryError(f"没有第 {number} 项（共 {len(result['results'])} 项）", "not-found")
    return result["results"][number - 1]


def cmd_find(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    result = provider.find(args.terms, ext=args.type, container=args.container, limit=args.limit)
    lines = header(provider, "找资料") + render_find(result)
    for action in ("open", "reveal", "copy"):
        number = getattr(args, action)
        if number is not None:
            done = provider.act(action, _act_target(result, number)["path"])
            result = {**result, "action": done}
            lines.append(done["message"])
    return (EXIT_OK if result["results"] else EXIT_ATTENTION), result, lines


def cmd_open(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    action = "reveal" if args.reveal else "copy" if args.copy else "open"
    done = provider.act(action, args.path)
    return EXIT_OK, done, [done["message"]]


def cmd_refs(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    result = provider.refs(args.path)
    return EXIT_OK, result, render_refs(result)


def cmd_progress(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    result = provider.items(status=args.status, term=args.term)
    lines, _ = render_items(result, expand_closed=args.all or args.status in CLOSED)
    return (EXIT_OK if result["items"] or not (args.term or args.status) else EXIT_ATTENTION), result, \
        header(provider, "看进展") + lines


def cmd_show(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    item = provider.item(args.item)
    return EXIT_OK, item, render_item(item)


def cmd_guide(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    guide = provider.guide()
    return EXIT_OK, guide, header(provider, "项目指南") + render_guide(guide)


def cmd_health(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    provider.reprompt = bool(getattr(args, "reprompt", False))
    health = provider.health()
    lines, _ = render_health(health, bool(getattr(args, "show_skipped", False)))
    must = any(finding["level"] == "must" for finding in health["findings"])
    return (EXIT_ATTENTION if must else EXIT_OK), health, header(provider, "检查项目与环境") + lines


def cmd_fix(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    if args.skip or args.unskip:
        return _fix_decisions(provider, args)
    choices = {path: "keep" for path in args.keep} | {path: "accept" for path in args.accept}
    if args.entry_name:
        choices["entry-name"] = args.entry_name
    ids, targets = _split_targets(args.ids)
    if targets:
        choices["_targets"] = "\n".join(targets)
    if args.reason:
        choices["_reason"] = args.reason
    plan = provider.fix_plan(ids, choices)
    lines = header(provider, "修复与更新") + render_plan(plan, choices)
    if not args.apply:
        lines.append(S.dim("以上为预览，没有写入。确认后加 --apply；定制文件用 --keep / --accept 指定。"))
        if any(item.get("options") for step in plan["steps"] for item in step["custom"]):
            lines.append(S.dim("不想要其中某项：fix --skip <ID> --reason …（ID 见 health / check）；只处理几项：在处理项后列出路径或字段。"))
        return EXIT_OK, {"plan": plan, "applied": False}, lines
    result = provider.apply_fix(plan, choices)
    return EXIT_OK, {"plan": plan, "applied": True, **result}, lines + render_apply(result)


def _split_targets(ids: list[str]) -> tuple[list[str], list[str]]:
    if ids and ids[0] in ("add-skeleton", "profile"):
        return ids[:1], ids[1:]
    return ids, []


def _fix_decisions(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    """--skip / --unskip: write only the decision file; the check itself stays read-only."""
    if not hasattr(provider, "decide"):
        raise EntryError("演示项目没有决定文件", "usage")
    result = provider.decide(args.skip, args.unskip, args.reason or "", args.apply)
    lines = header(provider, "记录决定")
    for item in result["decisions"]:
        if item["mode"] == "decide":
            verb = "已记录" if item["applied"] else "将记录"
            lines.append(f"  {verb}：{item['id']} → {item['entry']['decision']}"
                         + (f"（{item['entry']['reason']}）" if item["entry"].get("reason") else "（没有写理由）")
                         + f"，写入 {item['file']}（{'随项目同步' if item['scope'] == 'project' else '只在这台机器'}）")
        else:
            lines.append(f"  {'已撤销' if item['applied'] else '将撤销'}：{item['id']}（{item['file']}）")
    if not args.apply:
        lines.append(S.dim("以上为预览，没有写入。确认后加 --apply。"))
    return EXIT_OK, result, lines


def _record_change(args: argparse.Namespace) -> dict:
    status = "done" if args.close else "active" if args.reopen else args.status
    return {"title": args.title, "goal": args.goal, "slug": args.slug, "log": args.log,
            "next_action": args.next, "waiting_for": args.waiting, "status": status,
            "add_deliverable": args.add_deliverable}


def cmd_record(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    target = None if args.item == "new" else args.item
    if target is None and not (args.title and args.goal):
        raise EntryError("新建事项需要 --title 与 --goal", "usage")
    preview = provider.record_preview(target, _record_change(args))
    # 预览与写入分两次运行时，用预览当时的值核对；同一次运行里预览本身就是核对基准。
    for key in ("expect_updated_at", "expect_sha256"):
        if getattr(args, key):
            preview[key] = getattr(args, key)
    lines = header(provider, "记录事项") + render_preview(preview)
    if not args.apply:
        lines.append(S.dim("以上为预览，没有写入。确认后加 --apply。"))
        return EXIT_OK, {"preview": preview, "applied": False}, lines
    result = provider.record_apply(preview)
    lines += [S.green(result["verify"])]
    events = result.get("sop_events") or []
    if events:
        lines += ["", S.bold("命中流程（SOP）：")]
        for event in events:
            lines.append(f"  {event['id']}（{event['title']}）：{event['trigger']}")
            lines.append(S.dim(f"    是否按它执行由你决定；确认后运行：{event['command']}"))
    return EXIT_OK, {"preview": preview, "applied": True, **result}, lines


def cmd_tools(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    tools = provider.tools()
    if args.tool is None:
        return EXIT_OK, tools, header(provider, "项目工具") + render_tools(tools)
    tool = next((tool for tool in tools if tool["id"] == args.tool), None)
    if tool is None:
        raise EntryError(f"项目没有声明这个工具：{args.tool}", "not-found",
                         ["已声明：" + ("、".join(tool["id"] for tool in tools) or "（无）")])
    lines = render_tools([tool])
    if not (args.run and args.apply):
        lines.append(S.dim("以上为说明，没有执行。确认后加 --run --apply。"))
        return EXIT_OK, tool, lines
    result = provider.run_tool(args.tool)
    return EXIT_OK, result, lines + [result["message"]]


SOP_EXIT_CODES = {"usage": "usage", "invalid": "usage", "not-found": "not-found", "ambiguous": "ambiguous",
                  "conflict": "conflict", "unavailable": "unavailable", "failed": "failed"}


def cmd_sop(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    rest = list(args.sop_args)
    for flag in ("--output",):  # 子命令后面的 --output 交给本入口处理，保持与其他子命令一致
        if flag in rest and rest.index(flag) + 1 < len(rest):
            position = rest.index(flag)
            args.output = rest[position + 1]
            del rest[position:position + 2]
    if not rest:
        rest = ["list"]
    code, data, lines = provider.sop(rest)
    return code, data, header(provider, "流程（SOP）") + lines


def cmd_capabilities(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    capabilities = provider.capabilities()
    return EXIT_OK, capabilities, header(provider, "能力状态") + render_capabilities(capabilities)


def cmd_scenarios(_provider: Any, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    items = project_demo.describe()
    lines = [f"  --demo {pad(item['id'], 10)}{item['name']}（{item['kind']}）" for item in items]
    lines.append("  --demo-fault " + " | ".join(project_demo.FAULTS))
    return EXIT_OK, {"scenarios": items, "faults": list(project_demo.FAULTS)}, lines


def cmd_home(provider: Provider, args: argparse.Namespace) -> tuple[int, Any, list[str]]:
    if args.output == "text" and not args.no_input and sys.stdin.isatty() and sys.stdout.isatty():
        import project_menu
        return project_menu.run(provider), None, []
    lines = render_overview(provider) + ["", S.bold("可做的事")]
    lines += render_capabilities(provider.capabilities())
    lines.append(S.dim("在终端里直接运行可进入菜单；脚本中请用子命令，见 --help。"))
    return EXIT_OK, provider.overview(), lines


COMMANDS: dict[str, Callable[[Any, argparse.Namespace], tuple[int, Any, list[str]]]] = {
    "find": cmd_find, "open": cmd_open, "refs": cmd_refs, "progress": cmd_progress, "show": cmd_show,
    "guide": cmd_guide, "health": cmd_health, "fix": cmd_fix, "record": cmd_record, "tools": cmd_tools,
    "sop": cmd_sop, "capabilities": cmd_capabilities, "scenarios": cmd_scenarios, "home": cmd_home,
}


def parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    result =argparse.ArgumentParser(prog="agenthelm project", description="项目入口：不需要 AI Agent 的项目自助菜单",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)

    def globals_on(target: argparse.ArgumentParser, default: Any) -> None:
        def value(normal: Any) -> Any:
            return argparse.SUPPRESS if default is argparse.SUPPRESS else normal
        target.add_argument("--project", type=Path, default=value(None), help="项目目录；缺省从当前目录向上查找")
        target.add_argument("--output", choices=("text", "json"), default=value("text"))
        target.add_argument("--no-input", action="store_true", default=value(False), help="不进入交互菜单")
        target.add_argument("--demo", default=value(None), help="演示场景：study / personal / research / blank")
        target.add_argument("--demo-fault", default=value(None), choices=project_demo.FAULTS)
        target.add_argument("--offline", action="store_true", default=value(False),
                            help="不联网：版本检查只用缓存，需要联网的维护只预览")
        target.add_argument("--via-launcher", type=int, default=value(None), help=argparse.SUPPRESS)

    globals_on(result, None)
    globals_on(shared, argparse.SUPPRESS)
    sub = result.add_subparsers(dest="command")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[shared], help=help_text)

    find = add("find", "找资料")
    find.add_argument("terms", nargs="*")
    find.add_argument("--type", help="文件类型，如 pdf、xlsx")
    find.add_argument("--in", dest="container", help="限定事项或目录（片段）")
    find.add_argument("--limit", type=int, default=10)
    for action in ("open", "reveal", "copy"):
        find.add_argument(f"--{action}", type=int, metavar="N", help=f"对第 N 项执行 {action}")
    opener = add("open", "打开资料")
    opener.add_argument("path")
    opener.add_argument("--reveal", action="store_true")
    opener.add_argument("--copy", action="store_true")
    add("refs", "引用关系").add_argument("path")
    progress = add("progress", "看进展")
    progress.add_argument("term", nargs="?")
    progress.add_argument("--status", choices=tuple(GROUP_LABEL))
    progress.add_argument("--all", action="store_true", help="同时列出已完成与放弃的事项")
    add("show", "事项详情").add_argument("item")
    add("guide", "项目指南")
    health = add("health", "检查项目与环境")
    health.add_argument("--show-skipped", action="store_true", help="列出被你跳过的项及理由")
    health.add_argument("--reprompt", action="store_true", help="忽略已记录的决定，重新评估全部差异")
    fix = add("fix", "修复与更新")
    fix.add_argument("ids", nargs="*", help="处理项；add-skeleton / profile 后可跟路径 / 字段")
    fix.add_argument("--skip", action="append", default=[], metavar="ID", help="永久跳过这个发现（记入决定文件）")
    fix.add_argument("--unskip", action="append", default=[], metavar="ID", help="撤销对这个发现的跳过")
    fix.add_argument("--reason", help="--skip 的理由（建议写；随项目同步，换一台机器也看得到）")
    fix.add_argument("--apply", action="store_true")
    fix.add_argument("--keep", action="append", default=[])
    fix.add_argument("--accept", action="append", default=[])
    fix.add_argument("--entry-name", help="add-entry：启动器文件名（缺省 menu）")
    record = add("record", "记录事项")
    record.add_argument("item", help="事项，或 new 新建")
    for name in ("title", "goal", "slug", "log", "next", "waiting"):
        record.add_argument(f"--{name}")
    record.add_argument("--add-deliverable", action="append", default=[], metavar="路径",
                        help="登记资料或交付物（相对项目根目录，可重复）")
    record.add_argument("--status", choices=tuple(GROUP_LABEL))
    record.add_argument("--close", action="store_true")
    record.add_argument("--reopen", action="store_true")
    record.add_argument("--expect-updated-at", help="只在事项的 updated_at 仍为这个值时写入（取自预览的 JSON）")
    record.add_argument("--expect-sha256", help="只在权威记录的内容摘要仍为这个值时写入（取自预览的 JSON）")
    record.add_argument("--apply", action="store_true")
    tools = add("tools", "项目工具")
    tools.add_argument("tool", nargs="?")
    tools.add_argument("--run", action="store_true")
    tools.add_argument("--apply", action="store_true")
    sop_parser = add("sop", "流程（SOP）：用户自己的步骤序列，触发、前置检查、逐步记录")
    sop_parser.add_argument("sop_args", nargs=argparse.REMAINDER, help="子命令与参数，见 agenthelm project sop --help")
    add("capabilities", "能力状态")
    add("scenarios", "列出演示场景")
    return result


def emit(output: str, code: int, command: str, data: Any, lines: list[str], demo: bool) -> None:
    if output == "json":
        print(json.dumps({"ok": code in (EXIT_OK, EXIT_ATTENTION), "attention": code == EXIT_ATTENTION,
                          "exit_code": code, "command": command, "demo": demo, "data": data}, ensure_ascii=False, indent=2))
    elif lines:
        print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    command = args.command or "home"
    try:
        if command == "scenarios":
            code, data, lines = cmd_scenarios(None, args)
            emit(args.output, code, command, data, lines, True)
            return code
        provider = open_provider(args)
        check_entry_api(provider)
        code, data, lines = COMMANDS[command](provider, args)
        emit(args.output, code, command, data, lines, provider.simulated)
        return code
    except EntryError as exc:
        code = EXIT_BY_CODE.get(exc.code, EXIT_USAGE)
        if args.output == "json":
            print(json.dumps({"ok": False, "exit_code": code, "command": command, "demo": bool(args.demo),
                              "error": {"code": exc.code, "message": str(exc), "next": exc.next_steps}},
                             ensure_ascii=False, indent=2))
        else:
            print("\n".join(render_error(EntryError(str(exc), exc.code, exc.next_steps))), file=sys.stderr)
        return code
    except KeyboardInterrupt:
        print()
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
