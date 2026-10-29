"""项目入口的演示模式（--demo）：内置几个虚构项目，不读写任何真实文件。

用来在没有项目时体验菜单，也是界面测试的固定数据。数据形状与真实来源（project_real）
一致：资料结果沿用 ``assets.find`` 的字段，事项沿用 ``case.py`` 的 Frontmatter 字段。所有
“执行”都只返回模拟结果，并带 ``simulated: True``。

流程（SOP）的演示不同：它直接运行真实的 sop.py，但项目、SOP 与 Case 都在一个用完即删的
临时目录里（进程结束自动清理），不碰任何真实项目文件。唯一读取真实环境的是“执行前检查”：
它看的是这台机器上有没有对应的命令与环境变量，这正是它要演示的东西。
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import shutil
import tempfile
import weakref
from pathlib import Path
from typing import Any

from project_base import EntryError, sop_call
from i18n_orchestrator import _  # noqa: E402

DEMO_NOW = "2026-10-25T09:12:00+08:00"
FAULTS = ("index-broken", "offline", "runtime-old", "conflict")


def _asset(path: str, title: str, case: str, status: str, modified: str, *, role: str = "deliverable",
           versions: tuple[tuple[str, str], ...] = (), refs: int = 0, git: str = "tracked") -> dict:
    return {"path": path, "name": path.rsplit("/", 1)[-1], "title": title, "role": role,
            "container": case, "case_title": "", "status": status, "modified": modified, "git": git,
            "referenced_by": refs,
            "versions": [{"path": p, "name": p.rsplit("/", 1)[-1], "modified": m, "git": "tracked"}
                         for p, m in versions]}


def _item(case_id: str, title: str, status: str, updated: str, *, goal: str, next_action: str = "",
          waiting_for: str = "", resume_when: str = "", last: tuple[str, str] | None = None,
          deliverables: tuple[str, ...] = (), aliases: tuple[str, ...] = (), legacy: tuple[str, ...] = (),
          path: str | None = None) -> dict:
    return {"id": case_id, "title": title, "status": status, "goal": goal, "next_action": next_action,
            "waiting_for": waiting_for, "resume_when": resume_when,
            "last_record": {"at": last[0], "text": last[1]} if last else None,
            "deliverables": list(deliverables), "aliases": list(aliases), "legacy_ids": list(legacy),
            "updated_at": updated, "path": path or f"cases/{case_id}/CASE.md"}


STUDY = {
    "name": "日语 N2 备考", "kind": "学习",
    "about": "按周安排语法、词汇、听力与阅读，把笔记、练习记录和模拟题放在一起，随时看到复习到哪一步。",
    "launcher": "menu",
    "items_source": "cases/*/CASE.md（默认 Case 工具）",
    "items": [
        _item("CASE-20261003-002-grammar-review", "语法总复习", "active", "2026-10-24T21:30:00+08:00",
              goal="把 N2 语法 1–20 课过完一遍并整理错题", next_action="完成第 12–15 课练习",
              last=("2026-10-24T21:30:00+08:00", "完成第 11 课错题整理，新增 6 条易混语法"),
              deliverables=("cases/CASE-20261003-002-grammar-review/grammar-summary-v3.md",),
              aliases=("语法",)),
        _item("CASE-20261005-001-listening-daily", "听力每日练习", "active", "2026-10-23T22:05:00+08:00",
              goal="每天 20 分钟精听并记录正确率", next_action="今天听第 18 套第 2 部分",
              last=("2026-10-23T22:05:00+08:00", "第 17 套正确率 72%"),
              deliverables=("cases/CASE-20261005-001-listening-daily/listening-log.csv",)),
        _item("CASE-20261010-003-exam-signup", "报名 12 月考试", "waiting", "2026-10-20T12:00:00+08:00",
              goal="完成 12 月考试报名并保存准考信息", waiting_for="报名网站开放",
              resume_when="官网公布报名时间后（预计 10-28）",
              last=("2026-10-20T12:00:00+08:00", "确认报名所需材料：照片、证件扫描件")),
        _item("CASE-20260915-002-reading", "长文精读", "suspended", "2026-10-02T20:00:00+08:00",
              goal="每周精读两篇长文", next_action="恢复后从第 6 篇开始",
              resume_when="语法总复习完成后",
              last=("2026-10-02T20:00:00+08:00", "暂停：先集中复习语法")),
        _item("CASE-20260901-001-vocab-round1", "词汇第一轮", "done", "2026-09-28T21:00:00+08:00",
              goal="背完核心词汇 2000 个", last=("2026-09-28T21:00:00+08:00", "第一轮完成，导出卡片"),
              deliverables=("cases/CASE-20260901-001-vocab-round1/vocab-cards.apkg",),
              aliases=("单词", "词汇"), legacy=("T-0007",)),
    ],
    "assets": [
        _asset("cases/CASE-20261003-002-grammar-review/grammar-summary-v3.md", "语法总结（第 3 版）",
               "CASE-20261003-002-grammar-review", "active", "2026-10-24T21:28:00+08:00", refs=2,
               versions=(("cases/CASE-20261003-002-grammar-review/grammar-summary-v2.md", "2026-10-15T20:10:00+08:00"),
                         ("cases/CASE-20261003-002-grammar-review/grammar-summary-v1.md", "2026-10-06T19:40:00+08:00"))),
        _asset("cases/CASE-20261005-001-listening-daily/listening-log.csv", "听力练习记录",
               "CASE-20261005-001-listening-daily", "active", "2026-10-23T22:05:00+08:00", role="data", refs=1),
        _asset("materials/mock-exams/2025-12-mock.pdf", "模拟题 2025 年 12 月", "materials", "",
               "2026-09-20T10:00:00+08:00", role="source", refs=1),
        _asset("materials/mock-exams/2025-07-mock.pdf", "模拟题 2025 年 7 月", "materials", "",
               "2026-09-20T10:00:00+08:00", role="source"),
        _asset("cases/CASE-20260901-001-vocab-round1/vocab-cards.apkg", "词汇卡片导出",
               "CASE-20260901-001-vocab-round1", "done", "2026-09-28T21:00:00+08:00", refs=1),
        _asset("study-plan.md", "学习计划", "project", "", "2026-10-01T09:00:00+08:00",
               role="project-file", refs=0),
    ],
    "refs": {
        "cases/CASE-20261003-002-grammar-review/grammar-summary-v3.md": {
            "inbound": ["cases/CASE-20261003-002-grammar-review/CASE.md", "study-plan.md"],
            "outbound": ["materials/mock-exams/2025-12-mock.pdf", "img/particles.png（不存在）"]},
    },
    "file_count": 46,
    "where": [("事项与各自资料", "cases/"), ("教材与模拟题", "materials/"), ("学习计划", "study-plan.md")],
    "start": ["先看「看进展」里进行中的事项和下一动作", "需要哪份笔记或模拟题，用「找资料」输入关键词",
              "做完一步后，用「记录事项」追加一条记录"],
    "examples": [("找去年 12 月的模拟题", "1 → 模拟题 2025-12", "find 模拟题 2025-12"),
                 ("看语法复习到哪了", "2 → 输入 语法", "show 语法"),
                 ("按旧编号找已完成的词汇事项", "2 → 输入 T-0007", "show T-0007")],
    "health": [
        {"id": "broken-link", "level": "suggest", "area": "资料", "title": "资料里有 1 处断链",
         "reason": "grammar-summary-v3.md 引用的 img/particles.png 不存在",
         "impact": "打开这份笔记时缺一张图；不影响其他资料",
         "next": [{"label": "打开笔记修正链接", "action": "open:cases/CASE-20261003-002-grammar-review/grammar-summary-v3.md"}]},
        {"id": "update-orchestrator", "level": "update", "area": "插件", "title": "project-orchestrator 有新版本",
         "reason": "已装 1.7.0，云端 1.8.0（只改代码，无不兼容变更）", "impact": "不更新也能继续使用",
         "next": [{"label": "查看更新内容并更新", "action": "fix:update-orchestrator"}]},
        {"id": "opinion-missing", "level": "unavailable", "area": "规则", "title": "行为规则（Opinion）未配置",
         "reason": "项目没有 OPINION.md 的已确认版本", "impact": "只影响 AI Agent 的行为约定；本菜单不受影响",
         "next": [{"label": "需要时在 Agent 会话中用 opinion-manager 配置", "action": ""}]},
    ],
    "ok": ["运行环境（Python 3.12）", "插件兼容", "事项记录格式", "资料索引"],
    "fixes": {
        "update-orchestrator": {
            "title": "更新 project-orchestrator 1.7.0 → 1.8.0", "kind": "plugin", "network": True,
            "changes": ["新增：看进展支持按标签筛选", "修复：日期查找对“10月”识别不全"],
            "writes": [{"path": "~/.claude/plugins/cache/agenthelm/project-orchestrator/1.8.0/", "scope": "客户端插件目录"}],
            "custom": [], "breaking": False,
            "recovery": "旧版本仍保留在客户端缓存，可用 agenthelm update 回到 1.7.0",
            "verify": "重新读取已安装版本：1.8.0"},
    },
    "record": {"provider": "default", "write": True},
    "tools": [],
}

PERSONAL = {
    "name": "家里搬家", "kind": "个人事务",
    "about": "整理找房、比价、宽带迁移和旧物处理，把合同扫描件、报价和清单放在各自事项里。",
    "launcher": "menu",
    "items_source": "cases/*/CASE.md（默认 Case 工具）",
    "items": [
        _item("CASE-20261008-001-movers-quote", "搬家公司比价", "waiting", "2026-10-22T19:00:00+08:00",
              goal="选定搬家公司并确定日期", waiting_for="B 公司回复报价",
              resume_when="收到报价后对比三家", last=("2026-10-22T19:00:00+08:00", "A、C 两家报价已收到，已填入对比表"),
              deliverables=("cases/CASE-20261008-001-movers-quote/quote-compare-v2.xlsx",), aliases=("搬家公司",)),
        _item("CASE-20261009-002-broadband", "宽带迁移", "active", "2026-10-24T10:30:00+08:00",
              goal="新家入住当天能上网", next_action="打电话预约 11-02 上门",
              last=("2026-10-24T10:30:00+08:00", "确认新地址可办理")),
        _item("CASE-20261011-001-old-furniture", "旧家具处理", "active", "2026-10-21T21:00:00+08:00",
              goal="搬家前处理掉不带走的家具", next_action="给书柜拍照挂到二手平台",
              last=("2026-10-21T21:00:00+08:00", "列出 7 件不带走的家具"),
              deliverables=("cases/CASE-20261011-001-old-furniture/furniture-list.md",)),
        _item("CASE-20261020-001-address-change", "地址变更清单", "captured", "2026-10-20T08:00:00+08:00",
              goal="（待澄清）哪些地方需要改地址"),
        _item("CASE-20260920-001-find-flat", "找房", "done", "2026-10-05T18:00:00+08:00",
              goal="签下新房", last=("2026-10-05T18:00:00+08:00", "已签约，扫描件已存档"),
              deliverables=("cases/CASE-20260920-001-find-flat/lease-scan.pdf",), legacy=("找房子",)),
    ],
    "assets": [
        _asset("cases/CASE-20261008-001-movers-quote/quote-compare-v2.xlsx", "搬家报价对比（第 2 版）",
               "CASE-20261008-001-movers-quote", "waiting", "2026-10-22T19:00:00+08:00", refs=1,
               versions=(("cases/CASE-20261008-001-movers-quote/quote-compare-v1.xlsx", "2026-10-15T21:00:00+08:00"),)),
        _asset("cases/CASE-20260920-001-find-flat/lease-scan.pdf", "租房合同扫描件",
               "CASE-20260920-001-find-flat", "done", "2026-10-05T18:00:00+08:00", refs=1, git="ignored"),
        _asset("cases/CASE-20261011-001-old-furniture/furniture-list.md", "不带走的家具清单",
               "CASE-20261011-001-old-furniture", "active", "2026-10-21T21:00:00+08:00", refs=1),
        _asset("cases/CASE-20261011-001-old-furniture/photos/bookshelf.jpg", "书柜照片",
               "CASE-20261011-001-old-furniture", "active", "2026-10-21T21:05:00+08:00", role="image"),
    ],
    "refs": {},
    "file_count": 19,
    "where": [("事项与各自资料", "cases/"), ("合同等扫描件", "各事项目录内（不入 Git）")],
    "start": ["「看进展」先看等待中的事项：等谁、什么时候恢复", "找报价或合同，用「找资料」",
              "电话打完、事情办完，用「记录事项」追加一条"],
    "examples": [("找租房合同", "1 → 合同", "find 合同"), ("看还在等谁", "2 → 看「等待」分组", "progress --status waiting")],
    "health": [
        {"id": "template-update", "level": "update", "area": "项目模板", "title": "项目说明模板有新版本",
         "reason": "agents-init 项目契约模板 1.0.0 → 1.1.0；其中 AGENTS.md 你改过", "impact": "不更新不影响使用；更新时你改过的文件逐个选择保留或采用",
         "next": [{"label": "查看影响并选择", "action": "fix:template-update"}]},
        {"id": "no-ai-client", "level": "unavailable", "area": "环境", "title": "本机没有安装 AI 客户端",
         "reason": "没有找到 Claude Code、Codex 或 Cursor", "impact": "本菜单的全部日常功能不受影响；只有“安装插件到客户端”不可用",
         "next": [{"label": "需要时安装任一客户端后，在 5 修复与更新 里安装插件", "action": ""}]},
    ],
    "ok": ["运行环境（Python 3.11）", "事项记录格式", "资料索引"],
    "fixes": {
        "template-update": {
            "title": "项目说明模板 1.0.0 → 1.1.0", "kind": "template", "network": False,
            "changes": ["AGENTS.md：新增「人用入口」一节", "新增启动器说明"],
            "writes": [{"path": "AGENTS.md", "scope": "项目"}, {"path": "CLAUDE.md", "scope": "项目"},
                       {"path": ".agents/moe.sakanano.agent-pack/content.lock.json", "scope": "项目"}],
            "custom": [{"path": "AGENTS.md", "state": "你改过", "default": "keep"}],
            "untouched": ["CLAUDE.md"], "breaking": False,
            "recovery": "~/.agent-pack/recovery/20261025-091500-content/",
            "verify": "逐个比对写入后的摘要与预览一致；保留的文件内容不变"},
    },
    "record": {"provider": "default", "write": True},
    "tools": [],
}

RESEARCH = {
    "name": "城市鸟类观察", "kind": "研究",
    "about": "按观察点记录鸟类，整理照片与观察表，定期汇总成季度报告。事项由项目自己的工具 tools/obs.py 管理。",
    "launcher": "obs",
    "items_source": "项目工具 tools/obs.py（只读接口 list/show）",
    "status_map": {"进行中": "active", "待复核": "waiting", "归档": "done"},
    "items": [
        _item("OBS-2026Q4-01", "公园东湖越冬观察", "进行中", "2026-10-24T07:40:00+08:00",
              goal="每周两次记录东湖水鸟种类与数量", next_action="周日早上第 3 次观察",
              last=("2026-10-24T07:40:00+08:00", "记录 14 种，新增 1 种待确认"),
              deliverables=("data/east-lake/2026-10-24.csv",), path="observations/OBS-2026Q4-01.yaml"),
        _item("OBS-2026Q4-02", "待确认物种：疑似黑尾塍鹬", "待复核", "2026-10-24T08:10:00+08:00",
              goal="确认 10-24 拍到的个体", waiting_for="观鸟群里有经验的人回复",
              last=("2026-10-24T08:10:00+08:00", "照片已发出"), deliverables=("photos/2026-10-24/IMG_0412.jpg",),
              path="observations/OBS-2026Q4-02.yaml"),
        _item("OBS-2026Q3-05", "第三季度汇总", "归档", "2026-10-03T20:00:00+08:00",
              goal="汇总 7–9 月观察数据", last=("2026-10-03T20:00:00+08:00", "报告完成"),
              deliverables=("reports/2026-Q3-summary.md",), legacy=("Q3汇总",),
              path="observations/OBS-2026Q3-05.yaml"),
    ],
    "assets": [
        _asset("reports/2026-Q3-summary.md", "第三季度观察汇总", "reports", "", "2026-10-03T20:00:00+08:00", refs=1,
               versions=(("reports/2026-Q3-summary-draft.md", "2026-10-01T22:00:00+08:00"),)),
        _asset("data/east-lake/2026-10-24.csv", "东湖观察表 10-24", "data", "", "2026-10-24T07:40:00+08:00",
               role="data", refs=1),
        _asset("photos/2026-10-24/IMG_0412.jpg", "疑似黑尾塍鹬", "photos", "", "2026-10-24T07:20:00+08:00",
               role="image", refs=1, git="ignored"),
    ],
    "refs": {},
    "file_count": 312,
    "where": [("观察记录（权威）", "observations/"), ("观察表", "data/"), ("照片（不入 Git）", "photos/"),
              ("季度报告", "reports/")],
    "start": ["用 ./obs 打开本菜单", "「看进展」显示 tools/obs.py 提供的观察事项",
              "新建或修改观察记录请用 tools/obs.py 本身（本入口暂不支持写入，原因见「记录事项」）"],
    "examples": [("找第三季度报告", "1 → 季度 汇总", "find 季度 汇总"),
                 ("导出观察数据", "7 → 导出观察表", "tools export-csv")],
    "health": [
        {"id": "write-gap", "level": "unavailable", "area": "事项", "title": "记录事项不可用",
         "reason": "项目工具 tools/obs.py 只提供 list/show，没有写接口", "impact": "可以浏览与查找；新建和修改请直接用 tools/obs.py",
         "next": [{"label": "如需在菜单里写入，请项目维护者为 obs.py 增加 append / set-status 接口", "action": ""}]},
        {"id": "sync-unavailable", "level": "unavailable", "area": "项目工具", "title": "“同步到在线物种库”当前不可用",
         "reason": "缺少环境变量 BIRDDB_TOKEN（账号令牌）", "impact": "只影响这一项工具",
         "next": [{"label": "在本机设置 BIRDDB_TOKEN 后重新检查", "action": ""}]},
    ],
    "ok": ["运行环境（Python 3.13）", "项目工具 tools/obs.py 可运行", "资料索引"],
    "fixes": {},
    "record": {"provider": "project-tool", "write": False,
               "gap": "项目声明了自有工具 tools/obs.py，它只有只读接口（list / show），没有写接口。"
                      "本入口不会绕过项目工具直接改写 observations/ 下的文件。"},
    "tools": [
        {"id": "export-csv", "title": "导出观察表", "description": "把全部观察记录导出为一个 CSV",
         "command": ["python3", "tools/obs.py", "export", "--format", "csv", "--to", "exports/"],
         "network": False, "account": None, "writes": ["exports/"], "requires": ["python3"],
         "state": "available", "reason": ""},
        {"id": "sync-db", "title": "同步到在线物种库", "description": "把本季度记录上传到在线物种库",
         "command": ["python3", "tools/obs.py", "sync"], "network": True, "account": "BIRDDB_TOKEN",
         "writes": ["在线物种库（外部系统）"], "requires": ["python3", "网络", "BIRDDB_TOKEN"],
         "state": "unavailable", "reason": "缺少环境变量 BIRDDB_TOKEN"},
    ],
}

BLANK = {
    "name": "新项目", "kind": "通用",
    "about": "刚初始化的项目，还没有事项和资料。",
    "launcher": "menu",
    "items_source": "cases/*/CASE.md（默认 Case 工具）",
    "items": [], "assets": [], "refs": {}, "file_count": 0,
    "where": [("事项与各自资料", "cases/（目前为空）"), ("参考资料", "docs/refs/")],
    "start": ["用「记录事项」新建第一个事项", "把资料放进事项目录或 docs/refs/，之后就能在「找资料」里查到"],
    "examples": [("新建第一个事项", "6 → n", "record new --title 第一个事项 --goal 写下目标 --apply")],
    "health": [], "ok": ["运行环境", "插件兼容", "事项记录格式（暂无事项）"], "fixes": {},
    "record": {"provider": "default", "write": True}, "tools": [],
}

SCENARIOS = {"study": STUDY, "personal": PERSONAL, "research": RESEARCH, "blank": BLANK}
DEFAULT_GROUPS = {"active": "active", "waiting": "waiting", "captured": "captured", "suspended": "suspended",
                  "done": "done", "abandoned": "abandoned"}


DemoError = EntryError


# 演示数据本身是中文原文；按当前语言整棵翻译。旧编号、编号与路径是用来匹配和查找的标识，保持原样；
# 项目自定义的状态词（status 与 status_map 的键）一起翻译，两边保持对应。
KEEP_KEYS = {"legacy_ids", "id", "path", "command", "launcher"}


def translate_tree(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {(_(item_key) if key == "status_map" and isinstance(item_key, str) else item_key):
                item if item_key in KEEP_KEYS else translate_tree(item, item_key) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(translate_tree(item) for item in value)
    return _(value) if isinstance(value, str) else value


@contextlib.contextmanager
def _sop_home(root: Path):
    """演示期间把用户级 SOP 目录指到临时目录，结束后原样恢复环境变量。"""
    previous = os.environ.get("AGENTHELM_SOP_HOME")
    os.environ["AGENTHELM_SOP_HOME"] = str(root.parent / "user-sops")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHELM_SOP_HOME", None)
        else:
            os.environ["AGENTHELM_SOP_HOME"] = previous


class DemoProvider:
    """内存里的虚构项目；所有结果带 demo 标记，所有写入只模拟。"""

    simulated = True

    def __init__(self, scenario: str, fault: str | None = None) -> None:
        if scenario not in SCENARIOS:
            raise DemoError(_("没有这个演示场景：{0}").format(scenario), "usage", [_("可选：{0}").format(_('、').join(SCENARIOS))])
        if fault is not None and fault not in FAULTS:
            raise DemoError(_("没有这个演示故障：{0}").format(fault), "usage", [_("可选：{0}").format(_('、').join(FAULTS))])
        self.scenario_id = scenario
        self.data = translate_tree(copy.deepcopy(SCENARIOS[scenario]))
        self.fault = fault
        titles = {item["id"]: item["title"] for item in self.data["items"]}
        for asset in self.data["assets"]:
            asset["case_title"] = titles.get(asset["container"], "")
        self.name = self.data["name"]
        self.entry_api = 2 if fault == "runtime-old" else 1
        self.resolved: set[str] = set()  # fixes simulated in this session
        self._sandbox: Path | None = None

    # -------------------------------------------------------------- 流程（SOP）：临时目录里的真实代码

    def sop(self, argv: list[str]) -> tuple[int, dict, list[str]]:
        root = self._sop_sandbox()
        with _sop_home(root):
            return sop_call(argv, root)

    def _sop_sandbox(self) -> Path:
        if self._sandbox is not None:
            return self._sandbox / "project"
        self._sandbox = Path(tempfile.mkdtemp(prefix="agenthelm-demo-"))
        weakref.finalize(self, shutil.rmtree, self._sandbox, True)  # 进程结束或对象回收时清理
        root = self._sandbox / "project"
        profile = root / ".agents" / "moe.sakanano.agent-pack" / "project.json"
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({"schema_version": "1.0", "name": self.name, "sop": {"root": "sops"},
                                       "work": {"mode": "case-workspace", "case": {"root": "cases"}}}), encoding="utf-8")
        if self.scenario_id != "blank":
            self._seed_sops(root)
        return root

    def _seed_sops(self, root: Path) -> None:
        """三个已启用的样例流程：让 list / preflight / run / step 马上有东西可看。"""
        habits = ("--set", "on_error=ask", "--set", "on_missing_tool=ask", "--set", "artifact_keep=ask",
                  "--set", "resume=from-failed-step", "--set", "confirm_costly=ask")
        with _sop_home(root):
            sop_call(["defaults", *habits, "--apply"], root)
            for slug in ("knowledge-digest", "weekly-review", "video-to-transcript"):
                sop_call(["new", "--slug", slug, "--template", slug, "--apply"], root)
                path = root / "sops" / slug / "SOP.md"
                path.write_text(path.read_text(encoding="utf-8").replace("【待补充】", "演示"), encoding="utf-8")
                sop_call(["decide", slug, "--confirm", "trigger", "--confirm", "requires", "--confirm", "scope", "--apply"], root)
                sop_call(["activate", slug, "--apply"], root)

    # -------------------------------------------------------------- overview

    def index_state(self) -> dict:
        if self.fault == "index-broken" and "rebuild-index" not in self.resolved:
            return {"status": "broken", "built_at": "2026-10-25T08:50:00+08:00", "file_count": 0}
        return {"status": "fresh", "built_at": DEMO_NOW, "file_count": self.data["file_count"]}

    def _group(self, item: dict) -> str:
        mapping = self.data.get("status_map") or DEFAULT_GROUPS
        return mapping.get(item["status"], "other")

    def overview(self) -> dict:
        counts: dict[str, int] = {}
        for item in self.data["items"]:
            counts[self._group(item)] = counts.get(self._group(item), 0) + 1
        levels: dict[str, int] = {}
        for finding in self.health()["findings"]:
            levels[finding["level"]] = levels.get(finding["level"], 0) + 1
        return {"name": self.data["name"], "kind": self.data["kind"], "kind_label": _("{0}项目").format(self.data["kind"]),
                "launcher": self.data["launcher"],
                "index": self.index_state(), "items": counts, "health": levels, "read_at": DEMO_NOW,
                "tools": {"declared": len(self.data["tools"]),
                          "available": sum(tool["state"] == "available" for tool in self.data["tools"])}}

    # -------------------------------------------------------------- find

    def find(self, terms: list[str], *, ext: str | None = None, container: str | None = None,
             limit: int = 10) -> dict:
        if self.index_state()["status"] == "broken":
            raise DemoError(_("资料索引损坏，暂时无法查找"), "index-broken",
                            [_("重建索引：只改用户缓存里的索引，不改项目文件（5 修复与更新 → 重建索引，或 fix rebuild-index --apply）")])
        results = []
        for asset in self.data["assets"]:
            haystack = " ".join([asset["path"], asset["title"], asset["case_title"], asset["modified"]]).lower()
            if ext and not asset["name"].lower().endswith("." + ext.lower().lstrip(".")):
                continue
            if container and container.lower() not in asset["container"].lower():
                continue
            if all(_term_hits(term, haystack) for term in terms):
                results.append(asset)
        results.sort(key=lambda asset: asset["modified"], reverse=True)
        return {"ok": True, "total": len(results), "returned": min(len(results), limit),
                "results": results[:limit], "query": {"terms": terms}, "index": self.index_state()}

    def refs(self, path: str) -> dict:
        known = self.data["refs"].get(path, {})
        return {"path": path, "inbound": known.get("inbound", []), "outbound": known.get("outbound", [])}

    def act(self, action: str, path: str) -> dict:
        verbs = {"open": _("打开"), "reveal": _("在文件管理器中显示"), "copy": _("复制路径")}
        return {"action": action, "path": path, "simulated": True,
                "message": _("【演示】将{0} {1}（未实际执行）").format(verbs.get(action, action), path)}

    # -------------------------------------------------------------- progress

    def items(self, *, status: str | None = None, term: str | None = None) -> dict:
        rows = []
        for item in self.data["items"]:
            group = self._group(item)
            if status and group != status:
                continue
            if term and not _item_hits(term, item):
                continue
            rows.append({**item, "group": group})
        return {"items": rows, "source": self.data["items_source"], "read_at": DEMO_NOW}

    def item(self, token: str) -> dict:
        exact = [item for item in self.data["items"] if token in (item["id"], *item["legacy_ids"], *item["aliases"])]
        matches = exact or [item for item in self.data["items"] if _item_hits(token, item)]
        if not matches:
            raise DemoError(_("没有找到事项：{0}").format(token), "not-found", [_("试试更短的词，或在「看进展」里浏览全部")])
        if len(matches) > 1:
            raise DemoError(_("有 {0} 个事项都匹配“{1}”").format(len(matches), token), "ambiguous",
                            [f"{item['id']}  {item['title']}" for item in matches])
        item = matches[0]
        return {**item, "group": self._group(item), "source": self.data["items_source"]}

    # -------------------------------------------------------------- guide / health

    def guide(self) -> dict:
        return {"name": self.data["name"], "kind": self.data["kind"], "about": self.data["about"],
                "launcher": self.data["launcher"], "where": self.data["where"], "start": self.data["start"],
                "examples": self.data["examples"], "capabilities": self.capabilities()}

    def capabilities(self) -> list[dict]:
        index = self.index_state()["status"]
        record = self.data["record"]
        return [
            {"id": "find", "title": _("找资料"), "state": "unavailable" if index == "broken" else "available",
             "reason": _("资料索引损坏，可重建") if index == "broken" else "", "offline": True},
            {"id": "progress", "title": _("看进展"), "state": "available", "reason": "", "offline": True},
            {"id": "guide", "title": _("项目指南"), "state": "available", "reason": "", "offline": True},
            {"id": "health", "title": _("检查项目与环境"), "state": "available",
             "reason": _("联网项使用缓存") if self.fault == "offline" else "", "offline": True},
            {"id": "fix", "title": _("修复与更新"), "state": "available",
             "reason": _("离线：需要联网的更新只能预览") if self.fault == "offline" else "", "offline": False},
            {"id": "record", "title": _("记录事项"), "state": "available" if record["write"] else "unavailable",
             "reason": "" if record["write"] else _("项目工具没有写接口"), "offline": True},
            {"id": "tools", "title": _("项目工具"),
             "state": "available" if self.data["tools"] else "unavailable",
             "reason": "" if self.data["tools"] else _("项目没有声明专用工具"), "offline": True},
            {"id": "sop", "title": _("流程（SOP）"), "state": "available", "offline": True,
             "reason": _("演示：在临时目录里运行，结束后自动清理")},
        ]

    def health(self) -> dict:
        findings = [finding for finding in copy.deepcopy(self.data["health"])
                    if not any(step["action"] == f"fix:{fix_id}" for step in finding["next"] for fix_id in self.resolved)]
        if self.index_state()["status"] == "broken":
            findings.insert(0, {"id": "rebuild-index", "level": "must", "area": _("资料"), "title": _("资料索引损坏"),
                                "reason": _("缓存里的索引文件无法读取（可能是上次重建被中断）"),
                                "impact": _("「找资料」暂时不可用；项目文件本身没有受影响"),
                                "next": [{"label": _("重建索引（只改用户缓存）"), "action": "fix:rebuild-index"}]})
        online = {"status": "offline", "cached_at": "2026-10-22T09:00:00+08:00"} if self.fault == "offline" \
            else {"status": "online", "cached_at": DEMO_NOW}
        return {"findings": findings, "ok": self.data["ok"], "read_at": DEMO_NOW, "update_source": online}

    # -------------------------------------------------------------- fix

    def fix_plan(self, ids: list[str], choices: dict[str, str] | None = None) -> dict:
        fixes = {key: value for key, value in self.data["fixes"].items() if key not in self.resolved}
        if self.index_state()["status"] == "broken":
            fixes["rebuild-index"] = {
                "title": _("重建资料索引"), "kind": "index", "network": False,
                "changes": [_("删除损坏的索引并按当前文件重新登记")],
                "writes": [{"path": _("~/.cache/agenthelm/assets/<项目摘要>/"), "scope": _("用户缓存")}],
                "custom": [], "breaking": False, "recovery": _("不涉及项目文件；索引可随时再次重建"),
                "verify": _("重新读取索引状态：最新，文件数与项目一致")}
        chosen = ids or sorted(fixes)
        unknown = [fix_id for fix_id in chosen if fix_id not in fixes]
        if unknown:
            raise DemoError(_("没有这些可处理项：") + _("、").join(unknown), "not-found",
                            [_("可处理：") + (_("、").join(sorted(fixes)) or _("（无）"))])
        steps = []
        for fix_id in chosen:
            step = {"id": fix_id, **copy.deepcopy(fixes[fix_id])}
            step["blocked"] = _("离线：需要联网，只能预览") if step["network"] and self.fault == "offline" else ""
            steps.append(step)
        return {"steps": steps, "simulated": True, "update_source": self.health()["update_source"]}

    def apply_fix(self, plan: dict, choices: dict[str, str]) -> dict:
        results = []
        for step in plan["steps"]:
            if step["blocked"]:
                results.append({"id": step["id"], "status": "skipped", "detail": step["blocked"]})
                continue
            kept = [item["path"] for item in step["custom"] if choices.get(item["path"], item["default"]) == "keep"]
            accepted = [item["path"] for item in step["custom"] if item["path"] not in kept]
            written = [w["path"] for w in step["writes"] if w["path"] not in kept]
            self.resolved.add(step["id"])
            results.append({"id": step["id"], "status": "simulated", "written": written, "kept": kept,
                            "accepted": accepted, "recovery": step["recovery"], "verify": step["verify"]})
        return {"results": results, "simulated": True}

    # -------------------------------------------------------------- record

    def record_preview(self, target: str | None, change: dict) -> dict:
        record = self.data["record"]
        if not record["write"]:
            raise DemoError(_("这个项目不能在本入口记录事项"), "unavailable",
                            [record["gap"], _("需要时请直接使用项目工具，或请项目维护者补充写接口")])
        if target is None:
            slug = re.sub(r"[^a-z0-9]+", "-", (change.get("slug") or "new-item").lower()).strip("-") or "new-item"
            path = f"cases/CASE-20261025-001-{slug}/CASE.md"
            lines = [_("+ 新建 {0}").format(path), f"+ title: {change.get('title', '')}", "+ status: active",
                     f"+ goal: {change.get('goal', '')}", f"+ next_action: {change.get('next_action', '')}"]
            return {"target": None, "path": path, "lines": lines, "expect_updated_at": None, "simulated": True}
        item = self.item(target)
        lines = []
        for key, label in (("status", "status"), ("next_action", "next_action"), ("waiting_for", "waiting_for")):
            if change.get(key) is not None:
                lines += [f"- {label}: {item.get(key) or ''}", f"+ {label}: {change[key]}"]
        if change.get("log"):
            lines.append(_("+ 工作记录：- 2026-10-25T09:15 {0}").format(change['log']))
        for path in change.get("add_deliverable") or []:
            lines.append(f"+ deliverables: {path}")
        if not lines:
            raise DemoError(_("没有要修改的内容"), "usage", [_("至少给出一项：记录、下一动作、等待对象或状态")])
        lines.append(f"  updated_at: {item['updated_at']} → 2026-10-25T09:15:00+08:00")
        return {"target": item["id"], "path": item["path"], "lines": lines,
                "expect_updated_at": item["updated_at"], "simulated": True}

    def record_apply(self, preview: dict) -> dict:
        if self.fault == "conflict" and preview["target"]:
            raise DemoError(_("这个事项在你预览之后被修改过，没有写入"), "conflict",
                            [_("最新修改：2026-10-25T09:14（CASE.md 的 updated_at 已变化）"),
                             _("重新打开预览，确认对方的改动后再提交")])
        return {"path": preview["path"], "simulated": True,
                "verify": _("【演示】写入后重新读取：内容与预览一致，未知字段与原有正文保留")}

    # -------------------------------------------------------------- tools

    def tools(self) -> list[dict]:
        return copy.deepcopy(self.data["tools"])

    def run_tool(self, tool_id: str) -> dict:
        tool = next((tool for tool in self.data["tools"] if tool["id"] == tool_id), None)
        if tool is None:
            raise DemoError(_("项目没有声明这个工具：{0}").format(tool_id), "not-found")
        if tool["state"] != "available":
            raise DemoError(_("“{0}”当前不可用").format(tool['title']), "unavailable", [tool["reason"]])
        return {"id": tool_id, "simulated": True,
                "message": _("【演示】将运行：{0}（未实际执行）").format(' '.join(tool['command']))}


def _term_hits(term: str, haystack: str) -> bool:
    lowered = term.lower()
    month = re.fullmatch(r"(\d{1,2})月", lowered)
    if month:
        return f"-{int(month.group(1)):02d}" in haystack
    return lowered in haystack


def _item_hits(term: str, item: dict) -> bool:
    fields = [item["id"], item["title"], item["goal"], *item["aliases"], *item["legacy_ids"]]
    return term.lower() in " ".join(fields).lower()


def describe() -> list[dict[str, Any]]:
    return [{"id": key, "name": _(value["name"]), "kind": _(value["kind"])} for key, value in SCENARIOS.items()]
