"""写入Obsidian库：会话目录、按月记录、会话页、总索引。

目录结构（都在obsidian.vault / obsidian.folder下）：
    微信记录索引.md
    聊天/群聊/<会话名>/<会话名>.md             会话页：自动区 + 你自己的批注
    聊天/群聊/<会话名>/<会话名>（2026-09）.md   按月记录：整页自动生成
    聊天/私聊/...
    行动项/2026-10-02行动项.md

本工具生成的链接都带库内路径（[[微信记录/聊天/群聊/项目群A/项目群A|项目群A]]），
库里别处有同名笔记也不会串。
"""

from __future__ import annotations

import hashlib
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
# 自动区：从一个begin到最近的end，中间不能再出现begin。用户误删了end时，不会把两段之间的批注一起吞掉
_BLOCK_RE = re.compile(r"<!-- wxexport:begin[^>]*-->(?:(?!<!-- wxexport:begin).)*?<!-- wxexport:end -->", re.S)
_OWNER_RE = re.compile(r'^wechat_id:\s*"?([^"\n]*?)"?\s*$', re.M)
_COUNT_RE = re.compile(r"^messages:\s*(\d+)\s*$", re.M)
ROOT_INDEX = "微信记录索引"

# ---- 文件名 ----

# Windows文件名和Obsidian链接不认的字符，换成对应的全角字符，读起来不别扭
_FULLWIDTH = str.maketrans({
    "<": "＜", ">": "＞", ":": "：", '"': "＂", "/": "／", "\\": "＼", "|": "｜",
    "?": "？", "*": "＊", "#": "＃", "^": "＾", "[": "［", "]": "］", "%": "％",
})
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"{p}{n}" for p in ("COM", "LPT") for n in "0123456789¹²³")}


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


def _id_tail(chat_id: str) -> str:
    """重名时加在目录名后面区分用：取ID末尾4个字母数字，取不出来就用哈希。"""
    tail = re.sub(r"[^0-9A-Za-z]", "", re.sub(r"@chatroom$", "", chat_id))[-4:]
    return tail if len(tail) >= 2 else hashlib.md5(chat_id.encode("utf-8")).hexdigest()[:4]


def link_alias(text: str) -> str:
    """wiki链接里 | 后面的显示文字不能带 | 和方括号。"""
    return text.replace("|", "｜").replace("[", "［").replace("]", "］")


def _yaml(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # JSON字符串本身就是合法的YAML双引号字符串


# ---- Markdown转义 ----
# 聊天内容里的 $ 会被当成公式、# 会变成标签、%% 会把后文变成注释、![](…) 会去加载外部图片，都要转义；
# 行首的 > - + 1. 之类会变成引用和列表，也要处理。网址原样保留，转义了反而会把链接弄坏。

_URL = re.compile(r"https?://[^\s<>\"'（）【】「」《》，。；：！？、]+")
_SPECIAL = re.compile(r"\\|`|\*|\$|#|\[\[|\]\]|%%|~~|!\[|\]\(|<(?=[A-Za-z/!?])|&(?=#?\w+;)")
_BLOCK_START = re.compile(r"^[>+=|~:_-]")
_ORDERED = re.compile(r"^(\d{1,9})(?=[.)])")


def _escape_text(s: str) -> str:
    # 一遍替换，每个特殊字符前加反斜杠，不会重复转义
    return _SPECIAL.sub(lambda m: "".join("\\" + ch for ch in m.group()), s)


def escape_inline(s: str) -> str:
    s = re.sub(r"\s*[\r\n]+\s*", " ", s)  # 行内位置不能带换行，否则列表项、任务行会被拆开
    out, pos = [], 0
    for m in _URL.finditer(s):
        out += [_escape_text(s[pos:m.start()]), m.group()]
        pos = m.end()
    out.append(_escape_text(s[pos:]))
    return "".join(out)


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


def render_month(chat: Chat, month: str, msgs: list[Message], tz: tzinfo, chat_link: str) -> str:
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
        f"> 本页由wxexport生成，重新导出会整页覆盖。批注请写在{chat_link}。",
    ]
    for day, items in groupby(msgs, key=lambda m: m.time.astimezone(tz).date()):
        lines += ["", f"## {day_heading(day)}"]
        for m in items:
            lines.append("")
            lines += render_message(m, tz)
    return "\n".join(lines) + "\n"


# ---- 写入 ----


class VaultWriter:
    def __init__(self, root: Path, tz: tzinfo, state: State, link_prefix: str | None = None):
        self.root = root
        self.tz = tz
        self.state = state
        # 归档目录在库里的相对路径，用来拼带路径的链接
        self.link_prefix = (link_prefix if link_prefix is not None else root.name).strip("/")
        self._existing: dict[str, str] | None = None

    def _fmt(self, iso: str | None) -> str:
        dt = parse_time(iso, self.tz)
        return dt.astimezone(self.tz).strftime("%Y-%m-%d %H:%M") if dt else ""

    # ---- 会话目录 ----

    def folder_rel(self, chat: Chat) -> str:
        """会话目录（相对归档根目录）。第一次分配后就固定下来，备注改名也不动，保证链接不断。"""
        st = self.state.chat(chat.id)
        rel = st.get("folder")
        if rel and not (self.root / rel).is_dir() and self._scan_existing().get(chat.id):
            rel = None  # 进度里记的目录不在了，但库里能按wechat_id找到：用找到的
        if not rel:
            rel = self._scan_existing().get(chat.id) or self._assign_folder(chat)
            st["folder"] = rel
        st["name"] = chat.name
        st["is_group"] = chat.is_group
        return rel

    def _scan_existing(self) -> dict[str, str]:
        """扫一遍已有的会话页，按frontmatter里的wechat_id认回目录（进度文件丢了也能接上）。"""
        if self._existing is None:
            found: dict[str, str] = {}
            base = self.root / "聊天"
            if base.is_dir():
                for note in sorted(base.glob("*/*/*.md")):
                    if note.stem == note.parent.name:
                        owner = self._note_owner(note)
                        if owner:
                            found.setdefault(owner, note.parent.relative_to(self.root).as_posix())
            self._existing = found
        return self._existing

    def _name_budget(self) -> int:
        # Windows路径上限260（按UTF-16算）。会话名在路径里出现两次（目录名和文件名），
        # 再加上「聊天/群聊/」「（2026-09）.md」这些固定部分
        used = len(str(self.root).encode("utf-16-le")) // 2 + 30
        return max(12, min(60, (240 - used) // 2))

    def _assign_folder(self, chat: Chat) -> str:
        # 目录名在群聊、私聊之间也不重名（Windows不分大小写），搜索和快速切换时不会混
        taken = {PurePosixPath(v["folder"]).name.casefold()
                 for k, v in self.state.chats.items() if k != chat.id and v.get("folder")}
        taken |= {PurePosixPath(r).name.casefold() for cid, r in self._scan_existing().items() if cid != chat.id}
        name = safe_name(chat.name, self._name_budget())
        tail = _id_tail(chat.id)
        for i in range(100):
            base = name if i == 0 else f"{name}（{tail}{'' if i == 1 else i}）"
            if base.casefold() in taken:
                continue
            rel = f"聊天/{chat.kind}/{base}"
            owner = self._note_owner(self.root / rel / f"{base}.md")
            if owner is None or owner == chat.id:  # 不存在，或者本来就是这个会话的
                return rel
        raise RuntimeError(f"会话目录重名太多：{name}")

    @staticmethod
    def _note_owner(note: Path) -> str | None:
        if not note.exists():
            return None
        try:
            head = read_text(note)[:2000]
        except (OSError, UnicodeDecodeError):
            return ""
        m = _OWNER_RE.search(head)
        return m.group(1) if m else ""  # 存在但不是本工具建的：当成别人的，不占用

    def note_title(self, chat: Chat) -> str:
        return PurePosixPath(self.folder_rel(chat)).name

    def chat_dir(self, chat: Chat) -> Path:
        return self.root / self.folder_rel(chat)

    # ---- 链接 ----

    def _vault_path(self, chat: Chat, note: str) -> str:
        parts = [self.link_prefix, self.folder_rel(chat), note]
        return "/".join(p for p in parts if p)

    def chat_link(self, chat: Chat) -> str:
        title = self.note_title(chat)
        return f"[[{self._vault_path(chat, title)}|{link_alias(title)}]]"

    def month_link(self, chat: Chat, month: str, label: str) -> str:
        note = month_note_name(self.note_title(chat), month)
        return f"[[{self._vault_path(chat, note)}|{link_alias(label)}]]"

    def message_link(self, chat: Chat, m: Message) -> str:
        """指向按月记录里那一天的链接，显示「09-30 10:23 张总」。"""
        local = m.time.astimezone(self.tz)
        note = month_note_name(self.note_title(chat), local.strftime("%Y-%m"))
        target = f"{self._vault_path(chat, note)}#{day_heading(local.date())}"
        return f"[[{target}|{link_alias(f'{local:%m-%d %H:%M} {m.sender}')}]]"

    # ---- 写文件 ----

    def write_month(self, chat: Chat, month: str, msgs: list[Message], *, force: bool = False) -> str:
        """写一个月的记录。返回written（写了）、same（内容没变）或kept（新的比旧的少，没覆盖）。"""
        path = self.chat_dir(chat) / f"{month_note_name(self.note_title(chat), month)}.md"
        st = self.state.chat(chat.id)
        st["months"] = sorted(set(st.get("months", [])) | {month})
        if not force and path.exists():
            # 微信里删过记录、换过电脑，或者把export.since调到了月中，重写会让归档缩水：默认不覆盖
            try:
                m = _COUNT_RE.search(read_text(path)[:1000])
            except (OSError, UnicodeDecodeError):
                m = None
            if m and len(msgs) < int(m.group(1)):
                return "kept"
        text = render_month(chat, month, msgs, self.tz, self.chat_link(chat))
        return "written" if write_if_changed(path, text) else "same"

    def _months_on_disk(self, chat: Chat) -> set[str]:
        folder, title = self.chat_dir(chat), self.note_title(chat)
        pattern = re.compile(re.escape(title) + r"（(\d{4}-\d{2})）\.md")
        if not folder.is_dir():
            return set()
        return {m.group(1) for f in folder.iterdir() if (m := pattern.fullmatch(f.name))}

    def update_chat_note(self, chat: Chat) -> None:
        st = self.state.chat(chat.id)
        # 目录里已有的月份也算上：进度文件丢了重建时，旧月份的链接不会丢
        months = sorted(set(st.get("months", [])) | self._months_on_disk(chat), reverse=True)
        st["months"] = sorted(months)
        if not months:
            return
        title = self.note_title(chat)
        block = [BLOCK_BEGIN, f"- 类型：{chat.kind}"]
        if st.get("last_time"):
            block.append(f"- 最近一条：{self._fmt(st['last_time'])}")
        for year, ms in groupby(months, key=lambda x: x[:4]):
            links = " · ".join(self.month_link(chat, m, f"{int(m[5:])}月") for m in ms)
            block.append(f"- {year}：{links}")
        block.append(BLOCK_END)
        block_text = "\n".join(block)

        path = self.chat_dir(chat) / f"{title}.md"
        if path.exists():
            try:
                old = read_text(path)
            except UnicodeDecodeError:
                raise OSError(f"会话页的编码认不出来，没有改动：{path}") from None
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
                title = PurePosixPath(st["folder"]).name
                path = "/".join(p for p in (self.link_prefix, st["folder"], title) if p)
                rows.append((bool(st.get("is_group")), last, path, title, len(st["months"])))
        if not rows:
            return
        lines = [f"# {ROOT_INDEX}", "", f"> 由wxexport生成，共{len(rows)}个会话，按最近消息排序。"]
        oldest = datetime(2000, 1, 1, tzinfo=self.tz)
        for is_group, label in ((True, "群聊"), (False, "私聊")):
            part = sorted((r for r in rows if r[0] == is_group), key=lambda r: r[1] or oldest, reverse=True)
            if not part:
                continue
            lines += ["", f"## {label}（{len(part)}）", "", "| 会话 | 最近一条 | 月份数 |", "| --- | --- | --- |"]
            for _, last, path, title, n in part:
                when = last.astimezone(self.tz).strftime("%Y-%m-%d %H:%M") if last else ""
                lines.append(f"| [[{path}\\|{link_alias(title)}]] | {when} | {n} |")  # 表格里的 | 要转义
        write_if_changed(self.root / f"{ROOT_INDEX}.md", "\n".join(lines) + "\n")
