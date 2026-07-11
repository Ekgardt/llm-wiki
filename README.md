# LLM Wiki

[![Tests](https://img.shields.io/badge/tests-281%20passing-brightgreen.svg)](https://github.com/Ekgardt/llm-wiki/actions)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-3.4.0-blue.svg)](CHANGELOG.md)

**A local-first memory system for AI agents. Markdown files, git-tracked, zero cloud dependencies.**

LLM Wiki gives every AI coding agent you use — OpenCode, Codex, Claude Code, Cursor, Antigravity — a shared, persistent knowledge base that survives across sessions. It captures what you and the agents discuss, compiles durable knowledge pages from session transcripts, and injects the right context at the start of each session so you never re-explain the same thing twice.

Everything lives on your disk as plain markdown: readable in Obsidian, diffable in git, owned entirely by you.

**Languages:** [English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

---

## Table of Contents

- [How it works](#how-it-works)
- [Features](#features)
- [Quick Start](#quick-start)
- [Wire up your agents](#wire-up-your-agents)
- [Architecture](#architecture)
- [Benchmark](#benchmark)
- [Comparison](#comparison)
- [Contributing](#contributing)
- [Credits](#credits)
- [License](#license)

---

## How it works

```
You work normally in your AI agent (OpenCode / Codex / Claude Code / Cursor)
             ↓
Hooks silently capture breadcrumbs + classify sessions (FLUSH_MAJOR/MINOR/OK)
             ↓
Background compile distills daily logs into durable knowledge pages
(with VERIFY-BEFORE-WRITE — citations are checked, not trusted)
             ↓
Next session: guardrails + advisory + metacognitive context auto-injected
             ↓
Agent picks up where you stopped — no re-explaining, no repeated mistakes
```

The system follows the "compile, not retrieve" pattern ([Karpathy, April 2026](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)): raw session signals are captured in real time, then a background LLM pass compiles them into structured knowledge pages rather than relying on raw retrieval at query time.

---

## Features

### Capture pipeline
- **5 Claude Code hooks**: SessionStart, PreCompact, SessionEnd, UserPromptSubmit, PostToolUse — full lifecycle coverage
- **OpenCode plugin** (JS) — session.created, tool.execute.after, session.idle, experimental.session.compacting
- **Codex wrapper** (PowerShell) — wraps `codex` CLI, captures on exit
- **3-tier session classification**: FLUSH_MAJOR (decisions/lessons → triggers compile), FLUSH_MINOR (gotchas → save only), FLUSH_OK (chatter → skip)
- **Non-LLM breadcrumbs** — prompt + tool-usage tagging at ms-latency, no API calls
- **Secret redaction** — API keys, tokens, long base64 stripped before any write

### Compile pipeline
- **JSON-protocol compile** — no agent tool-use required, works with any LLM backend
- **VERIFY-BEFORE-WRITE** — Python-side deterministic citation verification; the LLM cannot fabricate evidence
- **Semantic dedup** — update preferred over create; auto-supersede on contradiction
- **Incremental** — SHA-256 hashing; only changed daily logs are recompiled
- **Concurrency-safe** — PID lock with stale detection; only one compile runs at a time
- **Persistent task queue** — offline-tolerant; deferred LLM work drains on next session

### Search and retrieval
- **Triple-fusion search**: BM25 (FTS5) + Vector (sentence-transformers) + Graph-neighbor (wikilink RRF)
- **Weighted RRF**: BM25=2.0, Vector=1.0, Graph=0.5 — prevents regression on known-item queries
- **Title + filename boost** — exact filename match short-circuits to rank 1
- **Typed-provenance ranking** — `source_authority: user` outranks `ai-derived` / `inferred`
- **Temporal queries** — `--as-of YYYY-MM-DD` filters by `valid_to` frontmatter
- **3-tier strategy** — DIRECT (<50 pages, index-only), HYBRID (50–300, +QMD), QMD (>300)

### Proactive intelligence
- **Guardrails** — auto-injects learned corrections at SessionStart (prevents repeating mistakes)
- **Advisory** — surfaces open threads, last decision, lint alerts, cross-project insights
- **Metacognitive context** — vault inventory, compile backlog, flush tier distribution
- **Feedback capture** — detects corrections/preferences in transcripts, saves as promotion candidates

### Multi-project and multi-agent
- **One vault, many projects** — 5-step collision-safe slug system, per-project `state.md`
- **Project bootstrap** — auto-generates context from git history, README, tech stack
- **Blackboard protocol** — parallel agents claim tasks, signal completion, detect conflicts
- **Loop detector** — flags repeated edit cycles (fix → review → redo)
- **Agent timeline** — attribution: which agent decided what and when

### Maintenance
- **14 lint checks (13 structural + 1 LLM-judged contradiction)** — broken wikilinks, orphans, missing frontmatter, invalid supersede chains, temporal validity, gaps, sparse pages, missing sources, contradictions
- **Type-aware archive** — debugging 60d, patterns 180d, decisions never
- **Nightly + weekly schedules** — compile, lint, archive, OKF migration (Task Scheduler on Windows, cron on Unix)
- **OKF v0.1 frontmatter** — `type`, `confidence`, `source_authority`, `supersede` fields; auto-migration from legacy pages

### Infrastructure
- **5 LLM backends** (auto-detected): OpenCode → Codex → Claude CLI → OpenAI → Ollama
- **Cross-platform**: Windows, macOS, Linux, WSL2
- **Zero runtime dependencies** — base install is stdlib-only; sentence-transformers and Cognee are optional
- **281 regression tests**, CI green on Ubuntu + Windows + macOS, Python 3.10 + 3.13
- **Pre-commit hooks**: ruff (static analysis) + structural lint + gitleaks (secret scanning)

---

## Quick Start

### Prerequisites

- Python 3.10+
- git
- An AI agent you already use (OpenCode, Codex, Claude Code, Cursor, or Antigravity)

### Install (one command)

**macOS / Linux / WSL2:**
```bash
curl -fsSL https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.sh | bash
```

**Windows:**
```powershell
irm https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.ps1 | iex
```

> **Production note:** The `main` branch URLs above are mutable. For production or audited deployments, pin to a specific release tag URL instead, e.g. `https://raw.githubusercontent.com/Ekgardt/llm-wiki/v3.4.0/install.sh`.

The installer:
1. Checks prerequisites (Python 3.10+, git)
2. Installs `uv` (fast Python package manager) if missing
3. Syncs dependencies (`uv sync`)
4. Runs the test suite (281 tests collected in 0.26s)
5. Sets `LLM_WIKI_ROOT` environment variable (user scope)
6. Creates runtime dirs (`cache/`, `logs/`, `run/`, `cache/cognee/` — gitignored)
7. Registers scheduled maintenance (cron on Unix, Task Scheduler on Windows)
8. Detects your agents and wires them up
9. Builds the FTS5 search index

### Manual install

```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
uv sync
uv run pytest -q          # 281 tests collected in 0.26s should pass
```

### Verify it works

```bash
uv run python scripts/search_memory.py "auth"
uv run python scripts/lookup_mode.py
```

---

## Wire up your agents

LLM Wiki auto-detects installed agents during install. Here's what gets wired:

| Agent | Integration | How |
|-------|-------------|-----|
| **OpenCode** | JS plugin | Copied to `~/.config/opencode/plugins/llm-wiki-memory.js` |
| **Codex CLI** | PowerShell wrapper | Sourced into `$PROFILE` (Windows) |
| **Claude Code** | settings.json hooks | Merged into `~/.claude/settings.json` (5 hooks: SessionStart, PreCompact, SessionEnd, UserPromptSubmit, PostToolUse) |
| **Cursor** | Rules file | Copy `integrations/cursor/rules/llm-wiki.mdc` manually |
| **Antigravity** | AGENTS.md snippet | Copy `integrations/antigravity/AGENTS.md` manually |
| **Obsidian** | Web Clipper template | Import `integrations/obsidian/Article-to-Inbox.json` |

All agents share the same vault — a decision recorded by Cursor is visible to OpenCode in its next session.

### Optional: semantic search

For hybrid BM25 + Vector search (finds semantically related pages even when keywords don't match):

```bash
uv sync --extra semantic
```

### Optional: Cognee graph (300+ pages)

For entity extraction + relationship graph at scale:

```bash
uv sync --extra cognee
```

See [docs/SETUP-COGNEE.md](docs/SETUP-COGNEE.md) for Ollama setup.

---

## Architecture

```
CODE          scripts/  tests/  docs/  skills/  rules/  integrations/  benchmark/
KNOWLEDGE     knowledge/{daily,notes,projects,raw,inbox,feedback}
RUNTIME       cache/  logs/  run/  cache/cognee/   (gitignored, inside vault)
```

- **CODE** — tracked in git. The pipeline, tests, docs, skills, rules, integrations.
- **KNOWLEDGE** — tracked in git (public examples). Full user data lives in the installed vault. Daily logs and personal pages are gitignored.
- **RUNTIME** — gitignored, regenerated on demand. Search indexes, compile logs, state.json, task queue.

Full design rationale (7 axioms, system architecture diagram, memory taxonomy, search architecture) in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

For the canonical structure reference (what lives where, env contracts, forbidden layouts), see [docs/STRUCTURE.md](docs/STRUCTURE.md).

---

## Benchmark

> **Methodology**: 60 known-item queries (exact title match + summary-derived keywords, not LLM-paraphrased) over 34 curated pages. BM25-only mode (FTS5). This measures "can the system find page X when given its title or summary keywords?" — the most relevant metric for personal knowledge retrieval. It is **not** LoCoMo or LongMemEval (multi-session conversation recall). Competitor numbers are from different datasets and are not directly comparable. Run `benchmark/run_benchmark.py` to reproduce.

| Metric | LLM Wiki v3.3 | agentmemory | Zep | Mem0 |
|--------|---------------|-------------|-----|------|
| Recall@1 | **95.0%** | n/a | n/a | n/a |
| Recall@3 | **100%** | n/a | n/a | n/a |
| Recall@5 | **100%** | 95.2% | 94.7% | 91.6% |
| Recall@10 | **100%** | n/a | n/a | n/a |
| MRR | **0.9667** | 0.882 | n/a | n/a |
| Latency p50 | **6ms** | 14ms | 155ms | 880ms |
| Token cost/search | **0** | ~1900 | $$ | $$ |

100% Recall@5 is achievable on small curated datasets; expect 85–95% on 500+ pages. Triple-fusion (BM25 + Vector + Graph) adds semantic recall on top of these BM25-only numbers.

Reproduce: `uv run python benchmark/run_benchmark.py`

---

## Comparison

| Capability | LLM Wiki | agentmemory | ReMe | akitaonrails |
|------------|----------|-------------|------|--------------|
| Markdown-first | Yes | No | Yes | Yes |
| Multi-agent (3+ tools) | Yes (5) | Yes (32+ via MCP) | Claude only | Yes (12+) |
| IDE support | Cursor + Antigravity + Obsidian | No | No | No |
| Compile-not-retrieve | Yes | No | No | No |
| VERIFY-BEFORE-WRITE | Yes | No | No | No |
| Guardrails (learned corrections) | Yes | No | No | No |
| Blackboard coordination | Yes | No | No | No |
| Loop detection | Yes | No | No | No |
| Agent timeline | Yes | No | No | No |
| Feedback learning | Yes | No | No | No |
| Zero runtime dependencies | Yes | No (Docker) | No (pip) | No (Rust) |
| Temporal validity (`valid_to`) | Yes | No | No | No |
| Typed provenance ranking | Yes | No | No | No |

---

## Contributing

Contributions are welcome. The bar is "does this survive contact with an actual multi-agent workflow?"

See [CONTRIBUTING.md](CONTRIBUTING.md) for:
- Development setup
- Release checklist (README i18n sync, CHANGELOG, version bump)
- Coding standards (ruff, pytest, pre-commit)
- How to add a new agent integration

---

## Credits

- [Karpathy's LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — the "compile, not retrieve" pattern
- [Harrison Chase's "Wiki Memory"](https://blog.langchain.dev/wiki-memory/) — agent-maintained files
- [Google's OKF spec](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) — vendor-neutral markdown knowledge format
- [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — capture/compact/subagent patterns
- [VEP Semantic DNA](https://vep.live) — confidence/supersede/temporal lifecycle

---

## License

[MIT](LICENSE)
