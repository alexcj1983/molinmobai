"""会话名单匹配，以及发给Claude之前的脱敏。"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase

from .model import Chat


def matches(patterns: list[str], chat: Chat) -> bool:
    """名单里每一项可以是ID、显示名，或带 * ? 的通配（如「*项目群*」）。"""
    return any(fnmatchcase(chat.id, p) or fnmatchcase(chat.name, p) for p in patterns)


# 顺序有讲究：身份证号先于银行卡号，国内手机号先于国际号码
_RULES = [
    (re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])"), "[身份证号]"),
    (re.compile(r"(?<!\d)\d{4}(?:[ -]?\d{4}){3}(?:[ -]?\d{1,3})?(?!\d)"), "[卡号]"),
    (re.compile(r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d(?:[ -]?\d{4}){2}(?!\d)"), "[手机号]"),
    (re.compile(r"(?<![\w+])\+\d{1,3}(?:[ -]?\d){7,13}(?!\d)"), "[电话]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[邮箱]"),
]


def mask(text: str) -> str:
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text
