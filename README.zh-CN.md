# LLM Wiki

[![Tests](https://img.shields.io/badge/tests-281%20passing-brightgreen.svg)](https://github.com/Ekgardt/llm-wiki/actions)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-3.4.0-blue.svg)](CHANGELOG.md)

**面向 AI 智能体的本地优先记忆系统。Markdown 文件，git 版本控制，零云依赖。**

LLM Wiki 为你使用的每一个 AI 编码智能体——OpenCode、Codex、Claude Code、Cursor、Antigravity——提供一个共享的、持久的知识库，跨会话保留。系统捕获你和智能体讨论的内容，将会话记录编译为持久知识页面，并在每次会话开始时注入合适的上下文，让你无需重复解释同样的事情。

一切以纯 Markdown 文件形式存储在你的磁盘上：可在 Obsidian 中阅读，可用 git 对比，完全归你所有。

**语言：** [English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

---

## 目录

- [工作原理](#工作原理)
- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [接入智能体](#接入智能体)
- [架构](#架构)
- [基准测试](#基准测试)
- [对比](#对比)
- [贡献](#贡献)
- [致谢](#致谢)
- [许可证](#许可证)

---

## 工作原理

```
你在 AI 智能体中正常工作（OpenCode / Codex / Claude Code / Cursor）
             ↓
钩子静默捕获 breadcrumbs + 分类会话（FLUSH_MAJOR/MINOR/OK）
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
- **5 个 Claude Code 钩子**：SessionStart、PreCompact、SessionEnd、UserPromptSubmit、PostToolUse——完整生命周期覆盖
- **OpenCode 插件**（JS）——session.created、tool.execute.after、session.idle、experimental.session.compacting
- **Codex 包装器**（PowerShell）——包装 `codex` CLI，退出时捕获
- **3 级会话分类**：FLUSH_MAJOR（决策/经验→触发编译）、FLUSH_MINOR（注意事项→仅保存）、FLUSH_OK（闲聊→跳过）
- **非 LLM breadcrumbs**——prompt 和 tool 调用标记，毫秒级延迟，无 API 调用
- **密钥脱敏**——API 密钥、令牌、长 base64 字符串在任何写入前清除

### 编译流水线
- **JSON 协议编译**——无需智能体 tool-use，适用于任何 LLM 后端
- **VERIFY-BEFORE-WRITE**——Python 端确定性引用验证；LLM 无法伪造证据
- **语义去重**——优先 update 而非 create；矛盾时自动 supersede
- **增量编译**——SHA-256 哈希；仅重新编译变更的 daily 日志
- **并发安全**——PID 锁 + stale 检测；同时只运行一个编译
- **持久任务队列**——离线容错；延迟 LLM 任务在下次会话时排空

### 搜索与检索
- **Triple-fusion 搜索**：BM25（FTS5）+ Vector（sentence-transformers）+ Graph-neighbor（wikilink RRF）
- **加权 RRF**：BM25=2.0、Vector=1.0、Graph=0.5——防止已知项查询回归
- **Title + filename 提升**——文件名精确匹配直接短路到 rank 1
- **Typed-provenance 排序**——`source_authority: user` 高于 `ai-derived` / `inferred`
- **时间查询**——`--as-of YYYY-MM-DD` 按 `valid_to` frontmatter 过滤
- **3 级策略**——DIRECT（<50 页，仅索引）、HYBRID（50–300，+QMD）、QMD（>300）

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
- **零运行时依赖**——基础安装仅用标准库；sentence-transformers 和 Cognee 为可选
- **281 个回归测试**，CI 在 Ubuntu + Windows + macOS 上通过，Python 3.10 + 3.13
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
> - **macOS / Linux / WSL2:** `https://raw.githubusercontent.com/Ekgardt/llm-wiki/v3.4.0/install.sh`
> - **Windows:** `https://raw.githubusercontent.com/Ekgardt/llm-wiki/v3.4.0/install.ps1`

安装程序会：
1. 检查前置条件（Python 3.10+、git）
2. 如缺失则安装 `uv`（快速 Python 包管理器）
3. 同步依赖（`uv sync`）
4. 运行测试套件（281 个测试）
5. 设置 `LLM_WIKI_ROOT` 环境变量（用户级）
6. 创建运行时目录（`cache/`、`logs/`、`run/`、`cache/cognee/`——gitignored）
7. 注册计划维护（Unix 上 cron，Windows 上 Task Scheduler）
8. 检测你的智能体并完成接入
9. 构建 FTS5 搜索索引

### 手动安装

```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
uv sync
uv run pytest -q          # 281 个测试应通过
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
| **OpenCode** | JS 插件 | 复制到 `~/.config/opencode/plugins/llm-wiki-memory.js` |
| **Codex CLI** | PowerShell 包装器 | 加入 `$PROFILE`（Windows） |
| **Claude Code** | settings.json 钩子 | 合并到 `~/.claude/settings.json`（5 个钩子：SessionStart、PreCompact、SessionEnd、UserPromptSubmit、PostToolUse） |
| **Cursor** | 规则文件 | 手动复制 `integrations/cursor/rules/llm-wiki.mdc` |
| **Antigravity** | AGENTS.md 片段 | 手动复制 `integrations/antigravity/AGENTS.md` |
| **Obsidian** | Web Clipper 模板 | 导入 `integrations/obsidian/Article-to-Inbox.json` |

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
- **RUNTIME**——gitignored，按需重新生成。搜索索引、编译日志、state.json、任务队列。

完整设计原理（7 条公理、系统架构图、记忆分类法、搜索架构）见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

规范结构参考（什么放在哪里、环境变量契约、禁止布局）见 [docs/STRUCTURE.md](docs/STRUCTURE.md)。

---

## 基准测试

> **方法论**：60 个已知项查询（精确标题匹配 + 摘要派生关键词，非 LLM 改写），覆盖 34 个精选页面。仅 BM25 模式（FTS5）。测量"系统能否在给定标题或摘要关键词时找到页面 X？"——对个人知识检索最相关的指标。这**不是** LoCoMo 或 LongMemEval（多会话对话召回）。竞争对手数据来自不同数据集，不可直接比较。运行 `benchmark/run_benchmark.py` 复现。

| 指标 | LLM Wiki v3.3 | agentmemory | Zep | Mem0 |
|------|---------------|-------------|-----|------|
| Recall@1 | **95.0%** | n/a | n/a | n/a |
| Recall@3 | **100%** | n/a | n/a | n/a |
| Recall@5 | **100%** | 95.2% | 94.7% | 91.6% |
| Recall@10 | **100%** | n/a | n/a | n/a |
| MRR | **0.9667** | 0.882 | n/a | n/a |
| 延迟 p50 | **6ms** | 14ms | 155ms | 880ms |
| Token 成本/搜索 | **0** | ~1900 | $$ | $$ |

100% Recall@5 在小型精选数据集上可实现；500+ 页时预期 85–95%。Triple-fusion（BM25 + Vector + Graph）在这些仅 BM25 的数字基础上增加了语义召回。

复现：`uv run python benchmark/run_benchmark.py`

---

## 对比

| 能力 | LLM Wiki | agentmemory | ReMe | akitaonrails |
|------|----------|-------------|------|--------------|
| Markdown 优先 | 是 | 否 | 是 | 是 |
| 多智能体（3+ 工具） | 是（5） | 是（32+ via MCP） | 仅 Claude | 是（12+） |
| IDE 支持 | Cursor + Antigravity + Obsidian | 否 | 否 | 否 |
| 编译而非检索 | 是 | 否 | 否 | 否 |
| VERIFY-BEFORE-WRITE | 是 | 否 | 否 | 否 |
| Guardrails（学习纠正） | 是 | 否 | 否 | 否 |
| Blackboard 协调 | 是 | 否 | 否 | 否 |
| 循环检测 | 是 | 否 | 否 | 否 |
| 智能体时间线 | 是 | 否 | 否 | 否 |
| 反馈学习 | 是 | 否 | 否 | 否 |
| 零运行时依赖 | 是 | 否（Docker） | 否（pip） | 否（Rust） |
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
