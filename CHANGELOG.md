# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

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
- **171** pytest tests (hermetic state under `.pytest_cache/`)

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

## [v3.2.0] — 2026-07-03 — "Proactive Intelligence + Multi-Agent Coordination"

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

## [v2.1] — 2026-07-03 — "Multi-tool, zero ops"

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

## [v1.0] — 2026-04 — "Karpathy-style vault with session memory"

### Added
- 3-layer architecture: raw/ (immutable) / knowledge/notes/ (compiled) / memory/ (session lore)
- 7-check structural lint with LLM contradiction detection
- Multi-project slug system with 5-step collision resolution
- QMD hybrid search (BM25 + vector + reranker)
- Promotion pipeline from project memory to cross-cutting wiki
