"""统一的数据模型。不管数据从哪个来源进来，都先转成这里的Chat / Message。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, tzinfo

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


_FRACTION = re.compile(r"\.(\d+)(?=([+-]\d{2}:?\d{2})?$)")


def parse_time(value, default_tz: tzinfo) -> datetime | None:
    """解析ISO 8601、'YYYY-MM-DD HH:MM[:SS]'、10位或13位时间戳。不带时区的按default_tz理解。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        ts = float(value)
        if ts > 1e12:  # 13位毫秒时间戳
            ts /= 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        s = str(value).strip().replace("/", "-")
        if s[-1:] in ("Z", "z"):
            s = s[:-1] + "+00:00"
        # Python 3.10的fromisoformat只认3位或6位小数秒，Go输出的位数不固定
        s = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    if dt.year < 2000:  # Go的零值时间是0001-01-01
        return None
    return dt
