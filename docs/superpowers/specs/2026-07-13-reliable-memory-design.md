# Reliable Memory Design

## Status

Approved scope: Stage 2 of the user-approved 2026-07-13 roadmap. The user
explicitly authorized implementation after requiring current-practice research.

## Goal

Make every automatic memory mutation recoverable, idempotent, evidence-backed,
and safe under concurrent local agents and process crashes. Markdown remains the
knowledge source of truth. Runtime databases coordinate work but never replace
the durable Markdown record.

## Research Basis And Decisions

The design applies these current practices as of 2026-07-13:

- SQLite atomic commit and rollback journals for short local coordination
  transactions.
- Transactional-outbox semantics: durable plan first, idempotent relay second.
- At-least-once queue delivery with lease fencing and idempotent handlers.
- Durable-execution checkpoints: persist input and intermediate results before
  side effects, then resume from the last committed cursor.
- Content-addressed action caches whose key includes every effective input.
- BagIt-style immutable archive packages with cryptographic manifests.
- Atomic claims with literal evidence verification before semantic evaluation.
- Selective classification: uncertain or evaluator-disputed claims are
  quarantined instead of being published or destructively superseding knowledge.

The bundled Python runtime currently uses SQLite 3.50.4. SQLite documents a rare
WAL-reset corruption bug through 3.51.2, fixed in 3.51.3 and backported to 3.50.7.
All new operational databases therefore use rollback-journal mode,
`synchronous=FULL`, short `BEGIN IMMEDIATE` transactions, and bounded busy
timeouts. WAL is forbidden until runtime version gating recognizes a fixed build.

LangGraph and Temporal informed checkpoint semantics, but Stage 2 does not add a
framework dependency. The required reducer and state are small, local, and easier
to audit directly. No persistent worker or daemon is introduced.

## Architecture

### 1. Recoverable Markdown Transactions

All automatic writes to `knowledge/daily/`, `knowledge/notes/`,
`knowledge/projects/`, `knowledge/inbox/claims/`, `knowledge/index.md`, and
`knowledge/log.md` pass through one transaction API. Each target operation is
`create`, `replace`, or `delete`; the literal sentinel `absent` is the before- or
after-state for a path that must not exist.

A transaction has four phases:

1. `preparing`: capture source snapshots, target before-hashes, intended
   after-hashes, and operation metadata without holding the writer gate during
   LLM calls.
2. `prepared`: write after-images and before-images, fsync them, validate schema,
   evidence, links, and hashes, then durably mark the plan prepared.
3. `applying`: acquire the global writer gate, compare every before-state, and
   atomically create, replace, or remove target files from same-directory unique
   temporary files. Creates use a no-clobber primitive rather than `os.replace`.
4. `committed`: verify every after-state, fsync directories where supported, and
   write the coordinator commit row atomically. Any `knowledge/log.md` entry is
   part of the prepared after-image; it is never appended after hash verification.

The coordinator lives at `run/markdown-transactions.sqlite3`; transaction
artifacts live under `run/transactions/<transaction-id>/`. Staged replacement
files are always created in the target directory so `os.replace` stays on one
filesystem even when `LLM_WIKI_STATE_ROOT` is on another disk.

The SQLite coordinator stores transaction state and hashes, not authoritative
knowledge. Before/after Markdown bytes are retained for 30 days for undo and
crash recovery. Automatic Git operations are forbidden.

Recovery runs before every mutation and during `doctor`/SessionStart:

- `preparing` without a valid prepared plan is discarded without touching
  targets. A staged create whose target now exists is a conflict; a staged
  delete succeeds only when the target still has the recorded before-hash.
- `prepared` or `applying` normally rolls forward from verified after-images.
- A target already matching its after-hash is a completed idempotent step.
- A target matching neither before nor after hash is a conflict; unknown user
  content is never overwritten.
- A corrupt after-image triggers rollback only for targets still matching known
  transaction hashes. Otherwise the transaction is quarantined as a conflict.
- Undo is a new forward transaction and is allowed only while all current targets
  match the original after-hashes.
- Transaction-specific preconditions, including project lease token and fencing
  epoch, are persisted in the prepared plan and rechecked on every normal or
  recovery apply. An obsolete checkpoint transaction is quarantined without
  appending its journal event or applying its projection.

There is no portable atomic swap for several fixed-path Markdown files. Internal
readers use the writer gate for a coherent before-or-after view. External editors
may briefly observe a mixed tree during replacement, but recovery converges to a
verified complete state. Hash/CAS safety is guaranteed only for cooperating
transaction-API writers. Concurrent mutation by an external editor is unsupported
and best-effort detected by after-state verification; a detected mismatch is
quarantined and is never repaired by overwriting the unknown bytes.

Compile planning snapshots the exact daily bytes sent to the model. The compiler
records that snapshot hash, never a later live-file hash. Daily appends that occur
during an LLM call therefore remain pending for the next compile.

### 2. Durable Project Checkpoints

Each project gains an append-only `knowledge/projects/<slug>/journal.md` beside
the generated `state.md`. The journal is durable Markdown and contains compact,
machine-readable checkpoint events with:

- occurrence ID and idempotency key;
- monotonic project sequence;
- agent, session, worktree, branch, and source event provenance;
- trigger and checkpoint reason;
- structured delta for goal, phase, current task, next actions, decisions,
  blockers, changed files, commands, and verification;
- evidence event IDs and the last applied sequence.

`state.md` is a deterministic projection of this journal. One short-lived process
holds a fenced project lease while advancing the projection. Lease rows live in
`run/markdown-transactions.sqlite3` and contain project slug, random lease token,
monotonic fencing epoch, owner, expiry, and last heartbeat. Journal sequence
allocation and projection preparation compare token, epoch, and unexpired lease
inside one short coordinator transaction. The final writer-gate transaction
rechecks those values before applying the projection, so a stale owner cannot
commit after a newer fencing epoch. Reapplying the same delta is a no-op through
stable task/decision/blocker IDs and upsert/close semantics.

Checkpoint triggers are deterministic:

- before compaction and after compaction confirmation;
- token estimate at 60%, then each additional 10%, forced at 80%;
- explicit decision, user correction, new blocker, or blocker resolution;
- task completion, cancellation, ownership transfer, or significant failure;
- significant file/public-contract/test-status change;
- ten dirty minutes on the next observed event, with a thirty-minute threshold
  also evaluated only on the next observed event;
- dirty Stop/session-idle and SessionEnd;
- SessionStart recovery.

There is no wall-clock guarantee while no hook, command, or agent process runs.
Token thresholds and compaction confirmation apply only when a host supplies
those signals. Otherwise every 20th significant lifecycle event is the exact
fallback trigger. A significant event is a decision, correction, blocker change,
task lifecycle change, ownership transfer, failed command, successful mutation,
public-contract change, or verification-result change; repeated reads and
unchanged status messages do not count. Ordinary events use a 30-second debounce.
Compaction, decisions, task completion, failure, ownership transfer, and
SessionEnd bypass it.

SessionStart recovers pending journal events before injecting a handoff. The
rendered handoff remains bounded and contains only active goal/task, up to three
next actions, blockers, recent decisions, and MCP identifiers for deeper reads.

### 3. Safe Daily Archive And Evidence Resolution

Eligible daily logs move into immutable BagIt-style bags under
`knowledge/daily/archive/YYYY-MM/bag-<timestamp>-<id>/`. Initial archives remain
uncompressed for direct evidence slicing. Compression is outside Stage 2.

Every bag contains `bagit.txt`, `bag-info.txt`, payload under `data/`, a SHA-256
payload manifest, canonical `archive-manifest.json`, and a SHA-256 tag manifest.
The archive manifest records logical daily ID, original path, source hash, payload
hash, compile receipt, terminal operation IDs, exact evidence block byte/line
spans and hashes, queue preflight, pins, and retention. Sealed bags are never
modified. `archive-index.json` is derived from valid bags and can be rebuilt.
Publication builds a uniquely named hidden sibling directory under the final
archive parent, fsyncs and validates the complete bag, then uses one atomic
directory rename to expose the final bag. The resolver verifies the published bag
before the flat source is removed through the Markdown transaction API. Recovery
accepts a temporary flat/archive duplicate only when logical ID and source hash
match, then finishes the source removal; any mismatch quarantines the archive
operation without deleting either copy.

A daily log is eligible only when all conditions pass under the writer gate:

- older than the 90-day hot retention period and not today;
- current hash equals a completed compile receipt;
- every compile operation is terminal;
- every evidence span resolves and matches its hash;
- no ready, leased, blocked, or legacy queue task references it;
- no active transaction or writer references it;
- no decision evidence, uncompiled content, failure, or manual pin applies.

Archive means move, never delete. Decision evidence and uncompiled/failed sources
remain pinned in the flat daily directory.

Evidence references become logical and content-addressed:
`daily:<date> sha256:<hash> block:<id> bytes:<start>-<end>`. The shared resolver
checks the flat source first, then validated archive bags, and fails closed on
ambiguity or hash mismatch. Compile, lint, MCP evidence reads, and archive
validation use the same resolver.

### 4. Versioned Compile Action Cache

The cache stores only a validated normalized compile plan. It never caches final
Markdown, timestamps, index output, or audit-log writes.

The action descriptor includes compiler semantic version, compile-plan schema
version and hash, normalization version, all plan-affecting feature flags, and an
ordered descriptor for each draft and critique model call. Each call descriptor
contains prompt-program hash, resolved provider and model, capability profile,
inference settings, structured-output mode, and actual fallback result. The source
manifest is a sorted list of logical path, byte length, and SHA-256 tuples covering
selected daily snapshots, active notes, generated knowledge snapshot, agent
contract, and log tail; its canonical hash is part of the action descriptor.
Cache selection follows provider resolution one candidate at a time. After a
provider availability probe succeeds, the compiler computes and checks only that
provider/model key. If the call fails and policy permits fallback, it repeats the
probe and key lookup for the next provider. A restored preferred provider therefore
selects its own key and cannot accidentally reuse an older fallback entry.

The physical key is SHA-256 over restricted canonical JSON. Absolute paths and
source text never appear in filenames. Cache entries live under `cache/compile/`,
use owner-restricted permissions, carry a payload digest, and are never shared
remotely by default.

Unknown or implicit model identity disables persistent cache hits. Provider
fallback recomputes the key. Successful empty plans are cacheable; provider,
parse, critique, schema, evidence, or path failures are not. Every cache hit
repeats deterministic validation before application.

Legacy `compiled_daily_hashes` entries are preserved diagnostically but cause one
v2 recompile. New receipts include source digest, action key, completion state,
operations, and evidence integrity. The internal compile plan is validated
against a committed JSON Schema. Provider-native structured output is used when
supported; prompt-only JSON remains a capability-marked fallback.

### 5. Priority Queue, Leases, And Dead Letter

The file-per-task queue is migrated once into `run/queue.sqlite3`. The database
uses rollback-journal mode and stores task kind and handler version, canonical
redacted payload and input hash, optional dedupe key, state, priority,
`available_at`, attempts, lease token/expiry/heartbeat, stable error code,
blocked capability, result reference, and attempt history.

States are `ready`, `leased`, `blocked`, `succeeded`, `dead`, and `cancelled`.
Claims occur in a short `BEGIN IMMEDIATE` transaction ordered by priority,
availability, and FIFO creation time. Priority is an integer from -100 to 100,
defaults to 0, and higher numbers run first. Handlers execute outside the
transaction.
Every heartbeat, result publication, and acknowledgement requires
`state='leased'`, the random lease token, and an unexpired lease. Results are
published under a stable operation ID before the fenced acknowledgement, so a
stale or duplicate worker cannot replace a completed result.

Delivery is explicitly at least once. Handler side effects use event/operation IDs
for idempotency. The default lease is 120 seconds with heartbeat every 40 seconds.
Retry allows 8 attempts and uses full-jitter exponential backoff with 30-second
base and 3600-second cap, unless a valid provider `Retry-After` is longer.
Dependency failures move to `blocked` without consuming retry budget. Permanent
input/version failures and exhausted retries move atomically to dead-letter state
and are never deleted automatically. Operators and MCP clients can inspect dead
tasks, cancel nonterminal tasks, or redrive a dead task as a new task linked to
the original; attempt history is never reset in place.

Succeeded and cancelled task results have a default 30-day retention period.
Dead tasks are retained indefinitely. Explicit `memory_queue.py purge` requires a
terminal cutoff and an export path, writes a canonical manifest plus task/result
records, verifies the export hash, and only then removes selected terminal rows and
result files. No automatic purge occurs, and any retained task or result blocks
deletion of `run/`.

`doctor --repair` unblocks only tasks whose named capability was repaired, then
starts the short-lived worker. By default the worker exits after 20 tasks, 600
seconds, or 2 idle seconds. No daemon runs continuously.

Existing `run/queue/*.json` and `.processing` tasks are imported under an
exclusive migration gate. Migration first proves that no legacy owner is active,
quiesces legacy claims, imports valid records with attempts and timestamps, and
quarantines malformed records without exposing payloads. It then commits
`run/queue-migrated-v2` before enabling SQLite claims. All upgraded legacy enqueue
and worker entry points check that marker and refuse file-queue writes. If a live
legacy owner cannot be excluded, migration aborts without enabling SQLite; two
supported writable queue backends never coexist.

### 6. Claim-Level Contradiction Detection

Durable pages may include one restricted-canonical-JSON claim ledger under
`## Claims`. Claims are components of existing OKF page types, not a new page
type. Each atomic claim records immutable ID and normalized fingerprint, text,
subject, controlled relation, typed value, qualifiers, half-open validity
interval, observed time, lifecycle status, confidence, source authority, exact
evidence span/hash, claim links, and extractor/schema versions.

The committed schemas are `scripts/schemas/claim-ledger-v1.json`,
`scripts/schemas/claim-candidate-v1.json`, and
`scripts/schemas/claim-relations-v1.json`. Relations are schema-enumerated as
`equals`, `has-state`, `has-value`, `member-of`, `located-at`, `starts-at`,
`ends-at`, `uses`, and `depends-on`; producers may only add a relation through a
schema-version change. Values are typed as string, number with unit, boolean,
entity ID, date, or timestamp. Qualifiers are canonical key/value pairs from the
same scalar types. A substantive claim is active, evidence-backed, and has a
relation other than descriptive metadata; titles, summaries, links, provenance,
and `mentions`-style observations are not substantive.

The pipeline is ordered and fail-closed:

1. Split source into immutable timestamp blocks.
2. Extract atomic claims through a strict schema.
3. Verify literal UTF-8 evidence bytes, range, and hash.
4. Apply deterministic scalar/entity/relation normalization.
5. Retrieve candidates from a derived SQLite claim index.
6. Resolve deterministic equality, interval, functional-relation, authority,
   and lifecycle cases.
7. Run evidence-conditioned semantic evaluation only for unresolved pairs.
8. Run a blind critique with a distinct evaluator identity when available;
   otherwise use an isolated second prompt/program call that receives evidence
   and the proposed label but not the first evaluator's rationale.
9. Let Python apply `refine`, `supersede`, `keep-both`, or `quarantine` policy.

Until a frozen benchmark demonstrates at most 1% false supersession, semantic
conflicts never destructively supersede claims. Evaluator disagreement, malformed
output, unsupported evidence, or low confidence creates a durable inactive
candidate under `knowledge/inbox/claims/`. Each candidate is transaction-written
Markdown with `type: claim-candidate`, inactive/quarantined status, provenance,
and one embedded record valid against `claim-candidate-v1.json`; candidates are
excluded from normal retrieval. Deterministic supersession still requires
equal-or-higher source authority and overlapping scope/time. A ledgerless page is
retrieval-only context and can never be automatically superseded.

A page becomes superseded only when all substantive claims are superseded.
`check_contradiction` keeps its current string input but returns structured claim
assessments, evidence, validity, and recommendations. The frozen contradiction
benchmark measures candidate recall, classification and lifecycle F1, false
supersession, quarantine risk/coverage, and provenance correctness.

## Runtime And Deletion Contract

`cache/` and `logs/` remain safely regenerable. `run/` now contains operationally
significant transactions and queued work. Deleting `run/` is allowed only after
`doctor` reports no nonterminal, conflicted, or quarantined transaction, no
transaction within the 30-day undo-retention window, and no retained queue task
or result, and no live project lease, writer, queue worker, or maintenance owner.
Deleting eligible committed artifacts loses undo history. Installers and repair
commands must never silently remove `run/`.

Runtime SQLite files require a local filesystem with correct locking. Network and
cloud-synchronized state roots are unsupported. Known network paths and failed
SQLite locking probes are rejected; cloud-folder detection is best-effort and
cannot identify every synchronization product. `LLM_WIKI_STATE_ROOT` may remain
on another local disk; target staging occurs beside each Markdown target.

## Configuration Defaults

Stage 2 has deterministic bounded defaults: SQLite busy timeout 10 seconds for
Markdown coordination and 5 seconds for queue operations; transaction undo and
artifact retention 30 days; archive hot retention 90 days; project lease 30
seconds with 10-second heartbeat; ordinary checkpoint debounce 30 seconds;
checkpoint event fallback 20 significant events; queue lease/heartbeat 120/40
seconds; queue attempts 8; retry base/cap 30/3600 seconds; worker task/time/idle
limits 20/600/2; queue priority range -100..100; succeeded/cancelled result
retention 30 days and dead-task retention unlimited. Test-only constructors may
override all values. Runtime commands
expose explicit CLI flags for archive retention, transaction retention, worker
limits, and queue retry/lease policy; no new environment variables are added.

## Failure Handling

- Unknown user bytes detected by transaction recovery are never overwritten.
- No LLM call runs while the global Markdown writer gate is held.
- No semantic evaluator directly mutates lifecycle state.
- No dead-letter task is silently deleted.
- No final archive bag is exposed before all hashes validate.
- No cache hit bypasses deterministic validation.
- No claim reaches entailment before literal evidence verification.
- No automatic operation manipulates Git staging, commits, remotes, or branches.
- Health output contains IDs, counts, states, and stable error codes, never
  secret-bearing payloads.

## Testing Strategy

- Kill transaction processes at every prepare/apply/commit/undo boundary.
- Race daily append against a blocked fake-LLM compile.
- Race multiple agents, queue workers, projectors, archive, and recovery.
- Verify lease fencing, retry timing, priority/FIFO, dead-letter, migration, and
  doctor redrive.
- Validate BagIt completeness, path safety, deterministic manifests, pins,
  flat/archive evidence equivalence, and crash recovery.
- Golden-test every compile-cache key dimension and provider fallback.
- Benchmark claim extraction, candidate recall, contradiction class, lifecycle
  recommendation, false supersession, and quarantine coverage.
- Exercise Windows sharing/reparse behavior and POSIX fsync/symlink behavior.
- Preserve the existing retrieval legacy gate.

## Explicit Non-Goals

- No UI, dashboard, cloud service, remote queue, or persistent daemon.
- No SQLite knowledge database or graph database as source of truth.
- No exactly-once claim for arbitrary external side effects.
- No automatic Git commits for runtime transactions.
- No eager semantic backfill of every existing page.
- No semantic automatic supersession before benchmark calibration.
- No gzip archive tier in the first implementation pass.
- No WAL on the current SQLite runtime.

## Primary Sources

- SQLite atomic commit: https://www.sqlite.org/atomiccommit.html
- SQLite transactions: https://www.sqlite.org/lang_transaction.html
- SQLite WAL and 2026 WAL-reset bug: https://www.sqlite.org/wal.html
- Transactional outbox: https://microservices.io/patterns/data/transactional-outbox
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- Temporal workflow execution: https://docs.temporal.io/workflow-execution
- AWS SQS visibility timeout: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html
- AWS SQS dead-letter queues: https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html
- RFC 8493 BagIt: https://www.rfc-editor.org/rfc/rfc8493.html
- RFC 8785 JSON Canonicalization: https://www.rfc-editor.org/rfc/rfc8785.html
- W3C PROV-DM: https://www.w3.org/TR/prov-dm/
- W3C OWL-Time: https://www.w3.org/TR/owl-time/
- FEVER: https://aclanthology.org/N18-1074/
- VitaminC: https://aclanthology.org/2021.naacl-main.52/
- FActScore: https://aclanthology.org/2023.emnlp-main.741/
