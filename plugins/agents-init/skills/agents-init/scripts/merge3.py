"""三方合并（基线、用户版本、新模板）：纯文本，不碰文件系统。

粒度选按行（diff3）。用 v6.6.0 / v6.7.0 / v6.8.0 的 AGENTS.md 渲染结果作基线、当前模板作新版，对基线的每一行
分别做“改写”和“其后插入”共 82 种单行用户改动，再加追加章节、改中间行、改标题下一行等场景：按行与“先按章节、
冲突的章节再按行”两种粒度的冲突块数完全相同（7 对 7）；真实模板两次发布之间只有两处改动，章节粒度没有带来收益，
却多一层实现与一种更难解释的冲突范围，所以用更简单的按行。

结果是片段列表：``Text`` 为已定的文本，``Conflict`` 为需要用户选择的一块（保留两边与基线）。
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field

MARK_MINE = "<<<<<<< "
MARK_BASE = "||||||| "
MARK_SPLIT = "======="
MARK_THEIRS = ">>>>>>> "


@dataclass
class Text:
    lines: list[str]


@dataclass
class Conflict:
    base: list[str]
    mine: list[str]
    theirs: list[str]
    number: int = 0


@dataclass
class Merged:
    parts: list[Text | Conflict] = field(default_factory=list)
    auto_hunks: int = 0  # 两边都动过但自动合并成功的块数（含只有一边改动的块不计）

    @property
    def conflicts(self) -> list[Conflict]:
        return [part for part in self.parts if isinstance(part, Conflict)]

    def render(self, picks: dict[int, str] | None = None, labels: tuple[str, str, str] = ("mine", "base", "theirs")) -> str:
        """合并后的文本；``picks`` 把冲突块编号映射到 mine / theirs / both，未选的块以标记保留两边。"""
        picks = picks or {}
        out: list[str] = []
        for part in self.parts:
            if isinstance(part, Text):
                out += part.lines
                continue
            choice = picks.get(part.number)
            if choice == "mine":
                out += part.mine
            elif choice == "theirs":
                out += part.theirs
            elif choice == "both":
                out += part.mine + part.theirs
            else:
                out += [_line(MARK_MINE + labels[0]), *part.mine, _line(MARK_BASE + labels[1]), *part.base,
                        _line(MARK_SPLIT), *part.theirs, _line(MARK_THEIRS + labels[2])]
        return "".join(out)

    def unresolved(self, picks: dict[int, str] | None = None) -> list[Conflict]:
        picks = picks or {}
        return [item for item in self.conflicts if picks.get(item.number) not in ("mine", "theirs", "both")]


def _line(text: str) -> str:
    return text + "\n"


def _changes(base: list, other: list) -> list[tuple[int, int, list]]:
    matcher = difflib.SequenceMatcher(None, base, other, autojunk=False)
    return [(i1, i2, other[j1:j2]) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"]


def _apply(base: list, start: int, end: int, changes: list[tuple[int, int, list]]) -> list:
    out, position = [], start
    for i1, i2, replacement in changes:
        out += base[position:i1] + replacement
        position = i2
    return out + base[position:end]


def diff3(base: list, mine: list, theirs: list):
    """三方合并通用实现：产出 ``("same", items)`` / ``("both", items)`` / ``("conflict", base, mine, theirs)``。

    两边的改动区间相交或首尾相接即视为同一组（与 git 一致，宁可多报冲突也不拼错）；一组里只有一边改动时直接采用，
    两边改成同样内容时算一致。
    """
    left, right = _changes(base, mine), _changes(base, theirs)
    out, position = [], 0
    while left or right:
        first = min((side[0] for side in (left, right) if side), key=lambda change: change[0])
        start = first[0]
        if start > position:
            out.append(("same", base[position:start]))
        end = max(first[1], start)
        group_l: list = []
        group_r: list = []
        grew = True
        while grew:
            grew = False
            for side, group in ((left, group_l), (right, group_r)):
                while side and side[0][0] <= end:
                    change = side.pop(0)
                    group.append(change)
                    end = max(end, change[1])
                    grew = True
        position = end
        if group_l and not group_r:
            out.append(("one", _apply(base, start, end, group_l)))
        elif group_r and not group_l:
            out.append(("one", _apply(base, start, end, group_r)))
        else:
            a, b = _apply(base, start, end, group_l), _apply(base, start, end, group_r)
            out.append(("both", a) if a == b else ("conflict", base[start:end], a, b))
    if position < len(base):
        out.append(("same", base[position:]))
    return out


def _flatten(items) -> list[str]:
    return [line for item in items for line in (item if isinstance(item, list) else [item])]


def merge_lines(base: list[str], mine: list[str], theirs: list[str]) -> Merged:
    result = Merged()
    for kind, *payload in diff3(base, mine, theirs):
        if kind == "conflict":
            result.parts.append(Conflict(*payload))
            continue
        if kind == "both":
            result.auto_hunks += 1
        result.parts.append(Text(payload[0]))
    return result


def merge_text(base: str, mine: str, theirs: str) -> Merged:
    """合并三份文本；冲突块按出现顺序编号 1、2、3…"""
    b, m, t = (text.splitlines(keepends=True) for text in (base, mine, theirs))
    # 末行没有换行符时补齐，避免“最后一行有无换行”被当成改动。
    for lines in (b, m, t):
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
    merged = merge_lines(b, m, t)
    for number, conflict in enumerate(merged.conflicts, 1):
        conflict.number = number
    return merged
