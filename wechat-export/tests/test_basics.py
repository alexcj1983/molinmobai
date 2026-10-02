from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from wxexport.config import ConfigError
from wxexport.model import parse_time
from wxexport.obsidian import escape_line, safe_name
from wxexport.privacy import mask

SH = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize("value, expected", [
    ("2026-09-30T10:23:00+03:00", datetime(2026, 9, 30, 10, 23, tzinfo=timezone(timedelta(hours=3)))),
    ("2026-09-30T07:23:00.123456789Z", datetime(2026, 9, 30, 7, 23, 0, 123456, tzinfo=timezone.utc)),
    ("2026-09-30 10:23", datetime(2026, 9, 30, 10, 23, tzinfo=SH)),
    ("2026/09/30 10:23:05", datetime(2026, 9, 30, 10, 23, 5, tzinfo=SH)),
    ("1759199000", datetime.fromtimestamp(1759199000, tz=timezone.utc)),
    ("1759199000000", datetime.fromtimestamp(1759199000, tz=timezone.utc)),
])
def test_parse_time(value, expected):
    assert parse_time(value, SH) == expected


@pytest.mark.parametrize("value", ["", None, "0001-01-01T00:00:00Z", "不是时间"])
def test_parse_time_rejects(value):
    assert parse_time(value, SH) is None


@pytest.mark.parametrize("name, expected", [
    ("项目群A", "项目群A"),
    ('a/b:c*?"<>|#^[x]', "a／b：c＊？＂＜＞｜＃＾［x］"),
    ("  .隐藏.  ", "隐藏"),
    ("CON", "_CON"),
    ("", "未命名"),
    ("x" * 80, "x" * 60),
    ("带\n换行\t的名字", "带换行的名字"),
])
def test_safe_name(name, expected):
    assert safe_name(name) == expected


@pytest.mark.parametrize("line, expected", [
    ("额度 $2.5M 和 $3M", r"额度 \$2.5M 和 \$3M"),
    ("#2标段", r"\#2标段"),
    ("- 列表", r"\- 列表"),
    ("1. 第一", r"1\. 第一"),
    ("> 引用", r"\> 引用"),
    ("[[链接]]", r"\[\[链接\]\]"),
    ("<b>粗</b>", r"\<b>粗\</b>"),
    ("%%注释%%", r"\%\%注释\%\%"),
    ("路径 D:\\docs", r"路径 D:\\docs"),
    ("    缩进", "缩进"),
    ("金额<100万", "金额<100万"),
])
def test_escape_line(line, expected):
    assert escape_line(line) == expected


def test_mask():
    text = "电话 13800138000、138 0013 8000、+86 13800138000、+20 100 123 4567，" \
           "身份证 11010519491231002X，卡号 6222 0212 3456 7890，邮箱 a.b@example.com，金额 128,500,000"
    out = mask(text)
    assert "13800138000" not in out and "0013" not in out
    assert out.count("[手机号]") == 3
    assert "[电话]" in out and "[身份证号]" in out and "[卡号]" in out and "[邮箱]" in out
    assert "128,500,000" in out  # 普通金额不动


def test_config_rejects_unknown_key(make_config):
    with pytest.raises(ConfigError, match="不认识"):
        make_config(export={"inculde": ["x"]})


def test_config_rejects_bad_effort(make_config):
    with pytest.raises(ConfigError, match="effort"):
        make_config(ai={"effort": "ultra"})


def test_config_since_and_lists(make_config):
    cfg = make_config(export={"since": "2026-09-15", "exclude": "家里"})
    assert str(cfg.export.since) == "2026-09-15"
    assert cfg.export.exclude == ["家里"]
    assert cfg.root.name == "微信记录"
