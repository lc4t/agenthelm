"""项目入口的真实数据来源：读当前项目，不调用模型，不需要 AI 客户端。

- 找资料复用交付物台账（assets.py），台账在用户缓存里，项目文件是权威；
- 看进展读 Case（case.py 的格式），或项目在 ``entry.items`` 声明的事项工具的只读接口；
- 检查汇总运行环境、入口、资料索引、事项记录、记忆与（经 agent-pack 的）插件和模板状态；
- 维护与写入分别在 project_maintain.py、project_write.py。

返回结构与 project_demo 一致，界面层不区分来源。
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import assets
import case_structure as structure
import case as case_tool
import project_maintain
import project_write
from project_base import (GROUP_KEYS, LAUNCHER_API, EntryError, entry_section, find_profile_root, inside,
                          read_profile)
from i18n_orchestrator import _  # noqa: E402

RECORD_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T[0-9:]{5,8}(?:Z|[+-]\d{2}:\d{2})?)\s+(.*)$")
TOOL_TIMEOUT_SECONDS = 30


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def last_record(body: str) -> dict | None:
    """The newest bullet under the work-log section, in either language (records are appended, so the last one)."""
    name = structure.existing_heading(body, "work_log")
    if name is None:
        return None
    marker = f"## {name}\n"
    start = body.find(marker)
    end = body.find("\n## ", start + len(marker))
    lines = [line[2:].strip() for line in body[start + len(marker):None if end < 0 else end].splitlines()
             if line.startswith("- ")]
    if not lines:
        return None
    match = RECORD_STAMP.match(lines[-1])
    return {"at": match.group(1), "text": match.group(2)} if match else {"at": "", "text": lines[-1]}


class RealProvider:
    simulated = False

    def __init__(self, start: Path | None, explicit: bool, *, offline: bool = False, host: Any = None,
                 via_launcher: int | None = None) -> None:
        self.root = find_profile_root(start, explicit)
        self.profile = read_profile(self.root)
        self.entry = entry_section(self.profile)
        self.entry_api = self.entry["api"]
        self.name = str(self.profile.get("name") or self.root.name)
        work = self.profile.get("work") if isinstance(self.profile.get("work"), dict) else {}
        self.work_mode = work.get("mode") or "legacy"
        case = work.get("case") if isinstance(work.get("case"), dict) else {}
        self.case_command = case.get("command")
        self.offline = offline
        self.host = host
        self.via_launcher = via_launcher
        self._health: dict | None = None
        self._items: dict | None = None
        self.reprompt = False  # health/check：忽略已记录的决定，重新评估
        self._skipped: list[dict] = []

    # -------------------------------------------------------------- shared pieces

    def assets_project(self) -> assets.Project:
        try:
            return assets.Project(self.root)
        except assets.AssetError as exc:
            raise EntryError(_("资料扫描范围配置有误：{0}").format(exc), "profile-invalid",
                             [_("检查 Profile 的 work.case.root 与 work.assets.roots")]) from exc

    def index_state(self) -> dict:
        try:
            state = assets.status(self.assets_project())
        except EntryError:
            return {"status": "broken", "built_at": "", "file_count": 0}
        status = {"fresh": "fresh", "stale": "stale", "missing": "missing"}.get(state["status"], "broken")
        return {"status": status, "built_at": state.get("built_at", ""), "file_count": state.get("file_count", 0)}

    def launcher(self) -> str | None:
        return self.entry["launcher"]

    def command_prefix(self) -> str:
        return f"./{self.launcher()}" if self.launcher() else "agenthelm project"

    # -------------------------------------------------------------- overview

    def overview(self) -> dict:
        types = (self.profile.get("profile") or {}).get("project_type") or []
        counts: dict[str, int] = {}
        try:
            for item in self.items()["items"]:
                counts[item["group"]] = counts.get(item["group"], 0) + 1
        except EntryError:
            pass
        levels: dict[str, int] = {}
        for finding in (self._health or {}).get("findings", []):
            levels[finding["level"]] = levels.get(finding["level"], 0) + 1
        tools = project_write.load_tools(self)
        return {"name": self.name, "kind": _("、").join(map(str, types)) or _("通用"),
                "kind_label": (_("、").join(map(str, types)) + _(" 项目")) if types else _("项目"),
                "launcher": self.launcher(), "index": self.index_state(), "items": counts,
                "health": levels, "health_checked": self._health is not None, "read_at": now_iso(),
                "tools": {"declared": len(tools["tools"]),
                          "available": sum(tool["state"] == "available" for tool in tools["tools"])}}

    # -------------------------------------------------------------- find

    def find(self, terms: list[str], *, ext: str | None = None, container: str | None = None,
             limit: int = 10) -> dict:
        project = self.assets_project()
        try:
            result = assets.find(project, terms, ext=ext, container=container, limit=limit)
        except assets.AssetError as exc:
            raise EntryError(_("查找失败：{0}").format(exc), "index-broken",
                             [_("重建资料索引（只改用户缓存）：fix rebuild-index --apply")]) from exc
        return {**result, "index": self.index_state()}

    def refs(self, path: str) -> dict:
        try:
            result = assets.refs(self.assets_project(), path)
        except assets.AssetError as exc:
            raise EntryError(_("查不到引用关系：{0}").format(exc), "index-broken") from exc
        return {"path": result["path"], "inbound": [item["src"] for item in result["inbound"]],
                "outbound": [item["dst"] for item in result["outbound"]],
                "affected": result.get("affected_containers", [])}

    def act(self, action: str, path: str) -> dict:
        target = inside(self.root, path, _("路径"))
        if not target.exists():
            raise EntryError(_("文件不存在：{0}").format(path), "not-found", [_("索引可能过期：重新查找一次")])
        absolute = str(target)
        verbs = {"open": _("打开"), "reveal": _("显示所在目录"), "copy": _("复制路径")}
        command = None if os.environ.get("AGENTHELM_OPEN") == "print" else _open_command(action, target)
        if command and shutil.which(command[0]):
            completed = subprocess.run(command, input=absolute.encode("utf-8") if action == "copy" else None,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                                       timeout=15)
            if completed.returncode == 0:
                return {"action": action, "path": path, "executed": True, "absolute": absolute,
                        "message": _("已{0}：{1}").format(verbs[action], path)}
        return {"action": action, "path": path, "executed": False, "absolute": absolute,
                "message": _("这台机器不能直接{0}，绝对路径：{1}").format(verbs[action], absolute)}

    # -------------------------------------------------------------- progress

    def items_source(self) -> str:
        items = self.entry["items"]
        if items:
            return _("项目工具 {0}（只读接口 list）").format(' '.join(items['command']))
        return _("{0}/*/CASE.md（默认 Case 工具）").format(self._case_root_text())

    def _case_root_text(self) -> str:
        work = self.profile.get("work") or {}
        return str((work.get("case") or {}).get("root") or "cases")

    def items(self, *, status: str | None = None, term: str | None = None) -> dict:
        if self._items is None:
            self._items = self._load_items()
        rows = [item for item in self._items["items"]
                if (not status or item["group"] == status) and (not term or _item_hits(term, item))]
        return {**self._items, "items": rows}

    def _load_items(self) -> dict:
        adapter = self.entry["items"]
        if adapter:
            raw = self._run_items_tool(adapter)
            status_map = adapter.get("status_map") or {}
            rows = [self._normalize(item, status_map) for item in raw]
            return {"items": rows, "invalid": [], "source": self.items_source(), "read_at": now_iso()}
        if self.work_mode != "case-workspace":
            raise EntryError(_("这个项目没有使用 Case 工作目录，也没有声明事项工具"), "unavailable",
                             [_("work.mode 为 legacy：事项由项目自己的方式管理"),
                              _("如需在本入口查看，可在 Profile 的 entry.items 声明事项工具（见 case-workspaces.md「项目入口适配」）")])
        try:
            project = case_tool.Project(self.root)
        except case_tool.CaseError as exc:
            raise EntryError(_("读不了事项：{0}").format(exc), "profile-invalid") from exc
        rows, invalid = [], []
        for directory in project.case_dirs():
            path = directory / "CASE.md"
            relative = path.relative_to(self.root).as_posix()
            try:
                data, body = case_tool.read_case(path)
            except (case_tool.CaseError, OSError, UnicodeDecodeError) as exc:
                invalid.append({"path": relative, "error": str(exc)})
                continue
            item = self._normalize({**data, "path": relative, "last_record": last_record(body)}, {})
            item["deliverables"] = [self._resolve_deliverable(directory, value) for value in item["deliverables"]]
            rows.append(item)
        rows.sort(key=lambda item: item["updated_at"], reverse=True)
        return {"items": rows, "invalid": invalid, "source": self.items_source(), "read_at": now_iso()}

    def _resolve_deliverable(self, case_dir: Path, value: str) -> str:
        """Deliverables may be written relative to the Case or to the project; show what exists."""
        for base in (case_dir, self.root):
            candidate = (base / value).resolve()
            if candidate.exists() and (candidate == self.root or self.root in candidate.parents):
                return candidate.relative_to(self.root).as_posix()
        return value

    def _run_items_tool(self, adapter: dict) -> list[dict]:
        command = [*adapter["command"], "list", "--all", "--output", "json"]
        try:
            completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True, check=False,
                                       timeout=TOOL_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EntryError(_("项目事项工具无法运行：{0}").format(exc), "unavailable",
                             [_("命令：{0}").format(' '.join(command)), _("检查 Profile 的 entry.items.command")]) from exc
        try:
            data = json.loads(completed.stdout)
        except ValueError as exc:
            detail = (completed.stderr or completed.stdout).strip()[:200]
            raise EntryError(_("项目事项工具的 list 没有返回 JSON"), "unavailable",
                             [_("命令：{0}").format(' '.join(command)), detail or _("（无输出）")]) from exc
        rows = data.get("items", data.get("cases")) if isinstance(data, dict) else data
        if completed.returncode != 0 or not isinstance(rows, list):
            raise EntryError(_("项目事项工具的 list 返回格式不符合约定"), "unavailable",
                             [_("约定：list --all --output json 返回 {\"items\": [...]}，见 case-workspaces.md")])
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _normalize(data: dict, status_map: dict) -> dict:
        def text(key: str) -> str:
            value = data.get(key)
            return value if isinstance(value, str) else ""

        def strings(key: str) -> list[str]:
            value = data.get(key)
            return [str(item) for item in value] if isinstance(value, list) else []

        status = text("status")
        group = status_map.get(status) or (status if status in GROUP_KEYS else "other")
        record = data.get("last_record")
        return {"id": text("id"), "title": text("title") or text("id"), "status": status, "group": group,
                "goal": text("goal"), "next_action": text("next_action"), "waiting_for": text("waiting_for"),
                "resume_when": text("resume_when"),
                "last_record": record if isinstance(record, dict) and record.get("text") else None,
                "deliverables": strings("deliverables"), "aliases": strings("aliases"),
                "legacy_ids": strings("legacy_ids"), "updated_at": text("updated_at"), "path": text("path")}

    def item(self, token: str) -> dict:
        rows = self.items()["items"]
        exact = [item for item in rows if token in (item["id"], *item["legacy_ids"], *item["aliases"])]
        matches = exact or [item for item in rows if _item_hits(token, item)]
        if not matches:
            raise EntryError(_("没有找到事项：{0}").format(token), "not-found", [_("试试更短的词，或在「看进展」里浏览全部")])
        if len(matches) > 1:
            raise EntryError(_("有 {0} 个事项都匹配“{1}”").format(len(matches), token), "ambiguous",
                             [f"{item['id']}  {item['title']}" for item in matches[:10]])
        return {**matches[0], "source": self.items_source()}

    # -------------------------------------------------------------- guide

    def guide(self) -> dict:
        prefix = self.command_prefix()
        about = self.entry["about"] or _readme_intro(self.root) or \
            _("（项目还没有写简介：可以在 README.md 开头写一段，或在 Profile 的 entry.about 写一句话）")
        where = [(item["label"], item["path"]) for item in self.entry["where"]]
        if not where:
            work = self.profile.get("work") or {}
            if self.work_mode == "case-workspace":
                where.append((_("事项与各自资料"), f"{self._case_root_text()}/"))
            for root in ((work.get("assets") or {}).get("roots") or []):
                where.append((_("资料"), f"{root}/"))
            for label, relative in ((_("参考资料"), "docs/refs"), (_("草稿"), "docs/drafts"), (_("记忆"), "memory")):
                if (self.root / relative).is_dir():
                    where.append((label, f"{relative}/"))
        start = self.entry["start"] or [_("「看进展」先看进行中和等待中的事项"), _("需要哪份资料，用「找资料」输入关键词"),
                                        _("做完一步，用「记录事项」追加一条记录")]
        guide_file = self.entry["guide"]
        if guide_file:
            where.insert(0, (_("项目首页"), guide_file))
        return {"name": self.name, "kind": self.overview()["kind"], "about": about,
                "launcher": self.launcher(), "command": prefix, "where": where, "start": start,
                "examples": [(_("按关键词和日期找资料"), _("1 → 关键词 2026-10"), _("find 关键词 2026-10")),
                             (_("看某个事项现在到哪了"), _("2 → 输入事项名称或旧编号"), _("show 名称")),
                             (_("检查项目和环境"), "4", "health"),
                             (_("不进菜单、给脚本用"), "—", "progress --output json")],
                "capabilities": self.capabilities(), "readme_mentions_entry": _readme_mentions(self)}

    def capabilities(self) -> list[dict]:
        index = self.index_state()["status"]
        try:
            self.items()
            progress = ("available", "")
        except EntryError as exc:
            progress = ("unavailable", str(exc))
        record = project_write.record_capability(self)
        tools = project_write.load_tools(self)
        if tools["error"]:
            tools_state = ("unavailable", tools["error"])
        elif tools["tools"]:
            tools_state = ("available", _("{0} 个已声明").format(len(tools['tools'])))
        else:
            tools_state = ("unavailable", _("项目没有声明专用工具"))
        index_reason = {"missing": _("首次查找时建立索引"), "stale": _("查找时自动更新索引"),
                        "broken": _("索引损坏：查找时会尝试重建，也可在「修复与更新」里重建")}.get(index, "")
        return [
            {"id": "find", "title": _("找资料"), "state": "available", "reason": index_reason, "offline": True},
            {"id": "progress", "title": _("看进展"), "state": progress[0], "reason": progress[1], "offline": True},
            {"id": "guide", "title": _("项目指南"), "state": "available", "reason": "", "offline": True},
            {"id": "health", "title": _("检查项目与环境"), "state": "available",
             "reason": _("联网检查使用缓存") if self.offline else "", "offline": True},
            {"id": "fix", "title": _("修复与更新"), "state": "available",
             "reason": _("需要联网的项离线时只能预览"), "offline": False},
            {"id": "record", "title": _("记录事项"), "state": record[0], "reason": record[1], "offline": True},
            {"id": "tools", "title": _("项目工具"), "state": tools_state[0], "reason": tools_state[1], "offline": True},
        ]

    # -------------------------------------------------------------- health

    def health(self) -> dict:
        findings: list[dict] = []
        ok: list[str] = []
        self._check_environment(findings, ok)
        self._check_entry(findings, ok)
        self._check_index(findings, ok)
        self._check_items(findings, ok)
        self._check_memory(findings, ok)
        project_write.check_tools(self, findings, ok)
        source = project_maintain.check_updates(self, findings, ok)
        self._health = {"findings": findings, "ok": ok, "read_at": now_iso(), "update_source": source,
                        "skipped": self._skipped}
        return self._health

    def _check_environment(self, findings: list[dict], ok: list[str]) -> None:
        ok.append(_("运行环境（Python {0}，{1}）").format(platform.python_version(), platform.system() or sys.platform))
        if self.host is None:
            findings.append({"id": "no-agent-pack", "level": "unavailable", "area": _("环境"),
                             "title": _("找不到 agent-pack，插件、模板与运行时状态无法检查"),
                             "reason": _("当前运行方式没有携带 agent-pack"), "impact": _("找资料、看进展、指南不受影响"),
                             "next": [{"label": _("用 agenthelm 运行时打开（agenthelm runtime install）"), "action": ""}]})

    def _check_entry(self, findings: list[dict], ok: list[str]) -> None:
        launcher = self.launcher()
        if not self.entry["declared"] or not launcher:
            findings.append({"id": "add-entry", "level": "suggest", "area": _("入口"), "title": _("项目根目录还没有人用入口"),
                             "reason": _("Profile 没有 entry.launcher"), "impact": _("不知道命令的人不容易发现本菜单"),
                             "next": [{"label": _("生成根目录启动器（默认名 menu，可改）"), "action": "fix:add-entry"}]})
            return
        path = self.root / launcher
        if not path.is_file():
            findings.append({"id": "update-content", "level": "suggest", "area": _("入口"),
                             "title": _("启动器 {0} 不存在").format(launcher), "reason": _("Profile 声明了它，但根目录没有这个文件"),
                             "impact": _("只能用 agenthelm project 打开"),
                             "next": [{"label": _("按模板重新生成"), "action": "fix:update-content"}]})
        elif not os.access(path, os.X_OK):
            findings.append({"id": "launcher-mode", "level": "suggest", "area": _("入口"),
                             "title": _("启动器 {0} 没有执行权限").format(launcher), "reason": _("可能是复制或解压时丢了权限"),
                             "impact": _("需要用 sh {0} 运行").format(launcher), "next": [{"label": _("运行 chmod +x {0}").format(launcher), "action": ""}]})
        elif "agenthelm" not in path.read_text(encoding="utf-8", errors="replace"):
            findings.append({"id": "launcher-custom", "level": "suggest", "area": _("入口"),
                             "title": _("./{0} 是项目自己的脚本").format(launcher), "reason": _("文件里没有调用 agenthelm"),
                             "impact": _("运行 ./{0} 可能打不开本菜单").format(launcher),
                             "next": [{"label": _("在脚本里调用 agenthelm project --project <项目根目录> \"$@\"，或用 fix add-entry --accept {0} 换成模板（先备份）").format(launcher), "action": ""}]})
        elif self.via_launcher is not None and self.via_launcher < LAUNCHER_API:
            findings.append({"id": "update-content", "level": "update", "area": _("入口"), "title": _("启动器有新版本"),
                             "reason": _("当前启动器接口 {0}，模板为 {1}").format(self.via_launcher, LAUNCHER_API),
                             "impact": _("旧启动器仍能用"), "next": [{"label": _("更新项目模板"), "action": "fix:update-content"}]})
        else:
            ok.append(_("项目入口 ./{0}").format(launcher))
        if not _readme_mentions(self):
            findings.append({"id": "readme-entry", "level": "suggest", "area": _("入口"), "title": _("README 没有提到人用入口"),
                             "reason": _("README.md 里没有 ./{0}").format(launcher), "impact": _("新接手的人可能不知道有这个菜单"),
                             "next": [{"label": _("在 README 末尾追加「怎样使用」（带标记，可删）"), "action": "fix:add-entry"}]})

    def _check_index(self, findings: list[dict], ok: list[str]) -> None:
        state = self.index_state()
        if state["status"] == "broken":
            findings.append({"id": "rebuild-index", "level": "must", "area": _("资料"), "title": _("资料索引损坏"),
                             "reason": _("用户缓存里的索引无法读取或格式不符"), "impact": _("找资料会先尝试重建；项目文件没有受影响"),
                             "next": [{"label": _("重建索引（只改用户缓存）"), "action": "fix:rebuild-index"}]})
            return
        if state["status"] == "missing":
            ok.append(_("资料索引（尚未建立，首次查找时建立）"))
            return
        try:
            report = assets.check(self.assets_project(), limit=5)
        except (assets.AssetError, EntryError) as exc:
            findings.append({"id": "rebuild-index", "level": "must", "area": _("资料"), "title": _("资料巡检失败"),
                             "reason": str(exc), "impact": _("找资料可能不完整"),
                             "next": [{"label": _("重建索引（只改用户缓存）"), "action": "fix:rebuild-index"}]})
            return
        broken = report["broken"].get("real-broken", [])
        count = report["broken_counts"].get("real-broken", 0)
        if count:
            first = broken[0]
            findings.append({"id": "broken-links", "level": "suggest", "area": _("资料"), "title": _("资料里有 {0} 处断链").format(count),
                             "reason": _("例如 {0} 引用的 {1} 不存在").format(first['src'], first['raw']),
                             "impact": _("打开这些文件时链接失效；不影响其他资料"),
                             "next": [{"label": _("打开第一处修正"), "action": f"open:{first['src']}"}]})
        unclaimed = sum(len(paths) for paths in report["unclaimed"].values())
        ok.append(_("资料索引（{0} 个文件，{1} 更新").format(report['totals']['files'], hhmm(report['built_at']))
                  + (_("；{0} 份交付物未被事项引用").format(unclaimed) if unclaimed else "") + _("）"))

    def _check_items(self, findings: list[dict], ok: list[str]) -> None:
        try:
            data = self.items()
        except EntryError as exc:
            findings.append({"id": "items-unavailable", "level": "unavailable", "area": _("事项"), "title": _("看进展不可用"),
                             "reason": str(exc), "impact": _("其他功能不受影响"),
                             "next": [{"label": step, "action": ""} for step in exc.next_steps[:1]]})
            return
        if data["invalid"]:
            first = data["invalid"][0]
            findings.append({"id": "invalid-items", "level": "suggest", "area": _("事项"),
                             "title": _("{0} 个事项记录读不出来").format(len(data['invalid'])),
                             "reason": _("{0}：{1}").format(first['path'], first['error']), "impact": _("这些事项不会出现在「看进展」里"),
                             "next": [{"label": _("打开记录修正格式"), "action": f"open:{first['path']}"}]})
        else:
            ok.append(_("事项记录（{0} 个）").format(len(data['items'])))

    def _check_memory(self, findings: list[dict], ok: list[str]) -> None:
        memory = self.profile.get("memory")
        if not isinstance(memory, dict) or memory.get("provider") != "project-orchestrator":
            return
        root = self.root / str(memory.get("root") or "memory")
        if not root.is_dir():
            return
        script = Path(__file__).resolve().parent / "memory.py"
        completed = subprocess.run([sys.executable, str(script), "check", "--project", str(self.root),
                                    "--output", "json"], capture_output=True, text=True, check=False, timeout=60)
        try:
            report = json.loads(completed.stdout)
        except ValueError:
            return
        problems = report.get("problems") or report.get("issues") or []
        if problems:
            first = problems[0]
            findings.append({"id": "memory", "level": "suggest", "area": _("记忆"), "title": _("记忆条目有 {0} 个问题").format(len(problems)),
                             "reason": str(first.get("message") or first.get("detail") or first)[:160],
                             "impact": _("只影响 AI Agent 召回记忆"), "next": [{"label": _("在 Agent 会话中运行记忆检查并处理"), "action": ""}]})
        else:
            ok.append(_("记忆条目"))

    # -------------------------------------------------------------- maintenance and writes

    def fix_plan(self, ids: list[str], choices: dict[str, str] | None = None) -> dict:
        return project_maintain.plan(self, ids, choices or {})

    def apply_fix(self, plan: dict, choices: dict[str, str]) -> dict:
        return project_maintain.apply(self, plan, choices)

    def decide(self, skip: list[str], unskip: list[str], reason: str, apply: bool) -> dict:
        return project_maintain.decide_ids(self, skip, unskip, reason, apply)

    def record_preview(self, target: str | None, change: dict) -> dict:
        return project_write.preview(self, target, change)

    def record_apply(self, preview: dict) -> dict:
        result = project_write.apply(self, preview)
        self._items = None
        return result

    def tools(self) -> list[dict]:
        return project_write.load_tools(self)["tools"]

    def run_tool(self, tool_id: str, *, capture: bool = True) -> dict:
        return project_write.run_tool(self, tool_id, capture=capture)


def hhmm(value: str | None) -> str:
    return (value or "")[5:16].replace("T", " ")


def _item_hits(term: str, item: dict) -> bool:
    fields = [item["id"], item["title"], item["goal"], *item["aliases"], *item["legacy_ids"]]
    return term.lower() in " ".join(fields).lower()


def _open_command(action: str, target: Path) -> list[str] | None:
    absolute = str(target)
    if sys.platform == "darwin":
        return {"open": ["open", absolute], "reveal": ["open", "-R", absolute], "copy": ["pbcopy"]}[action]
    if sys.platform.startswith("linux"):
        if action == "open":
            return ["xdg-open", absolute]
        if action == "reveal":
            return ["xdg-open", str(target.parent)]
        for command in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            if shutil.which(command[0]):
                return command
    return None


def _readme(root: Path) -> str:
    for name in ("README.md", "readme.md", "README"):
        path = root / name
        if path.is_file() and not path.is_symlink():
            try:
                return path.read_text(encoding="utf-8")[:20000]
            except (OSError, UnicodeDecodeError):
                return ""
    return ""


def _readme_intro(root: Path) -> str:
    """The first plain paragraph after the title."""
    paragraph: list[str] = []
    text = re.sub(r"<!-- agenthelm:entry -->.*?<!-- /agenthelm:entry -->", "", _readme(root), flags=re.S)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "<!--", "|", "```", "![", ">")) or not stripped:
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    return " ".join(paragraph)[:400]


def _readme_mentions(provider: RealProvider) -> bool:
    launcher = provider.launcher()
    text = _readme(provider.root)
    return bool(text) and (f"./{launcher}" in text if launcher else "agenthelm project" in text)
