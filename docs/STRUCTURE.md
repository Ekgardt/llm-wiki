# Canonical Structure Reference

> **Single source of truth for the llm-wiki repository layout.**
> Any agent working in this repo MUST read this file before changing
> structure, paths, or env contracts. Changes require explicit user sign-off
> (see `../AGENTS.md` §0 — the root agent contract). The
> `tests/test_structure.py` suite enforces the invariants defined here.

## Three-zone layout

```
llm-wiki/                          ← vault root (= $LLM_WIKI_ROOT)
│
├── scripts/                       CODE — pipeline + hooks + helpers
│   ├── reliable_memory.py            SQLite durability/default primitives
│   ├── markdown_transaction.py       recover/undo/prune Markdown transactions
│   ├── project_journal.py            checkpoints + deterministic state projection
│   ├── memory_queue.py               fenced SQLite priority queue + migration
│   ├── compile_cache.py              content-addressed compile plans + receipts
│   ├── archive_daily.py              immutable daily-log BagIt archive
│   ├── evidence_resolver.py          flat/archive evidence resolution
│   ├── claims.py                     atomic claims + quarantine
│   ├── contradiction_pipeline.py     claim contradiction policy
│   ├── schemas/                      transaction/queue/compile/archive/claim schemas
│   ├── lance_store.py               v4.0: LanceDB embedded vector backend (HNSW)
│   ├── reranker.py                  v4.0: cross-encoder reranker (ONNX)
│   ├── access_tracking.py           v4.0: access logs + Ebbinghaus decay
│   ├── reflection.py                v4.0: A-MEM page consolidation
│   ├── mcp_server.py                v4.0: MCP server (12 task-shaped tools, stdio)
│   ├── integration_adapter.py       v4.x: thin native lifecycle adapter
│   ├── event_envelope.py            v4.x: shared lifecycle event contract
│   ├── mcp_contract.py              v4.x: uniform MCP response envelope/resources
│   ├── doctor.py                    v4.x: degraded-only health + safe repair
│   ├── code_graph.py                v4.0: tree-sitter code intelligence
│   ├── impact_analysis.py           v4.0: LINK layer (code→wiki impact)
│   ├── build_tiers.py               v4.0: L0/L1/L2 progressive disclosure
│   └── queries/                     v4.0: 12 tree-sitter .scm language queries
├── tests/                         CODE — regression suite (pytest, 1801 tests)
├── docs/                          CODE — architecture + user guide
├── skills/                        CODE — 9 agent skills (SKILL.md)
├── rules/                         CODE — file-handling policies
├── integrations/                  CODE — IDE/agent integrations
├── benchmark/                     CODE — benchmark suite + report
│
├── knowledge/                     KNOWLEDGE — content (gitignored: personal)
│   ├── daily/                       append-only session logs
│   │   ├── receipts/                authoritative immutable compile receipts
│   │   └── archive/YYYY-MM/bag-…/   immutable uncompressed BagIt packages
│   ├── notes/                       durable OKF pages (flat slugs)
│   ├── projects/<slug>/             state.md projection + append-only journal.md
│   ├── raw/                         immutable sources
│   ├── inbox/                       unprocessed staging
│   └── feedback/                    correction candidates
│
├── cache/                        RUNTIME — gitignored (FTS5/vector/graph/LanceDB)
│   ├── lancedb/                     v4.0: LanceDB vector store (optional, --extra hybrid)
│   ├── models/                      v4.0: ML model cache (reranker, embeddings)
│   ├── compile/                     validated content-addressed compile plans
│   ├── claims.sqlite3               derived claim candidate index
│   ├── access_log.jsonl             v4.0: access tracking log
│   ├── code_tools.json               v4.0: atomic code-tool capability manifest
│   ├── vectors.npy                  v4.0: numpy binary vector cache (memory-mapped)
│   ├── vectors_meta.json            v4.0: vector metadata (paths, titles — no vectors)
│   └── index.sqlite                 FTS5 search index
├── logs/                         RUNTIME — gitignored (lint/compile/hook logs)
├── run/                          RUNTIME — gitignored operational state
│   ├── markdown-transactions.sqlite3 transaction/lease coordinator
│   ├── transactions/<id>/           before/after images + prepared plans
│   ├── queue.sqlite3                 rollback-journal priority queue
│   ├── queue-results/                fenced result receipts
│   ├── queue/                        legacy migration input only
│   ├── queue-migrated-v2             migration completion marker
│   ├── state.json                    automation + compile receipts
│   └── state.json.lock
│
├── AGENTS.md                     ROOT — agent contract (byte-identical to CLAUDE.md)
├── CLAUDE.md                     ROOT — agent contract (byte-identical to AGENTS.md)
├── CHANGELOG.md                  ROOT — Keep-a-Changelog
├── CONTRIBUTING.md               ROOT — contribution guide
├── README.md                     ROOT — English (primary)
├── README.ru.md                  ROOT — Russian (faithful translation)
├── README.zh-CN.md               ROOT — Chinese (faithful translation)
├── LICENSE                       ROOT — MIT
├── install.ps1                   ROOT — Windows installer
├── install.sh                    ROOT — Unix installer
├── pyproject.toml                ROOT — project metadata + ruff/pytest config
├── uv.lock                       ROOT — lockfile
├── .github/                      ROOT — CI workflows, issue templates
├── .gitignore                    ROOT — ignore rules
├── .gitattributes                ROOT — line-ending normalization
├── .gitleaksignore               ROOT — false-positive allowlist
└── .pre-commit-config.yaml       ROOT — pre-commit hooks (ruff + lint + gitleaks)
```

## Env contracts (fixed)

| Variable | Default | Purpose |
|----------|---------|---------|
| `$LLM_WIKI_ROOT` | Resolved from `scripts/` location (worktree-aware via `git rev-parse --git-common-dir`) | Vault root — code + knowledge + runtime |
| `$LLM_WIKI_STATE_ROOT` | **The vault root itself** | Runtime root → `cache/`, `logs/`, `run/` at vault root. Override for multi-disk or hermetic tests. |
| `$MEMORY_LLM_PROVIDER` | Auto-detected (`opencode` → `codex` → `claude` → `openai` → `ollama`) | LLM backend for compile/flush/query. `fake` for tests. |

## What lives where

### CODE zone (tracked in git)
- `scripts/` — Python pipeline and host helpers. Central hub:
  `memory_state.py` (path/lock/state), `compile_memory.py` (LLM compile +
  VERIFY-BEFORE-WRITE), `flush_memory.py` (3-tier classification),
  `maybe_compile.py` (PID-locked spawn), `search_memory.py` (triple-RRF),
  `llm_client.py` (5 backends + fake), `integration_adapter.py` (thin host
  lifecycle boundary), `mcp_server.py` (12 task-shaped tools), and `doctor.py`.
- `tests/` — 1801 tests collected. Hermetic via `conftest.py` (pins
  `LLM_WIKI_ROOT` to checkout, redirects `LLM_WIKI_STATE_ROOT` to a temp
  dir, defaults `MEMORY_LLM_PROVIDER=fake`).
- `docs/` — `ARCHITECTURE.md`, `USER-GUIDE.md`, `AGENTS.md` (knowledge
  subsystem brief — subordinate to the root `../AGENTS.md` contract),
  `EXPORTING.md`, `SETUP-COGNEE.md`, `operating-model.md`,
  `STRUCTURE.md` (this file).
- `scripts/queries/` — 12 language-specific Tree-sitter queries for function,
  class/type, call, and import extraction. Grammar packages are optional and
  loaded lazily by `code_graph.py`; `NOTICE.md` records grammar provenance and
  MIT notices.
- `scripts/schemas/` — closed JSON Schemas for transaction, project checkpoint,
  queue task, compile plan/receipt, archive manifest, and claim records.
- `skills/` — 9 SKILL.md files (knowledge-compile, knowledge-lookup,
  knowledge-review, knowledge-qa-file-back, contradict-check,
  crystallize-playbook, bridge-promote-insight, session-memory-compile,
  session-memory-review).
- `rules/` — 3 rule files (wiki-files, raw-files, output-files).
- `integrations/` — thin host wiring: claude-code (settings.json), cursor
  (rules), and antigravity (AGENTS.md). MCP is the common read/action interface.
  Obsidian is an optional Markdown viewer and requires no bundled integration.
- `benchmark/` — retrieval and frozen contradiction corpora/runners, including
  `run_benchmark.py`, `legacy-60-v1.json`, `run_contradiction_benchmark.py`, and
  `contradiction-v1.json`.

### KNOWLEDGE zone (tracked: public fixtures; gitignored: personal)
- `knowledge/daily/` — append-only `YYYY-MM-DD.md`. Private (gitignored).
  Public synthetic fixtures (`2026-04-13.md`, `2026-04-19.md`) are
  un-ignored to restore Evidence links.
- `knowledge/daily/receipts/` — authoritative immutable Markdown compile receipts,
  keyed by the exact source snapshot digest and committed with compile output.
- `knowledge/notes/` — durable OKF pages, flat `<slug>.md`. Public examples
  tracked via allowlist; personal pages gitignored.
- `knowledge/projects/<slug>/` — generated `state.md`, append-only
  `knowledge/projects/<slug>/journal.md`,
  `context.md`, `.blackboard/`. Template tracked; real projects gitignored.
- `knowledge/daily/archive/YYYY-MM/bag-<timestamp>-<id>/` — private immutable,
  uncompressed BagIt-style daily-log bags and
  a derived archive index. Archive means move, never delete; evidence resolves by
  logical ID, source hash, and byte span.
- `knowledge/raw/` — immutable sources. Gitignored (personal).
- `knowledge/inbox/` — unprocessed staging. Gitignored.
- `knowledge/feedback/` — correction candidates (JSON). Gitignored.

### RUNTIME zone (always gitignored, inside vault)
- `cache/` — `index.sqlite` (FTS5), `vectors.npy` (binary numpy, mmap),
  `vectors_meta.json` (metadata),
  `code_tools.json` (fresh code-tool detection and active semantic capabilities).
  v4.0: `lancedb/` (LanceDB vector store, optional), `models/` (ML model cache),
  `access_log.jsonl` (retrieval analytics), `cache/compile/` (validated compile-plan
  action cache), and `cache/claims.sqlite3` (derived claim index).
- `logs/` — `lint-YYYY-MM-DD.md`, `compile-last.log`, `session-start-last.txt`.
- `run/` — `state.json`, `compile.pid`, `run/markdown-transactions.sqlite3`,
  `run/transactions/`, `run/queue.sqlite3`, `run/queue-results/`, receipts, and
  locks. Existing `run/queue/*.json` is one-time migration input only.
- `cache/cognee/` — optional semantic graph data (only if Cognee installed).

**Runtime deletion contract.** `cache/` and `logs/` are regenerated on demand.
`run/` contains recoverable but operationally significant transactions and queued
work. Delete it only after `doctor` reports no nonterminal, conflicted, or
quarantined transaction, no transaction inside the 30-day undo window, and no
retained queue task or result, and no live project lease, writer, queue worker, or
maintenance owner. Deleting eligible committed artifacts loses undo history.
Installers and repair commands never remove it silently.

## Forbidden at vault root

These directories MUST NOT exist at the vault root (three-zone violation):

| Path | Reason |
|------|--------|
| `wiki/` | Legacy pre-three-zone. Consolidated into `knowledge/notes/`. |
| `memory/` | Legacy pre-three-zone. Consolidated into `knowledge/`. |
| `outputs/` | Legacy. No outputs zone in three-zone layout. |
| `state/` | Legacy runtime name. Use `run/` inside the vault. |
| `LLM-wiki-state/` | Legacy sibling layout. Runtime now lives inside the vault. |

The `tests/test_structure.py::test_forbidden_root_dirs_absent` test catches
any of these appearing.

## Changing this structure

1. **Describe the proposed change** in plain language (what, why, impact).
2. **Get explicit user sign-off.**
3. **Update this file** (`docs/STRUCTURE.md`) to reflect the new canonical
   layout.
4. **Update `tests/test_structure.py`** to enforce the new invariants.
5. **Update `AGENTS.md` + `CLAUDE.md`** (keep byte-identical).
6. **Update all scripts/docs that reference the changed paths.**
7. **Run `uv run pytest -q` + `uv run ruff check scripts/ tests/`** — must
   be green.

Never skip steps 1-2. Architectural improvisation is the root cause of the
most expensive bugs in this project's history.
