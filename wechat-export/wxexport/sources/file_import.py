"""读取其他工具导出的文件：chatlog格式的JSON，或CSV（列名可以在配置里映射）。

source.path可以是单个文件，也可以是文件夹（会读取里面所有 .json / .csv）。
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Iterable, Iterator

from ..fsutil import read_text
from ..model import Chat, Message, is_group_id, parse_time
from . import Source, SourceError
from .chatlog_api import convert_message

# 默认按chatlog导出CSV的表头；其他工具导出的CSV在source.csv_columns里改成对应列名，
# 不存在的列写空字符串。
DEFAULT_COLUMNS = {
    "time": "Time",
    "sender": "SenderName",
    "sender_id": "Sender",
    "chat": "TalkerName",
    "chat_id": "Talker",
    "content": "Content",
    "is_self": "",
}

# chatlog文本格式里的图片、文件等是指向本地服务的链接，服务一关就失效，换成占位文字
_MEDIA_LINK = re.compile(r"!\[(图片|视频|动画表情)\]\([^)]*\)")
_LABEL_LINK = re.compile(r"\[(链接|文件|小程序|视频号|音乐)\|([^\]]*)\]\(([^)]*)\)")
_LOCAL_URL = re.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?/", re.I)


def _clean_markup(text: str) -> str:
    text = _MEDIA_LINK.sub(lambda m: f"[{m.group(1)}]", text)

    def label(m: re.Match) -> str:
        url = m.group(3).strip()
        keep_url = url.startswith(("http://", "https://")) and not _LOCAL_URL.match(url)
        return " ".join(x for x in (f"[{m.group(1)}]", m.group(2).strip(), url if keep_url else "") if x)

    return _LABEL_LINK.sub(label, text)


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "y", "是", "我", "self"}


class FileSource(Source):
    def __init__(self, path: str, tz: tzinfo, columns: dict | None = None, self_ids: Iterable[str] = ()):
        self.path = Path(path).expanduser() if path else None
        self.tz = tz
        unknown = sorted(set(columns or {}) - set(DEFAULT_COLUMNS))
        if unknown:
            raise SourceError(f"source.csv_columns里有不认识的字段：{', '.join(unknown)}")
        self.columns = {**DEFAULT_COLUMNS, **{k: (v or "") for k, v in (columns or {}).items()}}
        self.self_ids = set(self_ids)
        self._messages: dict[str, list[Message]] | None = None
        self._names: dict[str, str] = {}

    def _files(self) -> list[Path]:
        if not self.path:
            raise SourceError("source.type是file时要填source.path")
        if self.path.is_file():
            return [self.path]
        if self.path.is_dir():
            return sorted(p for p in self.path.rglob("*") if p.is_file() and p.suffix.lower() in (".json", ".csv"))
        raise SourceError(f"找不到{self.path}")

    def _load(self) -> dict[str, list[Message]]:
        if self._messages is not None:
            return self._messages
        by_chat: dict[str, list[Message]] = defaultdict(list)
        seen: set = set()
        for f in self._files():
            loader = self._load_json if f.suffix.lower() == ".json" else self._load_csv
            for m in loader(f):
                key = (m.chat_id, m.time, m.sender_id or m.sender, m.text)
                if key in seen:  # 几份导出时间段有重叠时去重
                    continue
                seen.add(key)
                by_chat[m.chat_id].append(m)
        for msgs in by_chat.values():
            msgs.sort(key=lambda m: (m.time, m.seq))
        self._messages = dict(by_chat)
        return self._messages

    def _load_json(self, f: Path) -> Iterator[Message]:
        try:
            data = json.loads(read_text(f))
        except json.JSONDecodeError as e:
            raise SourceError(f"{f.name}不是合法的JSON：{e}") from None
        if isinstance(data, dict):
            data = data.get("messages") or data.get("items") or []
        if not isinstance(data, list):
            raise SourceError(f"{f.name}：没找到消息列表")
        fallback_id = "file:" + f.stem
        for raw in data:
            if not isinstance(raw, dict):
                continue
            m = convert_message(raw, None, self.tz, self.self_ids)
            if m is None:
                continue
            if not m.chat_id:
                m.chat_id = fallback_id
            self._names.setdefault(m.chat_id, str(raw.get("talkerName") or "").strip() or
                                   (f.stem if m.chat_id == fallback_id else m.chat_id))
            yield m

    def _load_csv(self, f: Path) -> Iterator[Message]:
        col = self.columns
        reader = csv.DictReader(io.StringIO(read_text(f)))
        fields = reader.fieldnames or []
        if col["time"] not in fields or col["content"] not in fields:
            raise SourceError(
                f"{f.name}：找不到时间列「{col['time']}」或内容列「{col['content']}」，"
                f"在source.csv_columns里改成实际的列名。这个文件的列：{', '.join(fields)}"
            )

        def cell(row: dict, key: str) -> str:
            name = col[key]
            return str(row.get(name) or "").strip() if name else ""

        fallback_id = "file:" + f.stem
        for row in reader:
            t = parse_time(cell(row, "time"), self.tz)
            if t is None:
                continue
            chat_name, chat_id = cell(row, "chat"), cell(row, "chat_id")
            if not chat_id:
                chat_id = ("file:" + chat_name) if chat_name else fallback_id
            self._names.setdefault(chat_id, chat_name or f.stem)
            sender, sender_id = cell(row, "sender"), cell(row, "sender_id")
            is_self = (_truthy(cell(row, "is_self")) if col["is_self"] else False) or bool(
                {sender, sender_id} & self.self_ids)
            yield Message(
                chat_id=chat_id,
                time=t,
                sender="我" if is_self else (sender or sender_id or "未知"),
                text=_clean_markup(cell(row, "content")),
                is_self=is_self,
                sender_id=sender_id,
            )

    def check(self) -> str:
        files = self._files()
        msgs = self._load()
        return f"读到{len(files)}个文件、{len(msgs)}个会话、{sum(len(v) for v in msgs.values())}条消息"

    def list_chats(self) -> list[Chat]:
        chats = []
        for cid, msgs in self._load().items():
            others = {m.sender_id or m.sender for m in msgs if not m.is_self and not m.is_system}
            chats.append(Chat(cid, self._names.get(cid) or cid, is_group_id(cid) or len(others) > 1,
                              msgs[-1].time if msgs else None))
        return chats

    def data_start(self) -> datetime | None:
        times = [msgs[0].time for msgs in self._load().values() if msgs]
        return min(times) if times else None

    def fetch(self, chat: Chat, start: datetime, end: datetime) -> list[Message]:
        return [m for m in self._load().get(chat.id, []) if start <= m.time < end]
