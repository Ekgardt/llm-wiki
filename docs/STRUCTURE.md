# Canonical Structure Reference

> **Single source of truth for the llm-wiki repository layout.**
> Any agent working in this repo MUST read this file before changing
> structure, paths, or env contracts. Changes require explicit user sign-off
> (see `../AGENTS.md` §0 — the root agent contract). The
> `tests/test_structure.py` suite enforces the invariants defined here.

## Three-zone layout

The [comparative adapter fixture](modules/comparative-adapter-fixture.md) retains
the existing deterministic subprocess test protocol in place. Its synthetic
metrics exercise the benchmark runner; they are not measured product results.

The [BEAM data owner](modules/beam-data.md) keeps the existing Parquet converter
and its seven original tests. The optional `benchmark-data` extra supplies
PyArrow 26.0.1; an explicitly selected Parquet suite and a three-OS CI job test
real file decoding. Base installation and ordinary base-test discovery remain
unchanged. No benchmark corpus or model is downloaded by these fixture checks.

The [cross-service fixture owner](modules/cross-service-fixture.md) qualifies
the existing two-repository Git fixture builder and its task/gold JSON in place.
The parity runner remains a separate consumer; fixture checks do not run a
model or establish comparative performance.

CI shard costs remain in `tests/shard_weights.json`, maintained by
`tests/shard_plan.py`. Incremental measurements preserve the slower recorded
cost; an explicit `--refresh` replaces costs from a reviewed complete
cross-platform JUnit set. A partial or local run is not a complete refresh.
This keeps the existing file layout, five shards and job deadlines unchanged.

```
llm-wiki/                          ← vault root (= $LLM_WIKI_ROOT)
│
├── scripts/                       CODE — pipeline + hooks + helpers
│   ├── reliable_memory.py            SQLite durability/default primitives
│   ├── markdown_transaction.py       recover/undo/prune Markdown transactions (facade; code in llm_wiki/markdown_*.py)
│   ├── project_journal.py            checkpoints + deterministic state projection
│   ├── queue_core.py                 fenced SQLite priority queue (facade; worker, results, failures, purge, sources and v2 migration in llm_wiki/queue_worker.py, llm_wiki/memory_queue_*.py, llm_wiki/queue_v2_migration.py; v3 reader mixins in llm_wiki/queue_reader_*.py)
│   ├── memory_queue.py               queue operator command; pickle names of queue records
│   ├── compile_cache.py              content-addressed compile plans + receipts
│   ├── compile_memory.py             compile command at its documented path
│   ├── compile_pipeline.py           compile library beneath the command (facade; code in llm_wiki/compile_pipeline_*.py)
│   ├── compile_receipts.py           compile receipt selection and publication
│   ├── compile_leased_run.py         one compile task leased from the queue
│   ├── compile_lock.py               the single compile lock file
│   ├── repository_index.py           repository index command at its documented path
│   ├── repository_indexing.py        repository index library beneath the command
│   ├── generation_search.py          search over the active generation
│   ├── generation_fts_validation.py  sealed full-text index validation
│   ├── capture_publication.py        durable publication of a capture intent
│   ├── capture_intent_records.py     capture decisions, Markdown commit, terminal records
│   ├── capture_handler_identity.py   capture handler versions
│   ├── prompt_capture_followup.py    running capture started from the prompt hook
│   ├── project_checkpoint.py         project checkpoint debounce and drain
│   ├── breadcrumb_bundle.py          reads a stored breadcrumb bundle back
│   ├── install_state.py              install state records and provider bundle proof
│   ├── codex_config_state.py         Codex configuration state
│   ├── reliability_v3_validation.py  Reliability v3 database validation
│   ├── weekly_budget.py              worst-case seconds of the weekly pass
│   ├── archive_daily.py              immutable daily-log BagIt archive
│   ├── evidence_resolver.py          flat/archive evidence resolution
│   ├── claims.py                     atomic claims + quarantine
│   ├── contradiction_pipeline.py     claim contradiction policy
│   ├── generation_catalog.py         immutable generation catalog + activation
│   ├── corpus_snapshot.py            source-hash corpus snapshots + chunks
│   ├── lsp_paths.py                  pure managed Pyright/LSP path derivation
│   ├── lsp_protocol.py               strict bounded single-writer LSP transport
│   ├── lsp_process_tree.py           POSIX group / Windows Job ownership
│   ├── lsp_process.py                leased LSP lifecycle + one restart
│   ├── lsp_security.py               contained repository/config/source reads
│   ├── pyright_profile.py            pinned identity discovery and qualification
│   ├── install_pyright.py            explicit managed-package installer
│   ├── install_control.py            resumable install/update/rollback ownership
│   ├── install_takeover.py           keeps and retires copies of replaced owned files, plans drop-ins
│   ├── integration_hook_config.py    bounded host hook-config projections
│   ├── pyright_session.py            Pyright readiness, sync, and semantic provider
│   ├── workspace_revision.py         bounded pre/post freshness proofs
│   ├── code_navigation.py            normalized precise-navigation facade
│   ├── code_navigation_renderer.py   deterministic compact result windows
│   ├── windows_workspace.py          Windows handle-relative filesystem boundary
│   ├── schemas/                      transaction/queue/compile/archive/claim schemas
│   ├── reranker.py                  cross-encoder reranker: bge-reranker-v2-m3, int8, on by default (2026-09-10)
│   ├── install_models.py            fetch and verify the two pinned models (the one network step for weights)
│   ├── access_tracking.py           explicit telemetry promotion + decay stats
│   ├── retrieval_telemetry.py       private bounded retrieval event cache
│   ├── reflection.py                v4.0: A-MEM page consolidation
│   ├── mcp_server.py                v4.0: MCP server (12 task-shaped tools, stdio)
│   ├── mcp_supervisor.py            stdio supervisor: reloads the server child on code change
│   ├── integration_adapter.py       v4.x: thin native lifecycle adapter (facade; code in llm_wiki/integration_*.py)
│   ├── event_envelope.py            v4.x: shared lifecycle event contract
│   ├── mcp_contract.py              v4.x: uniform MCP response envelope/resources
│   ├── llm_wiki/mcp_response_values.py Pure MCP schema and response value rules
│   ├── doctor.py                    v4.x: degraded-only health + safe repair (facade; checks in llm_wiki/doctor_*.py)
│   ├── generation_maintenance.py    builds and repairs the search generation
│   ├── nightly_budget.py            time budget of the nightly pass
│   ├── repair_installed_memory.py   explicit check/apply v3 adoption
│   ├── code_graph.py                v4.0: tree-sitter code intelligence
│   ├── impact_analysis.py           v4.0: LINK layer (code→wiki impact)
│   ├── impact_symbols.py            #24 B5: code symbols a diff reaches, beside impact
│   ├── symbol_search.py             #24 B2: ranked name search over the generation
│   ├── symbol_snippet.py            CODE-02 / #24 B3: exact snippet by qualified name
│   ├── path_coverage.py             CODE-05 / #24 B1: per-path index, freshness, parse ranges
│   ├── build_tiers.py               v4.0: L0/L1/L2 progressive disclosure
│   └── queries/                     v4.0: 12 tree-sitter .scm language queries
├── tests/                         CODE — full regression suite (pytest)
├── docs/                          CODE — architecture + user guide
├── skills/                        CODE — 9 agent skills (SKILL.md)
├── rules/                         CODE — file-handling policies
├── integrations/                  CODE — agent integrations
│   ├── claude-code/settings.json     hook and env template
│   └── codex/hooks.json              official lifecycle-hook template
├── benchmark/                     CODE — benchmark suite + report
│
├── knowledge/                     KNOWLEDGE — content (gitignored: personal)
│   ├── daily/                       append-only session logs
│   │   ├── receipts/                v3 authoritative; v2 readable history
│   │   └── archive/YYYY-MM/bag-…/   immutable uncompressed BagIt packages
│   ├── notes/                       durable OKF pages (flat slugs)
│   ├── projects/<slug>/             state.md projection + append-only journal.md
│   │                                (only state.md/context.md join the corpus;
│   │                                no slug for a directory inside the vault,
│   │                                a platform temp entry, or $HOME — 2026-09-23)
│   ├── raw/                         immutable sources
│   └── inbox/                       unprocessed staging
│
├── cache/                        RUNTIME — gitignored (FTS5/vector/graph)
│   ├── evidence-graph/              immutable corpus-generation layout
│   │   ├── catalog.sqlite3            active-generation catalog
│   │   ├── telemetry.sqlite3          private cross-generation telemetry
│   │   └── generations/<generation-id>/ immutable after activation
│   │       ├── manifest.json
│   │       ├── source-manifest.json
│   │       ├── incremental-manifest.json optional reuse/invalidation record
│   │       ├── evidence.sqlite3
│   │       ├── search.sqlite3          FTS5 chunks + keys; holds the `ledger` table
│   │       ├── vectors.npy             optional
│   │       └── vectors.json            optional
│   ├── fact-keys/keys.sqlite3       nightly fact keys and ledger records, copied into the next build
│   ├── models/                      v4.0: ML model cache (reranker, embeddings)
│   ├── compile/                     validated content-addressed compile plans
│   ├── claims.sqlite3               derived claim candidate index
│   ├── code-tools/                  managed code-tool artifacts
│   │   ├── pyright/1.1.411/           reserved pinned Pyright installation root
│   │   ├── typescript-language-server/6.0.0/   with tsserver 5.9.3
│   │   ├── gopls/v0.23.0/             built from the pinned Go 1.27.1 toolchain
│   │   └── rust-analyzer/1.98.1/      with its pinned Rust toolchain
│   └── code-hints/                  #24 C1: per-checkout hook-time symbol table
│       └── <checkout-hash>.sqlite3    derived from that checkout's newest generation
├── logs/                         RUNTIME — gitignored (lint/compile/hook logs)
├── run/                          RUNTIME — gitignored operational state
│   ├── markdown-transactions.sqlite3 current DB; approved legacy tombstone target
│   ├── markdown-transactions-v3.sqlite3 approved active transaction/owner DB
│   ├── markdown-transactions-v2-retired.sqlite3 approved upgrade evidence
│   ├── transactions/<id>/           before/after images (an append: its added bytes only), plans, proposed abort receipt
│   ├── queue.sqlite3                 current DB; approved legacy tombstone target
│   ├── queue-v3.sqlite3              approved active queue + owner DB
│   ├── queue-v2-retired.sqlite3      approved upgrade evidence
│   ├── queue-results/                fenced results + approved decisions/dispositions
│   ├── queue-quarantine/             malformed legacy/current queue evidence
│   │   └── capture-<sha256>/         proposed resumable raw + intent + manifest
│   ├── capture-intents/              approved target: unprocessed capture intents
│   │   ├── pending/<00-ff>/<id>.json file-first/index reconciliation boundary
│   │   └── ready/<00-ff>/<id>.json   indexed intents awaiting terminal outcome
│   ├── reliability-v3-migration.json approved resumable cutover manifest
│   ├── reliability-v3-adopted.json   approved complete cutover evidence
│   ├── queue/                        JSON queue of v3.3.0–v3.4.0: refused, never imported
│   ├── queue-migrated-v2             marker of earlier releases, read by nothing
│   ├── state.json                    automation + compile receipts
│   ├── lsp/<owner-nonce>/             bounded LSP process scratch
│   │   ├── owner.json                 immutable create-only owner evidence
│   │   ├── failure.json               optional immutable terminal evidence
│   │   └── lease.json                 bounded mutable live lease
│   ├── install/                       approved install/recovery ownership state
│   │   ├── manifest.json               owned paths + exact installed release
│   │   ├── transaction.json            resumable install/upgrade/rollback state
│   │   ├── install.lock                process-lifetime advisory writer lock
│   │   ├── preimages/                  verified owned-fragment/value preimages
│   │   ├── displaced/                  readable copies of owned files an update replaced
│   │   └── scheduler/                  non-secret native scheduler definitions
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

Both roots are read from the environment on every call, in one place:
`scripts/llm_wiki/runtime_roots.py`. No module keeps a copy of a root or of a path
derived from one at import time, so setting the variables reaches every module at
once; an empty value names nothing and falls back to the default.
`tests/test_roots_are_read_at_call_time.py` fails on a module that reads a root
at import, directly or through one of its own functions. See
`docs/research/2026-11-10-roots-are-read-at-call-time.md`.
| `$MEMORY_LLM_PROVIDER` | Auto-detected (`opencode` → `codex` → `claude` → `openai` → `ollama`) | LLM backend for compile/flush/query. `fake` for tests. |
| `$LLM_WIKI_DLP_POLICY` | Unset | Optional absolute path to an external bounded-literal/fingerprint policy. Invalid or digest-mismatched required policy fails closed. |
| `$LLM_WIKI_<SECTION>_<KEY>` | Unset | One run's override of a registered limit in `scripts/settings.py` (for example `LLM_WIKI_CORPUS_MAX_FILES`); beats `llm-wiki.toml`. An invalid value stops the caller with its name. |

### SQLite locking probe (approved 2026-10-01)

The temporary two-connection SQLite locking probe belongs in the existing
`run/` directory of the configured runtime root, on the same filesystem.
It must not create temporary files at the vault root: doing so changes the
root identity while the corpus reader verifies its sealed ancestor chain.
Runtime initialization creates `run/` before probing. A read-only health check
with no `run/` reports the probe unavailable and creates no directory.
Unsafe linked/reparse paths or a different filesystem are refused. The probe
keeps actual SQLite locking verification and removes only its own files.
No persistent database, runtime directory, setting, daemon, or tool is added.

### Operator limits (`llm-wiki.toml`, 2026-09-27)

Limits that depend on the vault and the machine are declared once in
`scripts/settings.py`, each with its default, unit, lower bound and reason, and are
read only through `settings.setting_value`. An optional `llm-wiki.toml` at the vault
root overrides them; it is gitignored and absent by default, so a vault without it
runs on the declared registry defaults. Precedence is
default < file < `LLM_WIKI_<SECTION>_<KEY>`. An unknown section or key, a non-integer
or a value below its bound stops the caller naming the key and its source; nothing
falls back silently. `doctor` has a `settings` check: it lists every value that
differs from its default with its source, errors on an invalid file, and warns at 80 %
of a vault-size ceiling. The first registered limits are the vault-size ceilings
(`index`, `compile`, `corpus`, `claims`, `extraction`, `search`, `impact`); every
refusal at one names the setting that raises it. Decision and sources:
`docs/research/2026-09-27-every-limit-states-its-reason.md`; inventory:
`docs/LIMITS-2026-09-27.md`.

In the uninstalled compile-progress candidate (2026-10-20),
`compile.max_sources` and `compile.max_total_source_bytes` are optional aggregate
admission quotas, default 0 (disabled). Positive file/environment values remain
enforced and negative values are invalid. These quotas control admitted work
volume or disk/cost policy, not peak memory of the streaming capture. Doctor
reports configured overrides but does not compute a near-ceiling share for a
disabled quota. Zero does not disable separate per-file, archive, model-input,
output, downstream index or ownership checks; their law-9 qualification is
independent. Existing positive private-vault settings are not rewritten.

The candidate index renderer also reads each source body once, overlays pending
transaction after-images without changing the caller's base mapping, and retains
only extracted navigation entries for ordering. Original and resulting page sets
are both checked against any explicit `index.max_pages` /
`index.max_total_bytes` quota; both now default to 0 (disabled). Private sources
remain subject to input checks and are excluded before their text is decoded
into public index entries. Per-page/output limits remain in force. Standalone
`rebuild_plan` now captures a digest-only claim manifest and reads note bodies
through a manifest-bound mapping: a changed body is refused before rendering.
The existing claim-tree transaction precondition still checks membership and
content at publication. Manifest-only claim/guardrail readers release bodies;
explicit content-bearing readers retain their original exact-body contract.
This adds a second read/hash of index notes instead of retaining the whole tree.

The approved 2026-10-18 compile correction setting is `compile.validation_retries`
(`LLM_WIKI_COMPILE_VALIDATION_RETRIES` for one run), default 2, minimum 0, no
additional upper ceiling. It counts correcting passes after the initial
attempt for each provider; a malformed or incomplete review resumes in the review
role without redrafting, within that same budget. There is no separate hidden
missing-verdict retry. Zero disables correction retries. The default is an
explicit cost/quality compromise: allow a format correction and a semantic
correction while preventing unlimited model passes. It is not a measured optimum.
Review it using complete-cycle retained facts, failures, tokens and latency after
model/prompt or workload changes. Source, citation, completeness and publication
checks remain mandatory regardless of the chosen value. The owner approved the
new environment contract before implementation; the decision is retained privately
as `compile-validation-retry-setting-decision-2026-10-18`.

### Generation maintenance windows (decision approved 2026-10-08)

The existing settings registry exposes `generation.nightly_seconds` and
`generation.post_compile_seconds`, including its standard environment overrides
`LLM_WIKI_GENERATION_NIGHTLY_SECONDS` and
`LLM_WIKI_GENERATION_POST_COMPILE_SECONDS`. The bounded builder keeps complete
sources, vectors, validation and catalog CAS activation. Windows task and Linux
service definitions must outlast the configured pass; persisted definitions keep
their original values for rollback. Values are qualified from a complete measured
pass and reconsidered after corpus growth or machine changes. No runtime root,
directory, database, daemon or MCP tool is added. The defaults are 2131 seconds,
an estimate from the 2026-10-08 full-build attempt plus independently measured
validation and collection variation; the complete installed build remains
unqualified after a kernel OOM. Installation and full-cycle qualification are
separate checks, and the windows must be remeasured after resource changes.
Decision: `knowledge/notes/generation-measured-time-budget-decision-20261008.md`.

### Full nightly diagnosis window (approved 2026-10-17)

`doctor.nightly_seconds` and `LLM_WIKI_DOCTOR_NIGHTLY_SECONDS` use the existing
positive-integer settings registry. The default preserves the previous 60-second
compatibility profile; its sufficiency requires qualification for each vault.
Both full nightly diagnosis and the scheduler whole-pass bound read this value
for the selected vault. Reinstall managed scheduler definitions after changing it.
Incomplete checks remain deferred and read failures remain failures. Requalify
the budget after history growth, load, or resource changes; a larger timeout does
not fix inefficient work. No new dependency, database, or runtime path is added.
Decision: `knowledge/notes/nightly-full-health-budget-decision-20261017.md`.

## What lives where

### CODE zone (tracked in git)
- `scripts/` — Python pipeline and host helpers. Central hub:
  `memory_state.py` (path/lock/state), `compile_memory.py` (LLM compile +
  VERIFY-BEFORE-WRITE), `flush_memory.py` (3-tier classification),
  `maybe_compile.py` (PID-locked spawn), `search_memory.py` (entry point; fusion lives in `retrieval.py`),
  `llm_client.py` (5 backends + fake), `integration_adapter.py` (thin host
  lifecycle boundary), `mcp_server.py` (12 task-shaped tools; run as a program it is
  `mcp_supervisor.py`, which serves the tools from a child it restarts, with the client's
  initialisation replayed, when the code under `scripts/` changes), and `doctor.py`.
  An empty `__init__.py` makes it a regular package for `import scripts.x` and static
  analysis; its programs still run by path and nothing is packaged
  (`docs/research/2026-11-05-scripts-is-a-regular-package.md`).
- `tests/` — full regression suite. Hermetic via `conftest.py` (pins
  `LLM_WIKI_ROOT` to checkout, redirects `LLM_WIKI_STATE_ROOT` to a temp
  dir, defaults `MEMORY_LLM_PROVIDER=fake`).
- `docs/` — `ARCHITECTURE.md`, `USER-GUIDE.md`, `AGENTS.md` (knowledge
  subsystem brief — subordinate to the root `../AGENTS.md` contract),
  `EXPORTING.md`, `operating-model.md`,
  `STRUCTURE.md` (this file).
- `scripts/queries/` — 12 language-specific Tree-sitter queries for function,
  class/type, call, and import extraction. Grammar packages are optional and
  loaded lazily by `code_graph.py`; `NOTICE.md` records grammar provenance and
  MIT notices.
- `scripts/schemas/` — closed JSON Schemas for transaction, project checkpoint,
  queue task, compile plan/receipt, archive manifest, and claim records.
  On 2026-10-04 the owner approved a separate `claim-ledger-v2.json` for
  `claim-ledger/v2` and `claim/v2`. The compatible readers and native compiler
  are installed; controlled publication/consumer tests and one actual native
  compile-and-retrieve cycle in the supported base profile are qualified.
  Installed hybrid retrieval, whole-day compilation and full nightly
  qualification remain pending.
  Historical v1 schemas and records remain strict. V2 preserves the complete
  physical citation, its source range and hash; a transient native logical-line
  selector is verified against the canonical capture before that citation is
  bound. All source parts covering the container must participate together.
  Admission uses existing full-source and full-page byte budgets, without a new
  literal-length cap. Readers must support both versions before v2 publication;
  unknown versions refuse explicitly. This adds no runtime root, database,
  environment variable, daemon or MCP tool. Historical readers remain necessary
  while retained pages, archives, undo or live consumers require v1.
- `skills/` — 9 SKILL.md files (knowledge-compile, knowledge-lookup,
  knowledge-review, knowledge-qa-file-back, contradict-check,
  crystallize-playbook, bridge-promote-insight, session-memory-compile,
  session-memory-review).
- `rules/` — 4 rule files (wiki-files, raw-files, output-files, development-laws).
  `development-laws.md` preserves the user's nine mandatory development laws verbatim;
  both `AGENTS.md` and `CLAUDE.md` require reading and following them.
- `integrations/` — thin host wiring: claude-code (settings.json) and codex
  (hooks.json). MCP is the common read/action interface.
  Obsidian is an optional Markdown viewer and requires no bundled integration.
  The owner approved the following launch contract on 2026-10-07; implementation
  and platform qualification remain pending. Own Codex hooks and MCP commands
  may carry the existing nonsecret installed provider bundle through a fixed
  same-process Python bootstrap in the existing uv invocation. Presence of any
  existing provider key, including an empty value, preserves the entire explicit
  host environment; otherwise the complete installed bundle applies. Malformed
  payloads refuse before target execution. The original script, arguments,
  imports, stdin, output and exit behavior must be qualified. No new environment
  key, runtime file, storage schema, process or global Codex setting is added.
  Exact command/root ownership, foreign and disabled registrations, preimages,
  CAS, uninstall and rollback drift guards remain required. MCP registration
  remains outside installer transaction ownership. Doctor's expected bundle
  requires the complete verified manifest/transaction/root/resource/desired
  snapshot chain and stable read identities; an old manifest without a bundle
  cannot establish the expected model. Changed native hook commands require
  normal user trust confirmation; trusted hashes are never written automatically.
  See `knowledge/notes/codex-installed-provider-bootstrap-decision.md`.
- `benchmark/` — retrieval and frozen contradiction corpora/runners, including
  `run_benchmark.py`, `run_retrieval_v2.py`, `retrieval-v2.json`,
  `retrieval-v2.schema.json`, `legacy-60-v1.json`,
  `run_contradiction_benchmark.py`, and `contradiction-v1.json`.
  `run_benchmark.py` defaults to retrieval-v2. Only plain `--legacy-only`
  selects the old gate; conflicting legacy flags fail closed.
  The frozen retrieval-v2 baseline binds to the exact versions of the five
  packages the benchmark loads (`jieba`, `numpy`, `sentence-transformers`,
  `torch`, `transformers`), not to the byte digest of the whole `uv.lock`.
  The recorded `uv_lock_sha256` stays in the report as provenance and must
  remain a well-formed digest, but it is no longer compared to the current
  lock file. See
  `knowledge/notes/baseline-environment-binding-decision.md`.

### KNOWLEDGE zone (gitignored: the repository ships no memory)
- `knowledge/daily/` — append-only `YYYY-MM-DD.md`. Private (gitignored);
  no daily log is published. The day and every `## [HH:MM:SS]` heading are the
  machine's local wall clock, read through `iso_time.local_now()` alone; a claim's
  `observed_at` is that reading converted to UTC (2026-09-27,
  `docs/research/2026-09-27-the-daily-log-keeps-one-clock.md`).
- `knowledge/daily/receipts/` — authoritative immutable Markdown compile receipts.
  Installed v3 binds logical path plus source-part digest; v2 is historical only.
  The approved v4 target additionally binds original source context as described
  above. V4 activation is not implied by recording the decision.
- `knowledge/notes/` — durable OKF pages, flat `<slug>.md`. All gitignored:
  the repository ships no memory (2026-09-10). The decision pages named in
  this document are the owner's private record; the contracts are stated here.
- `knowledge/projects/<slug>/` — generated `state.md`, append-only
  `knowledge/projects/<slug>/journal.md`,
  `context.md`, `.blackboard/`. Template tracked; real projects gitignored.
  `context.md` is written on request by
  `uv run python scripts/build_context.py <name> --write`; see
  `docs/research/2026-09-18-the-project-context-page-gets-its-command-back.md`.
- `knowledge/daily/archive/YYYY-MM/bag-<timestamp>-<id>/` — private immutable,
  uncompressed BagIt-style daily-log bags and
  a derived archive index. Archive means move, never delete; evidence resolves by
  logical ID, source hash, and byte span. A day compiled whole carries
  `archive-manifest/v1` and one embedded `compile-receipt.md`; a day compiled in
  parts carries `archive-manifest/v2`, whose `compile_parts` list each part's byte
  span, receipt reference and compile authority, with one embedded
  `compile-receipt-<n>.md` per part, all in the tag manifest. A page quoting one
  part resolves from the bag by that part's digest. See
  `docs/research/2026-09-26-a-split-day-is-archived-with-every-part.md`.
  Candidate streaming reader: `validate_bag` verifies the complete payload and
  returns metadata after closing its descriptor. `open_validated_bag` exposes an
  owned verified range only inside its context; results are provisional until
  final source verification succeeds on exit. Archive citation resolution scans
  bags individually, retaining selected evidence rather than a month's payloads.
  Terminal capture proofs keep the range open through incremental append-hash
  checks. Historical part discovery hashes candidates incrementally, then returns
  the matching part's bytes. The candidate archive writer also checks eligibility
  through owned ranges, consumes compile parts lazily, and copies payloads and
  quarantine files in chunks. Its source scope must exit successfully inside the
  hidden-build cleanup boundary before publication. Existing fsync, source fences,
  post-publication validation and transactional source deletion remain.
  Transaction undo-image allocation, historical selected-part allocation and
  remaining byte/member ceilings still need review; this does not establish full
  streaming of every archive operation or completion of the installed migration.
- `knowledge/raw/` — immutable sources. Gitignored (personal). One subtree is
  writable by the runtime: `knowledge/raw/sessions/<date>/<session>.md`, the
  session records of the 2026-08-23 retention decision. It is the only part of
  `raw/` inside the Markdown transaction's allowed roots. The weekly queue purge
  writes a second one outside the transaction: `knowledge/raw/queue-archive/<date>/`,
  owner-only, the export `memory_queue purge` makes of finished queue work (intents
  and decisions included) before it deletes that work from `run/` after
  `queue_result_retention_days` (2026-09-24).
- `knowledge/inbox/` — unprocessed staging. Gitignored.
- `knowledge/log-archive/` — rotated copies of the private vault log, written by
  the compile that passes 2 MiB (2026-09-25). Gitignored.
- `knowledge/feedback/` — retired 2026-09-25 (correction candidates needed a
  manual promote; compile learns corrections from the daily log). Old files stay
  gitignored and are read by nothing but the guardrail source manifest.

### RUNTIME zone (always gitignored, inside vault)
- Complete corpus generation is implemented by `generation_catalog.py`,
  `corpus_snapshot.py`, `evidence_graph_builder.py`, `evidence_graph.py`,
  `search_memory.py`, `code_extractor.py`, and `repository_scope.py`. The bounded
  rollback-journal catalog provides repository-scoped active selection, CAS
  activation, validated fallback, orphan recovery, and deadlines. Corpus snapshots
  bind immutable captured bytes to source hashes; Evidence Graph v2 and FTS are
  required artifacts built from the exact same snapshot. Incremental reuse never
  changes the requirement to publish a complete generation. POSIX collection is
  descriptor-authoritative; Windows reparse and identity checks are best effort.
  This adds no daemon or automatic legacy-cache removal.
- `cache/` — `evidence-graph/` (the generations: FTS5, vectors, graph). The
  `code_tools.json` tool manifest was retired on 2026-09-25: read by nothing, it
  ran the analysed repository's own `tsc`; an old copy is disposable cache.
  `cache/code-tools/<profile>/<version>/` are the managed language-server artifact
  roots (`pyright/1.1.411`, `typescript-language-server/6.0.0`, `gopls/v0.23.0`,
  `rust-analyzer/1.98.1`); `scripts/install_pyright.py` and
  `scripts/install_language_server.py` are the only supported download/publish paths
  and `scripts/lsp_paths.py` derives them without directory creation.
  v4.0: `models/` (ML model cache),
  `cache/compile/` (validated compile-plan action cache), and `cache/claims.sqlite3`
  (derived claim index).
- `cache/code-hints/<checkout-hash>.sqlite3` — issue #24 C1: the classes,
  functions and methods of one foreign checkout's newest generation, with
  qualified name, first location and resolved in/out degree, exported once by
  each index build and read by the `Grep`/`Glob`/`SubagentStart` hook adapter
  (`scripts/graph_hint.py`) in one indexed query. Disposable and derived: a
  missing or foreign file answers nothing, `refresh` re-exports it, and
  `repository_index.py retire` removes the file of a checkout that has no
  generation left. It is not a generation member and not a second catalog.
  Research: `docs/research/2026-09-11-the-graph-meets-the-agent-where-it-searches.md`.
- `cache/evidence-graph/` — disposable derived graph, FTS, vector, tier, and
  telemetry generation state, built over `knowledge/` only (the checkout's own
  code and docs are not memory; repositories have their own generations).
  The implemented v2 layout is:

```text
cache/evidence-graph/catalog.sqlite3
cache/evidence-graph/telemetry.sqlite3
cache/evidence-graph/generations/<generation-id>/
├── manifest.json
├── source-manifest.json
├── incremental-manifest.json    optional; present for incremental builds
├── evidence.sqlite3
├── search.sqlite3                the FTS chunks with their keys, and the `ledger` table
├── vectors.npy
└── vectors.json
```

  `catalog.sqlite3` contains generation metadata and selects one active generation.
  Repository-scoped readers validate its `repository_scope` before using that
  generation or any prior-generation fallback.
  `telemetry.sqlite3` is private, disposable cross-generation retrieval telemetry.
  It is not authoritative and contains query hashes rather than raw query or response
  content. Ingestion enforces a transactional row ceiling. Explicit bounded promotion
  records a per-page sequence watermark in the same recoverable Markdown mutation as
  access counters, making retries idempotent. Telemetry sits beside the catalog and
  generations, never under `run/`.
  A v2 generation is immutable after activation and always contains the source
  manifest, Evidence Graph, and FTS snapshot. The incremental manifest is optional;
  it describes reuse but never permits partial publication. The vector pair is
  optional and must
  be absent, complete, or explicitly stale; partial vectors are never silently
  used. `cache/evidence-graph/` can be deleted and regenerated from authoritative
  Markdown, Git, and project journals. No generation database belongs under `run/`;
  `run/` remains operational state only.
- Activation is validate-register-then-CAS: canonical manifests, exact
  `repository_scope`, source membership, artifact hashes, SQLite integrity, graph
  evidence spans, FTS contents, and the final directory seal validate before a short
  catalog transaction changes the active pointer. The live corpus is recaptured
  before publication. A failed, deferred, or partial build cannot replace the prior
  active generation. Recovery may register complete orphan generations without
  activating them. A corrupt active generation is replaced only by a revalidated
  same-scope prior generation from activation history/parent lineage.
- The legacy FTS5 index (`cache/index.sqlite`, `cache/.paths-manifest`) and the legacy
  vector cache (`cache/vectors.npy`, `cache/vectors_meta.json`) were retired on 2026-09-23:
  the generation is the only index. A search with no active generation reads Markdown
  directly, bounded by its deadline, and every such hit says `no_active_generation`; the
  installer's sync builds the first generation (`doctor --rebuild-generation` on
  demand), and the nightly refreshes it. Those files are read by nothing and may be deleted; nothing deletes them
  automatically. LanceDB was retired on 2026-09-07: its table was never built on
  the installed vault, its path was reachable only from the deadline-less legacy
  search, and its index was keyed to a different embedder than the product's — see
  `knowledge/notes/retire-lancedb-decision.md`.

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

### Central model selection (owner requested and installed 2026-10-19)

The existing private `llm-wiki.toml` holds `[llm] model = "provider/model-id"`
and its reasoning setting. `choose_model.py` is the one selection command;
`llm_client` reads this choice for CLI, MCP, hooks, queue and scheduled work.
Saved selection takes precedence over historical model/provider environment
projections and the host CLI's global model; endpoint credentials and DLP/local-only
requirements keep their existing boundaries. Explicit fake remains a test mode.
A resolved request keeps its identity across preparation/dispatch; an update does
not relabel in-flight calls. No new database, service or runtime root is added.

Provider catalogs are candidates, not proof of subscription entitlement. Selection
requires a successful short protected call before saving; failure leaves the old
selection intact. Installed provider model projections are removed after qualified adoption;
unconfigured installations retain their existing environment-based compatibility.
The owner request and scope are recorded privately in
`knowledge/notes/central-memory-model-selection-decision-20261019.md`.

The durable compile controller, its streaming artifacts, batches, repair trees and publication
checkpoints have their own contract: [durable compile contract](modules/durable-compile-contract.md).

## Where the rest of this reference lives

- [Compile and nightly contracts](contracts/compile-and-nightly.md)
- [Integration and recovery contracts](contracts/integration-and-recovery.md)
- [Reliability and capture contracts](contracts/reliability-and-capture.md)
- [Corpus and code navigation contracts](contracts/corpus-and-navigation.md)
- [Module map](MODULES.md): the owner guide of every module packet and the boundaries between modules
