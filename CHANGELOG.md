# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Documented the integrated Tasks 1-29 unified evidence contracts: Markdown/Git/
  project-journal authority, immutable derived generations, register-then-CAS
  activation, seal validation, prior-generation recovery, cache deletion, truthful
  retrieval fallback, token-source labels, and verified grounded-answer citations.
- Added a non-destructive legacy FTS/NumPy/Lance migration and rollback guide. Legacy
  caches remain readable and must be retained until installed-vault migration evidence
  proves removal safe; rollback never removes knowledge, Git history, journals, or
  operational `run/` state.
- Recorded the exact unchanged 12-tool MCP surface and current behavior. `recall`
  exposes retrieval trace fields; evidence reads fail closed; contradiction checks are
  structured; code/dead-code/architecture/impact paths are store-first with explicit
  live fallback; `doctor` retains nine closed operator actions. Planned token-budgeted
  MCP context, expanded architecture modes, and per-component envelope freshness are
  labelled **evidence pending** rather than shipped.
- Recorded truthful release-evidence limits: the model matrix currently selects no
  new embedding or reranker, deterministic comparative smoke does not execute
  Graphify, and no model-superiority, Graphify-parity, quality, or token-ratio claim is
  made. Real paired evidence remains pending.
- Documented Stage 2 reliable memory operations: Markdown-authoritative recoverable
  transactions and undo, fenced project journals, rollback-journal SQLite queue
  migration/work/redrive/export-first purge, content-addressed compile receipts,
  90-day-hot immutable BagIt archives, logical evidence, and quarantined claims.
- Added operator commands and deletion safety for source failures, retained
  transactions/tasks/results, the 30-day undo window, and live owners.
- Recorded the local-filesystem requirement, current `synchronous=FULL`/no-WAL
  policy, bounded defaults and CLI overrides, cooperating-writer CAS boundary, and
  explicit non-goals. The suite now collects **4440 tests**.
- Added canonical `repository-scope/v1` binding for repositories, linked worktrees,
  checkout roots, Git common directories, and captured commits. Generation readers
  reject the wrong repository/worktree scope instead of returning cross-checkout
  evidence.
- Implemented complete `corpus-generation/v2` publication. Canonical
  `source-manifest.json` binds exact source membership and hashes;
  `incremental-manifest.json` records source ownership, invalidation, and reuse.
  Evidence Graph v2 and FTS are required artifacts built from the same immutable
  snapshot, and no partial, stale, raced, or changed candidate can become active.
  Catalog selection, CAS activation, same-scope fallback, orphan recovery, and
  bounded nightly/doctor maintenance preserve the previous valid generation.
- Added deterministic workspace-level code extraction and incremental invalidation
  across supported source languages, including cross-file symbols, occurrences,
  calls, imports, routes, and dependencies where the extractor can prove them.
- Extended the existing MCP `recall` tool with opt-in grounded QA and verified
  citations without changing the 12-tool surface. MCP context, grounded QA,
  SessionStart, project handoff, and lifecycle injection now route final packing
  through the shared Context Compiler and one bounded token budget.
- Hardened deadlines, cancellation, bounded reads, repository resolution, generation
  publication, retrieval errors, and cleanup paths. Installers now preserve the real
  pytest exit status and clean up spawned test processes/process trees on interruption.
  Legacy FTS, NumPy, and Lance caches remain readable fallback state; there is no
  daemon, automatic migration, or automatic legacy-cache removal.
- Centralized LSP lifecycle transitions in one coordinator with serialized restart,
  heartbeat, and cleanup ownership. Terminal identity and deadlines are immutable,
  lease refresh remains independent during cleanup, and Windows process and file handles
  remain owned until exact PID exit and successful handle closure are proven.

## [4.0.0] — Unreleased

### v4.0: Unified Knowledge Intelligence

- **LanceDB embedded vector backend** (`scripts/lance_store.py`) — HNSW vector
  search, embedded (no daemon). Replaces PostgreSQL. `--extra hybrid` to enable.
- **Memory-mapped vector cache** — `vectors.npy` replaces `vectors.json`. Binary
  numpy format loads instantly via mmap instead of JSON parsing.
- **bge-small-en-v1.5 embedding** — pinned legacy optional-vector compatibility model.
- **Cross-encoder reranker** (`scripts/reranker.py`) — bge-reranker ONNX INT8,
  re-ranks top-20 results. `--extra reranker`.
- **Access tracking + Ebbinghaus forgetting** (`scripts/access_tracking.py`) —
  retrieval analytics, decay scoring, hybrid archive (time + access).
- **Multi-pass compile** — draft → critique pass, drops low-quality operations.
- **Typed edges** — `refines` (page stays alive) alongside `superseded_by`.
- **A-MEM reflection** (`scripts/reflection.py`) — weekly page consolidation.
- **MCP-first agent interface** (`scripts/mcp_server.py`) — 12 task-shaped tools
  including `doctor`, uniform response envelopes, and health/context resources.
- **Thin lifecycle boundary** (`scripts/integration_adapter.py`) — native hooks,
  plugins, and wrappers normalize host events that MCP cannot observe.
- **Automatic health** — SessionStart injects only degraded/error findings;
  healthy checks remain quiet and repairs require explicit opt-in.
- **Fenced recovery hardening** — fail-closed runtime deletion checks, bounded
  no-follow SessionStart recovery, background maintenance heartbeats, shared
  deadlines, safe operational SQLite opens, and protocol-visible MCP failures.
- **Local retrieval architecture** — SQLite FTS5 BM25 with optional local
  vectors/LanceDB, graph neighbors, and reranking. Removed active QMD wiring.
- **Obsidian viewer-only integration** — removed the bundled ingestion template;
  Obsidian remains an optional Markdown viewer, not a required frontend.
- **Code graph** (`scripts/code_graph.py`) — lazy tree-sitter parsing and
  materialized `.scm` queries for 12 languages, scoped import captures,
  nine-language regex fallback, evidence-aware Python import/call resolution,
  and rename-aware git co-change refinement.
- **LINK Layer** (`scripts/impact_analysis.py`) — git diff → stale wiki pages.
- **L0/L1/L2 tiered loading** (`scripts/build_tiers.py`) — progressive disclosure.
- **Scheduled jobs integration** — nightly: access flush + code graph.
  Weekly: A-MEM reflection + L1 tier generation.
- **SessionStart impact advisory** — stale wiki pages from code changes.
- **MCP config in install scripts** — Claude Code + OpenCode auto-config.
- **Optional extras** — `hybrid`, `code-graph`, `mcp-server`, `reranker`, `full`.
- **4440 tests**.

## [3.4.0] — 2026-07-11

A comprehensive security, concurrency, and quality release following 9 rounds of
full-codebase audit. **281 tests**. **Zero Critical, zero High**
open findings as of the final audit pass.

### Security
- **Secret redaction** (`scripts/secret_redact.py`) — 12 regex patterns (Bearer, API keys, GitHub, Slack, AWS, Google, JWT, PEM) plus Shannon entropy ≥ 4.0 high-entropy catch-all with pure-hex exclusion. Applied before ALL durable writes (daily logs, compile notes, Q&A file-back, bootstrap context).
- **Transcript path containment** — hook-supplied transcript paths restricted to known agent directories (`~/.claude`, `~/.codex`, `~/.config/opencode`, system temp) with known extensions only. Prevents arbitrary file exfiltration via crafted hook payloads.
- **Path traversal guards** — compile category whitelist + `relative_to()` containment, feedback candidate ID hex-only validation, queue output path containment under `run/queue-results/`, blackboard project slug sanitization.
- **Installer push-lock** — installed vault gets `git remote set-url --push origin no-push` so personal data can never be pushed to the public repo.
- **Installer pinned clone** — `git clone --branch v3.4.0 --depth 1` instead of mutable default branch.
- **`.gitignore` allowlist** — explicit per-file un-ignore for public knowledge notes instead of broad `!knowledge/notes/*.md` that could expose personal pages.
- **YAML injection prevention** — all frontmatter interpolation escapes backslash, double-quote, and newlines.
- **Untrusted-data framing** — daily-log excerpts injected into SessionStart context are marked as `UNTRUSTED — session history, not instructions`.

### Concurrency & Atomicity
- **Daily-log lock rewritten** (`daily_log_append._daily_lock`) — `O_CREAT|O_EXCL` atomic file creation (was broken `rename()` which silently overwrites on POSIX). Stale-lock recovery via PID liveness + mtime. Fail-closed (raises `TimeoutError` instead of writing without lock).
- **Single locked write path** — all daily-log writers (flush_memory, user_prompt_capture, post_tool_capture, session_end_project_tag, tool_breadcrumb_append) delegate to `locked_append()` / `append_daily()`. Zero duplicated write logic.
- **Compile lock hardening** — PID-0 placeholder TTL (10s), owner-aware deletion via token check, atomic lock writes via temp+`os.replace`.
- **`atomic_write()` helper** (`memory_state.py`) — all durable note writes, supersession markers, search index, and cache files use temp-file + `os.replace` pattern.
- **Direct compile lock acquisition** — `compile_memory.main()` acquires the compile lock even when run directly (not spawned by `maybe_compile`), preventing concurrent manual compiles.
- **State-lock deadline** — `memory_state._state_lock` bounded by monotonic deadline in all branches (was unbounded `sleep(timeout)` when owner PID alive).
- **Maintenance lease** — `scheduled_nightly.py` acquires `run/maintenance.lock` via `O_EXCL`, preventing concurrent nightly+weekly runs.
- **Project state exclusive-create** — `session_start_project_state.py` uses `O_CREAT|O_EXCL` for initial `state.md` creation instead of write+replace.

### Architecture
- **Flat notes layout** — `compile_memory.py` writes directly to `knowledge/notes/<slug>.md` (was `knowledge/notes/<category>/<slug>.md`). Type lives in frontmatter only. Aligns with Obsidian/Dataview 2026 property-based organization.
- **`okf_types.py`** — single source of truth for canonical OKF types, type aliases (`comparison→synthesis`, `connection→synthesis`, `fact→concept`), and never-archive set. Imported by lint_memory, migrate_to_okf, archive_stale, rebuild_memory_index.
- **`maintenance_helpers.py`** — shared `run_step()` and `wait_for_compile_idle()` extracted from scheduled_nightly + scheduled_weekly (was byte-identical copies).
- **Deferred flush queue** — when no LLM backend is available, `flush_memory` enqueues a typed `"flush"` task. The drain processor classifies the result and applies it to the daily log, restoring the deferred-work contract.
- **Queue stale-lease recovery** — `memory_queue.recover_stale_leases()` re-queues `.processing` files older than 10 minutes.

### Search & Retrieval
- **Superseded/archived exclusion** — `_collect_pages()` in search_memory, `_build_link_graph()` in graph_neighbors, `existing_knowledge_snapshot()` in compile_memory, and `rebuild_memory_index.py` all skip pages with `status: superseded` or `status: archived`.
- **Atomic FTS index rebuild** — search index built in `index.sqlite.tmp`, then atomically replaced via `os.replace`.
- **Path manifest** — `.paths-manifest` sidecar detects deleted pages and triggers search rebuild.
- **JSON vector cache** — `vectors.json` (was `pickle`, now safe `json.loads`).

### Lint (14 checks)
- **14th check: `invalid_type_value`** — flags pages whose `type:` is not in `CANONICAL_TYPES` after alias normalization.
- **`TYPE_ALIASES` applied** — `lint_memory.check_invalid_type_value` normalizes alias types before validation.
- **`orphan_gaps` frontmatter scan** — scans by `type: gap` frontmatter instead of looking for a `gaps/` subdirectory.
- **Skills/rules scope** — `--scope all` now includes `skills/` and `rules/` for OKF frontmatter conformance.
- **Temporal validity** — non-date `valid_to` values (e.g. "forever") are skipped instead of causing false positives.

### Testing (281 tests, up from 226)
- **`test_security_invariants.py`** (47 tests) — property-based tests covering path traversal, YAML injection, secret redaction, status filtering, daily-lock exclusivity (5 concurrent threads), compile evidence enforcement, and legacy path detection.
- **`test_quality_guards.py`** expanded — docs-equality tests for lint count, runtime dir names, installer version tags, daily-writer lock usage, clean-clone import resolution, and untracked module detection.
- **Behavioral tests** — concurrent writers (no interleaving), compile evidence (empty→drop, valid→pass), snapshot exclusion, lock fail-closed behavior.

### Documentation
- **Full i18n sync** — README.md, README.ru.md, README.zh-CN.md synchronized: version, test count, lint count (14), benchmark methodology (exact title + keywords, not paraphrased), runtime dirs (`cache/cognee/`), installer tags.
- **`docs/STRUCTURE.md`** — canonical structure reference with env contracts, runtime zone, and forbidden directories.
- **Skills updated** — all 9 skills use flat `knowledge/notes/<slug>.md` paths.
- **`operating-model.md`** — flat layout paths, multi-agent intro.
- **Knowledge notes** — legacy `memory/` references updated to `knowledge/daily/` + `knowledge/notes/`. Trust fields (`confidence`, `source_authority`) added to workflow pages. Taxonomy aligned with canonical types.

### Installer
- **`uv sync --locked`** — verifies lockfile is up-to-date, fails if stale.
- **Env-var overwrite warnings** — both installers warn before clobbering existing `LLM_WIKI_ROOT` / `LLM_WIKI_STATE_ROOT`.
- **Bounded cron cleanup** — install.sh removes only lines between `# LLM-Wiki-cron-start` / `-end` markers (was broad `grep -v` that could delete unrelated jobs).
- **Windows `cache\cognee`** — install.ps1 now creates `cache\cognee` instead of bare `cognee\`.
- **`install-scheduled-tasks.ps1`** — removed `_SafeExit` function (was broken when dot-sourced); inline `return`/`exit` at call sites.

### CI
- **Gitleaks** — SHA-pinned GitHub Action, enforced by regression test.
- **Cross-platform matrix** — Ubuntu + Windows + macOS, Python 3.10 + 3.13.
- **`uv sync --locked --dev`** — lockfile enforced in CI.

## [3.3.3] — 2026-07-10

### Fixed
- **GitHub Actions Gitleaks** — upgraded to the Node 24 `v3.0.0` action pinned by immutable commit SHA. The previous action attempted to download the removed Gitleaks 8.24.3 Windows archive and failed before tests ran.

### Tests
- **281 tests** — added a regression guard that prevents CI from reverting to the unavailable Gitleaks action.

### Docs
- **Benchmark numbers refreshed** — `benchmark/report.md` now reports MRR 0.9667, p50 6ms (BM25-only mode, 60 queries). `docs/ARCHITECTURE.md` search layer updated to match; previously cited stale MRR 0.942 / p50 41ms figures.

## [3.3.2] — 2026-07-09

### Fixed
- **Three-zone layout hardening** — removed machine-local `D:\projects\` / `D:\tools-agent\` paths from public `AGENTS.md` + `CLAUDE.md` (they leaked the author's disk layout into a public repo)
- **maybe_compile PID race** — placeholder PID-0 lock is now treated as "alive", preventing a concurrent-spawn race during the detached-spawn window
- **agent_timeline breadcrumb regex** — now matches the real writer format (`tool | sid | slug | tool\` target`); tool-event attribution was silently dead
- **bootstrap_project secret redaction** — git remote URL is now passed through `secret_redact` before being written to `knowledge/projects/<slug>/bootstrap.md`
- **archive_stale path doubling** — archived pages no longer land at a doubled `knowledge/notes/` prefix
- **blackboard complete_task race** — switched from non-atomic read-modify-rewrite of `tasks.jsonl` to an append-only `completed.jsonl` (prevents silent completion loss when two agents finish different tasks in the same window)
- **compile_memory singularize** — replaced `rstrip('s')` (mangled entities→entitie, syntheses→synthese) with an explicit `CATEGORY_SINGULAR` map
- **loop_detector / agent_timeline unicode** — topic-signature regex now matches non-ASCII letters (was ASCII-only `[a-z]{5,}`)
- **cognee_sync SKIP_SUBTREES** — pointed `projects/` skip at `knowledge/projects` (was `knowledge/notes/projects`, a no-op)
- **export_vault forbidden paths** — verify list now blocks the three-zone forbidden dirs at vault root (`cache/`, `logs/`, `run/`, `state/`, `wiki/`, `memory/`, `outputs/`, `.ci-lint-state/`)
- **codex-memory-wrapper.ps1** — removed legacy `memory-state/` fallback, quoted the daily-log path, renamed shadowed `$args` automatic variable

### Changed
- **flush_memory → maybe_compile** — `maybe_trigger_compile` now delegates to `maybe_compile.spawn_compile_if_idle` (PID lock is the single concurrency gate; hooks/wrappers/schedulers no longer spawn `compile_memory.py` directly)
- **search_memory `--as-of` + source_authority** — temporal validity windows and typed-provenance weights (`user` > `web` > `ai-derived` > `inferred`) applied to ranking; `_vector_search` takes `as_of` as an explicit parameter (was a misleading "thread-local-ish" global)
- **feedback_capture stdin contract** — OpenCode plugin's `feedback_capture.py` JSON-on-stdin path now actually parses and captures (was list/promote only)
- **Claude Code hooks** — `UserPromptSubmit` + `PostToolUse` wired into `integrations/claude-code/settings.json`
- **install.ps1** — copies the OpenCode plugin (was mkdir-only); detects Antigravity; writes Codex wrapper via `$env:LLM_WIKI_ROOT` (survives vault relocation)
- **migrate_to_okf** — now imports `ROOT` from `memory_state` (honors `LLM_WIKI_ROOT` + worktree-aware git resolution)

### Removed
- `.ci-lint-state/` leaked runtime artifact at vault root; added explicit `.gitignore` defense
- Dead `WIKI_DIR` aliases (kept as backward-compat shims for tests), stale `import re`/`hashlib`, legacy `--scope memory|wiki` lint scopes collapsed to a single `knowledge/notes` tree

### Tests
- **217 tests** (+1 path-migration guard that scans scripts/ for forbidden legacy tokens; +deepened settings.json hooks test that verifies referenced scripts exist + timeouts; +27 structure invariants)
- `tests/README.md` refreshed: full coverage table, hermetic-isolation note, `LLM_WIKI_TEST_USE_EXTERNAL_STATE` opt-in documented

### Docs
- `AGENTS.md` / `CLAUDE.md` / `ARCHITECTURE.md` / `SETUP-COGNEE.md` / `EXPORTING.md` / `integrations/README.md` synced to the three-zone layout, current benchmark numbers (MRR 0.942, ~41ms p50), 13 lint checks, and portable path conventions
- Knowledge notes with live `raw/` / `inbox/` / `memory/` instructions repointed to `knowledge/raw/` / `knowledge/inbox/` / `knowledge/` (historical editorial mentions left verbatim per append-only contract)
- Broken Evidence citation `[22:41:34]` in `prospective-memory-page-drift.md` repointed to the real `[23:13:01]` fixture block

---

## [3.3.1] — 2026-07-09

### Added
- Claude Code user-settings merge (`scripts/merge_claude_settings.py`) — safe install-time hook wiring with backup
- Secret redaction for daily capture (`scripts/secret_redact.py`)
- Public Evidence daily fixtures + curated sample notes under `knowledge/`
- Regression suite for path-safety, fake-LLM compile e2e, Claude merge

### Changed
- **Three-zone layout** complete: CODE / `knowledge/*` / runtime only under `$LLM_WIKI_STATE_ROOT/{run,logs,cache}`
- Installers (`install.ps1` / `install.sh`): correct `Ekgardt/llm-wiki` URLs, `run|logs|cache` dirs, OpenCode force-copy, Codex paths
- Compile: category whitelist + path containment, dry-run no side effects, AGENTS from `docs/AGENTS.md`
- Queue drain works without `output_path`; atomic `maybe_compile` lock
- Capture hooks use `update_state` + redaction; projects path under `knowledge/projects`
- Docs, skills, Cursor rules aligned to `knowledge/` (no live root `memory/` / `wiki/`)
- Search: FTS quote escape, vector `as_of`, JSON vector cache (no pickle)
- Archive under `knowledge/notes/archive/`; export forbids `.obsidian/`
- Benchmark scans flat notes (reproducible on public tree)

### Fixed
- Path traversal via LLM `category`; Codex wrapper `exit` killing shell; flush `--event` mapping
- OpenCode timestamp format (`[HH:MM:SS]`); broken QA dir; lint double-scan / wrong index path
- Doc falsehoods (test counts, install URLs); tracked wikilinks (0 missing)

### Security
- Capture redaction for common secret patterns
- Compile/feedback/blackboard path containment
- Gitleaks in CI; `uv sync --locked`

### Tests
- **178** pytest tests (hermetic state under `.pytest_cache/`)

## [3.3.0] — 2026-07-04

### Added
- **Vector warm-start** — plugin preloads sentence-transformers model + builds vector cache at session start
- **Russian / Chinese READMEs** + language selector
- **GitHub badges** (CI, license, tests, benchmark)

### Changed
- Cross-platform install (`install.sh` / `install.ps1`)
- Portable OpenCode plugin via `$LLM_WIKI_ROOT`
- Benchmark methodology disclosure

### Tests
- 155 tests at this release tag

---

## [3.2.0] — 2026-07-03 — "Proactive Intelligence + Multi-Agent Coordination"

### Added
- **Cursor integration** — `.cursor/rules/llm-wiki.mdc` rules file for vault access
- **Antigravity integration** — `AGENTS.md` snippet for vault access
- **IDE integrations guide** (`integrations/README.md`) — how IDE agents differ from CLI agents
- **Guard rails** (`scripts/build_guardrails.py`) — auto-injects learned corrections at SessionStart, preventing agents from repeating past mistakes
- **Feedback capture** (`scripts/feedback_capture.py`) — detects user corrections/preferences in session transcripts, saves as candidates for promotion to knowledge pages
- **Agent timeline** (`scripts/agent_timeline.py`) — attribution: shows which agent made which decision and when
- **Blackboard coordination** (`scripts/blackboard.py`) — parallel agents claim tasks, signal completion, detect conflicts (O(n) instead of O(n²) coordination)
- **Loop detector** (`scripts/loop_detector.py`) — prevents infinite "fix → review → redo" cycles across agents
- **Bootstrap from git** (`scripts/bootstrap_project.py`) — auto-generates project context from README, git log, tech stack, docs
- **Per-project context builder** (`scripts/build_context.py`) with auto-detect agent strengths
- **Graph-neighbor search** (`scripts/graph_neighbors.py`) — 3rd retrieval signal via wikilink graph RRF
- **Weighted RRF fusion** — BM25=2.0, Vector=1.0, Graph=0.5 (prevents search regression)
- **Title + filename boost** — exact match → 10x score, prevents duplicate-page confusion
- **Real-time contradiction check** — pre-write supersede detection in compile pipeline
- **Smart auto-archive** (`scripts/archive_stale.py`) — type-aware thresholds (decisions never archive, debugging at 60 days)
- **Temporal validity lint** (check #13) — `valid_from`/`valid_to` frontmatter validation
- **Benchmark suite** (`benchmark/run_benchmark.py`) — Recall@K, MRR, latency measurement

### Benchmark Results
| Metric | Value |
|---|---|
| Recall@2 | **100%** |
| Recall@5 | **100%** |
| Recall@10 | **100%** |
| MRR | **0.952** |
| Latency p50 | **28ms** |
| Token cost | **0** |

### Changed
- Search pipeline: BM25-only → BM25+Vector → BM25+Vector+Graph triple fusion
- Compile pipeline: added feedback capture integration at FLUSH classification
- SessionStart: added guard rails + advisory blocks before metacognitive context
- Nightly task: added FTS5 index rebuild + graph cache rebuild
- Weekly task: added auto-archive with type-aware thresholds
- FTS5 query: per-word quoting (prevents column-name interpretation, preserves AND semantics)

### Security
- All personal data scrubbed from git history (git-filter-repo)
- 0 dead imports, 0 TODO/FIXME, 0 absolute paths in tracked files
- Gitleaks: no leaks found

---

## [2.1] — 2026-07-03 — "Multi-tool, zero ops"

### Added
- Universal LLM client (5 backends: OpenCode/Codex/Claude/OpenAI/Ollama)
- Persistent deferred-task queue
- Concurrency-safe compile pipeline with PID lock
- 3-tier FLUSH classifier (MAJOR/MINOR/OK)
- OKF v0.1 frontmatter migration (100% conformant)
- Metacognitive SessionStart context
- Crystallize-playbook skill
- Windows Task Scheduler (nightly + weekly)
- OpenCode plugin + Codex PowerShell wrapper + Claude Code hooks

---

## [1.0] — 2026-04 — "Karpathy-style vault with session memory"

### Added
- 3-layer architecture: raw/ (immutable) / knowledge/notes/ (compiled) / memory/ (session lore)
- 7-check structural lint with LLM contradiction detection
- Multi-project slug system with 5-step collision resolution
- QMD hybrid search (BM25 + vector + reranker)
- Promotion pipeline from project memory to cross-cutting wiki
