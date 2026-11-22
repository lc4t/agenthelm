"""项目入口的交互菜单：按想完成的任务组织，每一步都能继续操作或回车返回。

只在终端里使用；脚本与 CI 请用 project_cli 的子命令，它们从不等待输入。
"""
from __future__ import annotations

import json
import re

import project_cli as cli
import project_extensions as ext
import project_ext_actions as actions
from project_cli import S, EntryError
from i18n_orchestrator import _, N_  # noqa: E402

MENU = (
    ("1", N_("找资料"), N_("按关键词、日期、类型查找并打开"), "find"),
    ("2", N_("看进展"), N_("浏览事项、下一动作与等待对象"), "progress"),
    ("3", N_("项目指南"), N_("这个项目做什么、资料在哪、怎样开始"), "guide"),
    ("4", N_("检查项目与环境"), N_("依赖、插件、记录与索引"), "health"),
    None,
    ("5", N_("修复与更新"), N_("重建索引、安装与更新"), "fix"),
    ("6", N_("记录事项"), N_("新建、追加记录、更新状态"), "record"),
    ("7", N_("项目工具"), N_("项目自行声明的操作"), "tools"),
    ("8", N_("流程（SOP）"), N_("自己定义的步骤序列：查看、检查所需工具、按它执行"), "sop"),
    ("9", N_("技能与 MCP"), N_("技能、MCP 的清单、重复与体检（只读）"), "extensions"),
)
# 第 9 屏的入口；最后一项为 False 表示本版尚未提供，只显示、不进入（不做成假入口）。
EXTENSION_ENTRIES = (
    ("1", N_("按客户端看"), N_("客户端 → 作用域 → 条目"), True),
    ("2", N_("按项目看"), N_("本项目 / 所有项目共用（只列本机已知存储）"), True),
    ("3", N_("按重复看"), N_("同名 · 同指纹 · 同名不同内容"), True),
    ("4", N_("技能体检"), N_("缺 description · 体积 · 加载成本 · 过期"), True),
    ("5", N_("移动或复制"), N_("在作用域、客户端之间迁移（先预览）"), True),
    ("6", N_("设置生效范围"), N_("全局 / 仅此项目（先预览）"), True),
)
ACTION_RE = re.compile(r"^([ofcvr])\s*(\d+)$")
RECORD_ACTIONS = (("1", N_("追加一条记录"), "log"), ("2", N_("更新下一动作"), "next_action"),
                  ("3", N_("改为等待（填写在等谁或等什么）"), "waiting"), ("4", N_("关闭（标为已完成）"), "close"),
                  ("5", N_("恢复（标为进行中）"), "reopen"), ("6", N_("登记资料或交付物"), "deliverable"))


class Back(Exception):
    pass


class Quit(Exception):
    pass


def ask(prompt: str) -> str:
    try:
        line = input(S.bold(S.cyan(f"\n{prompt} › "))).strip()
    except EOFError:
        raise Quit from None
    if line in ("q", "quit", "退出"):
        raise Quit
    if line in ("", "b", "返回"):
        raise Back
    return line


def choose(prompt: str, default: str) -> str:
    """Ask with a default: an empty answer means the default instead of going back."""
    try:
        return ask(prompt)
    except Back:
        return default


def show(lines: list[str]) -> None:
    print("\n".join(lines))


def fail(error: Exception) -> None:
    code = getattr(error, "code", "usage")
    show(cli.render_error(EntryError(str(error), code, getattr(error, "next_steps", []))))


def sub(screen, *args) -> None:
    """Run a nested screen; its "回车返回" comes back here instead of to the home page."""
    try:
        screen(*args)
    except Back:
        pass


def confirm(prompt: str = _("输入 yes 执行，回车取消")) -> bool:
    try:
        return ask(prompt).lower() in ("yes", "y", "是")
    except Back:
        print(S.dim(_("已取消，没有写入。")))
        return False


# ---------------------------------------------------------------- screens

def home(provider) -> None:
    print()
    show(cli.render_overview(provider))
    capabilities = {item["id"]: item for item in provider.capabilities()}
    health = provider.overview()["health"]
    print()
    for entry in MENU:
        if entry is None:
            print(S.dim(_("  ─────────── 维护")))
            continue
        key, title, hint, capability = entry
        title, hint = _(title), _(hint)
        state = capabilities.get(capability) or {"state": "unavailable", "reason": _("演示项目没有这一项")}
        if capability == "health" and any(health.values()):
            hint = " · ".join(_("{0} 项{1}").format(health[k], _(label)) for k, label in cli.LEVELS if health.get(k))
        if state["state"] != "available":
            hint = S.yellow(_("不可用：{0}").format(state['reason']))
        print(f"  {S.bold(S.cyan(key))}  {cli.pad(title, 16)}{S.dim(hint) if state['state'] == 'available' else hint}")
    print(S.dim(_("\n  输入序号；也可以直接输入关键词查资料。 ? 帮助 · q 退出")))


def find_screen(provider, first: str | None = None) -> None:
    show(cli.header(provider, _("找资料")))
    print(S.dim(_("输入关键词，可带日期（2025-12、10月）与 type:pdf、in:片段；回车返回")))
    result = None
    pending = first
    while True:
        line, pending = (pending, None) if pending else (ask(_("查找")), None)
        match = ACTION_RE.match(line) or re.match(r"^()(\d+)$", line)
        if match and result is not None:
            key, number = match.group(1) or "o", int(match.group(2))
            try:
                item = cli._act_target(result, number)
            except EntryError as exc:
                fail(exc)
                continue
            if key == "v":
                show(cli.render_versions(item) if item["versions"] else [S.dim(_("这份资料没有其他版本"))])
            elif key == "r":
                show(cli.render_refs(provider.refs(item["path"])))
            else:
                print(provider.act({"o": "open", "f": "reveal", "c": "copy"}[key], item["path"])["message"])
            continue
        terms, ext, container = [], None, None
        for token in line.split():
            if token.startswith("type:"):
                ext = token[5:]
            elif token.startswith("in:"):
                container = token[3:]
            else:
                terms.append(token)
        try:
            result = provider.find(terms, ext=ext, container=container)
        except EntryError as exc:
            fail(exc)
            print(S.dim(_("输入 5 可直接去「修复与更新」。")))
            if ask(_("下一步")) != "5":
                return
            sub(fix_screen, provider, ["rebuild-index"] if exc.code == "index-broken" else None)
            if provider.index_state()["status"] == "broken":
                return
            pending = line  # 修好了：用同一个词再查一次
            continue
        show(cli.render_find(result))
        if result["results"]:
            print(S.dim(_("o1 打开 · f1 显示所在目录 · c1 复制路径 · v1 看版本 · r1 看引用 · 继续输入词再查 · 回车返回")))


def item_screen(provider, item: dict) -> None:
    show(cli.render_item(item))
    while True:
        print(S.dim(_("o 打开权威记录 · d1 打开交付物 · f 显示所在目录 · w 为它记录 · 回车返回")))
        line = ask(_("事项"))
        if line == "o":
            print(provider.act("open", item["path"])["message"])
        elif line == "f":
            print(provider.act("reveal", item["path"])["message"])
        elif line == "w":
            sub(record_screen, provider, item["id"])
        elif re.fullmatch(r"d\d+", line) and 1 <= int(line[1:]) <= len(item["deliverables"]):
            print(provider.act("open", item["deliverables"][int(line[1:]) - 1])["message"])
        else:
            print(S.yellow(_("没有这个操作。")))


def progress_screen(provider) -> None:
    term, expand = None, False
    while True:
        show(cli.header(provider, _("看进展")))
        lines, numbered = cli.render_items(provider.items(term=term), expand_closed=expand)
        show(([S.dim(_("筛选：{0}（输入 * 清除）").format(term))] if term else []) + lines)
        print(S.dim(_("序号看详情 · 输入词查找（标题、别名、旧编号）· all 展开已完成 · 回车返回")))
        line = ask(_("进展"))
        if line.isdigit() and 1 <= int(line) <= len(numbered):
            sub(item_screen, provider, provider.item(numbered[int(line) - 1]["id"]))
        elif line == "all":
            expand = True
        elif line == "*":
            term = None
        else:
            term = line
            matches = provider.items(term=term)["items"]
            if len(matches) == 1:
                sub(item_screen, provider, provider.item(matches[0]["id"]))
                term = None
            elif not matches:
                print(S.yellow(_("没有事项匹配“{0}”。").format(line)))
                term = None
            else:
                expand = True


def guide_screen(provider) -> None:
    show(cli.header(provider, _("项目指南")) + cli.render_guide(provider.guide()))
    ask(_("回车返回"))


def health_screen(provider) -> None:
    show(cli.header(provider, _("检查项目与环境")))
    lines, numbered = cli.render_health(provider.health())
    show(lines)
    while True:
        print(S.dim(_("序号去处理 · 回车返回")))
        line = ask(_("检查"))
        if not (line.isdigit() and 1 <= int(line) <= len(numbered)):
            print(S.yellow(_("没有这一项。")))
            continue
        finding = numbered[int(line) - 1]
        actions = [step["action"] for step in finding["next"] if step["action"]]
        if not actions:
            print(S.dim(_("这一项不能在菜单里直接处理：")) + finding["next"][0]["label"])
            continue
        verb, _sep, target = actions[0].partition(":")
        if verb == "fix":
            sub(fix_screen, provider, [target])
        elif verb == "open":
            print(provider.act("open", target)["message"])
        elif verb == "ext":
            sub(extensions_dups_screen, provider, target)


def fix_screen(provider, preset: list[str] | None = None) -> None:
    show(cli.header(provider, _("修复与更新")))
    print(S.dim(_("流程：检查 → 选择处理项 → 查看影响 → 确认执行 → 验证结果")))
    available = provider.fix_plan([])["steps"]
    if not available:
        print(S.green(_("检查完成：没有需要处理的项。")))
        return
    if preset:
        ids = [step["id"] for step in available if step["id"] in preset]
        titles = [step["title"] for step in available if step["id"] in ids]
        print(S.bold(_("1–2. 检查并选中：")) + (_("、").join(titles) or S.yellow(_("这一项已处理或当前不可处理"))))
        if not ids:
            return
    else:
        print(S.bold(_("1. 检查结果")))
        for number, step in enumerate(available, 1):
            blocked = S.yellow(_("（{0}）").format(step['blocked'])) if step["blocked"] else ""
            print(f" {S.bold(S.cyan(str(number)))}  {step['title']}{blocked}")
        line = ask(_("2. 选择处理项（序号，空格分隔；all 全选）"))
        picks = range(1, len(available) + 1) if line == "all" else \
            [int(token) for token in line.split() if token.isdigit() and 1 <= int(token) <= len(available)]
        ids = [available[number - 1]["id"] for number in picks]
        if not ids:
            print(S.yellow(_("没有选中任何项。")))
            return
    choices: dict[str, str] = {}
    if "add-entry" in ids and not provider.overview().get("launcher"):
        name = choose(_("启动器文件名（回车用 menu）"), "")
        if name:
            choices["entry-name"] = name
    plan = provider.fix_plan(ids, dict(choices))
    print(S.bold(_("3. 查看影响")))
    show(cli.render_plan(plan))
    asked = False
    for step in plan["steps"]:
        for item in step["custom"]:
            if step["blocked"]:
                continue
            if item.get("options"):
                choices[item["path"]] = _decide_item(item, step["payload"].get("diffs", {}).get(item["path"], ""))
                if choices[item["path"]] == "skip" and "_reason" not in choices:
                    choices["_reason"] = choose(_("永久跳过的理由（回车略过；会随项目同步，换一台机器也看得到）"), "")
                asked = True
                continue
            if item.get("content"):
                choices[item["path"]] = _decide_content(item, step["payload"].get("diffs", {}).get(item["path"], ""))
                if choices[item["path"]] == "accept":  # 提示里已写明会丢弃多少行，这里的 a 就是显式确认
                    choices["_confirm"] = "\n".join([*(choices.get("_confirm") or "").split("\n"), item["path"]]).strip("\n")
                asked = True
                continue
            answer = choose(_("{0} {1}：k 不改（默认）/ a 采用").format(item['path'], item['state']), "k") \
                if item["path"] == "README.md" else \
                choose(_("{0} {1}：k 保留你的版本（默认）/ a 采用新模板").format(item['path'], item['state']), "k")
            choices[item["path"]] = "accept" if answer == "a" else "keep"
            asked = True
    if asked:
        plan = provider.fix_plan(ids, choices)
        print(S.bold(_("按你的选择，最终将：")))
        show(cli.render_plan(plan, choices))
    if all(step["blocked"] for step in plan["steps"]):
        print(S.yellow(_("所选项目前都不能执行，只能预览。")))
        return
    print(S.bold(_("4. 确认执行")))
    if not confirm():
        return
    print(S.bold(_("5. 验证结果")))
    show(cli.render_apply(provider.apply_fix(plan, choices)))


def _decide_content(item: dict, diff: str) -> str:
    """模板更新里你改过的文件：保留（默认）/ 采用新模板（写明丢弃多少行）/ 保留并追加区块 / 先看差异。"""
    lost = _("，会丢弃 {0} 行本地内容").format(item["lost"]) if item.get("lost") else ""
    options = {"k": "keep", "a": "accept", **({"p": "append"} if item.get("append") else {})}
    menu = _("k 保留你的版本（默认）/ a 采用新模板（先备份{0}）").format(lost) \
        + (_(" / p 保留原文并追加带标记的区块") if item.get("append") else "") + _(" / d 查看差异")
    while True:
        answer = choose(_("{0} {1}：{2}").format(item['path'], item['state'], menu), "k")
        if answer == "d":
            print(S.dim(diff or _("（没有差异内容可显示）")))
            continue
        if answer in options:
            return options[answer]
        print(S.yellow(_("请输入 {0} 或 d。")).format(_("、").join(options)))


def _decide_item(item: dict, diff: str) -> str:
    """每项四选一：采用 / 永久跳过 / 稍后 / 查看差异（看完再选）。"""
    while True:
        answer = choose(_("{0}（{1}）：a 采用（默认）/ s 永久跳过 / l 稍后 / d 查看差异").format(item['path'], item['state']), "a")
        if answer == "d":
            print(S.dim(diff or _("（没有差异内容可显示）")))
            continue
        if answer in ("a", "s", "l"):
            return {"a": "accept", "s": "skip", "l": "later"}[answer]
        print(S.yellow(_("请输入 a、s、l 或 d。")))


def record_screen(provider, target: str | None = None) -> None:
    show(cli.header(provider, _("记录事项")))
    capability = next(item for item in provider.capabilities() if item["id"] == "record")
    if capability["state"] != "available":
        try:
            provider.record_preview(None, {})
        except EntryError as exc:
            fail(exc)
        return
    if target is None:
        line = ask(_("n 新建 · 或输入事项（编号、名称、旧编号）"))
        target = None if line == "n" else line
    if target is None:
        change = {"title": ask(_("标题")), "goal": ask(_("目标"))}
        try:
            change["next_action"] = ask(_("下一动作（回车跳过）"))
        except Back:
            change["next_action"] = ""
    else:
        try:
            item = provider.item(target)
        except EntryError as exc:
            fail(exc)
            return
        print(f"{S.bold(item['title'])}  [{_(cli.GROUP_LABEL.get(item['group'], item['status']))}]")
        for key, title, _field in RECORD_ACTIONS:
            print(f"  {S.bold(S.cyan(key))}  {_(title)}")
        choice = dict((key, field) for key, _title, field in RECORD_ACTIONS).get(ask(_("要做什么")))
        if choice is None:
            print(S.yellow(_("没有这个操作。")))
            return
        target = item["id"]
        if choice == "close":
            change = {"status": "done"}
        elif choice == "reopen":
            change = {"status": "active"}
            if not item["next_action"]:
                change["next_action"] = ask(_("恢复后的下一动作"))
        elif choice == "waiting":
            change = {"status": "waiting", "waiting_for": ask(_("在等谁或等什么"))}
        elif choice == "deliverable":
            change = {"add_deliverable": [ask(_("文件路径（相对项目根目录，可从「找资料」c1 复制）"))]}
        else:
            change = {choice: ask(_("内容"))}
    try:
        preview = provider.record_preview(target, change)
    except EntryError as exc:
        fail(exc)
        return
    show(cli.render_preview(preview))
    if not confirm(_("输入 yes 写入，回车取消")):
        return
    try:
        print(S.green(provider.record_apply(preview)["verify"]))
    except EntryError as exc:
        fail(exc)


def tools_screen(provider) -> None:
    tools = provider.tools()
    show(cli.header(provider, _("项目工具")) + cli.render_tools(tools))
    while tools:
        line = ask(_("序号查看并执行 · 回车返回"))
        if not (line.isdigit() and 1 <= int(line) <= len(tools)):
            print(S.yellow(_("没有这一项。")))
            continue
        tool = tools[int(line) - 1]
        if tool["state"] != "available":
            print(S.yellow(_("“{0}”当前不可用：{1}").format(tool['title'], tool['reason'])))
            continue
        show(cli.render_tools([tool]))
        if confirm():
            try:
                print(provider.run_tool(tool["id"])["message"])
            except EntryError as exc:
                fail(exc)
    if not tools:
        ask(_("回车返回"))


def sop_screen(provider) -> None:
    while True:
        code, data, lines = provider.sop(["list", "--all"])
        show(cli.header(provider, _("流程（SOP）")) + lines)
        rows = data["sops"]
        print(S.dim(_("序号看详情 · d 哪些到期了 · n 新建草稿 · 回车返回")))
        line = ask(_("流程"))
        if line == "d":
            show(provider.sop(["due"])[2])
        elif line == "n":
            sub(sop_new_screen, provider)
        elif line.isdigit() and 1 <= int(line) <= len(rows):
            sub(sop_detail_screen, provider, rows[int(line) - 1]["id"])
        else:
            print(S.yellow(_("没有这一项。")))


def sop_new_screen(provider) -> None:
    slug = ask(_("英文短名（小写字母、数字、连字符）"))
    template = choose(_("样例：blank 空白 / video-to-transcript 视频转文字 / weekly-review 周复盘（回车用 blank）"), "blank")
    argv = ["new", "--slug", slug, "--template", template]
    show(provider.sop(argv)[2])
    if confirm():
        show(provider.sop([*argv, "--apply"])[2])
        print(S.dim(_("下一步：让 Agent 带你谈清触发、工具、出错和中间产物的约定，或运行 sop interview ") + slug))


def sop_detail_screen(provider, sop_id: str) -> None:
    show(provider.sop(["show", sop_id])[2])
    actions = {"c": _("执行前检查"), "q": _("还要谈清的事"), "v": _("校验"), "r": _("按它执行（先预览）"), "a": _("启用草稿（先预览）")}
    while True:
        print(S.dim(" · ".join(f"{key} {label}" for key, label in actions.items()) + _(" · 回车返回")))
        line = ask(_("流程"))
        if line == "c":
            show(provider.sop(["preflight", sop_id])[2])
        elif line == "q":
            show(provider.sop(["interview", sop_id])[2])
        elif line == "v":
            show(provider.sop(["validate", sop_id])[2])
        elif line in ("r", "a"):
            argv = ["run", sop_id] if line == "r" else ["activate", sop_id]
            code, _unused, lines = provider.sop(argv)
            show(lines)
            if code == 0 and confirm():
                show(provider.sop([*argv, "--apply"])[2])
        else:
            print(S.yellow(_("没有这个操作。")))


# ---------------------------------------------------------------- 第 9 屏：技能与 MCP（只读）

def extensions_screen(provider) -> None:
    while True:
        show(cli.header(provider, _("技能与 MCP")))
        state = provider.extensions_summary()
        if state["state"] != "available":
            show(ext.render_unavailable(state["reason"]))
            ask(_("回车返回"))
            return
        show(ext.render_summary(state["summary"]))
        print()
        for key, title, hint, ready in EXTENSION_ENTRIES:
            note = "" if ready else "  " + S.yellow(_("本版尚未提供"))
            print(f"  {S.bold(S.cyan(key))}  {cli.pad(_(title), 16)}{S.dim(_(hint))}{note}")
        print(S.dim(_("\n  输入序号；或输入条目名直接看详情。 ? 帮助 · 回车返回")))
        line = ask(_("技能与 MCP"))
        screens = {"1": extensions_clients_screen, "2": extensions_projects_screen, "3": extensions_dups_screen,
                   "4": extensions_check_screen, "5": extensions_pick_move, "6": extensions_pick_scope}
        if line in screens:
            sub(screens[line], provider)
        elif line in {entry[0] for entry in EXTENSION_ENTRIES}:
            print(S.yellow(_("这一项本版尚未提供；现在只能看，不能改。")))
        elif line in ("?", "help", "帮助"):
            print(extensions_help())
        else:
            sub(extensions_show_screen, provider, line)


def extensions_help() -> str:
    return (S.bold(_("怎么用")) + "\n"
            + _("  1–4 进入只读的清单；输入条目名（或 kind:名@客户端:作用域）看详情；回车返回，q 退出。") + "\n"
            + _("  清单与体检只读，不会改任何文件；5、6 与 m / s / d / p 是带预览的写入向导，确认前零写入。MCP 只显示名字、作用域、指纹前缀与变量名，不显示命令参数或任何值。") + "\n"
            + _("  「未知」表示证据不足，不做结论。命令行：extensions list / dups / show / check（--output json）。") + "\n"
            + _("  写入向导先预览，输入 yes 才执行，放宽另需键入条目名；命令行：extensions move / dedupe / scope。"))


def _extensions_view(provider, title: str, fetch, render) -> None:
    while True:
        data = fetch()
        show(cli.header(provider, title))
        show(render(data))
        print(S.dim(_("输入条目名看详情 · 回车返回")))
        sub(extensions_show_screen, provider, ask(_("条目")))


def extensions_clients_screen(provider) -> None:
    _extensions_view(provider, _("技能与 MCP · 按客户端看"), provider.extensions_list,
                     lambda data: ext.render_list(data, data["summary"]))


def extensions_projects_screen(provider) -> None:
    _extensions_view(provider, _("技能与 MCP · 按项目看"), provider.extensions_projects,
                     lambda data: ext.render_projects(data, data["summary"]))


def extensions_show_screen(provider, token: str) -> None:
    try:
        data = provider.extensions_show(token)
    except EntryError as exc:
        fail(exc)
        return
    if data["view"] == "entry":
        show(ext.render_show(data["entry"]))
        extensions_entry_actions(provider, data["entry"])
        return
    show(ext.render_group(1, data["group"]))
    ask(_("回车返回"))


def extensions_dups_screen(provider, focus: str | None = None) -> None:
    show(cli.header(provider, _("技能与 MCP · 按重复看")))
    lines, numbered = ext.render_dups(provider.extensions_dups())
    show(lines)
    if focus and any(group["id"] == focus for group in numbered):
        sub(extensions_group_screen, provider, next(group for group in numbered if group["id"] == focus))
    while numbered:
        print(S.dim(_("序号看这一组的成员详情 · 回车返回")))
        line = ask(_("重复"))
        if line.isdigit() and 1 <= int(line) <= len(numbered):
            sub(extensions_group_screen, provider, numbered[int(line) - 1])
        else:
            print(S.yellow(_("没有这一项。")))
    if not numbered:
        ask(_("回车返回"))


def extensions_group_screen(provider, group: dict) -> None:
    show(ext.render_group(1, group))
    for ref in group["members"]:
        print()
        show(ext.render_show(provider.extensions_show(ref)["entry"]))
    target = dedupe_group_for(provider, group)
    print()
    while True:
        if target is None:
            print(S.yellow(_(actions.NO_WRITE_GROUP)))
            ask(_("回车返回"))
            continue
        print(S.dim(_("d 处理这一组重复（先预览） · 回车返回")))
        if ask(_("重复")) == "d":
            sub(dedupe_wizard, provider, group, target)
            target = dedupe_group_for(provider, group)
        else:
            print(S.yellow(_("没有这个操作。")))


def extensions_check_screen(provider) -> None:
    similar = False
    while True:
        show(cli.header(provider, _("技能与 MCP · 技能体检")))
        show(ext.render_check(provider.extensions_check(similar)))
        print(S.dim(_("d 同时比较 description 相似度（只作提示）· 回车返回")))
        if ask(_("体检")) == "d":
            similar = True
        else:
            print(S.yellow(_("没有这个操作。")))


def help_text() -> str:
    bold, cyan = S.bold, S.cyan
    return (bold(_("怎么用")) + "\n"
            + _("  首页输入序号进入；直接输入词就是找资料。各页回车返回上一级，q 随时退出。") + "\n"
            + _("  找资料结果：{0} 打开 · {1} 显示所在目录 · {2} 复制路径 · {3} 看版本 · {4} 看引用").format(
                cyan("o1"), cyan("f1"), cyan("c1"), cyan("v1"), cyan("r1")) + "\n"
            + _("  写入类操作（修复、记录、工具）都先显示影响，输入 yes 才执行。") + "\n"
            + bold(_("不进菜单也可以")) + "\n"
            + _("  ./<启动器> find 词 · progress · show 事项 · guide · health · fix · record · tools · sop 子命令") + "\n"
            + _("  加 --output json 得到结构化结果；脚本里从不等待输入。完整说明：--help"))


SCREENS = {"1": find_screen, "2": progress_screen, "3": guide_screen, "4": health_screen,
           "5": fix_screen, "6": record_screen, "7": tools_screen, "8": sop_screen, "9": extensions_screen}


def run(provider) -> int:
    ATTENTION[0] = False
    try:
        import readline  # noqa: F401  line editing and history
    except ImportError:
        pass
    try:
        while True:
            home(provider)
            try:
                line = ask(_("选择"))
            except Back:
                continue
            try:
                if line in SCREENS:
                    SCREENS[line](provider)
                elif line in ("?", "help", "帮助"):
                    print(help_text())
                else:
                    find_screen(provider, line)
            except Back:
                continue
            except EntryError as exc:
                fail(exc)
    except Quit:
        return cli.EXIT_ATTENTION if ATTENTION[0] else cli.EXIT_OK
    except KeyboardInterrupt:
        print()
        return cli.EXIT_INTERRUPTED


# ---------------------------------------------------------------- 第 9 屏的写入向导：选条目 → 选目标 → 预览 → yes → 执行 → 读回

ATTENTION = [False]  # 任何一次写入失败或读回不一致后，菜单退出时返回 EXIT_ATTENTION
EXT_CLIENTS = ("claude", "codex", "cursor")
EXT_SCOPES = ("user", "project")


def dedupe_group_for(provider, group: dict) -> dict | None:
    """菜单侧的分组对应 ``agenthelm dedupe --list`` 里的哪一组；没有对应就没有写入动作。"""
    members = set(group["members"])
    for candidate in provider.extensions_dedupe_groups():
        refs = {member.get("ref") for member in candidate.get("members", [])}
        if candidate.get("kind") == group["kind"] and refs and refs <= members:
            return candidate
    return None


def extensions_entry_actions(provider, entry: dict) -> None:
    """条目详情下面的动作：m 移动或复制 · s 设置生效范围 · d 处理重复 · p 可粘贴片段。"""
    while True:
        keys = [("m", _("移动或复制")), ("s", _("设置生效范围")), ("d", _("处理重复")), ("p", _("可粘贴片段"))]
        print(S.dim(" · ".join(f"{key} {label}" for key, label in keys) + _(" · 回车返回")))
        line = ask(_("回车返回"))
        if line == "m":
            sub(extensions_move_wizard, provider, entry)
        elif line == "s":
            sub(extensions_scope_wizard, provider, entry)
        elif line == "d":
            sub(extensions_entry_dedupe, provider, entry)
        elif line == "p":
            show(extensions_manual_lines(provider, entry))
        else:
            print(S.yellow(_("没有这个操作。")))


def _pick_entry(provider, title: str) -> dict | None:
    token = ask(_("条目名或引用（kind:名@客户端:作用域）"))
    try:
        data = provider.extensions_show(token)
    except EntryError as exc:
        fail(exc)
        return None
    if data["view"] != "entry":
        print(S.yellow(_("请输入条目，而不是分组 id。")))
        return None
    return data["entry"]


def extensions_pick_move(provider) -> None:
    show(cli.header(provider, _("技能与 MCP · 移动或复制")))
    entry = _pick_entry(provider, "")
    if entry:
        show(ext.render_show(entry))
        extensions_move_wizard(provider, entry)


def extensions_pick_scope(provider) -> None:
    show(cli.header(provider, _("技能与 MCP · 设置生效范围")))
    entry = _pick_entry(provider, "")
    if entry:
        show(ext.render_show(entry))
        extensions_scope_wizard(provider, entry)


def _read_only(provider, entry: dict, reason: str) -> None:
    print(S.yellow(reason))
    show(extensions_manual_lines(provider, entry))


def _write_wizard(provider, view: str, preview, apply, *, name: str, group_id: str | None = None) -> None:
    """预览 → 放宽另需键入条目名 → 输入 yes → 执行 → 读回。任何一步取消都没有写入。"""
    result = preview()
    show(actions.render_action(result))
    if result.get("unsupported") or result.get("blocked"):
        print(S.yellow(_("计划有 blocked 项，不可执行；没有写入。")))
        return
    plan = result["plan"]
    if plan.get("direction") == "widen":
        print(S.red(_("放宽：此后所有项目都会看到它。")))
        try:
            typed = ask(_("键入条目名 {0} 确认放宽").format(name))
        except Back:
            typed = ""
        if typed != name:
            print(S.dim(_("已取消，没有写入。")))
            return
    if not confirm():
        return
    done = apply(plan["plan_digest"])
    attention = actions.finish(provider, view, done, group_id)
    provider.extensions_refresh()
    show(actions.render_action(done))
    if attention:
        ATTENTION[0] = True
        print(S.red(_("需要关注：请看上面的结果与恢复目录。")))


def _unsafe_entry(entry: dict) -> bool:
    return entry["evidence"] == "unknown"


def extensions_move_wizard(provider, entry: dict) -> None:
    if _unsafe_entry(entry):
        return _read_only(provider, entry, _("这个条目的证据为未知：只给只读信息与手工步骤，不写入。"))
    if entry["kind"] != "skill":
        return _read_only(provider, entry, _("MCP 条目本版只给手工命令与可粘贴片段，不由本菜单写入。"))
    targets = [(client, scope) for client in EXT_CLIENTS for scope in EXT_SCOPES if (client, scope) != (entry["client"], entry["scope"])]
    print(S.bold(_("目标（客户端:作用域）")))
    for index, (client, scope) in enumerate(targets, 1):
        widen = entry["scope"] == "project" and scope == "user"
        print(f"  {S.bold(S.cyan(str(index)))}  {client}:{scope}" + (S.red("  " + _("放宽")) if widen else ""))
    line = ask(_("选择目标"))
    if not line.isdigit() or not 1 <= int(line) <= len(targets):
        print(S.yellow(_("没有这一项。")))
        return
    client, scope = targets[int(line) - 1]
    to = f"{client}:{scope}"
    keep_source = choose(_("k 保留源（复制），回车 归档源（移动）"), "") == "k"
    options = {"keep_source": keep_source, "allow_widen": entry["scope"] == "project" and scope == "user"}
    probe = provider.extensions_move(entry["ref"], to, **options)
    if probe["plan"] and probe["plan"].get("confirmations"):
        options["confirm_content"] = True  # 预览里已列出「将进入项目目录」；用户输入 yes 才算确认
    if entry["scope"] == "user" and scope == "project":
        print(S.yellow(_("将进入项目目录：内容可能被提交到版本库；许可证与私有内容请你自己确认。")))
    if scope == "project" or entry["scope"] == "user":
        print(S.dim(_("受影响项目：未知（只能看到在本机同步过的项目）")))
    _write_wizard(provider, "move", lambda: provider.extensions_move(entry["ref"], to, **options),
                  lambda digest: provider.extensions_move(entry["ref"], to, apply=True, expect=digest, **options), name=entry["name"])


def extensions_scope_wizard(provider, entry: dict) -> None:
    if _unsafe_entry(entry):
        return _read_only(provider, entry, _("这个条目的证据为未知：只给只读信息与手工步骤，不写入。"))
    if entry["kind"] != "skill":
        return _read_only(provider, entry, _("MCP 条目本版只给手工命令与可粘贴片段，不由本菜单写入。"))
    print(S.bold(_("生效范围")))
    print(f"  {S.bold(S.cyan('1'))}  " + _("仅此项目（收窄）"))
    print(f"  {S.bold(S.cyan('2'))}  " + _("全局（所有项目；放宽，需键入条目名）"))
    line = ask(_("选择"))
    if line not in ("1", "2"):
        print(S.yellow(_("没有这一项。")))
        return
    effective = "project" if line == "1" else "user"
    options = {"allow_widen": effective == "user"}
    _write_wizard(provider, "scope", lambda: provider.extensions_scope(entry["ref"], effective, **options),
                  lambda digest: provider.extensions_scope(entry["ref"], effective, apply=True, expect=digest, **options), name=entry["name"])


def extensions_entry_dedupe(provider, entry: dict) -> None:
    if not entry["groups"]:
        print(S.yellow(_("这个条目不在任何重复组里。")))
        return
    analysis = provider._extensions()["analysis"]
    group = next(item for item in analysis["groups"] if item["id"] == entry["groups"][0])
    target = dedupe_group_for(provider, group)
    if target is None:
        print(S.yellow(_(actions.NO_WRITE_GROUP)))
        return
    dedupe_wizard(provider, group, target)


def dedupe_wizard(provider, group: dict, target: dict) -> None:
    print(S.bold(_("处置：{0}").format(group["name"])))
    for key, label in (("1", _("保留一份，退役其余")), ("2", _("都保留并标注")), ("3", _("在本机忽略这一组")), ("4", _("撤销忽略"))):
        print(f"  {S.bold(S.cyan(key))}  {label}")
    line = ask(_("选择"))
    gid, options = target["id"], {}
    if line == "1":
        refs = [member["ref"] for member in target["members"]]
        for index, ref in enumerate(refs, 1):
            print(f"  {S.bold(S.cyan(str(index)))}  {ref}")
        pick = ask(_("保留哪一份"))
        if not pick.isdigit() or not 1 <= int(pick) <= len(refs):
            print(S.yellow(_("没有这一项。")))
            return
        options = {"keep": refs[int(pick) - 1], "retire_others": True}
    elif line == "2":
        options = {"keep_all": True, "note": ask(_("备注（说明为什么保留）"))}
    elif line in ("3", "4"):
        options = {"ignore": True} if line == "3" else {"unignore": True}
    else:
        print(S.yellow(_("没有这一项。")))
        return
    _write_wizard(provider, "dedupe", lambda: provider.extensions_dedupe(gid, **options),
                  lambda digest: provider.extensions_dedupe(gid, apply=True, expect=digest, **options), name=group["name"], group_id=gid)


def extensions_manual_lines(provider, entry: dict) -> list[str]:
    """手工步骤与可粘贴片段：不写任何文件，不含任何值。"""
    lines = [S.bold(_("手工步骤与可粘贴片段（本屏不写入）"))]
    if _unsafe_entry(entry):
        return lines + [_("证据未知：无法确认这个条目的真实状态。请自己确认路径与客户端的加载情况，再手工复制或移动；之后到客户端里重启确认。")]
    try:
        plan = provider.extensions_scope(entry["ref"], "project").get("plan") or {}
    except EntryError:
        plan = {}
    if plan.get("snippet"):
        lines += [_("Project Profile 的声明片段（合并进 capabilities；之后用 agent-pack 的 sync 预览落地）："),
                  *json.dumps(plan["snippet"], ensure_ascii=False, indent=2).splitlines()]
    if entry["kind"] == "mcp":
        if entry["client"] == "claude":
            lines += [_("官方命令（先自己备份 ~/.claude.json）："), f"  claude mcp remove --scope {entry['scope']} {entry['name']}"]
        group = next((item for item in provider.extensions_dedupe_groups() if entry["ref"] in {m.get("ref") for m in item.get("members", [])}), None)
        lines.append(_("Claude Desktop 的可粘贴片段：") + (f"agenthelm dedupe {group['id']} --keep {entry['ref']} --desktop-snippet" if group
                                                        else _("先用 agenthelm dedupe --list 找到该条目所在的分组")))
    return lines
