"""项目入口的交互菜单：按想完成的任务组织，每一步都能继续操作或回车返回。

只在终端里使用；脚本与 CI 请用 project_cli 的子命令，它们从不等待输入。
"""
from __future__ import annotations

import re

import project_cli as cli
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
            print(provider.run_tool(tool["id"])["message"])
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
           "5": fix_screen, "6": record_screen, "7": tools_screen, "8": sop_screen}


def run(provider) -> int:
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
        return cli.EXIT_OK
    except KeyboardInterrupt:
        print()
        return cli.EXIT_INTERRUPTED
