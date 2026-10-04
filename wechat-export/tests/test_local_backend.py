"""行动项的本地后端（Ollama）和prompt默认后端。"""

import json
from datetime import datetime

import pytest

from conftest import GROUP, NOW, ZHANG
from wxexport.actions import LocalExtractor, make_extractor, run_actions
from wxexport.cli import main, select_chats
from wxexport.obsidian import VaultWriter
from wxexport.sources import open_source
from wxexport.state import State

GOOD = json.dumps({"summary": "业主要下周五前的保函材料。", "items": [
    {"kind": "待办", "task": "把保函材料给银行", "owner": "我方", "due": "2026-09-04", "due_text": "下周五前",
     "quote": "下周五前把保函材料给银行", "source_ids": ["m1"], "confidence": "高"}]}, ensure_ascii=False)


def run_local(cfg):
    source = open_source(cfg)
    state = State.for_root(cfg.root)
    writer = VaultWriter(cfg.root, cfg.tz, state)
    chats = select_chats(cfg, source, [], False, for_ai=True)
    result = run_actions(cfg, source, state, writer, chats,
                         since=datetime(2026, 9, 1, tzinfo=cfg.tz), now=NOW, log=lambda s: None)
    return result, state


def test_local_backend_extracts_and_stays_on_box(chatlog, ollama, make_config):
    ollama.content = lambda body: GOOD if "项目群A" in body["messages"][1]["content"] else '{"summary":"无实质内容","items":[]}'
    cfg = make_config(source={"base_url": chatlog.url},
                      ai={"backend": "local", "local_url": ollama.url, "local_model": "qwen2.5:7b",
                          "exclude": ["家里"]})
    result, state = run_local(cfg)
    assert result.path and result.failed == 0
    note = result.path.read_text(encoding="utf-8")
    assert "- [ ] 把保函材料给银行 📅 2026-09-04" in note
    # 发给本地模型的也脱敏了
    sent = " ".join(json.dumps(b, ensure_ascii=False) for b in ollama.chat_requests)
    assert "13800138000" not in sent and "[手机号]" in sent
    # 请求的是结构化输出 + 本机地址
    assert ollama.chat_requests[0]["format"]["type"] == "object"
    assert state.chat(GROUP)["actions_until"] == "2026-10-01T14:10:00+08:00"


def test_local_falls_back_to_plain_json_format():
    # 老版本Ollama不认JSON Schema：第一次对dict格式返回400，应自动退回format:"json"
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    got = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got.append(body.get("format"))
            if isinstance(body.get("format"), dict):
                msg = b'{"error":"invalid format"}'
                self.send_response(400)
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            out = json.dumps({"message": {"content": GOOD}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ext = LocalExtractor(f"http://127.0.0.1:{srv.server_address[1]}", "qwen2.5:7b")
        data = ext.extract("会话：项目群A\n<chat_log>\n[m1] 测试\n</chat_log>")
        assert data["items"][0]["task"] == "把保函材料给银行"
        assert isinstance(got[0], dict) and got[1] == "json"  # 先schema后退回json
    finally:
        srv.shutdown()
        srv.server_close()


def test_local_offline_is_fatal():
    from wxexport.actions import FatalAPIError

    off = LocalExtractor("http://127.0.0.1:9", "qwen2.5:7b", timeout=2)
    with pytest.raises(FatalAPIError, match="连不上本地模型"):
        off.extract("x")


def test_local_missing_model_is_fatal():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from wxexport.actions import FatalAPIError

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            msg = b'{"error":"model \\"llama3:70b\\" not found, try pulling it first"}'
            self.send_response(404)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ext = LocalExtractor(f"http://127.0.0.1:{srv.server_address[1]}", "llama3:70b", timeout=5)
        with pytest.raises(FatalAPIError, match="ollama pull llama3:70b"):
            ext.extract("x")
    finally:
        srv.shutdown()
        srv.server_close()


def test_local_num_ctx_shrinks_chunk_size(make_config, chatlog):
    cfg = make_config(source={"base_url": chatlog.url},
                      ai={"backend": "local", "local_url": "http://127.0.0.1:11434",
                          "local_model": "m", "local_num_ctx": 4096, "max_chars_per_request": 150000})
    ext = make_extractor(cfg)
    assert ext.max_chars == int(4096 * 1.5)  # 本地按num_ctx压小，不用150000


def test_prompt_is_the_default_backend(chatlog, make_config):
    cfg = make_config(source={"base_url": chatlog.url})
    assert cfg.ai.backend == "prompt"
    result, _ = run_local(cfg)
    assert result.path and "提示词" in str(result.path)  # 默认不调模型，出提示词文件


def test_doctor_reports_backends(chatlog, ollama, make_config, capsys):
    base = {"source": {"base_url": chatlog.url}}
    # prompt
    p = make_config(**base).path
    main(["-c", str(p), "doctor"])
    assert "prompt——只生成提示词" in capsys.readouterr().out
    # local 就绪
    p = make_config(ai={"backend": "local", "local_url": ollama.url, "local_model": "qwen2.5:7b"}, **base).path
    assert main(["-c", str(p), "doctor"]) == 0
    assert "qwen2.5:7b已就绪" in capsys.readouterr().out
    # local 模型没下载
    p = make_config(ai={"backend": "local", "local_url": ollama.url, "local_model": "llama3:70b"}, **base).path
    assert main(["-c", str(p), "doctor"]) == 1
    assert "还没下载" in capsys.readouterr().out


def test_claude_backend_warns_about_data_leaving(chatlog, make_config, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    p = make_config(source={"base_url": chatlog.url}, ai={"backend": "claude"}).path
    # 没密钥时doctor对claude后端判失败
    assert main(["-c", str(p), "doctor"]) == 1
    out = capsys.readouterr().out
    assert "会把聊天内容发到境外" in out
