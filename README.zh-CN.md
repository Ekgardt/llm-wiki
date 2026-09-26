# LLM Wiki

[![Tests](https://github.com/Ekgardt/llm-wiki/actions/workflows/tests.yml/badge.svg)](https://github.com/Ekgardt/llm-wiki/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-5.0.0-blue.svg)](CHANGELOG.md)

**为你所有的 AI 编码代理提供同一份本地记忆——磁盘上的纯 Markdown，存于 Git，归你所有。**

Claude Code、Codex 和 OpenCode 在会话结束时都会忘掉一切。LLM Wiki 记录每次会话发生
了什么，在夜间把它提炼成简短的知识页面，并把决策、经验和项目状态交给下一次会话——
无论你用的是哪个代理。同样的事情不必再解释第二遍。

存储、捕获、搜索和 MCP 服务器都在本地运行。把会话变成页面需要一个语言模型：你配置
的那个，或在 OpenCode、Codex、Claude、OpenAI 和 Ollama 中找到的第一个。只有 Ollama 是
本地的，其余都是云服务，因此自动检测并不保证一切留在本机。当前版本：**5.0.0**。

**语言：** [English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

---

## 工作原理

```
你照常与代理协作
      ↓  轻量钩子把每个会话事件交给 integration_adapter.py
会话记录  →  knowledge/raw/sessions/<日期>/  （已脱敏，每次会话都保留）
每日日志  →  knowledge/daily/<日期>.md
      ↓  编译（空闲时在会话开始时进行，并且每晚进行）
知识页面  →  knowledge/notes/<slug>.md   （每条引用都与来源核对）
      ↓
任何代理的下一次会话：学到的规则、未完成事项、最近的决策、项目状态——
以及 12 个 task-shaped MCP 工具，用来向记忆提问
```

思路是"编译，而不是检索"（[Karpathy，2026 年 4 月](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)）：
不在提问时搜索原始会话记录，而是由后台一次性把它们变成结构化页面，代理读取这些页面。

系统眼中的一个工作日：

- 你在 Claude Code 里修复一个错误，并说"这里再也不要用旧 API"。会话被记录下来；
  当晚这条纠正变成一个规则页面。
- 第二天早上你在同一仓库打开 Codex。它的第一条消息就已包含这条规则、项目的未完成
  事项和昨天的决策。
- 你问"我们为什么放弃 LanceDB？"。代理调用 `recall`，用决策页面和它所出自的会话
  中的确切行来回答。

---

## 快速开始

### 前提条件

- Python 3.10+ 和 git
- [uv](https://docs.astral.sh/uv/) 恰好 0.12.3——两个安装程序都会拒绝其他版本
- 你已在使用的代理：Claude Code、Codex 或 OpenCode
- Node 22——仅在需要精确的 TypeScript 或 Python 导航时（见下文）

### 安装

先克隆并阅读源码，然后从该副本运行安装程序：

```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
```

**macOS / Linux / WSL2：**
```bash
LLM_WIKI_ROOT="$(pwd)" bash ./install.sh
```

**Windows：**
```powershell
$env:LLM_WIKI_ROOT = (Get-Location).Path
.\install.ps1
```

安装程序按精确的锁文件构建知识库自己的 `.venv`，安装固定版本的搜索模型，运行有时限
的 production smoke 测试，接入它找到的每个代理，注册每晚和每周的维护，并构建第一个
搜索索引。它会逐个代理说明做了什么、是否还需要你手动处理。

远程引导安装仅支持精确的提交：把 `LLM_WIKI_COMMIT` 设为完整的 40 位提交 OID，并从
可信位置获取安装程序。分支和标签名会被拒绝。每个发布版本都列出其提交以及引导所运行
的每个文件的 SHA-256：

```bash
uv run python scripts/release_manifest.py v5.0.0 --markdown
```

### 检查

```bash
uv run python scripts/doctor.py
uv run python scripts/search_memory.py "auth"
```

`doctor` 只读，报告哪些正常、哪些处于 degraded 状态、哪些损坏，以及该运行什么。

### 依赖配置

MCP 属于 production 基线；`mcp-server` 保留为 compatibility alias。安装程序会替你完成；
手动执行：

```bash
uv sync --locked --no-default-groups
uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120
uv run --locked --no-sync python scripts/repair_installed_memory.py --check --json
```

修复命令默认只读。在新的或空闲的知识库上，安装程序会以
`--apply --adopt-ownership-v3 --confirm-all-agents-stopped` 运行它，把运行时数据迁移到
当前的数据库格式；若知识库中已有工作，会先请你确认没有代理在运行。它从不删除知识
或 `run/`。

可选扩展会叠加到已安装的内容上，并保留你已选择的部分：

```bash
uv sync --locked --no-default-groups --inexact --extra hybrid      # 向量 + reranker
uv sync --locked --no-default-groups --inexact --extra code-graph  # 代码索引
```

贡献者安装开发依赖组并运行完整回归套件：

```bash
uv sync --locked
uv run --locked --no-sync pytest -q
```

提供 pre-commit 钩子（ruff、结构 lint、gitleaks）。
可选；安装程序不会启用这些钩子：
`uv run --locked --no-sync pre-commit install --hook-type pre-commit --hook-type pre-push`。

---

## 代理

| 代理 | 状态 | 接入方式 |
|------|------|----------|
| **Claude Code** | 设置合并验证通过后自动接入 | MCP 服务器 + `settings.json` 中的钩子：七个生命周期事件交给 `integration_adapter.py`，两个钩子为搜索和子代理添加代码图提示 |
| **Codex CLI** | 配置验证通过后自动接入；在 `/hooks` 中批准一次钩子 | MCP 服务器 + 七个生命周期钩子 |
| **OpenCode** | 配置验证通过后自动接入 | MCP 服务器 + 轻量 JS 生命周期插件 |
| **Obsidian** | 仅 viewer | Obsidian 为可选 viewer：打开知识库文件夹即可，无需安装 |

Cursor 与 Antigravity 已于 2026-08-26 退出支持：安装程序不再检测或配置它们，
`uninstall` 仍会收回旧版安装写入的钩子。

所有代理共用一个知识库：在 Claude Code 中记录的决策会出现在 Codex 的下一次会话中。

### MCP 接口

本地 MCP 服务器提供 **12 个 task-shaped 工具**：`recall`、`read_page`、
`wiki_overview`、`vault_status`、`get_decisions`、`get_context`、
`check_contradiction`、`log_decision`、`compile`、`find_dead_code`、
`get_architecture` 和 `doctor`。每个回答都使用统一的 response envelope，写明 schema
版本、新鲜度、证据质量和警告；两个 MCP resources 提供健康状态与上下文。一切正常时
会话开始保持静默，只注入 degraded 或 error 结果。`doctor(repair=true)` 只执行安全、
幂等的本地修复。

服务器默认使用 stdio。同时运行多个代理时，一个共享的本地服务器更省资源：

```bash
uv run python scripts/mcp_http.py --port 8931
```

它只绑定字面 loopback 地址，拒绝任何 `Origin`，并要求它写入
`<state root>/run/mcp-http/token`（权限 0600）的 bearer 令牌。

---

## 你能得到什么

**不丢失任何会话的捕获。** 每次会话都会先写下自己的脱敏记录——对话、每次工具调用
一行、子代理的报告——然后才判断它的价值。之后由分类器决定它是否还值得编译成页面。
密钥、令牌、URL 和命令中的密码会在写入前被移除。

**可以信任的页面。** 编译把每日日志变成带 YAML 头部的类型化页面（决策、模式、调试、
概念……）。写入页面前，Python 会把每条引用与来源行及其摘要核对；第二轮模型审查每处
修改并丢弃薄弱的部分。与已有页面的矛盾会被记录，而不是覆盖：旧页面标记为
`superseded`，不确定的情况进入隔离区等待。每次写入都是可恢复的事务，两天内可撤销。

**会话开始时的上下文。** 从你的纠正中学到的规则、未完成事项、最近的决策、lint 提醒
以及来自其他项目的发现——并按你所在的项目区分：由某个项目的会话编译出的页面带有
`project:`，不会进入其他项目的会话。

**会说明自己如何作答的搜索。** 以词法搜索（BM25）为基础；`hybrid` 扩展加入多语言
向量（ONNX Runtime 上的 `intfloat/multilingual-e5-small`）和 cross-encoder reranker
（`BAAI/bge-reranker-v2-m3`），关系类问题还会用到证据图。排序会考虑说法的来源（你、
网络、模型、推测）以及它所在页面的类型。每个回答都报告请求的模式、实际使用的信号
以及回退原因。在第一个索引建成之前，搜索直接读取 Markdown，并如实说明。

**多个项目，一个知识库。** 每个仓库都有自己的项目文件夹，包含状态、上下文和只追加
的日志；新项目在首次出现时根据其 Git 历史和 README 生成初始上下文。

**自动运行的维护。** 每晚的维护会更新代码（仅 fast-forward，从不 push，若会触及你
修改过的文件则放弃），处理队列，编译，刷新搜索索引，为 `knowledge/` 保存本地 Git
快照，并报告健康状态。每周的维护运行 lint（17 项检查）、归档旧的每日日志并迁移页面
头部。Windows 使用 Task Scheduler，macOS 使用 LaunchAgent，Linux 使用用户级 systemd
定时器；cron 是明确的 degraded 后备方案。

**代码理解。** `get_architecture` 和 `find_dead_code` 依据你的仓库的代码索引作答；
精确的定义、引用、调用方和诊断来自固定版本的语言服务器（见[代码导航](#代码导航)）。

---

## 文件位置

```
CODE        scripts/  tests/  docs/  skills/  rules/  integrations/  benchmark/
KNOWLEDGE   knowledge/{daily,notes,projects,raw,inbox}
RUNTIME     cache/  logs/  run/        （位于知识库内，从不提交）
```

- **代码** 就是这个仓库。
- **知识** 是你的记忆。仓库发布时它是空的：每个页面、每日日志和会话记录都被
  `.gitignore` 拒绝，只跟踪 README。发布某个页面是一次有意的操作。
- **运行时数据** 不进入 Git。`cache/` 和 `logs/` 可以删除并重建；`run/` 保存事务、
  队列和撤销历史，遵循 [docs/STRUCTURE.md](docs/STRUCTURE.md) 中的删除规则。
- **权威来源** 是 Markdown、Git 历史和项目日志。搜索索引、向量、证据图和遥测都是
  派生的，可以重建。

设计依据：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。日常运维、恢复和备份：
[docs/USER-GUIDE.md](docs/USER-GUIDE.md)。

---

## 记忆的安全

运行时数据库使用 rollback journal 和 `synchronous=FULL`；请把 state root 放在本地
磁盘上（网络路径会被拒绝）。队列至少投递一次，因此每个处理程序都是幂等的。

```bash
uv run python scripts/doctor.py
uv run python scripts/doctor.py --repair
uv run python scripts/doctor.py --rebuild-generation
uv run python scripts/markdown_transaction.py recover
uv run python scripts/markdown_transaction.py undo <transaction-id>
uv run python scripts/markdown_transaction.py prune --retention-days 30
uv run python scripts/memory_queue.py work --max-tasks 20 --max-seconds 600 --idle-seconds 2 --lease-seconds 120 --heartbeat-seconds 40 --max-attempts 8 --retry-base-seconds 30 --retry-cap-seconds 3600
uv run python scripts/memory_queue.py redrive <task-id>
uv run python scripts/memory_queue.py purge --terminal-before <ISO-8601> --export <path>
uv run python scripts/archive_daily.py --commit --hot-days 90
```

超过 90 天的每日日志会移入经过校验、未压缩的 BagIt 包（由每周维护完成）；引用它们的
证据仍可解析。若需要在磁盘丢失后仍能保留的副本，知识库提供加密的 Restic 备份和分阶段
恢复；每晚的 `knowledge/` Git 快照只在本地且未加密。参见
[docs/USER-GUIDE.md](docs/USER-GUIDE.md) 的备份部分。

---

## 代码导航

精确模式——`definition`、`references`、`implementations`、`type`、`diagnostics`
以及带位置的 `callers`/`callees`——使用四个固定版本的语言服务器：**Pyright 1.1.411**
（Python）、**typescript-language-server 6.0.0** 配合 tsserver 5.9.3
（TypeScript/JavaScript）、**gopls v0.23.0**（Go）和 **rust-analyzer 1.98.1**（Rust）。
每个都需显式安装；查询从不下载任何东西：

```bash
uv run python scripts/install_pyright.py --state-root "$LLM_WIKI_STATE_ROOT"
uv run python scripts/install_language_server.py --profile typescript --state-root "$LLM_WIKI_STATE_ROOT"
uv run python scripts/install_language_server.py --profile gopls --state-root "$LLM_WIKI_STATE_ROOT"
uv run python scripts/install_language_server.py --profile rust-analyzer --state-root "$LLM_WIKI_STATE_ROOT"
```

没有服务器认领的文件会得到 `unsupported`；服务器缺失或出错时，回答会降级到代码索引，
而不是失败。
此路径仅适用于受信任的本地仓库，不是 OS sandbox。详见
[docs/CODE-NAVIGATION.md](docs/CODE-NAVIGATION.md)。

---

## 搜索索引

`cache/evidence-graph/catalog.sqlite3` 在 `cache/evidence-graph/generations/<generation-id>/`
下选择一个不可变的活动 generation：由你的页面的同一快照构建的全文索引、向量、证据图和
分层。新的 generation 只有在其清单、哈希、数据库和证据片段全部校验通过后才会激活；
构建失败时保留之前的那个。安装程序构建第一个 generation，每晚的维护刷新它，
`uv run python scripts/doctor.py --rebuild-generation` 可按需重建。删除
`cache/evidence-graph/` 只会耗费时间，不会丢失任何东西。

---

## 基准测试

检索以冻结的公开合成语料 `benchmark/retrieval-v2.json` 为门槛——多语言页面，带分级
证据、干扰页面、时间历史以及必须拒答的问题：

```bash
uv run python benchmark/run_benchmark.py
```

长期记忆在 LongMemEval 上测量（`benchmark/run_longmemeval.py`），矛盾处理在其自己的
冻结语料上测量：

```bash
uv run python benchmark/run_contradiction_benchmark.py --corpus benchmark/contradiction-v1.json
```

这里不声称与其他记忆系统相比较：它们公布的数字使用的是不同的数据集。

---

## 参与贡献

欢迎贡献。标准是"它能否经受住真实的多代理工作流？"。环境搭建、编码规范和发布清单
（三个 README、CHANGELOG 和版本号一起变更）见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 致谢

- [Karpathy's LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)——"编译，而不是检索"模式
- [Harrison Chase, "Wiki Memory"](https://blog.langchain.dev/wiki-memory/)——由代理维护的文件
- [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)——厂商中立的 Markdown 知识格式
- [Anthropic, effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)——捕获、压缩与子代理模式
- [VEP Semantic DNA](https://vep.live)——置信度、替代与时间生命周期

---

## 许可证

[MIT](LICENSE)
