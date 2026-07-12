# Architecture — LLM-Wiki Memory System v3.4

This document explains **why** the system is shaped the way it is. For **how to use it**, see [USER-GUIDE.md](USER-GUIDE.md).

## Three-zone layout (canonical)

```
CODE          scripts/  tests/  docs/  skills/  rules/  integrations/  benchmark/
KNOWLEDGE     knowledge/{daily,notes,projects,raw,inbox,feedback}
RUNTIME       cache/  cache/cognee/  logs/  run/   # inside vault, gitignored
              Override root via LLM_WIKI_STATE_ROOT (tests use a temp dir).
```

- `cache/`, `logs/`, `run/` live inside the vault as gitignored
  dirs — single-checkout portability, git never tracks their churn.
- Public source develops code; installed vault (`$LLM_WIKI_ROOT`) holds
  user knowledge + live runtime data.

## Design principles (the 7 axioms)

These are non-negotiable. If a proposed feature breaks one, the feature goes back to the drawing board.

### 1. Files over infrastructure
The vault is plain markdown. No databases, no proprietary formats, no daemons required to read your memory. `cat`, `git diff`, Obsidian, ripgrep — all work natively.

### 2. OKF v0.1 conformant
Every knowledge page has YAML frontmatter with at least `type:`. This guarantees future interoperability with any OKF-compatible tool.

### 3. LLM-agnostic
No hard dependency on OpenAI / Anthropic / anyone. The `llm_client.py` abstraction supports 5 backends and auto-detects the first alive one.

### 4. Smallest set of high-signal tokens
Anthropic's context-engineering principle: every byte of context injected into a prompt costs attention budget. SessionStart context is capped at ~2KB.

### 5. Provenance + supersede, never silent delete
Karpathy rule 7. When a new fact contradicts an old one, the old page is marked `status: superseded`. History is preserved.

### 6. Capture → Analyze → Update loop
LangChain's memory loop pattern. Raw signal is captured cheaply in real-time. Analysis happens at session end. Updates happen in detached background compiles.

### 7. One brain, many projects
The slug system (5-step collision resolution) lets a single vault track unlimited projects without namespace conflicts.

---

## System Architecture (v4.0)

```
┌──────────────────────────────────────────────────────────────────────┐
│  AGENTS (5 supported + MCP)                                           │
│                                                                       │
│  OpenCode ──→ plugin (JS) + MCP tools (9 task-shaped, stdio)         │
│  Codex CLI ──→ PowerShell wrapper + MCP                              │
│  Claude Code → hooks (5 lifecycle) + MCP + skills                    │
│  Cursor ────→ rules file + MCP                                       │
│  Antigravity → AGENTS.md + MCP                                       │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────────────┐
│  LLM BACKEND (llm_client.py — 5 auto-detected + Ollama local)        │
│  v4.0: constrained decoding (guided_json) for local LLM reliability  │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────────────┐
│  CAPTURE LAYER (real-time, non-LLM)                                  │
│  v4.0: access_tracking.py logs every retrieval (Ebbinghaus decay)    │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────────────┐
│  CLASSIFY + COMPILE LAYER (session end, background)                  │
│                                                                       │
│  v4.0: compile_memory.py ── multi-pass: draft → critique → write    │
│  v4.0: typed edges ── refines (not just supersedes)                  │
│  v4.0: reflection.py ── A-MEM evolution (weekly page consolidation) │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────────────┐
│  SEARCH + RETRIEVAL LAYER                                             │
│                                                                       │
│  SEARCH + RETRIEVAL LAYER                                             │
│                                                                       │
│  SQLite FTS5 (BM25, weight=2) — zero-dep, always works               │
│  v4.0: LanceDB (HNSW vector, weight=1) — embedded, --extra hybrid    │
│    fallback: numpy brute-force (.npy memory-mapped) when no LanceDB  │
│  Graph-neighbor (wikilinks, weight=0.5)                              │
│  v4.0: bge-small-en-v1.5 (MTEB 62.17, +25% over MiniLM)             │
│  v4.0: cross-encoder reranker (bge-reranker, ONNX, top-20 rerank)   │
│  Weighted RRF fusion: 2.0/(60+bm25) + 1.0/(60+vec) + 0.5*graph      │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────────────┐
│  v4.0: CODE INTELLIGENCE LAYER                                       │
│                                                                       │
│  code_graph.py ── tree-sitter: functions, classes, calls, imports   │
│  impact_analysis.py ── LINK: git diff → stale wiki pages            │
│  (unique: no other system connects code graph to knowledge graph)    │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────────────┤
│  PROACTIVE INTELLIGENCE (SessionStart injection)                     │
│                                                                       │
│  v4.0: hybrid forgetting (archive_stale.py + access_tracking decay)  │
│  v4.0: impact advisory (stale pages from code changes)               │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Memory taxonomy

Pages live **flat** as `<slug>.md` under `knowledge/notes/` by default (the
compile pipeline writes flat slugs). Typed subdirectories (`concepts/`,
`decisions/`, etc.) are optional and currently unused — both layouts are
valid and lint covers them equally.

| Type | Location (flat or typed subdir) | Purpose | Archive threshold |
|---|---|---|---|
| `concept` | `knowledge/notes/<slug>.md` or `…/concepts/` | Mental models | **Never** |
| `decision` | `knowledge/notes/<slug>.md` or `…/decisions/` | Dated choices with rationale | **Never** |
| `pattern` | `knowledge/notes/<slug>.md` or `…/patterns/` | Recurring approaches | 180 days |
| `debugging` | `knowledge/notes/<slug>.md` or `…/debugging/` | Symptom → cause → fix | 60 days |
| `qa` | `knowledge/notes/<slug>.md` or `…/qa/` | Settled answers | 365 days |
| `gap` | `knowledge/notes/<slug>.md` | Not-yet-written knowledge | 90 days |
| `workflow` | `knowledge/notes/<slug>.md` or `…/workflows/` | Auto-promoted playbooks | 365 days |
| `skill` | `skills/<name>/SKILL.md` | Agent workflows | **Never** |
| `project-state` | `knowledge/projects/<slug>/state.md` | Per-project handoff | **Never** |

---

## Search Architecture (v4.0)

Two tiers, same weighted RRF fusion:

### Base tier (zero-dep, always works)
1. **BM25 (weight=2.0)**: SQLite FTS5 full-text search. Title boost (5x exact), filename boost (10x match short-circuit).
2. **Vector (weight=1.0)**: numpy brute-force cosine similarity, bge-small-en-v1.5 (384 dims, MTEB 62.17). Vectors cached as `.npy` (memory-mapped binary — instant load).
3. **Graph-neighbor (weight=0.5)**: wikilink adjacency boost.

### Hybrid tier (`uv sync --extra hybrid`)
1. **BM25 (weight=2.0)**: SQLite FTS5 (same as base — 25 years battle-tested).
2. **Vector (weight=1.0)**: LanceDB HNSW (embedded, no daemon, Apache-2.0). Sub-ms at any scale.
3. **Graph-neighbor (weight=0.5)**: wikilink adjacency boost.
4. **Cross-encoder reranker**: bge-reranker-base (ONNX INT8, optional), re-scores top-20.

**RRF formula**: `score = 2.0/(60+bm25_rank) + 1.0/(60+vector_rank) + 0.5*graph_boost/(120+graph_rank)`

**v4.0 embedding**: BAAI/bge-small-en-v1.5 (MIT, MTEB 62.17, +25% over all-MiniLM-L6-v2).
Query instruction prefix for retrieval optimization.

**Why no PostgreSQL?** Every top local-first system (EverOS, repowise, codebase-memory-mcp,
Memtrace) uses embedded storage. PostgreSQL requires a daemon — violates Axiom #1.
SQLite + LanceDB gives the same hybrid quality without any server process.

---

## Why not just use Mem0 / Zep / Letta?

For most teams and most use cases, **you should**. Those tools are mature, supported, and solve real problems. LLM Wiki exists because:

- You already pay for OpenCode / Codex / Claude — paying again for memory feels redundant
- Your memory is your moat — sending project knowledge to a third party is a non-starter
- Markdown outlives vendors — your 2026 wiki will be readable in 2036
- Multi-tool is the future — most developers use 2-3 AI coding tools, not just one

---

## What's intentionally NOT here

- **No cloud sync** — your memory stays on your disk. Use git for remote backup.
- **No web UI** — Obsidian is the UI. Or `cat`.
- **No multi-user** — solo developer only.
- **No per-prompt RAG** — compile-not-retrieve pattern; session-start + session-end injection suffices.

## What v4.0 adds (optional, all behind `--extra` flags)

- **LanceDB hybrid vectors** (`--extra hybrid`): HNSW vector search, embedded, zero-daemon.
- **Cross-encoder reranker** (`--extra reranker`): bge-reranker ONNX, re-ranks top-20 results.
- **Code graph** (`--extra code-graph`): tree-sitter parsing of Python/JS/TS, call graph, impact analysis.
- **MCP server** (`--extra mcp-server`): 9 task-shaped tools, stdio transport, 100% local.
- **All v4.0 features degrade gracefully** — base install remains zero-dep, zero-daemon.
