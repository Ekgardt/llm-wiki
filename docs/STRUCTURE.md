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
│   ├── integration_adapter.py       v4.x: thin native lifecycle adapter
│   ├── event_envelope.py            v4.x: shared lifecycle event contract
│   ├── mcp_contract.py              v4.x: uniform MCP response envelope/resources
│   ├── doctor.py                    v4.x: degraded-only health + safe repair
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
runs on the defaults, which are the constants the registry replaced. Precedence is
default < file < `LLM_WIKI_<SECTION>_<KEY>`. An unknown section or key, a non-integer
or a value below its bound stops the caller naming the key and its source; nothing
falls back silently. `doctor` has a `settings` check: it lists every value that
differs from its default with its source, errors on an invalid file, and warns at 80 %
of a vault-size ceiling. The first registered limits are the vault-size ceilings
(`index`, `compile`, `corpus`, `claims`, `extraction`, `search`, `impact`); every
refusal at one names the setting that raises it. Decision and sources:
`docs/research/2026-09-27-every-limit-states-its-reason.md`; inventory:
`docs/LIMITS-2026-09-27.md`.

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

## External integration configuration preimages

Claude and Codex configuration merges may create byte-exact sibling preimages
outside the vault zones. Claude uses
`settings.json.bak-llm-wiki-<YYYYMMDD-HHMMSS-ffffff>` beside `settings.json`; Codex
uses `hooks.json.bak-llm-wiki-<YYYYMMDD-HHMMSS-ffffff>` beside `hooks.json`. The same
prefix applies to a retired Cursor or Antigravity fragment while `uninstall` takes it
back. A no-op merge creates no backup.

Only files with the destination's exact `.bak-llm-wiki-` prefix are owned by this
retention contract. After changed configuration is published and verified, each
integration retains no backup older than the configured retention age (90 days by default) when a newer
restore point exists, and at most 100 MiB in aggregate when older files can be
removed. The newest verified or sole preimage is never deleted. These files preserve
bytes only, not owner, ACL, alternate streams, or complete filesystem metadata. They
are not runtime state and do not replace private-vault backup/restore. See
`knowledge/notes/integration-config-backup-retention-decision.md`. The redundant ten-copy cap was removed on 2026-09-30; see `docs/research/2026-09-30-backup-retention-uses-age-and-bytes.md`.

## Approved audit-closure boundary

The user approved the security, recovery, install, scheduling, coordination, and
evidence contract on 2026-08-15. The only new runtime directory is `run/install/`.
It owns resumable install state, exact-release and external-path manifests, verified
preimages, and non-secret scheduler definitions. It does not contain backup passwords
or provider credentials. Restic receives credentials through its standard external
password command or protected password file.

Cognee is retired from the supported product. The optional package extra, sync script,
and setup path were removed. Existing `cache/cognee/` content is
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

The image carries what Git does not (decided 2026-09-17): the knowledge, the runtime
half, untracked files, and tracked files modified since `HEAD`. Paths git holds
identically are skipped, as are `cache/`, `logs/`, `run/`, `.git/`, `.venv/` and
regenerable tool caches (`__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`,
`node_modules`); directories left holding nothing are not part of the image. Where git
cannot answer, nothing is skipped on git's word. This is what lets `publish` — which
overwrites nothing — land in a fresh clone. See
`docs/research/2026-09-17-a-backup-image-carries-what-git-does-not.md`.

Windows Task Scheduler remains the native Windows scheduler. macOS uses a per-user
LaunchAgent and Linux uses a per-user systemd timer; cron is explicit degraded
fallback only. Blackboard tables reuse `markdown-transactions-v3.sqlite3`, capture
reuses Queue v3 intents/terminal proof, and the active operational database count
remains two. No daemon, MCP tool or runtime root is added, and no automatic Git
operation beyond the nightly fast-forward of the default branch.
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
fragments in the Claude Code and Codex user configuration files. A fragment written by
an install before 2026-08-26 in a retired host's file stays removable. It records exact
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

Files the installer owns whole -- the systemd units, the launchd plists, and the OpenCode
plugin -- are replaced by every install or update even when they were changed outside
it (2026-09-28). The changed version becomes the transaction's rollback point, and
`install_takeover.py` first keeps a readable copy under
`run/install/displaced/<time>-<resource-id>/` and reports what the new version does not
carry. A line added to a systemd unit moves to `<unit>.d/50-local.conf`; a line that
replaced one of ours with the same key is reported instead, because in a drop-in it would
add to ours, and an existing `50-local.conf` is never planned or touched. The drop-in is
a released resource of that update's transaction (`systemd_drop_in`): written after the
units, each write reloading systemd, reverted with the transaction on failure, kept out
of the manifest, and removed only by the `rollback` of that same generation, first, before
the unit is restored. launchd has no drop-ins, so its copy and the report keep the edit.
Copies older than `retention.config_backup_days` go on the next install unless their
digest is referenced by the active manifest or transaction. Shared files (the profile block, the cron block, the Claude and
Codex hook files, Windows variables and tasks) keep the fragment rules: a change outside
the installer is refused with the resource, its location, and `--adopt <resource-id>`
where it can be adopted. A transaction matches resources by id, kind and location, so a
rerun that drops resources or moves one (a profile in another file) is one update: the
new set is written and verified first, what it no longer owns goes back to its origin
last, and a failure reverts both; `rollback` restores the previous set first and takes
the new one back after. A set change that involves a cron entry or a Windows task, which
cannot be read back, is still taken back before its replacement and restored if that
fails; the installer says so, and a refusal names the entry and the uninstall command.
See `docs/research/2026-09-28-an-update-replaces-what-it-owns.md` and
`docs/research/2026-09-28-what-an-update-created-its-rollback-takes-back.md`.

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
Remote installer bootstrap adds mandatory full-OID input `LLM_WIKI_COMMIT`; the verified
commit becomes local `main` tracking `origin/main`, so the nightly fast-forward applies. The only
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

### Durable breadcrumb repair contract (2026-09-29; installed 2026-09-30)

For this repair the owner delegated decisions to the agent under the nine mandatory
development laws. The selected contract is recorded privately in
`knowledge/notes/durable-breadcrumb-delivery-decision.md`; research and qualification
requirements are in `docs/research/2026-09-29-breadcrumb-durability-proposal.md`.
The storage, deterministic worker, terminal/purge proof and recovery components
are implemented. Host adapters and compatibility hook scripts publish through the
common durable ingress before follow-ups. The running vault adopted these producers
on 2026-09-30 under the existing quiescence fence, after the full candidate suite
passed. A real Codex tool event passed complete terminal/source/journal verification.
The qualified retrieval and answer-cost evidence, and its limits, are recorded in
`docs/research/2026-09-30-durable-capture-installation.md`.

Prompt/tool events use distinct versioned manifests and integrity-linked parts
inside the existing `run/capture-intents/` layout. The manifest becomes ready only
after complete durable publication. Existing v1 session readers remain supported.
The worker processes breadcrumbs deterministically without a model. Before terminal
completion, it publishes the complete redacted event as immutable linked raw-source
Markdown inside the existing private `knowledge/raw/sessions/<date>/` tree and
commits the journal entry for the original occurrence day. A short journal reference
cannot authorize deletion of the only complete copy in runtime. Terminal proof and
purge validation bind permanent evidence, the input and the journal transaction.
Parts obey existing encoded-record and Markdown-reader contracts; no new logical
event-size or part-count ceiling is introduced. Readers, recovery, diagnostics and
cleanup must be qualified before producer cutover under the existing maintenance
fence. No new runtime root, database, daemon, MCP tool or dependency is introduced.

Both worker and nightly recovery discover complete pending manifests before
database indexing and preserve supported v1 session dispatch. Indexed recovery
advances past failed rows without a failed-row prefix cap. Read-only diagnostics
verify full linked evidence, distinguish complete pending from incomplete
publication, and report inaccessible directories. Inspection observes its caller
deadline and releases each SQLite metadata read before checking source files.
The replaced direct-append and state-based content/time suppression functions are
removed from the installed product, including the now-unused capture_operation module.
The two hook script entrypoints and historical --background/--delegate arguments
remain compatible because existing installed host configurations still name them.
Supported v1 session readers remain necessary for retained session records/tasks.

Only complete verified breadcrumb heads and their integrity-linked parts join the
existing session source kind in the corpus; ordinary session dumps and orphan
parts remain excluded. Search and its no-generation fallback return physical
chunks with verifiable source hashes and byte spans. Reranker admission uses
semantic scores when available; provenance still weighs the final model score.
Mixed primary/event pools use sequential scoring with separate cost observations
and the same semaphore and caller deadline. No output quota or larger default
request budget is introduced. Three paired answer tasks reached all new facts in
one call each; a separately tested smaller caller budget retained those answers.
This small fixture does not qualify a globally smaller answer window or prove
billed token savings.

Historical path-bound compile authority is `compile-receipt/v3`. V3 receipt filenames are
`knowledge/daily/receipts/v3-<source-identity-sha256>.md`; source identity hashes
canonical logical path plus content digest. Every receipt binds a sorted batch
manifest and one validated disposition to each source. Historical v2 digest-only
receipts remain readable as evidence but cannot authorize automatic skip or archive
under the path-bound v3 contract.

Installed source-context receipt contract (approved 2026-10-03, installed
2026-10-04): `compile-receipt/v4` additionally binds the
original daily SHA-256, its byte count, and the absolute selected part bounds.
The v4 source identity hashes those context fields with logical path and part
digest; `knowledge/daily/receipts/v4-<source-identity-sha256>.md` remains create-only.
V3 schema, identity, filenames and retained historical authority stay unchanged.
A context-aware skip verifies committed authority and the exact saved-length
prefix of the current daily. Benign appends preserve unchanged-part completion;
changed prefixes and legacy records without context are explicitly unverified.
Citation syntax and physical block/span validation remain unchanged. Archive
and Doctor readers dispatch by receipt version; old consumers are accounted
for before writer activation or source retirement. No env contract, runtime
root, database, daemon or MCP tool is added. See the approved proposal in
`docs/research/2026-10-03-source-context-receipt-proposal.md` and the owner's
private `knowledge/notes/compile-source-context-receipt-decision.md` and
`knowledge/notes/compile-source-context-receipt-installation-decision.md`.
Installation qualification does not establish completion of the later native
capture, full nightly, or complete resource-limit audit steps.

`queue-task/v3` describes production serialization. `input_hash` is SHA-256 over
the exact canonical stored payload. It is recomputed before every insertion, lease,
execution, terminal, operator, migration, or deletion transition. A dedupe key aliases
only the exact same kind, handler version, and payload hash.

Capture, project/Markdown writers, queue workers, compilers, Doctor, nightly, weekly,
and LSP use `maintenance_owners` in `markdown-transactions-v3.sqlite3` as the
canonical admission registry. Queue workers project the same token and epoch into
`queue_ownership` in `queue-v3.sqlite3`; the active database count remains two.
Normal admission verifies immutable adoption evidence, both active file identities,
complete schemas, and operational connection settings in read snapshots; it does not
certify every retained transaction before each event. Mutation paths still verify
record hashes, leases and fencing epochs. Complete adoption/doctor/candidate/backup
certification retains whole-file integrity and foreign-key checks and the coordinator's
operation and cross-table invariants. The admission cache tracks both database files;
a replacement invalidates the cached verdict. No directory or database is added.
See `docs/research/2026-10-02-capture-admission-is-not-history-certification.md`.
Expiry permits takeover only with positive process-death proof; unknown liveness
blocks.
Since 2026-09-10 the nightly and weekly passes take the `nightly`/`weekly`
lease through the adopted coordinator's registry (`acquire_scheduled_owner`),
refresh it with a heartbeat, stop between steps when the fence is lost, and
record that loss instead of success; `run/maintenance.lock` left by a dead
owner is reclaimed only with the registry's proof, never by age
(`knowledge/notes/nightly-takes-the-canonical-fence-decision.md`). On a vault
without a V3 coordinator the legacy PID marker remains the only fence, and
`compile.pid` stays the compile's legacy lock: both remain compatibility
evidence and deletion blockers until a separately approved installed-vault
migration removes them. Explicit offline repair retains the exact v2 database bytes, publishes two v3
replacements, and puts immutable JSON tombstones at the legacy active paths. Partial
adoption disables v3 mutation and requires the vault to remain offline. After complete
adoption, known v2 queue and transaction clients cannot open active v3 state. Doctor
reports a protected quiescent snapshot only after complete adoption; a normal doctor
owner admits capture while a before/after comparison of canonical admission epochs and
physical database identity rejects intervening activity. Before complete adoption it
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
and adds a Python 3.10-compatible, read-only LSP runtime owned by LLM Wiki. It serves
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

### Evidence-cache migration and rollback

The generation is the only index since 2026-09-23: the four legacy cache paths are
read by nothing and may be deleted, and nothing deletes them automatically.
Generation refresh is integrated with `doctor.run_generation_maintenance()`, the
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
See [health/capture qualification](research/2026-09-30-health-observation-does-not-refuse-capture.md).

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
