from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import GROUP, ZHANG
from wxexport.sources import SourceError
from wxexport.sources.chatlog_api import ChatlogAPISource

SH = ZoneInfo("Asia/Shanghai")


def source(mock) -> ChatlogAPISource:
    return ChatlogAPISource(mock.url, SH, timeout=10)


def test_list_chats_prefers_remark(chatlog):
    chats = {c.id: c for c in source(chatlog).list_chats()}
    assert chats[GROUP].name == "项目群A" and chats[GROUP].is_group
    assert chats[ZHANG].name == "张总" and not chats[ZHANG].is_group  # 备注优先于昵称
    assert chats[GROUP].last_time.astimezone(SH) == datetime(2026, 10, 1, 14, 10, tzinfo=SH)


def test_fetch_month_uses_archive_timezone(chatlog):
    src = source(chatlog)
    group = next(c for c in src.list_chats() if c.id == GROUP)
    sep = src.fetch(group, datetime(2026, 9, 1, tzinfo=SH), datetime(2026, 10, 1, tzinfo=SH))
    assert [m.seq for m in sep] == [1001, 1002, 1003, 1004, 1005]
    # UTC+3 的 8 月 31 日 23:30 是北京时间 9 月 1 日 04:30
    assert sep[0].time.astimezone(SH) == datetime(2026, 9, 1, 4, 30, tzinfo=SH)
    # 不分页：chatlog 只有不带 limit 时才会合并全部分片后再排序
    assert all("limit" not in q for q in chatlog.chatlog_requests())


def test_rendering_of_special_messages(chatlog):
    src = source(chatlog)
    group = next(c for c in src.list_chats() if c.id == GROUP)
    msgs = {m.seq: m for m in src.fetch(group, datetime(2026, 9, 1, tzinfo=SH), datetime(2026, 11, 1, tzinfo=SH))}
    assert msgs[1002].text == "[文件] 投标保函格式.docx"
    assert (msgs[1003].text, msgs[1003].quote_sender, msgs[1003].quote) == (
        "这个格式业主确认过了吗", "李工", "[文件] 投标保函格式.docx")
    assert msgs[1004].sender == "我" and msgs[1004].is_self
    assert msgs[1005].is_system
    assert msgs[1006].text == "[聊天记录] 群聊的聊天记录"
    assert msgs[1006].details == ["王经理 2026-09-29 18:00：合同草稿周三前发过来", "王经理 2026-09-29 18:01：[图片]"]
    assert msgs[1007].text == "[图片]"


def test_range_without_data_is_empty_not_error(chatlog):
    src = source(chatlog)
    group = next(c for c in src.list_chats() if c.id == GROUP)
    # 2024 年没有任何分片覆盖，chatlog 回 404「time range not found」
    assert src.fetch(group, datetime(2024, 1, 1, tzinfo=SH), datetime(2024, 6, 1, tzinfo=SH)) == []


def test_null_response_is_empty(chatlog):
    src = source(chatlog)
    group = next(c for c in src.list_chats() if c.id == GROUP)
    assert src.fetch(group, datetime(2026, 3, 1, tzinfo=SH), datetime(2026, 4, 1, tzinfo=SH)) == []


def test_long_range_is_split_into_windows(chatlog):
    src = source(chatlog)
    group = next(c for c in src.list_chats() if c.id == GROUP)
    chatlog.requests.clear()
    msgs = src.fetch(group, datetime(2026, 1, 1, tzinfo=SH), datetime(2026, 12, 1, tzinfo=SH))
    assert [m.seq for m in msgs] == [1001, 1002, 1003, 1004, 1005, 1006, 1007]  # 窗口交界处不重复
    assert len(chatlog.chatlog_requests()) == 4


def test_data_start_probe(chatlog):
    # 第一个有分片的年份是2025，往前留一个月余量
    assert source(chatlog).data_start() == datetime(2024, 12, 1, tzinfo=SH)


def test_unreachable_service():
    src = ChatlogAPISource("http://127.0.0.1:9", SH, timeout=3)
    with pytest.raises(SourceError, match="连不上"):
        src.check()


def test_wrong_service_path(chatlog):
    src = ChatlogAPISource(chatlog.url + "/wrong", SH, timeout=3)
    with pytest.raises(SourceError):
        src.list_chats()


def test_truncated_response_is_a_source_error(chatlog):
    chatlog.truncate.add(GROUP)
    src = source(chatlog)
    group = next(c for c in src.list_chats() if c.id == GROUP)
    with pytest.raises(SourceError, match="中途断开"):
        src.fetch(group, datetime(2026, 9, 1, tzinfo=SH), datetime(2026, 10, 1, tzinfo=SH))


def test_quote_of_own_message_and_transfer(chatlog):
    chatlog.add(GROUP, "2026-09-30 12:00", 1011, ZHANG, "张总", "收到", mtype=49, sub=57,
                contents={"refer": {"type": 1, "sender": "wxid_me", "senderName": "陈", "content": "额度确认过了"}})
    chatlog.add(GROUP, "2026-09-30 12:05", 1012, ZHANG, "张总", "[转账|发送 ￥200.00](午饭钱)", mtype=49, sub=2000)
    src = source(chatlog)
    group = next(c for c in src.list_chats() if c.id == GROUP)
    msgs = {m.seq: m for m in src.fetch(group, datetime(2026, 9, 1, tzinfo=SH), datetime(2026, 10, 1, tzinfo=SH))}
    assert msgs[1011].quote_sender == "我"  # 从自己发过的消息认出自己的ID
    assert msgs[1012].text == "[转账] 发送 ￥200.00 备注：午饭钱"


def test_same_second_messages_without_seq_are_kept(chatlog):
    for _ in range(3):  # Mac版微信3.x没有seq，同一秒连发三张图
        chatlog.add(ZHANG, "2026-09-30 09:00", 0, ZHANG, "张总", mtype=3)
    src = source(chatlog)
    zhang = next(c for c in src.list_chats() if c.id == ZHANG)
    msgs = src.fetch(zhang, datetime(2026, 9, 1, tzinfo=SH), datetime(2026, 12, 1, tzinfo=SH))
    assert sum(1 for m in msgs if m.text == "[图片]") == 3
