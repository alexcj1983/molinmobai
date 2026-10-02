"""用真实的 anthropic SDK 打一个本地模拟的 Messages API，确认请求体、beta 头和流式解析都对。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic
import pytest

from wxexport.actions import FALLBACK_BETA, MAX_TOKENS, ClaudeExtractor, FatalAPIError

RESULT = {"summary": "约了明天开会。", "items": [
    {"kind": "决定", "task": "明天上午十点开会", "owner": "张总", "due": "2026-10-03", "due_text": "明天",
     "quote": "明天上午十点开会", "source_ids": ["m1"], "confidence": "高"}]}


class MockMessagesAPI:
    def __init__(self, status=200):
        self.requests = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                mock.requests.append({"path": self.path, "beta": self.headers.get("anthropic-beta", ""),
                                      "key": self.headers.get("x-api-key", ""), "body": body})
                if status != 200:
                    payload = json.dumps({"type": "error", "error": {"type": "authentication_error",
                                                                      "message": "invalid x-api-key"}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                text = json.dumps(RESULT, ensure_ascii=False)
                events = [
                    ("message_start", {"type": "message_start", "message": {
                        "id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
                        "content": [], "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 321, "output_tokens": 1}}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0,
                                             "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                             "delta": {"type": "text_delta", "text": text[:15]}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                             "delta": {"type": "text_delta", "text": text[15:]}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                       "usage": {"output_tokens": 45}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                payload = "".join(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                                  for name, data in events).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api():
    mock = MockMessagesAPI()
    yield mock
    mock.close()


def test_request_shape_and_stream_parsing(api):
    client = anthropic.Anthropic(api_key="test-key", base_url=api.url, max_retries=0)
    ex = ClaudeExtractor("claude-opus-5-5", "medium", "我做海外电力工程项目", client)
    data = ex.extract("会话：张总（私聊）\n<chat_log>\n[m1] 10:00 张总：明天上午十点开会\n</chat_log>")
    assert data == RESULT
    assert (ex.input_tokens, ex.output_tokens) == (321, 45)

    [req] = api.requests
    assert req["path"].startswith("/v1/messages") and req["key"] == "test-key"
    assert FALLBACK_BETA in req["beta"]
    body = req["body"]
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True and body["max_tokens"] == MAX_TOKENS
    assert body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["required"] == ["summary", "items"]
    assert "关于用户：我做海外电力工程项目" in body["system"]
    assert "thinking" not in body and "temperature" not in body  # Opus 5.5 不接受这些


def test_auth_error_is_fatal():
    mock = MockMessagesAPI(status=401)
    try:
        client = anthropic.Anthropic(api_key="bad", base_url=mock.url, max_retries=0)
        with pytest.raises(FatalAPIError, match="密钥"):
            ClaudeExtractor("claude-opus-5-5", "medium", client=client).extract("x")
    finally:
        mock.close()


def test_connection_error_is_fatal():
    client = anthropic.Anthropic(api_key="k", base_url="http://127.0.0.1:9", max_retries=0)
    with pytest.raises(FatalAPIError, match="HTTPS_PROXY"):
        ClaudeExtractor("claude-opus-5-5", "medium", client=client).extract("x")
