import json
from datetime import date, datetime
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from conftest import NOW, t
from wxexport.actions import FALLBACK_BETA, FatalAPIError, run_actions
from wxexport.cli import select_chats
from wxexport.model import day_heading
from wxexport.obsidian import VaultWriter
from wxexport.sources import open_source
from wxexport.state import State


class Block:
    def __init__(self, type, text=""):
        self.type, self.text = type, text


class Reply:
    def __init__(self, data=None, stop_reason="end_turn", blocks=None, category=None):
        self.content = blocks if blocks is not None else [Block("text", json.dumps(data, ensure_ascii=False))]
        self.stop_reason = stop_reason
        self.stop_details = SimpleNamespace(category=category) if category else None
        self.usage = SimpleNamespace(input_tokens=100, output_tokens=20)


class FakeClient:
    """模仿 client.beta.messages.stream(...) 的上下文管理器接口。"""

    def __init__(self, responder):
        self.calls = []
        self.responder = responder
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.responder(kwargs)
        if isinstance(reply, Exception):
            raise reply

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return reply

        return Stream()


def prompt_of(kwargs) -> str:
    return kwargs["messages"][0]["content"]


GROUP_RESULT = {
    "summary": "业主要求下周五前提交保函材料，格式已确认。",
    "items": [
        {"kind": "待办", "task": "把保函材料给银行", "owner": "我方", "due": "2026-09-04", "due_text": "下周五前",
         "quote": "下周五前把保函材料给银行", "source_ids": ["m1"], "confidence": "高"},
        {"kind": "承诺", "task": "王经理发合同草稿", "owner": "王经理", "due": "未明确", "due_text": "周三前",
         "quote": "合同草稿周三前发过来", "source_ids": ["m5"], "confidence": "中"},
        {"kind": "决定", "task": "保函额度定为 $2.5M", "owner": "我方", "due": "未明确", "due_text": "未明确",
         "quote": "确认过了，$2.5M 的额度", "source_ids": ["m4"], "confidence": "高"},
        {"kind": "待办", "task": "凭空出来的事", "owner": "未明确", "due": "随便", "due_text": "未明确",
         "quote": "", "source_ids": ["m99"], "confidence": "高"},
    ],
}
EMPTY = {"summary": "无实质内容", "items": []}


def default_responder(kwargs):
    return Reply(GROUP_RESULT if "会话：项目群A" in prompt_of(kwargs) else EMPTY)


def setup(cfg):
    source = open_source(cfg)
    state = State.for_root(cfg.root)
    writer = VaultWriter(cfg.root, cfg.tz, state)
    chats = select_chats(cfg, source, [], False, for_ai=True)
    return source, state, writer, chats


def run(cfg, client, **kw):
    source, state, writer, chats = setup(cfg)
    since = kw.pop("since", datetime(2026, 9, 1, tzinfo=cfg.tz))
    path = run_actions(cfg, source, state, writer, chats, since=since, client=client, now=NOW,
                       log=lambda s: None, **kw)
    return path, state


def test_actions_note(chatlog, make_config):
    cfg = make_config(source={"base_url": chatlog.url}, ai={"exclude": ["家里"]})
    client = FakeClient(default_responder)
    path, state = run(cfg, client)

    assert path == cfg.root / "行动项" / "2026-10-02行动项.md"
    assert len(client.calls) == 2  # 项目群A、张总；「家里」在 ai.exclude 里
    call = next(c for c in client.calls if "项目群A" in prompt_of(c))
    assert call["model"] == "claude-opus-5-5"
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["output_config"]["effort"] == "medium"
    assert call["output_config"]["format"]["type"] == "json_schema"
    prompt = prompt_of(call)
    assert "<chat_log>" in prompt and "[手机号]" in prompt and "13800138000" not in prompt  # 默认脱敏
    assert "撤回" not in prompt  # 系统消息不发

    note = path.read_text(encoding="utf-8")
    heading = day_heading(date(2026, 9, 1))
    assert "## [[项目群A]]" in note
    assert "要点：业主要求下周五前提交保函材料，格式已确认。" in note
    assert "- [ ] 把保函材料给银行 📅 2026-09-04" in note
    assert "    - 待办 · 责任方：我方 · 截止：下周五前（2026-09-04） · 置信度：高" in note
    assert f"[[项目群A（2026-09）#{heading}|09-01 04:30 张总]]" in note
    assert "- [ ] 王经理发合同草稿\n    - 承诺 · 责任方：王经理 · 截止：周三前 · 置信度：中" in note
    assert "**已定事项**\n\n- 保函额度定为 \\$2.5M" in note
    assert "- [ ] 凭空出来的事\n    - 待办 · 责任方：未明确 · 截止：未明确 · 置信度：低" in note
    assert "原话：未摘录（出处未核实）" in note
    assert "## [[张总]]" not in note  # 无实质内容的会话不占地方
    # 进度按会话记到处理过的最后一条消息
    assert state.chat("100@chatroom")["actions_until"] == "2026-10-01T14:10:00+08:00"
    assert state.chat("wxid_zhang")["actions_until"] == "2026-10-01T13:05:00+08:00"

    # 立刻再跑：没有新时间段
    client2 = FakeClient(default_responder)
    assert run(cfg, client2, since=None)[0] is None and client2.calls == []


def test_mask_can_be_turned_off(chatlog, make_config):
    cfg = make_config(source={"base_url": chatlog.url}, ai={"mask": False})
    client = FakeClient(default_responder)
    run(cfg, client)
    assert any("13800138000" in prompt_of(c) for c in client.calls)


def test_refusal_is_listed_and_others_continue(chatlog, make_config):
    cfg = make_config(source={"base_url": chatlog.url})

    def responder(kwargs):
        if "项目群A" in prompt_of(kwargs):
            return Reply(stop_reason="refusal", blocks=[], category="cyber")
        return Reply({"summary": "约了明天上午开会。", "items": [
            {"kind": "决定", "task": "明天上午十点开会", "owner": "张总", "due": "2026-10-02", "due_text": "明天上午十点",
             "quote": "明天上午十点开会", "source_ids": ["m1"], "confidence": "高"}]})

    path, state = run(cfg, FakeClient(responder))
    note = path.read_text(encoding="utf-8")
    assert "## 未处理\n\n- [[项目群A]]：Claude拒绝处理这段记录（cyber）" in note
    assert "## [[张总]]" in note and "- 明天上午十点开会" in note
    # 被拒的会话进度不前进，下次自动重试
    assert "actions_until" not in state.chat("100@chatroom") and "actions_until" in state.chat("wxid_zhang")


def test_text_after_fallback_block_is_used(chatlog, make_config):
    cfg = make_config(source={"base_url": chatlog.url})

    def responder(kwargs):
        good = json.dumps(GROUP_RESULT if "项目群A" in prompt_of(kwargs) else EMPTY, ensure_ascii=False)
        return Reply(blocks=[Block("text", '{"summ'), Block("fallback"), Block("text", good)])

    path, _ = run(cfg, FakeClient(responder))
    assert "把保函材料给银行" in path.read_text(encoding="utf-8")


def test_fatal_error_stops_without_advancing(chatlog, make_config):
    cfg = make_config(source={"base_url": chatlog.url}, ai={"concurrency": 1})
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    client = FakeClient(lambda kwargs: err)
    with pytest.raises(FatalAPIError, match="HTTPS_PROXY"):
        run(cfg, client)
    assert len(client.calls) == 1  # 后面的会话直接跳过，不再打 API
    assert all("actions_until" not in v for v in State.for_root(cfg.root).chats.values())


def test_long_chat_is_chunked_with_continuous_ids(chatlog, make_config):
    for i in range(3):
        chatlog.add("wxid_long", f"2026-09-2{i} 10:00", 5000 + i, "wxid_long", "老刘", f"第{i}段" + "很长" * 1250)
    chatlog.sessions.append({"userName": "wxid_long", "nickName": "老刘", "nTime": t("2026-09-22 10:00").isoformat()})
    cfg = make_config(source={"base_url": chatlog.url}, ai={"max_chars_per_request": 5000})
    seen_ids = []

    def responder(kwargs):
        prompt = prompt_of(kwargs)
        if "会话：老刘" not in prompt:
            return Reply(EMPTY)
        ref = prompt.split("<chat_log>")[1].split("[")[1].split("]")[0]
        seen_ids.append(ref)
        return Reply({"summary": "", "items": [
            {"kind": "待办", "task": f"跟进{ref}", "owner": "我", "due": "未明确", "due_text": "未明确",
             "quote": "很长", "source_ids": [ref], "confidence": "高"}]})

    path, _ = run(cfg, FakeClient(responder))
    assert seen_ids == ["m1", "m2", "m3"]
    note = path.read_text(encoding="utf-8")
    for day in ("09-20", "09-21", "09-22"):
        assert f"|{day} 15:00 老刘]]" in note


def test_prompt_only_writes_bundle_without_api(chatlog, make_config):
    cfg = make_config(source={"base_url": chatlog.url}, ai={"context": "我做海外电力工程项目"})

    def must_not_call(kwargs):
        raise AssertionError("prompt-only 不应该调用 API")

    path, state = run(cfg, FakeClient(must_not_call), prompt_only=True)
    assert path == cfg.root / "行动项" / "提示词" / "2026-10-02 1200提示词.md"
    text = path.read_text(encoding="utf-8")
    assert "关于用户：我做海外电力工程项目" in text and "输出格式：按会话分节" in text
    assert "## 会话：项目群A（群聊）" in text and "[m1]" in text and "[手机号]" in text
    assert "```text" in text
    assert state.chat("100@chatroom")["actions_until"] == "2026-10-01T14:10:00+08:00"


def test_late_synced_message_is_not_missed(chatlog, make_config):
    """电脑微信没开时跑过一次，之后才同步进来、时间戳早于上次运行的消息，也要能处理到。"""
    cfg = make_config(source={"base_url": chatlog.url})
    path, _ = run(cfg, FakeClient(default_responder))
    first = path.read_text(encoding="utf-8")
    chatlog.add("100@chatroom", "2026-10-02 06:00", 1009, "wxid_zhang", "张总", "保函今天能出吗")
    chatlog.sessions[0]["nTime"] = t("2026-10-02 06:00").isoformat()
    later = NOW.replace(hour=13)
    source, state, writer, chats = setup(cfg)
    run_actions(cfg, source, state, writer, chats, client=FakeClient(default_responder), now=later, log=lambda s: None)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(first.rstrip("\n")) and "**补充 · 13:00**（范围10-01 14:10至10-02 13:00）" in text
