"""读取config.yaml。配置项说明见config.example.yaml。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

EFFORTS = ("low", "medium", "high", "xhigh", "max")
SOURCE_TYPES = ("chatlog_api", "file")


class ConfigError(Exception):
    pass


@dataclass
class SourceConfig:
    type: str = "chatlog_api"
    base_url: str = "http://127.0.0.1:5030"
    timeout: float = 120.0
    path: str = ""
    csv_columns: dict = field(default_factory=dict)
    file_timezone: str = ""
    self_ids: list = field(default_factory=list)


@dataclass
class ObsidianConfig:
    vault: str = ""
    folder: str = "微信记录"
    timezone: str = "Asia/Shanghai"


@dataclass
class ExportConfig:
    since: date | None = None
    include: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    groups: bool = True
    private: bool = True
    skip_official_accounts: bool = True


@dataclass
class AIConfig:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    context: str = ""
    exclude: list = field(default_factory=list)
    mask: bool = True
    default_days: int = 7
    max_chars_per_request: int = 150_000
    concurrency: int = 4


@dataclass
class Config:
    source: SourceConfig
    obsidian: ObsidianConfig
    export: ExportConfig
    ai: AIConfig
    path: Path | None = None

    @cached_property
    def tz(self) -> ZoneInfo:
        return _zone(self.obsidian.timezone, "obsidian.timezone")

    @cached_property
    def file_tz(self) -> ZoneInfo:
        if not self.source.file_timezone:
            return self.tz
        return _zone(self.source.file_timezone, "source.file_timezone")

    @property
    def vault(self) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(self.obsidian.vault)))

    @property
    def root(self) -> Path:
        """归档根目录：库路径 + 库内文件夹。"""
        return self.vault / self.obsidian.folder


def _zone(name: str, key: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ConfigError(
            f"{key}的时区「{name}」找不到。Windows需要先装时区数据：pip install tzdata"
        ) from None
    except ValueError:
        raise ConfigError(f"{key}的时区「{name}」写法不对，例：Asia/Shanghai") from None


def _section(cls, raw, name: str):
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise ConfigError(f"「{name}」下面应该是一组「键: 值」")
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"「{name}」里有不认识的配置项：{', '.join(unknown)}")
    return cls(**raw)


def _str_list(value, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, (str, int)) for v in value):
        return [str(v) for v in value]
    raise ConfigError(f"{key}应该是名字或ID的列表")


def default_config_path() -> Path:
    env = os.environ.get("WXEXPORT_CONFIG")
    return Path(env) if env else Path("config.yaml")


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path) if path else default_config_path()
    if not path.exists():
        raise ConfigError(
            f"找不到配置文件{path}。把config.example.yaml复制一份改名为config.yaml，按注释填好再运行。"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(
            f"{path}格式有误：{e}\n提示：Windows路径请用正斜杠（D:/Obsidian/库），或用单引号括起来。"
        ) from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}内容不是「键: 值」格式")
    unknown = sorted(set(raw) - {"source", "obsidian", "export", "ai"})
    if unknown:
        raise ConfigError(f"配置里有不认识的部分：{', '.join(unknown)}")

    cfg = Config(
        source=_section(SourceConfig, raw.get("source"), "source"),
        obsidian=_section(ObsidianConfig, raw.get("obsidian"), "obsidian"),
        export=_section(ExportConfig, raw.get("export"), "export"),
        ai=_section(AIConfig, raw.get("ai"), "ai"),
        path=path,
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if cfg.source.type not in SOURCE_TYPES:
        raise ConfigError(f"source.type只能是{' / '.join(SOURCE_TYPES)}")
    if not isinstance(cfg.source.csv_columns, dict):
        raise ConfigError("source.csv_columns应该是「字段: 列名」的映射")
    cfg.source.self_ids = _str_list(cfg.source.self_ids, "source.self_ids")
    if not str(cfg.obsidian.vault).strip():
        raise ConfigError("obsidian.vault没填：写你的Obsidian库所在文件夹")
    if not str(cfg.obsidian.folder).strip() or Path(cfg.obsidian.folder).is_absolute() or ".." in Path(cfg.obsidian.folder).parts:
        raise ConfigError("obsidian.folder要写库里的相对文件夹名，例：微信记录")

    since = cfg.export.since
    if isinstance(since, datetime):
        cfg.export.since = since.date()
    elif isinstance(since, str):
        try:
            cfg.export.since = date.fromisoformat(since.strip())
        except ValueError:
            raise ConfigError("export.since要写成2024-01-01这样的日期") from None
    elif since is not None and not isinstance(since, date):
        raise ConfigError("export.since要写成2024-01-01这样的日期")
    cfg.export.include = _str_list(cfg.export.include, "export.include")
    cfg.export.exclude = _str_list(cfg.export.exclude, "export.exclude")
    cfg.ai.exclude = _str_list(cfg.ai.exclude, "ai.exclude")

    if cfg.ai.effort not in EFFORTS:
        raise ConfigError(f"ai.effort只能是{' / '.join(EFFORTS)}")
    if not isinstance(cfg.ai.default_days, int) or cfg.ai.default_days < 1:
        raise ConfigError("ai.default_days要写正整数")
    if not isinstance(cfg.ai.max_chars_per_request, int) or cfg.ai.max_chars_per_request < 5_000:
        raise ConfigError("ai.max_chars_per_request至少5000")
    if not isinstance(cfg.ai.concurrency, int) or not 1 <= cfg.ai.concurrency <= 16:
        raise ConfigError("ai.concurrency要写1到16之间的整数")
    cfg.ai.context = str(cfg.ai.context or "").strip()
    # 时区写错在这里就报出来，别等导出到一半
    _ = cfg.tz, cfg.file_tz
