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
│   ├── integration_hook_config.py    bounded Cursor/Antigravity hook projections
│   ├── pyright_session.py            Pyright readiness, sync, and semantic provider
│   ├── workspace_revision.py         bounded pre/post freshness proofs
│   ├── code_navigation.py            normalized precise-navigation facade
│   ├── code_navigation_renderer.py   deterministic compact result windows
│   ├── windows_workspace.py          Windows handle-relative filesystem boundary
│   ├── schemas/                      transaction/queue/compile/archive/claim schemas
│   ├── lance_store.py               v4.0: LanceDB embedded vector backend (HNSW)
│   ├── reranker.py                  v4.0: cross-encoder reranker (ONNX)
│   ├── access_tracking.py           explicit telemetry promotion + decay stats
│   ├── retrieval_telemetry.py       private bounded retrieval event cache
│   ├── reflection.py                v4.0: A-MEM page consolidation
│   ├── mcp_server.py                v4.0: MCP server (12 task-shaped tools, stdio)
│   ├── integration_adapter.py       v4.x: thin native lifecycle adapter
│   ├── event_envelope.py            v4.x: shared lifecycle event contract
│   ├── mcp_contract.py              v4.x: uniform MCP response envelope/resources
│   ├── doctor.py                    v4.x: degraded-only health + safe repair
│   ├── repair_installed_memory.py   proposed target: explicit check/apply migration
│   ├── code_graph.py                v4.0: tree-sitter code intelligence
│   ├── impact_analysis.py           v4.0: LINK layer (code→wiki impact)
│   ├── build_tiers.py               v4.0: L0/L1/L2 progressive disclosure
│   └── queries/                     v4.0: 12 tree-sitter .scm language queries
├── tests/                         CODE — full regression suite (pytest)
├── docs/                          CODE — architecture + user guide
├── skills/                        CODE — 9 agent skills (SKILL.md)
├── rules/                         CODE — file-handling policies
├── integrations/                  CODE — IDE/agent integrations
│   ├── cursor/hooks.json             official local user-hook template
│   └── antigravity/hooks.json        official local user-hook template
├── benchmark/                     CODE — benchmark suite + report
│
├── knowledge/                     KNOWLEDGE — content (gitignored: personal)
│   ├── daily/                       append-only session logs
│   │   ├── receipts/                v2 current; immutable v3 proposed target
│   │   └── archive/YYYY-MM/bag-…/   immutable uncompressed BagIt packages
│   ├── notes/                       durable OKF pages (flat slugs)
│   ├── projects/<slug>/             state.md projection + append-only journal.md
│   ├── raw/                         immutable sources
│   ├── inbox/                       unprocessed staging
│   └── feedback/                    correction candidates
│
├── cache/                        RUNTIME — gitignored (FTS5/vector/graph/LanceDB)
│   ├── evidence-graph/              immutable corpus-generation layout
│   │   ├── catalog.sqlite3            active-generation catalog
│   │   ├── telemetry.sqlite3          private cross-generation telemetry
│   │   └── generations/<generation-id>/ immutable after activation
│   │       ├── manifest.json
│   │       ├── source-manifest.json
│   │       ├── incremental-manifest.json optional reuse/invalidation record
│   │       ├── evidence.sqlite3
│   │       ├── search.sqlite3
│   │       ├── vectors.npy             optional
│   │       └── vectors.json            optional
│   ├── lancedb/                     v4.0: LanceDB vector store (optional, --extra hybrid)
│   ├── models/                      v4.0: ML model cache (reranker, embeddings)
│   ├── compile/                     validated content-addressed compile plans
│   ├── claims.sqlite3               derived claim candidate index
│   ├── code-tools/                  managed code-tool artifacts
│   │   └── pyright/1.1.411/           reserved pinned Pyright installation root
│   ├── access_log.jsonl             legacy bounded read-only access history
│   ├── code_tools.json               v4.0: atomic code-tool capability manifest
│   ├── vectors.npy                  v4.0: numpy binary vector cache (memory-mapped)
│   ├── vectors_meta.json            v4.0: vector metadata (paths, titles — no vectors)
│   └── index.sqlite                 FTS5 search index
├── logs/                         RUNTIME — gitignored (lint/compile/hook logs)
├── run/                          RUNTIME — gitignored operational state
│   ├── markdown-transactions.sqlite3 current DB; approved legacy tombstone target
│   ├── markdown-transactions-v3.sqlite3 approved active transaction/owner DB
│   ├── markdown-transactions-v2-retired.sqlite3 approved upgrade evidence
│   ├── transactions/<id>/           before/after images, plans, proposed abort receipt
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
│   ├── queue/                        legacy migration input only
│   ├── queue-migrated-v2             migration completion marker
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
| `$MEMORY_LLM_PROVIDER` | Auto-detected (`opencode` → `codex` → `claude` → `openai` → `ollama`) | LLM backend for compile/flush/query. `fake` for tests. |
| `$LLM_WIKI_DLP_POLICY` | Unset | Optional absolute path to an external bounded-literal/fingerprint policy. Invalid or digest-mismatched required policy fails closed. |

## External integration configuration preimages

Claude, Codex, Cursor, and Antigravity configuration merges may create byte-exact
sibling preimages outside the vault zones. Claude uses
`settings.json.bak-llm-wiki-<YYYYMMDD-HHMMSS-ffffff>` beside `settings.json`; Codex
uses `hooks.json.bak-llm-wiki-<YYYYMMDD-HHMMSS-ffffff>` beside `hooks.json`. Cursor
and Antigravity use the same exact `hooks.json` sibling prefix at their official user
configuration paths. A no-op merge creates no backup.

Only files with the destination's exact `.bak-llm-wiki-` prefix are owned by this
retention contract. After changed configuration is published and verified, each
integration retains at most 10 backups, no backup older than 90 days when a newer
restore point exists, and at most 100 MiB in aggregate when older files can be
removed. The newest verified or sole preimage is never deleted. These files preserve
bytes only, not owner, ACL, alternate streams, or complete filesystem metadata. They
are not runtime state and do not replace private-vault backup/restore. See
`knowledge/notes/integration-config-backup-retention-decision.md`.

## Approved audit-closure boundary

The user approved the security, recovery, install, scheduling, coordination, and
evidence contract on 2026-08-15. The only new runtime directory is `run/install/`.
It owns resumable install state, exact-release and external-path manifests, verified
preimages, and non-secret scheduler definitions. It does not contain backup passwords
or provider credentials. Restic receives credentials through its standard external
password command or protected password file.

Cognee is retired from the supported product. The optional package extra, sync script,
and setup path are removed during implementation. Existing `cache/cognee/` content is
a disposable legacy cache: no supported reader depends on it, and no installer,
repair, or migration deletes it automatically.

First-party model calls share one fail-closed DLP boundary. Optional custom literals
and fingerprint allowlists are loaded only from the absolute external path named by
`LLM_WIKI_DLP_POLICY`; the policy is not a fourth root zone. Verified local-only mode
accepts only literal-loopback Ollama and requires verifiable Ollama cloud disablement.
The implemented strict path uses the existing `MEMORY_LLM_PROVIDER=ollama` override,
the official `OLLAMA_NO_CLOUD=1` server setting, and an explicit `127.0.0.1` or `::1`
endpoint. It disables provider fallback and rejects remote model metadata, but reports
`external_runtime_unverified` because LLM Wiki does not own or inspect the running
Ollama process and therefore cannot prove that it restarted with cloud disabled.

Private-vault backup is a coherent application snapshot, not a direct copy of live
SQLite files. The existing maintenance admission fence blocks cooperating writers;
SQLite online backup, source membership/hash recapture, and a manifest-bound staged
projection create the Restic input. Unknown or ambiguous owners, source races,
integrity failures, and schema mismatches block backup. Restore validates into an
empty staging target before publication and never guesses historical ownership.
The implemented CLI in `scripts/private_vault_backup.py` requires exact Restic
`0.19.1`, an external repository-file containing only the repository location, and
pre-existing empty staging/restore directories. Credentials remain in Restic's
external environment, password file, or password command. Backup returns an exact
snapshot ID plus manifest digest; restore requires both, runs `restic check`, and
keeps a validated `vault/` + `state/` image only on success. It does not publish over
an installed vault. Restic repositories must be outside the vault and staging tree.

Windows Task Scheduler remains the native Windows scheduler. macOS uses a per-user
LaunchAgent and Linux uses a per-user systemd timer; cron is explicit degraded
fallback only. Blackboard tables reuse `markdown-transactions-v3.sqlite3`, capture
reuses Queue v3 intents/terminal proof, and the active operational database count
remains two. No daemon, MCP tool, runtime root, or automatic Git operation is added.
Blackboard adds only `blackboard_claim_epochs` and `blackboard_claims` to the exact
coordinator-v3 schema. They provide bounded all-or-none resource claims, renewable
logical leases, expiry/reclaim, and monotonic fencing; authoritative task, conflict,
and resolution events remain append-only Markdown. Installed databases require the
existing explicit offline re-adoption path before clients accept the changed schema
digest. See `knowledge/notes/audit-closure-security-recovery-control-plane-decision.md`
and `knowledge/notes/blackboard-fenced-resource-claims-decision.md`.

### Install ownership state

The first install-control slice is defined by
`knowledge/notes/install-ownership-control-plane-decision.md`. The approved managed
IDE-hook extension is defined by
`knowledge/notes/managed-ide-hooks-install-update-decision.md`. Version 1 records
remain readable; validated installs adopt canonical `install-manifest/v2` and
`install-transaction/v2` for resumable resource-set updates and one retained committed
update rollback. No `complete.json`, install database, daemon, MCP tool, or
force-adoption path exists.

The manifest owns recognized LLM-Wiki profile fragments, Windows user root variables,
native scheduler resources, an explicitly selected cron block, and bounded structural
fragments in the official Cursor and Antigravity user hook files. It records exact
source identity and digests but does not claim that a dirty local checkout is an
immutable release. Other agent configuration, Git push protection, code upgrade, full
release inventory, and restored-vault publication remain separate follow-up scopes.

Transactions move through `prepared`, `mutating`, `publishing`, and `committed`.
Failure recovery uses `reverting` and `reverted`; malformed state, ambiguous ownership,
or external drift uses `quarantined`. A durable prepared transaction and all required
owned-fragment preimages precede external mutation. Update leaves the prior manifest
active until target verification and publishes generation +1. The original projection
is retained for uninstall; the prior installed projection supports only the latest
committed-update rollback. Restoration requires the exact expected installed value and
never overwrites a concurrent user edit.

`manifest.json`, `transaction.json`, preimages, and scheduler definitions are bounded,
digest-verified, and durably published on the supported local-filesystem boundary.
Whole profiles, whole crontabs, whole user hook files, unrelated task definitions,
provider credentials, and backup passwords are never copied into `run/install/`. An active manifest,
nonterminal transaction, quarantine, or unreadable install state blocks the offline
`run/` deletion snapshot.

## Approved Reliability v3 implementation scope

The user approved the repair direction and delegated the exact architecture decisions
on 2026-08-05, explicitly approved implementation of the operational database pair
and offline adoption backend on 2026-08-12, and approved durable capture producer
activation on 2026-08-16. This scope keeps the three root zones and existing runtime
environment variables.
Remote installer bootstrap adds mandatory full-OID input `LLM_WIKI_COMMIT`. The only
new runtime directory is `run/capture-intents/`. New create-only
`capture-intent/v1` records remain there until an immutable terminal record under
existing `run/queue-results/` proves committed Markdown, validated no-durable-content,
or explicit operator discard. Queue enqueue alone never permits intent deletion.
Provider-derived `capture-decision/v1` records are published in `queue-results/`
before side effects and reused after crashes.
Legacy `cache/transient-transcripts/` files are recovery input only; new capture work
is not written to disposable cache.

For supported SessionEnd and PreCompact evidence, `integration_adapter.py` is the one
synchronous producer boundary. It must publish bounded canonical redacted intent
evidence before returning. Detached execution may wake processing only: spawn success,
a process ID, or queue insertion is not ownership transfer and never authorizes source
deletion. Events without transcript evidence make no successful capture claim and do
not fabricate no-durable-content terminal outcomes. Recovery checks terminal record,
decision record, and deterministic transaction adoption before any provider retry.
Exact replay uses stable source event identity plus complete redacted-input digest;
an identity collision with different bytes fails closed.

Compile authority is `compile-receipt/v3`. V3 receipt filenames are
`knowledge/daily/receipts/v3-<source-identity-sha256>.md`; source identity hashes
canonical logical path plus content digest. Every receipt binds a sorted batch
manifest and one validated disposition to each source. Historical v2 digest-only
receipts remain readable as evidence but cannot authorize automatic skip or archive
under the path-bound v3 contract.

`queue-task/v3` describes production serialization. `input_hash` is SHA-256 over
the exact canonical stored payload. It is recomputed before every insertion, lease,
execution, terminal, operator, migration, or deletion transition. A dedupe key aliases
only the exact same kind, handler version, and payload hash.

Capture, project/Markdown writers, queue workers, compilers, Doctor, nightly, weekly,
and LSP use `maintenance_owners` in `markdown-transactions-v3.sqlite3` as the
canonical admission registry. Queue workers project the same token and epoch into
`queue_ownership` in `queue-v3.sqlite3`; the active database count remains two.
Expiry permits takeover only with positive process-death proof; unknown liveness
blocks.
Legacy `compile.pid` and `maintenance.lock` remain compatibility evidence and
deletion blockers until a separately approved installed-vault migration removes
them. Explicit offline repair retains the exact v2 database bytes, publishes two v3
replacements, and puts immutable JSON tombstones at the legacy active paths. Partial
adoption disables v3 mutation and requires the vault to remain offline. After complete
adoption, known v2 queue and transaction clients cannot open active v3 state. Doctor
reports a protected quiescent snapshot only after complete adoption; before that it
reports an unconditional legacy-protocol blocker. The snapshot is not a durable deletion permit;
`run/` deletion remains an offline operator action.

Operational migrations execute individual statements under explicit transactions,
verify their complete invariant on every startup, and remain restartable after any
statement. Operational databases remain rollback-journal, `synchronous=FULL`, local
filesystem only, and no WAL. The listed v3 paths remain unavailable to normal runtime
mutation until offline adoption, producer, replay, terminal, recovery, purge, and
complexity verification pass. See
`knowledge/notes/v4-reliability-contracts-decision.md` and
`knowledge/notes/reliability-v3-runtime-adoption-implementation-decision.md` and
`knowledge/notes/durable-capture-producer-activation-decision.md` and
`docs/superpowers/specs/2026-08-05-v4-reliability-repair-design.md`.

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

Publication is complete or absent. The builder writes and fsyncs every required
artifact, validates canonical manifests, repository scope, artifact hashes, SQLite
integrity, graph evidence spans, FTS content, and the final directory seal, then
registers the candidate and compare-and-swap activates it in the catalog. A partial,
stale, raced, timed-out, or changed candidate is never published as active. The
previous active generation stays readable. The catalog selects one active
generation, can register complete orphans without activating them, and repairs a
corrupt pointer only to a revalidated prior generation in activation history or
parent lineage.

`doctor.run_generation_maintenance()` is the shared bounded, fenced refresh path.
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
`corpus-generation/v2` with `evidence-graph/v2`. Foundation Tasks 1-5 of the
2026-07-21 Plan A remain implemented, including explicit Graph v3 selection contracts
and bounded sealed-workspace utilities, but its one-shot consent/SCIP/publication
Tasks 6-16 are superseded.

The replacement plan implements path derivation, position and URI conversion,
bounded protocol transport, process startup evidence, platform-qualified lifecycle
ownership, repository containment, safe diagnostic redaction, pinned profile
discovery and installation, capability-honest provider requests, document
synchronization, session-manager capacity, the normalized navigation facade,
deterministic rendering, MCP routing, doctor diagnostics, and qualification gates.

The implemented Python/Pyright slice keeps the existing structural Evidence Graph
and adds a Python 3.10-compatible, read-only LSP runtime owned by LLM Wiki. It serves
precise live navigation through modes of the existing 12 task-shaped MCP tools. It
adds no Serena runtime dependency, Rust rewrite, second graph, catalog, active
pointer, runtime root, persistent daemon, semantic result cache, or MCP tool.
Query-time LSP observations are not written into an active generation.

The runtime starts Pyright lazily within the owning MCP process, exposes only
allowlisted read operations, reports readiness and capability limitations, and falls
back to existing structural evidence when unavailable. Exact small results use a
deterministic compact renderer; the Context Compiler remains responsible for broad
multi-source synthesis. Pyright installation is a separate explicit operator action.
See
`knowledge/notes/read-only-lsp-navigation-engine-decision.md` and
`docs/superpowers/specs/2026-07-22-read-only-lsp-navigation-design.md`.

The approved managed Pyright artifact path is
`cache/code-tools/pyright/1.1.411/`. Live LSP process scratch is bounded under
`run/lsp/<owner-nonce>/`; doctor and deletion eligibility must treat a live owner
or retained failure evidence as protected operational state.

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
process group owns the pinned Pyright server and descendants only while they remain
in that group. A hostile descendant can call `setsid()` and escape; containing that
case is unsupported. The POSIX runtime is therefore limited to qualified Pyright in
trusted repositories and does not use a `/proc` or `ps` ancestry scan to claim
stronger ownership. Optional delegated cgroup v2 containment is a future Linux-only
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

## What lives where

### CODE zone (tracked in git)
- `scripts/` — Python pipeline and host helpers. Central hub:
  `memory_state.py` (path/lock/state), `compile_memory.py` (LLM compile +
  VERIFY-BEFORE-WRITE), `flush_memory.py` (3-tier classification),
  `maybe_compile.py` (PID-locked spawn), `search_memory.py` (triple-RRF),
  `llm_client.py` (5 backends + fake), `integration_adapter.py` (thin host
  lifecycle boundary), `mcp_server.py` (12 task-shaped tools), and `doctor.py`.
- `tests/` — full regression suite. Hermetic via `conftest.py` (pins
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

### KNOWLEDGE zone (tracked: public fixtures; gitignored: personal)
- `knowledge/daily/` — append-only `YYYY-MM-DD.md`. Private (gitignored).
  Public synthetic fixtures (`2026-04-13.md`, `2026-04-19.md`) are
  un-ignored to restore Evidence links.
- `knowledge/daily/receipts/` — authoritative immutable Markdown compile receipts.
  Current v2 is keyed by source digest. The proposed v3 target above adds logical
  path identity and commits one source receipt with compile output; v2 then remains
  historical evidence only.
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
- `cache/` — `index.sqlite` (FTS5), `vectors.npy` (binary numpy, mmap),
  `vectors_meta.json` (metadata),
  `code_tools.json` (fresh code-tool detection and active semantic capabilities).
  `cache/code-tools/pyright/1.1.411/` is the managed Pyright artifact root;
  `scripts/install_pyright.py` is the only supported download/publish path and
  `scripts/lsp_paths.py` derives it without directory creation.
  v4.0: `lancedb/` (LanceDB vector store, optional), `models/` (ML model cache),
  legacy bounded read-only `access_log.jsonl`, `cache/compile/` (validated compile-plan
  action cache), and `cache/claims.sqlite3` (derived claim index).
- `cache/evidence-graph/` — disposable derived graph, FTS, vector, tier, and
  telemetry generation state. The implemented v2 layout is:

```text
cache/evidence-graph/catalog.sqlite3
cache/evidence-graph/telemetry.sqlite3
cache/evidence-graph/generations/<generation-id>/
├── manifest.json
├── source-manifest.json
├── incremental-manifest.json    optional; present for incremental builds
├── evidence.sqlite3
├── search.sqlite3
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
  access counters, making retries idempotent. Legacy `access_log.jsonl` is stats-only
  history and is never promoted automatically. Telemetry sits beside the catalog and
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
- Legacy `cache/index.sqlite`, `cache/vectors.npy`, `cache/vectors_meta.json`, and
  `cache/lancedb/` remain readable during migration. They are disposable derived
  caches retained as fallback, not members of a generation. They must not be removed
  until installed-vault migration evidence makes that safe. The new reader switches
  only after a validated generation is active.

### Evidence-cache migration and rollback

There is no automatic legacy-cache deletion and no supported end-user migration CLI
yet. Generation refresh is integrated with `doctor.run_generation_maintenance()`
and nightly maintenance. Migration therefore preserves both layouts:

1. Keep authoritative `knowledge/`, Git history, and project journals unchanged.
2. Keep all four legacy cache paths while a candidate generation is built and
   validated.
3. Switch readers only through catalog CAS activation.
4. Verify returned generation/fallback fields before treating migration as complete.
5. Retain legacy caches until installed-vault evidence authorizes their removal.

For safe rollback, stop active commands and remove only the derived
`cache/evidence-graph/` tree, or reactivate a previously validated generation through
the catalog API. Do not delete `knowledge/`, project journals, Git data, or `run/`.
With legacy caches retained, readers fall back to legacy FTS/vector/Lance paths;
graph-dependent code tools use bounded live extraction and label it incomplete.
- `logs/` — `lint-YYYY-MM-DD.md`, `compile-last.log`, `session-start-last.txt`,
  `capture-failures.jsonl` (bounded trail of lost prompt/post-tool captures),
  and `logs/maintenance/` (owner-only `*.out.log` / `*.err.log` artifacts holding
  the full output of each nightly and weekly step). Both are disposable and
  bounded: the trail by size, the artifacts by age, count, and total size.
- `run/` — `state.json`, `compile.pid`, `run/markdown-transactions.sqlite3`,
  `run/transactions/`, `run/queue.sqlite3`, `run/queue-results/`, receipts, and
  locks. The proposed target adds `run/capture-intents/`, two active `*-v3.sqlite3`
  files, retained `*-v2-retired.sqlite3` upgrade evidence, legacy-path JSON
  tombstones, migration/adoption evidence, and explicit compatibility treatment for
  `maintenance.lock`. `run/lsp/<owner-nonce>/`
  holds bounded live process scratch created by the
  owning LSP lifecycle. Its `lease.json` is a bounded mutable live
  lease with a 10 seconds heartbeat and 30 seconds expiry, separate from immutable
  `owner.json` and `failure.json`. Existing `run/queue/*.json` is one-time
  migration input only. The approved audit-closure target adds `run/install/` for
  manifest-owned install, rollback, scheduler, and external-preimage state.
- `cache/cognee/` — retired disposable legacy cache. It has no supported reader and
  is never removed automatically.

**Runtime deletion contract.** `cache/` and `logs/` are regenerated on demand.
The current `run/` contains recoverable but operationally significant transactions
and queued work. Delete it only after `doctor` reports no nonterminal, conflicted, or
quarantined transaction, no transaction inside the 30-day undo window, and no
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
forfeits their replay suppression. Proposed Doctor acquires an admission gate while
checking but reports only a quiescent snapshot; it does not authorize a later
concurrent deletion. Deletion remains an offline operator action.

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
