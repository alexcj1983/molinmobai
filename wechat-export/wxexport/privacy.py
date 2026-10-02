"""会话名单匹配，以及发给Claude之前的脱敏。"""

from __future__ import annotations

import re
from datetime import date
from functools import lru_cache

from .model import Chat


@lru_cache(maxsize=512)
def _glob(pattern: str) -> re.Pattern:
    # 只认 * 和 ? 两种通配；方括号按普通字符处理，群名里常有「[内部]」这种写法
    parts = (".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern)
    return re.compile("".join(parts), re.S | re.I)


def is_glob(pattern: str) -> bool:
    return "*" in pattern or "?" in pattern


def name_matches(pattern: str, *values: str) -> bool:
    """完全相同（不分大小写）算匹配；带 * ? 的按通配匹配。"""
    p = pattern.casefold()
    return any(v.casefold() == p or (is_glob(pattern) and _glob(pattern).fullmatch(v)) for v in values)


def matches(patterns: list[str], chat: Chat) -> bool:
    """名单里每一项可以是ID、显示名，或带 * ? 的通配（如「*项目群*」）。"""
    return any(name_matches(p, chat.id, chat.name) for p in patterns)


# ---- 脱敏 ----
# 都要求号码前后不紧挨字母或数字；能校验的（身份证校验码、银行卡Luhn、IBAN）先校验，减少误伤合同号、金额。

_EDGE_L = r"(?<![0-9A-Za-z])"
_EDGE_R = r"(?![0-9A-Za-z])"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_IBAN = re.compile(_EDGE_L + r"[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?" + _EDGE_R)
_ID = re.compile(_EDGE_L + r"\d{6}[ -]?\d{8}[ -]?\d{3}[0-9Xx]" + _EDGE_R)
_CARD = re.compile(_EDGE_L + r"\d{4}(?:[ -]?\d{4}){3}(?:[ -]?\d{1,3})?" + _EDGE_R)
_CN_MOBILE = re.compile(_EDGE_L + r"(?:\+?86[ -]?)?1[3-9]\d(?:[ -]?\d{4}){2}" + _EDGE_R)
_INTL = re.compile(r"(?<![0-9A-Za-z+])\+\d{1,3}(?:[ .()-]*\d){7,13}" + _EDGE_R)
_EG_MOBILE = re.compile(_EDGE_L + r"01[0125](?:[ -]?\d){8}" + _EDGE_R)      # 埃及手机号本地写法
_ID_MOBILE = re.compile(_EDGE_L + r"08\d(?:[ -]?\d){7,10}" + _EDGE_R)       # 印尼手机号本地写法

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
# 金额常见的前缀（货币符号、币种）和后缀（单位），出现时那串数字按金额处理，不当手机号
_MONEY_BEFORE = re.compile(r"(?:rp|idr|egp|usd|us\$|rmb|cny|eur|le|e£|[$¥￥€£])\s*$", re.I)
_MONEY_AFTER = re.compile(r"^(?:\.\d|[元万亿块]|美元|美金|欧元|英镑|埃镑|镑|卢比|印尼盾|盾)")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _valid_id(s: str) -> bool:
    s = re.sub(r"[ -]", "", s).upper()
    try:
        born = date(int(s[6:10]), int(s[10:12]), int(s[12:14]))
    except ValueError:
        return False
    if not 1900 <= born.year <= 2100:
        return False
    total = sum(int(c) * w for c, w in zip(s[:17], _ID_WEIGHTS))
    return "10X98765432"[total % 11] == s[17]


def _luhn(s: str) -> bool:
    digits = [int(c) for c in _digits(s)][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    return total % 10 == 0


def _valid_iban(s: str) -> bool:
    s = s.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def _checked(pattern: re.Pattern, label: str, valid) -> tuple:
    return pattern, lambda m: label if valid(m.group()) else m.group()


def _phone(m: re.Match) -> str:
    text = m.string
    if _MONEY_BEFORE.search(text[max(0, m.start() - 6):m.start()]) or _MONEY_AFTER.match(text[m.end():m.end() + 4]):
        return m.group()
    return "[手机号]"


_RULES = [
    (_EMAIL, lambda m: "[邮箱]"),
    _checked(_IBAN, "[账号]", _valid_iban),
    _checked(_ID, "[身份证号]", _valid_id),
    _checked(_CARD, "[卡号]", _luhn),
    (_CN_MOBILE, _phone),
    (_INTL, lambda m: "[电话]"),
    (_EG_MOBILE, lambda m: "[电话]"),
    (_ID_MOBILE, lambda m: "[电话]"),
]


def mask(text: str) -> str:
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text
