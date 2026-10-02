import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import NOW
from wxexport.exporter import export_chats
from wxexport.obsidian import VaultWriter
from wxexport.sources import SourceError
from wxexport.sources.file_import import FileSource
from wxexport.state import State

SH = ZoneInfo("Asia/Shanghai")


def test_chatlog_csv_in_gbk(tmp_path):
    csv_text = (
        "Time,SenderName,Sender,TalkerName,Talker,Content\n"
        "2026-09-30 10:23:00,张总,wxid_zhang,项目群A,100@chatroom,明天开会\n"
        "2026-09-30 10:24:00,李工,wxid_li,项目群A,100@chatroom,![图片](http://127.0.0.1:5030/image/abc)\n"
        "2026-09-30 10:25:00,李工,wxid_li,项目群A,100@chatroom,[文件|保函.docx](http://127.0.0.1:5030/file/x)\n"
        "2026-09-30 10:26:00,我,wxid_me,项目群A,100@chatroom,[链接|招标公告](https://example.com/notice)\n"
    )
    (tmp_path / "export.csv").write_bytes(csv_text.encode("gb18030"))
    src = FileSource(str(tmp_path), SH, self_ids=["wxid_me"])
    [chat] = src.list_chats()
    assert (chat.id, chat.name, chat.is_group) == ("100@chatroom", "项目群A", True)
    msgs = src.fetch(chat, datetime(2026, 9, 1, tzinfo=SH), datetime(2026, 10, 1, tzinfo=SH))
    assert [m.text for m in msgs] == ["明天开会", "[图片]", "[文件] 保函.docx", "[链接] 招标公告 https://example.com/notice"]
    assert msgs[-1].sender == "我" and msgs[-1].is_self


def test_custom_csv_columns(tmp_path):
    (tmp_path / "老王.csv").write_text(
        "StrTime,NickName,StrContent,IsSender\n"
        "2026-09-29 18:00:00,老王,合同草稿周三前发,0\n"
        "2026-09-29 18:05:00,陈,收到,1\n",
        encoding="utf-8",
    )
    cols = {"time": "StrTime", "sender": "NickName", "content": "StrContent", "is_self": "IsSender",
            "sender_id": "", "chat": "", "chat_id": ""}
    src = FileSource(str(tmp_path / "老王.csv"), SH, cols)
    [chat] = src.list_chats()
    assert chat.name == "老王" and not chat.is_group  # 没有会话列时用文件名
    msgs = src.fetch(chat, datetime(2026, 9, 1, tzinfo=SH), datetime(2026, 10, 1, tzinfo=SH))
    assert [(m.sender, m.text) for m in msgs] == [("老王", "合同草稿周三前发"), ("我", "收到")]


def test_csv_missing_columns(tmp_path):
    (tmp_path / "x.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(SourceError, match="csv_columns"):
        FileSource(str(tmp_path), SH).list_chats()


def test_json_and_overlap_dedupe(tmp_path):
    raw = [{"seq": 1, "time": "2026-09-30T10:23:00+08:00", "talker": "wxid_zhang", "talkerName": "张总",
            "sender": "wxid_zhang", "senderName": "张总", "type": 1, "content": "你好"}]
    (tmp_path / "a.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"messages": raw}, ensure_ascii=False), encoding="utf-8")
    src = FileSource(str(tmp_path), SH)
    [chat] = src.list_chats()
    assert chat.name == "张总"
    assert len(src.fetch(chat, datetime(2026, 9, 1, tzinfo=SH), datetime(2026, 10, 1, tzinfo=SH))) == 1


def test_export_from_files(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "chat.csv").write_text(
        "Time,SenderName,Sender,TalkerName,Talker,Content\n"
        "2026-09-30 10:23:00,张总,wxid_zhang,张总,wxid_zhang,下周交保函\n", encoding="utf-8")
    src = FileSource(str(tmp_path / "in"), SH)
    root = tmp_path / "vault" / "微信记录"
    state = State.for_root(root)
    export_chats(src, VaultWriter(root, SH, state), state, src.list_chats(), tz=SH, now=NOW, log=lambda s: None)
    text = (root / "聊天" / "私聊" / "张总" / "张总（2026-09）.md").read_text(encoding="utf-8")
    assert "**10:23 张总**：下周交保函" in text
