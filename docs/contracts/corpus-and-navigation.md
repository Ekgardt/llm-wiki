# Corpus and code navigation contracts

This part of the structure reference moved here from `docs/STRUCTURE.md` so every module
packet does not carry it. It is as binding as that file.

## Implemented corpus-generation checkpoint

The current checkpoint implements one complete `corpus-generation/v2` with
`evidence-graph/v2` for one
repository checkout or worktree. `repository_scope` is a closed
`repository-scope/v1` object containing the repository ID, checkout ID, canonical
checkout root, Git common directory, and captured commit. Repository identity is
shared by linked worktrees; checkout identity remains specific to the worktree.
Readers requesting repository-scoped evidence accept only an active or validated
fallback generation with the exact same scope. A scope mismatch returns no
generation rather than reading another checkout's evidence.

Every v2 generation contains `manifest.json`, canonical `source-manifest.json`,
`evidence.sqlite3`, and `search.sqlite3`. Incremental builds also contain canonical
`incremental-manifest.json`, which records source deltas, ownership, dependency and
workspace invalidation metadata, and exact reuse configuration. Optional vectors
remain an all-or-nothing pair. `source-manifest.json` binds the captured source
membership, hashes, collection policy, and collector/extractor identity. The
Evidence Graph and FTS artifacts are both built from that exact immutable
`CorpusSnapshot`; live membership and hashes are recaptured immediately before
publication.

The owner approved sequential derived-generation processing on 2026-10-04.
The implementation is pending qualification. The internal chunk sequence retains
its length, stable order, indexed access, fields, hashes and physical spans while
deriving chunks from the captured immutable sources without retaining the complete
object corpus. FTS writing and validation use successive rows; vector construction
uses the existing temporary `.npy` artifact through memory mapping, with successive
metadata processing. Consumers must not rematerialize the complete chunk corpus.
This changes no persistent database, directory, runtime root, environment contract,
generation schema, dependency or MCP tool. Source rechecks, fenced ownership,
deadlines, artifact integrity and complete-or-absent activation remain mandatory.
The unsupported global chunk ceiling is replaced only after memory, compatibility,
failure and real-corpus qualification; this approval does not remove unrelated
resource contracts. See the private `streamed-derived-generations-decision.md`.

`search.sqlite3` also carries the **ledger of things and events** (approved
2026-09-22): one table, `ledger`, whose rows are posted by the nightly fact-keys
call into `cache/fact-keys/keys.sqlite3` — kind, canonical thing, event, day,
quantity, whether the user said it and dated it, and the pointer to the source
bytes — and copied into the artifact at build time exactly as the keys are. Each
row is posted once under a digest of its fields and pointer; two rows of one thing
within 30 days are one event unless a stated quantity contradicts; a count is made
by code over every row (`scripts/ledger.py: count`, `reconcile`), never by a model
over retrieved chunks, and costs no provider call. The table is sealed by the
artifact's digest, disposable and derived like the rest of the generation; an
artifact built before the table existed carries none and a reader answers "no
ledger", not zero. A thing seen on two or more days opens the recurrence gate: its
entity page under `knowledge/notes/ledger-<kind>-<thing>.md` is created or extended
with dated pointer lines by code, never rewritten by a model. Decision:
`knowledge/notes/ledger-of-things-and-events-decision.md`; research:
`docs/research/2026-09-22-a-ledger-of-things-and-events-posted-once.md`.

Publication is complete or absent. The builder writes and fsyncs every required
artifact, validates canonical manifests, repository scope, artifact hashes, SQLite
integrity, graph evidence spans, FTS content, and the final directory seal, then
registers the candidate and compare-and-swap activates it in the catalog. A partial,
stale, raced, timed-out, or changed candidate is never published as active. The
previous active generation stays readable. The catalog selects one active
generation, can register complete orphans without activating them, and repairs a
corrupt pointer only to a revalidated prior generation in activation history or
parent lineage.

`generation_maintenance.run_generation_maintenance()` is the shared bounded, fenced refresh path.
It resolves repository/worktree scope, captures knowledge and approved workspace
code, performs workspace-level extraction, reuses exact parent records when valid,
and returns `current`, `built`, `deferred`, or `error` without mutating knowledge.
Nightly maintenance invokes this same path and treats only `current` or `built` as
success. This checkpoint does not document a native code-index kernel, multi-repo
portfolio generation, temporal/control-plane services, or an operator console as
implemented.

## Implemented Python code navigation

This section records the implemented Python/Pyright slice. The runtime path helpers are implemented,
while the authoritative corpus checkpoint remains
`corpus-generation/v2` with `evidence-graph/v2`. The whole 2026-07-21 Plan A is
superseded: its one-shot consent/SCIP/publication Tasks 6-16 were superseded by the
replacement plan below, and on 2026-09-18 its foundation — the Graph v3 selection
contracts, the sealed-workspace utilities and the `code_capture` manifest section that
nothing in production ever filled — was removed from the code
(`docs/research/2026-09-18-the-superseded-plan-a-seam-leaves-the-code.md`).

The replacement plan implements path derivation, position and URI conversion,
bounded protocol transport, process startup evidence, platform-qualified lifecycle
ownership, repository containment, safe diagnostic redaction, pinned profile
discovery and installation, capability-honest provider requests, document
synchronization, session-manager capacity, the normalized navigation facade,
deterministic rendering, MCP routing, doctor diagnostics, and qualification gates.

The implemented Python/Pyright slice keeps the existing structural Evidence Graph
and adds a Python 3.14 read-only LSP runtime owned by LLM Wiki. It serves
precise live navigation through modes of the existing 12 task-shaped MCP tools. It
adds no Serena runtime dependency, Rust rewrite, second graph, catalog, active
pointer, runtime root, persistent daemon, semantic result cache, or MCP tool.
Query-time LSP observations are not written into an active generation.

Structural answers read a repository's generation through a process-local reader
cache (`scripts/evidence_reader_cache.py`, 2026-09-10): a generation is validated
once per MCP process and reused while `catalog.sqlite3`, the generation's
`evidence.sqlite3` and the checkout's Git state keep their stat identity. It holds
no state on disk and adds no runtime root. A foreign repository's generation is
refreshed incrementally by `repository_index.py refresh` — spawned detached by the
MCP server once per checkout and commit when a structural answer finds the
checkout's commit ahead of the generation's, and by the nightly `refresh-all`
step — under the ownership registry's `doctor` role scoped
`repository:<repository_id>`. It is not a daemon: the process exits when the
refresh does. Answers carry a `freshness` block naming both commits.

The [reader-cache maintenance contract](../modules/evidence-reader-cache.md) owns
process-local leases, identity reuse and retirement. Graph selection, validation
and refresh remain with their existing callers.

The runtime starts a managed language server lazily within the owning MCP process,
exposes only allowlisted read operations, reports readiness and capability
limitations, and falls back to existing structural evidence when unavailable. The
server is chosen by file suffix from the four managed profiles; a file whose suffix
no profile claims answers `unsupported` before any server is asked. Exact small results
use a deterministic compact renderer; the Context Compiler remains responsible for
broad multi-source synthesis. Installation is a separate explicit operator action
per profile: `scripts/install_pyright.py`, or
`scripts/install_language_server.py --profile <name>`. See
`knowledge/notes/read-only-lsp-navigation-engine-decision.md` and
`docs/superpowers/specs/2026-07-22-read-only-lsp-navigation-design.md`.

The approved managed artifact paths are `cache/code-tools/pyright/1.1.411/`,
`cache/code-tools/typescript-language-server/6.0.0/`,
`cache/code-tools/gopls/v0.23.0/` and `cache/code-tools/rust-analyzer/1.98.1/`.
Live LSP process scratch is bounded under
`run/lsp/<owner-nonce>/`, which also holds the sealed digest-verified copy of a
native server that is launched from it; doctor and deletion eligibility must treat a
live owner or retained failure evidence as protected operational state.

Every startup coordinator enters an eight-entry module registry before its first
owned mutation. Successful startup hands ownership to the instance's existing
normal-exit callback and leaves the registry. A higher-level owner may atomically
adopt `StartupCleanupError`; `PyrightSession` then removes that coordinator from the
bounded registry, retains the error, installs its own normal-exit cleanup, and retries
from `start()` or `close()` under the caller deadline. Unadopted incomplete startups
stay in the module registry. Both paths use the same absolute-deadline cleanup driver.

While lifecycle ownership is live, `run/lsp/<owner-nonce>/lease.json` is a bounded
mutable live lease distinct from immutable create-only `owner.json` and
`failure.json`. It contains only canonical process/nonces, timestamps, schema, and
live-state fields. It is refreshed every 10 seconds and expires after 30 seconds.
Controlled success or terminal failure stops and joins the heartbeat before lease
removal; abrupt death leaves the lease to expire. Updates are atomic, owner-only,
and anchored to the retained owner-directory handle. A Windows replacement retries
only errors 5, 32, and 33 with stop-aware waits bounded by the caller deadline and
the previous lease's monotonic expiry. Evidence and lease publication own at most
one serialized hidden temporary name. On Windows, that name is reserved before the
create call so post-create validation failure remains recoverable; POSIX records it
immediately after atomic creation returns and before validation. Failed temp
deletion keeps the lifecycle in `CLEANUP_PENDING` with its lease and owner, blocking
evidence success, lease removal, scratch deletion, and owner close. The exact
handle-relative name is retried before another publication or terminal finalization.
A successful terminal layout never contains a hidden temp. See
`knowledge/notes/lsp-live-lease-decision.md`.

LSP process containment is platform-qualified rather than one portable sandbox. A
Windows Job Object owns the assigned server tree. On Linux and macOS, a POSIX
process group owns the assigned managed server and its descendants only while they
remain in that group. A hostile descendant can call `setsid()` and escape; containing
that case is unsupported. The POSIX runtime is therefore limited to the qualified
managed servers in trusted repositories and does not use a `/proc` or `ps` ancestry
scan to claim stronger ownership. Optional delegated cgroup v2 containment is a future Linux-only
candidate requiring a separate capability-gated design. See
`knowledge/notes/lsp-process-containment-decision.md`.

Each LSP process generation linearizes expected exits, observed process death, and
one sticky failure-intent selection under a single generation lock. The exit monitor
records normal `wait()` completion before invoking protocol callbacks. Therefore an
unexpected death observed before shutdown remains a failure, an expected shutdown
marked before death remains successful, and duplicate process/protocol callbacks
cannot enqueue multiple generation failures. A second fatal intent handled by the
recovery thread quiesces that thread's tracked role and completes terminal cleanup
without waiting for a caller action. Retained cleanup diagnostics contain at most
one sanitized current record for each fixed cleanup step and never retain raw
exception graphs; successful retries clear the resolved step.

On Windows, a generation retains CPython's direct-process handle until the process
is reaped and its protocol, stderr, and exit-monitor owners are joined. Cleanup
closes that handle before releasing the Job Object. A failed close keeps the
generation, Job, and lease in `CLEANUP_PENDING` for idempotent retry; the retained
`Popen` object continues to expose its cached return code. POSIX process-group
release ordering is unchanged. Windows Job bounds and completion use current
`ActiveProcesses`; lifetime `TotalProcesses` and racing PID-list snapshots are not
compared. PID capture is a best-effort identity aid. A snapshot error is discarded
only after direct-process reap and bounded stable active zero; unknown, over-bound,
or nonzero active state remains fail-closed.

### Evidence-cache migration and rollback

The generation is the only index since 2026-09-23: the four legacy cache paths are
read by nothing and may be deleted, and nothing deletes them automatically.
Generation refresh is integrated with `generation_maintenance.run_generation_maintenance()`, the
nightly pass and every successful command-line compile; `doctor --rebuild-generation`
builds one on demand.

1. Keep authoritative `knowledge/`, Git history, and project journals unchanged.
2. Switch readers only through catalog CAS activation.
3. Verify returned generation/fallback fields.

For safe rollback, stop active commands and remove only the derived
`cache/evidence-graph/` tree, or reactivate a previously validated generation through
the catalog API. Do not delete `knowledge/`, project journals, Git data, or `run/`.
Until a generation is active, memory search reads Markdown directly and says
`no_active_generation`; graph-dependent code tools use bounded live extraction and
label it incomplete.
- `logs/` — `lint-YYYY-MM-DD.md`, `compile-last.log`, `session-start-last.txt`,
  `capture-failures.jsonl` (bounded trail of lost prompt/post-tool captures),
  and `logs/maintenance/` (owner-only `*.out.log` / `*.err.log` artifacts holding
  the full output of each nightly and weekly step). Both are disposable and
  bounded: the trail by size, the artifacts by age, count, and total size.
- `run/` — `state.json`, `compile.pid`, `run/markdown-transactions.sqlite3`,
  `run/transactions/`, `run/queue.sqlite3`, `run/queue-results/`, receipts, and
  locks. The adopted v3 layout adds `run/capture-intents/`, two active `*-v3.sqlite3`
  files, retained `*-v2-retired.sqlite3` upgrade evidence, legacy-path JSON
  tombstones, migration/adoption evidence, and explicit compatibility treatment for
  `maintenance.lock`. `run/lsp/<owner-nonce>/`
  holds bounded live process scratch created by the
  owning LSP lifecycle. Its `lease.json` is a bounded mutable live
  lease with a 10 seconds heartbeat and 30 seconds expiry, separate from immutable
  `owner.json` and `failure.json`. Existing `run/queue/*.json` is refused and
  named, never imported (2026-09-23). The approved audit-closure target adds `run/install/` for
  manifest-owned install, rollback, scheduler, and external-preimage state.
- `cache/cognee/` — retired disposable legacy cache. It has no supported reader and
  is never removed automatically.

**Runtime deletion contract.** `cache/` and `logs/` are regenerated on demand.
The current `run/` contains recoverable but operationally significant transactions
and queued work. Delete it only after `doctor` reports no nonterminal, conflicted, or
quarantined transaction, no transaction inside the 2-day undo window, and no
retained queue task or result, and no live project lease, writer, queue worker, or
maintenance or LSP owner, and no retained LSP failure evidence. Deleting eligible
committed artifacts loses undo history.
Installers and repair commands never remove it silently.

The proposed Reliability v3 target additionally treats every unresolved capture
intent, compiler, compatibility marker, retired database, missing or mismatched
tombstone/migration/adoption evidence, and expired owner without positive death proof
as a blocker. A complete validated
tombstone/adoption set does not independently block otherwise eligible whole-`run/`
deletion. Validated capture terminal records survive ordinary purge but cease to be
independent blockers after 30 days; deleting the whole eligible runtime deliberately
forfeits their replay suppression. Doctor uses the existing shared `doctor` admission
role and renews its lease under the caller deadline. It checks canonical owner epochs
and physical database identity before and after validation, and refuses a quiescent
verdict if another owner appears, the observation changes, ownership is lost or the
deadline expires. It reports only a snapshot and never authorizes a later concurrent
deletion. The exclusive `runtime-deletion-check` role still protects offline actions.
See [health/capture qualification](../research/2026-09-30-health-observation-does-not-refuse-capture.md).
