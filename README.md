# LLM Wiki

[![Tests](https://img.shields.io/badge/tests-1804%20collected-brightgreen.svg)](https://github.com/Ekgardt/llm-wiki/actions)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-4.0.0-blue.svg)](CHANGELOG.md)

**A local-first memory system for AI agents. Markdown files, git-tracked, zero cloud dependencies.**

LLM Wiki gives every AI coding agent you use — OpenCode, Codex, Claude Code, Cursor, Antigravity — one MCP-first interface to a shared, persistent knowledge base. MCP handles reads and actions; thin native lifecycle adapters capture session events that MCP cannot observe. Durable knowledge survives across sessions so you never re-explain the same thing twice.

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
Your agent reads and acts through the local MCP server
             ↓
Thin hooks/plugins send lifecycle events through integration_adapter.py
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
- **Thin lifecycle adapters**: Claude Code hooks, the OpenCode plugin, and the Codex wrapper normalize events through `integration_adapter.py`
- **3-tier session classification**: FLUSH_MAJOR (decisions/lessons → triggers compile), FLUSH_MINOR (gotchas → save only), FLUSH_OK (chatter → skip)
- **Non-LLM breadcrumbs** — prompt + tool-usage tagging at ms-latency, no API calls
- **Secret redaction** — API keys, tokens, long base64 stripped before any write

### Agent-native interface
- **MCP-first access** — 12 task-shaped local tools for recall, context, decisions, maintenance, code intelligence, and `doctor`
- **Uniform response envelope** — every tool reports schema version, freshness, evidence quality, warnings, and data; MCP resources expose health and context
- **Automatic health** — SessionStart stays quiet when healthy and injects only degraded/error findings; `doctor(repair=true)` limits repairs to safe, idempotent local actions

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
- **Local retrieval modes** — direct page reads at small scale, SQLite FTS5 BM25 as the always-available base, and optional vectors/LanceDB + graph + reranker for hybrid retrieval

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
- **Local and zero-daemon** — the installed baseline includes the MCP package; vector search and Cognee remain optional
- **1804 regression tests**, CI green on Ubuntu + Windows + macOS, Python 3.10 + 3.13
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

> **Production note:** The `main` branch URLs above are mutable. For production or audited deployments, pin to a specific release tag URL instead:
> - **macOS / Linux / WSL2:** `https://raw.githubusercontent.com/Ekgardt/llm-wiki/v4.0.0/install.sh`
> - **Windows:** `https://raw.githubusercontent.com/Ekgardt/llm-wiki/v4.0.0/install.ps1`

The installer:
1. Checks prerequisites (Python 3.10+, git)
2. Installs `uv` (fast Python package manager) if missing
3. Syncs locked baseline dependencies (`uv sync --locked --extra mcp-server`)
4. Runs the test suite (1804 tests collected)
5. Sets `LLM_WIKI_ROOT` environment variable (user scope)
6. Creates runtime dirs (`cache/`, `logs/`, `run/`, `cache/cognee/` — gitignored)
7. Registers scheduled maintenance (cron on Unix, Task Scheduler on Windows)
8. Detects your agents and wires them up
9. Builds the FTS5 search index

### Manual install

```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
uv sync --locked --extra mcp-server
uv run pytest -q          # collects 1804 tests
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
| **OpenCode** | MCP + thin JS lifecycle plugin | MCP provides reads/actions; the plugin forwards lifecycle events to `integration_adapter.py` |
| **Codex CLI** | MCP + thin wrapper | MCP provides reads/actions; the wrapper forwards lifecycle events |
| **Claude Code** | MCP + thin settings.json hooks | MCP provides reads/actions; five hooks forward lifecycle events |
| **Cursor** | MCP + rules file | Configure MCP; copy `integrations/cursor/rules/llm-wiki.mdc` for guidance |
| **Antigravity** | MCP + AGENTS.md snippet | Configure MCP; copy `integrations/antigravity/AGENTS.md` for guidance |
| **Obsidian** | Optional Markdown viewer | Open the vault directly; no Obsidian UI or ingestion feature is required |

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
- **RUNTIME** — gitignored. Search indexes and logs are disposable; transactions, queue state, and undo images under `run/` are operational state.

Full design rationale (7 axioms, system architecture diagram, memory taxonomy, search architecture) in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

For the canonical structure reference (what lives where, env contracts, forbidden layouts), see [docs/STRUCTURE.md](docs/STRUCTURE.md).

---

## Reliable memory operations

Markdown remains authoritative. Runtime SQLite coordinates recoverable writes and queued work but is not a knowledge source. Operational databases use rollback-journal mode, `synchronous=FULL`, and no WAL on the current SQLite runtime. Keep the state root on a local filesystem; network paths are rejected and cloud-synchronized folder detection is best-effort.

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

Queue delivery is at least once, so handlers use stable operation IDs for idempotency. Archives move eligible daily logs older than the 90-day hot window into verified, uncompressed BagIt packages while preserving logical evidence resolution. Uncertain or evaluator-disputed claims are quarantined; semantic supersession stays disabled until the frozen benchmark gate is met. See [docs/USER-GUIDE.md](docs/USER-GUIDE.md) for recovery, retention, and deletion safety.

---

## Benchmark

> **Methodology**: BM25-only FTS5 over the git-tracked public corpus, with graph, vectors, and reranking disabled. `current-generated-v2` has 112 deterministic known-item queries: exact title, summary keywords, partial title, and slug. `legacy-60-v1.json` stores the original 60 query texts and gold paths verbatim, so later page edits cannot change that gate. Ignored personal pages and `$LLM_WIKI_ROOT` are excluded, so a clean clone reproduces the same corpus. This is not LoCoMo or LongMemEval; competitor rows use different datasets.

| Metric | Current 112 | Legacy 60 | agentmemory | Zep | Mem0 |
|--------|-------------|-----------|-------------|-----|------|
| Recall@1 | **94.6%** | n/a | n/a | n/a | n/a |
| Recall@3 | **100.0%** | n/a | n/a | n/a | n/a |
| Recall@5 | **100.0%** | **100.0%** | 95.2% | 94.7% | 91.6% |
| Recall@10 | **100.0%** | n/a | n/a | n/a | n/a |
| MRR | **0.9702** | **0.9694** | 0.882 | n/a | n/a |
| Latency p50 | **6.3ms** | n/a | 14ms | 155ms | 880ms |

Regression gates are Recall@5 >=95% for the expanding current corpus and 100% for `legacy-60-v1`. The old 60-query report remains directly visible rather than being silently replaced by the larger corpus.

Reproduce: `uv run python benchmark/run_benchmark.py`

### MCP agent interface

The local stdio MCP server exposes **12 task-shaped tools**, including `doctor`, with one response envelope and health/context resources. `find_dead_code(directory)` returns conservative candidates, while `get_architecture(directory)` reports entry points, routes, canonical-symbol hotspots, and communities. Filesystem analysis requires an explicit existing non-root directory and never falls back to the process CWD.

---

## Comparison

| Capability | LLM Wiki | agentmemory | ReMe | akitaonrails |
|------------|----------|-------------|------|--------------|
| Markdown-first | Yes | No | Yes | Yes |
| Multi-agent (3+ tools) | Yes (5) | Yes (32+ via MCP) | Claude only | Yes (12+) |
| IDE support | Cursor + Antigravity; optional Obsidian viewer | No | No | No |
| Compile-not-retrieve | Yes | No | No | No |
| VERIFY-BEFORE-WRITE | Yes | No | No | No |
| Guardrails (learned corrections) | Yes | No | No | No |
| Blackboard coordination | Yes | No | No | No |
| Loop detection | Yes | No | No | No |
| Agent timeline | Yes | No | No | No |
| Feedback learning | Yes | No | No | No |
| Local / zero-daemon | Yes | No (Docker) | No (pip) | No (Rust) |
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
