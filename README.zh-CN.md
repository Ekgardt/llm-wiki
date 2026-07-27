# LLM Wiki

[![Tests](https://img.shields.io/badge/tests-4688%20collected-brightgreen.svg)](https://github.com/Ekgardt/llm-wiki/actions)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-4.0.0-blue.svg)](CHANGELOG.md)

**面向 AI 智能体的本地优先记忆系统。Markdown 文件，git 版本控制，零云依赖。**

LLM Wiki 为你使用的每一个 AI 编码智能体——OpenCode、Codex、Claude Code、Cursor、Antigravity——提供统一的 MCP-first 接口和共享的持久知识库。MCP 负责读取与操作；轻量原生 lifecycle adapter 捕获 MCP 无法观察的会话事件。知识跨会话保留，让你无需重复解释同样的事情。

一切以纯 Markdown 文件形式存储在你的磁盘上：可在 Obsidian 中阅读，可用 git 对比，完全归你所有。

**语言：** [English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

---

## 目录

- [工作原理](#工作原理)
- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [接入智能体](#接入智能体)
- [架构](#架构)
- [Evidence generation 与迁移](#evidence-generation-与迁移)
- [基准测试](#基准测试)
- [对比](#对比)
- [贡献](#贡献)
- [致谢](#致谢)
- [许可证](#许可证)

---

## 工作原理

```
智能体通过本地 MCP 服务器读取记忆并执行操作
             ↓
轻量钩子/插件通过 integration_adapter.py 转发 lifecycle 事件
             ↓
后台编译将 daily 日志提炼为持久知识页面
（带 VERIFY-BEFORE-WRITE——引用会被验证，而非信任 LLM）
             ↓
下次会话：guardrails + advisory + 元认知上下文自动注入
             ↓
智能体从你停下的地方继续——无需重复解释
```

系统遵循"编译而非检索"模式（[Karpathy，2026 年 4 月](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)）：原始会话信号实时捕获，随后后台 LLM 处理将其编译为结构化知识页面，而非在查询时依赖原始检索。

---

## 功能特性

### 捕获流水线
- **轻量 lifecycle adapter**：Claude Code 钩子、OpenCode 插件和 Codex 包装器通过 `integration_adapter.py` 规范化事件
- **3 级会话分类**：FLUSH_MAJOR（决策/经验→触发编译）、FLUSH_MINOR（注意事项→仅保存）、FLUSH_OK（闲聊→跳过）
- **非 LLM breadcrumbs**——prompt 和 tool 调用标记，毫秒级延迟，无 API 调用
- **密钥脱敏**——API 密钥、令牌、长 base64 字符串在任何写入前清除

### Agent-native 接口
- **MCP-first 访问**——12 个本地 task-shaped 工具，覆盖 recall、上下文、决策、维护、代码智能和 `doctor`
- **统一 response envelope**——每个工具返回 schema version、freshness、evidence quality、warnings 和 data；MCP resources 提供 health 与 context
- **自动健康检查**——健康时 SessionStart 保持静默，仅注入 degraded/error 结果；`doctor(repair=true)` 只执行安全、幂等的本地修复

### 编译流水线
- **JSON 协议编译**——无需智能体 tool-use，适用于任何 LLM 后端
- **VERIFY-BEFORE-WRITE**——Python 端确定性引用验证；LLM 无法伪造证据
- **带 quarantine 的语义去重**——优先 update 而非 create；不确定或 evaluator 有分歧的矛盾进入 quarantine，automatic semantic supersession 保持禁用
- **增量编译**——SHA-256 哈希；仅重新编译变更的 daily 日志
- **并发安全**——PID 锁 + stale 检测；同时只运行一个编译
- **持久任务队列**——离线容错；延迟 LLM 任务在下次会话时排空

### 搜索与检索
- **Generation-consistent retrieval**：一个经过验证的不可变 generation 可将 FTS、vectors、graph、tiers 和 evidence 绑定到同一 source snapshot
- **如实的 retrieval trace**：结果报告 requested/effective mode、实际使用的 signals、generation、reranker 状态和 fallback 原因
- **可用时进行 Triple-fusion**：BM25（FTS5）+ Vector（sentence-transformers）+ evidence-backed Graph-neighbor RRF
- **加权 RRF**：BM25=2.0、Vector=1.0、Graph=0.5——防止已知项查询回归
- **Title + filename 提升**——文件名精确匹配直接短路到 rank 1
- **Typed-provenance 排序**——`source_authority: user` 高于 `ai-derived` / `inferred`
- **时间查询**——`--as-of YYYY-MM-DD` 按 `valid_to` frontmatter 过滤
- **本地检索模式**——小规模直接读取页面，始终可用的 SQLite FTS5 BM25，以及可选的 vectors/LanceDB + graph + reranker 混合检索
- **Grounded QA**——检索到的 source span 带有 citation ID、路径、source/span 哈希、revision 及 byte/line 范围；证据不足、冲突或超出时间范围时会拒答

### 主动智能
- **Guardrails**——在 SessionStart 自动注入已学习的纠正（防止重复犯错）
- **Advisory**——呈现开放线程、最近决策、lint 告警、跨项目洞察
- **元认知上下文**——vault 清单、编译积压、flush 层级分布
- **反馈捕获**——检测记录中的纠正/偏好，保存为提升候选

### 多项目与多智能体
- **一个 vault，多个项目**——5 步 collision-safe slug 系统，每个项目独立的 `state.md`
- **项目引导**——从 git 历史、README、技术栈自动生成上下文
- **Blackboard 协议**——并行智能体认领任务、信号完成、检测冲突
- **循环检测器**——标记重复编辑循环（fix → review → redo）
- **智能体时间线**——归因：哪个智能体何时做了什么决策

### 维护
- **14 项 lint 检查（13 项结构性 + 1 项 LLM 判定矛盾）**——损坏的 wikilinks、孤儿页面、缺失 frontmatter、无效 supersede 链、时间有效性、gap、稀疏页面、缺失来源、矛盾
- **类型感知归档**——debugging 60 天、patterns 180 天、decisions 永不
- **Nightly + weekly 计划**——编译、lint、归档、OKF 迁移（Windows 上 Task Scheduler，Unix 上 cron）
- **OKF v0.1 frontmatter**——`type`、`confidence`、`source_authority`、`supersede` 字段；从遗留页面自动迁移

### 基础设施
- **5 个 LLM 后端**（自动检测）：OpenCode → Codex → Claude CLI → OpenAI → Ollama
- **跨平台**：Windows、macOS、Linux、WSL2
- **本地且零 daemon**——安装基线包含 MCP 包；vector search 和 Cognee 仍为可选项
- **4688 个回归测试**，CI 在 Ubuntu + Windows + macOS 上通过，Python 3.10 + 3.13
- **Pre-commit 钩子**：ruff（静态分析）+ 结构 lint + gitleaks（密钥扫描）

---

## 快速开始

### 前置条件

- Python 3.10+
- git
- 一个你已在使用的 AI 智能体（OpenCode、Codex、Claude Code、Cursor 或 Antigravity）

### 安装（一条命令）

**macOS / Linux / WSL2:**
```bash
curl -fsSL https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.sh | bash
```

**Windows:**
```powershell
irm https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.ps1 | iex
```

> **生产环境提示：** 上方的 `main` 分支 URL 可能会变化。对于生产或审计部署，请改用特定 release 标签的 URL，例如：
> - **macOS / Linux / WSL2:** `https://raw.githubusercontent.com/Ekgardt/llm-wiki/v4.0.0/install.sh`
> - **Windows:** `https://raw.githubusercontent.com/Ekgardt/llm-wiki/v4.0.0/install.ps1`

安装程序会：
1. 检查前置条件（Python 3.10+、git）
2. 如缺失则安装 `uv`（快速 Python 包管理器）
3. 同步 locked 基线依赖（`uv sync --locked --extra mcp-server`）
4. 运行测试套件（4688 个测试）
5. 设置 `LLM_WIKI_ROOT` 环境变量（用户级）
6. 创建运行时目录（`cache/`、`logs/`、`run/`、`cache/cognee/`——gitignored）
7. 注册计划维护（Unix 上 cron，Windows 上 Task Scheduler）
8. 检测你的智能体并完成接入
9. 构建 FTS5 搜索索引

### 手动安装

```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
uv sync --locked --extra mcp-server
uv run pytest -q          # 收集 4688 个测试
```

### 验证可用

```bash
uv run python scripts/search_memory.py "auth"
uv run python scripts/lookup_mode.py
```

---

## 接入智能体

LLM Wiki 在安装时自动检测已安装的智能体。以下是接入内容：

| 智能体 | 集成方式 | 如何接入 |
|--------|----------|----------|
| **OpenCode** | MCP + 轻量 JS lifecycle 插件 | MCP 提供读取/操作；插件将事件转发到 `integration_adapter.py` |
| **Codex CLI** | MCP + 轻量包装器 | MCP 提供读取/操作；包装器转发 lifecycle 事件 |
| **Claude Code** | MCP + 轻量 settings.json 钩子 | MCP 提供读取/操作；五个钩子转发 lifecycle 事件 |
| **Cursor** | MCP + 规则文件 | 配置 MCP；复制 `integrations/cursor/rules/llm-wiki.mdc` 作为操作指引 |
| **Antigravity** | MCP + AGENTS.md 片段 | 配置 MCP；复制 `integrations/antigravity/AGENTS.md` 作为操作指引 |
| **Obsidian** | 可选 Markdown viewer | 直接打开 vault；不要求 Obsidian UI 或 ingestion 功能 |

所有智能体共享同一个 vault——Cursor 记录的决策在 OpenCode 的下次会话中可见。

### 可选：语义搜索

用于混合 BM25 + Vector 搜索（即使关键词不匹配也能找到语义相关页面）：

```bash
uv sync --extra semantic
```

### 可选：Cognee 图谱（300+ 页）

用于大规模实体提取 + 关系图：

```bash
uv sync --extra cognee
```

参见 [docs/SETUP-COGNEE.md](docs/SETUP-COGNEE.md) 了解 Ollama 设置。

---

## 架构

```
CODE          scripts/  tests/  docs/  skills/  rules/  integrations/  benchmark/
KNOWLEDGE     knowledge/{daily,notes,projects,raw,inbox,feedback}
RUNTIME       cache/  logs/  run/  cache/cognee/   （gitignored，vault 内）
```

- **CODE**——git 跟踪。流水线、测试、文档、技能、规则、集成。
- **KNOWLEDGE**——git 跟踪（源码中仅公开示例）。完整用户数据位于已安装的 vault 中。Daily 日志和个人页面 gitignored。
- **RUNTIME**——gitignored。搜索索引和日志可丢弃；`run/` 中的事务、队列状态和 undo 映像属于操作状态。
- **权威边界**——Markdown、Git history 和 append-only project journal 是权威来源。FTS、vectors、Evidence Graph 数据库、tiers、telemetry 和 model cache 都是可重建的派生状态。

完整设计原理（7 条公理、系统架构图、记忆分类法、搜索架构）见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

规范结构参考（什么放在哪里、环境变量契约、禁止布局）见 [docs/STRUCTURE.md](docs/STRUCTURE.md)。

---

## Evidence generation 与迁移

`cache/evidence-graph/catalog.sqlite3` 在 `cache/evidence-graph/generations/<generation-id>/` 中选择一个不可变的 active generation。候选 generation 只有在 manifest、source membership、artifact 哈希、数据库完整性和 evidence span 全部验证后才会注册。激活通过 compare-and-swap 更新指针。激活前构建失败或中断时，先前 generation 仍保持 active；active generation 损坏时，会跳过它并使用最新的已验证历史 generation。恢复时可注册完整的 orphan generation，但不会自动激活。

删除 `cache/evidence-graph/` 只会删除派生状态。先停止活动命令，保留 `run/`，并在期望 generation-backed retrieval 前完成重建。在 installed-vault migration evidence 足以证明安全之前，必须保留 legacy `cache/index.sqlite`、`cache/vectors.npy`、`cache/vectors_meta.json` 和 `cache/lancedb/`。如果无法打开已验证 generation，retrieval 会回退到这些 legacy 路径或 lexical/live extraction，并明确报告 fallback。安全 rollback 绝不删除 `knowledge/`、Git history、project journal 或 `run/`。

Model matrix 固定候选 revision，并要求 EN/RU/ZH quality、resource、license 和 Pareto gates 全部通过后才选择 defaults。目前没有选定新的 embedding model 或 reranker：**evidence pending**。现有可选 vector 兼容路径仍使用固定的 legacy model。Token count 标记为 `reported`、`tokenizer`、`estimated`、`mixed` 或 `unknown`；货币成本另行标记为 `reported`、`estimated` 或 `unknown`。UTF-8 byte 估算只用于保守规划，并非独立于 tokenizer 的保证。

真实 Graphify 对比与 model superiority evidence 尚未获得：**evidence pending**。确定性 comparative smoke 只验证 orchestration，不支持质量或 token-ratio 声明。

激活、恢复、rollback、citation 和准确 MCP 行为见 [docs/USER-GUIDE.md](docs/USER-GUIDE.md)。

---

## 可靠记忆操作

Markdown 仍是权威来源。Runtime SQLite 用于协调可恢复写入和排队工作，但不是知识来源。操作数据库使用 rollback-journal、`synchronous=FULL`，当前 SQLite runtime 不使用 WAL。State root 必须位于本地文件系统；网络路径会被拒绝，对云同步目录的检测为 best-effort。

```bash
uv run python scripts/doctor.py
uv run python scripts/doctor.py --repair
uv run python scripts/markdown_transaction.py recover
uv run python scripts/markdown_transaction.py undo <transaction-id>
uv run python scripts/markdown_transaction.py prune --retention-days 30
uv run python scripts/memory_queue.py migrate
uv run python scripts/memory_queue.py work --max-tasks 20 --max-seconds 600 --idle-seconds 2 --lease-seconds 120 --heartbeat-seconds 40 --max-attempts 8 --retry-base-seconds 30 --retry-cap-seconds 3600
uv run python scripts/memory_queue.py redrive <task-id>
uv run python scripts/memory_queue.py purge --terminal-before <ISO-8601> --export <path>
uv run python scripts/archive_daily.py --commit --hot-days 90
uv run python benchmark/run_contradiction_benchmark.py --corpus benchmark/contradiction-v1.json
```

队列采用至少一次投递，因此 handler 使用稳定 operation ID 保证幂等。归档把超过 90 天 hot window 且符合条件的 daily 日志移动到经过验证、未压缩的 BagIt 包，同时保留逻辑 evidence 解析。无法确定或 evaluator 有分歧的 claims 会进入 quarantine；在 frozen benchmark gate 达标之前，semantic supersession 保持禁用。恢复、保留和安全删除流程见 [docs/USER-GUIDE.md](docs/USER-GUIDE.md)。

---

## 基准测试

> **历史 legacy 方法论**：仅在 git-tracked 公共语料上运行 BM25/FTS5，禁用 graph、vectors 和 reranker。`current-generated-v2` 当时包含 112 个确定性查询：精确标题、摘要关键词、部分标题和 slug。`legacy-60-v1.json` 逐字保存原始 60 条查询文本及其 gold path，因此后续页面内容修改不会改变该门禁。忽略的个人页面和 `$LLM_WIKI_ROOT` 不参与，因此 clean clone 可复现相同语料。这不是 LoCoMo 或 LongMemEval；竞争对手数字来自不同数据集。

| 历史指标 | 历史当前 112 | 历史 legacy 60 | agentmemory | Zep | Mem0 |
|------|----------|-----------|-------------|-----|------|
| Recall@1 | **94.6%** | n/a | n/a | n/a | n/a |
| Recall@3 | **100.0%** | n/a | n/a | n/a | n/a |
| Recall@5 | **100.0%** | **100.0%** | 95.2% | 94.7% | 91.6% |
| Recall@10 | **100.0%** | n/a | n/a | n/a | n/a |
| MRR | **0.9702** | **0.9694** | 0.882 | n/a | n/a |
| 延迟 p50 | **6.3ms** | n/a | 14ms | 155ms | 880ms |

这些是 legacy runner 的历史结果。默认命令现在运行 frozen retrieval-v2 benchmark。只有单独使用 `--legacy-only` 才会选择旧 gate；它与 `--semantic` 或 `--report` 组合时会以失败关闭。

运行 retrieval-v2：`uv run python benchmark/run_benchmark.py`

复现旧 gate：`uv run python benchmark/run_benchmark.py --legacy-only`

### MCP 智能体接口

本地 stdio MCP 服务器提供 **12 个 task-shaped 工具**，包括 `doctor`，并统一使用 response envelope 和 health/context resources。`find_dead_code(directory)` 返回保守候选项，`get_architecture(directory)` 返回入口点、路由、基于 canonical symbol ID 的热点和社区。文件系统分析要求显式提供存在的非根目录，且绝不回退到进程 CWD。

---

## 对比

| 能力 | LLM Wiki | agentmemory | ReMe | akitaonrails |
|------|----------|-------------|------|--------------|
| Markdown 优先 | 是 | 否 | 是 | 是 |
| 多智能体（3+ 工具） | 是（5） | 是（32+ via MCP） | 仅 Claude | 是（12+） |
| IDE 支持 | Cursor + Antigravity；Obsidian 为可选 viewer | 否 | 否 | 否 |
| 编译而非检索 | 是 | 否 | 否 | 否 |
| VERIFY-BEFORE-WRITE | 是 | 否 | 否 | 否 |
| Guardrails（学习纠正） | 是 | 否 | 否 | 否 |
| Blackboard 协调 | 是 | 否 | 否 | 否 |
| 循环检测 | 是 | 否 | 否 | 否 |
| 智能体时间线 | 是 | 否 | 否 | 否 |
| 反馈学习 | 是 | 否 | 否 | 否 |
| 本地 / 零 daemon | 是 | 否（Docker） | 否（pip） | 否（Rust） |
| 时间有效性（`valid_to`） | 是 | 否 | 否 | 否 |
| Typed-provenance 排序 | 是 | 否 | 否 | 否 |

---

## 贡献

欢迎贡献。接受标准是"这是否能在真实的多智能体工作流中存活？"

参见 [CONTRIBUTING.md](CONTRIBUTING.md)：
- 开发环境设置
- 发布检查清单（README i18n 同步、CHANGELOG、版本提升）
- 编码标准（ruff、pytest、pre-commit）
- 如何添加新的智能体集成

---

## 致谢

- [Karpathy LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)——"编译而非检索"模式
- [Harrison Chase "Wiki Memory"](https://blog.langchain.dev/wiki-memory/)——智能体维护的文件
- [Google OKF spec](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)——厂商中立的 Markdown 知识格式
- [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)——capture/compact/subagent 模式
- [VEP Semantic DNA](https://vep.live)——confidence/supersede/temporal 生命周期

---

## 许可证

[MIT](LICENSE)
