# LLM Wiki — AI 智能体记忆系统

**语言：** [English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

![CI](https://github.com/Ekgardt/llm-wiki/actions/workflows/tests.yml/badge.svg)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-176%20passing-brightgreen.svg)](https://github.com/Ekgardt/llm-wiki/actions)
[![Benchmark](https://img.shields.io/badge/Recall%405-100%25-blue.svg)](benchmark/run_benchmark.py)

**面向管理多个 AI 智能体的独立开发者的主动式记忆系统。Markdown 优先。零云成本。Recall@5 = 100%。$0/月。**

多数记忆产品（Mem0、Zep、Letta）要把数据放进它们的云并收费。本项目把一切保存在**你磁盘上的 markdown**——可用 Obsidian 阅读、`git diff` 追踪、完全归你所有——并复用你**已有的** LLM 订阅（OpenCode、Codex CLI、Claude Code）。

```
在 OpenCode / Codex / Claude Code 中正常工作
            ↓
系统静默记录 breadcrumbs 并分类会话
            ↓
后台 compile 将日志提炼为知识页面
            ↓
下次会话：guard rails + advisory + 上下文自动注入
            ↓
智能体从上次停下的地方继续——无需重复解释
```

---

## 基准测试（2026 年 7 月）

> **方法说明：** 在公开 sample 笔记上做 known-item retrieval  
> （实时查询数见 `benchmark/run_benchmark.py`）。**不是** LoCoMo / LongMemEval。  
> 小规模 curated 集可达 100% Recall@5；500+ 页预期约 85–95%。  
> 竞品数字来自不同数据集，不可直接对比。

| 指标 | **LLM Wiki v3.3.1** | agentmemory | Zep | Mem0 |
|---|---|---|---|---|
| **Recall@2** | **100%** | n/a | n/a | n/a |
| **Recall@5** | **100%** | 95.2% | 94.7% | 91.6% |
| **Recall@10** | **100%** | 98.6% | n/a | n/a |
| **MRR** | **0.942** | 0.882 | n/a | n/a |
| **延迟 p50** | **41ms** | 14ms | 155ms | 880ms |
| **检索 token 成本** | **0** | ~1900 | $$ | $$ |
| **月费用** | **$0** | ~$10 | $200+ | $50–150 |

复现：`uv run python benchmark/run_benchmark.py --semantic`

---

## 核心能力

### 流水线
- **SKEPTICAL 编译器** + VERIFY-BEFORE-WRITE（Python 侧核对引用）
- **三级 FLUSH**（MAJOR / MINOR / OK）
- **JSON 编译协议** — 任意 LLM 后端
- **COMPILE_AUDIT** — 引用校验 / 去重 / 矛盾

### 检索
- **BM25 + 向量 + 图邻居**（加权 RRF）
- 标题/文件名加权、`--project`、`--since` / `--as-of`
- `source_authority` 参与排序

### 主动智能
- Guard rails、advisory、元认知上下文
- Feedback 捕获 → 晋升为知识页

### 多智能体
- Blackboard、loop detector、agent timeline

### 基础设施
- 5 后端：OpenCode → Codex → Claude → OpenAI → Ollama
- 离线任务队列、编译 PID 锁
- 夜间/每周维护（cron / Task Scheduler）
- OpenCode 插件、Codex wrapper、Claude hooks（含 settings 合并）
- Cursor + Antigravity
- **176 个 pytest**，CI + gitleaks

---

## 安装（一条命令）

**macOS / Linux / WSL2：**
```bash
curl -fsSL https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.sh | bash
```

**Windows：**
```powershell
irm https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.ps1 | iex
```

安装器会：装依赖、跑 **176** 测试、设置 `LLM_WIKI_ROOT` / `LLM_WIKI_STATE_ROOT`、定时任务、检测代理、Claude merge、构建搜索索引。

**手动：**
```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
uv sync
uv run pytest -q          # 176 tests
```

### 智能体接入
| 智能体 | 方式 |
|---|---|
| OpenCode | 插件 → `~/.config/opencode/plugins/` |
| Codex | wrapper / `codex_memory.py` |
| Claude Code | hooks + `merge_claude_settings.py` |
| Cursor | `integrations/cursor/rules/` |
| Antigravity | `integrations/antigravity/AGENTS.md` |

可选语义检索：`uv pip install sentence-transformers`

---

## 架构（三区）

```
CODE         scripts/  tests/  docs/  skills/  rules/  integrations/
KNOWLEDGE    knowledge/{daily,notes,projects,raw,inbox,feedback}
RUNTIME      $LLM_WIKI_STATE_ROOT/{run,logs,cache}   # 在 vault 外
```

```
原始材料 / 会话
    ↓ capture
knowledge/daily/  （只追加）
    ↓ compile
knowledge/notes/  （持久知识页）
    ↓ search（BM25 + Vector + Graph）
会话上下文（guard rails + advisory）
    ↓ SessionStart
智能体看到规则、决策、open threads
```

---

## 公开历史说明

开源前用 `git-filter-repo` 清理了个人数据。公开 commit 数 ≠ 实际开发量。  
**176 个测试**验证代码。`knowledge/daily/` 样例为合成 Evidence，不含私人会话。

---

## 平台

- **Windows** — 完整支持（Task Scheduler、PowerShell）
- **macOS/Linux** — Python 脚本 + cron/systemd
- **OpenCode** — 全平台（JS）
- **Codex wrapper** — PowerShell；Unix 可用 alias 调 `codex_memory.py`

---

## 对比

| | **LLM Wiki v3.3.1** | agentmemory | ReMe |
|---|---|---|---|
| Markdown 优先 | ✅ | ❌ | ✅ |
| 多工具 | ✅ OpenCode+Codex+Claude | MCP | 仅 Claude |
| IDE | ✅ Cursor+Antigravity | ❌ | ❌ |
| Guard rails / blackboard / loop | ✅ | ❌ | ❌ |
| $0/月、无云锁定 | ✅ | ✅ | ✅ |
| Recall@5 | **100%** | 95.2% | n/a |

---

## 致谢

- [Karpathy LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
- [Harrison Chase — Wiki Memory](https://blog.langchain.dev/wiki-memory/)
- [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
- [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

## 许可证

[MIT](LICENSE)
