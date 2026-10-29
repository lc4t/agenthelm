"""运行时文案的多语言：源语言是中文，``_("中文原文")`` 在中文下原样返回，在英文下查 ``locale/en.json``。

语言按这个顺序决定：调用方指定（如 ``nudge.language``）→ 环境变量 ``AGENTHELM_LANG`` → 用户配置
``~/.config/agenthelm/config.json`` 的 ``language`` → 系统 locale（``LC_ALL``、``LC_MESSAGES``、``LANG``）→ 英文。
``auto``、空值和 C/POSIX 视为未指定；中文 locale 用中文，其他语言的 locale 暂用英文。
没有英文译文的文案原样输出（中文），测试守住译文完整。四个插件各带一份相同的本模块（模块名不同，以免
同一进程里互相覆盖），``tests/test_i18n_parity.py`` 守住它们行为一致。模块级常量里的文案用 ``N_`` 标记
（原样返回，仅供提取），使用处再调用 ``_``，这样语言在运行时而不是导入时决定。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

LANGUAGES = ("zh", "en")
DEFAULT_LANGUAGE = "en"
LOCALE_DIR = Path(__file__).resolve().parent / "locale"
_catalogs: dict[str, dict[str, str]] = {}
_configured: dict[str, tuple[tuple, str | None]] = {}


def normalize(value: object) -> str | None:
    text = str(value or "").strip().lower().replace("_", "-")
    if not text or text in ("auto", "c", "posix") or text.startswith(("c.", "posix.")):
        return None
    return "zh" if text.startswith("zh") else "en"


def configured_language(environ: Mapping[str, str]) -> str | None:
    """用户配置里的 language；按文件修改时间缓存，读不了就当没有。"""
    base = environ.get("XDG_CONFIG_HOME") or (str(Path(environ["HOME"]) / ".config") if environ.get("HOME") else "")
    if not base:
        return None
    path = Path(base) / "agenthelm" / "config.json"
    try:
        info = path.stat()
        signature = (info.st_mtime_ns, info.st_size)
        cached = _configured.get(str(path))
        if cached and cached[0] == signature:
            return cached[1]
        data = json.loads(path.read_text(encoding="utf-8"))
        value = normalize(data.get("language")) if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
    _configured[str(path)] = (signature, value)
    return value


def resolve_language(requested: str | None = None, environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    for candidate in (requested, environ.get("AGENTHELM_LANG"), configured_language(environ),
                      environ.get("LC_ALL"), environ.get("LC_MESSAGES"), environ.get("LANG")):
        language = normalize(candidate)
        if language:
            return language
    return DEFAULT_LANGUAGE


def catalog(language: str) -> dict[str, str]:
    if language not in _catalogs:
        try:
            data = json.loads((LOCALE_DIR / f"{language}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _catalogs[language] = data if isinstance(data, dict) else {}
    return _catalogs[language]


def _(message: str) -> str:
    language = resolve_language()
    return message if language == "zh" else catalog(language).get(message, message)


def N_(message: str) -> str:
    return message
