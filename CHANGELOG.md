# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- OpenCode `chat.message` capture defensively accepts observed runtime payloads whose message object omits `role`, while still rejecting explicit non-user roles and preserving prompt deduplication.
- OpenCode lifecycle events now use the supported `event()` hook and inject bounded session context automatically.
- Pending memory compilation runs through the active authenticated OpenCode SDK session; SDK-only mode no longer falls back to a separate CLI or lower model.
- OpenCode SDK-only classification, compile, and deferred queue service prompts select exactly `openai/gpt-5.6-luna`; each operation-owned ephemeral session is aborted and then deleted in `finally`, with provider and cleanup errors logged.
- Compile locking uses OS-held file locks on Windows and POSIX, with bounded timeouts, crash-safe release, and durable timeout errors; failed backend setup/authentication text is rejected as model output.
- Crash replay identifies writes by stable batch/operation position, so changed provider wording cannot duplicate a committed note when progress persistence is interrupted.
- Accepted compile plans are fsynced to a bounded durable journal before note mutation; strict whole-plan validation, exact batch-set progress, and resumable index state prevent partial rejection, budget-layout skips, and false completion.
- Per-daily compile generations are pinned by strictly re-derived immutable fsynced manifests, while byte-prefix checkpoints in atomic state preserve append cursors independently of manifest retention; retries keep the original ordered layout across budget changes and source appends, provider plans accept only create/update actions, and malformed SDK containers fail durably.
- Codex uses native lifecycle hooks merged non-destructively into `~/.codex/hooks.json` with a timestamped backup.
- Deferred tasks use a durable monotonic enqueue sequence under a cross-process lock; deterministic journaled migration preserves FIFO order for legacy queues and interrupted upgrades. SDK and manual drainers now share one atomic claim path, prioritize eligible noncompile work, and never count terminal or backed-off entries against the processing cap.
- SDK queue results require a matching task ID, lease ID, and digest; acknowledgement follows successful application only, while failures are durably re-queued with a 60-second backoff, a 5-attempt ceiling, and 10-minute stale-lease recovery. SDK and manual compile success now share lease-revalidated queue-to-daily settlement, requeue new work without an attempt, and report manual work as pending rather than completed. Manual compile controls wait for the synchronous compiler exit, deferred flush markers are checked and appended under one daily lock, and queued provenance is redacted and bounded before persistence.
- Queue provenance recovery is immutable at read time and fails closed on unsupported or malformed version schemas. Bounded, abortable authenticated source-session lookup finishes before provider work; its transient SDK root is independently revalidated against canonical project ownership, no duplicate apply follows Python's "failure recorded" report, and exhausted tasks remain unchanged.
- Nightly maintenance returns nonzero for failed or remaining queue/compile work and required lint/index failures; weekly holds one lease across all mutations and the final Markdown, FTS5, and graph rebuild, reports exhausted task counts and canonical IDs without payloads, and never deletes failed tasks automatically.
- Installers treat only an absolute `XDG_CONFIG_HOME` as OpenCode's effective config root, fall back to `~/.config`, and on Windows add the normalized compatibility destination only when distinct.
- Installed-memory repair now uses schema-v4 read-only audit, sealed unapproved preparation, manifest-bound apply, and verify stages. Every output is preflighted outside vault/state roots before work, manifests must be exact direct transaction children, and a durable `preparing` journal owns all source staging before its first byte. Mutating apply accepts only schema v4 with the top-level `approved` field changed to `true`, reuses the preparation journal, revalidates audit/source/identity state under fixed-order writer locks, and physically removes only byte-exact noncanonical note shadows, explicitly named active stale notes, producer-shaped unpromoted idle feedback, or whole generated-only daily files with exact completion-marker coverage; it may also replace one exact trusted handoff placeholder. Ordinary and ambiguous content remains preserved or report-only. Commit and rollback persist exact purge authorization before each staging deletion, while preparation recovery preserves unknown paths. Completed v4 cleanup has no source backup or rollback. Legacy schema-v3 recovery and verification remain available, but v3 cannot enter mutating apply. No production cleanup or live restart is claimed by this entry.
- SessionStart serializes project-state claims through a runtime advisory lock and atomically publishes complete states with strict JSON ownership metadata. Missing bootstrap output is retried by a detached, per-project-locked worker and published atomically; compile health uses only bounded file metadata and durable queue/generation/index evidence, while Claude hook replacement is limited to exact legacy commands and current-vault exec paths.
- Claude settings merging now refuses malformed or non-object existing JSON without changing a byte; new project states render placeholders in one pass; project slugs use one bounded Unicode-alphanumeric/`.`/`-` grammar across allocation and daily selection; published bootstrap data appears as bounded, explicitly untrusted SessionStart context without entering the search index; and persisted compile/index warning or failure state can no longer report as up to date.
- Project ownership now rejects empty, oversized, relative, and non-native roots before resolution, with malformed canonical JSON failing closed. Bounded contained scans include valid absolute legacy ownership, exact-path legacy states receive atomic persisted runtime aliases without handoff changes, and all consumers reserve and reuse those aliases. Slug allocation verifies deterministic 6/12/24/40/64-character hash candidates before bounded UUID fallback under the claim lock; malformed `Project slug` heading metadata cannot become global daily content. SessionStart preserves identity and the saved handoff ahead of untrusted bootstrap detail, and SDK compilation publishes canonical compile/index health only after the final index rebuild succeeds.
- Project identity now converges through one confirmed ownership/claim helper. Registry inventory is complete under the claim lock and fails closed on iteration or state-read errors, including aliases beyond entry 512; capture, flush, Codex, SessionEnd, OpenCode, and bootstrap consumers no longer synthesize basename, free-candidate, explicit, or `unknown` identities. Daily context requires both the persisted alias and absolute root, exact legacy state paths flow into advisory and bootstrap consumers, bootstrap provenance is revalidated before write and injection, and SessionStart bounds index/log/state/bootstrap reads while malformed top-level runtime state is preserved and treated as empty.
- Claude settings installation is version-aware: Claude Code before 2.1.139 and unknown versions receive absolute, literal-quoted Bash or PowerShell command hooks, while 2.1.139 and newer retain direct argv hooks. Legacy and modern forms remerge idempotently, unrelated hooks remain intact, and SessionStart now covers `fork`.
- Claude and Codex hook mergers now reject malformed known handler fields before writing while preserving unknown fields, existing opaque values, and emptied managed blocks that carry future metadata. Timeouts are nonnegative integers; insertable Claude schema fields are type-checked; Codex Windows paths preserve literal percent characters through `cmd.exe`; and unmarked legacy ownership requires the current vault root. Atomic publication keeps an advisory lock plus an expected-base check directly before replacement, as optimistic protection against noncooperating writers.
- The shell installer now authenticates LLM Wiki checkout identity instead of trusting ambient stdin working directories, accepts only valid explicit roots, resolves profile symlinks and atomically updates their referents with final content and symlink-binding conflict detection, handles unset `SHELL`, and escapes literal percent characters for crontab parsing. Successive-install regression harnesses explicitly provision the agent branch whose post-configuration call they assert.
- Lifecycle hooks, OpenCode append/queue/compile bridges, and stdin search now apply explicit byte caps before decoding attacker-controlled input. Oversized, invalid UTF-8, malformed, non-object, lone-surrogate, and parser-resource-failing JSON fail closed before append, spawn, lease acknowledgement, state mutation, feedback capture, or search. SessionStart keeps interactive stdin nonblocking; trusted explicit directories bypass stdin while still conflict-checking env, and rejected non-interactive input without one exits before context/runtime work. OpenCode shared-cache generation supplies an explicit empty JSON object. SessionEnd reads and rejects stdin before filesystem validation or error logging.
- State updates now distinguish missing files from transient read failures, atomic writes use exclusive per-writer temp files, recursive inventories reject nested links and Windows reparse points before traversal, and each bound directory scan revalidates no-follow device/inode/type identity before consuming entries. Windows scans also hold a validated minimum-list-access, no-delete-share directory handle across scanner open and iteration, while every scanner-bound entry is compared with an independent lexical no-follow identity/type/reparse check before matching or traversal. Queue inventory fails closed under explicit entry/byte/depth caps and noncanonical case-variant task suffixes. Codex child execution uses one deadline through pipe-thread completion, including descendant-inherited handles, while OpenCode child execution has a bounded deadline and transcript work is capped before message normalization.
- Compile dedup snapshots now read each page once under a strict 64 KiB UTF-8 cap, use the search body parser, and fail closed on ambiguous frontmatter status without treating body prose as metadata. Feedback listing and guardrails share one bounded JSON file loader while listing retains its stricter candidate schema. PowerShell profile temps mask both read-only and reparse attributes before publication while retaining defensive failure cleanup.
- OpenCode project attribution now uses `worktree`, then `directory`. Prompt capture remains enabled, while tool breadcrumbs are limited to direct file mutation tools; read, search, and shell activity creates none, and persisted targets/provenance are bounded and redacted.
- Daily context normalizes timestamp blocks and legacy heading/bullet summaries in memory, prioritizes project-matching durable summaries and user prompts, and excludes tool breadcrumbs.
- Queue maintenance uses bounded repeatable passes and continuations while eligible work remains. Durable task provenance is applied once, while missing legacy provenance remains explicit.
- Compile admission is deterministic before journaling or mutation: completed lifecycle records require a valid tier, matching source session, canonical project identity, and durable-section bullets. Create and update citations are section-checked, updates require the exact source project identity, provider counters cannot bypass admission, and ambiguous pairs remain report-only.
- Compile evidence citations now require one complete normalized bullet and resolve through a single bounded occurrence index reused by normalization, journal replay, execution, and receipt validation; substring decoys cannot authorize or poison an exact citation.
- Compile completion now requires every pinned batch, a successful Markdown index rebuild, and a self-validating effect receipt. Stale or missing receipts invalidate trust and replay; bounded journals/manifests can reactivate from nondestructive retired stores and fail closed at truthful quotas.
- Full retired compile stores now have a compiler-owned cold archive with `audit` -> `backup-only` -> approved `apply` -> `verify` phases. It archives only whole components outside state, queue, and active-artifact recovery closure; seals byte-exact reviewed payloads, retains exact original hot files, resumes from a durable progress journal, rejects link/ABA/identity drift, and deliberately stays outside automatic replay.
- Retrieval and Q&A publication now use immutable canonical snapshots with exact source hashes, strict UTF-8 candidate sizing, secret-safe bounded raw/sanitized provider evidence, rendered-page collision identities, hardlink rejection, versioned generation manifests, same-connection FTS schema/generation validation, exact bounded/no-follow vector-cache schemas, one shared compile/Q&A publication lock, corrupt-SQLite rebuild/retry, lexical JSON resource preflight, relevance-first trust tie-breaks, strict ISO dates, and canonical benchmark inputs.
- Project handoff injection now requires an exact canonical root/runtime-alias JSON tuple, removes only structurally exact template sections plus PID/time-only handoff metadata, and preserves legitimate angle-bracket and code content. Bootstrap context records an exact Git HEAD or explicit non-Git status and is omitted when that bounded, confirmed-root freshness check no longer matches.
- Windows Task Scheduler launches maintenance with `pythonw`, and maintenance child processes are created without console windows. The OpenCode plugin also bypasses uv's console venv redirector by resolving the base `pythonw.exe` from `pyvenv.cfg` while retaining venv packages, preventing repeated foreground `conhost.exe` flashes during SDK queue work.

### Testing
- **2793 tests collected (platform-stable); local Windows verification: 2749 passed, 44 skipped.** Skip count varies with optional Bash, PowerShell, and symlink availability. Coverage includes OpenCode lifecycle injection and project attribution, mutation-only breadcrumbs, dual-format project-safe daily context, repeatable queue maintenance and provenance, deterministic project-scoped compile admission, effect-receipt replay, bounded retired-store reactivation and transactional cold archive, windowless Windows maintenance, bounded and Unicode-scalar-safe stdin, durable compile journals and generation manifests, OS-backed compile and queue locks, complete project-claim inventory, strict handoff/template filtering, Git-fresh bootstrap provenance, bounded hook/advisory/bootstrap/settings inputs, safe Claude/Codex hook migration, XDG-aware plugin installation, transactional installed-memory repair, canonical active-note retrieval, grounded create-only Q&A filing, and durable weekly failed-queue reporting. CI has not yet verified this Unreleased worktree.

## [3.4.0] — 2026-07-11

A comprehensive security, concurrency, and quality release following 9 rounds of
full-codebase audit. **281 tests** (up from 226). **Zero Critical, zero High**
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
