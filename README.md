# LLM Wiki — Multi-Tool Agent Memory

**Languages:** [English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

![CI](https://github.com/Ekgardt/llm-wiki/actions/workflows/tests.yml/badge.svg)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-160%20passing-brightgreen.svg)](https://github.com/Ekgardt/llm-wiki/actions)
[![Benchmark](https://img.shields.io/badge/Recall%405-100%25-blue.svg)](benchmark/run_benchmark.py)

**The proactive memory system for solo developers managing multiple AI agents. Markdown-first. Zero cloud cost. Recall@5 = 100%. $0/month.**

Most agent memory tools (Mem0, Zep, Letta) want your data in their cloud and a monthly fee. This one keeps everything on your disk as plain markdown — readable in Obsidian, diffable in git, owned by you — and uses the LLM subscriptions you **already have** (OpenCode, Codex CLI, Claude Code) to do the heavy lifting.

```
You work normally in OpenCode / Codex / Claude Code
            ↓
System silently captures breadcrumbs + classifies sessions
            ↓
Detached background compile distills durable knowledge pages
            ↓
Next session: guard rails + advisory + context auto-injected
            ↓
Agent picks up where you stopped — no re-explaining, no repeated mistakes
```

---

## Benchmark (July 2026)

> **Methodology disclosure**: 52 queries (known-item retrieval) over 34 pages.
> This is NOT LoCoMo or LongMemEval (multi-session conversation recall).
> It measures "can the system find page X when given a paraphrased query?" —
> the most relevant metric for personal knowledge retrieval. 100% Recall@5
> is achievable on small curated datasets; expect 85-95% on 500+ pages.
> Competitor numbers (95.2%, 94.7%) are from different datasets and are
> not directly comparable. Run `benchmark/run_benchmark.py` to reproduce.

| Metric | **LLM Wiki v3.3** | agentmemory (24.5k★) | Zep | Mem0 |
|---|---|---|---|---|
| **Recall@2** | **100%** | n/a | n/a | n/a |
| **Recall@5** | **100%** 🥇 | 95.2% | 94.7% | 91.6% |
| **Recall@10** | **100%** | 98.6% | n/a | n/a |
| **MRR** | **0.942** | 0.882 | n/a | n/a |
| **Latency p50** | **41ms** | 14ms | 155ms | 880ms |
| **Token cost/search** | **0** 🥇 | ~1900 | $$ | $$ |
| **Monthly cost** | **$0** | ~$10 | $200+ | $50-150 |

Reproduce: `uv run python benchmark/run_benchmark.py --semantic`

---

## 41 Features

### Core Pipeline
- **SKEPTICAL compiler** with VERIFY-BEFORE-WRITE (Python-side citation verification — LLM cannot fake evidence)
- **3-tier FLUSH classifier** (MAJOR/MINOR/OK) — decides what's worth saving at session end
- **JSON-protocol compile** — no agent tool-use required, works with any LLM backend
- **COMPILE_AUDIT sentinel** — tracks verified citations, dedup checks, stubs skipped, contradictions

### Search & Retrieval
- **Triple-fusion search**: BM25 + Vector (sentence-transformers) + Graph-neighbor (wikilink RRF)
- **Weighted RRF**: BM25 weight=2, Vector weight=1, Graph weight=0.5 (prevents regression)
- **Title + filename boost**: exact match → 10x score, prevents duplicate-page confusion
- **Project-scoped search** (`--project your-app`) — boost current project's pages
- **Temporal queries** (`--since 2026-03`, `--as-of 2026-04-01`) — date range + validity windows
- **Typed provenance ranking** — `source_authority: user` outranks ai-derived/inferred
- **Recall@2 = 100%** — correct page always in top 2 results

### Proactive Intelligence
- **Guard rails** — auto-injects learned corrections at SessionStart (prevents repeating mistakes)
- **Advisory** — open threads, last decision, lint alerts, cross-project insights
- **Metacognitive context** — vault inventory, compile backlog, flush tier distribution
- **Feedback capture** — detects corrections/preferences in session transcripts, saves as candidates
- **Feedback promotion** — promoted candidates become knowledge pages + guard rail rules

### Multi-Agent Coordination
- **Blackboard protocol** — parallel agents claim tasks, signal completion, detect conflicts
- **Loop detector** — prevents infinite "fix → review → redo" cycles (detects repeated edits)
- **Agent timeline** — "who decided what and when" attribution across agents
- **Auto-detect agent strengths** — learns from history, no hardcoded roles

### Infrastructure
- **5 LLM backends**: OpenCode SDK → Codex CLI → Claude CLI → OpenAI API → Ollama (auto-detect)
- **Persistent task queue** — deferred LLM work survives offline, drains on next session
- **Concurrency-safe compile** — PID lock with stale detection
- **Windows Task Scheduler** — nightly (03:00) + weekly (Sunday 04:00), zero manual steps
- **Cross-platform**: macOS, Linux, WSL2, Windows
- **One-command install**: `curl ... | bash` (Unix) or `irm ... | iex` (Windows)
- **3 CLI integrations**: OpenCode plugin, Codex wrapper, Claude Code hooks
- **2 IDE integrations**: Cursor (rules file), Antigravity (AGENTS.md)

### Quality & Standards
- **OKF v0.1 conformant** — 100% of pages have `type:` frontmatter
- **13 lint checks** — broken wikilinks, orphans, missing frontmatter, supersede chains, temporal validity, gaps
- **Temporal validity** — `valid_from`/`valid_to` frontmatter, stale-fact detection
- **Smart auto-archive** — type-aware thresholds (decisions never archive, debugging at 60 days)
- **160 pytest tests**, CI green on Ubuntu

---

## Public history note

Git history was scrubbed via `git-filter-repo` to remove personal project
data (daily logs, project state files) before making the repo public.
The system was developed over multiple sessions; the public commit count
does not reflect total development effort. All 160 tests verify the
code works. Sample `knowledge/daily/` fixtures restore Evidence links
without publishing private session content.

---

## Cross-platform notes

- **Windows**: full support (Task Scheduler, PowerShell hooks, native paths)
- **macOS/Linux**: core scripts work (Python cross-platform). Replace
  Task Scheduler with cron or systemd timers. `scripts/scheduled_nightly.py`
  and `scripts/scheduled_weekly.py` are plain Python — callable from any scheduler.
- **OpenCode plugin**: works on all platforms (JS, no OS deps)
- **Codex wrapper**: PowerShell-specific; macOS/Linux users can call
  `codex_memory.py daily-log` from a shell alias

---

## Quick Start (one command)

**macOS / Linux / WSL2:**
```bash
curl -fsSL https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.sh | bash
```

**Windows:**
```powershell
irm https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.ps1 | iex
```

The installer checks prerequisites, installs dependencies, runs 160 tests, sets up scheduled maintenance, and detects your agents automatically.

That's it. The installer:
1. Checks prerequisites (Python 3.10+, git)
2. Installs `uv` if missing (fast Python package manager)
3. Installs dependencies (`uv sync`)
4. Runs 160 tests to verify everything works
5. Sets `LLM_WIKI_ROOT` environment variable
6. Sets up scheduled maintenance (cron on Unix, Task Scheduler on Windows)
7. Detects your agents (OpenCode, Codex, Claude Code, Cursor) and wires them up
8. Builds the FTS5 search index

**Manual install** (if you prefer):
```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
uv sync
uv run pytest -q          # 160 tests should pass
```

### Wire up your tools

**OpenCode** — plugin autoloads from `~/.config/opencode/plugins/`

**Codex CLI** — add to `$PROFILE`:
```powershell
. "LLM-wiki/scripts/codex-memory-wrapper.ps1"
```

**Windows Task Scheduler** (auto-maintenance):
```powershell
$env:LLM_WIKI_ROOT\scripts\install-scheduled-tasks.ps1
```

**Optional: semantic search** (for hybrid BM25+Vector):
```bash
uv pip install sentence-transformers
```

---

## Architecture

```
RAW SOURCES (immutable)
    ↓ ingest
DAILY LOGS (session capture, append-only)
    ↓ compile (FLUSH MAJOR/MINOR → knowledge pages)
KNOWLEDGE PAGES (durable, OKF frontmatter, git-tracked)
    ↓ search (BM25 + Vector + Graph triple RRF)
SESSION CONTEXT (guard rails + advisory + metacognitive)
    ↓ inject at SessionStart
AGENT SEES: rules + decisions + open threads + project state
```

---

## Comparison

| | **LLM Wiki v3.3** | agentmemory | ReMe | akitaonrails |
|---|---|---|---|---|
| Markdown-first | ✅ | ❌ | ✅ | ✅ |
| Multi-tool (3+) | ✅ OpenCode+Codex+Claude | 32+ (MCP) | Claude only | 12+ |
| IDE support | ✅ Cursor+Antigravity | ❌ | ❌ | ❌ |
| Guard rails | ✅ | ❌ | ❌ | ❌ |
| Blackboard coordination | ✅ | ❌ | ❌ | ❌ |
| Loop detection | ✅ | ❌ | ❌ | ❌ |
| Agent timeline | ✅ | ❌ | ❌ | ❌ |
| Feedback learning | ✅ | ❌ | ❌ | ❌ |
| Zero runtime deps | ✅ | ❌ Docker | ❌ pip | ❌ Rust |
| $0/month | ✅ | ✅ | ✅ | ✅ |
| Benchmark Recall@5 | **100%** | 95.2% | n/a | n/a |

---

## Credits

- [Karpathy's LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — compile-not-retrieve pattern
- [Harrison Chase's "Wiki Memory"](https://blog.langchain.dev/wiki-memory/) — agent-maintained files
- [Google's OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) — vendor-neutral markdown spec
- [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — capture/compact/subagent patterns
- [VEP Semantic DNA](https://vep.live) — confidence/supersede/temporal lifecycle

---

## License

[MIT](LICENSE) — do whatever you want.
