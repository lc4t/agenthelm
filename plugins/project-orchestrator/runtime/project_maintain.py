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
from i18n_orchestrator import _, N_  # noqa: E402
from i18n_orchestrator import _  # noqa: E402

CLIENT_LABEL = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "legacy": N_("旧方式安装")}


def _iso(seconds: float | None) -> str:
    return datetime.fromtimestamp(seconds).astimezone().replace(microsecond=0).isoformat() if seconds else ""


def runtime_module(host: Any) -> Any:
    path = Path(host.__file__).resolve().parent / "agenthelm_runtime.py"
    spec = importlib.util.spec_from_file_location("agenthelm_runtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------- check

SCAFFOLD_NEXT = {"add-skeleton": N_("按模板补上（只新建，不改已有文件）"),
                 "profile": N_("把字段改为模板值（先备份，预览里有 diff）"),
                 "repair-lock": N_("按实际版本修复内容锁（不涉及你的文件）"),
                 "update-content": N_("逐文件选择保留或采用新模板"),
                 "case-naming": N_("查看改名计划与受影响的引用（只给计划，不移动目录）"),
                 "update-region": N_("更新受管区块：运行 init_project.py --mode region（改过的区块逐个 --keep / --accept）")}


def _scaffold_findings(scaffold: dict | None, findings: list[dict], ok: list[str]) -> None:
    if scaffold is None:
        return
    if scaffold.get("error"):
        findings.append({"id": "scaffold-check", "level": "unavailable", "area": _("脚手架"), "title": _("无法检查项目脚手架"),
                         "reason": scaffold["error"], "impact": _("只影响脚手架一致性提示"), "next": []})
        return
    for item in scaffold["findings"]:
        mark = _("（稍后）") if item.get("deferred") else _("（期望内容已变化）") if item.get("changed_since_decision") else ""
        # 菜单只执行 FIXES 里的处理项；其余（如 update-region）只给出怎么处理的说明。
        step = {"label": _(SCAFFOLD_NEXT.get(item["fix"], item["fix"])), "action": f"fix:{item['fix']}" if item["fix"] in FIXES else ""} \
            if item["fix"] else {"label": _("这一项需要你自己处理"), "action": ""}
        findings.append({"id": item["id"], "level": "suggest", "area": _("脚手架"), "title": item["title"] + mark,
                         "reason": item["reason"], "impact": _("脚手架和最新版不一致；不影响日常使用，"
                         "不想要的可以永久跳过（一次决定，所有机器生效）"), "next": [step],
                         "decision": {"id": item["id"], "fingerprint": item["fingerprint"], "scope": "project"}})
    for message in scaffold["decisions"]["errors"]:
        findings.append({"id": "scaffold-decisions", "level": "unavailable", "area": _("脚手架"),
                         "title": _("决定文件有问题"), "reason": message, "impact": _("已记录的决定暂时不生效"), "next": []})
    if scaffold["decisions"]["conflict_copies"]:
        findings.append({"id": "scaffold-decisions-conflict", "level": "suggest", "area": _("脚手架"),
                         "title": _("同步服务留下了决定文件的冲突副本"),
                         "reason": _("、").join(scaffold["decisions"]["conflict_copies"]),
                         "impact": _("另一台机器写入的决定可能没有生效"),
                         "next": [{"label": _("把其中的条目合并进 check-decisions.json 后删除副本"), "action": ""}]})
    if not scaffold["findings"]:
        ok.append(_("项目脚手架（与期望一致") + (_("，{0} 项已跳过").format(len(scaffold['skipped'])) if scaffold["skipped"] else "") + _("）"))


def _local_only_finding(item: dict) -> dict:
    """agent-pack 的 local-only 发现 -> 健康检查里的一项；选项里第一个带命令的对应 fix local-only。"""
    steps, linked = [], False
    for option in item.get("options") or []:
        action = "fix:local-only" if option.get("command") and not linked else ""
        linked = linked or bool(action)
        steps.append({"label": option["label"], "action": action})
    return {"id": item["id"], "level": "unavailable" if item.get("kind") == "error" else "suggest", "area": _("同步盘"),
            "title": item["title"], "reason": item["reason"], "impact": item["impact"], "next": steps,
            "decision": {"id": item["id"], "fingerprint": item["fingerprint"], "scope": "machine"}}


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
            findings.append({"id": "sync-folder", "level": "suggest", "area": _("环境"), "title": item["title"],
                             "reason": item["reason"], "impact": item["impact"],
                             "next": [{"label": _("查看同步盘使用指引（只给方案，不改文件、不删内容）"), "action": "fix:sync-folder"}],
                             "decision": {"id": item["id"], "fingerprint": item["fingerprint"], "scope": "machine"}})
        elif item.get("fix") == "local-only" or item.get("kind") == "error":
            findings.append(_local_only_finding(item))
    try:
        report = host.run_check(env, provider.root, offline=provider.offline)
    except (host.CheckError, OSError, ValueError) as exc:
        findings.append({"id": "update-check", "level": "unavailable", "area": _("更新"), "title": _("无法检查更新"),
                         "reason": str(exc), "impact": _("不影响找资料、看进展等日常功能"),
                         "next": [{"label": _("联网后重新检查"), "action": ""}]})
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
        details = _("；").join(_("{0} {1} → {2}（{3}）").format(item['plugin'], item['version'], item['latest'], _(CLIENT_LABEL[item['client']]))
                           for item in behind)
        findings.append({"id": "update-plugins", "level": "update", "area": _("插件"),
                         "title": _("{0} 个插件有新版本").format(len(behind)) + (_("（含不兼容变更）") if breaking else ""),
                         "reason": details, "impact": _("只影响 AI 客户端里的插件；不更新也能继续使用本菜单"),
                         "next": [{"label": _("查看更新内容与迁移说明，再决定是否更新"), "action": "fix:update-plugins"}]})
    elif plugins:
        ok.append(_("客户端插件（最新）"))
    if not plugins:
        clients = host.detect_clients(env)
        if clients:
            findings.append({"id": "no-plugins", "level": "unavailable", "area": _("插件"),
                             "title": _("AI 客户端里还没有安装 agenthelm 插件"),
                             "reason": _("检测到 ") + _("、").join(_(CLIENT_LABEL.get(c, c)) for c in clients) + _("，但没有市场安装"),
                             "impact": _("只影响 AI Agent 使用；本菜单不受影响"),
                             "next": [{"label": _("需要时运行 agenthelm install 预览安装"), "action": ""}]})
        else:
            findings.append({"id": "no-ai-client", "level": "unavailable", "area": _("环境"), "title": _("本机没有 AI 客户端"),
                             "reason": _("没有找到 Claude Code、Codex 或 Cursor"),
                             "impact": _("本菜单的日常功能全部不受影响；只有“安装插件到客户端”不可用"),
                             "next": [{"label": _("需要时安装任一客户端，再运行 agenthelm install"), "action": ""}]})
    if report["summary"]["legacy_installs"]:
        findings.append({"id": "legacy", "level": "suggest", "area": _("插件"), "title": _("有旧方式安装待迁移"),
                         "reason": _("旧的复制 + 软链安装与市场安装并存会重复"), "impact": _("Agent 里可能看到重复的 Skill"),
                         "next": [{"label": _("运行 agenthelm migrate --install 预览迁移"), "action": ""}]})
    for issue in host.doctor(env, report)["issues"]:
        if issue["issue"] in ("version-skew", "duplicate", "incomplete"):
            findings.append({"id": f"doctor-{issue['issue']}", "level": "suggest", "area": _("插件"),
                             "title": {"version-skew": _("各客户端插件版本不一致"), "duplicate": _("插件重复安装"),
                                       "incomplete": _("插件没有装全")}[issue["issue"]],
                             "reason": issue["detail"], "impact": _("只影响 AI Agent 使用"),
                             "next": [{"label": _("运行 agenthelm doctor 查看"), "action": ""}]})


def _content_findings(report: dict, findings: list[dict], ok: list[str]) -> None:
    updates = [item for item in report["content"] if item.get("status") in ("update-available", "conflict")]
    for item in report["content"]:
        if item.get("pack") is None:
            findings.append({"id": "content-lock", "level": "suggest", "area": _("项目模板"), "title": _("内容锁无法读取"),
                             "reason": item.get("error", ""), "impact": _("模板更新无法判断哪些文件改过"),
                             "next": [{"label": _("检查 .agents/moe.sakanano.agent-pack/content.lock.json"), "action": ""}]})
    if updates:
        changed = sorted({path for item in updates for path in item["locally_changed"]})
        findings.append({"id": "update-content", "level": "update", "area": _("项目模板"),
                         "title": _("项目模板/样例有新版本"),
                         "reason": _("；").join(f"{item['pack']} {item['version']} → {item['latest']}" for item in updates)
                         + (_("；你改过的：{0}").format(_('、').join(changed)) if changed else ""),
                         "impact": _("不更新不影响使用；更新时你改过的文件逐个选择保留或采用"),
                         "next": [{"label": _("查看影响并选择"), "action": "fix:update-content"}]})
    elif report["content"]:
        ok.append(_("项目模板（最新）"))


def _opinion_findings(report: dict, findings: list[dict], ok: list[str]) -> None:
    for item in report["opinion"]:
        if item["status"] == "update-available":
            refs = _("；").join(f"{update['current']} → {update['available'][-1]}" for update in item["updates"])
            findings.append({"id": "opinion-updates", "level": "update", "area": _("个人规则"),
                             "title": _("行为规则模板有新版本"), "reason": refs,
                             "impact": _("工具升级不会自动采纳个人规则；当前规则继续有效"),
                             "next": [{"label": _("需要时在 Agent 会话中用 opinion-manager 预览后确认"), "action": ""}]})
        elif item["status"] in ("error", "unknown"):
            findings.append({"id": "opinion-check", "level": "unavailable", "area": _("个人规则"),
                             "title": _("行为规则版本无法检查"), "reason": item.get("error", ""),
                             "impact": _("只影响规则更新提示"), "next": []})
        else:
            ok.append(_("行为规则（已是锁定的最新版本）"))


def _check_runtime(provider: Any, env: Any, latest: dict | None, findings: list[dict], ok: list[str],
                   machine: dict | None = None) -> None:
    runtime = runtime_module(provider.host)
    data = runtime.report(env, latest)
    active = data["current"]
    if active is None:
        if machine is not None and not machine["findings"]:
            return  # 用户已决定不提示这台机器的运行时（决定存在用户状态目录，不进项目）
        text = runtime.missing_text(data, runtime.command_status()) if hasattr(runtime, "missing_text") else {
            "reason": _("本次由 {0} 提供；装好运行时后，不依赖 AI 客户端与 npx 也能打开菜单").format(data['running_from']),
            "impact": _("现在能用；换一台没有 AI 客户端的机器或客户端插件被移除时会打不开"),
            "label": _("安装运行时（只写用户数据目录）")}
        decision = ({"id": "local:runtime-missing", "fingerprint": "runtime-missing", "scope": "machine"}
                    if machine is not None else None)
        findings.append({"id": "install-runtime", "level": "suggest", "area": _("运行时"), "title": _("这台机器还没有安装用户级运行时"),
                         "reason": text["reason"], "impact": text["impact"],
                         "next": [{"label": text["label"], "action": "fix:install-runtime"}],
                         **({"decision": decision} if decision else {})})
    elif data["update_available"]:
        findings.append({"id": "update-runtime", "level": "update", "area": _("运行时"), "title": _("运行时有新版本"),
                         "reason": _("当前 {0}，最新 {1}").format(active['version'], data['latest_release']),
                         "impact": _("不更新也能继续使用"), "next": [{"label": _("下载并切换（旧版本保留）"), "action": "fix:update-runtime"}]})
    else:
        ok.append(_("运行时 {0}").format(active['version']))


# ---------------------------------------------------------------- plan

FIXES = ("rebuild-index", "update-plugins", "update-content", "install-runtime", "update-runtime", "add-entry",
         "add-skeleton", "profile", "repair-lock", "case-naming", "sync-folder", "local-only")
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
            verb, _sep, target = step["action"].partition(":")
            if verb == "fix" and target in FIXES and target not in found:
                found.append(target)
    return found


def plan(provider: Any, ids: list[str], choices: dict[str, str]) -> dict:
    offered = available_ids(provider)
    chosen = ids or offered
    unknown = [fix_id for fix_id in chosen if fix_id not in FIXES]
    if unknown:
        raise EntryError(_("没有这些处理项：") + _("、").join(unknown), "not-found", [_("可处理：") + (_("、").join(offered) or _("（无）"))])
    steps = [_plan_step(provider, fix_id, choices) for fix_id in chosen]
    return {"steps": steps, "simulated": False, "update_source": (provider._health or {}).get("update_source")}


def _step(fix_id: str, title: str, **values: Any) -> dict:
    step = {"id": fix_id, "title": title, "kind": fix_id, "network": False, "changes": [], "writes": [],
            "custom": [], "breaking": False, "recovery": "", "verify": "", "blocked": "", "payload": {}}
    step.update(values)
    return step


def _need_host(provider: Any, fix_id: str, title: str) -> dict | None:
    if provider.host is None:
        return _step(fix_id, title, blocked=_("当前运行方式没有携带 agent-pack：用 agenthelm 运行时打开后再处理"))
    return None


def _plan_step(provider: Any, fix_id: str, choices: dict[str, str]) -> dict:
    if fix_id == "rebuild-index":
        state_dir = provider.assets_project().state_dir()
        return _step(fix_id, _("重建资料索引"), changes=[_("按当前文件重新登记资料、版本与引用")],
                     writes=[{"path": str(state_dir), "scope": _("用户缓存（不改项目文件）")}],
                     recovery=_("不涉及项目文件；索引可随时再次重建"), verify=_("重新读取索引状态"))
    if fix_id == "add-entry":
        return _plan_entry(provider, choices)
    if fix_id in ("add-skeleton", "profile", "repair-lock"):
        return _plan_scaffold(provider, fix_id, choices)
    if fix_id in ("case-naming", "sync-folder", "local-only"):
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
        return _step(fix_id, fix_id, blocked=_("版本信息不可用（离线且没有缓存）：联网后重新检查"))
    if fix_id == "update-plugins":
        steps = host.update_steps(env, report, None, None, env.home, [], [], None, "plugins")
        behind = [item for item in report["plugins"] if item["status"] == "behind"]
        changes = [_("{0} → {1}：{2}").format(item['plugin'], step['to'], step['summary']) for item in behind for step in item["steps"]]
        actions = [action for item in behind for step in item["steps"] for action in step["actions"]]
        blocked = next((step.blocked for step in steps if step.blocked), "") or \
            (_("当前离线：需要联网，只能预览") if provider.offline else "")
        return _step(fix_id, _("更新客户端里的插件"), network=True, changes=changes + [_("需要的动作：{0}").format(a) for a in actions],
                     writes=[{"path": step.description + (_("（{0}）").format(' '.join(step.command)) if step.command else ""),
                              "scope": _("客户端插件目录")} for step in steps],
                     breaking=any(item["breaking"] for item in behind),
                     recovery=_("客户端保留旧版本缓存；需要时用客户端命令回到旧版本"),
                     verify=_("重新检查各客户端已安装的版本"), blocked=blocked)
    return _plan_content(provider, env, report, choices)


def _run_refresh(provider: Any, accept: list[str], keep: list[str], recovery: Path, *, apply: bool) -> dict:
    """agents-init 的 refresh：模板包版本没有更新、但有文件与当前模板不同（例如没有基线）时直接用它。"""
    script = _init_script(provider)
    if script is None:
        raise EntryError(_("找不到支持刷新的 agents-init"), "unavailable", [_("更新插件或运行时后再试")])
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
        raise EntryError(_("agents-init 没有返回结果：") + (completed.stderr or "").strip()[:200], "failed") from exc
    if "files" not in result:
        raise EntryError(str(result.get("error") or _("agents-init 拒绝")), "failed")
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
            return step.description, [], preview.get("error", _("预览失败")), False
        return step.description, (preview.get("result") or {}).get("files") or [], "", False
    try:
        result = _run_refresh(provider, accept, keep, recovery, apply=False)
    except EntryError as exc:
        return _("项目模板/样例"), [], str(exc), True
    files = result.get("files") or []
    if all(item["state"] in ("unchanged", "kept-by-decision") for item in files):
        return _("项目模板/样例"), [], _("没有需要更新的模板"), True
    return _("按当前模板对比项目契约文件"), files, "", True


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
    custom = [{"path": item["path"], "state": _("你改过"), "default": "keep"} for item in files
              if item["state"] == "modified" or (item["path"] in choices and item["state"] in ("modified", "missing"))]
    for item in files:
        if item["state"] == "missing" and not any(entry["path"] == item["path"] for entry in custom):
            custom.append({"path": item["path"], "state": _("新文件（目前不存在）"), "default": "keep"})
    effective = {item["path"]: choices.get(item["path"], item["default"]) for item in custom}
    writes = []
    for item in files:
        action = item["action"]
        if action == "blocked":
            action = "replace" if effective.get(item["path"]) == "accept" else "keep"
        if action == "skip":
            action = "create" if effective.get(item["path"]) == "accept" else "keep"
        if action in ("replace", "create"):
            writes.append({"path": item["path"], "scope": _("项目（先备份）") if action == "replace" else _("项目（新建）")})
    # 刷新总会把内容锁更新到新版本（保留的文件沿用旧基线），所以它总在写入范围里。
    writes.append({"path": ".agents/moe.sakanano.agent-pack/content.lock.json", "scope": _("项目（内容锁）")})
    return _step("update-content", step.description, changes=[_content_change(item, effective) for item in files],
                 writes=writes, custom=custom, recovery=str(recovery),
                 verify=_("重新检查模板版本；保留的文件内容不变"),
                 payload={"recovery": str(recovery), "files": files, "direct": direct})


CONTENT_STATE = {"unchanged": N_("与新模板相同"), "clean": N_("没改过"), "modified": N_("你改过"), "missing": N_("目前不存在"),
                 "local-only": N_("只有你的改动，新模板没变")}
CONTENT_ACTION = {"keep": N_("保留"), "replace": N_("换成新模板（先备份）"), "create": N_("新建"), "skip": N_("不新建"),
                  "blocked": N_("需要你选择")}


def _content_change(item: dict, effective: dict[str, str]) -> str:
    action = item["action"]
    if action in ("blocked", "skip"):
        chosen = effective.get(item["path"], "keep")
        action = ("replace" if action == "blocked" else "create") if chosen == "accept" else \
            ("keep" if action == "blocked" else "skip")
    note = _("（保留则不含新模板的改动）") if item["state"] == "modified" and action == "keep" else ""
    return _("{0}：{1} → {2}{3}").format(item['path'], _(CONTENT_STATE.get(item['state'], item['state'])), _(CONTENT_ACTION.get(action, action)), note)


def _plan_runtime(provider: Any, env: Any, fix_id: str) -> dict:
    runtime = runtime_module(provider.host)
    host = provider.host
    title = _("安装用户级运行时") if fix_id == "install-runtime" else _("更新用户级运行时")
    if fix_id == "install-runtime" and runtime.bundled_root() is not None:
        root, origin = runtime.bundled_root(), _("随附内容")
        network = False
    else:
        if provider.offline:
            return _step(fix_id, title, network=True, blocked=_("当前离线：需要下载发布包，只能预览；也可用 agenthelm runtime install --source <发布包>"))
        try:
            latest, _origin = host.load_latest(env, None)
        except (host.CheckError, OSError, ValueError) as exc:
            return _step(fix_id, title, network=True, blocked=_("拿不到版本信息：{0}").format(exc))
        version = runtime.release_version(latest)
        return _step(fix_id, title, network=True, changes=[_("下载发布包 {0} 并核对摘要").format(version or _('（最新）'))],
                     writes=[{"path": str(runtime.runtimes_dir(env) / (version or _("<最新版本>"))), "scope": _("用户数据目录（新版本）")},
                             {"path": str(runtime.current_link(env)), "scope": _("切换当前版本")}],
                     recovery=_("旧版本保留；agenthelm runtime use <旧版本> --apply 切回"), verify=_("重新读取当前运行时版本"),
                     payload={"from_release": True})
    planned = runtime.plan(env, root, origin)
    return _step(fix_id, title, network=network, changes=[_("运行时 {0}（{1}）").format(planned['version'], origin)],
                 writes=planned["writes"], recovery=_("旧版本保留；agenthelm runtime use <旧版本> --apply 切回"),
                 verify=_("重新读取当前运行时版本"), payload={"from_release": False, "plan": planned})


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
        raise EntryError(_("找不到支持生成入口的 agents-init"), "unavailable", [_("更新插件或运行时后再试")])
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
        raise EntryError(_("agents-init 没有返回结果：") + (completed.stderr or "").strip()[:200], "failed") from exc


def _recovery_dir(provider: Any, label: str) -> str:
    if provider.host is not None:
        return str(provider.host.recovery_dir_for(provider.host.current_env(), label))
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return str(Path.home() / ".agent-pack" / "recovery" / f"{stamp}-agenthelm-{label}")


def _plan_entry(provider: Any, choices: dict[str, str]) -> dict:
    name = choices.get("entry-name") or provider.launcher() or DEFAULT_LAUNCHER
    if not LAUNCHER_RE.fullmatch(name):
        raise EntryError(_("启动器名称不合法：{0}").format(name), "usage", [_("只能用字母、数字、. _ -，例如 menu、go")])
    title = _("生成根目录启动器 ./{0}").format(name) if provider.launcher() != name else _("完善入口 ./{0}").format(name)
    recovery = choices.get("_recovery") or _recovery_dir(provider, "entry")
    try:
        result = _run_init(provider, name, choices, recovery, apply=False)
        if (result.get("entry") or {}).get("collisions") and name not in choices:
            choices = {**choices, name: "keep"}  # 同名文件默认保留：预览就按保留来算写入范围
            result = _run_init(provider, name, choices, recovery, apply=False)
    except EntryError as exc:
        return _step("add-entry", title, blocked=str(exc))
    if result.get("code"):
        return _step("add-entry", title, blocked=result.get("error", _("agents-init 拒绝")))
    entry = result.get("entry") or {}
    custom = [{"path": name, "state": _("已有同名文件（你的）"), "default": "keep"}] \
        if entry.get("action") in ("keep", "replace", "blocked") else []
    readme = provider.root / "README.md"
    has_section = readme.is_file() and "<!-- agenthelm:entry -->" in readme.read_text(encoding="utf-8", errors="replace")
    if not has_section:
        custom.append({"path": "README.md", "state": _("可追加「怎样使用」一节（带标记）"), "default": "keep"})
    writes = [{"path": path, "scope": _("项目")} for path in entry.get("writes", [])]
    return _step("add-entry", title, changes=entry.get("changes", []), writes=writes, custom=custom,
                 recovery=recovery if choices.get(name) == "accept" else _("新增文件可直接删除；Profile 改动前后见预览"),
                 verify=_("重新读取 Profile 的 entry 段与 ./{0}").format(name), payload={"name": name, "recovery": recovery})


# ---------------------------------------------------------------- scaffold fixes

SCAFFOLD_TITLE = {"add-skeleton": N_("补上缺少的骨架文件"), "profile": N_("修正 Profile 字段"), "repair-lock": N_("修复内容锁版本")}
DECISIONS_FILE = ".agents/moe.sakanano.agent-pack/check-decisions.json"


def scaffold_module(provider: Any) -> Any | None:
    host = provider.host
    return host.scaffold_module(host.current_env()) if host is not None and hasattr(host, "scaffold_module") else None


def _preview_lines(text: str, limit: int = 30) -> str:
    lines = text.splitlines()
    shown = [f"+{line}" for line in lines[:limit]]
    return "\n".join(shown + ([_("... 还有 {0} 行").format(len(lines) - limit)] if len(lines) > limit else [])) or _("（空文件）")


def _plan_scaffold(provider: Any, fix_id: str, choices: dict[str, str]) -> dict:
    title = _(SCAFFOLD_TITLE[fix_id])
    module = scaffold_module(provider)
    if module is None:
        return _step(fix_id, title, blocked=_("需要带脚手架检查的 agents-init（更新插件或运行时后再试）"))
    root = provider.root
    state_dir = provider.host.current_env().state_dir
    wanted = [item for item in (choices.get("_targets") or "").split("\n") if item]
    try:
        if fix_id == "repair-lock":
            result = module.repair_lock(root)
            if result["blocked"]:
                return _step(fix_id, title, blocked=result["blocked"])
            return _step(fix_id, title, changes=result["changes"] + [result["diff"]] if result["diff"] else result["changes"],
                         writes=[{"path": ".agents/moe.sakanano.agent-pack/content.lock.json", "scope": _("项目（内容锁）")}],
                         recovery=_("内容锁由工具生成；写入前再次核对内容未变，可用 git 或同步盘历史还原"),
                         verify=_("重新检查：内容锁不再显示 0.0.0"))
        report = module.check(root, state_dir=state_dir, reprompt=True)
        recovery = Path(choices.get("_recovery") or _recovery_dir(provider, fix_id))
        if fix_id == "add-skeleton":
            rows = [row["path"] for row in report["skeleton"] if row["state"] == "missing" and row["required"]]
            rows = [p for p in rows if not wanted or p in wanted]
            unknown = [p for p in wanted if p not in {r["path"] for r in report["skeleton"]}]
            if unknown:
                raise EntryError(_("不在期望骨架清单里：") + _("、").join(unknown), "usage")
            if not rows:
                return _step(fix_id, title, blocked=_("没有缺少的骨架文件"))
            ctx = module.Context(root)
            custom = [{"path": p, "state": _("缺少"), "default": "accept", "options": True} for p in rows]
            decided = {p: choices.get(p, "accept") for p in rows}
            accepted = [p for p, choice in decided.items() if choice == "accept"]
            preview = module.add_skeleton(root, accepted, apply=False, state_dir=state_dir) if accepted else \
                {"create": [], "unchanged": []}
            writes = [{"path": item["path"], "scope": _("项目（新建）")} for item in preview["create"]]
            other = {p: c for p, c in decided.items() if c != "accept"}
            if other:
                writes.append({"path": DECISIONS_FILE, "scope": _("项目（决定文件，随项目同步）")})
            return _step(fix_id, title, changes=[_("{0}：{1}").format(p, {'accept': _('新建'), 'skip': _('永久跳过'), 'later': _('稍后')}[c])
                                                 for p, c in decided.items()],
                         writes=writes, custom=custom,
                         recovery=_("只新建文件，已有文件不会被改动；新建的可直接删除"),
                         verify=_("重新检查：这些文件不再显示为缺少"),
                         payload={"accepted": accepted, "decisions": other, "recovery": str(recovery),
                                  "diffs": {p: _preview_lines(ctx.expected_text(p) or "") for p in rows}})
        # profile
        rows = {row["field"]: row for row in report["profile"] if row["state"] != "ok" and row["mode"] in ("default", "equal")}
        unknown = [f for f in wanted if f not in rows]
        if unknown:
            raise EntryError(_("这些字段不需要或不能自动修复：") + _("、").join(unknown), "usage")
        fields = [f for f in rows if not wanted or f in wanted]
        if not fields:
            return _step(fix_id, title, blocked=_("没有需要修正的 Profile 字段"))
        decided = {f: choices.get(f, "accept") for f in fields}
        accepted = [f for f, choice in decided.items() if choice == "accept"]
        diffs = {f: module.fix_profile(root, [f], apply=False, state_dir=state_dir)["diff"] for f in fields}
        writes = []
        if accepted:
            writes.append({"path": ".agents/moe.sakanano.agent-pack/project.json", "scope": _("项目（先备份）")})
        other = {f: c for f, c in decided.items() if c != "accept"}
        if other:
            writes.append({"path": DECISIONS_FILE, "scope": _("项目（决定文件，随项目同步）")})
        return _step(fix_id, title,
                     changes=[f"{f} → {json.dumps(rows[f]['expected'], ensure_ascii=False)}"
                              if decided[f] == "accept" else _("{0}：{1}").format(f, {'skip': _('永久跳过'), 'later': _('稍后')}[decided[f]])
                              for f in fields],
                     writes=writes, custom=[{"path": f, "state": _("缺少") if rows[f]["state"] == "missing" else _("不符合"),
                                             "default": "accept", "options": True} for f in fields],
                     recovery=str(recovery), verify=_("重新检查：这些字段不再显示为缺少或不符合"),
                     payload={"accepted": accepted, "decisions": other, "recovery": str(recovery), "diffs": diffs})
    except Exception as exc:  # noqa: BLE001 - init errors come from a separately loaded module copy
        if isinstance(exc, EntryError):
            raise
        return _step(fix_id, title, blocked=str(exc))


PLAN_ONLY = N_("只给方案，不会自动执行；按方案自己处理后重新检查")


def _plan_guidance(provider: Any, fix_id: str) -> dict:
    """Plan-only items: they explain what to do and change nothing (no directory moves, no deletions)."""
    if fix_id == "case-naming":
        title = _("Case 目录命名：改名计划")
        module = scaffold_module(provider)
        if module is None:
            return _step(fix_id, title, blocked=_("需要带脚手架检查的 agents-init（更新插件或运行时后再试）"))
        try:
            plan = module.case_naming_plan(provider.root)
        except Exception as exc:  # noqa: BLE001
            return _step(fix_id, title, blocked=str(_entry_error(exc)))
        if plan["error"]:
            return _step(fix_id, title, blocked=_("Profile 的 work.case.directory_naming 无法使用：") + plan["error"])
        if not plan["renames"]:
            return _step(fix_id, title, blocked=_("Case 目录命名与规则一致，没有需要改的"))
        changes = []
        for item in plan["renames"]:
            line = f"{item['from']} → {item['to'] or _('（需要你决定新名字）')}"
            if item["references"]:
                line += _("；被这些文件提到，改名后要同步：{0}").format(_('、').join(item['references'][:5])) \
                        + (_(" 等 {0} 个").format(len(item['references'])) if len(item["references"]) > 5 else "")
            if item["note"]:
                line += _("（{0}）").format(item['note'])
            changes.append(line)
        if plan["declaration_hint"]:
            hint = json.dumps(plan["declaration_hint"]["if_this_is_your_convention"], ensure_ascii=False)
            changes.append(_("如果这些名字正是你的约定，不用改名：在 Profile 里声明它，之后按声明检查 → ") + hint)
        changes.append(_("每个 Case 的 CASE.md 里的 id 字段也要和新目录名保持一致"))
        return _step(fix_id, title, changes=changes, recovery=_("本处理项不移动、不改写任何文件"),
                     verify=_("按计划改名并更新引用后，重新运行检查"), blocked=_(PLAN_ONLY) + _("（不会移动目录）"))
    if fix_id == "local-only":
        return _plan_local_only(provider)
    title = _("同步盘使用指引")
    host = provider.host
    where = host.sync_folder(provider.root) if host is not None and hasattr(host, "sync_folder") else None
    if where is None:
        return _step(fix_id, title, blocked=_("项目不在同步盘里"))
    changes = [
        _("随项目同步的：Profile、内容锁、决定文件（check-decisions.json）、所有项目文件"),
        _("每台机器各一份的：用户级运行时（~/.local/share/agenthelm）、版本缓存、wf 的资料台账缓存（按项目绝对路径命名）、本机决定"),
        _("所以：两台机器的 wf 结果可能不同——台账各自重建，且取决于各自装的 project-orchestrator 版本；"
        "用 agenthelm check --fingerprint 对比两台机器的脚手架是否一致"),
    ]
    if where["rebuildable"]:
        listed = _("、").join(where["rebuildable"][:8])
        changes.append(_("项目里有可重建目录：{0}。它们不需要同步，可以：").format(listed))
        changes.append(_("  · 推荐：agenthelm local-only check 逐项查看，agenthelm local-only fix 把 .venv、node_modules 等"
                       "外置到本机（~/.local/share/agenthelm/local-only/…）并在原位置建软链接；yarn 经典版、bun、uv 实测可用，"
                       "npm 会把链接换回真实目录、pnpm 会安装失败，同步目录里请尽量避免它们"))
        changes.append(_("  · 写进 .gitignore（版本管理忽略它们，不影响同步盘）"))
        changes.append(_("  · 或在 {0} 的设置里把这些目录设为不同步（选择性同步）").format(where['service']))
    changes.append(_("本工具不会替你移动、改名或删除任何内容"))
    return _step(fix_id, title, changes=changes, recovery=_("本处理项不改任何文件"), verify=_("不涉及"),
                 blocked=_(PLAN_ONLY))


def _plan_local_only(provider: Any) -> dict:
    """只展示 agenthelm local-only fix 的预览：移动目录属于用户的同步盘内容，由它自带预览与回滚的命令执行。"""
    title = _("本机专属目录：外置到本机")
    host = provider.host
    if host is None or not hasattr(host, "local_only_plan"):
        return _step("local-only", title, blocked=_("需要带 local-only 的 agent-pack（更新插件或运行时后再试）"))
    try:
        plan = host.local_only_plan(host.current_env(), provider.root)
    except Exception as exc:  # noqa: BLE001 - LocalOnlyError 等：把原因原样给用户
        return _step("local-only", title, blocked=str(exc))
    changes = [step["detail"] for step in plan["steps"]] + [_("不处理：{0}").format(message) for message in plan["blocked"]]
    if not plan["steps"]:
        return _step("local-only", title, changes=changes,
                     blocked=_("没有可以自动处理的项；体积阈值等需要你选择，见 health 里各项给出的选项"))
    changes.append(_("执行：agenthelm local-only fix --apply（先预览；移动失败整体撤销）"))
    return _step("local-only", title, changes=changes, recovery=_("移动失败整体撤销；旧配置备份在 ~/.agent-pack/recovery/"),
                 verify=_("重新运行 health，或 agenthelm local-only check"), blocked=PLAN_ONLY + _("（这里只展示预览）"))


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
        raise EntryError(_("需要带脚手架检查的 agents-init（更新插件或运行时后再试）"), "unavailable")
    health = provider._health or provider.health()
    known = {f["decision"]["id"]: f["decision"] for f in health["findings"] if f.get("decision")}
    env = provider.host.current_env()
    results = []
    for identifier in skip:
        if identifier not in known:
            raise EntryError(_("现在没有这个发现：{0}").format(identifier), "usage", [_("可跳过：") + (_("、").join(sorted(known)) or _("（无）"))])
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
                            "detail": _("预览之后情况有变化（写入范围不同），没有执行：请重新预览")})
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
        return {**base, "verify": _("内容锁版本已修复") if result["applied"] else _("没有需要修复的内容")}
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
            "verify": _("重新检查：") + (_("、").join(accepted) or _("没有写入文件"))
                      + (_("；已记录决定：{0}").format(_('、').join(recorded)) if recorded else ""),
            "recovery": payload["recovery"] if fix_id == "profile" and accepted else _("只新建文件，无需备份")}


def _apply_step(provider: Any, step: dict, choices: dict[str, str]) -> dict:
    fix_id = step["id"]
    base = {"id": fix_id, "status": "applied", "written": [w["path"] for w in step["writes"]], "kept": [],
            "accepted": [], "recovery": step["recovery"]}
    if fix_id == "rebuild-index":
        result = assets.rebuild(provider.assets_project(), force=True)
        state = assets.status(provider.assets_project())
        return {**base, "verify": _("索引{0}，{1} 个文件").format(_('最新') if state['status'] == 'fresh' else _('状态：') + state['status'], result['file_count'])}
    if fix_id in ("add-skeleton", "profile", "repair-lock"):
        try:
            return _apply_scaffold(provider, step, choices, base)
        except Exception as exc:  # noqa: BLE001
            raise _entry_error(exc) from exc
    if fix_id == "add-entry":
        name = step["payload"]["name"]
        result = _run_init(provider, name, choices, step["payload"]["recovery"], apply=True)
        if not result.get("applied"):
            raise EntryError(_("没有写入：") + str(result.get("hint") or result.get("error") or result.get("blocked")), "failed")
        launcher = provider.root / name
        ready = launcher.is_file() and launcher.stat().st_mode & 0o111
        return {**base, "verify": _("./{0} {1}；Profile 已声明 entry").format(name, _('已就绪') if ready else _('未就绪'))}
    host = provider.host
    env = host.current_env()
    if fix_id in ("install-runtime", "update-runtime"):
        runtime = runtime_module(host)
        if step["payload"].get("from_release"):
            import tempfile
            with tempfile.TemporaryDirectory(prefix="agenthelm-runtime-") as temporary:
                latest, _origin = host.load_latest(env, None, max_age=0)
                root = host.release_source(latest, Path(temporary))
                planned = runtime.plan(env, root, _("GitHub Release（已核对摘要）"))
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
                "verify": _("重新检查：") + (_("全部为最新") if not behind else _("仍有 {0} 个落后").format(len(behind)))}
    # update-content
    custom = {item["path"] for item in step["custom"]}
    keep = [path for path in custom if choices.get(path, "keep") == "keep"]
    accept = [path for path in custom if choices.get(path) == "accept"]
    before = {path: _digest(provider.root / path) for path in keep}
    recovery = Path(step["payload"]["recovery"])
    if step["payload"].get("direct"):
        result = _run_refresh(provider, accept, keep, recovery, apply=True)
        if not result.get("applied"):
            raise EntryError(_("模板更新没有执行：") + str(result.get("hint") or result), "failed")
    else:
        steps = host.update_steps(env, provider._report, provider.root, None, env.home, accept, keep, recovery, "content")
        outcome = host.execute_steps(steps, apply=True, recovery_root=None, child_env=env.child_environment())[0]
        result = outcome.get("result") or {}
        if outcome["status"] == "failed" or not result.get("applied"):
            raise EntryError(_("模板更新没有执行：") + str(outcome.get("error") or result.get("hint") or result), "failed")
    untouched = all(_digest(provider.root / path) == digest for path, digest in before.items())
    return {**base, "kept": keep, "accepted": accept, "recovery": result.get("recovery") or _("没有替换文件，无需备份"),
            "verify": _("重新读取：保留的文件") + (_("内容未变") if untouched else _("被改动了，请检查"))
                      + _("；内容锁已更新到新版本")}


def _digest(path: Path) -> str | None:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
