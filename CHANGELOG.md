# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- CI installs the production profile into a clean environment on Windows and macOS too, and runs the install smoke there (audit OPS-14).
- One end-to-end nightly test runs the pass with its real step runner against real child processes, one of which fails, and checks the report line, the artifact and the recorded state (audit OPS-17).
- **The query surface answers the whole graph (#24, B).** `get_architecture`
  gains `mode=search` — ranked qualified names with in/out degree, exact
  `total` and `has_more`, globs, a path prefix — and `depth` (1–8) on
  `callers`/`callees`, a breadth-first CALLS closure reporting
  `depth_applied` and `depth_frontier_open`. `mode=snippet` accepts
  `owner.name` and cuts the block out of the generation's stored bytes at the
  exact definition span (`precision: "exact"`, `freshness` against the
  working tree). `mode=coverage` now answers `indexed` and `freshness` from
  the generation's own source row — a foreign repository's indexed file
  answered `indexed=false` beside a real node count — and adds a `parse`
  block naming the `ERROR`/`MISSING` ranges (tree-sitter) or `SyntaxError`
  line (Python) the extractor could not read. `mode=impact` adds
  `affected_symbols`: the code symbols a dirty diff reaches within eight
  hops, beside the unchanged `affected` groups. No new tool, no generation
  format change. Measured warm on a 1 020-file fixture: search ~99 ms,
  snippet 13 ms, coverage 16 ms, callers depth 3 from 20 seeds 144 ms.
  `data_flow`/`cross_service` tracing is not feasible on the current graph
  (no `DATA_FLOWS` or route-call edges) and is named as such. New modules
  `scripts/symbol_search.py`, `scripts/impact_symbols.py`; new readers
  `EvidenceGraph.source_by_path`, `source_observations`, `search_nodes`.
- **The weights arrive with the install.** `scripts/install_models.py`
  fetches the encoder and the default reranker at their pinned commits,
  only the files the loaders read, verifies `model.safetensors` against the
  size and SHA-256 recorded beside each revision, removes a file that does
  not match, and never fetches a present file again. The installer runs it
  when the semantic extra is present, the nightly pass runs it every night
  (a no-op once the weights are there), and `doctor` reports `models:
  degraded` with the command while they are missing. Before this nothing in
  the product downloaded a model: a fresh install answered by words alone
  and only the trace said so.
- **Background incremental refresh of repository indexes (#24, A).**
  `repository_index.py refresh <dir>` hashes a registered repository's sources
  against its newest generation and rebuilds only when something changed,
  reusing every unchanged record (one edited file in a 1 022-file fixture:
  100 rebuilt, 922 reused, 16 s against 60 s for the full build), fenced under
  the ownership registry's `doctor` role scoped to that one repository. The
  MCP server spawns it detached once per repository and commit when a
  structural answer finds the checkout's commit ahead of the generation's; the
  nightly pass runs `refresh-all`. Structural answers carry a `freshness`
  block naming both commits and what was done. `repository_index.py` gains a
  command line (`index`, `list`, `detect`, `refresh`, `refresh-all`).


### Changed

- The retrieval stand obeys the complexity gate: one run is an object with a method per stage (build, selection, embedding and its lexical fallback, materialized retrieval, reranking, evaluation, report), report verification and selection aggregation are named checks, the CLI is a table of modes; report bytes, messages, error order and clock reads unchanged (audit H3).
- The navigation stand obeys the complexity gate: schema validation is one function per keyword, the fixture run is one object with a phase per measurement, the evidence check is a list of named predicates, the gates are one entry per field; reports, error order and gate verdicts unchanged (audit L13).
- The comparative stand obeys the complexity gate: one check per manifest section, one finding collector per preflight probe, the paired statistics as named steps; messages, codes and RNG consumption unchanged (audit L13).
- The scale stand obeys the complexity gate: the three optional adapter cells share one cell shape, the crash matrix is one outcome per point with named steps; reports and adoption reasons unchanged (audit L13).
- The Python qualification generator and the contradiction benchmark obey the complexity gate; their output is byte-identical (audit L13, first two files).
- `pyright_profile` obeys the complexity gate: the Node probe is one run with named phases, JSONC normalisation is two small scanners, and each system-candidate shape is one function; degradation codes and precedence unchanged (audit OPS-15, the last file in scope).
- `lsp_protocol` obeys the complexity gate: the frame reader, the JSON validators, start-up, the writer loop and the fatal transition are small steps under the one state lock; messages, outcomes and lock discipline unchanged (audit OPS-15).
- `lsp_security` obeys the complexity gate: the no-follow walk, the provider URI check and the path redaction scanners are pipelines of named steps; every containment message and refusal order is unchanged (audit OPS-15).
- `sync_memory`, `install_smoke` and `lsp_positions` obey the complexity gate: the sync run is a table of actions over one `_SyncRun`, the three `uv` steps are one step record, the file-URI parser is a pipeline of named checks; behaviour and messages unchanged (audit OPS-15).
- Three tests no longer sleep to assert that a worker is still blocked; the outcome after the release is the proof (audit M9).
- The public search path is a pipeline over one `_SearchRun` object instead of a 290-line function with eleven closures; behaviour and the trace are unchanged (audit L8).
- The 16 and 64 GiB constants of the evidence graph and the generation catalog are declared as absurdity ceilings that name where the real read bounds live (audit M8).
- Test helpers obey the complexity gate: the 24-arm damage ladder in the evidence-graph tests is a table, and six other test functions are split into named helpers (audit M12).
- **A `recall` row carries its page, not the trace.** Through MCP each row
  repeated the twelve trace fields and thirteen per-signal scores the
  envelope already reports once; rows now carry the page, its score and
  one per-signal score. Measured on one five-row call: a row 1 112 → 541
  bytes, the envelope 8 849 → 4 996 (audit M7, rule 4). Research:
  `docs/research/2026-09-11-a-row-carries-its-page-not-the-trace.md`.
- **The installer job runs on macOS too.** The LaunchAgent path had no CI
  evidence (audit OPS-14); `macos-15` joins the installer matrix. Research:
  `docs/research/2026-09-11-the-installer-runs-on-every-platform-it-claims.md`.
- **The shipped Claude Code allowlist grants only read-only forms.** `Bash(sed *)`,
  `Bash(xargs *)`, `Bash(sort *)` and `Bash(uv run --directory *)` let an
  agent rewrite files or run any Python without a prompt under a
  read-only-looking name; they are gone, `sed -n *` stays, and the built-in
  read-only commands (`ls`, `cat`, `grep`, `find`, …) need no entry. The
  settings merge retires exactly those four strings from an installed
  `~/.claude/settings.json` on the next install or sync (audit OPS-12).
  Research: `docs/research/2026-09-10-an-allowlist-that-reads-as-read-only-must-be-read-only.md`.
- **The nightly and weekly passes take the canonical fence.** On an adopted
  vault they hold the registry's `nightly`/`weekly` lease with a heartbeat,
  so the doctor and the `run/` deletion contract see a running pass; a lost
  fence stops the pass before its next step and is recorded as
  `owner_fence_lost` instead of success; a marker a dead owner left behind
  is reclaimed only with the registry's proof (expired lease and a provably
  dead process, or an ownerless marker naming a PID that no longer exists),
  never by age. A vault without a V3 coordinator keeps the legacy marker. The
  `inspect.signature` ownership plumbing that no step ever received is gone
  (audit OPS-02, OPS-03; owner's yes 2026-09-10). Decision:
  `knowledge/notes/nightly-takes-the-canonical-fence-decision.md`; research:
  `docs/research/2026-09-10-the-nightly-and-the-fence-it-never-takes.md`.
- **The cross-encoder reranker is on by default.** `BAAI/bge-reranker-v2-m3`
  at its matrix-pinned revision is the product default when the environment
  names no reranker (`LLMWIKI_RERANKER_MODEL=off` switches it off), it
  reranks every question in a rerank profile instead of waiting for a
  trigger a Russian question over English pages never matched, its depth is
  10 over the fused pool of 20, and the MCP server loads it at start-up so no
  question pays the load. Measured on the 45-query cross-lingual corpus:
  cross-language MRR 0.60 → 0.98 on the shipped encoder, where swapping the
  encoder gained at most 0.04 (issue #29.3). A load that fails is recorded
  once and not retried per question.
- **Structural code answers are warm (#24, A).** The validated Evidence Graph
  reader is kept per MCP process and reused while the catalog, the artifact
  and the checkout's Git state keep their stat identity, instead of the
  catalog re-validating the generation three times per open and hashing every
  artifact each time. Same 44.7 MB generation, warm p50: `callers` 42 ms (was
  511), `callees` 23 ms (249), `symbol` 86 ms (1 007), snippet 21 ms (335),
  coverage 21 ms (258).
- **`code_graph.py --callers` no longer re-parses a repository without a
  generation** (300 s on 1 026 files in #24): it names `mode=index` and exits
  2; `--live` opts into the scan.

### Fixed

- The scale stand's exact cell no longer grades itself: it reports no recall, with the reason, instead of 1.0 by construction (audit M11).
- A queue heartbeat thread that does not stop within twice its heartbeat is refused by name (`heartbeat_stop_timeout`) instead of joined forever (audit OPS-18).
- Impact analysis no longer names the symbol after a grown line as changed: a hunk is matched against the generation's occurrences by its old byte range, the coordinate system the generation indexed (audit M13).
- **Git warnings are not diff records.** `impact_analysis` read Git's
  stderr together with the `-z` record stream, so on a checkout with
  `core.autocrlf=true` the advisory line about line endings made every
  impact answer empty and partial (`malformed zero-delimited Git diff
  record`, PR30 runs 34535006773–34550352312 on Windows). Stderr is kept
  apart and quoted only on failure. Research:
  `docs/research/2026-09-11-git-warnings-are-not-diff-records.md`.
- **`claim_operation` is a pipeline under CCN 5** (the gate refused
  `mutate` at 13 in a file the OPS-21 fix touched).
- **One answer to "is this process alive".** `scripts/process_liveness.py`
  holds the three-state probe (`alive`, `dead`, `unknown`) lifted from the
  doctor; `memory_state`, `markdown_transaction` and the doctor delegate to
  it, and a legacy lock treats doubt as alive — a process owned by another
  user is no longer read as dead and stolen (audit OPS-08). Research:
  `docs/research/2026-09-11-one-answer-to-is-this-process-alive.md`.
- **The compile-lock owner token travels in return values.** `maybe_compile`
  no longer keeps the claimant's token in a module global; the claim returns
  it and the release takes it (audit OPS-22).
- **`compile_memory` imports its lock module once.** Two per-call
  `sys.path.insert` grew the import path on every compile-lock check
  (audit M10).
- **A skipped night and a failed adoption say why.** The doctor's stale
  nightly message names the last pass's skip reason when the skip is newer
  than the last run; `install.sh` keeps the V3 adoption's stderr in
  `logs/install-adoption.err.log` and quotes its tail on failure instead of
  telling the user to rerun the command blind (audit OPS-23, OPS-20).
  Research: `docs/research/2026-09-11-a-skipped-night-and-a-failed-adoption-say-why.md`.
- **Nine small findings closed at once.** A bad `MEMORY_LLM_TIMEOUT_S` is
  refused by name; the compile budget is written once; the session record
  renders its transcript once; the self-alias of `GenerationSealChanged` is
  gone; a generation's descriptors close with `OSError` handling and one
  named error; the flush docstring says "bounded"; `--status` opens the
  legacy index read-only; the optional-stage worker and the detached
  provider no longer swallow interrupts; `merge_claude_settings` is under
  the complexity gate (audit L5–L12). Research:
  `docs/research/2026-09-11-nine-small-findings-closed-at-once.md`.
- **A dropped best-effort write is counted, and one bound is declared once.**
  Six best-effort writes (capture-operation state, feedback capture, the
  three MCP telemetry emitters) now count their failure in the
  capture-failure trail instead of `pass`; the nightly hands the repository
  refresh its own budget (`--budget-seconds`) and waits a margin longer, so
  the child's graceful deferral runs before the parent's kill (audit OPS-21,
  OPS-10). Research:
  `docs/research/2026-09-10-a-dropped-best-effort-write-is-counted-and-one-bound-is-declared-once.md`.
- **A silent fallback names its cause.** An encode that raised, a corrupt
  `vectors.npy` or catalog, an unusable graph all looked like "no vectors
  yet"; the eight sites now record `Class: redacted message` by kind
  (`search_memory.degradation_reasons()`), say it once on stderr, and the
  health resource carries `retrieval_degradations` (audit M5, M6). Research:
  `docs/research/2026-09-10-a-silent-fallback-names-its-cause.md`.
- **A page that cannot be flushed is named.** `access_tracking.py` was the
  one module under `scripts/` the complexity gate refused (four functions
  up to CCN 15, six bare excepts); a page whose frontmatter export failed
  was skipped in silence while the cursor moved on. The module is a pipeline
  of small steps with the same behaviour, and a failed page is reported with
  its reason (`last_flush_failures()`, stderr, `--flush` output) while the
  bounded scan still advances (audit H2). Research:
  `docs/research/2026-09-10-a-page-that-cannot-be-flushed-is-named.md`.
- **A failing in-process step says why, not only its class.** The nightly's
  health report, three doctor repair paths and the self-update reported a
  failure as `RuntimeError` alone; they now report
  `Class: redacted message` through one helper, and the tests assert the
  message (audit OPS-09, the rest of OPS-17). Research:
  `docs/research/2026-09-10-a-failing-step-says-why-not-only-its-class.md`.
- **A stale lock is moved aside and checked before it is removed.** The
  three legacy lock stealers (`run/compile.pid`, `run/state.json.lock`, the
  legacy `run/maintenance.lock`) decided "stale" from one read and then
  unlinked whatever was at the path, so two stealers could remove each
  other's fresh lock and both proceed. One helper now renames the lock aside
  (one winner), deletes it only while it still holds the judged bytes, and
  puts a fresh owner's lock back; the legacy marker is judged by its process,
  not by age (audit OPS-07). Research:
  `docs/research/2026-09-10-a-stale-lock-is-moved-aside-and-checked-before-it-is-removed.md`.
- **A maintenance step that times out takes its children with it.** The
  nightly and weekly runner killed only the direct child at the bound; a
  step's own workers (queue processors, repository refresh, model download)
  kept writing while the pass moved on. Steps now run through the tree
  runner the sync already used, and the log says whether the tree ended
  (audit OPS-06). Research:
  `docs/research/2026-09-10-a-step-that-times-out-takes-its-children-with-it.md`.
- **A failed MCP warm-up is in the health answer.** The retrieval warm-up
  swallowed every failure, including `KeyboardInterrupt` and `MemoryError`,
  and the first answers of a session silently fell back to the lexical leg.
  It now records its state (`not_started`, `running`, `warm` with seconds,
  `failed` with stage and error class), prints one line to the server log,
  and `llm-wiki://health` carries it with a warning (audit OPS-13). Research:
  `docs/research/2026-09-10-a-failed-warm-up-is-in-the-health-answer.md`.
- **Docstrings and docs say what the code does.** The nightly's header
  names its scheduler and its steps, `maybe_compile` and its tests describe
  the lock they have, the search module quotes measured costs instead of
  "<10 ms", and the last LanceDB and `SETUP-COGNEE.md` mentions are gone
  (audit OPS-16, L1, L2, L3).
- **The status names what the search reads.** `search_memory.py --status`
  reports the active generation (id, extractor, vector state, model) before
  the legacy index, `--rebuild` says it rebuilds the legacy index only, and
  the user guide documents the on-by-default vectors with `--no-semantic`
  and points an empty search at `doctor.py` (audit M2, M3). Research:
  `docs/research/2026-09-10-the-status-names-what-the-search-reads.md`.
- **One page ceiling for every reader of `knowledge/`.** The guardrails
  snapshot, the compile after-image, the index rebuild and access telemetry
  refused a page at 4 MiB, backlink repair at 512 KiB, while the journal,
  claim tree, corpus and search accepted 8 MiB — the family of the journal
  incident (audit M4). `bounded_io.MAX_KNOWLEDGE_PAGE_BYTES` is now the one
  declaration and eleven readers alias it; a test holds them equal. Research:
  `docs/research/2026-09-10-one-page-ceiling-for-every-reader-of-knowledge.md`.
- **The Windows installer says "owned" only after the transaction committed.**
  A failed install-ownership transaction no longer prints
  `Claude settings owned by the install transaction` or lists Claude Code as
  active automatic (audit OPS-05). Research:
  `docs/research/2026-09-10-the-installer-says-owned-only-after-the-transaction-committed.md`.
- **A lost session record is written down.** `write_session_evidence`
  still never raises, and a refused write now lands in the capture-failure
  trail and counters with its reason and session (audit H5). Research:
  `docs/research/2026-09-10-a-lost-session-record-is-written-down.md`.
- **Every hang bound in the tests comes from one place.** 330 literal
  bounds on `join`, `result`, `get` and `wait` in 26 files under `tests/` now
  name `SHORT_TIMEOUT` or `LONG_TIMEOUT` from `tests/slow_machine.py`; a
  bound the test expects to elapse, or a pause whose result the test
  discards, stays literal by design, and
  `test_no_test_carries_a_literal_hang_bound` keeps the count at zero
  (audit OPS-04). Research:
  `docs/research/2026-09-10-every-hang-bound-in-the-tests-comes-from-one-place.md`.
- **A compile lock lives as long as its process, not thirty minutes.** The
  legacy lock declared a live compile stale after 30 minutes, so the nightly
  ran lint, backlink repair and the index rebuild on top of it and the next
  trigger reported a race that never happened (audit OPS-01, OPS-11, H4).
  One predicate now answers absent, stale or live for every reader; a lost
  claim names the lock that won; `maybe_compile` decides its exit code from
  the outcome and not from the reason text; `compile_memory` refuses to run
  when the lock cannot be taken or read and says why. Research:
  `docs/research/2026-09-10-a-lock-lives-as-long-as-its-process-not-thirty-minutes.md`.
- **Warm structural answers on every supported Python, and the tests that
  prove them pass on every runner.** PR30 run 34500804888 was red in 13 of 50
  jobs. The reader cache demanded a serialized sqlite3 build (`threadsafety
  == 3`), which Python 3.10 never reports, so on 3.10 every answer reopened
  its generation and the MCP answer lost its `freshness` block; a lease
  already hands the reader to one thread at a time, which is all SQLite's
  multi-thread mode asks, so only a single-thread build is refused now.
  Three Windows-only test defects were fixed at the class: the idle-reader
  test drives the cache's clock instead of trusting a 15.6 ms monotonic step,
  a test removes a Git checkout through `tests/filesystem.py::remove_tree`
  (read-only objects), and the timeout-scale test no longer starts a child
  Python without `SYSTEMROOT`. Research:
  `docs/research/2026-09-10-a-cached-reader-needs-one-thread-at-a-time-not-a-serialized-build.md`.
- **The compile commits again; a project journal is no longer a claim page.**
  No compile had committed since 2026-09-07: the claim index read
  `knowledge/projects/*/journal.md` and refused the 4.2 MB `no-hands`
  journal at a 4 MiB cap the journal itself did not have, so every draft
  was recorded as a validation error. The claim tree, the claim index and
  lint now share one file set, `context.md` and `state.md`: the journal is
  the event log the state is projected from, carries no claim ledger, and
  is not scanned. Every reader that can still meet a project page accepts
  the journal's own 8 MiB ceiling, and one test holds the bounds together.
- **Tests wait as long as the slowest supported machine needs.** Two
  Windows jobs went red on waits sized for a fast disk. Every wait in the
  tests now comes from `tests/slow_machine.py` as CPython's
  `SHORT_TIMEOUT`/`LONG_TIMEOUT`, scaled by `LLM_WIKI_TEST_TIMEOUT_SCALE`,
  and a generation build reports the seconds each phase cost in its
  outcome, built or deferred, which the nightly log prints.
- **A generation built by an older extractor is served, not refused.** The
  FTS content check re-derived every chunk with the current chunker and
  compared; after the 2026-09-08 chunker change every generation built
  before it was "semantically invalid", retrieval fell to the legacy BM25
  index (`generation_unavailable`, lexical-only, empty for Russian) and the
  six failing validations cost 40 s on every search of every process. The
  content check now applies only to generations this extractor built; an
  older one is validated structurally and served until the nightly rebuilds
  it, and a refusal is remembered under the same hashed identity as a success.

### Removed

- **The second, unreachable retrieval pipeline.** `search_memory._search_backends`
  had no caller; 40 functions reachable only from it (legacy triple RRF, its
  own reranker call, its own generation search) and the tests that existed
  only for them are gone — 897 lines. The live path is unchanged:
  `search()` → `retrieval.retrieve_via_search_memory` → `retrieval.fuse_rrf`.
  Audit H1; research `docs/research/2026-09-10-one-retrieval-pipeline-not-two.md`.
- **The repository ships no memory.** The 89 published pages under
  `knowledge/notes/` (the owner's architecture decisions and the
  demonstration pages), the two synthetic daily logs and the vault's log
  entries leave the repository; a fresh install starts with an empty memory
  instead of another person's guard rails and "89 curated pages" (issue #19).
  The owner's pages stay where they are, private. `knowledge/index.md` and
  `knowledge/log.md` ship as empty skeletons the runtime fills.
- **The memory index no longer holds the product's own code.** The vault
  generation collects `knowledge/` only; `scripts/`, `docs/`, `tests/` and
  `benchmark/` of the checkout were 92 % of an installed vault's chunks and
  outranked the user's pages in `recall` (issue #29.2). Code is indexed per
  repository, the checkout included when its owner asks.
- **The legacy BM25 benchmark gates are retired.** `run_benchmark.py
  --legacy-only` and the generated-query run measured recall over the pages
  the repository used to ship; with no pages shipped there is nothing to
  measure. `run_benchmark.py` is the retrieval-v2 entry point; the CI step
  and `benchmark/legacy-60-v1.json` are gone.
- **The vault stands are retired**: retrieval, application, contamination
  and lift attribution, with their answer-key and entry-point helpers. Their
  questions and gold pages were the owner's decision pages; with those pages
  private there is nothing public to run them on. The synthetic
  `retrieval-v2.json` corpus (which carries cross-language queries) and the
  LongMemEval stand are the public measurements.
- **Cursor and Antigravity are no longer supported platforms.** The owner uses
  neither, and carrying two hosts nobody exercises meant two managed hook
  formats, two doctor checks, two installer detections, and two event
  projections whose only evidence was their own tests. Claude Code, OpenCode,
  and Codex CLI remain supported. Gone: `integrations/cursor/`,
  `integrations/antigravity/`, the `--cursor-hooks` / `--antigravity-hooks`
  install flags, installer detection in `install.sh` and `install.ps1`, the
  IDE branches in `integration_adapter.py`, the two `event_envelope.py` agent
  patterns, the two `doctor.py` integration hosts, and the `cursor` /
  `antigravity` values of `flush_memory.py --agent`.
- What an existing user of those hosts loses: automatic capture and injected
  session context. MCP reads and actions were never platform-specific and are
  unaffected — any agent that speaks MCP can still use the vault.

### Fixed

- Two users' first-day findings (issues #17–#29, PR #27), each with its test
  and, where the design changed, a dated note under `docs/research/`:
  a fresh install adopts Reliability V3 so capture works at once (#17); the
  nightly follows a slow compile instead of failing it and prunes superseded
  generations (#21, #29.4); the provider and model chosen at install reach
  the hooks and the scheduler units (#22); a deleted project's journal can
  be rebuilt from its checkpoints (#20); a compile names every claim it drops
  and widens a partial quote to its line instead of dropping it (#28);
  `doctor` reads the vector state without the budget (#29.1); an empty
  `recall` names the generation it searched and the envelope repeats the
  trace's partial and fallback state (#26.1); a compile says `published` or
  `quarantined` per batch and records `last_compile_outcome` (#26.2); a
  capture write deferred by a writer race is counted apart from a lost one
  (#26.3); the claim check names each code's cause, the pages and the repair
  (#29.5); session start reads the nightly's health report instead of saying
  "not measured" every morning (#23.5); a busy maintenance fence names its
  holder (#29.6); Codex hooks are discovered natively and the internal
  classifier no longer captures itself (PR #27).
- On a vault that adopted Reliability V3 before its queue was ever used, the
  doctor reported the v2 queue migration as pending for ever and `--repair`
  aborted on the v2 tombstone before repairing anything; adoption retires
  that migration and both now say so. A capture write refused by a writer
  race is classified by the exception's type and code, never by its text;
  the state-lock timeout and the bound-elsewhere refusal are typed for it.
- The Codex hook probe waits for the peer it killed to be reaped within its
  own cleanup budget instead of the already-expired probe deadline; on
  Windows it had returned while the peer was still exiting.
- `uninstall` and `rollback` still take back a Cursor or Antigravity hook
  fragment written by an install from before the retirement. Deleting the
  writing code outright would have made the manifest name a resource the code
  no longer supplies, and the control plane fails closed on that
  (`install_resource_request_mismatch`) — leaving the fragment in
  `~/.cursor/hooks.json` or `~/.gemini/config/hooks.json` pointing at a vault
  nothing maintains, with no way to remove it. The projection readers and
  writers for both formats are therefore kept as a removal-only path;
  `write_owned` on them refuses with `install_resource_retired`.

- The grounded answer now captures the same corpus retrieval searched. It read
  the vault without the approved code roots, so every candidate under `docs/`
  or `scripts/` fell out of the snapshot and the answer refused itself for lack
  of evidence on questions search had just answered.
- A grounded answer sheds its weakest retrieved span instead of refusing when
  the selection overflows the context budget. Long pages made the mandatory set
  exceed the budget outright.
- The grounded-answer deadline is 120 s, measured: one provider round trip for
  a 4 KiB evidence prompt takes 32.5 s and the previous 30 s bound could not
  complete a single real call.
- A cited span must agree with the claim's own figures. Entailment is still not
  verified and not claimed; what is refused is the citation from the right page
  and the wrong sentence — "expires after 30 seconds" supported by the line
  about refreshing every 10.
- The installer-timeout test no longer depends on the kernel scheduling the
  child inside the installer's half-second escalation window, and its failure
  now reports which markers the child left behind.

## [4.0.0] — 2026-08-25

First release of the v4 line. The version has carried 4.0.0 since the platform
work landed, but nothing was ever tagged; this release publishes the audited
state, including everything found and fixed during the audit week.

### Since the audit (2026-08-18 → 2026-08-25)

#### Added

- Session records age out of the active tree after ninety days, moving to
  `knowledge/raw/sessions/archive/<YYYY-MM>/` — same bytes, one directory
  deeper, never deleted, in the weekly pass.
- `archive_stale.py --restore <slug>` brings an archived page back: archiving
  is dormancy, not deletion, so reactivation is part of the contract.
- A second benchmark stand measures *applying* memory rather than recalling it:
  each case counts only when the exact token needed to act reaches the reader.
  Live vault: 0.857 against grep's 0.429.
- The redactor knows the prefixed secret shapes a 2026 scanner catches —
  `gho_`/`ghu_`/`ghs_`/`ghr_`, `github_pat_`, `sk_live_`/`rk_test_`, `xapp-`,
  `npm_`, `hf_`, `pypi-`, `GOCSPX-`.

#### Fixed

- `doctor` answers every check at the default budget again. The corpus-wide
  generation check ran first and spent the whole budget; it now runs last. And
  `run/state.json` had outgrown the 256 KiB bound its readers use, which
  silently blinded the scheduler and capture checks — writers now keep it under
  three quarters of that.
- A cold CLI query costs 13 s instead of 47: the cross-encoder is loaded only
  when asked for with `--rerank`, since a one-shot call pays that load every
  time while the resident MCP server pays it once.
- A compiled page's citation resolves again. A day longer than 16 KiB is
  compiled in parts, and the page cites the part it was written from, but the
  resolver only ever compared the whole file — so every page compiled from a
  split day failed its own evidence check. The reader now accepts an
  entry-aligned slice that starts where a part starts and still hashes to what
  the page recorded; an edit inside the cited region still fails.

- The nightly pass adds the backlinks the vault owes instead of leaving them as
  findings for a person to clear. Compile writes pages that link outward and
  cannot edit the pages they name; the repair appends the missing link through
  the same transaction machinery as every other automatic writer, and never
  names a private page inside a published one. A superseded or archived page is
  history and is no longer asked to link forward.

- `doctor` calls a vault with registered generations and no active one
  degraded. An empty pointer used to read as "not activated yet", which is true
  for a young vault and false for one whose main read path just went away.

- A lost maintenance fence names itself: which check saw it, and what the owner
  row held at that moment. Three checks used to raise the same bare string, so a
  deferred nightly rebuild could only say that the fence was gone.

- Completing a blackboard task and resolving a blackboard conflict survive a
  retry. Both publish under a stable operation id but stamped the record with
  the moment of the write, so a caller retrying after a transient failure was
  refused with `operation_id is already bound to a different request` instead
  of finding its own earlier publication already there.

- `doctor` reports a locked FTS index as busy instead of as an exhausted time
  budget. The two verdicts were decided by the clock, so on a slow machine the
  wait for the lock consumed the budget and the more actionable answer was
  lost.

- A new structural test fails when any module we own binds the same top-level
  name twice. A second definition silently replaces the first, so an edit to
  the wrong copy changes nothing and an edit to the right one changes
  everything.

- Read-only opens on the generation catalog, retrieval telemetry, the MCP queue
  view, and `doctor` wait out a lock instead of failing on one. A commit on a
  rollback-journal database locks readers out for milliseconds, and a
  zero-length wait turned that normal moment into `database is locked` — a
  flaky test failure and a false `doctor` finding about an unreadable database.
  `doctor` spends at most half of its remaining budget waiting, so a genuinely
  busy database is still reported as busy rather than as an exhausted budget.

- A generation-catalog writer without a caller deadline waits out contention
  instead of surfacing `database is locked`. Two compare-and-swaps on a loaded
  machine can hold the write lock for longer than the previous five-second
  window, which turned a decided race into a raw SQLite error.

- Windows cleanup verification reads the process tree from the kernel snapshot.
  It used to ask `wmic`, which current Windows no longer ships, and fall back to
  a PowerShell CIM enumeration that does not finish inside its budget on a
  loaded machine; without an answer the worker reported
  `process_cleanup_failed` for a tree it had already terminated. Both tools are
  removed rather than kept as fallbacks.

- A grounded answer now fails when a cited span shares no content with the claim
  it is offered for, which is the case where a truthful citation about a
  different subject passed every other gate. It is a necessary condition, not
  entailment: support itself is still unverified and is not claimed.

- A Markdown write no longer fails when the writer-gate heartbeat is starved by a
  busy database. Ownership loss is now decided by the projection row, which a
  reclaim deletes, and the error names its cause. A queue worker likewise waits
  out a busy database instead of ending its run with `database is locked`, and a
  concurrent Pyright installer waits for a lock it does not own instead of
  deleting it.
- `doctor` reports lost captures: a new `capture` check names the per-kind counts
  from `state.json` and points at `logs/capture-failures.jsonl`. The two capture
  wrappers now record that loss when they are invoked directly and their detached
  flush cannot start. `doctor.py` also accepts `--time-budget` so a slow machine
  can finish its checks instead of reporting an exhausted budget.
- The installed OpenCode plugin carries the vault root it was installed from, so
  an OpenCode started from a desktop launcher captures instead of silently
  disabling itself. The environment still wins when it is set.
- The structural lint no longer counts `knowledge/daily/README.md` as an
  uncompiled daily log, and `docs/EXPORTING.md` no longer presents an export as a
  way to migrate a vault: an export carries committed files only.

- Made the v4 reliability platform fail closed: Windows path/handle identity no
  longer truncates IDs or compares incompatible creation/change times; SQLite
  readers close explicitly; MCP timeout tests drain late workers; metadata
  publication checks POSIX directory sync and Win32 flush/write-through moves;
  and locked production, optional, and development profiles directly own their
  required dependencies. Installers now run a bounded production MCP smoke
  instead of requiring pytest.
- Added a nonmutating installed-vault Reliability V3 inspector and redacted CLI,
  deterministic fresh/upgrade cutover validation, retained byte-identical v2 evidence,
  tombstone and crash-resume checks, and read-only SQLite validation. Public apply/adoption
  performs the offline v3 cutover on a fresh or quiescent vault (the earlier
  `reliability_v3_runtime_activation_incomplete` refusal was lifted once the v3 queue
  writers and canonical ownership landed); no runtime state is deleted. Since
  2026-09-10 the installer runs it, because session capture is refused until it has
  (issue #17).
- Bounded Claude and Codex integration configuration backups to 10 files, 90 days,
  and 100 MiB per integration. Changed merges now verify a byte-exact sibling
  preimage before atomic publication, preserve the newest restore point, prune only
  exact LLM-Wiki-owned backup names after verification, and create no backup for a
  no-op merge.

### Changed

- The Pyright qualification gate `warm_overhead_p95_ms` is 30 ms instead of 20.
  Three consecutive four-vCPU hosted runs measured 22.80, 22.08, and 22.16 ms,
  a spread under one millisecond, so the number described the machine rather
  than a regression: the navigation facade pays for its freshness guarantee
  with an extra workspace-revision walk. The gate still fails closed and now
  names the slowest supported machine class. See
  `knowledge/notes/warm-navigation-overhead-threshold-decision.md`.

### Added

- `benchmark/run_flush_classification.py` measures what session classification
  keeps and what it drops: tier accuracy, durable-content recall, and the
  false-promotion rate against a labelled corpus, scored through the product's
  own classification prompt. The shipped corpus is nine public synthetic cases;
  a real answer needs an installed vault's own sessions via `--corpus`.

- `memory_queue.py restore --export <path>` brings the work in one verified purge
  export back as new ready tasks, refusing the whole export if the manifest, the
  records digest, any result digest, or the id list fails to verify.
- `memory_queue.py purge --include-dead` retires attempts-exhausted tasks through
  that same export-first path. Without the flag they are retained, because a dead
  task records work the system promised and never did.

- Read-only Python code navigation through pinned Pyright 1.1.411: bounded stdlib
  LSP protocol, generation-aware process lifecycle ownership, workspace-revision
  freshness retry, normalized navigation facade, deterministic renderer, precise
  `get_architecture` modes, Pyright doctor diagnostics, seven-day LSP runtime
  retention, and a deterministic 100 KLOC qualification corpus with closed gates.
  Navigation adds no MCP tool, graph, semantic cache, daemon, or runtime root;
  Pyright is installed by one explicit operator command and never downloads
  during a query. Market superiority remains unclaimed.

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
  explicit non-goals. The full regression suite remains the release gate.
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
- Added the repository-scoped Pyright provider-session core with exact initialization,
  capability/readiness evidence, bounded document opening, semantic locations, hover,
  call hierarchy, push diagnostics, progress handling, restart replay, document
  synchronization, session-manager capacity, the normalized navigation facade,
  deterministic rendering, and precise MCP routing.

### Fixed

- Bound untrusted LSP runtime JSON nesting before decoding, independently of the
  process-wide Python recursion limit changed by optional dependencies.
- Reuse one bounded revision-keyed workspace inventory proof between immediate
  pre/post navigation checks. Directory, file, Git-index, semantics, cancellation,
  and race fences remain fail-closed, while unchanged 100 KLOC queries avoid a
  duplicate tree enumeration.
- Harden Pyright session ownership, generation-scoped `didOpen`, aggregate source and
  diagnostic bounds, interrupt cleanup, and lifecycle-owned readiness promotion. On
  POSIX, execute digest-verified server bytes from a held read-only descriptor through
  a bounded Node loader that preserves the original CommonJS filename and arguments.
- Requalify the pinned Pyright server before every initial or replacement LSP
  generation, hold its launch guard through bootstrap, and reject changed
  executables before a candidate generation can become active.
- Cap Pyright session startup at 60 seconds or the shorter caller deadline,
  apply that absolute deadline to preflight and cleanup, and preserve the
  entry-time budget for an autonomous configured restart.
- Require PATH-resolved Node executables to be local regular files outside reparse
  points and network filesystems, probe versions without reader threads through
  platform-qualified whole-tree cleanup before reading inherited pipes, and
  recursively freeze attested Pyright configuration values behind an explicit
  thaw-copy boundary.
- Prevent Pyright source-category laundering, block ancestor config search when a
  bounded root `pyproject.toml` has no object `[tool.pyright]`, fingerprint ordered
  per-file `pyrightconfig.json`/`pyproject.toml` inheritance, retain empty managed
  version roots as broken-install evidence, and reject recursive or unsupported
  config and receipt domains before canonicalization.
- Fail closed on LSP diagnostic text over the 256 KiB strict UTF-8 byte ceiling
  or containing lone surrogates before semantic scanning. Use O(1) code-point
  pre-rejection plus bounded allocation-free byte sizing, normalize and redact
  complete accepted values before the 1024-character output cap, scan canceled
  POSIX path components without overlapping retries, accept only local extended
  Windows drive aliases, and treat encoded backslashes as POSIX filename
  characters while continuing to reject encoded path separators.
- Normalize bounded POSIX LSP log paths and local file URIs lexically, with
  strict one-pass UTF-8 decoding, local-authority and sibling boundaries, and
  matching Windows spaced roots after dot-segment normalization.
- Complete the LSP security scanner with restartable nested terminal states,
  safe-space control boundaries, immediate component-prefix Windows root matching,
  canonical spaced-root aliases, and bounded one-character failed-candidate recovery.
- Finish LSP log token parsing by consuming ISO escape intermediates and
  recognizing Windows line/column, quote, and terminal-punctuation suffixes
  with one bounded linear component scan.
- Finish LSP alias containment by consuming two-byte ESC controls, matching
  mixed long/8.3 Windows path components through a bounded local API, keeping
  percent triplets literal in native paths, and scanning through quoted names.
- Reject Windows components that exact handle-relative opens reveal despite an
  omitted directory entry, scan URL authorities without treating RFC sub-delimiters
  as token boundaries, strip complete terminal control strings before credential
  matching, and redact local-drive path tokens through bounded lexical normalization.
- Normalize control and Unicode-format aliases before LSP credential redaction,
  scan every punctuation-separated URL authority, and match Windows path roots by
  normalized components including dot segments, trailing dots/spaces, and encoded
  Unicode case aliases without matching sibling prefixes.
- Publish explicit LSP recovery transitions with one sticky worker handoff. Recovery
  retries incomplete restart-failure cleanup with fresh bounded deadlines and event
  backoff, while the startup registry retains only coordinators with live ownership.
  Windows Job cleanup also gives captured process identities a bounded settle window
  after stable active-zero accounting without making reusable PIDs authoritative.
- Preserve exact startup and restart failure intent before deadline-bounded lifecycle
  transitions. Cleanup retries can no longer commit success, delete scratch, or drop
  startup recovery before immutable failure evidence reaches `STOPPED_FAILURE`.
- Complete second-fatal LSP cleanup autonomously from the recovery owner. The
  recovery thread quiesces its own tracked role, joins the heartbeat, releases all
  generation owners and handles, removes the live lease, and reaches
  `STOPPED_FAILURE` without a later caller `close()`.
- Retry Windows LSP lease replacement only for access-denied, sharing-violation,
  and lock-violation errors. Stop-aware waits are bounded by both the operation
  deadline and the previously published lease expiry, so a reader cannot cause a
  refresh to outlive the lease silently.
- Retain incomplete LSP startup ownership in a bounded module registry before the
  first owned mutation. Normal interpreter exit retries unadopted owners under one
  deadline; higher-level sessions can adopt returned cleanup ownership without
  consuming registry capacity and use the same bounded retry path.
- Bound retained LSP cleanup diagnostics to one sanitized current record per step.
  Stored records contain no raw exception, message, path, traceback, or frame graph,
  and successful retries clear stale records.
- Use current `ActiveProcesses`, not lifetime Job totals or a racing PID-list
  snapshot, for Windows LSP process bounds and cleanup completion. PID capture is
  now best effort, and an earlier snapshot error is discarded after direct-process
  reap plus stable active zero. An unavailable, over-bound, or persistently nonzero
  active count remains a retryable cleanup failure.
- Close each CPython 3.10 Windows LSP process handle after direct-process reap and
  all protocol, stderr, and exit-monitor joins. Handle-close failures now retain
  the generation, Job, lease, and `CLEANUP_PENDING` state for idempotent retry;
  retained `Popen` objects keep their cached return code and `poll()` behavior.
- Linearize each LSP generation's expected and observed exits under one lock.
  An unexpected process death recorded before shutdown now remains a terminal
  failure, while shutdown marked before death remains successful; duplicate
  process and protocol callbacks still select exactly one failure intent.
- Publish LSP failure evidence only once after exact durable validation. Public
  `FAILED` state and waiter notification now follow that evidence barrier; failed
  evidence remains retryable with live ownership and a `DEGRADED` public state.
- Treat false Windows directory flushes and failed POSIX directory `fsync` calls as
  incomplete LSP evidence publication. Retries repeat the barrier before terminal
  `FAILED` state, lease removal, or owner release.
- Qualify LSP containment by platform: Windows Job Objects own assigned trees;
  POSIX process groups cover pinned Pyright descendants only while they remain
  in-group, and hostile `setsid()` escape is explicitly unsupported.
- Retain failed LSP evidence and lease temporary-file cleanup as bounded owned
  state. On Windows, the unique name is reserved before creation so post-create
  identity validation remains recoverable. Cleanup retries the exact handle-relative
  name before another publish or terminal finalization, so no hidden temp can release
  the lease or owner.

### v4.0 foundation

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
- **7018 tests** at release (4489 when the v4 platform work landed).

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
