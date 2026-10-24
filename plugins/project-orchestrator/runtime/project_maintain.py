"""项目入口的维护：版本检查，以及“检查 → 选择 → 看影响 → 确认 → 验证”的处理项。

处理项按影响对象分开，互不混合：

- ``rebuild-index``：重建资料索引，只写用户缓存；
- ``update-plugins``：各客户端里的插件代码（调用客户端自己的市场命令，需联网）；
- ``update-content``：项目里由模板生成的文件，改过的文件逐个保留或采用，先备份；
- ``install-runtime`` / ``update-runtime``：用户级运行时，新版本装在旁边，旧版本保留；
- ``add-entry``：为已初始化的项目补上根目录启动器与 Profile 的 entry 段。

个人规则（Opinion）的新版本只报告，从不在这里采纳：那由 opinion-manager 预览后经用户确认。
插件、模板与运行时的检查经 agent-pack（``HOST``）完成；没有它时这些项标为不可用。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import assets
from project_base import DEFAULT_LAUNCHER, LAUNCHER_RE, EntryError

CLIENT_LABEL = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "legacy": "旧方式安装"}


def _iso(seconds: float | None) -> str:
    return datetime.fromtimestamp(seconds).astimezone().replace(microsecond=0).isoformat() if seconds else ""


def runtime_module(host: Any) -> Any:
    path = Path(host.__file__).resolve().parent / "agenthelm_runtime.py"
    spec = importlib.util.spec_from_file_location("agenthelm_runtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------- check

def check_updates(provider: Any, findings: list[dict], ok: list[str]) -> dict:
    host = provider.host
    if host is None:
        return {"status": "unavailable", "cached_at": ""}
    env = host.current_env()
    try:
        report = host.run_check(env, provider.root, offline=provider.offline)
    except (host.CheckError, OSError, ValueError) as exc:
        findings.append({"id": "update-check", "level": "unavailable", "area": "更新", "title": "无法检查更新",
                         "reason": str(exc), "impact": "不影响找资料、看进展等日常功能",
                         "next": [{"label": "联网后重新检查", "action": ""}]})
        provider._report = None
        _check_runtime(provider, env, None, findings, ok)
        return {"status": "offline", "cached_at": ""}
    provider._report = report
    origin = report["latest"]["source"]
    if origin.get("stale") or (provider.offline and origin.get("cached")):
        source = {"status": "offline", "cached_at": _iso(origin.get("fetched_at"))}
    else:
        source = {"status": "cache" if origin.get("cached") else "online", "cached_at": _iso(origin.get("fetched_at"))}
    _plugin_findings(provider, env, report, findings, ok)
    _content_findings(report, findings, ok)
    _opinion_findings(report, findings, ok)
    _check_runtime(provider, env, report["_latest"], findings, ok)
    return source


def _plugin_findings(provider: Any, env: Any, report: dict, findings: list[dict], ok: list[str]) -> None:
    host = provider.host
    plugins = [item for item in report["plugins"] if item["client"] != "legacy"]
    behind = [item for item in plugins if item["status"] == "behind"]
    if behind:
        breaking = [item for item in behind if item["breaking"]]
        details = "；".join(f"{item['plugin']} {item['version']} → {item['latest']}（{CLIENT_LABEL[item['client']]}）"
                           for item in behind)
        findings.append({"id": "update-plugins", "level": "update", "area": "插件",
                         "title": f"{len(behind)} 个插件有新版本" + ("（含不兼容变更）" if breaking else ""),
                         "reason": details, "impact": "只影响 AI 客户端里的插件；不更新也能继续使用本菜单",
                         "next": [{"label": "查看更新内容与迁移说明，再决定是否更新", "action": "fix:update-plugins"}]})
    elif plugins:
        ok.append("客户端插件（最新）")
    if not plugins:
        clients = host.detect_clients(env)
        if clients:
            findings.append({"id": "no-plugins", "level": "unavailable", "area": "插件",
                             "title": "AI 客户端里还没有安装 agenthelm 插件",
                             "reason": "检测到 " + "、".join(CLIENT_LABEL.get(c, c) for c in clients) + "，但没有市场安装",
                             "impact": "只影响 AI Agent 使用；本菜单不受影响",
                             "next": [{"label": "需要时运行 agenthelm install 预览安装", "action": ""}]})
        else:
            findings.append({"id": "no-ai-client", "level": "unavailable", "area": "环境", "title": "本机没有 AI 客户端",
                             "reason": "没有找到 Claude Code、Codex 或 Cursor",
                             "impact": "本菜单的日常功能全部不受影响；只有“安装插件到客户端”不可用",
                             "next": [{"label": "需要时安装任一客户端，再运行 agenthelm install", "action": ""}]})
    if report["summary"]["legacy_installs"]:
        findings.append({"id": "legacy", "level": "suggest", "area": "插件", "title": "有旧方式安装待迁移",
                         "reason": "旧的复制 + 软链安装与市场安装并存会重复", "impact": "Agent 里可能看到重复的 Skill",
                         "next": [{"label": "运行 agenthelm migrate --install 预览迁移", "action": ""}]})
    for issue in host.doctor(env, report)["issues"]:
        if issue["issue"] in ("version-skew", "duplicate", "incomplete"):
            findings.append({"id": f"doctor-{issue['issue']}", "level": "suggest", "area": "插件",
                             "title": {"version-skew": "各客户端插件版本不一致", "duplicate": "插件重复安装",
                                       "incomplete": "插件没有装全"}[issue["issue"]],
                             "reason": issue["detail"], "impact": "只影响 AI Agent 使用",
                             "next": [{"label": "运行 agenthelm doctor 查看", "action": ""}]})


def _content_findings(report: dict, findings: list[dict], ok: list[str]) -> None:
    updates = [item for item in report["content"] if item.get("status") in ("update-available", "conflict")]
    for item in report["content"]:
        if item.get("pack") is None:
            findings.append({"id": "content-lock", "level": "suggest", "area": "项目模板", "title": "内容锁无法读取",
                             "reason": item.get("error", ""), "impact": "模板更新无法判断哪些文件改过",
                             "next": [{"label": "检查 .agents/moe.sakanano.agent-pack/content.lock.json", "action": ""}]})
    if updates:
        changed = sorted({path for item in updates for path in item["locally_changed"]})
        findings.append({"id": "update-content", "level": "update", "area": "项目模板",
                         "title": "项目模板/样例有新版本",
                         "reason": "；".join(f"{item['pack']} {item['version']} → {item['latest']}" for item in updates)
                         + (f"；你改过的：{'、'.join(changed)}" if changed else ""),
                         "impact": "不更新不影响使用；更新时你改过的文件逐个选择保留或采用",
                         "next": [{"label": "查看影响并选择", "action": "fix:update-content"}]})
    elif report["content"]:
        ok.append("项目模板（最新）")


def _opinion_findings(report: dict, findings: list[dict], ok: list[str]) -> None:
    for item in report["opinion"]:
        if item["status"] == "update-available":
            refs = "；".join(f"{update['current']} → {update['available'][-1]}" for update in item["updates"])
            findings.append({"id": "opinion-updates", "level": "update", "area": "个人规则",
                             "title": "行为规则模板有新版本", "reason": refs,
                             "impact": "工具升级不会自动采纳个人规则；当前规则继续有效",
                             "next": [{"label": "需要时在 Agent 会话中用 opinion-manager 预览后确认", "action": ""}]})
        elif item["status"] in ("error", "unknown"):
            findings.append({"id": "opinion-check", "level": "unavailable", "area": "个人规则",
                             "title": "行为规则版本无法检查", "reason": item.get("error", ""),
                             "impact": "只影响规则更新提示", "next": []})
        else:
            ok.append("行为规则（已是锁定的最新版本）")


def _check_runtime(provider: Any, env: Any, latest: dict | None, findings: list[dict], ok: list[str]) -> None:
    runtime = runtime_module(provider.host)
    data = runtime.report(env, latest)
    active = data["current"]
    if active is None:
        findings.append({"id": "install-runtime", "level": "suggest", "area": "运行时", "title": "还没有安装用户级运行时",
                         "reason": f"本次由 {data['running_from']} 提供；装好运行时后，不依赖 AI 客户端与 npx 也能打开菜单",
                         "impact": "现在能用；换一台没有 AI 客户端的机器或客户端插件被移除时会打不开",
                         "next": [{"label": "安装运行时（只写用户数据目录）", "action": "fix:install-runtime"}]})
    elif data["update_available"]:
        findings.append({"id": "update-runtime", "level": "update", "area": "运行时", "title": "运行时有新版本",
                         "reason": f"当前 {active['version']}，最新 {data['latest_release']}",
                         "impact": "不更新也能继续使用", "next": [{"label": "下载并切换（旧版本保留）", "action": "fix:update-runtime"}]})
    else:
        ok.append(f"运行时 {active['version']}")


# ---------------------------------------------------------------- plan

FIXES = ("rebuild-index", "update-plugins", "update-content", "install-runtime", "update-runtime", "add-entry")


def available_ids(provider: Any) -> list[str]:
    health = provider._health or provider.health()
    found = []
    for finding in health["findings"]:
        for step in finding["next"]:
            verb, _, target = step["action"].partition(":")
            if verb == "fix" and target in FIXES and target not in found:
                found.append(target)
    return found


def plan(provider: Any, ids: list[str], choices: dict[str, str]) -> dict:
    offered = available_ids(provider)
    chosen = ids or offered
    unknown = [fix_id for fix_id in chosen if fix_id not in FIXES]
    if unknown:
        raise EntryError("没有这些处理项：" + "、".join(unknown), "not-found", ["可处理：" + ("、".join(offered) or "（无）")])
    steps = [_plan_step(provider, fix_id, choices) for fix_id in chosen]
    return {"steps": steps, "simulated": False, "update_source": (provider._health or {}).get("update_source")}


def _step(fix_id: str, title: str, **values: Any) -> dict:
    step = {"id": fix_id, "title": title, "kind": fix_id, "network": False, "changes": [], "writes": [],
            "custom": [], "breaking": False, "recovery": "", "verify": "", "blocked": "", "payload": {}}
    step.update(values)
    return step


def _need_host(provider: Any, fix_id: str, title: str) -> dict | None:
    if provider.host is None:
        return _step(fix_id, title, blocked="当前运行方式没有携带 agent-pack：用 agenthelm 运行时打开后再处理")
    return None


def _plan_step(provider: Any, fix_id: str, choices: dict[str, str]) -> dict:
    if fix_id == "rebuild-index":
        state_dir = provider.assets_project().state_dir()
        return _step(fix_id, "重建资料索引", changes=["按当前文件重新登记资料、版本与引用"],
                     writes=[{"path": str(state_dir), "scope": "用户缓存（不改项目文件）"}],
                     recovery="不涉及项目文件；索引可随时再次重建", verify="重新读取索引状态")
    if fix_id == "add-entry":
        return _plan_entry(provider, choices)
    blocked = _need_host(provider, fix_id, fix_id)
    if blocked:
        return blocked
    host = provider.host
    env = host.current_env()
    if fix_id in ("install-runtime", "update-runtime"):
        return _plan_runtime(provider, env, fix_id)
    report = getattr(provider, "_report", None)
    if report is None:
        return _step(fix_id, fix_id, blocked="版本信息不可用（离线且没有缓存）：联网后重新检查")
    if fix_id == "update-plugins":
        steps = host.update_steps(env, report, None, None, env.home, [], [], None, "plugins")
        behind = [item for item in report["plugins"] if item["status"] == "behind"]
        changes = [f"{item['plugin']} → {step['to']}：{step['summary']}" for item in behind for step in item["steps"]]
        actions = [action for item in behind for step in item["steps"] for action in step["actions"]]
        blocked = next((step.blocked for step in steps if step.blocked), "") or \
            ("当前离线：需要联网，只能预览" if provider.offline else "")
        return _step(fix_id, "更新客户端里的插件", network=True, changes=changes + [f"需要的动作：{a}" for a in actions],
                     writes=[{"path": step.description + (f"（{' '.join(step.command)}）" if step.command else ""),
                              "scope": "客户端插件目录"} for step in steps],
                     breaking=any(item["breaking"] for item in behind),
                     recovery="客户端保留旧版本缓存；需要时用客户端命令回到旧版本",
                     verify="重新检查各客户端已安装的版本", blocked=blocked)
    return _plan_content(provider, env, report, choices)


def _plan_content(provider: Any, env: Any, report: dict, choices: dict[str, str]) -> dict:
    host = provider.host
    accept = sorted(path for path, choice in choices.items() if choice == "accept")
    keep = sorted(path for path, choice in choices.items() if choice == "keep")
    recovery = Path(choices.get("_recovery") or host.recovery_dir_for(env, "content"))
    steps = host.update_steps(env, report, provider.root, None, env.home, accept, keep, recovery, "content")
    if not steps:
        return _step("update-content", "项目模板/样例", blocked="没有需要更新的模板")
    step = steps[0]
    if step.blocked:
        return _step("update-content", step.description, blocked=step.blocked)
    preview = host.execute_steps([step], apply=False, recovery_root=None, child_env=env.child_environment())[0]
    files = (preview.get("result") or {}).get("files") or []
    if preview.get("status") == "failed":
        return _step("update-content", step.description, blocked=preview.get("error", "预览失败"))
    custom = [{"path": item["path"], "state": "你改过", "default": "keep"} for item in files
              if item["state"] == "modified" or (item["path"] in choices and item["state"] in ("modified", "missing"))]
    for item in files:
        if item["state"] == "missing" and not any(entry["path"] == item["path"] for entry in custom):
            custom.append({"path": item["path"], "state": "新文件（目前不存在）", "default": "keep"})
    effective = {item["path"]: choices.get(item["path"], item["default"]) for item in custom}
    writes = []
    for item in files:
        action = item["action"]
        if action == "blocked":
            action = "replace" if effective.get(item["path"]) == "accept" else "keep"
        if action == "skip":
            action = "create" if effective.get(item["path"]) == "accept" else "keep"
        if action in ("replace", "create"):
            writes.append({"path": item["path"], "scope": "项目（先备份）" if action == "replace" else "项目（新建）"})
    # 刷新总会把内容锁更新到新版本（保留的文件沿用旧基线），所以它总在写入范围里。
    writes.append({"path": ".agents/moe.sakanano.agent-pack/content.lock.json", "scope": "项目（内容锁）"})
    return _step("update-content", step.description, changes=[_content_change(item, effective) for item in files],
                 writes=writes, custom=custom, recovery=str(recovery),
                 verify="重新检查模板版本；保留的文件内容不变", payload={"recovery": str(recovery), "files": files})


CONTENT_STATE = {"unchanged": "与新模板相同", "clean": "没改过", "modified": "你改过", "missing": "目前不存在",
                 "local-only": "只有你的改动，新模板没变"}
CONTENT_ACTION = {"keep": "保留", "replace": "换成新模板（先备份）", "create": "新建", "skip": "不新建",
                  "blocked": "需要你选择"}


def _content_change(item: dict, effective: dict[str, str]) -> str:
    action = item["action"]
    if action in ("blocked", "skip"):
        chosen = effective.get(item["path"], "keep")
        action = ("replace" if action == "blocked" else "create") if chosen == "accept" else \
            ("keep" if action == "blocked" else "skip")
    note = "（保留则不含新模板的改动）" if item["state"] == "modified" and action == "keep" else ""
    return f"{item['path']}：{CONTENT_STATE.get(item['state'], item['state'])} → {CONTENT_ACTION.get(action, action)}{note}"


def _plan_runtime(provider: Any, env: Any, fix_id: str) -> dict:
    runtime = runtime_module(provider.host)
    host = provider.host
    title = "安装用户级运行时" if fix_id == "install-runtime" else "更新用户级运行时"
    if fix_id == "install-runtime" and runtime.bundled_root() is not None:
        root, origin = runtime.bundled_root(), "随附内容"
        network = False
    else:
        if provider.offline:
            return _step(fix_id, title, network=True, blocked="当前离线：需要下载发布包，只能预览；也可用 agenthelm runtime install --source <发布包>")
        try:
            latest, _ = host.load_latest(env, None)
        except (host.CheckError, OSError, ValueError) as exc:
            return _step(fix_id, title, network=True, blocked=f"拿不到版本信息：{exc}")
        version = runtime.release_version(latest)
        return _step(fix_id, title, network=True, changes=[f"下载发布包 {version or '（最新）'} 并核对摘要"],
                     writes=[{"path": str(runtime.runtimes_dir(env) / (version or "<最新版本>")), "scope": "用户数据目录（新版本）"},
                             {"path": str(runtime.current_link(env)), "scope": "切换当前版本"}],
                     recovery="旧版本保留；agenthelm runtime use <旧版本> --apply 切回", verify="重新读取当前运行时版本",
                     payload={"from_release": True})
    planned = runtime.plan(env, root, origin)
    return _step(fix_id, title, network=network, changes=[f"运行时 {planned['version']}（{origin}）"],
                 writes=planned["writes"], recovery="旧版本保留；agenthelm runtime use <旧版本> --apply 切回",
                 verify="重新读取当前运行时版本", payload={"from_release": False, "plan": planned})


def _init_script(provider: Any) -> Path | None:
    candidates = [Path(__file__).resolve().parents[2] / "agents-init"]
    if provider.host is not None:
        env = provider.host.current_env()
        candidates += [Path(item["path"]) for item in provider.host.installed_plugins(env, {"agents-init"})]
    for root in candidates:
        script = root / "skills" / "agents-init" / "scripts" / "init_project.py"
        if script.is_file() and "--entry-name" in script.read_text(encoding="utf-8"):
            return script
    return None


def _run_init(provider: Any, name: str, choices: dict[str, str], recovery: str, *, apply: bool) -> dict:
    script = _init_script(provider)
    if script is None:
        raise EntryError("找不到支持生成入口的 agents-init", "unavailable", ["更新插件或运行时后再试"])
    command = [sys.executable, str(script), "--project", str(provider.root), "--mode", "entry",
               "--entry-name", name, "--output", "json"]
    if choices.get(name) in ("keep", "accept"):
        command += [f"--{choices[name]}", name]
    if choices.get(name) == "accept":
        command += ["--recovery-dir", recovery]
    if choices.get("README.md") == "accept":
        command.append("--readme-entry")
    if apply:
        command.append("--apply")
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        raise EntryError("agents-init 没有返回结果：" + (completed.stderr or "").strip()[:200], "failed") from exc


def _recovery_dir(provider: Any, label: str) -> str:
    if provider.host is not None:
        return str(provider.host.recovery_dir_for(provider.host.current_env(), label))
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return str(Path.home() / ".agent-pack" / "recovery" / f"{stamp}-agenthelm-{label}")


def _plan_entry(provider: Any, choices: dict[str, str]) -> dict:
    name = choices.get("entry-name") or provider.launcher() or DEFAULT_LAUNCHER
    if not LAUNCHER_RE.fullmatch(name):
        raise EntryError(f"启动器名称不合法：{name}", "usage", ["只能用字母、数字、. _ -，例如 menu、go"])
    title = f"生成根目录启动器 ./{name}" if provider.launcher() != name else f"完善入口 ./{name}"
    recovery = choices.get("_recovery") or _recovery_dir(provider, "entry")
    try:
        result = _run_init(provider, name, choices, recovery, apply=False)
        if (result.get("entry") or {}).get("collisions") and name not in choices:
            choices = {**choices, name: "keep"}  # 同名文件默认保留：预览就按保留来算写入范围
            result = _run_init(provider, name, choices, recovery, apply=False)
    except EntryError as exc:
        return _step("add-entry", title, blocked=str(exc))
    if result.get("code"):
        return _step("add-entry", title, blocked=result.get("error", "agents-init 拒绝"))
    entry = result.get("entry") or {}
    custom = [{"path": name, "state": "已有同名文件（你的）", "default": "keep"}] \
        if entry.get("action") in ("keep", "replace", "blocked") else []
    readme = provider.root / "README.md"
    has_section = readme.is_file() and "<!-- agenthelm:entry -->" in readme.read_text(encoding="utf-8", errors="replace")
    if not has_section:
        custom.append({"path": "README.md", "state": "可追加「怎样使用」一节（带标记）", "default": "keep"})
    writes = [{"path": path, "scope": "项目"} for path in entry.get("writes", [])]
    return _step("add-entry", title, changes=entry.get("changes", []), writes=writes, custom=custom,
                 recovery=recovery if choices.get(name) == "accept" else "新增文件可直接删除；Profile 改动前后见预览",
                 verify=f"重新读取 Profile 的 entry 段与 ./{name}", payload={"name": name, "recovery": recovery})


# ---------------------------------------------------------------- apply

def apply(provider: Any, planned: dict, choices: dict[str, str]) -> dict:
    results = []
    for step in planned["steps"]:
        if step["blocked"]:
            results.append({"id": step["id"], "status": "skipped", "detail": step["blocked"]})
            continue
        effective = {**{item["path"]: item["default"] for item in step["custom"]}, **choices,
                     "_recovery": step["payload"].get("recovery", "")}
        if step["id"] == "add-entry":
            effective["entry-name"] = step["payload"]["name"]
        fresh = _plan_step(provider, step["id"], effective)
        if [w["path"] for w in fresh["writes"]] != [w["path"] for w in step["writes"]]:
            results.append({"id": step["id"], "status": "skipped",
                            "detail": "预览之后情况有变化（写入范围不同），没有执行：请重新预览"})
            continue
        try:
            results.append(_apply_step(provider, fresh, effective))
        except EntryError as exc:
            results.append({"id": step["id"], "status": "failed", "detail": str(exc)})
            break
    provider._health = None
    return {"results": results, "simulated": False}


def _apply_step(provider: Any, step: dict, choices: dict[str, str]) -> dict:
    fix_id = step["id"]
    base = {"id": fix_id, "status": "applied", "written": [w["path"] for w in step["writes"]], "kept": [],
            "accepted": [], "recovery": step["recovery"]}
    if fix_id == "rebuild-index":
        result = assets.rebuild(provider.assets_project(), force=True)
        state = assets.status(provider.assets_project())
        return {**base, "verify": f"索引{'最新' if state['status'] == 'fresh' else '状态：' + state['status']}，"
                                  f"{result['file_count']} 个文件"}
    if fix_id == "add-entry":
        name = step["payload"]["name"]
        result = _run_init(provider, name, choices, step["payload"]["recovery"], apply=True)
        if not result.get("applied"):
            raise EntryError("没有写入：" + str(result.get("hint") or result.get("error") or result.get("blocked")), "failed")
        launcher = provider.root / name
        ready = launcher.is_file() and launcher.stat().st_mode & 0o111
        return {**base, "verify": f"./{name} {'已就绪' if ready else '未就绪'}；Profile 已声明 entry"}
    host = provider.host
    env = host.current_env()
    if fix_id in ("install-runtime", "update-runtime"):
        runtime = runtime_module(host)
        if step["payload"].get("from_release"):
            import tempfile
            with tempfile.TemporaryDirectory(prefix="agenthelm-runtime-") as temporary:
                latest, _ = host.load_latest(env, None, max_age=0)
                root = host.release_source(latest, Path(temporary))
                planned = runtime.plan(env, root, "GitHub Release（已核对摘要）")
                result = runtime.apply(env, planned, root)
        else:
            planned = step["payload"]["plan"]
            result = runtime.apply(env, planned, runtime.bundled_root())
        return {**base, "verify": result["verify"],
                "recovery": result["rollback"] or step["recovery"], "status": "applied" if result["ok"] else "failed"}
    if fix_id == "update-plugins":
        steps = host.update_steps(env, provider._report, None, None, env.home, [], [], None, "plugins")
        outcome = host.execute_steps(steps, apply=True, recovery_root=None, child_env=env.child_environment())
        failed = [item for item in outcome if item["status"] == "failed"]
        after = host.run_check(env, provider.root, max_age=0)
        behind = [item for item in after["plugins"] if item["status"] == "behind"]
        return {**base, "status": "failed" if failed else "applied",
                "detail": failed[0].get("output", "") if failed else "",
                "verify": "重新检查：" + ("全部为最新" if not behind else f"仍有 {len(behind)} 个落后")}
    # update-content
    custom = {item["path"] for item in step["custom"]}
    keep = [path for path in custom if choices.get(path, "keep") == "keep"]
    accept = [path for path in custom if choices.get(path) == "accept"]
    before = {path: _digest(provider.root / path) for path in keep}
    recovery = Path(step["payload"]["recovery"])
    steps = host.update_steps(env, provider._report, provider.root, None, env.home, accept, keep, recovery, "content")
    outcome = host.execute_steps(steps, apply=True, recovery_root=None, child_env=env.child_environment())[0]
    result = outcome.get("result") or {}
    if outcome["status"] == "failed" or not result.get("applied"):
        raise EntryError("模板更新没有执行：" + str(outcome.get("error") or result.get("hint") or result), "failed")
    untouched = all(_digest(provider.root / path) == digest for path, digest in before.items())
    return {**base, "kept": keep, "accepted": accept, "recovery": result.get("recovery") or "没有替换文件，无需备份",
            "verify": "重新读取：保留的文件" + ("内容未变" if untouched else "被改动了，请检查")
                      + "；内容锁已更新到新版本"}


def _digest(path: Path) -> str | None:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
