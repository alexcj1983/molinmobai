"""测试夹具：一个模拟 chatlog 接口的本地 HTTP 服务，加一套示例数据。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from wxexport.config import load_config

# 模拟 chatlog 跑在一台 UTC+3 的电脑上，和归档用的北京时间不一致
SERVER_TZ = timezone(timedelta(hours=3))


def t(s: str) -> datetime:
    """'2026-09-30 10:23' → 服务器时区的时间。"""
    return datetime.fromisoformat(s).replace(tzinfo=SERVER_TZ)


class MockChatlog:
    def __init__(self):
        self.sessions: list[dict] = []
        self.contacts: list[dict] = []
        self.rooms: list[dict] = []
        self.messages: dict[str, list[dict]] = {}
        self.coverage = (t("2025-01-01 00:00"), t("2027-01-01 00:00"))  # 有数据库分片覆盖的时间段
        self.requests: list[tuple[str, dict]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def add(self, talker: str, when: str, seq: int, sender: str, sender_name: str, content: str = "", *,
            is_self: bool = False, mtype: int = 1, sub: int = 0, contents: dict | None = None) -> None:
        raw = {
            "seq": seq, "time": t(when).isoformat(), "talker": talker, "talkerName": "",
            "isChatRoom": talker.endswith("@chatroom"), "sender": sender, "senderName": sender_name,
            "isSelf": is_self, "type": mtype, "subType": sub, "content": content,
        }
        if contents:
            raw["contents"] = contents
        self.messages.setdefault(talker, []).append(raw)

    def chatlog_requests(self) -> list[dict]:
        return [q for p, q in self.requests if p == "/api/v1/chatlog"]

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code: int, obj) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                mock.requests.append((u.path, q))
                if u.path == "/health":
                    return self._json(200, {"status": "ok"})
                if u.path == "/api/v1/session":
                    return self._json(200, {"items": mock.sessions})
                if u.path == "/api/v1/contact":
                    return self._json(200, {"items": mock.contacts})
                if u.path == "/api/v1/chatroom":
                    return self._json(200, {"items": mock.rooms})
                if u.path == "/api/v1/chatlog":
                    a, b = q["time"].split("~")
                    start = t(a + " 00:00")
                    end = t(b + " 00:00") + timedelta(days=1)
                    lo, hi = mock.coverage
                    if end <= lo or start >= hi:
                        # 和 chatlog 一样：错误体是一个 JSON 字符串
                        return self._json(404, f"time range not found: {start} - {end}")
                    msgs = [m for m in mock.messages.get(q["talker"], [])
                            if start <= datetime.fromisoformat(m["time"]) < end]
                    msgs.sort(key=lambda m: m["seq"])
                    if q.get("limit"):
                        off = int(q.get("offset", 0))
                        msgs = msgs[off: off + int(q["limit"])]
                    return self._json(200, msgs or None)  # Go 的空切片是 null
                return self._json(404, {"error": "Not found"})

        return Handler


GROUP = "100@chatroom"
ZHANG = "wxid_zhang"


def load_sample(mock: MockChatlog) -> None:
    mock.contacts = [
        {"userName": ZHANG, "alias": "", "remark": "张总", "nickName": "Zhang", "isFriend": True},
        {"userName": "wxid_li", "alias": "", "remark": "", "nickName": "李工", "isFriend": True},
        {"userName": "wxid_family", "alias": "", "remark": "家里", "nickName": "", "isFriend": True},
    ]
    mock.rooms = [{"name": GROUP, "owner": ZHANG, "users": [], "remark": "", "nickName": "项目群A"}]
    mock.sessions = [
        {"userName": GROUP, "nOrder": 3, "nickName": "项目群A", "content": "", "nTime": t("2026-10-01 09:10").isoformat()},
        {"userName": ZHANG, "nOrder": 2, "nickName": "Zhang", "content": "", "nTime": t("2026-10-01 08:05").isoformat()},
        {"userName": "wxid_family", "nOrder": 1, "nickName": "家里", "content": "", "nTime": t("2026-09-20 20:00").isoformat()},
        {"userName": "gh_abc", "nOrder": 0, "nickName": "某公众号", "content": "", "nTime": t("2026-09-30 08:00").isoformat()},
    ]
    # 8 月 31 日 23:30（UTC+3）在北京时间是 9 月 1 日 04:30，应归到 9 月
    mock.add(GROUP, "2026-08-31 23:30", 1001, ZHANG, "张总", "下周五前把保函材料给银行，有问题打 13800138000")
    mock.add(GROUP, "2026-09-30 10:23", 1002, "wxid_li", "李工", mtype=49, sub=6,
             contents={"title": "投标保函格式.docx", "md5": "abc"})
    mock.add(GROUP, "2026-09-30 10:40", 1003, ZHANG, "张总", "这个格式业主确认过了吗", mtype=49, sub=57,
             contents={"refer": {"type": 49, "subType": 6, "sender": "wxid_li", "senderName": "李工",
                                 "content": "", "contents": {"title": "投标保函格式.docx"}}})
    mock.add(GROUP, "2026-09-30 11:00", 1004, "wxid_me", "", "确认过了，$2.5M 的额度 #2标段\n- 第二行\n1. 第三行",
             is_self=True)
    mock.add(GROUP, "2026-09-30 11:05", 1005, "", "", "张总撤回了一条消息", mtype=10000)
    mock.add(GROUP, "2026-10-01 09:00", 1006, "wxid_li", "李工", mtype=49, sub=19, contents={
        "title": "群聊的聊天记录",
        "recordInfo": {"Title": "群聊的聊天记录", "DataList": {"Count": "2", "DataItems": [
            {"DataType": "1", "SourceName": "王经理", "SourceTime": "2026-09-29 18:00", "DataDesc": "合同草稿周三前发过来"},
            {"DataType": "2", "SourceName": "王经理", "SourceTime": "2026-09-29 18:01"},
        ]}},
    })
    mock.add(GROUP, "2026-10-01 09:10", 1007, ZHANG, "张总", mtype=3, contents={"md5": "img"})
    mock.add(ZHANG, "2026-10-01 08:00", 2001, ZHANG, "张总", "明天上午十点开会")
    mock.add(ZHANG, "2026-10-01 08:05", 2002, "wxid_me", "", "好的", is_self=True)
    mock.add("wxid_family", "2026-09-20 20:00", 3001, "wxid_family", "家里", "周末回来吃饭")


@pytest.fixture
def chatlog():
    mock = MockChatlog()
    load_sample(mock)
    yield mock
    mock.close()


@pytest.fixture
def make_config(tmp_path):
    """按需覆盖配置项，写成 config.yaml 再走正式的加载流程。"""

    def make(source: dict | None = None, export: dict | None = None, ai: dict | None = None, obsidian: dict | None = None):
        vault = tmp_path / "vault"
        vault.mkdir(exist_ok=True)
        data = {
            "source": {"type": "chatlog_api", "base_url": "http://127.0.0.1:9", **(source or {})},
            "obsidian": {"vault": vault.as_posix(), "folder": "微信记录", "timezone": "Asia/Shanghai", **(obsidian or {})},
            "export": {**(export or {})},
            "ai": {**(ai or {})},
        }
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return load_config(path)

    return make


# 测试里统一的“现在”：北京时间 2026-10-02 12:00
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone(timedelta(hours=8)))
