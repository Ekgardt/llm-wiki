# Architecture — LLM-Wiki Memory System v4.0

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

Local-first describes the authoritative storage and product runtime, not every model
transport. OpenCode, Codex, Claude, and OpenAI may process prompts remotely. Ollama is
local only when explicitly forced to a literal loopback IP and the selected model has
local metadata. An external Ollama process prevents the client from proving that cloud
disablement was applied after restart, so that state remains explicitly unverified.

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
│  AGENTS                                                              │
│                                                                      │
│  NATIVE LIFECYCLE EVENTS                                             │
│  hooks/plugins/wrappers → integration_adapter.py → local capture     │
│  Capture is deterministic and does not require an LLM.               │
│                                                                      │
│  MCP READS + ACTIONS                                                 │
│  12 task-shaped MCP tools → local search/code/doctor/maintenance     │
│  MCP responses and resources do not require an LLM.                  │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ captured session signals
                               ↓
┌──────────────────────────────────────────────────────────────────────┐
│  CLASSIFY + COMPILE                                                  │
│  flush_memory.py / compile_memory.py → durable Markdown pages        │
│                               │                                      │
│                               ↓                                      │
│  LLM BACKEND (CLASSIFY + COMPILE ONLY)                               │
│  llm_client.py: 5 backends including Ollama + fail-closed DLP        │
└──────────────────────────────┬───────────────────────────────────────┘
                               ↓
┌──────────────────────────────────────────────────────────────────────┐
│  LOCAL SEARCH + INTELLIGENCE                                        │
│  validated immutable generation: FTS + Evidence Graph + evidence     │
│  optional generation vectors; legacy FTS/vector/Lance compatibility  │
│  retrieval planner + context compiler + grounded QA                  │
└──────────────────────────────────────────────────────────────────────┘
```

## Read-only Python navigation

The implemented Python slice keeps structural discovery in the existing Evidence
Graph and adds an owned, Python 3.10-compatible LSP path through pinned Pyright
1.1.411. The existing `get_architecture` tool routes precise modes through one lazy
repository-scoped session. The facade validates input bytes, synchronizes documents,
normalizes provider facts, merges explicit structural fallback, and proves the
workspace revision again before publishing a result.

The LSP runtime is derived and read-only at the product API boundary. There is no semantic result cache,
no second graph, and no persistent daemon; query-time LSP observations are not written
into an active generation. Small exact results use the
deterministic navigation renderer; broad architecture and impact synthesis continue
to use the Context Compiler.

Pyright installation is an explicit operator action into
`cache/code-tools/pyright/1.1.411/`. Process scratch is bounded under
`run/lsp/<owner-nonce>/` and protected by the existing `run/` deletion contract.
Windows owns the assigned server tree with a Job Object. POSIX owns the pinned server
and descendants with a process group only while they remain in that group; hostile
`setsid()` escape is unsupported. The runtime therefore supports trusted local
repositories and does not claim to be an OS sandbox.

The deterministic 100 KLOC fixture qualifies correctness, freshness, recovery,
ownership, output bounds, latency, and client RSS without claiming market
superiority. See [CODE-NAVIGATION.md](CODE-NAVIGATION.md).

## Approved superset direction

The 2026-07-19 product decision extends this architecture from a memory and
evidence system into a local control plane for one operator managing many agents.
The extension preserves all seven axioms and adds four requirements:

1. Code, knowledge, project, task, and decision evidence share one validated
   repository-scoped generation.
2. Episodic, semantic, procedural, and prospective memory have distinct lifecycle
   rules and temporal provenance.
3. Objectives, tasks, executions, workspaces, artifacts, checkpoints, budgets,
   capabilities, and exceptions survive individual sessions.
4. SessionStart, MCP, grounded QA, handoff, impact, and task execution all use one
   Adaptive Context Compiler.

An optional watcher, HTTP MCP transport, and local operator console are permitted
adapters. They are not authoritative and are not required for offline operation.
The console is an exception and portfolio-decision surface, not a chat dashboard.
See `knowledge/notes/solo-operator-superset-product-decision.md` and
`docs/superpowers/plans/2026-07-19-solo-operator-superset-roadmap.md`.

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

## Unified evidence retrieval architecture

Markdown, Git, and append-only project journals are authoritative. The generation
catalog, Evidence Graph, FTS, vectors, L0/L1/L2 tiers, contextual artifacts,
telemetry, and model caches are derived runtime state. A derived record may guide
retrieval, but it cannot override its captured source bytes.

`cache/evidence-graph/catalog.sqlite3` is the single active pointer. A generation
captures source membership and hashes, then seals its artifacts in `manifest.json`.
Registration verifies the manifest, artifact hashes, SQLite integrity and schema,
source membership, and evidence byte ranges/hashes. Compare-and-swap activation is
the only publication point. Readers seal the catalog pointer and artifacts before
use and recheck them after opening so a concurrent activation cannot mix generations.

Interrupted candidates are not active. Recovery can register a complete immediate
child orphan without activating it. If the active generation is corrupt or missing,
the catalog walks activation history and parent links, validates candidates again,
and repairs the pointer to the newest usable prior generation. If none is usable,
consumers fall back honestly to legacy retrieval or bounded live extraction.

Three local tiers share the same Markdown corpus:

### Base retrieval tier (no optional search extras)
1. **BM25 (weight=2.0)**: SQLite FTS5 full-text search. Title boost (5x exact), filename boost (10x match short-circuit).
2. **Graph-neighbor (weight=0.5)**: wikilink adjacency boost.

No embedding model, vector cache, or optional package is required in this tier.

### Optional semantic tier (`uv sync --extra semantic`)
1. **BM25 (weight=2.0)**: SQLite FTS5.
2. **Vector (weight=1.0)**: numpy brute-force cosine similarity using BAAI/bge-small-en-v1.5. Vectors are cached in `vectors.npy` with `vectors_meta.json` metadata.
3. **Graph-neighbor (weight=0.5)**: wikilink adjacency boost.

### Hybrid tier (`uv sync --extra hybrid`)
1. **BM25 (weight=2.0)**: SQLite FTS5 (same as base — 25 years battle-tested).
2. **Vector (weight=1.0)**: LanceDB HNSW compatibility backend (embedded, no daemon, Apache-2.0).
3. **Graph-neighbor (weight=0.5)**: wikilink adjacency boost.
4. **Cross-encoder reranker**: bge-reranker-base (ONNX INT8, optional), re-scores top-20.

**RRF formula**: `score = 2.0/(60+bm25_rank) + 1.0/(60+vector_rank) + 0.5/(60+graph_rank)` for signals that actually return a ranked candidate.

The legacy optional vector path remains pinned to `BAAI/bge-small-en-v1.5` for
compatibility. `benchmark/model-matrix-v1.json` defines pinned multilingual embedding
and reranker candidates, but its default embedding and reranker are both `null` and
its status is `awaiting_raw_benchmark`. No new default or superiority claim is
allowed until raw EN/RU/ZH quality, latency, RAM, license, regression, and Pareto
evidence passes the selection contract. **Evidence pending.**

**Why no PostgreSQL?** PostgreSQL requires a daemon, which violates Axiom #1.
SQLite + optional LanceDB preserves a local, zero-daemon deployment shape. This is
not a measured quality-equivalence claim against PostgreSQL or another service.

### Context and token contract

SessionStart, project context, compiled context, and grounded QA use complete-item
packing under a shared budget. Safety, health, handoff, blocker, decision, evidence,
and history priority classes are ordered before relevance-per-token utility. Mandatory
items fail closed when they cannot fit; arbitrary string truncation is not used as a
substitute for preserving evidence boundaries.

Token values carry a source label: `reported` means the provider reported usage;
`tokenizer` means a model-specific adapter counted it; `estimated` currently means a
UTF-8 byte estimate; `mixed` combines count sources; `unknown` carries no numeric
claim. Monetary cost has a separate `reported|estimated|unknown` label. The byte
estimate is conservative planning data, not a universal token upper bound.

### Grounded-answer contract

QA retrieves child chunks, groups them by authoritative parent source, and sends only
captured spans as untrusted data. Each usable citation includes an ID, repository-
relative path, source hash, revision, byte and line ranges, span hash, and supplied
text. Deterministic verification checks root containment, all hashes/ranges, and that
every claim cites supplied evidence. Generated summaries and a measured small-vault
cached index are orientation only. Valid statuses are `answered`,
`insufficient_evidence`, `conflicting_evidence`, and `unsupported_time_scope`.
Generation is read-only; `--file-back` crosses the recoverable Markdown transaction
boundary only after verification.

### Comparison evidence

The pinned Graphify contract and deterministic comparative smoke exist. The smoke
does not execute Graphify and explicitly reports no quality claim, no token-ratio
claim, and unavailable hard gates. Real paired Graphify evidence is **evidence
pending**. No architecture or model superiority follows from the smoke.

---

## Why not just use Mem0 / Zep / Letta?

For most teams and most use cases, **you should**. Those tools are mature, supported, and solve real problems. LLM Wiki exists because:

- You already pay for OpenCode / Codex / Claude — paying again for memory feels redundant
- Your memory is your moat — sending project knowledge to a third party is a non-starter
- Markdown outlives vendors — your 2026 wiki will be readable in 2036
- Multi-tool is the future — most developers use 2-3 AI coding tools, not just one

---

## Current omissions, not permanent product exclusions

- **No cloud sync** — your memory stays on your disk. Use git for remote backup.
- **No canonical frontend today** — Obsidian remains an optional Markdown viewer. The approved target adds a local exception-driven operator console without making it authoritative.
- **No multi-user** — solo developer only.
- **No unbounded full-vault prompt dump** — ordinary QA retrieves and verifies bounded evidence; only a measured genuinely small vault may use the `CACHED_FULL` orientation profile.

## Reliable mutation architecture

Markdown remains authoritative. SQLite stores coordination state, hashes, leases,
receipts, queue metadata, and derived claim indexes; there is no SQLite knowledge source
and no graph database as a source of truth. Automatic writes use four-phase
recoverable transactions (`preparing`, `prepared`, `applying`, `committed`) with
before/after images and compare-and-swap (CAS) checks. Internal readers take the
writer gate for a coherent view. Because several fixed paths cannot be swapped as
one portable filesystem operation, an external editor may briefly see a mixed tree.
CAS guarantees apply to cooperating transaction-API writers; concurrent external
edits are unsupported, best-effort detected, and quarantined rather than overwritten.

The coordinator and queue use SQLite rollback-journal mode, `synchronous=FULL`,
short `BEGIN IMMEDIATE` transactions, and bounded busy timeouts. There is no WAL
on the current SQLite runtime. Runtime SQLite must be on a local filesystem with
working locks. Known network paths and failed locking probes are rejected;
cloud-synchronized folder detection is best-effort and cannot identify every
product. A separate local `LLM_WIKI_STATE_ROOT` disk is supported because target
replacement staging is created beside each Markdown target.

Project `state.md` is a deterministic projection of append-only `journal.md`.
Transactions persist project lease token and fencing epoch, preventing a stale
projector from committing. Compilation snapshots exact source bytes, stores only
validated plans in a content-addressed local cache, and emits completion receipts.
Provider fallback has a distinct cache key; unknown provider/model identity disables
persistent cache hits.

Queue delivery is at least once, never exactly-once. Leases and acknowledgements are
fenced; handlers use stable operation IDs for idempotent side effects. Legacy JSON
tasks migrate once to the rollback-journal `run/queue.sqlite3`; malformed sources are
quarantined, and migration aborts if a legacy owner cannot be excluded. Workers are
short-lived and bounded, not a persistent daemon. Terminal purge is manual and
export-first. A source failure, retained task/result, or live owner remains visible
and blocks unsafe cleanup.

Daily logs leave the 90-day hot set only after compile receipts, terminal operations,
queue preflight, evidence spans, and pins validate. Archive publication is an atomic
rename of a complete immutable BagIt directory. Bags remain uncompressed so evidence
can be sliced directly; there is no gzip tier. Claims must pass literal evidence
verification before contradiction evaluation. Uncertain or disputed candidates go
to quarantine, and automatic semantic supersession remains disabled until the frozen
benchmark demonstrates no more than 1% false supersession. There is no eager backfill
of claim ledgers.

The system performs no automatic Git staging, commit, branch, or remote operation.
It adds no cloud service, remote queue/cache, or persistent daemon. Runtime databases
coordinate local work only. Operational defaults are 10-second transaction and
5-second queue busy timeouts; 30-day transaction/undo retention; 90-day archive hot
retention; 30/10-second project lease/heartbeat; 30-second checkpoint debounce and
20-event fallback; 120/40-second queue lease/heartbeat; 8 attempts with 30/3600-second
retry base/cap; and worker limits of 20 tasks, 600 seconds, and 2 idle seconds.
Explicit CLI flags override runtime retention and worker/queue policy; Stage 2 adds
no environment variables.

## What v4.0 adds (optional, all behind `--extra` flags)

- **LanceDB hybrid vectors** (`--extra hybrid`): HNSW vector search, embedded, zero-daemon.
- **Cross-encoder reranker** (`--extra reranker`): bge-reranker ONNX, re-ranks top-20 results.
- **Code graph** (`--extra code-graph`): lazy tree-sitter parsing of Python,
  JavaScript, TypeScript, Go, Rust, Java, C, C++, Ruby, PHP, C#, and Bash;
  materialized `.scm` queries, call graph, and impact analysis.
- **MCP server**: the installer baseline includes the MCP package and exposes
  12 task-shaped tools including `doctor`, a uniform response envelope, and
  health/context resources. For manual dependency selection from source, use
  `uv sync --locked --extra mcp-server`; transport remains local stdio.
- **Automatic health**: SessionStart injects only degraded/error findings; healthy
  checks stay quiet. Repairs are explicit and limited to safe, idempotent actions.
- **All v4.0 features degrade gracefully** — the installed product remains local and zero-daemon.
