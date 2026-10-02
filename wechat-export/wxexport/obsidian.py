"""写入Obsidian库：会话目录、按月记录、会话页、总索引。

目录结构（都在obsidian.vault / obsidian.folder下）：
    微信记录索引.md
    聊天/群聊/<会话名>/<会话名>.md             会话页：自动区 + 你自己的批注
    聊天/群聊/<会话名>/<会话名>（2026-09）.md   按月记录：整页自动生成
    聊天/私聊/...
    行动项/2026-10-02行动项.md
"""

from __future__ import annotations

import json
import re
from datetime import datetime, tzinfo
from itertools import groupby
from pathlib import Path, PurePosixPath

from .fsutil import read_text, write_if_changed
from .model import Chat, Message, day_heading, parse_time
from .state import State

BLOCK_BEGIN = "<!-- wxexport:begin（自动生成，导出时会刷新这一段） -->"
BLOCK_END = "<!-- wxexport:end -->"
_BLOCK_RE = re.compile(r"<!-- wxexport:begin.*?-->.*?<!-- wxexport:end -->", re.S)
_OWNER_RE = re.compile(r'^wechat_id:\s*"?([^"\n]*?)"?\s*$', re.M)
ROOT_INDEX = "微信记录索引"

# ---- 文件名 ----

# Windows文件名和Obsidian链接不认的字符，换成对应的全角字符，读起来不别扭
_FULLWIDTH = str.maketrans({
    "<": "＜", ">": "＞", ":": "：", '"': "＂", "/": "／", "\\": "＼", "|": "｜",
    "?": "？", "*": "＊", "#": "＃", "^": "＾", "[": "［", "]": "］",
})
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str, max_len: int = 60) -> str:
    s = "".join(ch for ch in name.translate(_FULLWIDTH) if ch.isprintable())
    s = re.sub(r"\s+", " ", s).strip(" .")  # Windows不允许末尾的点和空格；开头的点会被Obsidian当隐藏文件
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    if not s:
        s = "未命名"
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    return s


def link_alias(text: str) -> str:
    """wiki链接里 | 后面的显示文字不能带 | 和方括号。"""
    return text.replace("|", "｜").replace("[", "［").replace("]", "］")


def _yaml(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # JSON字符串本身就是合法的YAML双引号字符串


# ---- Markdown转义 ----
# 聊天内容里的 $ 会被当成公式、# 会变成标签、%% 会把后文变成注释，都要转义；
# 行首的 > - + 1. 之类会变成引用和列表，也要处理。

_TAG_START = re.compile(r"<(?=[A-Za-z/!?])")
_BLOCK_START = re.compile(r"^[>+=|~-]")
_ORDERED = re.compile(r"^(\d{1,9})(?=[.)])")


def escape_inline(s: str) -> str:
    s = s.replace("\\", "\\\\")
    for ch in "`*$#":
        s = s.replace(ch, "\\" + ch)
    s = s.replace("[[", "\\[\\[").replace("]]", "\\]\\]").replace("%%", "\\%\\%")
    return _TAG_START.sub(r"\\<", s)


def escape_line(line: str) -> str:
    s = escape_inline(line.strip())  # 去掉行首空白，免得被当成代码块
    if _BLOCK_START.match(s):
        return "\\" + s
    m = _ORDERED.match(s)
    if m:
        return m.group(1) + "\\" + s[len(m.group(1)):]
    return s


def escape_block(text: str) -> list[str]:
    return [escape_line(ln) for ln in text.splitlines() if ln.strip()]


# ---- 渲染 ----


def render_message(m: Message, tz: tzinfo) -> list[str]:
    hhmm = m.time.astimezone(tz).strftime("%H:%M")
    body = escape_block(m.text)
    if m.is_system:
        return [f"*{hhmm} {' '.join(body) or '系统消息'}*"]
    lines = [f"**{hhmm} {escape_inline(m.sender)}**：" + (body[0] if body else "")]
    lines += body[1:]
    # 引用和合并转发放在正文下面，和微信界面一致；引用块可以直接接在段落后面
    if m.quote or m.quote_sender:
        who = f" {escape_inline(m.quote_sender)}" if m.quote_sender else ""
        lines.append(f"> 引用{who}：{escape_line(m.quote)}")
    lines += [f"> {escape_line(d)}" for d in m.details]
    return lines


def month_note_name(title: str, month: str) -> str:
    return f"{title}（{month}）"


def render_month(chat: Chat, title: str, month: str, msgs: list[Message], tz: tzinfo) -> str:
    lines = [
        "---",
        f"wechat_chat: {_yaml(chat.name)}",
        f"wechat_id: {_yaml(chat.id)}",
        f"type: {chat.kind}",
        f"month: {_yaml(month)}",
        f"messages: {len(msgs)}",
        "tags: [微信记录]",
        "---",
        "",
        f"> 本页由wxexport生成，重新导出会整页覆盖。批注请写在[[{title}]]。",
    ]
    for day, items in groupby(msgs, key=lambda m: m.time.astimezone(tz).date()):
        lines += ["", f"## {day_heading(day)}"]
        for m in items:
            lines.append("")
            lines += render_message(m, tz)
    return "\n".join(lines) + "\n"


# ---- 写入 ----


class VaultWriter:
    def __init__(self, root: Path, tz: tzinfo, state: State):
        self.root = root
        self.tz = tz
        self.state = state

    def _fmt(self, iso: str | None) -> str:
        dt = parse_time(iso, self.tz)
        return dt.astimezone(self.tz).strftime("%Y-%m-%d %H:%M") if dt else ""

    def folder_rel(self, chat: Chat) -> str:
        """会话目录（相对归档根目录）。第一次分配后就固定下来，备注改名也不动，保证链接不断。"""
        st = self.state.chat(chat.id)
        if not st.get("folder"):
            st["folder"] = self._assign_folder(chat)
        st["name"] = chat.name
        st["is_group"] = chat.is_group
        return st["folder"]

    def _assign_folder(self, chat: Chat) -> str:
        base = f"聊天/{chat.kind}/{safe_name(chat.name)}"
        taken = {v.get("folder") for k, v in self.state.chats.items() if k != chat.id}
        tail = re.sub(r"@chatroom$", "", chat.id)[-4:]
        for i in range(100):
            rel = base if i == 0 else f"{base}（{tail}{'' if i == 1 else i}）"
            if rel in taken:
                continue
            owner = self._note_owner(self.root / rel / f"{PurePosixPath(rel).name}.md")
            if owner is None or owner == chat.id:  # 不存在，或者本来就是这个会话的（进度文件丢了的情况）
                return rel
        raise RuntimeError(f"会话目录重名太多：{base}")

    @staticmethod
    def _note_owner(note: Path) -> str | None:
        if not note.exists():
            return None
        try:
            head = read_text(note)[:2000]
        except OSError:
            return ""
        m = _OWNER_RE.search(head)
        return m.group(1) if m else ""  # 存在但不是本工具建的：当成别人的，不占用

    def note_title(self, chat: Chat) -> str:
        return PurePosixPath(self.folder_rel(chat)).name

    def chat_dir(self, chat: Chat) -> Path:
        return self.root / self.folder_rel(chat)

    def write_month(self, chat: Chat, month: str, msgs: list[Message]) -> bool:
        title = self.note_title(chat)
        path = self.chat_dir(chat) / f"{month_note_name(title, month)}.md"
        changed = write_if_changed(path, render_month(chat, title, month, msgs, self.tz))
        st = self.state.chat(chat.id)
        st["months"] = sorted(set(st.get("months", [])) | {month})
        return changed

    def message_link(self, chat: Chat, m: Message) -> str:
        """指向按月记录里那一天的链接，显示「09-30 10:23 张总」。"""
        local = m.time.astimezone(self.tz)
        target = f"{month_note_name(self.note_title(chat), local.strftime('%Y-%m'))}#{day_heading(local.date())}"
        return f"[[{target}|{link_alias(f'{local:%m-%d %H:%M} {m.sender}')}]]"

    def update_chat_note(self, chat: Chat) -> None:
        st = self.state.chat(chat.id)
        months = sorted(st.get("months", []), reverse=True)
        if not months:
            return
        title = self.note_title(chat)
        block = [BLOCK_BEGIN, f"- 类型：{chat.kind}"]
        if st.get("last_time"):
            block.append(f"- 最近一条：{self._fmt(st['last_time'])}")
        for year, ms in groupby(months, key=lambda x: x[:4]):
            links = " · ".join(f"[[{month_note_name(title, m)}|{int(m[5:])}月]]" for m in ms)
            block.append(f"- {year}：{links}")
        block.append(BLOCK_END)
        block_text = "\n".join(block)

        path = self.chat_dir(chat) / f"{title}.md"
        if path.exists():
            old = read_text(path)
            if _BLOCK_RE.search(old):
                new = _BLOCK_RE.sub(lambda _: block_text, old, count=1)
            else:  # 自动区被删了：补回到末尾，别动你写的内容
                new = old.rstrip("\n") + "\n\n" + block_text + "\n"
        else:
            new = "\n".join([
                "---",
                f"wechat_chat: {_yaml(chat.name)}",
                f"wechat_id: {_yaml(chat.id)}",
                f"type: {chat.kind}",
                "tags: [微信记录]",
                "---",
                "",
                f"# {title}",
                "",
                block_text,
                "",
                "## 批注",
                "",
                "",
            ])
        write_if_changed(path, new)

    def update_root_index(self) -> None:
        rows = []
        for st in self.state.chats.values():
            if st.get("months") and st.get("folder"):
                last = parse_time(st.get("last_time"), self.tz)
                rows.append((bool(st.get("is_group")), last, PurePosixPath(st["folder"]).name, len(st["months"])))
        if not rows:
            return
        lines = [f"# {ROOT_INDEX}", "", f"> 由wxexport生成，共{len(rows)}个会话，按最近消息排序。"]
        oldest = datetime(2000, 1, 1, tzinfo=self.tz)
        for is_group, label in ((True, "群聊"), (False, "私聊")):
            part = sorted((r for r in rows if r[0] == is_group), key=lambda r: r[1] or oldest, reverse=True)
            if not part:
                continue
            lines += ["", f"## {label}（{len(part)}）", "", "| 会话 | 最近一条 | 月份数 |", "| --- | --- | --- |"]
            for _, last, title, n in part:
                when = last.astimezone(self.tz).strftime("%Y-%m-%d %H:%M") if last else ""
                lines.append(f"| [[{title}]] | {when} | {n} |")
        write_if_changed(self.root / f"{ROOT_INDEX}.md", "\n".join(lines) + "\n")
