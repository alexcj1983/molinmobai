"""命令行入口：python -m wxexport <命令>。"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import __version__
from .actions import FatalAPIError, run_actions
from .config import Config, ConfigError, load_config
from .exporter import ChatResult, export_chats
from .model import Chat, is_noise_chat
from .obsidian import VaultWriter
from .privacy import is_glob, matches, name_matches
from .sources import Source, SourceError, open_source
from .state import State, StateError


class UsageError(Exception):
    pass


# ---- 参数处理 ----


def _day(s: str, tz) -> datetime:
    try:
        d = date.fromisoformat(s.strip())
    except ValueError:
        raise UsageError(f"日期「{s}」要写成2026-09-01这样") from None
    return datetime(d.year, d.month, d.day, tzinfo=tz)


def _window(args, tz) -> tuple[datetime | None, datetime | None]:
    since = until = None
    if getattr(args, "days", None) is not None:
        if args.days < 1:
            raise UsageError("--days要大于0")
        today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        since = today - timedelta(days=args.days - 1)
    if getattr(args, "since", None):
        since = _day(args.since, tz)
    if getattr(args, "until", None):
        until = _day(args.until, tz) + timedelta(days=1)  # 含当天
    if since and until and since >= until:
        raise UsageError("开始日期要早于结束日期")
    return since, until


def _ambiguous(token: str, found: list[Chat]) -> str:
    lines = [f"「{token}」对应多个会话，请写完整名字或ID："]
    lines += [f"  {c.kind}  {c.name}  [{c.id}]" for c in found[:15]]
    if len(found) > 15:
        lines.append(f"  ……共{len(found)}个")
    return "\n".join(lines)


def _resolve_one(token: str, chats: list[Chat], source: Source) -> list[Chat]:
    exact = ([c for c in chats if token in (c.id, c.name)]
             or [c for c in chats if name_matches(token, c.id, c.name) and not is_glob(token)])
    if len(exact) == 1:
        return exact
    if len(exact) > 1:
        raise UsageError(_ambiguous(token, exact))
    if is_glob(token):  # 通配（只认*和?）：匹配到几个算几个
        found = [c for c in chats if name_matches(token, c.id, c.name)]
        if found:
            return found
        raise UsageError(f"「{token}」没有匹配到任何会话")
    found = [c for c in chats if token.casefold() in c.name.casefold()]
    if len(found) == 1:
        return found
    if len(found) > 1:
        raise UsageError(_ambiguous(token, found))
    if token.startswith("wxid_") or token.endswith("@chatroom"):  # 会话列表里没有，但给的是ID
        c = source.chat_by_id(token)
        if c:
            return [c]
    raise UsageError(f"没找到会话「{token}」，先用list命令查一下名字或ID")


def _warn(source: Source) -> None:
    for w in source.take_warnings():
        print(f"提醒：{w}")


def select_chats(cfg: Config, source: Source, tokens: list[str], use_all: bool, for_ai: bool) -> list[Chat]:
    chats = source.list_chats()
    _warn(source)

    def blocked(c: Chat) -> str:
        if matches(cfg.export.exclude, c):
            return "在export.exclude里"
        if for_ai and matches(cfg.ai.exclude, c):
            return "在ai.exclude里，不发给Claude"
        return ""

    if tokens:
        chosen: list[Chat] = []
        for token in tokens:
            for c in _resolve_one(token, chats, source):
                why = blocked(c)
                if why:
                    print(f"跳过「{c.name}」：{why}")
                elif all(c.id != x.id for x in chosen):
                    chosen.append(c)
        return chosen

    pool = [c for c in chats if not (cfg.export.skip_official_accounts and is_noise_chat(c.id))]
    pool = [c for c in pool if (cfg.export.groups if c.is_group else cfg.export.private)]
    pool = [c for c in pool if not blocked(c)]
    if cfg.export.include and not use_all:
        pool = [c for c in pool if matches(cfg.export.include, c)]
    return pool


def _floor(cfg: Config) -> datetime | None:
    d = cfg.export.since
    return datetime(d.year, d.month, d.day, tzinfo=cfg.tz) if d else None


def _open(cfg: Config) -> tuple[Source, State, VaultWriter]:
    if not cfg.vault.is_dir():
        raise ConfigError(f"Obsidian库不存在：{cfg.vault}（检查obsidian.vault）")
    source = open_source(cfg)
    state = State.for_root(cfg.root)
    return source, state, VaultWriter(cfg.root, cfg.tz, state, link_prefix=Path(cfg.obsidian.folder).as_posix())


def _report(results: list[ChatResult]) -> None:
    done = [r for r in results if r.messages]
    skipped = sum(1 for r in results if r.skipped)
    failed = [r for r in results if r.error]
    print(f"完成：{len(done)}个会话有更新，共{sum(r.messages for r in done)}条；"
          f"{skipped}个没有新消息" + (f"；{len(failed)}个出错" if failed else ""))
    for r in failed:
        print(f"  ✗ {r.chat.name}：{r.error}")


# ---- 命令 ----


def cmd_doctor(cfg: Config, args) -> int:
    ok = True
    print(f"配置文件：{cfg.path}")
    if cfg.vault.is_dir():
        try:
            cfg.root.mkdir(parents=True, exist_ok=True)
            probe = cfg.root / ".wxexport-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            print(f"✓ 归档目录可写：{cfg.root}")
        except OSError as e:
            ok = False
            print(f"✗ 归档目录写不进去：{cfg.root}（{e}）")
    else:
        ok = False
        print(f"✗ Obsidian库不存在：{cfg.vault}")
    try:
        print(f"✓ {open_source(cfg).check()}")
    except SourceError as e:
        ok = False
        print(f"✗ 数据源：{e}")
    ok = _check_backend(cfg) and ok
    return 0 if ok else 1


def _check_backend(cfg: Config) -> bool:
    b = cfg.ai.backend
    if b == "prompt":
        print("· 行动项后端：prompt——只生成提示词文件，不调任何模型，什么都不出电脑")
        return True
    if b == "local":
        try:
            names = _ollama_models(cfg.ai.local_url, timeout=5)
        except OSError as e:
            print(f"✗ 行动项后端：local——连不上本地模型服务{cfg.ai.local_url}（{e}）。装了Ollama并在运行吗？")
            return False
        want = cfg.ai.local_model
        if any(n == want or n.split(":")[0] == want.split(":")[0] for n in names):
            print(f"✓ 行动项后端：local——{cfg.ai.local_url}，模型{want}已就绪，数据不出电脑")
            return True
        print(f"✗ 行动项后端：local——模型「{want}」还没下载。先运行：ollama pull {want}"
              + (f"（已有：{'、'.join(names)}）" if names else ""))
        return False
    # claude
    print("· 行动项后端：claude——⚠ 会把聊天内容发到境外Claude。只在你确认这些会话可以外发时用")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if has_key:
        print(f"  ✓ 已设置Claude API密钥（模型{cfg.ai.model}，effort {cfg.ai.effort}）")
    else:
        print("  ✗ 没检测到ANTHROPIC_API_KEY（环境变量），调用会失败")
    proxy = next((os.environ[k] for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
                  if os.environ.get(k)), "")
    print(f"  · HTTPS代理：{proxy or '未设置'}（在中国大陆调用Claude API需要）")
    return has_key


def _ollama_models(base_url: str, timeout: float) -> list[str]:
    import json
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机不走代理
    with opener.open(base_url.rstrip("/") + "/api/tags", timeout=timeout) as resp:
        data = json.loads(resp.read())
    return [str(m.get("name") or "") for m in (data.get("models") or []) if isinstance(m, dict)]


def cmd_list(cfg: Config, args) -> int:
    source = open_source(cfg)
    chats = source.list_chats()
    _warn(source)
    if not args.all:
        chats = [c for c in chats if not is_noise_chat(c.id)]
    if args.groups:
        chats = [c for c in chats if c.is_group]
    if args.private:
        chats = [c for c in chats if not c.is_group]
    if args.keyword:
        chats = [c for c in chats if args.keyword in c.name or args.keyword in c.id]
    oldest = datetime(2000, 1, 1, tzinfo=cfg.tz)
    chats.sort(key=lambda c: c.last_time or oldest, reverse=True)
    for c in chats[: args.limit]:
        when = c.last_time.astimezone(cfg.tz).strftime("%Y-%m-%d %H:%M") if c.last_time else "—".ljust(16)
        flag = "（不导出）" if matches(cfg.export.exclude, c) else ("（不发Claude）" if matches(cfg.ai.exclude, c) else "")
        print(f"{c.kind}  {when}  {c.name}  [{c.id}]{flag}")
    print(f"共{len(chats)}个会话，显示{min(len(chats), args.limit)}个")
    return 0


def cmd_export(cfg: Config, args) -> int:
    since, until = _window(args, cfg.tz)
    if args.full and (since or until):
        raise UsageError("--full不能和--since、--days、--until一起用")
    source, state, writer = _open(cfg)
    chats = select_chats(cfg, source, args.chat, args.all, for_ai=False)
    if not chats:
        print("没有要导出的会话。")
        return 0
    mode = "全量重导" if args.full else ("按时间段导出（按月对齐）" if since or until else "增量导出")
    print(f"{mode}：{len(chats)}个会话 → {cfg.root}")
    results = export_chats(source, writer, state, chats, tz=cfg.tz, floor=_floor(cfg),
                           since=since, until=until, full=args.full, force=args.force)
    _report(results)
    return 1 if any(r.error for r in results) else 0


def _apply_backend(cfg: Config, args) -> None:
    if getattr(args, "backend", None):
        cfg.ai.backend = args.backend
    if cfg.ai.backend == "claude" and not args.prompt_only:
        print("⚠ 行动项用的是云端Claude，会把这些会话的内容发到境外。不想外发就用--prompt-only，或在配置里把ai.backend改成local")


def cmd_actions(cfg: Config, args) -> int:
    since, until = _window(args, cfg.tz)
    _apply_backend(cfg, args)
    source, state, writer = _open(cfg)
    chats = select_chats(cfg, source, args.chat, False, for_ai=True)
    result = run_actions(cfg, source, state, writer, chats, since=since, until=until, prompt_only=args.prompt_only)
    if result.path:
        print(f"已写入：{result.path}")
    return 1 if result.failed else 0


def cmd_run(cfg: Config, args) -> int:
    _apply_backend(cfg, args)
    source, state, writer = _open(cfg)
    chats = select_chats(cfg, source, [], False, for_ai=False)
    print(f"① 增量导出：{len(chats)}个会话 → {cfg.root}")
    results = export_chats(source, writer, state, chats, tz=cfg.tz, floor=_floor(cfg))
    _report(results)
    print("② 提取行动项")
    ai_chats = [c for c in chats if not matches(cfg.ai.exclude, c)]
    result = run_actions(cfg, source, state, writer, ai_chats, prompt_only=args.prompt_only)
    if result.path:
        print(f"已写入：{result.path}")
    return 1 if result.failed or any(r.error for r in results) else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wxexport", description="微信聊天记录归档到Obsidian，并用Claude提取行动项。")
    p.add_argument("-c", "--config", help="配置文件（默认当前目录的config.yaml，或环境变量WXEXPORT_CONFIG）")
    p.add_argument("--version", action="version", version=f"wxexport {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="命令")

    sub.add_parser("doctor", help="检查配置、数据源、Obsidian和API设置").set_defaults(func=cmd_doctor)

    ls = sub.add_parser("list", help="列出会话（按最近消息排序），查名字和ID")
    ls.add_argument("keyword", nargs="?", help="按名字或ID筛选")
    ls.add_argument("--groups", action="store_true", help="只看群聊")
    ls.add_argument("--private", action="store_true", help="只看私聊")
    ls.add_argument("--all", action="store_true", help="包括公众号和系统账号")
    ls.add_argument("-n", "--limit", type=int, default=50, help="最多显示几个（默认50）")
    ls.set_defaults(func=cmd_list)

    def add_common(sp: argparse.ArgumentParser) -> None:
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--since", metavar="日期", help="从这天开始，例2026-09-01")
        g.add_argument("--days", type=int, metavar="N", help="最近N天（含今天）")
        sp.add_argument("--until", metavar="日期", help="到这天为止（含当天）")
        sp.add_argument("--chat", action="append", default=[], metavar="会话",
                        help='指定会话：名字、ID或通配（如 "*项目*"），可写多次')

    ex = sub.add_parser("export", help="导出到Obsidian（默认增量）")
    add_common(ex)
    ex.add_argument("--all", action="store_true", help="忽略export.include，导出全部会话")
    ex.add_argument("--full", action="store_true", help="忽略进度，从头重导")
    ex.add_argument("--force", action="store_true", help="新取到的消息比归档里少时也覆盖（默认不覆盖）")
    ex.set_defaults(func=cmd_export)

    backends = ("prompt", "local", "claude")
    ac = sub.add_parser("actions", help="提取行动项（默认从上次处理到现在）")
    add_common(ac)
    ac.add_argument("--backend", choices=backends, help="临时指定后端，覆盖配置里的ai.backend")
    ac.add_argument("--prompt-only", action="store_true", help="只生成提示词文件，不调任何模型")
    ac.set_defaults(func=cmd_actions)

    rn = sub.add_parser("run", help="日常一键：增量导出 + 提取行动项")
    rn.add_argument("--backend", choices=backends, help="临时指定后端，覆盖配置里的ai.backend")
    rn.add_argument("--prompt-only", action="store_true", help="行动项这一步只生成提示词文件")
    rn.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # 老式控制台编码打不出的字符换成问号，别让程序崩
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        cfg = load_config(args.config)
        return args.func(cfg, args)
    except (ConfigError, SourceError, StateError, UsageError, FatalAPIError) as e:
        print(f"出错：{e}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as e:  # 文件被占用、磁盘满、编码认不出之类
        print(f"出错：文件读写失败（{type(e).__name__}）：{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。做完的会话进度已保存，下次运行会接着来。", file=sys.stderr)
        return 130
