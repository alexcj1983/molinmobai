"""数据源。新增来源时实现Source的几个方法即可，后面的归档和行动项流程不用改。"""

from __future__ import annotations

from datetime import datetime

from ..model import Chat, Message


class SourceError(Exception):
    """数据源读不到数据：服务没开、地址不对、文件不存在等。"""


class Source:
    def check(self) -> str:
        """连通性检查，返回一句说明；有问题抛SourceError。"""
        raise NotImplementedError

    def list_chats(self) -> list[Chat]:
        raise NotImplementedError

    def chat_by_id(self, chat_id: str) -> Chat | None:
        """会话列表里没有、但用户直接给了ID时用。不支持就返回None。"""
        return None

    def data_start(self) -> datetime | None:
        """整个数据最早从什么时候开始（粗到年即可），用来省掉全量导出时的空查询。"""
        return None

    def fetch(self, chat: Chat, start: datetime, end: datetime) -> list[Message]:
        """取 [start, end) 之间的消息，按时间排好序。"""
        raise NotImplementedError


def open_source(cfg) -> Source:
    if cfg.source.type == "chatlog_api":
        from .chatlog_api import ChatlogAPISource

        return ChatlogAPISource(cfg.source.base_url, cfg.tz, cfg.source.timeout, cfg.source.self_ids)
    if cfg.source.type == "file":
        from .file_import import FileSource

        return FileSource(cfg.source.path, cfg.file_tz, cfg.source.csv_columns, cfg.source.self_ids)
    raise SourceError(f"不支持的数据源类型：{cfg.source.type}")
