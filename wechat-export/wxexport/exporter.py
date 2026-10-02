"""导出流程：选好的会话 → 按时间窗口取消息 → 按月写入Obsidian → 刷新会话页和总索引。

按月整页重写：每次只重写涉及到的月份，月份文件永远是完整的一个月，重复运行结果一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from itertools import groupby
from typing import Callable

from .model import Chat, parse_time
from .obsidian import VaultWriter
from .sources import Source, SourceError
from .state import State

FIRST_DAY = (2011, 1, 1)  # 微信上线的年份
WINDOW_MONTHS = 3         # 每次向数据源取几个月
MAX_ERRORS_IN_A_ROW = 3   # 连续几个会话出错就停


@dataclass
class ChatResult:
    chat: Chat
    messages: int = 0
    months_written: list[str] = field(default_factory=list)
    months_kept: list[str] = field(default_factory=list)  # 新取到的比归档少，没覆盖
    skipped: bool = False
    error: str = ""


def month_floor(dt: datetime, tz: tzinfo) -> datetime:
    d = dt.astimezone(tz)
    return datetime(d.year, d.month, 1, tzinfo=tz)


def add_months(dt: datetime, n: int) -> datetime:
    y, m = divmod(dt.month - 1 + n, 12)
    return dt.replace(year=dt.year + y, month=m + 1, day=1)


def export_chats(
    source: Source,
    writer: VaultWriter,
    state: State,
    chats: list[Chat],
    *,
    tz: tzinfo,
    floor: datetime | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    full: bool = False,
    force: bool = False,
    log: Callable[[str], None] = print,
    now: datetime | None = None,
) -> list[ChatResult]:
    """导出一批会话。

    floor        配置里的export.since，任何导出都不早于它
    since/until  本次手动指定的时间段（按月对齐）；不指定就按进度增量导出
    full         忽略进度，从头重导
    force        新取到的消息比归档里少时也覆盖（默认不覆盖，防止微信端删了记录导致归档缩水）
    """
    now = now or datetime.now(tz)
    end = min(until, now) if until else now
    # 涉及到的最后一个月要整月重写，所以窗口一直拉到那个月底
    win_end = add_months(month_floor(end - timedelta(microseconds=1), tz), 1)
    data_start: datetime | None = None
    results: list[ChatResult] = []
    errors_in_a_row = 0

    try:
        for i, chat in enumerate(chats, 1):
            res = ChatResult(chat)
            results.append(res)
            st = state.chat(chat.id)
            last = parse_time(st.get("last_time"), tz)

            if since is not None:
                start = since
            elif full or last is None:
                if data_start is None:  # 探测一次全程复用，省掉数据起点之前的空查询
                    data_start = source.data_start() or datetime(*FIRST_DAY, tzinfo=tz)
                start = max(floor, data_start) if floor else data_start
            elif chat.last_time is not None and chat.last_time <= last:
                res.skipped = True  # 会话列表显示没有新消息
                continue
            else:
                start = last  # 从上次导出的最后一条所在月份开始重写
            if floor is not None and start < floor:
                start = floor

            newest = last
            try:
                cur = month_floor(start, tz)
                while cur < win_end:
                    nxt = min(add_months(cur, WINDOW_MONTHS), win_end)
                    fetch_from = max(cur, floor) if floor else cur
                    if fetch_from < nxt:
                        msgs = source.fetch(chat, fetch_from, nxt)
                        for month, group in groupby(msgs, key=lambda m: m.time.astimezone(tz).strftime("%Y-%m")):
                            items = list(group)
                            status = writer.write_month(chat, month, items, force=force)
                            if status == "written":
                                res.months_written.append(month)
                            elif status == "kept":
                                res.months_kept.append(month)
                            res.messages += len(items)
                            if newest is None or items[-1].time > newest:
                                newest = items[-1].time
                    cur = nxt
                # 会话页写成功才推进进度；写失败的话下次整段重来
                old_last = st.get("last_time")
                if newest is not None:
                    st["last_time"] = newest.astimezone(tz).isoformat()
                try:
                    writer.update_chat_note(chat)
                except OSError:
                    if old_last is None:
                        st.pop("last_time", None)
                    else:
                        st["last_time"] = old_last
                    raise
                state.save()  # 每个会话存一次，中途中断也不丢进度
            except (OSError, SourceError) as e:
                # 单个会话取数或写文件出错：记下来接着做别的会话；连着几个都出错多半是服务或磁盘有问题，停下
                res.error = str(e) if isinstance(e, SourceError) else f"写文件失败：{e}"
                log(f"  [{i}/{len(chats)}] {chat.name}：{res.error}")
                errors_in_a_row += 1
                if errors_in_a_row >= MAX_ERRORS_IN_A_ROW:
                    raise SourceError(f"连续{errors_in_a_row}个会话出错，先停下。最后一个错误：{res.error}") from None
                continue
            errors_in_a_row = 0
            if res.messages:
                changed = "、".join(res.months_written) or "内容无变化"
                kept = (f"；{'、'.join(res.months_kept)}新取到的消息比归档里少，没覆盖（确认要覆盖用--force）"
                        if res.months_kept else "")
                log(f"  [{i}/{len(chats)}] {chat.name}：{res.messages}条，更新{changed}{kept}")
    finally:
        try:
            writer.update_root_index()
            state.save()
        except OSError as e:
            log(f"总索引或进度文件写入失败：{e}")
    return results
