"""增量导出的进度记录。文件放在归档目录里，文件名以点开头，Obsidian不会显示它。"""

from __future__ import annotations

import json
from pathlib import Path

from .fsutil import atomic_write_text

STATE_FILE = ".wxexport-state.json"


class StateError(Exception):
    pass


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"version": 1, "chats": {}, "actions": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise StateError(
                    f"进度文件损坏：{path}（{e}）。删掉它会从头重新导出，已有的笔记不受影响。"
                ) from None
            if not isinstance(loaded, dict):
                raise StateError(f"进度文件格式不对：{path}")
            self.data.update(loaded)

    @classmethod
    def for_root(cls, root: Path) -> "State":
        return cls(root / STATE_FILE)

    @property
    def chats(self) -> dict:
        return self.data.setdefault("chats", {})

    def chat(self, chat_id: str) -> dict:
        return self.chats.setdefault(chat_id, {})

    @property
    def actions(self) -> dict:
        return self.data.setdefault("actions", {})

    def save(self) -> None:
        atomic_write_text(self.path, json.dumps(self.data, ensure_ascii=False, indent=1) + "\n")
