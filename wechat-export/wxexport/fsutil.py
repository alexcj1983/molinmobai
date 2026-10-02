"""文件读写小工具：原子写入、编码兜底。"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def read_text(path: Path) -> str:
    """优先UTF-8（带不带BOM都行），不行再按GB18030（Windows下Excel存的CSV常见）。"""
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("gb18030")


def atomic_write_text(path: Path, text: str) -> None:
    """先写临时文件再替换，写到一半断电或被打断也不会留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件以点开头，Obsidian不会去索引它
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        # Windows上文件被同步盘或杀毒软件短暂占用时replace会失败，重试几次
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.3)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_if_changed(path: Path, text: str) -> bool:
    """内容没变就不写，避免同步盘、Obsidian反复刷新。返回是否真的写了。"""
    if path.exists():
        try:
            if read_text(path) == text:
                return False
        except OSError:
            pass
    atomic_write_text(path, text)
    return True
