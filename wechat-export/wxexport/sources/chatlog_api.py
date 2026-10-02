"""chatlog兼容的本地HTTP接口（默认http://127.0.0.1:5030）。

只读取接口已经提供的数据：/api/v1/session、/contact、/chatroom、/chatlog。本工具不做任何解密，
接口服务由你自己选择和运行。

和接口打交道的几个约定（按chatlog v0.0.31的行为）：
- 不传limit时，服务端会把各个数据库分片的结果合并、按序号排好再返回，结果最完整；
  传了limit反而可能在分片之间提前截断，所以这里一律按时间窗口取、不分页。
- 时间段没有任何数据库分片覆盖时返回404「time range not found」，按“没有消息”处理。
- 日期按运行chatlog那台电脑的时区理解，所以查询时前后各放宽一天，再按精确时间筛选。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, tzinfo
from typing import Any, Iterable

from ..model import Chat, Message, is_group_id, one_line, parse_time
from . import Source, SourceError

WINDOW_MONTHS = 3        # 单次请求最多覆盖的月数，控制返回体大小
FIRST_YEAR = 2011        # 微信上线年份，全量导出最多从这里查起


class _NoData(SourceError):
    """该时间段没有数据库分片覆盖。"""


def _str(v) -> str:
    return "" if v is None else str(v).strip()


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _join(label: str, *parts: str) -> str:
    return " ".join([label, *[p for p in parts if p]])


def _add_months(dt: datetime, n: int) -> datetime:
    y, m = divmod(dt.month - 1 + n, 12)
    return dt.replace(year=dt.year + y, month=m + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


# 合并转发里每条的类型（DataType），正文为空时用来占位
_RECORD_TYPES = {"2": "[图片]", "3": "[语音]", "4": "[视频]", "5": "[链接]", "6": "[位置]",
                 "8": "[文件]", "17": "[聊天记录]", "19": "[小程序]"}
_SIMPLE_TYPES = {3: "[图片]", 34: "[语音]", 42: "[名片]", 43: "[视频]", 47: "[表情]", 50: "[语音通话]"}
_RECORD_LIMIT = 200


def record_items(info) -> list[str]:
    """把合并转发 / 笔记 / 群公告里的recordInfo展开成「发送人 时间：内容」一行一条。"""
    if not isinstance(info, dict):
        return []
    data_list = info.get("DataList")
    items = (data_list.get("DataItems") or []) if isinstance(data_list, dict) else []
    out = []
    for it in items[:_RECORD_LIMIT]:
        if not isinstance(it, dict):
            continue
        body = (_str(it.get("DataDesc")) or _str(it.get("DataTitle"))
                or _RECORD_TYPES.get(_str(it.get("DataType")), "[消息]"))
        head = " ".join(x for x in (_str(it.get("SourceName")), _str(it.get("SourceTime"))) if x)
        out.append(f"{head}：{one_line(body, 500)}" if head else one_line(body, 500))
    if len(items) > _RECORD_LIMIT:
        out.append(f"……另有{len(items) - _RECORD_LIMIT}条未展开")
    return out


def render_content(mtype: int, sub: int, content: str, contents) -> tuple[str, list[str]]:
    """消息正文渲染成纯文本，返回 (正文, 展开条目)。"""
    c = contents if isinstance(contents, dict) else {}
    title, desc, url = _str(c.get("title")), _str(c.get("desc")), _str(c.get("url"))
    if mtype == 1:
        return content, []
    if mtype in _SIMPLE_TYPES:
        return _SIMPLE_TYPES[mtype], []
    if mtype == 48:
        return _join("[位置]", _str(c.get("label")), _str(c.get("cityname"))), []
    if mtype == 10000:
        return content, []
    if mtype != 49:
        return content or f"[消息类型{mtype}]", []

    # 49是“分享”大类，按子类型细分
    if sub in (1, 4, 5):
        return _join("[链接]", title or desc, url), []
    if sub == 6:
        return _join("[文件]", title), []
    if sub == 8:
        return "[动图]", []
    if sub in (19, 24, 87):
        label = {19: "[聊天记录]", 24: "[笔记]", 87: "[群公告]"}[sub]
        items = record_items(c.get("recordInfo"))
        if not items and desc:
            items = [ln.strip() for ln in desc.splitlines() if ln.strip()]
        return _join(label, title), items
    if sub in (33, 36):
        return _join("[小程序]", title), []
    if sub == 51:
        return _join("[视频号]", title, url), []
    if sub == 57:  # 引用回复：正文就是回复的话，被引用的内容另外处理
        return content, []
    if sub == 62:
        return content or "[拍一拍]", []
    if sub == 63:
        return _join("[视频号直播]", title), []
    if sub == 92:
        return _join("[音乐]", title, url), []
    if sub == 2000:
        return content or "[转账]", []
    if sub == 2001:
        return "[红包]", []
    if sub == 2003:
        return "[红包封面]", []
    return _join("[分享]", title), []


def convert_message(raw: dict, chat_id: str | None, tz: tzinfo, self_ids: Iterable[str] = ()) -> Message | None:
    """chatlog的消息JSON → Message。时间解析不了的返回None。"""
    t = parse_time(raw.get("time"), tz)
    if t is None:
        return None
    self_ids = set(self_ids)
    mtype, sub = _int(raw.get("type")), _int(raw.get("subType"))
    content = raw.get("content") or ""
    contents = raw.get("contents") or {}
    sender_id = _str(raw.get("sender"))
    is_system = mtype == 10000
    is_self = bool(raw.get("isSelf")) or (bool(sender_id) and sender_id in self_ids)
    if is_self:
        sender = "我"
    elif is_system:
        sender = "系统消息"
    else:
        sender = _str(raw.get("senderName")) or sender_id or "未知"

    text, details = render_content(mtype, sub, content, contents)
    quote_sender = quote = ""
    refer = contents.get("refer") if isinstance(contents, dict) and mtype == 49 and sub == 57 else None
    if isinstance(refer, dict):
        r_id = _str(refer.get("sender"))
        quote_sender = "我" if refer.get("isSelf") or (r_id and r_id in self_ids) else (
            _str(refer.get("senderName")) or r_id)
        r_text, _ = render_content(_int(refer.get("type")), _int(refer.get("subType")),
                                   refer.get("content") or "", refer.get("contents") or {})
        quote = one_line(r_text, 80) or "[消息]"

    return Message(
        chat_id=chat_id or _str(raw.get("talker")),
        time=t,
        sender=sender,
        text=text.strip(),
        is_self=is_self,
        is_system=is_system,
        seq=_int(raw.get("seq")),
        sender_id=sender_id,
        quote_sender=quote_sender,
        quote=quote,
        details=details,
    )


class ChatlogAPISource(Source):
    def __init__(self, base_url: str, tz: tzinfo, timeout: float = 120.0, self_ids: Iterable[str] = ()):
        self.base_url = base_url.rstrip("/")
        self.tz = tz
        self.timeout = timeout
        self.self_ids = set(self_ids)
        # 本地服务不走系统代理：开着全局代理时，127.0.0.1的请求也可能被转发出去
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._names: dict[str, str] | None = None
        self._sessions: list[dict] | None = None
        self._data_start: datetime | None | bool = False  # False表示还没探测

    # ---- HTTP ----

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = self.base_url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        try:
            with self._opener.open(url, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace").strip()
            if e.code == 404 and "time range not found" in detail:
                raise _NoData(detail) from None
            raise SourceError(f"{path}返回HTTP {e.code}：{detail[:300] or e.reason}") from None
        except urllib.error.URLError as e:
            raise SourceError(f"连不上{self.base_url}（{e.reason}）。数据接口服务开着吗？") from None
        except (TimeoutError, ConnectionError) as e:
            raise SourceError(f"{self.base_url}没响应（{e}）。可以在配置里调大source.timeout") from None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SourceError(f"{path}返回的不是JSON，source.base_url填对了吗？") from None

    def _items(self, path: str) -> list[dict]:
        data = self._get(path, {"format": "json"})
        items = data.get("items") if isinstance(data, dict) else None
        return [x for x in (items or []) if isinstance(x, dict)]

    # ---- Source接口 ----

    def check(self) -> str:
        health = self._get("/health")
        if not isinstance(health, dict) or health.get("status") != "ok":
            raise SourceError(f"{self.base_url}/health返回异常：{health!r}")
        n = len(self._load_sessions())
        return f"数据接口正常：{self.base_url}，共{n}个会话"

    def _load_sessions(self) -> list[dict]:
        if self._sessions is None:
            self._sessions = [s for s in self._items("/api/v1/session") if _str(s.get("userName"))]
        return self._sessions

    def _load_names(self) -> dict[str, str]:
        if self._names is None:
            names: dict[str, str] = {}
            for c in self._items("/api/v1/contact"):
                uid = _str(c.get("userName"))
                if uid:
                    names[uid] = _str(c.get("remark")) or _str(c.get("nickName"))
            for r in self._items("/api/v1/chatroom"):
                rid = _str(r.get("name"))
                if rid:
                    names[rid] = _str(r.get("remark")) or _str(r.get("nickName")) or names.get(rid, "")
            self._names = names
        return self._names

    def list_chats(self) -> list[Chat]:
        names = self._load_names()
        chats = []
        for s in self._load_sessions():
            cid = _str(s.get("userName"))
            name = names.get(cid) or _str(s.get("nickName")) or cid
            chats.append(Chat(cid, name, is_group_id(cid), parse_time(s.get("nTime"), self.tz)))
        return chats

    def chat_by_id(self, chat_id: str) -> Chat | None:
        return Chat(chat_id, self._load_names().get(chat_id) or chat_id, is_group_id(chat_id))

    def data_start(self) -> datetime | None:
        """逐年探测第一个有数据分片的年份。分片覆盖与会话无关，探测一次全程复用。"""
        if self._data_start is not False:
            return self._data_start
        sessions = self._load_sessions()
        result = None
        if sessions:
            probe = _str(sessions[0].get("userName"))
            now = datetime.now(self.tz)
            for year in range(FIRST_YEAR, now.year + 1):
                try:
                    self._get("/api/v1/chatlog", {"time": f"{year}-01-01~{year}-12-31", "talker": probe,
                                                  "format": "json", "limit": 1})
                except _NoData:
                    continue
                result = datetime(year, 1, 1, tzinfo=self.tz)
                break
        self._data_start = result
        return result

    def fetch(self, chat: Chat, start: datetime, end: datetime) -> list[Message]:
        out: list[Message] = []
        seen: set = set()
        win_start = start
        while win_start < end:
            win_end = min(_add_months(win_start.astimezone(self.tz), WINDOW_MONTHS), end)
            q_from = (win_start - timedelta(days=1)).astimezone(self.tz).date()
            q_to = (win_end + timedelta(days=1)).astimezone(self.tz).date()
            try:
                raw_list = self._get("/api/v1/chatlog", {
                    "time": f"{q_from:%Y-%m-%d}~{q_to:%Y-%m-%d}",
                    "talker": chat.id,
                    "format": "json",
                })
            except _NoData:
                raw_list = []
            # Go的空切片会序列化成null
            for raw in raw_list or []:
                if not isinstance(raw, dict):
                    continue
                msg = convert_message(raw, chat.id, self.tz, self.self_ids)
                if msg is None or not (start <= msg.time < end):
                    continue
                key = msg.seq or (msg.time, msg.sender_id, msg.text)
                if key in seen:  # 相邻窗口放宽的那一天会重复取到
                    continue
                seen.add(key)
                out.append(msg)
            win_start = win_end
        out.sort(key=lambda m: (m.time, m.seq))
        return out
