# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [3.3.3] — 2026-07-10

### Fixed
- **GitHub Actions Gitleaks** — upgraded to the Node 24 `v3.0.0` action pinned by immutable commit SHA. The previous action attempted to download the removed Gitleaks 8.24.3 Windows archive and failed before tests ran.

### Tests
- **218 tests** — added a regression guard that prevents CI from reverting to the unavailable Gitleaks action.

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
