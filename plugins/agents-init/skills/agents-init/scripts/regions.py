#!/usr/bin/env python3
"""受管区块（managed region）：工具只拥有带标记的区块，文件里其余内容归用户。

区块形如::

    <!-- agenthelm:region claude-import sha=0123456789ab -->
    @AGENTS.md
    <!-- /agenthelm:region claude-import -->

``sha`` 是区块正文的指纹。区块被改动（正文与指纹不符）视为用户编辑：升级时给出差异，由用户决定，
区块之外的内容永远不被读取以外的方式触碰。本模块只处理文本，不读写文件；落盘、备份、决定记录由调用方负责。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

ID_PATTERN = r"[a-z0-9][a-z0-9._-]{0,63}"
ID_RE = re.compile(rf"^{ID_PATTERN}$")
REGION_RE = re.compile(
    rf"<!-- agenthelm:region (?P<id>{ID_PATTERN}) sha=(?P<sha>[0-9a-f]{{12}}) -->\n"
    rf"(?P<body>.*?)"
    rf"<!-- /agenthelm:region (?P=id) -->\n?",
    re.DOTALL,
)


@dataclass(frozen=True)
class Region:
    id: str
    body: str
    recorded: str  # 标记里记录的正文指纹
    start: int
    end: int

    @property
    def digest(self) -> str:
        return digest(self.body)

    @property
    def intact(self) -> bool:
        """区块正文与标记里的指纹一致，即用户没有改过区块内部。"""
        return self.digest == self.recorded


def normalize(body: str) -> str:
    return body if body.endswith("\n") else body + "\n"


def digest(body: str) -> str:
    return hashlib.sha256(normalize(body).encode("utf-8")).hexdigest()[:12]


def render(region_id: str, body: str) -> str:
    if not ID_RE.fullmatch(region_id):
        raise ValueError(f"区块 ID 不合法：{region_id}")
    body = normalize(body)
    if "<!-- agenthelm:region" in body or "<!-- /agenthelm:region" in body:
        raise ValueError("区块正文不能包含区块标记")
    return f"<!-- agenthelm:region {region_id} sha={digest(body)} -->\n{body}<!-- /agenthelm:region {region_id} -->\n"


def find(text: str) -> list[Region]:
    return [Region(m.group("id"), m.group("body"), m.group("sha"), m.start(), m.end()) for m in REGION_RE.finditer(text)]


def get(text: str, region_id: str) -> Region | None:
    return next((region for region in find(text) if region.id == region_id), None)


def upsert(text: str, region_id: str, body: str, *, force: bool = False) -> tuple[str, str]:
    """把区块写入文本，返回 (新文本, 状态)。

    状态：``inserted`` 追加到末尾；``unchanged`` 已是这个正文；``updated`` 区块完好，换成新正文；
    ``modified`` 区块内部被用户改过，没有 ``force`` 时文本原样返回。
    """
    existing = get(text, region_id)
    rendered = render(region_id, body)
    if existing is None:
        separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        return text + separator + rendered, "inserted"
    if existing.body == normalize(body) and existing.intact:
        return text, "unchanged"
    if not existing.intact and not force:
        return text, "modified"
    return text[:existing.start] + rendered + text[existing.end:], "updated"


def remove(text: str, region_id: str) -> tuple[str, bool]:
    existing = get(text, region_id)
    if existing is None:
        return text, False
    before = text[:existing.start].rstrip("\n")
    after = text[existing.end:].lstrip("\n")
    joined = before + ("\n\n" if before and after else "\n" if before else "") + after
    return joined, True
