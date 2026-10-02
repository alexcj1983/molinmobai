from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from wxexport.config import ConfigError
from wxexport.model import parse_time
from wxexport.obsidian import escape_line, safe_name
from wxexport.model import Chat
from wxexport.privacy import mask, matches

SH = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize("value, expected", [
    ("2026-09-30T10:23:00+03:00", datetime(2026, 9, 30, 10, 23, tzinfo=timezone(timedelta(hours=3)))),
    ("2026-09-30T07:23:00.123456789Z", datetime(2026, 9, 30, 7, 23, 0, 123456, tzinfo=timezone.utc)),
    ("2026-09-30 10:23", datetime(2026, 9, 30, 10, 23, tzinfo=SH)),
    ("2026/09/30 10:23:05", datetime(2026, 9, 30, 10, 23, 5, tzinfo=SH)),
    ("1759199000", datetime.fromtimestamp(1759199000, tz=timezone.utc)),
    ("1759199000000", datetime.fromtimestamp(1759199000, tz=timezone.utc)),
    ("2026/9/3 9:05", datetime(2026, 9, 3, 9, 5, tzinfo=SH)),           # Excel另存过的CSV
    ("2026-09-30 10:23:00 +0800", datetime(2026, 9, 30, 10, 23, tzinfo=timezone(timedelta(hours=8)))),
    ("2026年9月30日 10:23", datetime(2026, 9, 30, 10, 23, tzinfo=SH)),
    ("20260930102300", datetime(2026, 9, 30, 10, 23, tzinfo=SH)),
])
def test_parse_time(value, expected):
    assert parse_time(value, SH) == expected


@pytest.mark.parametrize("value", ["", None, "0001-01-01T00:00:00Z", "不是时间", "123456", "2026-02-30 10:00"])
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
    ("文章 https://mp.weixin.qq.com/s?a=1&b=2#wechat_redirect 看下",
     "文章 https://mp.weixin.qq.com/s?a=1&b=2#wechat_redirect 看下"),   # 网址原样保留
    ("![x](http://t.cn/a.png)", r"\!\[x\]\(http://t.cn/a.png)"),     # 不去加载外部图片
    ("[转账](午饭钱)", r"[转账\]\(午饭钱)"),
    ("~~删除线~~", r"\~\~删除线\~\~"),
    ("&copy; 2026", r"\&copy; 2026"),
    (":--- | ---", r"\:--- | ---"),
    ("___", r"\___"),
])
def test_escape_line(line, expected):
    assert escape_line(line) == expected


def test_mask():
    text = "电话 13800138000、138 0013 8000、+86 13800138000、+20 100 123 4567，" \
           "身份证 11010519491231002X，卡号 6222 0212 3456 7894，邮箱 a.b@example.com，金额 128,500,000"
    out = mask(text)
    assert "13800138000" not in out and "0013" not in out
    assert out.count("[手机号]") == 3
    assert "[电话]" in out and "[身份证号]" in out and "[卡号]" in out and "[邮箱]" in out
    assert "128,500,000" in out  # 普通金额不动


@pytest.mark.parametrize("text, expected", [
    ("合同发到wang@abc.com，明天前确认", "合同发到[邮箱]，明天前确认"),         # 紧挨着的中文不能被吞掉
    ("Ahmed电话+20 100 123 4567", "Ahmed电话[电话]"),
    ("手机+201001234567", "手机[电话]"),
    ("印尼代理+62 (21) 5555 1234", "印尼代理[电话]"),
    ("埃及号01001234567", "埃及号[电话]"),
    ("印尼号0812-3456-7890", "印尼号[电话]"),
    ("身份证110105 19491231 002X", "身份证[身份证号]"),
    ("账号EG38 0019 0005 0000 0000 2631 8000 2", "账号[账号]"),
    ("Rp 15000000000", "Rp 15000000000"),                                       # 金额不当手机号
    ("IDR 18500000000.00", "IDR 18500000000.00"),
    ("报价13500000000元", "报价13500000000元"),
    ("E1100000001234567001", "E1100000001234567001"),                           # 招标编号不当卡号
    ("合同号 2025110100012345", "合同号 2025110100012345"),                      # 过不了Luhn
    ("信用代码 911101080000000000", "信用代码 911101080000000000"),               # 校验码不对，不当身份证
])
def test_mask_cases(text, expected):
    assert mask(text) == expected


def test_matching_brackets_and_case():
    chat = Chat("123@chatroom", "[内部]财务群", True)
    assert matches(["[内部]财务群"], chat)            # 方括号按普通字符
    assert matches(["*财务*"], chat)
    assert matches(["*FINANCE*"], Chat("x", "Finance Team", True))   # 不分大小写
    assert not matches(["[内]*"], Chat("y", "内部群", True))


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
