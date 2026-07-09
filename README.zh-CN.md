# LLM Wiki — AI 智能体记忆系统

![CI](https://github.com/Ekgardt/llm-wiki/actions/workflows/tests.yml/badge.svg)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**面向管理多个 AI 智能体的独立开发者的主动式记忆系统。Markdown 优先。零云成本。Recall@5 = 100%。$0/月。**

语言：[English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

---

## 这是什么

你的记忆就是**磁盘上的 markdown 文件**。不是云端、不是向量数据库、不是订阅服务。普通的 `.md` 文件，可在 Obsidian 中阅读、用 `git diff` 追踪、完全归你所有。

系统使用你**已有的** LLM 订阅（OpenCode、Codex CLI、Claude Code）来分类会话、编译知识并提供主动上下文。

## 安装（一条命令）

**macOS / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.sh | bash
```

**Windows:**
```powershell
irm https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.ps1 | iex
```

## 性能指标

| 指标 | 结果 |
|---|---|
| Recall@2 | **100%** |
| Recall@5 | **100%** |
| MRR | **0.942** |
| 延迟 p50 | **41ms** |
| 月费用 | **$0** |

## 核心功能

- **三重混合搜索**：BM25 + 向量 + 图邻居（加权 RRF 融合）
- **Guard rails**：会话开始时自动注入学到的规则
- **Blackboard**：并行智能体协调协议
- **Loop detector**：防止无限"修复 → 审查 → 重做"循环
- **Agent timeline**："谁做了什么决定，何时做的"
- **Feedback learning**：系统从你的纠正中自我学习
- **5 个 LLM 后端**：OpenCode → Codex → Claude → OpenAI → Ollama（自动检测）
- **Persistent queue**：离线任务持久化，下次会话自动处理
- **OKF v0.1**：100% 符合开放知识格式
- **155 个测试**，CI 在 Ubuntu 上通过

## 工作原理

```
在 OpenCode / Codex / Claude Code 中正常工作
            ↓
系统静默记录操作 + 分类会话
            ↓
后台编译将原始日志转化为知识页面
            ↓
下次会话：guard rails + advisory + 上下文 — 全自动
            ↓
智能体从上次停止处继续 — 无需重复解释
```

## 支持的智能体

| 智能体 | 集成方式 |
|---|---|
| OpenCode | 插件（JS，自动加载） |
| Codex CLI | PowerShell 封装 |
| Claude Code | 钩子（settings.json） |
| Cursor | 规则文件 |
| Antigravity | AGENTS.md |

## 许可证

[MIT](LICENSE) — 随意使用。
