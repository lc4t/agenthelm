"""Case 正文的结构：小节名与默认文字，按项目文件语言（Project Profile 的 ``language``）。

文件里写什么语言是项目的属性，随项目同步，不随命令行界面语言（AGENTHELM_LANG）变化：``language`` 为 ``en`` 的项目
新建的 Case 用英文小节名与默认文字，缺省或 ``zh`` 的项目保持原来的中文。读取与更新时两种小节名都认，所以
旧 Case、换过语言的项目与手写的 Case 都能照常追加记录；找不到小节时按项目语言新建。
"""
from __future__ import annotations

LANGUAGES = ("zh", "en")
SECTIONS = {
    "current_state": ("当前可信状态", "Current trusted state"),
    "next_action": ("下一动作", "Next action"),
    "decisions": ("决策记录", "Decision log"),
    "work_log": ("工作记录", "Work log"),
    "result": ("结果", "Result"),
}
# 默认文字：新建 Case 时写进去的字段占位。
TEXTS = {
    "just_created": ("刚创建", "Just created"),
    "to_define": ("待明确", "To be defined"),
}
# 模板里的空小节占位；第一次追加记录时被替换掉。
PLACEHOLDERS = ("暂无。", "无。", "待执行。", "None yet.", "None.", "Pending.")


def language_of(profile: dict) -> str:
    """The project's file language; projects that predate the field stay Chinese."""
    value = profile.get("language")
    return value if value in LANGUAGES else "zh"


def names(key: str) -> tuple[str, ...]:
    return SECTIONS[key]


def key_of(name: str) -> str:
    """The logical section for a logical key or for a heading in either language."""
    if name in SECTIONS:
        return name
    for key, variants in SECTIONS.items():
        if name in variants:
            return key
    raise KeyError(name)


def heading(key: str, language: str) -> str:
    return SECTIONS[key][LANGUAGES.index(language)]


def existing_heading(body: str, key: str) -> str | None:
    """The heading of this section as the file spells it, if present in either language."""
    for name in SECTIONS[key]:
        if f"## {name}\n" in body or body.endswith(f"## {name}"):
            return name
    return None


def resolve(body: str, name: str, language: str) -> str:
    """The heading to use for a section: the file's own spelling, else the project language's.

    A heading outside the Case vocabulary (for example the ones SOP adds) is used as written."""
    try:
        key = key_of(name)
    except KeyError:
        return name
    return existing_heading(body, key) or heading(key, language)


def text(key: str, language: str) -> str:
    return TEXTS[key][LANGUAGES.index(language)]
