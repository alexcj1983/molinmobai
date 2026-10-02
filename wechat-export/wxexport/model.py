"""统一的数据模型。不管数据从哪个来源进来，都先转成这里的Chat / Message。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo

WEEKDAYS = "一二三四五六日"

# 公众号ID以gh_开头；下面这些是微信内置账号，归档没有意义。文件传输助手（filehelper）保留。
SYSTEM_IDS = frozenset({
    "weixin", "fmessage", "medianote", "floatbottle", "qmessage", "qqmail", "tmessage",
    "newsapp", "brandsessionholder", "brandservicesessionholder", "notifymessage",
    "opencustomerservicemsg", "notification_messages", "mphelper", "qqsafe", "voip",
    "voipinfo", "officialaccounts", "exmail_tool", "readerapp", "masssendapp", "feedsapp",
    "blogapp", "userexperience_alarm", "lbsapp", "shakeapp", "qqfriend", "voicevoipapp",
    "voiceinputapp", "cardpackage", "downloaderapp", "linkedinplugin", "facebookapp",
    "meishiapp", "googlecontact", "weibo", "chatroom_session_box",
})


@dataclass
class Chat:
    id: str                          # wxid_xxx或xxx@chatroom
    name: str                        # 显示名，备注优先
    is_group: bool
    last_time: datetime | None = None  # 最近一条消息的时间（数据源能给就给）

    @property
    def kind(self) -> str:
        return "群聊" if self.is_group else "私聊"


@dataclass
class Message:
    chat_id: str
    time: datetime                   # 一律带时区
    sender: str                      # 显示名；自己发的统一写“我”
    text: str                        # 渲染好的正文，图片、语音等用 [图片] 之类占位
    is_self: bool = False
    is_system: bool = False
    seq: int = 0
    sender_id: str = ""
    quote_sender: str = ""           # 引用回复：被引用的人
    quote_sender_id: str = ""
    quote: str = ""                  # 引用回复：被引用的内容（单行）
    details: list[str] = field(default_factory=list)  # 合并转发、群公告等展开后的条目


def is_group_id(chat_id: str) -> bool:
    return chat_id.endswith("@chatroom")


def is_noise_chat(chat_id: str) -> bool:
    return chat_id.startswith(("gh_", "@")) or chat_id in SYSTEM_IDS


def day_heading(d: date) -> str:
    return f"{d:%Y-%m-%d}（周{WEEKDAYS[d.weekday()]}）"


def one_line(text: str, limit: int = 0) -> str:
    s = " ".join(text.split())
    if limit and len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


# 年月日之间可以是 - / . 或「年月日」，月日时可以不补零（Excel另存过的CSV常见「2026/9/30 9:30」），
# 小数秒位数不限，时区写成Z、+08:00或+0800都行
_DT = re.compile(
    r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?"
    r"(?:[ T]+(\d{1,2}):(\d{1,2})(?::(\d{1,2})(?:[.,](\d+))?)?)?"
    r"\s*(Z|z|[+-]\d{2}:?\d{2})?$"
)


def _from_ts(ts: float) -> datetime | None:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_text(s: str) -> datetime | None:
    m = _DT.match(s)
    if not m:
        return None
    y, mo, d, hh, mi, ss, frac, off = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0),
                      int((frac or "0")[:6].ljust(6, "0")))
    except ValueError:
        return None
    if off:
        if off in ("Z", "z"):
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            sign = -1 if off[0] == "-" else 1
            dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[-2:]))))
    return dt


def parse_time(value, default_tz: tzinfo) -> datetime | None:
    """解析各种常见时间写法和10位、13位时间戳。不带时区的按default_tz理解，解析不了返回None。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = _from_ts(value / 1000 if value > 1e12 else value)
    else:
        s = str(value).strip()
        if s.isdigit():
            if len(s) == 10:
                dt = _from_ts(int(s))
            elif len(s) == 13:
                dt = _from_ts(int(s) / 1000)
            elif len(s) == 14:  # 20260930102300
                dt = _parse_text(f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:]}")
            else:
                return None
        else:
            dt = _parse_text(s)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    if dt.year < 2000:  # Go的零值时间是0001-01-01
        return None
    return dt
