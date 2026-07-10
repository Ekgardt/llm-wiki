# Architecture — LLM-Wiki Memory System v3.3

This document explains **why** the system is shaped the way it is. For **how to use it**, see [USER-GUIDE.md](USER-GUIDE.md).

## Three-zone layout (canonical)

```
CODE          scripts/  tests/  docs/  skills/  rules/  integrations/  benchmark/
KNOWLEDGE     knowledge/{daily,notes,projects,raw,inbox,feedback}
RUNTIME       cache/  logs/  run/   # inside vault, gitignored
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

## System Architecture (v3.3)

```
┌──────────────────────────────────────────────────────────────────────┐
│  AGENTS (5 supported)                                                 │
│                                                                       │
│  OpenCode ──→ plugin (JS, autoload, OpenCode SDK for LLM)            │
│  Codex CLI ──→ PowerShell wrapper (auto-capture on exit)             │
│  Claude Code → settings.json hooks (SessionStart/End/Prompt/Tool)    │
│  Cursor ────→ rules file (.cursor/rules/llm-wiki.mdc)               │
│  Antigravity → AGENTS.md snippet                                      │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────────────┐
│  LLM BACKEND (llm_client.py — 5 auto-detected backends)              │
│                                                                       │
│  Priority: OpenCode SDK → Codex exec → Claude CLI → OpenAI → Ollama  │
│  If none available → task enqueued to persistent queue                │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────────────┐
│  CAPTURE LAYER (real-time, non-LLM)                                  │
│                                                                       │
│  user_prompt_capture.py ── breadcrumb per prompt (ms-fast)           │
│  post_tool_capture.py ──── breadcrumb per Edit/Write/Bash            │
│  daily_log_append.py ───── structured FLUSH block append             │
│  heartbeat_record.py ───── project activity signal                    │
│  feedback_capture.py ───── correction/preference detection           │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────────────┤
│  CLASSIFY + COMPILE LAYER (session end, background)                  │
│                                                                       │
│  flush_memory.py ── 3-tier FLUSH classifier (MAJOR/MINOR/OK)        │
│  compile_memory.py ─ JSON protocol → VERIFY-BEFORE-WRITE → pages    │
│  maybe_compile.py ── PID lock + stale detection + detached spawn    │
│  memory_queue.py ─── persistent deferred task queue (crash-safe)    │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────────────┤
│  SEARCH + RETRIEVAL LAYER (on-demand, ~41ms p50)                     │
│                                                                       │
│  search_memory.py ── Triple-fusion:                                  │
│    BM25 (FTS5, weight=2) + Vector (MiniLM, weight=1)                │
│    + Graph-neighbor (wikilinks, weight=0.5)                         │
│  Weighted RRF fusion with title/filename boost + short-circuit       │
│  Recall@5 = 100%, MRR = 0.942, p50 = 41ms                               │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────────────┤
│  PROACTIVE INTELLIGENCE (SessionStart injection)                     │
│                                                                       │
│  build_guardrails.py ── learned rules from corrections (0 tokens)    │
│  build_advisory.py ──── open threads, last decision, lint alerts     │
│  build_context.py ───── per-project knowledge + agent-aware ranking  │
│  session_start_context.py ── metacognitive block + all of above     │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────────────┤
│  MULTI-AGENT COORDINATION (when agents run in parallel)              │
│                                                                       │
│  blackboard.py ───── task claiming, signal passing, conflict detect  │
│  loop_detector.py ─── prevents infinite fix-review-redo cycles       │
│  agent_timeline.py ── attribution: who decided what and when        │
└──────────────────────────┬───────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────────────┤
│  MAINTENANCE (automatic, scheduled)                                   │
│                                                                       │
│  scheduled_nightly.py (03:00) ── compile + lint + index + graph     │
│  scheduled_weekly.py (Sun 04:00) ── OKF sweep + archive + prune     │
│  archive_stale.py ── type-aware thresholds (decisions never expire)  │
│  lint_memory.py ─── 13 checks (structural + semantic + temporal)    │
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
| `fact` | `knowledge/notes/<slug>.md` or `…/facts/` | Atomic facts | 120 days |
| `skill` | `skills/<name>/SKILL.md` | Agent workflows | **Never** |
| `project-state` | `knowledge/projects/<slug>/state.md` | Per-project handoff | **Never** |

---

## Search Architecture

Three retrieval signals fused via Weighted RRF:

1. **BM25 (weight=2.0)**: FTS5 full-text search, per-word tokenized. Most reliable for known-item retrieval. Title boost (5x exact, 3x subset), filename boost (10x exact match short-circuit), path preference (1.3x for knowledge/notes/).

2. **Vector (weight=1.0)**: sentence-transformers all-MiniLM-L6-v2 (384 dims, ~90MB). Optional dependency — degrades gracefully to BM25-only. Finds semantic relationships ("database performance" → "N+1 query fix").

3. **Graph-neighbor (weight=0.5)**: wikilink adjacency boost. When BM25 finds page A, pages linked FROM A get a soft boost. Finds connected knowledge through the link graph.

**RRF formula**: `score = 2.0/(60+bm25_rank) + 1.0/(60+vector_rank) + 0.5*graph_boost/(120+graph_rank)`

**Short-circuit**: if filename slug exactly matches query → return at rank 1 immediately (bypasses RRF).

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
- **No Neo4j / graph DB** — wikilinks + FTS5 suffice at personal scale.
- **No per-prompt RAG** — session-start + session-end injection is sufficient for solo dev.
