from datetime import date, datetime

from conftest import GROUP, NOW, ZHANG, t
from wxexport.cli import select_chats
from wxexport.exporter import export_chats
from wxexport.model import day_heading
from wxexport.obsidian import VaultWriter
from wxexport.sources import open_source
from wxexport.state import State


def run_export(cfg, chats=None, **kw):
    source = open_source(cfg)
    state = State.for_root(cfg.root)
    writer = VaultWriter(cfg.root, cfg.tz, state)
    if chats is None:
        chats = select_chats(cfg, source, [], False, for_ai=False)
    floor = None
    if cfg.export.since:
        d = cfg.export.since
        floor = datetime(d.year, d.month, d.day, tzinfo=cfg.tz)
    results = export_chats(source, writer, state, chats, tz=cfg.tz, floor=floor, now=NOW, log=lambda s: None, **kw)
    return results, state


def read(path):
    return path.read_text(encoding="utf-8")


def test_full_export_layout_and_content(chatlog, make_config):
    cfg = make_config(source={"base_url": chatlog.url})
    results, state = run_export(cfg)
    root = cfg.root
    group_dir = root / "聊天" / "群聊" / "项目群A"
    assert (group_dir / "项目群A.md").exists()
    sep = read(group_dir / "项目群A（2026-09）.md")
    oct_ = read(group_dir / "项目群A（2026-10）.md")
    assert (root / "聊天" / "私聊" / "张总" / "张总（2026-10）.md").exists()
    assert not any("公众号" in p.name for p in root.rglob("*"))  # 公众号默认跳过

    assert f"## {day_heading(date(2026, 9, 1))}" in sep  # 时区换算后归到 9 月 1 日
    assert "**04:30 张总**：下周五前把保函材料给银行" in sep
    assert r"**16:00 我**：确认过了，\$2.5M 的额度 \#2标段" in sep
    assert "\n\\- 第二行\n1\\. 第三行" in sep
    assert "**15:40 张总**：这个格式业主确认过了吗\n> 引用 李工：[文件] 投标保函格式.docx" in sep
    assert "*16:05 张总撤回了一条消息*" in sep
    assert 'wechat_id: "100@chatroom"' in sep and "messages: 5" in sep
    assert "> 王经理 2026-09-29 18:00：合同草稿周三前发过来" in oct_

    note = read(group_dir / "项目群A.md")
    assert "[[项目群A（2026-10）|10月]] · [[项目群A（2026-09）|9月]]" in note
    assert "- 最近一条：2026-10-01 14:10" in note
    index = read(root / "微信记录索引.md")
    assert "| [[项目群A]] | 2026-10-01 14:10 | 2 |" in index
    assert state.chat(GROUP)["last_time"] == "2026-10-01T14:10:00+08:00"


def test_incremental_rewrites_only_touched_month(chatlog, make_config):
    cfg = make_config(source={"base_url": chatlog.url})
    run_export(cfg)
    group_dir = cfg.root / "聊天" / "群聊" / "项目群A"
    note = group_dir / "项目群A.md"
    note.write_text(read(note) + "我的批注：保函额度要再确认\n", encoding="utf-8")
    sep_file = group_dir / "项目群A（2026-09）.md"
    sep_mtime = sep_file.stat().st_mtime_ns

    # 没有新消息：全部跳过，什么都不写
    results, _ = run_export(cfg)
    assert all(r.skipped for r in results)

    # 来了一条 10 月的新消息
    chatlog.add(GROUP, "2026-10-02 05:00", 1008, ZHANG, "张总", "银行那边今天回复")
    chatlog.sessions[0]["nTime"] = t("2026-10-02 05:00").isoformat()
    results, _ = run_export(cfg)
    group = next(r for r in results if r.chat.id == GROUP)
    assert group.months_written == ["2026-10"] and group.messages == 3  # 10 月整月重写
    assert sep_file.stat().st_mtime_ns == sep_mtime
    assert "银行那边今天回复" in read(group_dir / "项目群A（2026-10）.md")
    assert "我的批注：保函额度要再确认" in read(note)  # 自己写的内容保留
    assert "- 最近一条：2026-10-02 10:00" in read(note)


def test_exclude_and_include(chatlog, make_config):
    cfg = make_config(source={"base_url": chatlog.url}, export={"exclude": ["家里"]})
    run_export(cfg)
    assert not (cfg.root / "聊天" / "私聊" / "家里").exists()

    cfg2 = make_config(source={"base_url": chatlog.url}, export={"include": ["*项目*"]},
                       obsidian={"folder": "另一处"})
    run_export(cfg2)
    assert (cfg2.root / "聊天" / "群聊" / "项目群A").exists()
    assert not (cfg2.root / "聊天" / "私聊").exists()


def test_floor_cuts_first_month(chatlog, make_config):
    cfg = make_config(source={"base_url": chatlog.url}, export={"since": "2026-09-15"})
    run_export(cfg)
    sep = read(cfg.root / "聊天" / "群聊" / "项目群A" / "项目群A（2026-09）.md")
    assert "下周五前" not in sep and "messages: 4" in sep


def test_same_name_chats_get_separate_folders(chatlog, make_config):
    chatlog.contacts.append({"userName": "wxid_zhang2", "remark": "张总", "nickName": ""})
    chatlog.sessions.append({"userName": "wxid_zhang2", "nickName": "", "nTime": t("2026-10-01 07:00").isoformat()})
    chatlog.add("wxid_zhang2", "2026-10-01 07:00", 4001, "wxid_zhang2", "张总", "另一个张总")
    cfg = make_config(source={"base_url": chatlog.url})
    _, state = run_export(cfg)
    assert state.chat(ZHANG)["folder"] == "聊天/私聊/张总"
    assert state.chat("wxid_zhang2")["folder"] == "聊天/私聊/张总（ang2）"  # 重名时加 ID 尾巴区分


def test_lost_state_reuses_own_folder(chatlog, make_config):
    cfg = make_config(source={"base_url": chatlog.url})
    run_export(cfg)
    (cfg.root / ".wxexport-state.json").unlink()
    _, state = run_export(cfg)
    assert state.chat(GROUP)["folder"] == "聊天/群聊/项目群A"  # 认得出是自己建的目录，不另起炉灶


def test_foreign_note_with_same_name_is_not_taken(chatlog, make_config):
    cfg = make_config(source={"base_url": chatlog.url})
    own = cfg.root / "聊天" / "群聊" / "项目群A"
    own.mkdir(parents=True)
    (own / "项目群A.md").write_text("# 我自己的笔记\n", encoding="utf-8")
    _, state = run_export(cfg)
    assert state.chat(GROUP)["folder"] != "聊天/群聊/项目群A"
    assert read(own / "项目群A.md") == "# 我自己的笔记\n"
