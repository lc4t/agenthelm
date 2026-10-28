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
import case as case_tool
import project_maintain
import private_paths
import project_write
import sop as sop_tool
from project_base import (GROUP_KEYS, LAUNCHER_API, EntryError, entry_section, find_profile_root, inside,
                          read_profile, sop_call)

RECORD_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T[0-9:]{5,8}(?:Z|[+-]\d{2}:\d{2})?)\s+(.*)$")
TOOL_TIMEOUT_SECONDS = 30


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def last_record(body: str) -> dict | None:
    """The newest bullet under ``## 工作记录`` (records are appended, so the last one)."""
    marker = "## 工作记录\n"
    start = body.find(marker)
    if start < 0:
        return None
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
            raise EntryError(f"资料扫描范围配置有误：{exc}", "profile-invalid",
                             ["检查 Profile 的 work.case.root 与 work.assets.roots"]) from exc

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
        return {"name": self.name, "kind": "、".join(map(str, types)) or "通用",
                "kind_label": ("、".join(map(str, types)) + " 项目") if types else "项目",
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
            raise EntryError(f"查找失败：{exc}", "index-broken",
                             ["重建资料索引（只改用户缓存）：fix rebuild-index --apply"]) from exc
        return {**result, "index": self.index_state()}

    def refs(self, path: str) -> dict:
        try:
            result = assets.refs(self.assets_project(), path)
        except assets.AssetError as exc:
            raise EntryError(f"查不到引用关系：{exc}", "index-broken") from exc
        return {"path": result["path"], "inbound": [item["src"] for item in result["inbound"]],
                "outbound": [item["dst"] for item in result["outbound"]],
                "affected": result.get("affected_containers", [])}

    def act(self, action: str, path: str) -> dict:
        target = inside(self.root, path, "路径")
        if not target.exists():
            raise EntryError(f"文件不存在：{path}", "not-found", ["索引可能过期：重新查找一次"])
        absolute = str(target)
        verbs = {"open": "打开", "reveal": "显示所在目录", "copy": "复制路径"}
        command = None if os.environ.get("AGENTHELM_OPEN") == "print" else _open_command(action, target)
        if command and shutil.which(command[0]):
            completed = subprocess.run(command, input=absolute.encode("utf-8") if action == "copy" else None,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                                       timeout=15)
            if completed.returncode == 0:
                return {"action": action, "path": path, "executed": True, "absolute": absolute,
                        "message": f"已{verbs[action]}：{path}"}
        return {"action": action, "path": path, "executed": False, "absolute": absolute,
                "message": f"这台机器不能直接{verbs[action]}，绝对路径：{absolute}"}

    # -------------------------------------------------------------- progress

    def items_source(self) -> str:
        items = self.entry["items"]
        if items:
            return f"项目工具 {' '.join(items['command'])}（只读接口 list）"
        return f"{self._case_root_text()}/*/CASE.md（默认 Case 工具）"

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
            raise EntryError("这个项目没有使用 Case 工作目录，也没有声明事项工具", "unavailable",
                             ["work.mode 为 legacy：事项由项目自己的方式管理",
                              "如需在本入口查看，可在 Profile 的 entry.items 声明事项工具（见 case-workspaces.md「项目入口适配」）"])
        try:
            project = case_tool.Project(self.root)
        except case_tool.CaseError as exc:
            raise EntryError(f"读不了事项：{exc}", "profile-invalid") from exc
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
            raise EntryError(f"项目事项工具无法运行：{exc}", "unavailable",
                             [f"命令：{' '.join(command)}", "检查 Profile 的 entry.items.command"]) from exc
        try:
            data = json.loads(completed.stdout)
        except ValueError as exc:
            detail = (completed.stderr or completed.stdout).strip()[:200]
            raise EntryError("项目事项工具的 list 没有返回 JSON", "unavailable",
                             [f"命令：{' '.join(command)}", detail or "（无输出）"]) from exc
        rows = data.get("items", data.get("cases")) if isinstance(data, dict) else data
        if completed.returncode != 0 or not isinstance(rows, list):
            raise EntryError("项目事项工具的 list 返回格式不符合约定", "unavailable",
                             ["约定：list --all --output json 返回 {\"items\": [...]}，见 case-workspaces.md"])
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
            raise EntryError(f"没有找到事项：{token}", "not-found", ["试试更短的词，或在「看进展」里浏览全部"])
        if len(matches) > 1:
            raise EntryError(f"有 {len(matches)} 个事项都匹配“{token}”", "ambiguous",
                             [f"{item['id']}  {item['title']}" for item in matches[:10]])
        return {**matches[0], "source": self.items_source()}

    # -------------------------------------------------------------- guide

    def guide(self) -> dict:
        prefix = self.command_prefix()
        about = self.entry["about"] or _readme_intro(self.root) or \
            "（项目还没有写简介：可以在 README.md 开头写一段，或在 Profile 的 entry.about 写一句话）"
        where = [(item["label"], item["path"]) for item in self.entry["where"]]
        if not where:
            work = self.profile.get("work") or {}
            if self.work_mode == "case-workspace":
                where.append(("事项与各自资料", f"{self._case_root_text()}/"))
            for root in ((work.get("assets") or {}).get("roots") or []):
                where.append(("资料", f"{root}/"))
            for label, relative in (("参考资料", "docs/refs"), ("草稿", "docs/drafts"), ("记忆", "memory"), ("流程（SOP）", "sops")):
                if (self.root / relative).is_dir():
                    where.append((label, f"{relative}/"))
        start = self.entry["start"] or ["「看进展」先看进行中和等待中的事项", "需要哪份资料，用「找资料」输入关键词",
                                        "做完一步，用「记录事项」追加一条记录"]
        guide_file = self.entry["guide"]
        if guide_file:
            where.insert(0, ("项目首页", guide_file))
        return {"name": self.name, "kind": self.overview()["kind"], "about": about,
                "launcher": self.launcher(), "command": prefix, "where": where, "start": start,
                "examples": [("按关键词和日期找资料", "1 → 关键词 2026-10", "find 关键词 2026-10"),
                             ("看某个事项现在到哪了", "2 → 输入事项名称或旧编号", "show 名称"),
                             ("检查项目和环境", "4", "health"),
                             ("不进菜单、给脚本用", "—", "progress --output json")],
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
            tools_state = ("available", f"{len(tools['tools'])} 个已声明")
        else:
            tools_state = ("unavailable", "项目没有声明专用工具")
        index_reason = {"missing": "首次查找时建立索引", "stale": "查找时自动更新索引",
                        "broken": "索引损坏：查找时会尝试重建，也可在「修复与更新」里重建"}.get(index, "")
        sops = {"id": "sop", "title": "流程（SOP）", "state": "available", "offline": True,
                "reason": "" if any(True for _ in sop_tool.load_all(self.root)) else "还没有流程：sop new 创建，或让 Agent 带你谈清"}
        return [
            {"id": "find", "title": "找资料", "state": "available", "reason": index_reason, "offline": True},
            {"id": "progress", "title": "看进展", "state": progress[0], "reason": progress[1], "offline": True},
            {"id": "guide", "title": "项目指南", "state": "available", "reason": "", "offline": True},
            {"id": "health", "title": "检查项目与环境", "state": "available",
             "reason": "联网检查使用缓存" if self.offline else "", "offline": True},
            {"id": "fix", "title": "修复与更新", "state": "available",
             "reason": "需要联网的项离线时只能预览", "offline": False},
            {"id": "record", "title": "记录事项", "state": record[0], "reason": record[1], "offline": True},
            {"id": "tools", "title": "项目工具", "state": tools_state[0], "reason": tools_state[1], "offline": True},
            sops,
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
        self._check_sop(findings, ok)
        self._check_private(findings, ok)
        project_write.check_tools(self, findings, ok)
        source = project_maintain.check_updates(self, findings, ok)
        self._health = {"findings": findings, "ok": ok, "read_at": now_iso(), "update_source": source,
                        "skipped": self._skipped}
        return self._health

    def _check_environment(self, findings: list[dict], ok: list[str]) -> None:
        ok.append(f"运行环境（Python {platform.python_version()}，{platform.system() or sys.platform}）")
        if self.host is None:
            findings.append({"id": "no-agent-pack", "level": "unavailable", "area": "环境",
                             "title": "找不到 agent-pack，插件、模板与运行时状态无法检查",
                             "reason": "当前运行方式没有携带 agent-pack", "impact": "找资料、看进展、指南不受影响",
                             "next": [{"label": "用 agenthelm 运行时打开（agenthelm runtime install）", "action": ""}]})

    def _check_entry(self, findings: list[dict], ok: list[str]) -> None:
        launcher = self.launcher()
        if not self.entry["declared"] or not launcher:
            findings.append({"id": "add-entry", "level": "suggest", "area": "入口", "title": "项目根目录还没有人用入口",
                             "reason": "Profile 没有 entry.launcher", "impact": "不知道命令的人不容易发现本菜单",
                             "next": [{"label": "生成根目录启动器（默认名 menu，可改）", "action": "fix:add-entry"}]})
            return
        path = self.root / launcher
        if not path.is_file():
            findings.append({"id": "update-content", "level": "suggest", "area": "入口",
                             "title": f"启动器 {launcher} 不存在", "reason": "Profile 声明了它，但根目录没有这个文件",
                             "impact": "只能用 agenthelm project 打开",
                             "next": [{"label": "按模板重新生成", "action": "fix:update-content"}]})
        elif not os.access(path, os.X_OK):
            findings.append({"id": "launcher-mode", "level": "suggest", "area": "入口",
                             "title": f"启动器 {launcher} 没有执行权限", "reason": "可能是复制或解压时丢了权限",
                             "impact": f"需要用 sh {launcher} 运行", "next": [{"label": f"运行 chmod +x {launcher}", "action": ""}]})
        elif "agenthelm" not in path.read_text(encoding="utf-8", errors="replace"):
            findings.append({"id": "launcher-custom", "level": "suggest", "area": "入口",
                             "title": f"./{launcher} 是项目自己的脚本", "reason": "文件里没有调用 agenthelm",
                             "impact": f"运行 ./{launcher} 可能打不开本菜单",
                             "next": [{"label": f"在脚本里调用 agenthelm project --project <项目根目录> \"$@\"，"
                                       f"或用 fix add-entry --accept {launcher} 换成模板（先备份）", "action": ""}]})
        elif self.via_launcher is not None and self.via_launcher < LAUNCHER_API:
            findings.append({"id": "update-content", "level": "update", "area": "入口", "title": "启动器有新版本",
                             "reason": f"当前启动器接口 {self.via_launcher}，模板为 {LAUNCHER_API}",
                             "impact": "旧启动器仍能用", "next": [{"label": "更新项目模板", "action": "fix:update-content"}]})
        else:
            ok.append(f"项目入口 ./{launcher}")
        if not _readme_mentions(self):
            findings.append({"id": "readme-entry", "level": "suggest", "area": "入口", "title": "README 没有提到人用入口",
                             "reason": f"README.md 里没有 ./{launcher}", "impact": "新接手的人可能不知道有这个菜单",
                             "next": [{"label": "在 README 末尾追加「怎样使用」（带标记，可删）", "action": "fix:add-entry"}]})

    def _check_index(self, findings: list[dict], ok: list[str]) -> None:
        state = self.index_state()
        if state["status"] == "broken":
            findings.append({"id": "rebuild-index", "level": "must", "area": "资料", "title": "资料索引损坏",
                             "reason": "用户缓存里的索引无法读取或格式不符", "impact": "找资料会先尝试重建；项目文件没有受影响",
                             "next": [{"label": "重建索引（只改用户缓存）", "action": "fix:rebuild-index"}]})
            return
        if state["status"] == "missing":
            ok.append("资料索引（尚未建立，首次查找时建立）")
            return
        try:
            report = assets.check(self.assets_project(), limit=5)
        except (assets.AssetError, EntryError) as exc:
            findings.append({"id": "rebuild-index", "level": "must", "area": "资料", "title": "资料巡检失败",
                             "reason": str(exc), "impact": "找资料可能不完整",
                             "next": [{"label": "重建索引（只改用户缓存）", "action": "fix:rebuild-index"}]})
            return
        broken = report["broken"].get("real-broken", [])
        count = report["broken_counts"].get("real-broken", 0)
        if count:
            first = broken[0]
            findings.append({"id": "broken-links", "level": "suggest", "area": "资料", "title": f"资料里有 {count} 处断链",
                             "reason": f"例如 {first['src']} 引用的 {first['raw']} 不存在",
                             "impact": "打开这些文件时链接失效；不影响其他资料",
                             "next": [{"label": "打开第一处修正", "action": f"open:{first['src']}"}]})
        unclaimed = sum(len(paths) for paths in report["unclaimed"].values())
        ok.append(f"资料索引（{report['totals']['files']} 个文件，{hhmm(report['built_at'])} 更新"
                  + (f"；{unclaimed} 份交付物未被事项引用" if unclaimed else "") + "）")

    def _check_items(self, findings: list[dict], ok: list[str]) -> None:
        try:
            data = self.items()
        except EntryError as exc:
            findings.append({"id": "items-unavailable", "level": "unavailable", "area": "事项", "title": "看进展不可用",
                             "reason": str(exc), "impact": "其他功能不受影响",
                             "next": [{"label": step, "action": ""} for step in exc.next_steps[:1]]})
            return
        if data["invalid"]:
            first = data["invalid"][0]
            findings.append({"id": "invalid-items", "level": "suggest", "area": "事项",
                             "title": f"{len(data['invalid'])} 个事项记录读不出来",
                             "reason": f"{first['path']}：{first['error']}", "impact": "这些事项不会出现在「看进展」里",
                             "next": [{"label": "打开记录修正格式", "action": f"open:{first['path']}"}]})
        else:
            ok.append(f"事项记录（{len(data['items'])} 个）")

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
            findings.append({"id": "memory", "level": "suggest", "area": "记忆", "title": f"记忆条目有 {len(problems)} 个问题",
                             "reason": str(first.get("message") or first.get("detail") or first)[:160],
                             "impact": "只影响 AI Agent 召回记忆", "next": [{"label": "在 Agent 会话中运行记忆检查并处理", "action": ""}]})
        else:
            ok.append("记忆条目")

    def _check_sop(self, findings: list[dict], ok: list[str]) -> None:
        try:
            sops = sop_tool.load_all(self.root)
            if not sops:
                return
            defaults = sop_tool.read_defaults(self.root)
            results = sop_tool.check_all(sops, defaults, self.root)
            runs, _ = sop_tool.last_runs(self.root)
            due = [info for sop in sops if not sop["shadowed"] and (info := sop_tool.due_info(sop, runs, sop_tool.today())) and info["due"]]
        except (sop_tool.SopError, OSError) as exc:
            findings.append({"id": "sop", "level": "suggest", "area": "流程", "title": "流程（SOP）读取失败",
                             "reason": str(exc)[:160], "impact": "AI Agent 和本入口都读不到流程",
                             "next": [{"label": "运行 sop validate 查看原因", "action": ""}]})
            return
        broken = [result for result in results if not result["valid"]]
        if broken:
            first = next(item for result in broken for item in result["issues"] if item["level"] == "error")
            findings.append({"id": "sop", "level": "suggest", "area": "流程", "title": f"{len(broken)} 个流程有错误",
                             "reason": f"{broken[0]['id']}：{first['message']}"[:160],
                             "impact": "有错误的 active 流程仍会被索引，Agent 可能照着不完整的步骤执行",
                             "next": [{"label": "运行 sop validate 查看全部问题", "action": ""}]})
        else:
            active = sum(1 for sop in sops if sop["meta"].get("status") == "active" and not sop["shadowed"])
            ok.append(f"流程（{len(sops)} 个，{active} 个已启用）")
        if due:
            names = "、".join(f"{info['id']}（{info['schedule']}，上次 {info['last_run'] or '从未'}）" for info in due)
            findings.append({"id": "sop-due", "level": "suggest", "area": "流程", "title": f"{len(due)} 个流程已到期",
                             "reason": names[:160], "impact": "只是提醒：到期不会自动执行",
                             "next": [{"label": "让 Agent 按它执行，或运行 sop run <id>", "action": ""}]})

    def _check_private(self, findings: list[dict], ok: list[str]) -> None:
        """私有目录：内容默认不进 git；用到它的事项应标为 private。"""
        dirs = private_paths.declared(self.root)
        if not dirs:
            return
        for item in dirs:
            state = private_paths.git_state(self.root, item)
            if state["ignored"] is False:
                findings.append({"id": f"private-ignore:{item}", "level": "suggest", "area": "私有目录",
                                 "title": f"{item}/ 没有被 git 忽略", "reason": "私有目录的内容默认不应进入 git",
                                 "impact": "git add 时可能把私有内容一起提交",
                                 "next": [{"label": f"运行 sop private --apply，在 {item}/ 里创建 .gitignore（先预览）", "action": ""}]})
            elif state["tracked"]:
                findings.append({"id": f"private-tracked:{item}", "level": "suggest", "area": "私有目录",
                                 "title": f"{item}/ 下有 {len(state['tracked'])} 个文件已被 git 跟踪",
                                 "reason": f"例如 {state['tracked'][0]}", "impact": "它们会随提交一起上传；.gitignore 对已跟踪的文件无效",
                                 "next": [{"label": f"确认后运行 git rm -r --cached {item} 取消跟踪（历史里的内容仍在）", "action": ""}]})
            else:
                ok.append(f"私有目录 {item}/（" + ("已被 git 忽略" if state["ignored"] else "不在 git 仓库里") + "；读取前须经你确认）")
        loose = []
        try:
            for directory in case_tool.Project(self.root).case_dirs():
                data, _ = case_tool.read_case(directory / "CASE.md")
                refs = [*(data.get("deliverables") or []), *(data.get("evidence") or [])]
                if data.get("sensitivity") != "private" and any(
                        private_paths.is_private(self.root, value, dirs) or private_paths.is_private(self.root, str(directory / value), dirs)
                        for value in refs):
                    loose.append(data.get("id", directory.name))
        except (case_tool.CaseError, OSError, UnicodeDecodeError):
            return
        if loose:
            findings.append({"id": "private-cases", "level": "suggest", "area": "私有目录",
                             "title": f"{len(loose)} 个事项用到了私有目录，但没有标为 private", "reason": "、".join(loose[:3]),
                             "impact": "按约定，用到私有目录的事项应标为 private",
                             "next": [{"label": "运行 case.py update <事项> --sensitivity private", "action": ""}]})

    def sop(self, argv: list[str]) -> tuple[int, dict, list[str]]:
        return sop_call(argv, self.root)

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
