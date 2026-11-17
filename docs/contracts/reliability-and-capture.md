# Reliability and capture contracts

This part of the structure reference moved here from `docs/STRUCTURE.md` so every module
packet does not carry it. It is as binding as that file.

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
If a refused source creation is retried after identical bytes were restored,
the retry durably reasserts those bytes under a hash precondition and links its
committed transaction to the refusal. The original refusal stays in history;
file existence alone does not supply this transaction witness.
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
