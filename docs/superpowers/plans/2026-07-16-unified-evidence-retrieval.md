# Unified Evidence Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the remaining Stage 1 and Stage 2 work, then build a generation-consistent retrieval and project-intelligence pipeline that uses the smallest useful evidence-backed context across knowledge, code, Git, and project history.

**Architecture:** Markdown, Git, and project journals remain authoritative. A disposable generation under `cache/evidence-graph/` binds corpus hashes, FTS, vectors, tiers, code and knowledge edges, and evidence spans; a rollback-journal SQLite catalog atomically selects the active generation. A deterministic Adaptive Context Compiler plans retrieval, fuses only comparable ranks, conditionally reranks, materializes L0/L1/L2 or parent source spans, and packs complete items into an explicit token budget while reporting every signal, fallback, and freshness condition.

**Tech Stack:** Python 3.10+ standard library, SQLite rollback journal, FTS5, NumPy exact vector search, optional Sentence Transformers/ONNX Runtime, optional LanceDB compatibility adapter, optional USearch after a measured crossover, Tree-sitter, Jedi, Markdown/OKF, MCP Python SDK, pytest, Ruff, Git.

---

## Status And Authority

This is the canonical implementation roadmap after the 2026-07-16 architecture review. It combines:

- the implemented Agent-Native Foundation plan;
- the implemented Reliable Memory plan and its remaining writer-boundary gap;
- the previously intended Stage 3 retrieval-quality work;
- the proposed Graphify-oriented Persistent Evidence Graph and Adaptive Context Compiler;
- current retrieval, provenance, graph, and local-indexing practices researched on 2026-07-16.

This plan does not reopen completed Stage 1 or Stage 2 decisions. It extends them. In particular:

- Markdown remains the knowledge source of truth.
- The project journal remains the project-history source of truth.
- Git and current worktree bytes remain the code source of truth.
- All graph, FTS, vector, tier, and telemetry databases are derived runtime state.
- No daemon, cloud service, mandatory external database, automatic Git operation, or WAL mode is introduced.
- The existing 12 MCP tools remain task-shaped; behavior is enriched without tool proliferation.

No implementation task may silently change these constraints. A contradictory requirement must be handled as a new architecture decision, not improvised inside a task.

## What Changes From The Previous Roadmap

The old sequence would have improved multilingual vectors and query answering before fixing mixed generations, discarded code graphs, token accounting, and retrieval truthfulness. That would create a second retrieval layer over stale or incomparable signals.

The new dependency order is:

```text
Stage 1/2 closure
  -> measurement and telemetry
  -> corpus generations and truthful retrieval contract
  -> Adaptive Context Compiler and grounded QA
  -> Persistent Evidence Graph
  -> incremental project intelligence
  -> comparative proof and release
```

Graphify parity features that do not improve this critical path are deferred: visual explorer, broad document ingestion, HTTP MCP, package-wide CLI migration, eager Git-history backfill, and additional languages.

## Research Basis

The implementation must preserve the conclusions of the dated research instead of selecting components from vendor claims:

- SQLite FTS5 Porter stemming is English-only; mixed EN/RU/ZH retrieval needs Unicode-aware lexical handling and explicit Chinese segmentation tests: <https://www.sqlite.org/fts5.html> and <https://unicode-org.github.io/icu/userguide/boundaryanalysis/>.
- Rank fusion defaults to RRF when no judged data exists. A normalized learned or linear fusion is allowed only when a separate validation split beats RRF: <https://arxiv.org/abs/2210.11934>.
- Fine-grained retrieval should expand to authoritative parent sections/pages before generation: <https://arxiv.org/abs/2312.06648>.
- Generated contextual prefixes remain evaluation-gated; deterministic title, heading, project, type, and validity context comes first: <https://www.anthropic.com/news/contextual-retrieval>.
- Exact vector search remains the default until real p95 latency justifies ANN. USearch is the first lightweight ANN candidate; LanceDB remains an optional compatibility backend: <https://github.com/unum-cloud/USearch> and <https://docs.lancedb.com/indexing/vector-index>.
- Evidence uses immutable source hashes plus selectors and provenance concepts aligned with W3C Web Annotation and PROV: <https://www.w3.org/TR/annotation-model/> and <https://www.w3.org/TR/prov-dm/>.
- Code symbol interchange should align with SCIP identity where a compiler-backed resolver is available: <https://scip-code.org/>.
- Retrieved Markdown is untrusted data and cannot control tools, filters, citation policy, or output schemas: <https://genai.owasp.org/llmrisk/llm01-prompt-injection/>.
- Codex lifecycle capture uses official hooks rather than transcript-wrapper assumptions: <https://developers.openai.com/codex/hooks/>.
- The external comparison target is Graphify-Labs/graphify commit `cb96bdaa0c367bec8d5c5aee5d7c9ebb727e9780`, not a moving `latest`: <https://github.com/Graphify-Labs/graphify/commit/cb96bdaa0c367bec8d5c5aee5d7c9ebb727e9780>.

## Model Policy

Embedding and reranking models are replaceable adapters, not architecture.

The first benchmark matrix is:

| Role | Required candidates | Reason |
|---|---|---|
| Existing baseline | `BAAI/bge-small-en-v1.5` | Preserve the shipped result and quantify multilingual weakness. |
| Permissive multilingual dense | `Qwen/Qwen3-Embedding-0.6B` | Strong modern EN/RU/ZH candidate, Apache-2.0, no remote model code. |
| Dense plus learned sparse | `BAAI/bge-m3` | Tests whether one-pass dense+sparse improves exact terms and mixed scripts. |
| Compact multilingual | `google/embeddinggemma-300m` | Tests lower CPU/RAM cost and Matryoshka dimensions. |
| Stable multilingual control | `intfloat/multilingual-e5-large-instruct` | Mature MIT-licensed control with known query-prefix requirements. |
| Mature reranker | `BAAI/bge-reranker-v2-m3` | Permissive multilingual cross-encoder baseline. |
| Modern reranker | `Qwen/Qwen3-Reranker-0.6B` | Permissive modern candidate for difficult queries. |

Jina v5 and GTE may be measured in an extended research run, but they cannot become defaults while their noncommercial license or `trust_remote_code` requirement conflicts with the shipping policy. No model dependency, model download, vector dimension, or query prefix becomes a default before Task 10's frozen benchmark passes.

Qwen is not the generation provider. It is only an optional local embedding/reranking candidate. OpenCode, Codex, Claude, OpenAI, and Ollama remain the LLM provider abstraction.

## Target Runtime Layout

The implementation proposes this new derived cache layout:

```text
cache/evidence-graph/
  catalog.sqlite3
  generations/
    <generation-id>/
      manifest.json
      evidence.sqlite3
      search.sqlite3
      vectors.npy
      vectors.json
```

`catalog.sqlite3` contains generation metadata and one active-generation pointer. A generation is immutable after activation. `manifest.json` binds source membership, source SHA-256 values, schema/extractor/tokenizer/model versions, vector dimensions, and every artifact digest. `evidence.sqlite3` stores source records, nodes, occurrences, assertions, evidence, unresolved observations, and dependency records. `search.sqlite3` stores corpus metadata and FTS5. Vector files are optional and must be absent, complete, or explicitly stale; partial vector state is never silently used.

The legacy paths `cache/index.sqlite`, `cache/vectors.npy`, `cache/vectors_meta.json`, and `cache/lancedb/` remain readable during migration. The new reader switches only after a validated generation is active. Removal of legacy readers is outside this plan unless installed-vault migration evidence proves it safe.

Before Task 5 writes this layout, update `docs/STRUCTURE.md`, `AGENTS.md`, `CLAUDE.md`, and `tests/test_structure.py` as required by the repository architecture contract.

## Stable Internal Contracts

### Corpus Generation

```python
@dataclass(frozen=True)
class SourceRecord:
    logical_id: str
    relative_path: str
    sha256: str
    size: int
    media_type: str
    language: str | None
    git_oid: str | None

@dataclass(frozen=True)
class CorpusGeneration:
    generation_id: str
    parent_generation_id: str | None
    source_manifest_sha256: str
    schema_version: str
    extractor_manifest_sha256: str
```

### Retrieval Result

```python
@dataclass(frozen=True)
class RetrievalTrace:
    requested_mode: str
    effective_mode: str
    signals_used: tuple[str, ...]
    fallback_reason: str | None
    corpus_generation: str
    partial: bool

@dataclass(frozen=True)
class RetrievalCandidate:
    candidate_id: str
    parent_id: str
    relative_path: str
    heading_path: tuple[str, ...]
    source_sha256: str
    byte_start: int
    byte_end: int
    bm25_rank: int | None
    bm25_score: float | None
    vector_rank: int | None
    vector_score: float | None
    graph_rank: int | None
    graph_score: float | None
    rrf_score: float
    rerank_score: float | None
    final_score: float
    evidence_ids: tuple[str, ...]
```

Raw backend scores retain their native meaning in named fields. All normalized scores and `final_score` use larger-is-better. Reranking combines with `rrf_score`, never an overloaded backend `score`.

### Context Budget

```python
@dataclass(frozen=True)
class ContextBudget:
    model: str | None
    max_input_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int

@dataclass(frozen=True)
class ContextItem:
    item_id: str
    text: str
    source: str
    priority: int
    relevance: float
    confidence: str
    freshness: str
    token_cost: int
    mandatory: bool
    representation: str
```

The packer includes mandatory items first, rejects an impossible budget explicitly, then selects complete optional items deterministically by priority, relevance, authority, diversity, and token cost. It never slices final Markdown in the middle of an item.

### Evidence

```json
{
  "logical_source_id": "source:knowledge/notes/example.md",
  "relative_path": "knowledge/notes/example.md",
  "source_sha256": "64 lowercase hexadecimal characters",
  "line_start": 10,
  "line_end": 14,
  "byte_start": 312,
  "byte_end": 498,
  "span_sha256": "64 lowercase hexadecimal characters",
  "git_commit": null
}
```

Byte ranges are authoritative and half-open. Line ranges are human-readable metadata. Every confirmed graph assertion and every generated factual QA claim must resolve to evidence from the exact generation supplied to the consumer.

## File Map

### New Core Modules

- `scripts/sync_memory.py`: bounded dependency/integration/index synchronization without knowledge mutation.
- `scripts/generation_catalog.py`: shared immutable-generation registration, validation, compare-and-swap activation, and recovery.
- `scripts/corpus_snapshot.py`: shared page/code/project collection, source hashing, heading-aware chunks, and source manifests.
- `scripts/retrieval.py`: query analysis, retrieval profiles, backend adapters, fusion, candidate schema, and trace.
- `scripts/context_budget.py`: token counters, budget contracts, complete-item packing, and usage-source labels.
- `scripts/context_compiler.py`: L0/L1/L2/parent materialization, diversity, evidence packing, and grounded QA context.
- `scripts/retrieval_telemetry.py`: append-only cross-process impressions, reads, injections, and outcomes with bounded retention.
- `scripts/evidence_graph.py`: graph schema, read queries, evidence validation, and graph-specific integrity checks.
- `scripts/evidence_graph_builder.py`: full and incremental generation construction.
- `scripts/code_extractor.py`: deterministic code nodes, occurrences, edges, and unresolved observations from existing parsers/resolvers.
- `scripts/knowledge_extractor.py`: typed page, wikilink, supersession, claim, evidence, project, and explicit symbol-reference extraction.
- `scripts/project_extractor.py`: journal/checkpoint/project edges without replacing journal authority.
- `scripts/schemas/evidence-graph-manifest-v1.json`: closed generation manifest contract.
- `scripts/schemas/retrieval-trace-v1.json`: MCP/CLI retrieval trace contract.
- `scripts/schemas/grounded-answer-v1.json`: claim/citation/abstention answer contract.

### Primary Modified Modules

- `scripts/build_guardrails.py`
- `scripts/codex_memory.py`
- `scripts/codex-memory-wrapper.ps1`
- `scripts/doctor.py`
- `scripts/search_memory.py`
- `scripts/lance_store.py`
- `scripts/rebuild_lance_index.py`
- `scripts/reranker.py`
- `scripts/access_tracking.py`
- `scripts/contextual_retrieval.py`
- `scripts/build_tiers.py`
- `scripts/query_memory.py`
- `scripts/session_start_context.py`
- `scripts/build_context.py`
- `scripts/llm_client.py`
- `scripts/code_graph.py`
- `scripts/graph_neighbors.py`
- `scripts/impact_analysis.py`
- `scripts/mcp_server.py`
- `scripts/mcp_contract.py`
- `scripts/scheduled_nightly.py`
- `install.ps1`
- `install.sh`
- `pyproject.toml`
- `uv.lock`

### Benchmark And Documentation

- `benchmark/retrieval-v2.schema.json`
- `benchmark/retrieval-v2.json`
- `benchmark/run_retrieval_v2.py`
- `benchmark/graph-fixtures/`
- `benchmark/comparative-v1.schema.json`
- `benchmark/comparative-v1.json`
- `benchmark/run_comparative.py`
- `docs/STRUCTURE.md`
- `docs/ARCHITECTURE.md`
- `docs/USER-GUIDE.md`
- `docs/operating-model.md`
- `docs/ROADMAP-v5.md`
- `AGENTS.md`
- `CLAUDE.md`
- `README.md`
- `README.ru.md`
- `README.zh-CN.md`
- `CHANGELOG.md`

---

## Workstream A: Close Stages 1 And 2

### Task 1: Freeze The Existing Baseline

**Files:**
- Create: `benchmark/baseline-2026-07-16.md`
- Create: `benchmark/baseline-2026-07-16-retrieval.json`
- Modify: `docs/ROADMAP-v5.md`
- Test: existing suite only

- [ ] Record `git status`, current branch, current commit, Python/SQLite versions, and installed optional extras without modifying runtime state.
- [ ] Run hermetic full tests with source root and a temporary state root.
- [ ] Run Ruff over `scripts/`, `tests/`, and `benchmark/`.
- [ ] Run legacy retrieval and contradiction benchmarks and record exact JSON results.
- [ ] Record the current `AGENTS.md`/`CLAUDE.md` SHA-256 and byte equality.

Commands:

```powershell
$env:LLM_WIKI_ROOT='D:\projects\llm-wiki'
$env:LLM_WIKI_STATE_ROOT='C:\Users\User\AppData\Local\Temp\opencode\llm-wiki-stage3-baseline'
uv run pytest -q
uv run ruff check scripts/ tests/ benchmark/
uv run python benchmark/run_benchmark.py --legacy-only --json
uv run python benchmark/run_contradiction_benchmark.py
```

Expected: the existing suite and both existing benchmark gates pass before any behavior changes.

### Task 2: Close The Markdown Writer Boundary

**Files:**
- Modify: `scripts/build_guardrails.py`
- Modify: `scripts/markdown_transaction.py`
- Modify: `tests/test_guardrails.py`
- Modify: `tests/test_automatic_writer_integration.py`
- Modify: `tests/test_security_invariants.py`

- [ ] Add a failing test proving `build_guardrails --apply` delegates the `knowledge/guardrails.md` replacement to `mutate_knowledge()` with a stable operation ID and current-source precondition.
- [ ] Add `knowledge/guardrails.md` to the allowed transaction target contract and writer integration matrix.
- [ ] Run the focused tests and verify the direct `atomic_write()` path fails the boundary assertion.
- [ ] Replace the direct write with one recoverable Markdown mutation; keep non-`--apply` generation read-only.
- [ ] Run the automatic-writer scanner and transaction/security tests.

Command:

```powershell
uv run pytest tests/test_guardrails.py tests/test_automatic_writer_integration.py tests/test_security_invariants.py -q
```

Expected: no authoritative automatic Markdown write bypasses `MarkdownCoordinator`.

### Task 3: Add Bounded Sync

**Files:**
- Create: `scripts/sync_memory.py`
- Create: `tests/test_sync_memory.py`
- Modify: `scripts/doctor.py`
- Modify: `install.ps1`
- Modify: `install.sh`
- Modify: `docs/USER-GUIDE.md`

- [ ] Write failing tests for a dry-run plan, dependency lock check, integration configuration check, derived-index freshness check, bounded repair, and an assertion that no path under `knowledge/` is written.
- [ ] Define ordered sync actions: `environment`, `dependencies`, `integrations`, `transactions`, `queue`, `indexes`, `doctor`.
- [ ] Implement `--check`, `--apply`, and `--json`; default to `--check` when neither action flag is present.
- [ ] Reuse doctor repair primitives and explicit index builders instead of shelling through installers.
- [ ] Make every action idempotent, bounded, and independently reported as `ok`, `changed`, `skipped`, or `error`.
- [ ] Run tests with missing optional dependencies and with a read-only fake knowledge tree.

Public command for this version:

```powershell
uv run python scripts/sync_memory.py --check --json
uv run python scripts/sync_memory.py --apply --json
```

No `[project.scripts]` entry is added yet; packaging is deferred until internal APIs stabilize.

### Task 4: Replace Codex Wrapper Assumptions With Official Hooks

**Files:**
- Modify: `scripts/codex_memory.py`
- Modify: `scripts/codex-memory-wrapper.ps1`
- Create: `integrations/codex/hooks.json`
- Modify: `install.ps1`
- Modify: `install.sh`
- Modify: `scripts/doctor.py`
- Modify: `tests/test_integration_injection.py`
- Modify: `tests/test_plugin_helpers.py`

- [ ] Add hook-input fixtures for `SessionStart`, `PreCompact`, `PostCompact`, and `Stop` with `session_id`, `transcript_path`, `cwd`, `hook_event_name`, and optional turn fields.
- [ ] Add failing tests that normalize those payloads through the shared event envelope and inject SessionStart context through stdout.
- [ ] Add `codex_memory.py hook` to read one JSON object from stdin, validate the event, invoke shared lifecycle code, and emit only Codex-supported output fields.
- [ ] Configure `commandWindows` and POSIX commands in the shipped hook template.
- [ ] Keep the PowerShell wrapper as a compatibility fallback, but make doctor prefer official trusted hooks and report heartbeat-only capture honestly.
- [ ] Add Unix and Windows installer tests that preserve existing user config and never duplicate hook definitions.

Expected: normal Codex use has lifecycle parity on Windows and Unix without relying on an unstable transcript parser.

**Gate A:** Stages 1 and 2 are complete only when the writer scanner is clean, `sync --check` is idempotent, Codex official-hook fixtures pass, doctor reports truthful integration state, and the full baseline remains green.

---

## Workstream B: Measurement, Tokens, And Telemetry

### Task 5: Record The Architecture Decision And Runtime Layout

**Files:**
- Create: `knowledge/notes/derived-evidence-generation-decision.md`
- Modify: `knowledge/index.md`
- Modify: `knowledge/log.md`
- Modify: `.gitignore`
- Modify: `docs/STRUCTURE.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `tests/test_structure.py`

- [ ] Write a public architecture decision with `type: decision`, high confidence, user source authority, date, rationale, rejected alternatives, and a source link to this plan.
- [ ] Add only that public architecture page to the explicit `.gitignore` allowlist; do not broaden the private-note allowlist.
- [ ] State that `cache/evidence-graph/` is disposable derived state and never authoritative.
- [ ] Add the proposed generation layout to `docs/STRUCTURE.md` and forbid generation databases under `run/`.
- [ ] Update root agent contracts byte-identically with generation, deletion, and no-daemon rules.
- [ ] Add structural tests for allowed paths, catalog location, generation contents, and identical agent contracts.
- [ ] Run the structural, OKF, and contract tests before creating runtime code.

The decision page is a public product architecture record, not personal knowledge. Do not include user data, private paths beyond repository-relative examples, or session details.

### Task 6: Add Token And Provider Usage Contracts

**Files:**
- Create: `scripts/context_budget.py`
- Create: `tests/test_context_budget.py`
- Modify: `scripts/llm_client.py`
- Modify: `tests/test_llm_descriptors.py`

- [ ] Write failing tests for `ContextBudget`, `TokenUsage`, and counters with `reported`, `tokenizer`, `estimated`, and `unknown` sources.
- [ ] Add optional usage fields to `LLMResult` after all existing fields so positional construction remains compatible.
- [ ] Parse provider-reported usage from OpenAI, Ollama, and any structured OpenCode response that actually supplies it.
- [ ] Preserve `None` and `cost_kind="unknown"` when CLI providers do not report usage; never fabricate exact values.
- [ ] Add pre-call token counting adapters and conservative safety margins for unknown model tokenizers.
- [ ] Forward provider-specific output limits only when the backend contract supports them and expose `max_tokens_enforced` truthfully.
- [ ] Run all LLM-client, compile, queue, and contradiction tests because they construct or serialize `LLMResult`.

Required result fields:

```python
@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    duration_ms: int | None = None
    estimated_cost: float | None = None
    cost_kind: Literal["reported", "estimated", "unknown"] = "unknown"
```

### Task 7: Make Retrieval Telemetry Durable And Private

**Files:**
- Create: `scripts/retrieval_telemetry.py`
- Create: `tests/test_retrieval_telemetry.py`
- Modify: `scripts/access_tracking.py`
- Modify: `tests/test_access_tracking.py`
- Modify: `scripts/search_memory.py`
- Modify: `scripts/mcp_server.py`

- [ ] Write failing cross-process tests showing an event written by one process is visible to another and survives normal CLI exit.
- [ ] Define separate event kinds: `impression`, `page_read`, `evidence_read`, `context_injected`, `user_selected`, and `task_outcome`.
- [ ] Store append-only JSONL or rollback-journal SQLite under `cache/`; use a lock and one-record atomic append/transaction.
- [ ] Store query SHA-256, retrieval mode, candidate ID, rank, generation, source tool, timestamp, and outcome. Do not store raw query or response text.
- [ ] Add bounded retention and compaction that never rewrites knowledge frontmatter.
- [ ] Preserve the old access frontmatter updater only as an explicit export/promotion operation, not the event transport.
- [ ] Verify exact-title short-circuits and MCP reads emit the correct distinct events.

### Task 8: Build The Frozen Retrieval And QA Corpus

**Files:**
- Create: `benchmark/retrieval-v2.schema.json`
- Create: `benchmark/retrieval-v2.json`
- Create: `benchmark/run_retrieval_v2.py`
- Create: `tests/test_retrieval_v2_benchmark.py`
- Modify: `benchmark/run_benchmark.py`

- [ ] Define a closed corpus schema with native-language query text, language, profile, answerability, relevant parents, required evidence spans, negative candidates, temporal scope, project scope, and allowed abstention reason.
- [ ] Add public synthetic EN/RU/ZH fixtures covering exact names, paths, commands, code symbols, paraphrases, cross-language retrieval, temporal decisions, supersession, contradictions, multi-parent synthesis, no-answer cases, and prompt injection.
- [ ] Keep generated title/summary queries only as a legacy slice; do not use them as the primary quality claim.
- [ ] Report Recall@10/20/50, all-evidence Recall@20, parent Recall@10, nDCG@10, MRR@10, false-answer rate, p50/p95 latency, peak RSS, build time, and index size.
- [ ] Report EN, RU, ZH, and cross-language slices independently and macro-average them.
- [ ] Add deterministic fake embedding/reranker adapters so CI validates orchestration without downloading models.
- [ ] Keep real-model runs explicit and cache-isolated.

Initial release thresholds:

| Metric | Gate |
|---|---|
| Parent Recall@10 | at least 0.95 |
| All-required-evidence Recall@20 | at least 0.85 |
| nDCG@10 | at least 0.80 |
| MRR@10 | at least 0.85 |
| Language gap | no language more than 0.03 below the corresponding overall gate |
| No-answer false-answer rate | at most 0.03 |

These are initial product gates, not universal research constants. Any future threshold change requires a dated benchmark decision and raw before/after results.

**Gate B:** telemetry survives process boundaries, usage labels never overclaim precision, and the frozen benchmark runs deterministically before ranking behavior changes.

---

## Workstream C: Truthful Retrieval And Corpus Generations

### Task 9: Create A Shared Corpus Snapshot

**Files:**
- Create: `scripts/generation_catalog.py`
- Create: `scripts/corpus_snapshot.py`
- Create: `tests/test_generation_catalog.py`
- Create: `tests/test_corpus_snapshot.py`
- Modify: `scripts/search_memory.py`
- Modify: `scripts/rebuild_lance_index.py`
- Modify: `scripts/contextual_retrieval.py`
- Modify: `scripts/build_tiers.py`

- [ ] Write failing catalog tests for immutable generation registration, validated artifact manifests, compare-and-swap activation, orphan recovery, and prior-generation fallback.
- [ ] Implement the shared rollback-journal catalog and active-generation pointer before any new FTS, vector, tier, or graph artifact is published.
- [ ] Write failing corpus tests for active-page filtering, typed subdirectories, duplicate stems, source hashes, content changes with preserved mtimes, and coherent snapshots during concurrent writes.
- [ ] Implement one collector for notes, project state/journal views, allowed daily evidence, and optional code sources.
- [ ] Parse Markdown into heading-aware retrieval chunks while preserving parent page, heading ancestry, byte ranges, source hash, type, project, authority, confidence, status, validity, and language.
- [ ] Exclude superseded and archived pages from active retrieval while preserving explicit historical/as-of access.
- [ ] Use source hashes and collector/extractor versions, never mtime alone, for freshness.
- [ ] Make FTS, NumPy, Lance compatibility, tiers, and contextual indexing consume the same immutable collection result.
- [ ] Reject publication if live membership or any source hash changes between snapshot and activation.

### Task 10: Benchmark Lexical And Model Candidates

**Files:**
- Modify: `benchmark/run_retrieval_v2.py`
- Create: `benchmark/model-matrix-v1.json`
- Create: `tests/test_model_policy.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] Add lexical configurations for mixed `unicode61`, English Porter side-index, Chinese trigram fallback, and one pinned segmentation strategy used identically for documents and queries.
- [ ] Run BM25-only ablations before adding dense models.
- [ ] Run the required embedding matrix with documented query prefixes, dimensions, precision, batch size, max input length, and immutable model revision.
- [ ] Run rerankers on identical frozen candidate lists at top 10, 20, and 50.
- [ ] Measure cold/warm latency, indexing throughput, peak RSS, vector bytes, and per-language quality.
- [ ] Reject a model that improves macro-average while violating a language, license, remote-code, RAM, or latency gate.
- [ ] Select and pin one default embedding model, dimensions, and query/document formatting only if it materially beats the current baseline.
- [ ] Keep semantic retrieval optional and preserve BM25-only operation when model dependencies or vectors are unavailable.

Selection rule:

```text
eligible = permissive shipping terms
           AND no unreviewed remote model code
           AND every language gate passes
           AND parent Recall@10 does not regress
winner = Pareto-efficient quality, p95 latency, peak RSS, and index size
```

There is no predetermined Qwen winner.

### Task 11: Introduce The Retrieval Contract And Planner

**Files:**
- Create: `scripts/retrieval.py`
- Create: `scripts/schemas/retrieval-trace-v1.json`
- Create: `tests/test_retrieval.py`
- Modify: `scripts/search_memory.py`
- Modify: `tests/test_search_ranking.py`

- [ ] Write failing tests for result fields, larger-is-better normalized scores, deterministic ties, requested/effective modes, used signals, fallback reasons, and source generation.
- [ ] Implement deterministic query analysis for exact identifiers, quoted phrases, natural-language questions, temporal language, graph relations, repo-map requests, impact requests, and global synthesis.
- [ ] Implement profiles `DIRECT`, `EXACT`, `BASE`, `HYBRID`, `GRAPH`, `TEMPORAL`, `REPO_MAP`, `IMPACT`, `GLOBAL`, and `CACHED_FULL`.
- [ ] Run lexical and dense retrieval independently with identical hard filters.
- [ ] Fuse ranks with RRF; keep raw BM25, cosine, and distance fields separate.
- [ ] Preserve the existing `search()` API as a compatibility wrapper over the new orchestrator.
- [ ] Add CLI switches for requested profile and explicit graph/reranker disablement.

### Task 12: Fix Vector Freshness And Backend Parity

**Files:**
- Modify: `scripts/search_memory.py`
- Modify: `scripts/lance_store.py`
- Modify: `scripts/rebuild_lance_index.py`
- Modify: `tests/test_lance_store.py`
- Modify: `tests/test_search_ranking.py`

- [ ] Write failing tests for deleted, changed, superseded, wrong-model, wrong-dimension, partial, and corrupt vector generations.
- [ ] Publish NumPy vector and metadata artifacts as one validated immutable generation.
- [ ] Keep the NumPy matrix memory-mapped or contiguous; do not convert it to a Python list.
- [ ] Apply `project`, `since`, `as_of`, validity, status, and authority filters identically across exact NumPy and Lance compatibility paths.
- [ ] Convert Lance distance to an explicitly named distance and normalized larger-is-better similarity before fusion.
- [ ] Refuse stale vectors with `effective_mode="base"` and a concrete fallback reason.
- [ ] Replace destructive active-table rebuild with build-validate-activate behavior.
- [ ] Correct HNSW/IVF documentation to match the implementation actually selected by benchmark.

### Task 13: Correct Reranking And Conditional Invocation

**Files:**
- Modify: `scripts/reranker.py`
- Modify: `tests/test_reranker.py`
- Modify: `scripts/retrieval.py`

- [ ] Replace the permanently skipped successful-path test with a deterministic fake cross-encoder test.
- [ ] Cache model and tokenizer together.
- [ ] Preserve candidates beyond the reranked prefix when the requested result limit exceeds the rerank depth.
- [ ] Blend a normalized reranker value with `rrf_score`, then sort by `final_score` with deterministic ties.
- [ ] Invoke reranking only for configured profiles when rankings disagree, top scores are close, or cross-language/synthesis ambiguity exists.
- [ ] Bypass reranking for exact titles, paths, symbols, IDs, quoted phrases, and tiny hard-filtered sets.
- [ ] Report `reranker_applied`, model ID/revision, depth, duration, and fallback reason.

**Gate C:** no stale or mismatched backend is silently used; every result reports its generation and actual signals; filters have backend parity; reranking cannot mix raw backend magnitudes.

---

## Workstream D: Adaptive Context Compiler And Grounded QA

### Task 14: Implement Complete-Item Token Packing

**Files:**
- Modify: `scripts/context_budget.py`
- Create: `tests/test_context_packing.py`
- Modify: `scripts/session_start_context.py`
- Modify: `scripts/build_context.py`
- Modify: `tests/test_context_noise.py`

- [ ] Write failing tests for mandatory-item reservation, per-section bounds, deterministic selection, diversity, impossible budgets, Unicode/code token costs, and no mid-item truncation.
- [ ] Implement priority classes for safety, degraded health, active project handoff, blockers, decisions, evidence, and optional history.
- [ ] Pack mandatory complete items first and fail visibly when they exceed the usable budget.
- [ ] Select optional items by deterministic utility per token with per-source and per-parent diversity caps.
- [ ] Keep a final emergency byte cap only as a failure guard; it must drop a whole item and emit a warning rather than slice Markdown.
- [ ] Return packed token count, counter source, dropped item IDs, and reasons.
- [ ] Replace independent SessionStart/project character caps with one shared budget.

### Task 15: Connect L0/L1/L2 And Deterministic Context

**Files:**
- Create: `scripts/context_compiler.py`
- Create: `tests/test_context_compiler.py`
- Modify: `scripts/build_tiers.py`
- Modify: `scripts/contextual_retrieval.py`
- Modify: `scripts/build_advisory.py`

- [ ] Write failing tests for broad L0 candidates, L1 promotion, L2/source evidence, source-hash invalidation, duplicate stems, and generated-context disablement.
- [ ] Key tier and context caches by logical path plus source hash, generator version, and model descriptor where applicable.
- [ ] Prefix chunks deterministically with page title, heading ancestry, project, type, status, aliases, and validity metadata.
- [ ] Use L0 for broad ranking metadata, L1 for shortlisted orientation, and L2/source spans for final evidence.
- [ ] Expand small parent pages in full; expand large pages to the matched heading subtree plus bounded adjacent context.
- [ ] Keep LLM-generated contextual text disabled by default until a frozen ablation improves retrieval without violating ingestion-cost or faithfulness gates.
- [ ] Return a retrieval trace and materialization trace with every compiled package.

### Task 16: Replace Full-Index Querying With Grounded QA

**Files:**
- Modify: `scripts/query_memory.py`
- Create: `scripts/schemas/grounded-answer-v1.json`
- Create: `tests/test_grounded_qa.py`
- Modify: `scripts/evidence_resolver.py`

- [ ] Write failing tests proving ordinary QA never sends all of `knowledge/index.md`, while a measured `CACHED_FULL` profile may do so for a genuinely small vault.
- [ ] Retrieve child candidates, group by parent, materialize parent sections/pages, and pack them under one budget.
- [ ] Render each source as untrusted evidence with citation ID, relative path, source hash, revision, and span.
- [ ] Require structured statuses `answered`, `insufficient_evidence`, `conflicting_evidence`, and `unsupported_time_scope`.
- [ ] Require atomic factual claims with adjacent citation IDs.
- [ ] Verify citation IDs, paths, root containment, source hashes, ranges, span hashes, and that every cited span was supplied to the model.
- [ ] Reject generated-summary citations as authoritative evidence.
- [ ] Keep QA generation read-only; it receives no shell, network, mutation, or arbitrary-file tool.
- [ ] Preserve `--file-back` through the existing Markdown transaction boundary only after the answer passes verification.

QA hard gates:

| Metric | Gate |
|---|---|
| Citation reference validity | 1.00 |
| Cited span present in generation context | 1.00 |
| Factual claims cited or explicit abstention | 1.00 |
| Citation precision | at least 0.97 |
| Citation recall | at least 0.95 |
| Unanswerable abstention recall | at least 0.95 |
| Unauthorized path/tool action | 0 |

### Task 17: Integrate Compiler With Existing MCP Tools

**Files:**
- Modify: `scripts/mcp_server.py`
- Modify: `scripts/mcp_contract.py`
- Modify: `tests/test_mcp_server.py`
- Modify: `tests/test_mcp_contract.py`

- [ ] Make `recall` call the planner and expose requested/effective mode, signals, generation, reranker state, and fallback reason.
- [ ] Make `get_context` return a token-budgeted package with repo map, relevant pages/symbols, decisions, incidents, active task, and evidence.
- [ ] Extend `get_architecture` mode values to `summary`, `symbol`, `callers`, `callees`, `dependencies`, `path`, `community`, and `impact` without adding a tool.
- [ ] Keep `find_dead_code` and other graph-dependent tools on live-scan fallback until an Evidence Graph generation is active.
- [ ] Replace FTS-file-age freshness with per-component generation/freshness details.
- [ ] Validate the structured retrieval trace against the committed closed schema.
- [ ] Keep text JSON compatibility and the existing common MCP envelope.

**Gate D:** SessionStart and explicit query/context calls obey one token budget, use tiers deliberately, preserve evidence, and never claim hybrid/graph/reranker use that did not occur.

---

## Workstream E: Persistent Evidence Graph

### Task 18: Implement Evidence Graph Schema And Queries

**Files:**
- Create: `scripts/evidence_graph.py`
- Create: `scripts/schemas/evidence-graph-manifest-v1.json`
- Create: `tests/test_evidence_graph.py`
- Create: `tests/test_evidence_graph_recovery.py`
- Modify: `scripts/generation_catalog.py`

- [ ] Write failing schema tests for generations, source snapshots, generation sources, nodes, occurrences, assertions, evidence, observations, and dependencies.
- [ ] Reuse the Task 9 catalog for graph generation registration and activation; do not create a second active pointer or graph-only catalog.
- [ ] Create immutable generation databases with indexes for both traversal directions, source spans, node kinds, resolution status, and dependency invalidation.
- [ ] Store logical node identity separately from occurrences and source locations.
- [ ] Store unresolved observations with controlled reasons rather than fake target nodes.
- [ ] Validate every evidence byte range and span hash against the captured source hash.
- [ ] Add bounded recursive-CTE queries for neighbors, paths, callers, callees, dependencies, code-to-doc, doc-to-code, evidence, and unresolved observations.

Minimum generation tables:

```sql
CREATE TABLE source (
  source_id TEXT PRIMARY KEY,
  relative_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  media_type TEXT NOT NULL,
  language TEXT,
  git_oid TEXT
);
CREATE TABLE node (
  node_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  identity_scheme TEXT NOT NULL,
  identity_key TEXT NOT NULL,
  metadata_json TEXT NOT NULL
);
CREATE TABLE occurrence (
  occurrence_id TEXT PRIMARY KEY,
  node_id TEXT,
  source_id TEXT NOT NULL,
  role TEXT NOT NULL,
  byte_start INTEGER NOT NULL,
  byte_end INTEGER NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL
);
CREATE TABLE assertion (
  assertion_id TEXT PRIMARY KEY,
  source_node_id TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  target_node_id TEXT,
  literal_json TEXT,
  confidence TEXT NOT NULL,
  authority TEXT NOT NULL,
  resolution TEXT NOT NULL,
  extractor TEXT NOT NULL
);
CREATE TABLE evidence (
  evidence_id TEXT PRIMARY KEY,
  assertion_id TEXT,
  observation_id TEXT,
  source_id TEXT NOT NULL,
  byte_start INTEGER NOT NULL,
  byte_end INTEGER NOT NULL,
  span_sha256 TEXT NOT NULL
);
```

### Task 19: Build Atomic Full Generations

**Files:**
- Create: `scripts/evidence_graph_builder.py`
- Create: `tests/test_evidence_graph_builder.py`
- Modify: `scripts/reliable_memory.py`

- [ ] Write kill-point tests before generation directory creation, during extraction, after database commit, after validation, before activation, and after activation.
- [ ] Snapshot source membership and exact hashes before extraction.
- [ ] Build an unpublished generation directory and database while readers continue using the prior generation.
- [ ] Validate schema version, manifests, foreign keys, `PRAGMA integrity_check`, evidence spans, artifact hashes, and source membership.
- [ ] Fsync files and directories where supported.
- [ ] Activate by compare-and-swap against the expected active generation in one short catalog transaction.
- [ ] On startup, ignore orphan/incomplete generations and fall back to the prior valid generation if the active target is missing or corrupt.
- [ ] Never modify an active generation in place.

### Task 20: Extract Code Into The Graph

**Files:**
- Create: `scripts/code_extractor.py`
- Create: `tests/test_code_extractor.py`
- Modify: `scripts/code_graph.py`
- Modify: `tests/test_code_graph.py`

- [ ] Refactor current parsing/resolution into a pure extractor that accepts immutable source records and returns nodes, occurrences, assertions, evidence, and observations.
- [ ] Emit repository, directory, file, module, class, function, method, route, table, and entry-point nodes where supported.
- [ ] Emit `CONTAINS`, `DEFINES`, `IMPORTS`, `CALLS`, `INHERITS`, `IMPLEMENTS`, `EXPOSES`, `READS`, `WRITES`, and `CO_CHANGED_WITH` only at the resolver's honest confidence/resolution level.
- [ ] Use SCIP symbols where available; otherwise use repository ID, language, path, qualified owner/name, and normalized signature. Never use line number as logical identity.
- [ ] Keep dynamic dispatch, ambiguous targets, missing dependencies, parse errors, and unsupported semantics as unresolved observations.
- [ ] Preserve line/byte spans for every declaration and confirmed relationship.
- [ ] Keep `code_graph.py` public functions as store-first facades with on-demand extraction fallback.

### Task 21: Extract Knowledge And Project Evidence

**Files:**
- Create: `scripts/knowledge_extractor.py`
- Create: `scripts/project_extractor.py`
- Create: `tests/test_knowledge_extractor.py`
- Create: `tests/test_project_extractor.py`
- Modify: `scripts/graph_neighbors.py`
- Modify: `scripts/claims.py`
- Modify: `scripts/project_journal.py`

- [ ] Emit typed knowledge-page, decision, debugging-note, project, checkpoint, session, claim, and evidence nodes from authoritative sources.
- [ ] Emit inbound and outbound `LINKS_TO`, `BELONGS_TO_PROJECT`, `EVIDENCED_BY`, and `SUPERSEDES` assertions with exact source spans.
- [ ] Emit `REFERENCES_SYMBOL` as confirmed only for explicit canonical symbol references, unambiguous path-qualified references, or compiler-backed IDs.
- [ ] Store bare-name mentions and ambiguous links as observations, not confirmed edges.
- [ ] Project journal events to `PROJECT_HAS_CHECKPOINT`, `CHECKPOINT_CHANGED_FILE`, `CHECKPOINT_RECORDED_DECISION`, `CHECKPOINT_HAS_BLOCKER`, and `CHECKPOINT_EVIDENCED_BY_EVENT` without mutating the journal.
- [ ] Replace process-local wikilink adjacency with active-generation reads; keep source-scan fallback when no valid graph exists.
- [ ] Remove all dependence on `list(set(...))` ordering and use explicit hop count plus deterministic node ordering.

### Task 22: Connect Graph Retrieval Without Letting It Dominate

**Files:**
- Modify: `scripts/retrieval.py`
- Modify: `scripts/context_compiler.py`
- Create: `tests/test_graph_retrieval.py`

- [ ] Seed graph expansion only from high-confidence lexical/dense candidates.
- [ ] Traverse one typed hop by default with per-edge decay and per-seed/global caps.
- [ ] Include inbound and outbound edges where the query profile permits them.
- [ ] Preserve the exact path and evidence that caused every expanded result.
- [ ] Rerank expanded nodes against the original query before final fusion.
- [ ] Prevent graph-only proximity from outranking weak textual relevance unless the requested operation is a direct graph query.
- [ ] Benchmark graph expansion separately and disable an edge family that lowers held-out quality.

**Gate E:** a full graph generation is reproducible, crash-safe, evidence-verifiable, and queryable; code and wikilink tools prefer it but retain honest fallback; graph expansion is bounded and explainable.

---

## Workstream F: Incremental Project Intelligence

### Task 23: Add Incremental Generation Reuse

**Files:**
- Modify: `scripts/evidence_graph_builder.py`
- Create: `tests/test_evidence_graph_incremental.py`

- [ ] Hash added, changed, deleted, and renamed source snapshots.
- [ ] Reuse extraction only when source hash, extractor version, grammar/compiler version, resolver config, schema version, and workspace manifest all match.
- [ ] Re-extract changed files and invalidate reverse dependents when exports, imports, signatures, aliases, or project metadata change.
- [ ] Remove records derived exclusively from deleted sources in the new generation.
- [ ] Record every reused or rebuilt record's dependencies.
- [ ] Publish a complete immutable generation even when most records are reused.
- [ ] Compare canonical node/assertion/evidence output against a clean full rebuild for every fixture.

### Task 24: Replace Regex Impact With Diff-To-Graph Traversal

**Files:**
- Modify: `scripts/impact_analysis.py`
- Modify: `tests/test_impact_analysis.py`
- Modify: `scripts/mcp_server.py`

- [ ] Parse explicit Git endpoints: worktree versus index, index versus `HEAD`, two commits, or merge-base versus branch.
- [ ] Union staged and unstaged changes for the default dirty-worktree view.
- [ ] Load old and new blobs, including deleted files, using machine-readable zero-delimited diff records.
- [ ] Map changed byte/line ranges to enclosing old/new canonical symbols.
- [ ] Traverse confirmed callers/importers and explicit page-to-symbol edges.
- [ ] Return affected decisions, pages, tests, and project checkpoints with evidence paths.
- [ ] Classify output as `exact`, `conservative`, or `unresolved`.
- [ ] Keep textual name matching only as a separately labeled low-confidence fallback.

### Task 25: Move Code Intelligence Tools To Store-First Queries

**Files:**
- Modify: `scripts/code_graph.py`
- Modify: `scripts/mcp_server.py`
- Modify: `tests/test_code_graph.py`
- Modify: `tests/test_mcp_server.py`

- [ ] Make callers, callees, dead code, routes, entry points, hotspots, communities, dependencies, paths, and architecture read the active generation.
- [ ] Preserve on-demand extraction only when no valid generation exists or the caller explicitly requests live fallback.
- [ ] Report source generation, graph completeness, unresolved edge count, and fallback state.
- [ ] Cache bounded derived community results inside a generation, not in process globals.
- [ ] Verify concurrent readers continue using the prior generation during rebuild and switch cleanly after activation.

### Task 26: Maintain Generations Through Doctor, Nightly, And Sync

**Files:**
- Modify: `scripts/doctor.py`
- Modify: `scripts/scheduled_nightly.py`
- Modify: `scripts/sync_memory.py`
- Modify: `tests/test_doctor.py`
- Create: `tests/test_generation_maintenance.py`

- [ ] Add doctor checks for active generation, catalog/schema version, source manifest, evidence integrity, vector model/dimension, unindexed delta, unresolved observations, and age.
- [ ] Add safe repair for orphan cleanup, prior-generation fallback, and explicit rebuild; never delete the only valid generation.
- [ ] Make nightly maintenance build or incrementally refresh a generation instead of constructing process-local graphs that disappear.
- [ ] Fence maintenance through the existing maintenance ownership contract.
- [ ] Bound work by elapsed time and source count; return partial state for a deferred continuation rather than holding a daemon.
- [ ] Make `sync --check` report stale generations and `sync --apply` invoke the same bounded builder.

**Gate F:** incremental output equals a clean rebuild, deletes and renames are correct, impact results preserve uncertainty, and all code/knowledge graph tools use active generation or explicit fallback.

---

## Workstream G: Comparative Proof And Release

### Task 27: Build A Pinned Graphify Comparative Harness

**Files:**
- Create: `benchmark/comparative-v1.schema.json`
- Create: `benchmark/comparative-v1.json`
- Create: `benchmark/run_comparative.py`
- Create: `tests/test_comparative_benchmark.py`

- [ ] Pin Graphify to commit `cb96bdaa0c367bec8d5c5aee5d7c9ebb727e9780`, Python version, dependencies, model, tokenizer, and configuration.
- [ ] Define adapters for grep/read baseline, pinned Graphify, current LLM Wiki, Evidence Graph only, hybrid retrieval, and Adaptive Context Compiler.
- [ ] Run each system on identical repositories, commits, hardware, task prompts, model, context budget, and retry policy.
- [ ] Measure executable task success where possible, blinded factual correctness otherwise, retrieval quality, total uncached input/output/cache tokens, indexing time, incremental time, p50/p95 query latency, peak RAM, index size, edge precision/recall, and freshness.
- [ ] Store raw per-task ledgers and all failures; never publish only averages.
- [ ] Use paired bootstrap or randomization confidence intervals and multiple seeds for stochastic agent tasks.
- [ ] Keep the comparative suite optional in ordinary CI; add deterministic adapter/schema smoke tests to CI.

Public superiority gate:

```text
quality lower confidence bound > -0.02 versus pinned Graphify
AND token-ratio upper confidence bound < 0.90
AND evidence/freshness/crash hard gates pass
```

A stronger `quality lower bound > 0` may support a superiority claim after the task set is large enough. The existing Graphify six-question code result is not sufficient for that claim.

### Task 28: Run Scale And Failure Matrices

**Files:**
- Create: `benchmark/run_scale_matrix.py`
- Create: `tests/test_scale_matrix.py`

- [ ] Generate synthetic but structurally realistic corpora at 1k, 5k, 20k, 50k, and 100k chunks.
- [ ] Compare exact NumPy, optional sqlite-vec research path, USearch, LanceDB flat/ANN, and any selected model dimensions.
- [ ] Measure 100%, 10%, 1%, and 0.1% filter selectivity.
- [ ] Measure cold/warm p50/p95/p99, batch throughput, RSS, disk, build, update, delete, startup, and concurrent readers.
- [ ] Compute ANN Recall@10/50 against exact NumPy ground truth.
- [ ] Inject crash points before fsync, before activation, and after activation on Windows and Unix CI where available.
- [ ] Keep exact search unless it exceeds the product p95 target materially and ANN achieves at least 0.98 recall with at least 2x measured latency improvement.

### Task 29: Security And Adversarial QA Gates

**Files:**
- Create: `benchmark/adversarial-retrieval-v1.json`
- Create: `tests/test_retrieval_security.py`
- Modify: `scripts/context_compiler.py`
- Modify: `scripts/query_memory.py`

- [ ] Add EN/RU/ZH prompt-injection fixtures in prose, code blocks, HTML comments, Unicode obfuscation, Base64, split documents, and conflicting parent pages.
- [ ] Verify retrieved content cannot alter retrieval filters, output schema, citation validation, tool permissions, or file roots.
- [ ] Verify QA has no mutation, shell, network, or arbitrary file capability.
- [ ] Require zero unauthorized path reads, tool actions, knowledge writes, or policy changes.
- [ ] Measure direct answer-manipulation attack success separately and require at most 1% on the frozen adversarial set.

### Task 30: Documentation, Migration, And Release Evidence

**Files:**
- Modify: `README.md`
- Modify: `README.ru.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/STRUCTURE.md`
- Modify: `docs/USER-GUIDE.md`
- Modify: `docs/operating-model.md`
- Modify: `docs/ROADMAP-v5.md`
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml` only for an explicitly approved release version

- [ ] Document authoritative versus derived state, generation activation/recovery, cache deletion, fallback behavior, model policy, token usage labels, and evidence citations.
- [ ] Document migration from legacy FTS/vector/Lance paths and retain rollback instructions that do not delete knowledge.
- [ ] Document every MCP behavior change without changing the 12-tool count.
- [ ] Synchronize English, Russian, and Chinese README claims in one change.
- [ ] Update test counts only from a fresh collection.
- [ ] Update version and changelog only when the operator explicitly authorizes a release.
- [ ] Run README i18n, structure, full pytest, Ruff, lint, retrieval, contradiction, graph, QA, security, scale smoke, and comparative smoke gates.

Final verification commands:

```powershell
$env:LLM_WIKI_ROOT='D:\projects\llm-wiki'
$env:LLM_WIKI_STATE_ROOT='C:\Users\User\AppData\Local\Temp\opencode\llm-wiki-stage3-final'
uv run pytest -q
uv run ruff check scripts/ tests/ benchmark/
uv run python scripts/lint_memory.py --scope all --fail-on-findings
uv run python benchmark/run_benchmark.py --legacy-only --json
uv run python benchmark/run_contradiction_benchmark.py
uv run python benchmark/run_retrieval_v2.py --json
uv run python benchmark/run_comparative.py --smoke --json
uv run python benchmark/run_scale_matrix.py --smoke --json
```

**Gate G:** all hard correctness, citation, security, freshness, and crash gates pass; retrieval meets per-language thresholds; no optional model or ANN backend is selected without measured evidence; public comparison claims include pinned baselines, raw ledgers, and uncertainty.

---

## Deferred Major-Version Work

The following work is intentionally outside the critical implementation path:

- read-only visual graph explorer;
- Docling PDF/DOCX/PPTX/XLSX/HTML/image/audio/video ingestion;
- Streamable HTTP MCP;
- conventional Python package migration and stable `llm-wiki` console suite;
- GraphML/JSON/Cypher export beyond benchmark needs;
- Kotlin, Swift, Scala, Dart, Lua, SQL, and HCL capability expansion;
- broad SCIP/LSP/compiler adapters beyond measured demand;
- eager historical Git graph backfill and full bitemporal query language;
- cross-repository and cross-service topology;
- learned retrieval router or self-modifying ranking policy;
- LLM-generated graph edges, communities, or contextual summaries by default.

Each deferred feature needs its own design, benchmark, and architecture approval. None may be smuggled into the graph foundation as an undocumented convenience.

## Execution Order And Checkpoints

Execute tasks in numeric order. Parallel work is allowed only inside these independent groups after their prerequisites pass:

- Tasks 6, 7, and 8 may proceed in parallel after Task 5.
- Tasks 12 and 13 may proceed in parallel after Tasks 9 and 11.
- Tasks 14 and 15 may proceed in parallel after Task 6 and Task 11.
- Tasks 20 and 21 may proceed in parallel after Tasks 18 and 19.
- Tasks 27, 28, and 29 may proceed in parallel after Gate F.

Stop and review at every lettered gate. Do not start the next workstream with a failing prior gate or unexplained metric regression.

Suggested commit boundaries, only when the operator explicitly requests commits:

1. Stage 1/2 closure.
2. Token and telemetry contracts plus frozen benchmark.
3. Corpus generation and truthful retrieval.
4. Context Compiler and grounded QA.
5. Full Evidence Graph generation.
6. Incremental project intelligence.
7. Comparative proof, docs, and release evidence.

## Definition Of Done

This roadmap is complete when all statements below are true:

- Every automatic authoritative Markdown write uses the recoverable transaction boundary.
- `sync` and official Codex hooks work idempotently on Windows and Unix.
- Every retrieval response states requested/effective mode, actual signals, generation, freshness, and fallback.
- FTS, vectors, tiers, graph, and evidence consumed together belong to one validated generation or are explicitly marked partial.
- Query answering retrieves evidence instead of sending the full index by default.
- SessionStart, project context, MCP context, and QA use one token-aware complete-item packer.
- Code and wikilink graphs persist as rebuildable immutable generations.
- Callers, callees, dead code, architecture, communities, and impact use store-first queries with honest fallback.
- Incremental generation output is equivalent to a clean full rebuild.
- Confirmed graph assertions and factual QA claims have verifiable evidence.
- EN/RU/ZH retrieval and QA gates pass independently.
- No selected model, reranker, or ANN backend is justified only by a vendor leaderboard.
- Graphify comparisons are pinned, paired, reproducible, and publish raw evidence.
- Full tests, Ruff, structural lint, existing benchmarks, new retrieval/QA/security gates, and smoke scale/comparative suites pass from a clean temporary state root.

## Self-Review

- Scope coverage: completed Stage 1/2 work is preserved; known residual gaps, prior Stage 3 goals, Graphify-oriented integration, current model/index practices, Evidence Graph, Context Compiler, incremental indexing, QA, security, and comparative proof all map to explicit tasks.
- Architecture consistency: every new database is derived under `cache/`; Markdown/Git/journals remain authoritative; no daemon, WAL, cloud, automatic Git operation, or additional MCP tool is introduced.
- Dependency consistency: measurement precedes model/backend selection; truthful generations precede adaptive compilation; stable identities and full generation activation precede incremental reuse; comparative claims follow correctness and security gates.
- Placeholder scan: the plan contains no deferred implementation disguised as a required task. Explicitly deferred major-version features are listed as non-goals and require separate designs.
- Type consistency: generation, retrieval, context, evidence, telemetry, and answer contracts have one name and direction throughout the plan.
