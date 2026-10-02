# 微信聊天记录导出（wxexport）

把自己的微信聊天记录按会话、按月归档进Obsidian，再用Claude把新消息里的待办、承诺、决定整理成行动项笔记。

## 先说清楚的几件事

- **本工具不解密任何数据。** 数据从哪来由你决定：chatlog兼容的本地接口，或者其他工具导出的JSON/CSV文件。
- **数据源工具的现状。** chatlog作者2025年10月20日收到微信官方函件后，删除了全部代码和提交历史，并建议保留旧副本的用户删除、自行评估合规风险（见[原仓库说明](https://github.com/sjzar/chatlog)）。留痕等同类项目也已下架。网上流传的分支和安装包来源不明，这类程序按设计能读到你全部聊天记录，被植入后门的后果很严重。用不用、用哪个，由你自己判断。
- **调用Claude等于把聊天内容发到境外服务。** 工作群里有公司敏感信息的，先对照公司保密规定。不能外发的会话放进`ai.exclude`，或者只用`--prompt-only`，自己决定贴什么出去。
- 只用于你自己的账号和数据。

## 能做什么

| 命令 | 作用 |
| --- | --- |
| `doctor` | 检查配置、数据源、Obsidian库、API密钥和代理 |
| `list` | 列出会话（按最近消息排序），查名字和ID |
| `export` | 导出到Obsidian。默认增量，可以指定会话、时间段，或者全量重导 |
| `actions` | 提取行动项。默认只看每个会话上次处理之后的新消息 |
| `run` | 日常一键：增量导出，再提取行动项 |

## 安装（Windows）

1. 装Python 3.10或更新版本（python.org下载，安装时勾选「Add python.exe to PATH」）。
2. 在`wechat-export`文件夹里打开PowerShell：

   ```powershell
   py -m pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
   ```

3. 把`config.example.yaml`复制一份，改名为`config.yaml`，按注释填好Obsidian库路径和数据源。
4. 检查一遍：

   ```powershell
   py -m wxexport doctor
   ```

## 数据源

### chatlog兼容接口

接口服务开着时（默认`http://127.0.0.1:5030`），工具只读这几个接口：`/api/v1/session`、`/api/v1/contact`、`/api/v1/chatroom`、`/api/v1/chatlog`。访问本地接口不走系统代理，开着全局代理也不影响。

### 导出文件

`source.type`改成`file`，`source.path`指向文件或文件夹（会读取里面所有`.json`和`.csv`）：

- JSON：chatlog格式的消息数组，或`{"messages": [...]}`。
- CSV：默认按chatlog的表头（`Time,SenderName,Sender,TalkerName,Talker,Content`）。其他工具导出的CSV，在`source.csv_columns`里把字段对应到实际列名；没有的列写`""`。GBK编码的CSV也能读。

几份导出时间段有重叠时会自动去重。

### 手机上的记录

不用另做一套。先用微信自带的迁移功能把手机记录迁到电脑版：手机微信「设置」里的「聊天记录迁移与备份→迁移→迁移到电脑微信」（不同版本入口位置略有差别，手机和电脑要连同一个网络）。迁移完成后电脑版里就有这些记录，再走同一条流程。

## 日常用法

```powershell
py -m wxexport run                                    # 每天一次：增量导出+行动项
py -m wxexport list 项目                              # 找会话
py -m wxexport export --chat "项目群A"                 # 只导出一个会话（写名字的一部分也行，只要不重名）
py -m wxexport export --chat "*项目*" --since 2026-01-01   # 通配，从指定日期起
py -m wxexport export --all --full                    # 全部会话从头重导
py -m wxexport actions --days 3                       # 最近3天的行动项
py -m wxexport actions --chat "张总" --since 2026-09-01  # 指定会话和起点
py -m wxexport actions --prompt-only                  # 不调用API，只生成提示词文件
```

几点说明：

- 「全量」和「自选」都靠参数：不带`--chat`就按配置里的`export.include`/`export.exclude`选会话；带了`--chat`只处理指定的，但`export.exclude`里的照样跳过。
- 导出按月整页重写：`--since`/`--until`涉及到的月份会整月重新生成，结果和重复运行一致。
- `export.since`是全局下限，任何导出都不会早于它。之后把它往前调，要用`--full`才会补上更早的记录。
- 行动项的进度按会话记：每个会话记住处理到了哪条消息。电脑版微信没开着时跑过一次、之后才同步进来的消息不会漏。被拒绝或出错的会话进度不前进，下次自动重试；总是失败的放进`ai.exclude`。指定了`--until`算补看历史，不推进进度。

## Obsidian里的结构

```text
微信记录/
  微信记录索引.md
  聊天/群聊/项目群A/项目群A.md              会话页：自动区+你的批注
  聊天/群聊/项目群A/项目群A（2026-09）.md    按月记录，整页自动生成
  聊天/私聊/张总/……
  行动项/2026-10-02行动项.md
  行动项/提示词/……                         --prompt-only生成的文件
```

按月记录长这样：

```markdown
## 2026-09-30（周三）

**15:40 张总**：这个格式业主确认过了吗
> 引用 李工：[文件] 投标保函格式.docx

**16:00 我**：确认过了，\$2.5M的额度
```

行动项笔记长这样（Obsidian Tasks插件能直接识别）：

```markdown
## [[项目群A]]

要点：业主要求下周五前提交保函材料，格式已确认。

- [ ] 把保函材料给银行 📅 2026-09-04
    - 待办 · 责任方：我方 · 截止：下周五前（2026-09-04） · 置信度：高
    - 原话：「下周五前把保函材料给银行」[[项目群A（2026-09）#2026-09-01（周二）|09-01 04:30 张总]]
```

使用规则：

- 按月记录每次会整页重写，别在里面写东西。批注写在会话页「## 批注」下面，导出只刷新会话页里的自动区。
- 会话改了备注名，目录名不变，已有链接不会断。
- 每条行动项都带原话和指向原文那一天的链接。Claude给出的消息编号对不上原文的，标「出处未核实」并降为低置信度。截止期原文没提的写「未明确」，不推断。
- 聊天里的`$`、`#`、`%%`等符号会转义，不会被Obsidian当成公式、标签或注释。

## Claude设置

- 密钥：设置环境变量`ANTHROPIC_API_KEY`。PowerShell里执行`setx ANTHROPIC_API_KEY "sk-ant-..."`，重开窗口后生效。
- 模型默认`claude-opus-5-5`，`ai.effort`默认`medium`。调高更细，也更贵；每次运行结束会打印token用量。
- 被安全策略拦下时，服务端会按类别自动换推荐的模型重跑；仍然失败的会话列在当天笔记的「未处理」里。
- `ai.context`可以写一句工作背景，帮Claude看懂行话。

## 网络（中国大陆）

- 调Claude API需要代理。设置`HTTPS_PROXY`，例如`setx HTTPS_PROXY "http://127.0.0.1:7890"`，端口按你的代理软件填。
- 装依赖慢就用清华镜像（安装命令里的`-i`参数）。

## 隐私

- `config.yaml`和进度文件`.wxexport-state.json`已在`.gitignore`里，不会提交。
- `export.exclude`：永远不导出；`ai.exclude`：导出到Obsidian，但不发给Claude。
- `ai.mask`默认打开：发给Claude前把手机号、身份证号、银行卡号、邮箱换成占位符。16到19位的纯数字编号也会被当成卡号遮掉，影响理解的话可以关掉。
- 归档只写你本地的Obsidian库；除了调用Claude那一步，工具不向外发送任何内容。

## 常见问题

- **提示找不到时区**：`py -m pip install tzdata`。
- **YAML报错**：Windows路径用正斜杠`D:/Obsidian/库`，或者用单引号括起来。
- **时间和微信里差几个小时**：归档统一按`obsidian.timezone`（默认北京时间），不跟电脑时区走。
- **想重建归档**：删掉`微信记录/.wxexport-state.json`再导出。已有目录会被认出来继续用，按月记录全部重新生成，会话页里你写的批注保留。行动项进度也会清空，下次按`ai.default_days`往回看，可能和之前的笔记有重复。

## 开发

```powershell
py -m pip install -e ".[dev]"
py -m pytest
```

测试用本地模拟的chatlog接口和Messages API，不需要真实数据，也不需要API密钥。
