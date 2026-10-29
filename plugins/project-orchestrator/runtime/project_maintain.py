"""项目入口的维护：版本检查，以及“检查 → 选择 → 看影响 → 确认 → 验证”的处理项。

处理项按影响对象分开，互不混合：

- ``rebuild-index``：重建资料索引，只写用户缓存；
- ``update-plugins``：各客户端里的插件代码（调用客户端自己的市场命令，需联网）；
- ``update-content``：项目里由模板生成的文件，改过的文件逐个保留或采用，先备份；
- ``install-runtime`` / ``update-runtime``：用户级运行时，新版本装在旁边，旧版本保留；
- ``add-entry``：为已初始化的项目补上根目录启动器与 Profile 的 entry 段；
- ``add-skeleton`` / ``profile`` / ``repair-lock``：脚手架差异（缺少的骨架文件、Profile 字段、内容锁版本）；
  逐项可选 采用 / 永久跳过 / 稍后，跳过与稍后记入决定文件（见 agents-init 的 scaffold.py）。

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

SCAFFOLD_NEXT = {"add-skeleton": "按模板补上（只新建，不改已有文件）",
                 "profile": "把字段改为模板值（先备份，预览里有 diff）",
                 "repair-lock": "按实际版本修复内容锁（不涉及你的文件）",
                 "update-content": "逐文件选择保留或采用新模板",
                 "case-naming": "查看改名计划与受影响的引用（只给计划，不移动目录）",
                 "update-region": "更新受管区块：运行 init_project.py --mode region（改过的区块逐个 --keep / --accept）"}


def _scaffold_findings(scaffold: dict | None, findings: list[dict], ok: list[str]) -> None:
    if scaffold is None:
        return
    if scaffold.get("error"):
        findings.append({"id": "scaffold-check", "level": "unavailable", "area": "脚手架", "title": "无法检查项目脚手架",
                         "reason": scaffold["error"], "impact": "只影响脚手架一致性提示", "next": []})
        return
    for item in scaffold["findings"]:
        mark = "（稍后）" if item.get("deferred") else "（期望内容已变化）" if item.get("changed_since_decision") else ""
        # 菜单只执行 FIXES 里的处理项；其余（如 update-region）只给出怎么处理的说明。
        step = {"label": SCAFFOLD_NEXT.get(item["fix"], item["fix"]), "action": f"fix:{item['fix']}" if item["fix"] in FIXES else ""} \
            if item["fix"] else {"label": "这一项需要你自己处理", "action": ""}
        findings.append({"id": item["id"], "level": "suggest", "area": "脚手架", "title": item["title"] + mark,
                         "reason": item["reason"], "impact": "脚手架和最新版不一致；不影响日常使用，"
                         "不想要的可以永久跳过（一次决定，所有机器生效）", "next": [step],
                         "decision": {"id": item["id"], "fingerprint": item["fingerprint"], "scope": "project"}})
    for message in scaffold["decisions"]["errors"]:
        findings.append({"id": "scaffold-decisions", "level": "unavailable", "area": "脚手架",
                         "title": "决定文件有问题", "reason": message, "impact": "已记录的决定暂时不生效", "next": []})
    if scaffold["decisions"]["conflict_copies"]:
        findings.append({"id": "scaffold-decisions-conflict", "level": "suggest", "area": "脚手架",
                         "title": "同步服务留下了决定文件的冲突副本",
                         "reason": "、".join(scaffold["decisions"]["conflict_copies"]),
                         "impact": "另一台机器写入的决定可能没有生效",
                         "next": [{"label": "把其中的条目合并进 check-decisions.json 后删除副本", "action": ""}]})
    if not scaffold["findings"]:
        ok.append("项目脚手架（与期望一致" + (f"，{len(scaffold['skipped'])} 项已跳过" if scaffold["skipped"] else "") + "）")


def check_updates(provider: Any, findings: list[dict], ok: list[str]) -> dict:
    host = provider.host
    if host is None:
        return {"status": "unavailable", "cached_at": ""}
    env = host.current_env()
    # 脚手架与本机检查不依赖联网，先做；决定文件让已决定的项不再出现。
    reprompt = getattr(provider, "reprompt", False)
    scaffold = host.scaffold_report(env, provider.root, reprompt=reprompt) if hasattr(host, "scaffold_report") else None
    machine = host.machine_findings(env, provider.root, reprompt=reprompt) if hasattr(host, "machine_findings") else None
    provider._skipped = [*(scaffold["skipped"] if scaffold else []), *(machine["skipped"] if machine else [])]
    _scaffold_findings(scaffold, findings, ok)
    for item in (machine["findings"] if machine else []):
        if item["id"] == "local:sync-folder":
            findings.append({"id": "sync-folder", "level": "suggest", "area": "环境", "title": item["title"],
                             "reason": item["reason"], "impact": item["impact"],
                             "next": [{"label": "查看同步盘使用指引（只给方案，不改文件、不删内容）", "action": "fix:sync-folder"}],
                             "decision": {"id": item["id"], "fingerprint": item["fingerprint"], "scope": "machine"}})
    try:
        report = host.run_check(env, provider.root, offline=provider.offline)
    except (host.CheckError, OSError, ValueError) as exc:
        findings.append({"id": "update-check", "level": "unavailable", "area": "更新", "title": "无法检查更新",
                         "reason": str(exc), "impact": "不影响找资料、看进展等日常功能",
                         "next": [{"label": "联网后重新检查", "action": ""}]})
        provider._report = None
        _check_runtime(provider, env, None, findings, ok, machine)
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
    _check_runtime(provider, env, report["_latest"], findings, ok, machine)
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


def _check_runtime(provider: Any, env: Any, latest: dict | None, findings: list[dict], ok: list[str],
                   machine: dict | None = None) -> None:
    runtime = runtime_module(provider.host)
    data = runtime.report(env, latest)
    active = data["current"]
    if active is None:
        if machine is not None and not machine["findings"]:
            return  # 用户已决定不提示这台机器的运行时（决定存在用户状态目录，不进项目）
        text = runtime.missing_text(data, runtime.command_status()) if hasattr(runtime, "missing_text") else {
            "reason": f"本次由 {data['running_from']} 提供；装好运行时后，不依赖 AI 客户端与 npx 也能打开菜单",
            "impact": "现在能用；换一台没有 AI 客户端的机器或客户端插件被移除时会打不开",
            "label": "安装运行时（只写用户数据目录）"}
        decision = ({"id": "local:runtime-missing", "fingerprint": "runtime-missing", "scope": "machine"}
                    if machine is not None else None)
        findings.append({"id": "install-runtime", "level": "suggest", "area": "运行时", "title": "这台机器还没有安装用户级运行时",
                         "reason": text["reason"], "impact": text["impact"],
                         "next": [{"label": text["label"], "action": "fix:install-runtime"}],
                         **({"decision": decision} if decision else {})})
    elif data["update_available"]:
        findings.append({"id": "update-runtime", "level": "update", "area": "运行时", "title": "运行时有新版本",
                         "reason": f"当前 {active['version']}，最新 {data['latest_release']}",
                         "impact": "不更新也能继续使用", "next": [{"label": "下载并切换（旧版本保留）", "action": "fix:update-runtime"}]})
    else:
        ok.append(f"运行时 {active['version']}")


# ---------------------------------------------------------------- plan

FIXES = ("rebuild-index", "update-plugins", "update-content", "install-runtime", "update-runtime", "add-entry",
         "add-skeleton", "profile", "repair-lock", "case-naming", "sync-folder")
# 可以带目标的处理项：fix add-skeleton <路径…>、fix profile <字段…>
TARGETED = ("add-skeleton", "profile")


def split_targets(ids: list[str]) -> tuple[list[str], list[str]]:
    """``["add-skeleton", "a.md"]`` -> (["add-skeleton"], ["a.md"]); other ids are returned unchanged."""
    if ids and ids[0] in TARGETED:
        return ids[:1], ids[1:]
    return ids, []


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
    if fix_id in ("add-skeleton", "profile", "repair-lock"):
        return _plan_scaffold(provider, fix_id, choices)
    if fix_id in ("case-naming", "sync-folder"):
        return _plan_guidance(provider, fix_id)
    blocked = _need_host(provider, fix_id, fix_id)
    if blocked:
        return blocked
    host = provider.host
    env = host.current_env()
    if fix_id in ("install-runtime", "update-runtime"):
        return _plan_runtime(provider, env, fix_id)
    report = getattr(provider, "_report", None)
    if report is None and fix_id != "update-content":
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


def _run_refresh(provider: Any, accept: list[str], keep: list[str], recovery: Path, *, apply: bool) -> dict:
    """agents-init 的 refresh：模板包版本没有更新、但有文件与当前模板不同（例如没有基线）时直接用它。"""
    script = _init_script(provider)
    if script is None:
        raise EntryError("找不到支持刷新的 agents-init", "unavailable", ["更新插件或运行时后再试"])
    command = [sys.executable, str(script), "--project", str(provider.root), "--mode", "refresh", "--output", "json"]
    for path in accept:
        command += ["--accept", path]
    for path in keep:
        command += ["--keep", path]
    if apply or accept:
        command += ["--recovery-dir", str(recovery)]
    if apply:
        command.append("--apply")
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
    try:
        result = json.loads(completed.stdout)
    except ValueError as exc:
        raise EntryError("agents-init 没有返回结果：" + (completed.stderr or "").strip()[:200], "failed") from exc
    if "files" not in result:
        raise EntryError(str(result.get("error") or "agents-init 拒绝"), "failed")
    return result


def _content_preview(provider: Any, env: Any, report: dict | None, accept: list[str], keep: list[str],
                     recovery: Path) -> tuple[str, list[dict], str, bool]:
    """(说明, 文件计划, 阻塞原因, 是否走 refresh 直通路径)。"""
    host = provider.host
    steps = host.update_steps(env, report, provider.root, None, env.home, accept, keep, recovery, "content") \
        if report is not None else []
    if steps:
        step = steps[0]
        if step.blocked:
            return step.description, [], step.blocked, False
        preview = host.execute_steps([step], apply=False, recovery_root=None, child_env=env.child_environment())[0]
        if preview.get("status") == "failed":
            return step.description, [], preview.get("error", "预览失败"), False
        return step.description, (preview.get("result") or {}).get("files") or [], "", False
    try:
        result = _run_refresh(provider, accept, keep, recovery, apply=False)
    except EntryError as exc:
        return "项目模板/样例", [], str(exc), True
    files = result.get("files") or []
    if all(item["state"] in ("unchanged", "kept-by-decision") for item in files):
        return "项目模板/样例", [], "没有需要更新的模板", True
    return "按当前模板对比项目契约文件", files, "", True


def _plan_content(provider: Any, env: Any, report: dict | None, choices: dict[str, str]) -> dict:
    host = provider.host
    accept = sorted(path for path, choice in choices.items() if choice == "accept")
    keep = sorted(path for path, choice in choices.items() if choice == "keep")
    recovery = Path(choices.get("_recovery") or host.recovery_dir_for(env, "content"))
    description, files, error, direct = _content_preview(provider, env, report, accept, keep, recovery)
    if error:
        return _step("update-content", description, blocked=error)

    class _Step:  # 保持下面沿用的 step.description 写法
        pass

    step = _Step()
    step.description = description
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
                 verify="重新检查模板版本；保留的文件内容不变",
                 payload={"recovery": str(recovery), "files": files, "direct": direct})


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


# ---------------------------------------------------------------- scaffold fixes

SCAFFOLD_TITLE = {"add-skeleton": "补上缺少的骨架文件", "profile": "修正 Profile 字段", "repair-lock": "修复内容锁版本"}
DECISIONS_FILE = ".agents/moe.sakanano.agent-pack/check-decisions.json"


def scaffold_module(provider: Any) -> Any | None:
    host = provider.host
    return host.scaffold_module(host.current_env()) if host is not None and hasattr(host, "scaffold_module") else None


def _preview_lines(text: str, limit: int = 30) -> str:
    lines = text.splitlines()
    shown = [f"+{line}" for line in lines[:limit]]
    return "\n".join(shown + ([f"... 还有 {len(lines) - limit} 行"] if len(lines) > limit else [])) or "（空文件）"


def _plan_scaffold(provider: Any, fix_id: str, choices: dict[str, str]) -> dict:
    title = SCAFFOLD_TITLE[fix_id]
    module = scaffold_module(provider)
    if module is None:
        return _step(fix_id, title, blocked="需要带脚手架检查的 agents-init（更新插件或运行时后再试）")
    root = provider.root
    state_dir = provider.host.current_env().state_dir
    wanted = [item for item in (choices.get("_targets") or "").split("\n") if item]
    try:
        if fix_id == "repair-lock":
            result = module.repair_lock(root)
            if result["blocked"]:
                return _step(fix_id, title, blocked=result["blocked"])
            return _step(fix_id, title, changes=result["changes"] + [result["diff"]] if result["diff"] else result["changes"],
                         writes=[{"path": ".agents/moe.sakanano.agent-pack/content.lock.json", "scope": "项目（内容锁）"}],
                         recovery="内容锁由工具生成；写入前再次核对内容未变，可用 git 或同步盘历史还原",
                         verify="重新检查：内容锁不再显示 0.0.0")
        report = module.check(root, state_dir=state_dir, reprompt=True)
        recovery = Path(choices.get("_recovery") or _recovery_dir(provider, fix_id))
        if fix_id == "add-skeleton":
            rows = [row["path"] for row in report["skeleton"] if row["state"] == "missing" and row["required"]]
            rows = [p for p in rows if not wanted or p in wanted]
            unknown = [p for p in wanted if p not in {r["path"] for r in report["skeleton"]}]
            if unknown:
                raise EntryError("不在期望骨架清单里：" + "、".join(unknown), "usage")
            if not rows:
                return _step(fix_id, title, blocked="没有缺少的骨架文件")
            ctx = module.Context(root)
            custom = [{"path": p, "state": "缺少", "default": "accept", "options": True} for p in rows]
            decided = {p: choices.get(p, "accept") for p in rows}
            accepted = [p for p, choice in decided.items() if choice == "accept"]
            preview = module.add_skeleton(root, accepted, apply=False, state_dir=state_dir) if accepted else \
                {"create": [], "unchanged": []}
            writes = [{"path": item["path"], "scope": "项目（新建）"} for item in preview["create"]]
            other = {p: c for p, c in decided.items() if c != "accept"}
            if other:
                writes.append({"path": DECISIONS_FILE, "scope": "项目（决定文件，随项目同步）"})
            return _step(fix_id, title, changes=[f"{p}：{ {'accept': '新建', 'skip': '永久跳过', 'later': '稍后'}[c] }"
                                                 for p, c in decided.items()],
                         writes=writes, custom=custom,
                         recovery="只新建文件，已有文件不会被改动；新建的可直接删除",
                         verify="重新检查：这些文件不再显示为缺少",
                         payload={"accepted": accepted, "decisions": other, "recovery": str(recovery),
                                  "diffs": {p: _preview_lines(ctx.expected_text(p) or "") for p in rows}})
        # profile
        rows = {row["field"]: row for row in report["profile"] if row["state"] != "ok" and row["mode"] in ("default", "equal")}
        unknown = [f for f in wanted if f not in rows]
        if unknown:
            raise EntryError("这些字段不需要或不能自动修复：" + "、".join(unknown), "usage")
        fields = [f for f in rows if not wanted or f in wanted]
        if not fields:
            return _step(fix_id, title, blocked="没有需要修正的 Profile 字段")
        decided = {f: choices.get(f, "accept") for f in fields}
        accepted = [f for f, choice in decided.items() if choice == "accept"]
        diffs = {f: module.fix_profile(root, [f], apply=False, state_dir=state_dir)["diff"] for f in fields}
        writes = []
        if accepted:
            writes.append({"path": ".agents/moe.sakanano.agent-pack/project.json", "scope": "项目（先备份）"})
        other = {f: c for f, c in decided.items() if c != "accept"}
        if other:
            writes.append({"path": DECISIONS_FILE, "scope": "项目（决定文件，随项目同步）"})
        return _step(fix_id, title,
                     changes=[f"{f} → {json.dumps(rows[f]['expected'], ensure_ascii=False)}"
                              if decided[f] == "accept" else f"{f}：{ {'skip': '永久跳过', 'later': '稍后'}[decided[f]] }"
                              for f in fields],
                     writes=writes, custom=[{"path": f, "state": "缺少" if rows[f]["state"] == "missing" else "不符合",
                                             "default": "accept", "options": True} for f in fields],
                     recovery=str(recovery), verify="重新检查：这些字段不再显示为缺少或不符合",
                     payload={"accepted": accepted, "decisions": other, "recovery": str(recovery), "diffs": diffs})
    except Exception as exc:  # noqa: BLE001 - init errors come from a separately loaded module copy
        if isinstance(exc, EntryError):
            raise
        return _step(fix_id, title, blocked=str(exc))


PLAN_ONLY = "只给方案，不会自动执行；按方案自己处理后重新检查"


def _plan_guidance(provider: Any, fix_id: str) -> dict:
    """Plan-only items: they explain what to do and change nothing (no directory moves, no deletions)."""
    if fix_id == "case-naming":
        title = "Case 目录命名：改名计划"
        module = scaffold_module(provider)
        if module is None:
            return _step(fix_id, title, blocked="需要带脚手架检查的 agents-init（更新插件或运行时后再试）")
        try:
            plan = module.case_naming_plan(provider.root)
        except Exception as exc:  # noqa: BLE001
            return _step(fix_id, title, blocked=str(_entry_error(exc)))
        if plan["error"]:
            return _step(fix_id, title, blocked="Profile 的 work.case.directory_naming 无法使用：" + plan["error"])
        if not plan["renames"]:
            return _step(fix_id, title, blocked="Case 目录命名与规则一致，没有需要改的")
        changes = []
        for item in plan["renames"]:
            line = f"{item['from']} → {item['to'] or '（需要你决定新名字）'}"
            if item["references"]:
                line += f"；被这些文件提到，改名后要同步：{'、'.join(item['references'][:5])}" \
                        + (f" 等 {len(item['references'])} 个" if len(item["references"]) > 5 else "")
            if item["note"]:
                line += f"（{item['note']}）"
            changes.append(line)
        if plan["declaration_hint"]:
            hint = json.dumps(plan["declaration_hint"]["if_this_is_your_convention"], ensure_ascii=False)
            changes.append("如果这些名字正是你的约定，不用改名：在 Profile 里声明它，之后按声明检查 → " + hint)
        changes.append("每个 Case 的 CASE.md 里的 id 字段也要和新目录名保持一致")
        return _step(fix_id, title, changes=changes, recovery="本处理项不移动、不改写任何文件",
                     verify="按计划改名并更新引用后，重新运行检查", blocked=PLAN_ONLY + "（不会移动目录）")
    title = "同步盘使用指引"
    host = provider.host
    where = host.sync_folder(provider.root) if host is not None and hasattr(host, "sync_folder") else None
    if where is None:
        return _step(fix_id, title, blocked="项目不在同步盘里")
    changes = [
        "随项目同步的：Profile、内容锁、决定文件（check-decisions.json）、所有项目文件",
        "每台机器各一份的：用户级运行时（~/.local/share/agenthelm）、版本缓存、wf 的资料台账缓存（按项目绝对路径命名）、本机决定",
        "所以：两台机器的 wf 结果可能不同——台账各自重建，且取决于各自装的 project-orchestrator 版本；"
        "用 agenthelm check --fingerprint 对比两台机器的脚手架是否一致",
    ]
    if where["rebuildable"]:
        listed = "、".join(where["rebuildable"][:8])
        changes.append(f"项目里有可重建目录：{listed}。它们不需要同步，可以：")
        changes.append("  · 写进 .gitignore（版本管理忽略它们，不影响同步盘）")
        if where["service"] == "iCloud Drive":
            changes.append("  · iCloud：把目录改名为 <名字>.nosync，再建同名符号链接——例如 "
                           "mv node_modules node_modules.nosync && ln -s node_modules.nosync node_modules（需要你自己执行）")
        else:
            changes.append(f"  · {where['service']}：在它的设置里把这些目录设为不同步（选择性同步）")
    changes.append("本工具不会替你移动、改名或删除任何内容")
    return _step(fix_id, title, changes=changes, recovery="本处理项不改任何文件", verify="不涉及",
                 blocked=PLAN_ONLY)


def record_decisions(provider: Any, items: dict[str, str], reason: str, *, prefix: str, apply: bool) -> list[str]:
    """Persist 永久跳过 / 稍后 choices (``{path_or_field: skip|later}``) made while fixing."""
    module = scaffold_module(provider)
    env = provider.host.current_env()
    written = []
    for key, choice in items.items():
        module.decide(provider.root, f"{prefix}:{key}", choice, reason=reason if choice == "skip" else "",
                      apply=apply, state_dir=env.state_dir)
        written.append(f"{prefix}:{key}")
    return written


def _entry_error(exc: Exception) -> EntryError:
    """agents-init raises its own InitError (a separately loaded class): map its code to the entry's."""
    if isinstance(exc, EntryError):
        return exc
    code = {"conflict": "conflict", "profile-invalid": "profile-invalid"}.get(getattr(exc, "code", ""), "usage")
    return EntryError(str(exc), code)


def decide_ids(provider: Any, skip: list[str], unskip: list[str], reason: str, apply: bool) -> dict:
    """``fix --skip <id> --reason …`` / ``--unskip <id>``: only decisions are written, never project files."""
    module = scaffold_module(provider)
    if module is None:
        raise EntryError("需要带脚手架检查的 agents-init（更新插件或运行时后再试）", "unavailable")
    health = provider._health or provider.health()
    known = {f["decision"]["id"]: f["decision"] for f in health["findings"] if f.get("decision")}
    env = provider.host.current_env()
    results = []
    for identifier in skip:
        if identifier not in known:
            raise EntryError(f"现在没有这个发现：{identifier}", "usage", ["可跳过：" + ("、".join(sorted(known)) or "（无）")])
        item = known[identifier]
        try:
            results.append(module.decide(provider.root, identifier, "skip", reason=reason,
                                         fingerprint=item["fingerprint"], scope=item["scope"], apply=apply,
                                         state_dir=env.state_dir))
        except Exception as exc:  # noqa: BLE001
            raise _entry_error(exc) from exc
    for identifier in unskip:
        try:
            results.append(module.undecide(provider.root, identifier, apply=apply, state_dir=env.state_dir))
        except Exception as exc:  # noqa: BLE001
            raise _entry_error(exc) from exc
    provider._health = None
    return {"decisions": results, "applied": apply}


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


def _apply_scaffold(provider: Any, step: dict, choices: dict[str, str], base: dict) -> dict:
    fix_id = step["id"]
    module = scaffold_module(provider)
    env = provider.host.current_env()
    payload = step["payload"]
    if fix_id == "repair-lock":
        result = module.repair_lock(provider.root, apply=True)
        return {**base, "verify": "内容锁版本已修复" if result["applied"] else "没有需要修复的内容"}
    accepted = payload["accepted"]
    written = []
    if fix_id == "add-skeleton" and accepted:
        result = module.add_skeleton(provider.root, accepted, apply=True, state_dir=env.state_dir)
        written = [item["path"] for item in result["create"]]
    if fix_id == "profile" and accepted:
        result = module.fix_profile(provider.root, accepted, apply=True, recovery_dir=Path(payload["recovery"]),
                                    state_dir=env.state_dir)
        written = [".agents/moe.sakanano.agent-pack/project.json"]
    prefix = "skel" if fix_id == "add-skeleton" else "profile"
    recorded = record_decisions(provider, payload["decisions"], choices.get("_reason", ""), prefix=prefix, apply=True)
    return {**base, "written": written + ([DECISIONS_FILE] if recorded else []),
            "verify": "重新检查：" + ("、".join(accepted) or "没有写入文件")
                      + (f"；已记录决定：{'、'.join(recorded)}" if recorded else ""),
            "recovery": payload["recovery"] if fix_id == "profile" and accepted else "只新建文件，无需备份"}


def _apply_step(provider: Any, step: dict, choices: dict[str, str]) -> dict:
    fix_id = step["id"]
    base = {"id": fix_id, "status": "applied", "written": [w["path"] for w in step["writes"]], "kept": [],
            "accepted": [], "recovery": step["recovery"]}
    if fix_id == "rebuild-index":
        result = assets.rebuild(provider.assets_project(), force=True)
        state = assets.status(provider.assets_project())
        return {**base, "verify": f"索引{'最新' if state['status'] == 'fresh' else '状态：' + state['status']}，"
                                  f"{result['file_count']} 个文件"}
    if fix_id in ("add-skeleton", "profile", "repair-lock"):
        try:
            return _apply_scaffold(provider, step, choices, base)
        except Exception as exc:  # noqa: BLE001
            raise _entry_error(exc) from exc
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
    if step["payload"].get("direct"):
        result = _run_refresh(provider, accept, keep, recovery, apply=True)
        if not result.get("applied"):
            raise EntryError("模板更新没有执行：" + str(result.get("hint") or result), "failed")
    else:
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
