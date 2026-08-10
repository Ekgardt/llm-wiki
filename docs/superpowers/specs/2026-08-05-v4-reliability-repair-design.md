---
type: decision
title: "V4 Reliability Repair Design"
date: 2026-08-05
confidence: high
source_authority: user
status: approved
---
# V4 Reliability Repair Design

One-sentence summary: LLM Wiki will repair its current reliability boundaries in bounded, versioned layers while preserving Markdown authority, the 12-tool MCP surface, the three-zone layout, and the local no-daemon operating model.

## Status

The user approved the repair direction and selected implementation base on
2026-08-05, then explicitly delegated the exact architecture decisions after review
against current sources. The base is `feature/native-code-kernel` at
`0e13e71ca4aa9b2afafcaf0d7e8ec66f7055fa2b`; implementation uses a separate branch
and worktree. This document is the approved target and does not claim implementation.

The approved architecture delta is:

- add `run/capture-intents/` as the only new runtime directory;
- write immutable `capture-decision/v1`, `capture-terminal/v1`, and corrupt-task
  disposition packages under the existing `run/queue-results/` directory;
- write malformed capture packages under the existing `run/queue-quarantine/`
  directory;
- write `transaction-abort/v1` receipts under existing
  `run/transactions/<transaction-id>/` directories;
- add path-bound receipt files under the existing
  `knowledge/daily/receipts/` directory;
- add `scripts/repair_installed_memory.py` and v3 JSON schemas under
  `scripts/schemas/`;
- add `LLM_WIKI_COMMIT` as a mandatory installer input only for remote bootstrap;
- move active coordination to `run/markdown-transactions-v3.sqlite3` and
  `run/queue-v3.sqlite3` while retaining the v2 database bytes as
  `run/markdown-transactions-v2-retired.sqlite3` and
  `run/queue-v2-retired.sqlite3` on upgraded vaults;
- replace the two legacy active database paths with immutable JSON tombstones and
  add `run/reliability-v3-migration.json` plus
  `run/reliability-v3-adopted.json` as resumable cutover evidence; and
- version the changed `read_page` and `get_context` data payloads without changing
  the 12 MCP tool names or outer response envelope.

Production changes must conform to this list and the lifecycle semantics below.

## Baseline Evidence

The clean Windows baseline used Python 3.14.6, SQLite 3.53.1, uv 0.11.6, and
Node 24.14.0. It produced:

- 5,996 collected tests;
- 5,224 passed;
- 427 failed;
- 48 setup errors;
- 297 skipped;
- Ruff green across `scripts`, `tests`, and `benchmark`;
- JavaScript syntax green for the OpenCode plugin.

The 475 failed or errored invocations reduce to eight root-cause classes:

- 256 Windows/Python 3.14 file-snapshot failures caused by comparing path-based
  creation time with descriptor-based metadata change time;
- 120 Windows directory-identity failures caused by comparing Python's 64-bit
  `st_dev` with the legacy 32-bit Win32 volume serial;
- 35 failures from missing `jsonschema`;
- 25 failures from missing `numpy`;
- 24 failures from missing tree-sitter or Jedi dependencies;
- 1 failure from the missing MCP package;
- 12 leaked SQLite handles that block replace or unlink on Windows;
- 2 full-suite-only timing/pollution failures.

These baseline defects are part of this repair. The implementation must address root
causes rather than update assertions to accept broken behavior.

## Goals

- Prevent acknowledged session evidence from being lost when a provider, queue,
  child process, or host adapter fails.
- Make compile and queue authority depend on exact, versioned logical input identity.
- Make every live worker, compiler, and maintenance operation visible to Doctor and
  the `run/` deletion contract.
- Bound work before it consumes provider, graph, subprocess, memory, or response
  budgets.
- Make SessionStart project-specific and economical in final serialized tokens.
- Make OpenCode integration conform to OpenCode 1.18.13 rather than test-only hook
  shapes.
- Make installation fail closed, reproducible, additive for selected extras, and
  correct for XDG and OpenCode configuration precedence.
- Support the declared Python 3.10+ range, including current stable Python 3.14.
- Leave the code easier to reason about by reusing existing transaction, budget,
  process-tree, and lifecycle primitives.

## Non-Goals

- No thirteenth MCP tool and no change to the existing 12 tool names.
- No persistent daemon, remote queue, cloud service, automatic Git operation, or
  second runtime root.
- No switch to SQLite WAL.
- No Evidence Graph v3 activation and no query-time graph publication.
- No semantic project-goal extraction in a native hook. Hooks may emit deterministic
  evidence only.
- No release, version bump, or `v4.0.0` tag in this repair.
- No automatic model or Pyright download from a query, hook, Doctor, or MCP path.
- No rewrite of the transaction, queue, retrieval, or LSP subsystems.

## Preserved Architecture

- Markdown, Git, raw episodes, project journals, accepted decisions, and accepted
  artifacts remain authoritative.
- SQLite remains local coordination or derived state only.
- Operational SQLite stays in rollback-journal mode with `synchronous=FULL` and
  short explicit transactions.
- `cache/` and `logs/` remain disposable. `run/` remains protected operational
  state.
- Queue delivery remains at least once. Retained terminal authority makes repeated
  delivery safe while `run/` is retained; the design does not claim exactly once or
  replay suppression after an explicit whole-runtime reset.
- The active corpus remains `corpus-generation/v2` with `evidence-graph/v2`.
- The qualified Python navigation path remains Pyright 1.1.411, Node 22, and the
  existing platform-qualified process ownership contract.

## Architecture

The repair is split into five independently testable slices:

1. platform, dependencies, installation, and release gates;
2. lifecycle capture, feedback provenance, and SessionStart context;
3. queue, compile receipts, migrations, and operational ownership;
4. deadlines, bounded work, MCP output, and runtime token economy;
5. documentation, clean-environment verification, and cross-platform release gates.

Each slice follows red-green-refactor. A slice is not complete until its focused
tests, dependency impact checks, platform checks, and the relevant larger suites are
green.

## Platform And Filesystem Identity

### Windows File Snapshots

Python 3.12-3.14 path `stat` and descriptor `fstat` expose different meanings for
`st_ctime` on Windows. Therefore:

- path-to-descriptor comparisons use full file identity, type, size, and mtime;
- descriptor-before to descriptor-after comparisons may additionally use ctime;
- path `st_ctime` is never compared with descriptor `st_ctime`;
- a final path lookup must still match the opened descriptor's identity, type, size,
  and mtime;
- zero or unavailable file identity fails closed where exact identity is required.

The existing content hash remains the authority for bytes. Metadata is a race fence,
not a substitute for hashing.

### Windows Volume And File Identity

Live handle identity uses `GetFileInformationByHandleEx(FileIdInfo)` and its 64-bit
volume serial plus 128-bit file ID. It must not truncate Python's `st_dev` or a native
file ID to the legacy `BY_HANDLE_FILE_INFORMATION` width.

Persisted identity records gain an explicit format version. Legacy prepared
transactions are recaptured only when before/after hashes and retained parent
evidence prove the target state. Ambiguous records are quarantined, not guessed.

### SQLite Resource Lifetime

Every SQLite connection has an explicit close boundary. Transaction context managers
do not stand in for connection closure. Read paths that need both semantics use
`contextlib.closing()` around the connection and a nested transaction context where
needed.

### Durable Metadata Publication

Durable publication uses one checked platform primitive. POSIX flushes file content,
performs the same-filesystem rename/link, and requires parent-directory `fsync`; an
unsupported or failed directory sync is an error. Windows flushes file handles with
`FlushFileBuffers` and publishes or replaces names with `MoveFileExW` plus
`MOVEFILE_WRITE_THROUGH`; failure or unavailable capability returns
`metadata_durability_unavailable` and is never reported as success. The current silent
Windows return from `fsync_directory()` is not sufficient for this contract.

Recovery never assumes a rename completed merely because it was attempted. Capture
keeps pending evidence until ready/index reconciliation; terminal evidence precedes
intent deletion. Database cutover creates and verifies a separate retired v2 copy
before replacing a legacy path with its tombstone, and keeps candidates plus the
migration manifest until adoption. After restart, exact path identities and hashes
select the valid old, new, or duplicate state; missing or conflicting evidence is
quarantined. POSIX claims file-plus-directory durability. Windows claims the checked
`FlushFileBuffers`/`MOVEFILE_WRITE_THROUGH` contract and fail-closed recovery, not a
stronger portable directory-`fsync` guarantee that Win32 does not expose.

## Capture And Integration Contract

### OpenCode 1.18.13

The plugin uses only official hooks and payloads:

- lifecycle events arrive through `event({event})`;
- idle is recognized from `session.status` with status `idle`;
- direct user evidence arrives through awaited `chat.message`;
- tool evidence reads `sessionID`, `callID`, and `args`;
- `apply_patch` is a significant mutation tool;
- the second `tool.execute.after` argument is consumed as bounded, redacted result
  evidence, including changed paths, verification outcomes, and failures;
- role, message ID, text-part type, `synthetic`, and `ignored` fields are preserved;
- reasoning, ignored, and synthetic continuation text is not attributed to the user;
- `PluginInput.worktree` is the checkout identity and `directory` is only the current
  working directory;
- the awaited system-transform hook lazily loads SessionStart context; fire-and-forget
  lifecycle events may warm the same per-session promise but cannot own completion.

The awaited `chat.message`, `tool.execute.after`, and
`experimental.session.compacting` hooks persist bounded incremental evidence before
returning. The generic `session.status=idle` event never fetches a transcript or
starts an unowned asynchronous chain. Before its first promise boundary it performs
only a local handoff of the already durable session intent with a 1-second aggregate
deadline. If that handoff or the host process fails, the intent remains for bounded
SessionStart recovery.

Doctor and tests validate official event shapes and installed plugin content rather
than substring markers for unsupported direct hook keys.

### Durable Capture Intent

New transient capture writes move from disposable
`cache/transient-transcripts/` to `run/capture-intents/`.

Each create-only `capture-intent/v1` file is canonical UTF-8 JSON and contains:

- intent, source occurrence, and source event IDs;
- occurrence time, host, event, session, project slug, and checkout/worktree;
- trigger and checkpoint reason;
- chunk index/count, ordered role-preserved messages and text parts; and
- complete-redacted-input digest, chunk digest, and schema version.

The adapter derives stable source event and occurrence IDs from official host IDs.
For an in-bounds host event with no standalone occurrence ID, it hashes adapter name,
event type, session ID, checkpoint cursor, and complete redacted-input digest. A
source timestamp is included only when supplied stably by the host; local observation
time is not part of canonical intent bytes. The intent ID is SHA-256 over canonical JSON
containing schema version, source event and occurrence IDs, stable source timestamp,
checkpoint reason, chunk index, and chunk digest. Re-delivery therefore resolves to
the same path and bytes. An existing path with different bytes is quarantined as an
identity conflict.

The filename is `<intent-id>.json`, sharded by the first two hex characters of the
intent ID. Creation publishes first under
`run/capture-intents/pending/<shard>/` and inserts a derived pending index row in
`queue-v3.sqlite3`. It then create-only publishes the same verified bytes under
`run/capture-intents/ready/<shard>/`, marks the index ready, and only then removes the
pending name. Every file and directory boundary uses Durable Metadata Publication
above. The file remains authority when a crash interrupts any boundary; recovery
reconciles one or two names and the derived row before enqueue.

Creation and recovery hold that intent's fence in mode `capture` from the first
pending publication through atomic queue task/link insertion. Enqueue verifies the
exact capture-fence token/epoch and absence of a terminal record. If delivery fails,
the ready intent remains and the fence is released for later recovery; no taskless
discard can overlap the checked enqueue boundary.

One canonical intent file is at most 1 MiB and contains at most 256 evidence items.
IDs are at most 256 UTF-8 bytes, a project slug at most 256 bytes, and any normalized
path at most 4,096 bytes. A text part is split at UTF-8 code-point boundaries into
pieces of at most 64 KiB; batching then splits only between complete pieces. No text
is silently truncated to meet the file ceiling.

Before redaction, traversal is limited to depth 64, 4,096 nodes, any one raw string to
4,194,304 UTF-16 code units, and all visited strings to 4,194,304 code units. UTF-8
encoding/redaction is incremental and stops at 16 MiB of raw bytes. Adapter stdin or
IPC bytes are likewise capped while reading, before JSON parsing. After redaction,
one source occurrence is limited to 8 MiB of canonical evidence, 2,048 evidence
items, and 8 intent chunks. These raw and redacted ceilings bound work even when a
large secret collapses to a short redaction marker.

At the first exceeded bound, the adapter writes a `capture-overflow/v1` intent of at
most 64 KiB with stable source fields, limit code, capped counters, bounded prefix
digest, and `capture_overflow`; it does not emit `semantic_ok` or claim successful
capture. If the host has no stable occurrence ID, overflow ID is the SHA-256 of the
adapter, event, session, checkpoint cursor, limit code, and bounded prefix digest.
That record sets `source_identity_incomplete=true` and deterministically coalesces
retries of the same oversized checkpoint instead of creating unlimited blockers. It
is never enqueued for semantic processing. An awaited host hook returns an explicit
integration failure after the overflow record is durable. Overflow remains an
operator-visible deletion blocker and is never silently truncated into normal
evidence.

The file is written with exclusive creation, owner-only permissions, flush, file
sync, and parent-directory sync where supported. Queue payload and dedupe identity
contain the intent ID, normalized relative path, file digest, and handler version;
enqueue records a reference but is not an ownership transfer and never permits
intent deletion.

A terminal outcome is an immutable, canonical `capture-terminal/v1` record at
`run/queue-results/capture-<intent-id>.json`. It is at most 64 KiB and binds the
intent ID and digest, sorted semantic decision references and digests, exactly one
processing binding, and exactly one disposition. Processing binding is either
`task`, with task ID plus active capture-task link/resolution digest, or `taskless`,
with reason exactly `capture_overflow` or `pre_enqueue_operator_discard` plus the
intent digest. Taskless binding permits only `operator_discard`, has no semantic
decision, and uses only the intent fence. Semantic processing and
`markdown_committed`/`no_durable_content` require a validated task binding.

Dispositions are:

- `markdown_committed`, with committed transaction/output identities and the exact
  decision digest applied by that transaction;
- `no_durable_content`, created only from a schema-valid `FLUSH_OK` decision whose
  digest is embedded in the terminal record; or
- `operator_discard`, with current POSIX UID or Windows SID, command operation ID,
  nonempty reason of at most 4,096 UTF-8 bytes, and time; this disposition may have no
  semantic decision but must bind the rollback receipt if a transaction existed.

The terminal record is exclusively created, file-synced, parent-synced, and verified
before the intent is unlinked and its parent is synced. A conflicting terminal record
quarantines the intent. Terminal records follow the existing queue-result minimum
30-day retention, are excluded from ordinary result purge, and remain replay authority
until an explicit otherwise eligible whole-`run/` deletion. After 30 days a validated
terminal record does not independently block that whole-run deletion. Crash recovery
may repeat work, so every downstream write and transaction uses the deterministic
intent ID as its idempotency identity. Deleting all eligible `run/` state explicitly
forfeits old-event replay suppression; no idempotency claim survives that operator
reset.

No nondeterministic provider result is applied directly. Each classifier, feedback,
or verification stage first exclusively publishes a bounded immutable
`capture-decision/v1` result through a separate nonterminal decision ledger. Stage is
exactly `flush`, `feedback`, or `feedback-verify`. Its filename is
`capture-decision-<decision-key-sha256>.json`, where the key hashes canonical intent
ID and stage rather than interpolating untrusted text. The record binds intent and
input digests, provider/model, validated wire output, exact normalized operation plan
including chosen timestamps, active capture-task link/resolution digest, and decision
digest. The record is at most 1 MiB and is
synced and read-back verified before any Markdown or queue side effect. A semantic
result that cannot produce a valid record within that ceiling is `invalid_output` and
remains nonterminal.

`semantic_decisions` in `queue-v3.sqlite3` is keyed by intent ID and stage and stores
the decision path, digest, and publication state. Publication syncs the create-only
file, inserts or verifies the exact ledger row, and remains nonterminal. It never
populates the task's `result_reference`/`result_sha256`, changes task state to
`succeeded`, or triggers lease-expiry result adoption. The queue's single terminal
result slot is populated only with a validated terminal capture record after all
side effects and terminal checks complete. File/ledger partial states reconcile by
exact decision key and digest.

Recovery order is terminal record, existing decision record, then deterministic
transaction/result adoption, and only then provider invocation. A crash after a
Markdown commit but before terminal publication adopts the transaction with the
intent-derived operation ID and expected decision digest; it never calls the provider
again. A crash before decision publication may call the provider again because no
side effect exists. Conflicting decision bytes quarantine the intent rather than
retrying different bytes under the same operation ID.
Decision records share the 30-day minimum and cannot be purged before their terminal
record or while a task or intent references them. A committed transaction reference
is retention-live only until both its 30-day undo window has expired and the matching
terminal record has been validated; the immutable transaction row may retain the
decision digest after the queue-result file becomes purge-eligible. For
`no_durable_content`, the decision file becomes purge-eligible after 30 days once its
validated terminal embeds the decision digest; the terminal itself remains replay
authority until whole-`run/` deletion.

No untrusted identifier is interpolated into a runtime filename. Intent and decision
names use lowercase 64-hex digests. Other result packages use a lowercase SHA-256 key
over canonical identity fields; original IDs remain inside validated content.

Worker and operator outcomes are linearized by explicit item fences. `intent_fences`
in `markdown-transactions-v3.sqlite3` is keyed by intent ID and records mode
`capture`, `worker`, or `operator`, token, fencing epoch, process identity, heartbeat,
and expiry.
A worker acquires it before provider work and holds it through the final terminal-file
publication. Every Markdown side-effect transaction verifies the exact intent token
and epoch plus active capture-task link/resolution digest as SQL preconditions in the
same database transaction. An operator discard
can acquire the fence only when no worker fence is live; expiry still requires
positive process-death proof. In that acquisition transaction it checks the
intent-derived operation ID. If Markdown already committed, discard is forbidden and
recovery must publish `markdown_committed` instead.

Operator discard also fails closed on transaction lifecycle. `preparing`, `prepared`,
or `applying` must choose one recovery direction under the operator fence before any
discard decision. Forward recovery that commits permits only `markdown_committed`.
Rollback first transactionally changes the row to `aborting` with deterministic abort
operation ID and expected before-image manifest; normal recovery treats `aborting` as
rollback-only and can no longer recover it forward.

Rollback restores and verifies every path, then exclusively publishes
`run/transactions/<transaction-id>/abort-receipt.json` as a canonical
`transaction-abort/v1` record of at most 64 KiB. It binds transaction/intent/fence and
abort operation IDs, before-image manifest, observed restoration hashes, and abort
time. Only after read-back verification may one SQLite transaction recheck the
operator fence, receipt digest, and restored tree and change `aborting` to terminal
`aborted`. Terminal discard then binds that receipt digest.

Recovery closes every crash boundary: `aborting` without a receipt resumes restore;
a valid receipt with `aborting` is adopted into `aborted`; `aborted` without the exact
receipt is quarantined as corruption; and a terminal discard cannot publish before
row and receipt agree. `conflicted` or `quarantined` transactions cannot enter abort
and remain blockers for explicit transaction repair. A discard terminal record is
never published while a transaction can still recover forward to commit.

`task_fences` in `queue-v3.sqlite3` similarly serializes one queue worker or
`queue-operator` for a task. Every task terminal transition verifies its exact task
fence and lease in the same queue transaction. Operator discard or corrupt quarantine
for a task-backed intent requires both task and intent fences and is refused while
either worker fence is live. Taskless overflow/pre-enqueue discard requires only the
operator-mode intent fence because no task exists; its transaction rechecks that no
capture-task link was inserted. Capture/recovery enqueue requires capture mode, so it
cannot commit while operator mode is held. Thus an operator terminal record cannot
win while a worker remains able to commit later side effects.

When both fences are required, every worker and operator acquires task fence first and
intent fence second, then releases intent fence first and task fence last. Failure to
acquire the second fence releases only the exact first-fence token. A crash between
acquisitions leaves the task fence as a visible blocker until expiry plus positive
process-death proof; no path acquires the pair in the opposite order.

Cancellation, redrive, export, and ordinary purge cannot discard an unresolved
capture intent. Explicit operator discard creates a durable discard receipt before
the queue row or intent may be removed. Automatic retention cleanup never acts as
operator discard.

Provider errors, invalid output, queue errors, child timeout, and detached-parent
death never produce a semantic OK acknowledgement. A bounded SessionStart recovery
pass reserves its first 250 milliseconds, 8 intents, and 2 MiB for at most 4,096
entries from one `pending/<shard>/`. It advances a 00-ff shard cursor in existing
`run/state.json` after every pass and does not claim portable lexical order within a
directory. Every selected valid item leaves pending; malformed items move to bounded
quarantine, so repeated round-robin passes make progress without sorting or scanning
the complete tree. Remaining capacity processes up to 18 oldest and 6 newest
distinct indexed-ready rows. The complete pass stops at 32 processed intents, 8 MiB,
or 1 second; unused capacity from either lane may be borrowed only after that lane is
empty. The reserved lane prevents ready backlog from starving crash orphans;
two-ended SQL ordering prevents sustained indexed backlog from starving old or new
work. It reports indexed counts plus whether pending reconciliation was truncated,
without an unbounded filesystem count. Existing files under
`cache/transient-transcripts/` remain readable as legacy recovery input and are
converted to a deterministic intent with matching redacted evidence. A legacy file
is removed only after the new intent is synced and read-back verified; the new intent
then remains until its own terminal record exists.

Malformed pending files use package
`run/queue-quarantine/capture-<quarantine-key-sha256>/`. The key hashes original
relative path, bounded file identity/size metadata, and bounded prefix or complete
digest; no untrusted name is interpolated. Quarantine first publishes immutable
`quarantine-intent.json` while retaining the original pending name, then create-only
hard-links the same local file as `raw`, verifies identity, and finally publishes a
`capture-quarantine/v1` `manifest.json` of at most 64 KiB. Only a verified final
manifest permits unlink of the exact original pending identity. Unsupported hard-link
publication leaves the source in pending and reports a blocker rather than copying or
dropping it.

Recovery is explicit for every partial package: intent-only resumes raw/manifest
publication; raw-without-manifest verifies the link and completes the manifest;
manifest-plus-source verifies then removes only the source name; missing intent or
missing both raw and source is quarantined as a conflict. The manifest records why
normal intent validation failed and whether the raw digest is complete. Raw and
manifest remain deletion blockers. Explicit installed-vault repair may either validate
and restore the exact intent or create `capture-quarantine-disposition/v1` with
UID/SID and a reason of at most 4,096 bytes. No automatic cleanup occurs; a
dispositioned package follows the 30-day queue-result retention before purge
eligibility.

Restore is also receipt-driven. Repair first create-only publishes and indexes the
validated intent under its normal ready shard, then writes immutable
`capture-quarantine-restore/v1` as `restore-receipt.json` inside the package. The
receipt binds package manifest, ready file/index, intent, repair operation, and
UID/SID digests. Crash recovery adopts a verified ready publication into the receipt
or resumes publication; it never removes package evidence first. A restored package
ceases to be an independent blocker only when its restore receipt validates and the
restored intent remains present or has a valid terminal record. Package evidence then
has 30-day retention and becomes purge-eligible; missing restored authority makes it
a blocker again.

### Flush Outcomes And Deferred Parity

Closed outcomes distinguish:

- `no_capture`;
- `semantic_ok`;
- `major_written`;
- `minor_written`;
- `deferred`;
- `capture_overflow`;
- `provider_error`;
- `invalid_output`;
- `delivery_error`.

Classifier wire grammar is closed. Valid output is exactly `FLUSH_OK`, or
`FLUSH_MAJOR\n<body>`, or `FLUSH_MINOR\n<body>` with a nonempty body. Leading or
trailing prose, Markdown fences, decorated sentinels, an empty body, and unknown
tokens are `invalid_output`; provider exceptions are `provider_error`.

`no_capture` is valid only when no intent was created. For an existing intent,
`semantic_ok` maps to terminal disposition `no_durable_content`, `major_written` and
`minor_written` map to `markdown_committed`, and every other outcome is nonterminal.
Metrics count only the outcome they name. Deferred handling performs the same daily
write, feedback evaluation, dedupe reconciliation, counters, and compile trigger as
the live path. Redrive preserves the source intent identity.

For in-bounds capture, dedupe identity is the deterministic intent ID. Reusing a
session and event name does not suppress a different source occurrence or content
digest, while duplicate host delivery cannot create a different queue payload.
Overflow is not queued and follows its explicitly coalescing identity above.

### Feedback Authority

Correction candidates retain actor, message/event ID, project, evidence span,
derivation, and source digest. Direct role-identified user text may have
`source_authority: user`. Text inferred from an LLM summary remains
`source_authority: ai-derived` unless the user explicitly confirms it. Confidence
uses only `high`, `medium`, or `low`.

### Deterministic Project Deltas

The existing adapter may project deterministic facts already observed by a hook:
changed paths, commands, verification results, and failure signals. It does not infer
goals, current tasks, next actions, blockers, or decisions. Semantic handoff remains
manual through the existing product surface until a separately approved design
changes that boundary.

## SessionStart And Token Contract

SessionStart resolves and reserves the current project before reading project data.
It recovers project transactions before building context and passes the explicit
project slug to advisory, guardrail, daily, and handoff readers.

The combined route preserves the existing project marker and home/scratch exclusion,
first-project state/bootstrap creation, nightly catch-up, debug dump, and repeated
installer merge behavior. The canonical Claude ownership markers include
`integration_adapter.py`, so reinstalling cannot duplicate SessionStart or Stop
commands.

One packer budgets the final response. It operates on complete items and then
measures the final serialized output. Text is not repeated in multiple response
fields. Mandatory items are limited to current safety and active-task evidence;
legacy handoff history is optional. Oversized active fields are rejected or bounded
before packing rather than collapsing the entire response.

The default injection target remains at most 2,000 estimated tokens. The response
reports the estimator and measured serialized size. Unknown token counts fail closed
for model-bound calls.

## Compile Receipt V3

`compile-receipt/v3` binds authority to source identity rather than bytes alone.

Each source descriptor contains:

- normalized logical path;
- SHA-256 digest;
- byte size;
- source occurrence bounds when available.

One successful batch creates one immutable receipt per source. The receipt path is
`knowledge/daily/receipts/v3-<source-identity-sha256>.md`, where the identity digest
is over canonical JSON containing the logical path and content digest. The
deterministic operation ID and action key make a retry of the same successful batch
resolve to the exact same bytes and path.

A receipt also contains the sorted batch manifest, action key, operation ID,
packing/tokenizer identity, provider/model budget, and one validated terminal
disposition for each source. Receipt dispositions are only `compiled` and
`no_durable_content`. A quarantined or otherwise unresolved source receives no
compile receipt and remains pending, so a later successful retry can create the one
immutable source receipt without replacing history.

Compilation packs bounded batches before provider dispatch. The critique receives
the proposed operations and their cited evidence, not a duplicate unrestricted
source blob. Receipts are committed in the same Markdown transaction as output,
index, and log changes.

V2 receipts remain readable as historical evidence but never authorize automatic
skip or archive because they cannot prove logical source identity. V2 files are not
rewritten, and migration never blanket-recompiles every v2 source. A source receives
v3 authority only when normal scheduling or an explicit bounded repair selects and
successfully compiles that exact path and digest. Work remains subject to normal item,
byte, provider, and aggregate-operation budgets.

## Queue Task V3

`queue-task/v3` describes the actual serialized production record and is validated
by round-tripping the production exporter. V2 remains a published historical schema
and is not silently changed.

Capture-backed tasks also have an immutable `capture_task_links` row in
`queue-v3.sqlite3`, written atomically with enqueue and keyed by task ID. It stores
lowercase 64-hex intent ID, intent-file digest, handler version, and link digest
outside the payload BLOB. Normal processing requires that row, the derived capture
index, and the authoritative intent file to agree. Corrupt-task handling never infers
intent identity from corrupt payload bytes. It acquires an intent fence only from a
validated link; a missing or conflicting link leaves both task and candidate intents
blocked for explicit repair and permits export-only, not disposition.

Explicit repair never edits an immutable link. Under canonical `repair` ownership and
task/intent quiescence it appends `capture-task-link-resolution/v1` to
`capture_task_link_resolutions` in `queue-v3.sqlite3`, binding the task,
all observed link/index/file digests, current UID/SID, reason, and either one validated
intent or `none`. A validated intent becomes the active overlay; `none` quarantines
the relation, leaves the task nonterminal, and requires the normal
`quarantine-corrupt` export/disposition path without an intent fence; it does not
directly change task state. Every intent remains independently unresolved.

A later resolution may supersede the prior digest only before consumption. The first
semantic decision, transaction, terminal result, or corrupt disposition appends an
immutable `capture_task_link_seals` row in `queue-v3.sqlite3`, binding the active
original-link or resolution digest to that consumer. Resolution append/supersede and
first seal serialize in the same queue transaction; the seal always commits before a
side effect. A sealed digest is included in every downstream precondition and cannot
be superseded. A discovered post-seal error creates conflict/correction
evidence rather than rewriting consumed authority. Zero or multiple unsuperseded
unsealed leaves remain conflicted and blocked. Thus every conflict has an
evidence-preserving operator path without guessing from corrupt payload.

`input_hash` is the single payload identity field and is SHA-256 over the exact
canonical UTF-8 bytes stored in the v3 payload BLOB. Canonical form is the existing
`canonical_json_bytes()` domain: NFC strings and keys, string keys only, no floats,
sorted keys, compact separators, and no ASCII escaping. On read, parse-and-reencode
must reproduce the stored bytes before the hash can be trusted. Documentation,
schema, Doctor, migration, and dedupe use that name and definition consistently.

Hash and canonical-form validation occurs before every normal insertion, claim, lease
renewal or expiry, execute handoff, completion, failure, cancellation, export,
redrive, result adoption, dead-lettering, migration, and purge. A mismatch is never
dispatched or copied into a new task. The closed exceptions are metadata transition
to `dead/payload_hash_mismatch`, fenced explicit transition through
`quarantine_pending` to `quarantined`, and later disposition-authorized purge. These
paths validate raw
bytes against disposition/package digests instead of asserting canonical payload
validity. Detection streams the payload only for form/digest checks; it never parses
it into work, executes it, exports it automatically, or deletes it. Corrupt rows and
their evidence remain deletion blockers until explicit export and operator
disposition.

The explicit CLI operation `quarantine-corrupt` requires task ID and a nonempty
reason of at most 4,096 UTF-8 bytes. It holds canonical role `queue-operator` through
package and database publication. Its first fenced transaction streams raw payload,
task metadata, and attempt history digests, records a deterministic
`corrupt_export_operations` row, captures the current `lineage_generation`, and moves
the task to nonexecutable/non-redrivable `quarantine_pending`. Every creation,
deletion, or change of an incoming `redrive_of` link must increment that parent
generation and is rejected while the parent is `quarantine_pending` or
`quarantined`. This freezes lineage without enumerating it in the first transaction;
only the exact disposition-authorized purge protocol below may later remove links.

The operation writes only to fixed existing runtime surface
`run/queue-results/corrupt-<disposition-key-sha256>/`, where the key hashes canonical
task ID, observed raw/history/metadata digests, actor identity, and reason. Raw
payload, history, and metadata are streamed into owner-only files. Incoming redrive
links are exported in resumable pages of at most 1,000 links, 1 MiB, or 5 seconds per
invocation. Each page is create-only and updates a rolling root hash and cursor in the
operation row. No invocation scans or buffers the complete fan-out. Recovery adopts
verified pages and resumes the cursor; it never interprets or executes payload.

After all links are paged, a bounded manifest records raw/history hashes,
`lineage_generation`, link count, page count, and rolling root. One final
`BEGIN IMMEDIATE` transaction re-streams raw/history digests, verifies unchanged task
metadata and lineage generation/count, inserts immutable `corrupt_dispositions`, and
transitions `quarantine_pending` to terminal `quarantined`. It does not delete payload,
attempt history, or incoming lineage. Any mismatch aborts the final transition while
leaving the resumable operation and task nonexecutable.

An exact orphan package is adopted on retry; a nonmatching package remains an explicit
retained-result blocker and can receive only a superseded-package disposition, never
silent removal. `quarantined` tasks cannot execute or redrive. Their package, row,
history, and disposition block deletion for at least 30 days and while any referenced
capture intent is unresolved. After retention, fenced purge creates a deterministic
`corrupt_purge_operations` row, verifies package/disposition and frozen lineage root,
and moves the parent from `quarantined` to non-redrivable `purge_pending`. Normal link
mutation remains forbidden. Only that exact purge token may delete incoming links,
and only leaves-first when each child is independently terminal and retention-eligible.

Purge is resumable: each invocation processes at most 1,000 links, 1 MiB of metadata,
or 5 seconds in one transaction; records a create-only page receipt; increments both
lineage generation and the operation's expected generation; and advances its cursor.
An ineligible child pauses with a blocker rather than forcing one-shot traversal. When
incoming count reaches zero, purge writes a bounded `corrupt-purge/v1` receipt, then a
final transaction re-streams raw/history digests and deletes parent history/task only
if the exact operation, package, disposition, generation, and zero-link state still
match. Receipt-with-row resumes the final deletion; deletion without the prior receipt
is impossible through the API. The operation evidence then follows queue-result
retention. A fully dispositioned lineage may instead be included in an otherwise
eligible offline whole-`run/` deletion without first rewriting its foreign keys.

A dedupe key aliases work only when `kind`, `handler_version`, and `input_hash` all
match. Any mismatch raises `dedupe_conflict`. Exact duplicates return the existing
task ID.

The closed v3 states are `ready`, `leased`, `blocked`, `succeeded`, `dead`,
`cancelled`, `quarantine_pending`, `quarantined`, and `purge_pending`. The schema
records real retry limits, timestamps,
lease fields, result reference,
and bounded attempt history. Canonical payload is at most 1 MiB, nesting depth at
most 32, any string at most 256 KiB, and any array or object at most 1,024 members.
Task ID and owner/token fields are at most 256 bytes, kind 64 bytes, dedupe key 512
bytes, result reference 4,096 bytes, error code 64 bytes, and blocked capability 128
bytes. Handler version is 1 through 2,147,483,647; attempts and immutable attempt
history are limited by the configured policy with a hard maximum of 100. Export
metadata retains the existing 64 MiB aggregate ceiling and each result the existing
16 MiB ceiling. All limits measure canonical UTF-8 bytes, not Python character count.

## Operational Ownership

Every actor that can create or retain state under `run/` uses one admission and lease
protocol. `maintenance_owners` in `run/markdown-transactions-v3.sqlite3` is the
canonical admission registry. The closed roles are `capture`, `project`,
`markdown-writer`, `queue-worker`, `compile`, `doctor`, `nightly`, `weekly`, `lsp`,
`queue-operator`, `repair`, and `runtime-deletion-check`. Existing project, writer,
queue, and LSP leases remain
their domain fences but project the same canonical token and fencing epoch. Queue
workers publish that projection in `queue_ownership` in `run/queue-v3.sqlite3`.
Domain rows are not separate deletion authority, and the active database count
remains two.

Each canonical owner records:

- closed role plus unique actor instance ID;
- random token;
- process identity;
- monotonic fencing epoch;
- acquired, heartbeat, and expiry timestamps;
- compare-and-update heartbeat and exact-token release;
- affected-row verification on every mutation.

Queue worker, queue operator, repair, compile, nightly, and weekly leases use 120
seconds with a 40-second heartbeat. Existing project and LSP domains retain 30 seconds with a
10-second heartbeat; capture, Markdown-writer, Doctor, and
`runtime-deletion-check` use the same 30/10 values. Doctor's protected scan has a
20-second aggregate deadline and fails closed before its lease can expire.
The canonical row always uses the domain's exact expiry. Nested work receives the
caller's lease and cannot reacquire or release it. Every external publication rechecks
the current token and epoch. Process ID plus process-start identity protects against
PID reuse.

Expiry alone never permits takeover. Replacement requires both lease expiry and
positive proof that the recorded process identity is dead. Unsupported or denied
liveness checks produce `owner_liveness_unknown`, retain the row, and block takeover
and deletion. A successful takeover increments the fencing epoch. Queue acquisition
commits canonical admission first and matching queue projection second; projection
failure releases only the same canonical token. Release clears the queue projection
first and canonical admission last. A crash at either boundary therefore leaves a
visible blocker.

All other domain projections follow the same rule. A direct top-level project or
writer acquisition inserts canonical admission and its domain row together in one
transaction because they share the v3 transaction database. Nested writer work under
an existing project, compile, or maintenance owner verifies the caller's exact
canonical token and epoch, then atomically inserts only a writer domain fence that
references that parent; it neither reacquires nor releases canonical admission.
Nested release removes only that writer fence. LSP creates canonical admission first, then
`owner.json`/`lease.json` with the same token and epoch; controlled release removes
the domain lease first and canonical row last. Capture creates canonical admission
before any pending intent and releases only after file/index/handoff reconciliation.
An external publication requires both canonical and domain fences when a domain fence
exists. A canonical row without its domain projection and a domain projection without
its canonical row both block Doctor; repair clears either only after expiry and
positive process-death proof. Legacy compile/maintenance markers are the sole
marker-first exception described below, because pre-v3 exclusion requires it.

The queue worker and LSP roles cover their complete process lifetimes, including idle
waits. Capture covers intent creation through durable handoff; project and Markdown
writer roles cover their complete mutations. Unknown roles fail closed. Every
acquisition transaction first checks that no `runtime-deletion-check` owner exists.
Doctor may acquire that role only in one canonical transaction that finds no other
owner. This blocks all new v3 capture, project, writer, queue, compile, scheduled, and
LSP admissions while Doctor checks domain databases and filesystem evidence. Doctor
releases it before returning and reports only a quiescent snapshot, never a durable
deletion permit. Actual `run/` deletion remains an explicit offline operator action
after all agents are stopped; no command in this repair deletes `run/`.

`compile.pid` and `maintenance.lock` remain compatibility evidence during migration.
`compile.pid` retains its current three-line PID/start/owner format.
`maintenance.lock` remains exactly one ASCII decimal PID so existing nightly/weekly
parsers do not treat a v3 marker as malformed and delete it. For compile and scheduled
maintenance, acquisition creates the legacy-format marker first, captures its exact
content digest plus POSIX device/inode or Windows full file identity, and stores that
identity with the canonical token before work begins. A lease failure removes only a
marker with the same captured identity, digest, and PID. Release clears canonical
admission first and the exact legacy marker last. This ordering excludes an older
process that understands only the marker and a newer process that understands both.
A live but expired compatibility owner is not reclaimed automatically; it requires
explicit repair.

An installed vault has no automatic proof that every pre-v3 process is absent.
Therefore normal startup never auto-adopts ownership v3. The explicit
`repair_installed_memory.py --apply --adopt-ownership-v3` flow requires an offline
maintenance window and operator confirmation that every agent process using the
vault is stopped. It obtains exclusive transactions, rejects live legacy markers or
in-flight queue leases, recovers an expired lease only after positive process-death
proof, and verifies stable source database identities and hashes before cutover.

The active v3 databases use new paths:

- `run/markdown-transactions-v3.sqlite3`;
- `run/queue-v3.sqlite3`.

On upgrade, the exact v2 bytes are retained at
`run/markdown-transactions-v2-retired.sqlite3` and
`run/queue-v2-retired.sqlite3`. Each legacy active path is replaced with an
owner-only canonical JSON `operational-db-tombstone/v1` record. Its `source_state` is
exactly `upgrade` or `fresh`. `upgrade` requires legacy and replacement relative
paths plus retired-file SHA-256; `fresh` forbids retired path/hash and binds only the
legacy tombstone path, replacement path, operation ID, and adoption schema digest. A
normal pre-v3 process then fails when it tries to open the tombstone as SQLite; it
cannot reach either active v3 database. Tombstones and retired databases
are never removed individually in this repair. Retired v2 databases remain deletion
blockers. A complete validated tombstone/adoption set does not independently block an
otherwise eligible whole-`run/` offline deletion; a partial or mismatched set does.

`run/reliability-v3-migration.json` is an immutable create-only operation manifest.
`run/reliability-v3-adopted.json` is created only after both v3 databases, both
tombstones, all retained source hashes, schema versions, PRAGMA values, and path
identities validate under the same operation ID. V3 mutation requires that complete
adoption record. Fresh vaults create v3 databases, legacy-path tombstones, and the
migration/adoption records directly with `source_state=fresh`; they have no retired
v2 files.

Tombstones are at most 4 KiB; migration and adoption records are at most 64 KiB.
They use canonical UTF-8 JSON, owner-only permissions, exclusive creation, file and
parent sync, normalized relative paths, and lowercase SHA-256 values. An existing
path with different bytes or identity is a conflict and is never overwritten.

This cutover does not claim to make two filesystem operations atomic. If repair is
interrupted, v3 mutation remains disabled and explicit repair resumes from the
manifest and observed hashes. The operator must keep the vault offline until the
adoption record exists. After completed adoption, ordinary restarted pre-v3 code is
blocked from the known v2 queue and MarkdownCoordinator databases by tombstones rather
than cooperative protocol checks. Adoption also requires installed host integrations
to match the v3 content digest. Arbitrary same-user execution of a separate old
checkout or direct Markdown editing cannot be fenced by local SQLite paths and remains
outside the cooperating-writer guarantee; this is why cutover requires the explicit
offline operator window.

Before a complete adoption record exists, Doctor always reports
`legacy_protocol_unquiesced` as a hard deletion blocker. It may inspect state but
cannot claim an admission-protected or deletion-eligible snapshot, because pre-v3
actors do not honor the v3 registry.

Doctor excludes only its own exact `runtime-deletion-check` token from the protected
snapshot; every other live, expired-but-not-proven-dead, or unknown owner blocks
deletion. Migration and adoption records are evidence, not owners.

Removal of compatibility markers, retired databases, or legacy-path tombstones
requires later installed-vault evidence and is outside this repair.

Scheduled jobs hold ownership for their complete run, require terminal child
outcomes, and never release the outer lease around nested maintenance. Weekly work
performs the final Markdown index, FTS, and generation refresh after its own
mutations. The production daily append remains protected by the recoverable
transaction writer; the unused `_daily_lock` and tests that only assert its presence
are removed.

## Atomic And Resumable SQLite Migrations

Operational schema initialization uses explicit transaction control and individual
statements. It does not call `executescript()` inside an already-open transaction.

Every migration verifies its completed invariant on every startup. Column presence
alone is not completion. Backfills, required non-null values, indexes, and schema
version markers must all agree before the migration is considered complete. A crash
at any statement can be rerun safely. Installed databases with partially applied
historical migrations are repaired conservatively; ambiguous state is reported and
left protected.

The two operational databases and their path swaps cannot commit atomically together.
Adoption therefore uses one deterministic operation ID. Under the required offline
window, repair creates and verifies a byte-for-byte retired copy while the source is
exclusively locked, then creates each v3 candidate with SQLite's backup API, closes
it, and validates integrity, schema, and source-row reconciliation. For each legacy
database it then replaces the still-retained legacy active path with a prepared
tombstone, publishes the validated v3 candidate, and applies the checked metadata
publication contract at every boundary. The immutable migration manifest makes every
observed partial path state resumable or quarantinable by exact hash. Runtime mutation
remains disabled until the final adoption record validates the complete pair. Safety
during an incomplete cutover depends on the explicit offline maintenance window;
the design does not claim otherwise.

After setting SQLite PRAGMAs, the code reads them back and rejects an operational
database that is not using `journal_mode=DELETE`, `synchronous=FULL`,
`foreign_keys=ON`, and `trusted_schema=OFF`. V3 also validates its fixed
`application_id` and `user_version`.

## Deadlines, Processes, And Bounded Work

### LLM Calls

One absolute monotonic deadline covers the complete provider fallback operation.
Each provider receives only the remaining time. HTTP bodies, CLI stdout/stderr, and
output files are read to a ceiling while data arrives, not after buffering. Each
response body, stdout stream, and output file is limited to 4 MiB; stderr is limited
to 1 MiB. A provider request is limited by its declared token budget and the existing
32 MiB aggregate source ceiling. Overflow is a terminal provider-attempt error and
still performs full process/session cleanup. Timeout cleanup owns the entire
supported process tree.

OpenCode uses `/global/health`, `/session/:id/message`, optional Basic auth, quoted
session IDs, bounded JSON parsing, and operation-owned session cleanup: abort, wait
for idle within the remaining deadline, then delete. Only session IDs created and
recorded by the current operation may be cleaned up.

### Process Trees

Queue and maintenance subprocesses reuse a generic form of the qualified LSP
process-tree runner. Windows uses a kill-on-close Job Object. POSIX uses a new process
group and bounded TERM/KILL escalation. The existing hostile `setsid()` limitation
remains documented; no ancestry scan is called ownership.

### Graph And Navigation

Structural graph tools do not silently perform a live workspace scan when no active
generation exists. Live extraction requires `live=true`. Both stored and live paths
accept cancellation and one caller deadline. Query-time live extraction is capped at
2,000 files, 8 MiB per file, 64 MiB total source bytes, 10,000 directory entries,
2,000 directories, depth 32, and 1,000 result records. Exceeding a bound returns a
stable `live_budget_exceeded` diagnostic and no result presented as complete. Stored
queries retain the MCP `limit` maximum of 100 and graph traversal depth maximum 32.

Navigation limits offset to 10,000 and limit to 100, pushes only `offset + limit` into
provider work, caps prepared call-hierarchy items at that same value, caches immutable
Pyright discovery identity, and retains one navigation facade per managed session.
Freshness checks and output semantics remain unchanged.

### MCP Output

Budgets apply to the final serialized package, not only an intermediate text field.
Repeated item text is replaced by stable references. The outer MCP envelope remains
version `1.0`; changed data payloads carry their own version.

`read_page` returns `read-page/v2` data. Inputs are `offset_bytes` and `limit_bytes`;
the defaults are 0 and 65,536, and the maximum limit remains the existing 4 MiB
safety ceiling. Offsets are UTF-8 byte offsets aligned to a code-point boundary. The
result includes source SHA-256, source byte size, returned byte range, `next_offset`,
and `eof`. A pre/post source hash mismatch returns stale evidence and no content.
Existing callers that omit pagination receive the bounded first window with
`partial=true` when more bytes remain.

`get_context` resolves the at-most-20 requested slugs directly under the caller
deadline before corpus compilation; it does not capture and hash the complete vault
and then filter. Its `context-package/v2` stores item text once and uses stable item
IDs from decisions, incidents, active-task, and evidence views. The final serialized
package, traces included, must fit the requested token budget.

Tool schemas bound every string and collection and reject unknown arguments
consistently.

The MCP server makes no claim that every tool is network-free. It opens no listening
network service in stdio mode, but explicitly requested compile or grounded-provider
operations may call the configured provider. Optional local embedding and reranker
models load with local-only behavior; a missing model degrades without downloading
during a query.

### Session Maintenance And Model Loading

SessionStart launches at most one live queue maintenance worker through the worker
owner lease. Advisory inventory is bounded and reused during one context build.
Optional model initialization is single-flight. Its failure cache holds at most 128
model-ID/revision entries, expires each after 60 seconds, and evicts least-recently
used entries so every request does not repeat an expensive failed load.

There is no selected default reranker while the model matrix says evidence is
pending. The mutable `BAAI/bge-reranker-base@main` default is removed. Reranking is
enabled only with an explicitly configured model ID and immutable revision that is
already present locally. The optional dependency description and EN/RU/ZH docs use
the same model status. A future default requires the existing matrix quality,
resource, license, and Pareto gates.

Grounded-provider work uses the existing bounded straggler-slot pattern. A caller
deadline cannot create an unbounded number of detached provider threads.

## Installer And Environment Contract

### Bootstrap And Git Safety

Pipe mode never treats the caller's current directory as the source checkout. A
remote bootstrap requires `LLM_WIKI_COMMIT` as a full 40-hex commit OID and verifies
that exact checked-out OID, repository identity, and required files after clone. A
signed release tag may be a human label, but it is resolved and checked against the
documented full OID; a tag name alone is not treated as immutable. Until a real
release exists, public documentation does not advertise a nonexistent production
URL.

An existing checkout's Git remotes are not changed silently. Accidental-push
protection is automatic only for a clone created by the installer, or when the user
passes an explicit protection option. All configured push URLs are handled and
verified.

### Root And Config Resolution

The installer resolves absolute vault and state roots once, exports them immediately,
persists the exact values in an installer-owned profile block, and passes both values
to cron or Task Scheduler. It preserves an explicit custom state root.

On POSIX, an empty, unset, or relative `XDG_CONFIG_HOME` falls back to
`$HOME/.config`; only an absolute value is accepted. OpenCode resolution recognizes
the global directory, `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, JSON, and JSONC. It
loads the documented precedence chain and compares the effective final
`mcp.llm-wiki` entry instead of substring searching. The installer writes only its
selected user-level source. If a later project, custom-directory, inline, or managed
source overrides that entry, installation reports a conflict and does not claim the
integration is active.

### uv And Dependencies

Fresh production provisioning uses the lockfile, excludes default development
groups, and includes the MCP baseline. Re-runs and optional-extra additions are
locked and inexact so they do not remove already selected extras. Hooks, MCP, cron,
and unattended helpers use `uv run --locked --no-sync`.

Core unconditional imports are direct runtime dependencies. Test-only dependencies
needed by the documented full suite are direct development dependencies. Optional
features remain extras, and clean CI proves each advertised environment without
accidental transitive or development packages.

The uv bootstrap is version-pinned. Node 22 is documented as an optional prerequisite
only for qualified precise Python navigation.

### Installer Verification

Mandatory native commands are checked by exit code. PowerShell captures
`$LASTEXITCODE` immediately through one Windows PowerShell 5.1-compatible helper.
The installer smoke gate has a 120-second aggregate deadline. It verifies imports in
the clean production environment, runs Doctor in non-repair JSON mode, and performs
one stdio MCP initialize/list-tools exchange that returns exactly 12 tools. It does
not run the complete regression suite. Any timeout or mandatory failure exits
nonzero and the final summary cannot claim success. The full suite stays in
development and CI.

### Installed-Vault Repair

`scripts/repair_installed_memory.py` is the explicit installed-vault migration
surface. Its default is read-only `--check`; `--apply` is required for mutation. It
reuses the same resumable schema, ownership, capture-intent, and receipt validators as
runtime startup. It never changes Git, deletes knowledge, removes `run/`, or removes
legacy caches and compatibility markers.
After v3 adoption, mutating repair holds canonical role `repair`; the adoption cutover
itself runs only under the separately defined offline exclusive gate.

### Worktree Cleanup

`cleanup_worktrees.py` parses NUL-delimited Git worktree metadata, identifies the
primary worktree from Git rather than the caller's CWD, and limits reporting and
deletion to exact approved agent-worktree roots using resolved containment. Global
`git worktree prune` is removed from normal cleanup and requires a separately named
explicit action.

## CI And Release Evidence

CI pins actions by full commit SHA, pins uv, uses explicit runner generations, and
tests minimum Python 3.10 plus current Python 3.14. It includes:

- Ruff over `scripts`, `tests`, and `benchmark`;
- the full hermetic suite with measured platform timeouts;
- a cold clean production environment without dev dependencies;
- optional MCP and code-graph environments;
- real Pyright qualification with shell-independent state-root passing;
- deterministic benchmark gates with schema-valid retained reports;
- installer smoke and injected native-command failure cases.

Focused jobs have a 15-minute ceiling, clean-environment and installer jobs 20
minutes, Linux full-suite jobs 45 minutes, and Windows full-suite jobs 60 minutes.
Changing a ceiling requires a retained report with per-test durations and p95 job
runtime; a timeout is a failure, not an automatic retry or waiver.

README translations remain synchronized whenever public install or architecture text
changes. `CHANGELOG.md` records the repair under Unreleased. No badge, release URL,
or green claim is updated until the corresponding evidence exists.

## Testing Strategy

Every behavior change starts with a focused failing behavioral test. Presence-only
assertions and source-string checks are replaced where they currently mask invalid
runtime behavior.

Required focused tests include:

- official OpenCode event, message, tool, compaction, and resume shapes;
- provider error, queue error, detached crash, orphan capture recovery, and
  live/deferred parity;
- deterministic duplicate capture, chunk bounds, terminal/discard receipt conflicts,
  aggregate overflow, reserved pending recovery, semantic-decision adoption, 30-day
  retention, and verified legacy-intent transfer;
- three-stage nonterminal decision publication and worker-versus-operator intent/task
  fence races at every precommit/terminal boundary, including incomplete transaction
  recovery and dual-fence partial acquisition;
- direct-user versus AI-derived feedback authority;
- concurrent project SessionStart isolation and final serialized token bounds;
- identical bytes at two logical daily paths and conservative v2 receipt handling;
- payload/hash/dedupe corruption at every queue transition;
- crash-at-every-statement migration recovery;
- live idle worker/compiler/maintenance deletion blockers, dead-proof takeover,
  admission races for capture/project/writer/queue/LSP actors, completed-cutover
  tombstones against pre-v3 clients, every partial path cutover, and Doctor snapshot
  semantics;
- PID-only maintenance-marker interoperability, nested writer projection, and
  digest-only runtime names for hostile identifiers;
- Windows Python 3.10-3.14 path/descriptor identity and SQLite close behavior;
- POSIX directory-sync failure and injected Windows flush/write-through publication
  failure plus every old/new/duplicate recovery state;
- POSIX and Windows process-tree cleanup;
- clean production installation, XDG variants, root persistence, and failed native
  commands;
- deadline propagation, graph no-fallback behavior, MCP pagination, and final output
  budgets.

Completion evidence requires focused suites, structure and i18n guards, Ruff,
compileall, Node syntax, shell syntax, PowerShell AST parsing, the full Windows suite,
and a clean Linux matrix. CI supplies macOS confirmation. A baseline failure is not
waived merely because it predates the repair branch.

## Rollout And Compatibility

- Read old queue rows and v2 receipts; write only new canonical records after the
  explicit offline migration gate succeeds.
- Never rewrite historical v2 schemas or receipts.
- Keep legacy capture files and lock markers as bounded compatibility inputs.
- Fail closed when old authority is ambiguous.
- Do not remove legacy caches, markers, or readers in this repair.
- Doctor reports migration and compatibility state before any destructive action.
- After completed adoption, pre-v3 queue and Markdown clients are tested against
  legacy-path tombstones and cannot reach active v3 databases. Interrupted adoption
  is explicitly offline and resumable; it is not mislabeled as concurrently safe.

## Rejected Alternatives

### Minimal Green-Test Patch

Rejected because it would leave acknowledged capture loss, ambiguous receipt
authority, invisible live owners, and unbounded provider work.

### Runtime Rewrite

Rejected because the repository already contains strong transaction, queue, budget,
and process ownership primitives. Replacing them would increase risk and maintenance
cost without improving the required contracts.

### New MCP Checkpoint Tool

Rejected for this repair because the approved product boundary is exactly 12 tools.
Deterministic hook evidence can be repaired without adding a semantic authority
surface.

### SQLite WAL

Rejected because the local rollback-journal contract is sufficient, simpler, and
compatible with current operational safety assumptions.

## Finding-To-Test Matrix

The retained baseline command is
`uv run --locked --no-sync pytest -q` on Windows/Python 3.14.6. The result summary
and root-cause counts are recorded in Baseline Evidence above. The following IDs are
the closure ledger for implementation; each row requires a test that fails when its
fix is reverted.

| ID | Confirmed defect | Contract section | Required regression evidence |
|---|---|---|---|
| B1 | Path `stat` ctime differs from descriptor `fstat` ctime on Windows/Python 3.14 | Platform And Filesystem Identity | `test_prepare_and_apply_create_replace_delete` plus new path/descriptor 3.10-3.14 matrix |
| B2 | Python `st_dev` is wider than legacy Win32 volume serial | Platform And Filesystem Identity | `test_repository_contracts_are_frozen_slotted_normalized_and_deterministic` plus `FILE_ID_INFO` identity tests |
| B3 | `jsonschema` is missing from the documented development environment | uv And Dependencies | clean locked schema-validation tests |
| B4 | `numpy` is missing from the advertised hybrid environment | uv And Dependencies | clean locked vector tests |
| B5 | tree-sitter or Jedi dependencies are missing from code-graph lanes | uv And Dependencies | clean locked code-graph tests per advertised extra |
| B6 | MCP is missing from the production baseline | uv And Dependencies | no-dev stdio MCP initialize/list-tools smoke |
| B7 | SQLite read connections survive transaction context exit | SQLite Resource Lifetime | Windows replace/unlink tests in generation and search suites |
| B8 | Two MCP timeout tests leak late work under full-suite load | Testing Strategy | event/barrier-based timeout tests plus pollution sentinel |
| B9 | Windows directory sync silently returns without a durable metadata contract | Durable Metadata Publication | injected flush/move failure and old/new/duplicate crash-state tests |
| O1 | Unsupported direct OpenCode lifecycle hook keys | OpenCode 1.18.13 | official `event.properties` Node harness |
| O2 | Direct user messages are not captured | OpenCode 1.18.13 | awaited `chat.message` intent test |
| O3 | Tool `args`, result output, and `apply_patch` are ignored | OpenCode 1.18.13 | two-argument tool hook with changed-path and failure evidence |
| O4 | Role, synthetic, ignored, and reasoning provenance is flattened | Durable Capture Intent | role-preserving message fixture and exclusion tests |
| O5 | Idle handoff and orphan recovery can monopolize host startup | OpenCode 1.18.13 | 1-second handoff and reserved-pending 32-item/8-MiB/1-second recovery tests |
| C1 | Provider/queue failures can be counted as semantic OK | Flush Outcomes And Deferred Parity | exact grammar and every closed outcome test |
| C2 | Detached or fire-and-forget capture can lose ownership | Durable Capture Intent | killpoint matrix from intent fsync through terminal receipt |
| C3 | Deferred handling differs from live handling | Flush Outcomes And Deferred Parity | same-intent live/deferred parity test |
| C4 | AI summaries can be promoted as user authority | Feedback Authority | direct-user versus AI-derived promotion test |
| C5 | Duplicate host delivery can create conflicting queue identities | Durable Capture Intent | deterministic ID/path/bytes and exact queue duplicate test |
| C6 | Discard and legacy transfer have no durable terminal proof | Durable Capture Intent | UID/SID discard, conflict, retention, and legacy transfer killpoint tests |
| C7 | Provider output can change after commit-before-terminal crash | Durable Capture Intent | immutable decision-first and committed-transaction adoption killpoints |
| C8 | One source event can create unbounded capture work | Durable Capture Intent | raw traversal/16-MiB encoding plus 8-MiB/2,048-item/8-chunk overflow and retry-coalescing tests |
| C9 | Terminal and decision retention can diverge or lose replay authority | Durable Capture Intent | terminal binding, ordinary-purge exclusion, whole-run reset, and undo-window tests |
| C10 | Operator discard can race a worker's later Markdown commit | Durable Capture Intent | per-intent worker/operator fence and commit-precondition killpoints |
| C11 | Discard can hide a recoverable incomplete Markdown transaction | Durable Capture Intent | absent/aborted/committed/incomplete/conflicted transaction disposition tests |
| C12 | Malformed pending capture has no durable resolution path | Durable Capture Intent | fixed quarantine manifest, restore/discard, retention, and deletion-blocker tests |
| S1 | SessionStart can infer another project's global state | SessionStart And Token Contract | concurrent same-second multi-project isolation test |
| S2 | Combined SessionStart bypasses prior marker/bootstrap/catch-up behavior | SessionStart And Token Contract | preserved route behavior and repeated-install tests |
| S3 | Context budget does not cover final repeated serialization | SessionStart And Token Contract | final serialized budget and no-duplicate-text test |
| R1 | Digest-only receipts alias two logical dailies | Compile Receipt V3 | identical bytes at two paths, v2 ambiguity, and v3 archive test |
| R2 | Compile sends an unbounded source blob twice | Compile Receipt V3 | provider-budget batch and per-source disposition test |
| Q1 | Queue hash is stored but not enforced | Queue Task V3 | corruption tests at every enumerated insertion/lease/terminal/operator transition |
| Q2 | Dedupe aliases different work | Queue Task V3 | exact duplicate versus kind/version/payload conflict test |
| Q3 | Published queue schema differs from production export | Queue Task V3 | production round-trip validation against `queue-task/v3` |
| Q4 | Corrupt queue rows have no safe terminal disposition | Queue Task V3 | quarantine freeze, paged fan-out export and purge resume, full CAS, retention, and lineage tests |
| Q5 | Untrusted IDs can escape or exceed result path components | Queue Task V3 | fixed 64-hex path keys with traversal, reserved-name, case, and 256-byte IDs |
| Q6 | Nonterminal semantic stages cannot share the queue's single terminal result slot | Queue Task V3 | three-stage decision ledger, lease-expiry non-adoption, and terminal-only result tests |
| Q7 | Corrupt payload cannot prove which intent fence it references | Queue Task V3 | immutable capture-task link agreement, ambiguity blocker, and export-only tests |
| M1 | `executescript()` commits before the assumed transaction | Atomic And Resumable SQLite Migrations | crash-at-every-statement migration tests |
| M2 | Column-presence checks can leave partial backfills permanent | Atomic And Resumable SQLite Migrations | historical partial-schema repair fixtures |
| L1 | Idle workers, compile, and scheduled maintenance can be invisible to Doctor | Operational Ownership | real actor lifetime and `run/` deletion blocker tests |
| L2 | Legacy and fenced ownership can race | Operational Ownership | old/new acquisition order, rollback, takeover, and Doctor self-token tests |
| L3 | A pre-v3 process can restart after completed adoption | Operational Ownership | old queue and Markdown clients fail on legacy-path tombstones and cannot alter v3 databases |
| L4 | Cross-database adoption and deletion checks can expose mixed snapshots | Operational Ownership | crash after every rename/fsync, exact repair resume, all-role admission races, dead-proof takeover, and non-permit Doctor results |
| L5 | Nested writers can reacquire or release their caller's lease | Operational Ownership | direct versus parent-referencing nested writer transaction tests |
| L6 | A tokenized maintenance marker is deleted by v2 PID-only parsers | Operational Ownership | frozen v2 nightly/weekly parser interop and exact marker-identity release tests |
| P1 | Provider fallback resets timeout for every candidate | LLM Calls | one absolute-deadline multi-provider test |
| P2 | HTTP, CLI, and output-file reads are unbounded | LLM Calls | overflow cleanup tests for every provider transport |
| P3 | Queue and maintenance subprocesses do not own full supported trees | Process Trees | Windows Job and POSIX group timeout tests |
| G1 | Graph tools silently run an unbounded live scan | Graph And Navigation | absent-generation no-live test and bounded `live=true` test |
| G2 | Grounded QA can accumulate untracked provider threads | Session Maintenance And Model Loading | bounded straggler-cap test |
| G3 | Navigation computes far more results than it renders | Graph And Navigation | provider-call and discovery-cache bounds |
| G4 | Failed optional model loads repeat without a closed cache bound | Session Maintenance And Model Loading | single-flight, 128-entry LRU, and 60-second expiry tests |
| MCP1 | `read_page` can return multi-megabyte content without pagination | MCP Output | byte-window, UTF-8 boundary, digest, EOF, and stale tests |
| MCP2 | `get_context` scans the whole corpus and repeats text | MCP Output | direct-slug I/O bound and final package budget tests |
| I1 | Pipe install can target caller CWD and nonexistent release refs | Bootstrap And Git Safety | isolated pipe-mode full-OID tests |
| I2 | Mandatory installer failures still exit successfully | Installer Verification | Bash and PowerShell native-command injection tests |
| I3 | XDG/OpenCode precedence and root/state persistence are wrong | Root And Config Resolution | unset/relative/absolute/override and scheduler-env tests |
| I4 | Exact sync removes selected extras and unattended `uv run` mutates env | uv And Dependencies | fresh and repeated environment inventory tests |
| I5 | Worktree cleanup can delete out-of-scope worktrees | Worktree Cleanup | primary, containment, and no-global-prune tests |
| I6 | Installed-vault repair surface is absent | Installed-Vault Repair | default check-only, explicit apply, and non-destructive tests |
| CI1 | Actions/tools/runners are mutable and timeouts are too short | CI And Release Evidence | workflow policy tests and measured timeout evidence |
| CI2 | Clean production dependency lane is absent | CI And Release Evidence | no-dev MCP smoke on Python 3.10 and 3.14 |
| ML1 | Reranker defaults to mutable `main` despite no selected winner | Session Maintenance And Model Loading | no-default, immutable explicit revision, and local-only tests |
| D1 | Docs claim unavailable release URLs, green CI, or implemented target state | CI And Release Evidence | EN/RU/ZH parity and claim-evidence guards |

## Sources

- OpenCode 1.18.13 plugin contract: https://github.com/anomalyco/opencode/blob/v1.18.13/packages/plugin/src/index.ts
- OpenCode 1.18.13 dispatch: https://github.com/anomalyco/opencode/blob/v1.18.13/packages/opencode/src/plugin/index.ts
- OpenCode server API: https://opencode.ai/docs/server/
- OpenCode configuration: https://opencode.ai/docs/config/
- Python 3.14.6 `os`: https://docs.python.org/3.14/library/os.html
- CPython Windows stat implementation: https://github.com/python/cpython/blob/v3.14.6/Modules/posixmodule.c
- Microsoft `FILE_ID_INFO`: https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info
- Microsoft Job Objects: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
- Microsoft `MoveFileExW`: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw
- Microsoft `FlushFileBuffers`: https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers
- Python `sqlite3`: https://docs.python.org/3.14/library/sqlite3.html
- Python SQLite backup API: https://docs.python.org/3.14/library/sqlite3.html#sqlite3.Connection.backup
- SQLite transactions: https://sqlite.org/lang_transaction.html
- SQLite `trusted_schema`: https://sqlite.org/pragma.html#pragma_trusted_schema
- SQLite WAL-reset notice: https://sqlite.org/wal.html#walresetbug
- uv locking and syncing: https://docs.astral.sh/uv/concepts/projects/sync/
- XDG Base Directory Specification 0.8: https://specifications.freedesktop.org/basedir-spec/latest/
- GitHub Actions hardening: https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions
