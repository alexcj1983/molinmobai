from conftest import t
from wxexport.cli import main


def cfg_path(make_config, chatlog, **kw) -> str:
    return str(make_config(source={"base_url": chatlog.url}, **kw).path)


def test_doctor_ok(chatlog, make_config, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert main(["-c", cfg_path(make_config, chatlog), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "✓ 归档目录可写" in out and "✓ 数据接口正常" in out and "共4个会话" in out
    assert "没检测到ANTHROPIC_API_KEY" in out


def test_doctor_reports_dead_source(make_config, capsys):
    assert main(["-c", str(make_config().path), "doctor"]) == 1
    assert "✗ 数据源：连不上" in capsys.readouterr().out


def test_missing_config(tmp_path, capsys):
    assert main(["-c", str(tmp_path / "nope.yaml"), "list"]) == 1
    assert "config.example.yaml" in capsys.readouterr().err


def test_list(chatlog, make_config, capsys):
    path = cfg_path(make_config, chatlog, export={"exclude": ["家里"]})
    assert main(["-c", path, "list"]) == 0
    out = capsys.readouterr().out
    assert "群聊  2026-10-01 14:10  项目群A  [100@chatroom]" in out
    assert "家里  [wxid_family]（不导出）" in out
    assert "某公众号" not in out
    assert main(["-c", path, "list", "--all"]) == 0
    assert "某公众号" in capsys.readouterr().out


def test_export_one_chat_by_partial_name(chatlog, make_config, capsys):
    path = cfg_path(make_config, chatlog)
    assert main(["-c", path, "export", "--chat", "项目"]) == 0
    root = make_config(source={"base_url": chatlog.url}).root
    assert (root / "聊天" / "群聊" / "项目群A" / "项目群A（2026-09）.md").exists()
    assert not (root / "聊天" / "私聊").exists()


def test_ambiguous_name(chatlog, make_config, capsys):
    chatlog.contacts.append({"userName": "wxid_zg", "remark": "张工", "nickName": ""})
    chatlog.sessions.append({"userName": "wxid_zg", "nickName": "", "nTime": t("2026-09-01 10:00").isoformat()})
    assert main(["-c", cfg_path(make_config, chatlog), "export", "--chat", "张"]) == 1
    err = capsys.readouterr().err
    assert "对应多个会话" in err and "wxid_zg" in err


def test_excluded_chat_is_skipped_even_when_named(chatlog, make_config, capsys):
    path = cfg_path(make_config, chatlog, export={"exclude": ["家里"]})
    assert main(["-c", path, "export", "--chat", "家里"]) == 0
    out = capsys.readouterr().out
    assert "跳过「家里」" in out and "没有要导出的会话" in out


def test_full_with_since_is_rejected(chatlog, make_config, capsys):
    assert main(["-c", cfg_path(make_config, chatlog), "export", "--full", "--since", "2026-09-01"]) == 1
    assert "--full" in capsys.readouterr().err


def test_actions_prompt_only_and_run(chatlog, make_config, capsys):
    path = cfg_path(make_config, chatlog)
    assert main(["-c", path, "actions", "--prompt-only", "--since", "2026-09-01"]) == 0
    out = capsys.readouterr().out
    assert "已写入：" in out and "提示词" in out
    assert main(["-c", path, "run", "--prompt-only"]) == 0
    assert "① 增量导出" in capsys.readouterr().out
