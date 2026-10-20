# Durable compile progress: approved design and storage implementation

Status on 2026-10-19: storage implementation in an isolated development branch.
Not installed; no active queue schema migration or complete compile qualification.
The owner approved the revised one-journal design after an explicit architecture
question. The private decision is
`knowledge/notes/compile-durable-progress-decision-20261019.md`.

## Problem and design

A queue task can retain a terminal result, but it does not retain the compiler's
intermediate model answers and validation progress. Process-local repair feedback
and disposable validated-plan cache do not provide durable resumption. Existing
Markdown transactions and receipts already protect publication; replacing them
would lose useful guarantees without addressing this gap.

Use one append-only `compile_work_events` table inside the existing queue database.
Current progress is its latest event for a work identity, queried through the
composite primary key. A separate current-state projection was rejected: an
isolated metadata experiment found an indexed lookup sufficient at the measured
workload, without another synchronized state. This is evidence for that storage
choice, not a throughput or token-efficiency claim for the whole compiler.

Research checked on 2026-10-19:

- [SQLite atomic commit](https://sqlite.org/atomiccommit.html): retain rollback
  journal and FULL synchronization under the local-filesystem contract.
- [SQLite constraints](https://www.sqlite.org/lang_createtable.html): explicit
  NOT NULL, primary-key and foreign-key protection; no implicit null assumption.
- [AWS retry guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/retry-backoff.html):
  distinguish transient failures from unchanged invalid inputs and unknown outcomes.
- [Temporal workflow execution](https://docs.temporal.io/workflow-execution):
  durable progress is required for resumption; no Temporal runtime is introduced.

The existing Codex, Claude, OpenCode, OpenAI-compatible and Ollama adapters do not
expose verified recovery of a lost response. A durable dispatch with no recoverable
answer must therefore block automatic repeat expense until authorized. CLI restart
or provider fallback is not that authorization. This policy is approved but its
model-call integration is not yet implemented.

## Implemented storage boundary

`compile_progress.py` defines checkpoint records and migration SQL without opening
or creating a database. Append requires a caller-owned transaction, immutable work
and source identity, and the next sequence. Records are canonical JSON with a
whole-event digest. Reading rejects corrupt records. SQL prevents updates and
unqualified deletion; retained records prevent task deletion through a foreign key.
These checks do not replace semantic transition validation or publication receipts.

The adopted queue backend exposes checkpoint read/write through its existing
verified lease and payload boundary. Checkpoints cannot name another task and only
compile tasks can append them. They do not bind a terminal result or acknowledge
queue delivery.

Intermediate answers use the existing owner-only queue result directory. Their
operation identity is determined by work identity and sequence before writing, so
recovery can locate a fully written answer whose checkpoint transaction never
committed. Writing precedes recording its digest/reference. Missing files do not
become empty successful answers; changed data and escaping references are refused.
A fence failure after writing leaves evidence, not a false completed checkpoint.

The existing queue result byte bound is retained in these uninstalled APIs; its
resource justification remains part of the mandatory limit audit before activation.
No new numeric workload ceiling, dependency, service or runtime root is introduced.
The model, validation and source/claim requirements are unchanged.

## Related defect reproduced and fixed

The shared `_write_durable_file` previously compared an already-existing target
with `Path.read_bytes()` and then checked permissions following the path. An
owner-only symlink to equal bytes could be accepted; comparison also read an
arbitrarily large existing file before discovering a conflict. Two tests reproduce
these defects on the original implementation.

The shared conflict path now uses the existing stable owner-only reader with the
expected payload length as its bound. A longer file cannot be equal, so this is
an exact comparison requirement, not an arbitrary size ceiling. Symlinks, changing
files and permission failures remain conflicts. Export and recovery callers share
the fix; no parallel implementation was added.

## Evidence and verification scope

New tests cover reopen, stale sequence, immutable source identity, corruption,
rollback, abrupt process exit before and after commit, foreign-key retention,
invalid JSON values, missing schema, lease loss, payload mutation, task kind,
artifact corruption, missing answers and the file/event crash boundary.

The first journal test run failed to import the absent feature, and the queue API
and artifact tests failed against absent methods. These establish missing-feature
coverage, not proof that the entire previous compiler failure is now resolved.
The two durable-file regressions reproduced actual behavior in the existing helper.
All test databases and answer artifacts are isolated from the working vault.

Exact test counts, commands and complexity measurements are recorded in the local
execution report. Python 3.10 is checked as well as the installed Python version.
Native Windows/macOS lifecycle behavior and full real-model compilation remain
unqualified. No useful-cycle cost or performance improvement is claimed yet.

## Full health and recovery certification

Full queue certification now streams the optional compile journal in primary-key
order, verifies its schema and immutability triggers, event hashes, source identity,
sequence continuity, and every referenced answer through the existing owner-only
artifact reader. Full doctor, staged backup validation and the runtime deletion
guard share this audit. Missing or changed answers fail validation. Retained events
block runtime deletion; that finding alone does not block a backup.

The journal is optional only for the current pre-migration schema. Migration must
make its presence mandatory through the versioned schema contract.

Page-based queue, coordinator and blackboard checks no longer apply the 256 MiB
whole-file read ceiling. They retain caller deadlines and filesystem, ownership
and database-contract validation. Actual whole-file artifact read bounds remain.
Regression tests reproduced ignored journal damage, missing retention accounting
and the inappropriate ceiling on each of these three page-based checks.

## Dependencies blocking activation

### Adoption envelope and compatible readers (2026-10-19)

For the approved offline migration, use a v2 envelope at the existing adoption
record path. Embed the complete original v1 record plus its canonical digest;
bind the new queue identity, queue schema digest, operation identity and verified
backup manifest digest. Validate the original migration and tombstones through
the existing validator, substituting only the queue file identity/version for
active-file validation. Old clients reject the v2 record. New clients require
the exact version named by the applicable adoption record; accepting a v4 file
alone must never bypass adoption. The coordinator remains v3.

This avoids rewriting every old migration/tombstone reference or adding permanent
sidecar activation pointers. The envelope is a completed-migration receipt, not an
in-flight journal or a substitute for backup verification. Publication must still
block admission while incomplete, preserve preimages and recover after interruption.
The JSON reader budget is the existing 64 KiB budget per component: original record
plus fixed upgrade envelope. This bounds metadata parsing, not the SQLite database.

Sources rechecked on 2026-10-19: [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html),
[Python filesystem replacement](https://docs.python.org/3/library/os.html#os.replace)
and [PostgreSQL upgrade guidance](https://www.postgresql.org/docs/current/upgrading.html).
One SQLite commit does not atomically replace an external JSON file. Therefore
schema completion alone cannot be reported as a completed installed migration.

The development implementation now reads both envelopes, checks all original v1
evidence, and pins each adopted queue client to the version declared by its record.
All queue ownership connections use that same opener. A cached successful admission
does not authorize a later downgrade: existing and newly opened clients reject it.
Full adoption certification additionally audits retained compile artifacts, while
ordinary admission checks contracts without scanning all event history.

Admission tests manually construct a migrated fixture and a v2 envelope. The backup
manifest digest in that fixture is test data, not proof of a real verified backup.
The fixture's file replacement is not a production publication/recovery procedure.
Those procedures remain unfinished and this code is not installed on the live vault.
The old v1 reader remains necessary for unmigrated installations and historical
adoption evidence embedded in v2. It is not a duplicate compile controller; removing
it before migrating those consumers would break admission and recovery.

### Candidate schema migration (2026-10-19; isolated implementation)

The approved offline migration will first qualify a separate queue copy. Its
schema step uses one SQLite transaction for all journal objects and user_version
4. Version 4 is the next queue schema identity, not a new database path or a change
to the coordinator. Existing version-3 clients must refuse the candidate. The
candidate API must reject the active queue path and aliases to its inode.

`compile_progress_migration.migrate_candidate` now implements this schema step.
It validates the existing queue schema and retained data before upgrading, uses
the common operational database opener and transaction boundary, requires all
journal protections in version 4, and verifies already-migrated candidates on
retry. It does not overwrite or repair a partial unversioned journal. It neither
creates missing database files nor changes normal client version support.

Tests cover abrupt process exit at each DDL/version/commit boundary, exceptions,
SQLite page exhaustion, invalid source identity and payload, partial schema,
retained queue data, idempotence, deadlines and active-file/hardlink refusal.
The first missing-feature run failed because the module did not exist; that is
not evidence of a repaired production outage. The candidate step is isolated
from installed runtime and does not yet implement publication or backup staging.

Alternatives considered: installing each DDL statement separately exposes partial
schema; changing the global supported version first disables the unmigrated vault;
rewriting the adopted-pair manifest without preserving its preimage destroys
recovery evidence. The chosen first step changes only a candidate and preserves
the existing queue rows. Publication still needs the maintenance fence, verified
backup of both databases and artifacts, coherent adoption-record transition and
interruption recovery. Candidate qualification is not publication authorization.

Primary sources checked on 2026-10-19: [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
for explicit transaction boundaries; [Python 3.10 sqlite3](https://docs.python.org/3.10/library/sqlite3.html)
for supported APIs and transaction behavior; [PostgreSQL upgrade guidance](https://www.postgresql.org/docs/current/upgrading.html)
for distinguishing migration from compatible updates and preserving recovery
copies. PostgreSQL is a comparison source, not a runtime dependency or evidence
for SQLite behavior. [SQLite backup API](https://www.sqlite.org/backup.html)
supports coherent database copies. Python 3.10 compatibility tests remain useful,
but upstream now marks that Python release unsupported; they do not establish
upstream security support.

The queue application/version contract is also pinned in the adopted database-pair
manifest. Updating SQLite user_version alone would make ordinary clients refuse
the active queue. Offline migration must update that contract coherently, preserve
preimages and evidence, reject old clients, and recover from every interrupted step.
The SQL statements are deliberately not added to automatic v3 initialization while
that migration is unfinished. Ordinary reads never create the new table.

### Offline artifact fingerprints (2026-10-19)

The migration receipt helper previously read all artifact bytes, then captured
file identity separately. A path replacement between these operations produced
a receipt with the old digest and the new inode. A regression reproduces that
acceptance. Whole-file reading also made the inherited 256 MiB database budget
unsuitable for the installed queue (364 MiB) and coordinator (1.8 GiB).

Receipt creation now streams through the shared 64 KiB I/O chunk size, checks
descriptor and path metadata before/after hashing, and returns the identity bound
to that read. A second adversarial test found that an immediate same-size rewrite
with restored mtime also retained identical ctime on the execution filesystem.
Two complete hash passes through the same descriptor now have to agree. This is
a read-and-verification pair, not a retry limit. It doubles offline read I/O,
keeps constant memory, and detects that reproduced rewrite. It is not an atomic
snapshot against noncooperating writers; the canonical maintenance fence remains
mandatory. Its memory bound does not limit artifact size. Growth past the
initial size, truncation, replacement and caller deadline expiry fail explicitly.
Small JSON artifact budgets remain explicit. Other legacy adoption paths still
have whole-file limits; this change does not claim those paths handle large DBs.
This primitive requires offline files and closed SQLite connections: descriptor
closure can release process-owned SQLite locks. It does not replace quiescence,
SQLite backup, or atomic publication. Native Windows/macOS remain unqualified.

Alternatives: raising the whole-file ceiling would retain proportional memory
cost and the identity race; unguarded streaming would permit mixed-version hashes.
The selected implementation shares existing owner/containment/identity checks and
adds no dependency or new storage location. Sources rechecked on 2026-10-19:
[SQLite backup API](https://www.sqlite.org/backup.html),
[Python filesystem APIs](https://docs.python.org/3/library/os.html),
[PostgreSQL upgrade guidance](https://www.postgresql.org/docs/current/upgrading.html).
These support coherent copies, descriptor operations and preservation of migration
evidence respectively; the adversarial regression is the evidence for this race.

### Offline cutover design refinement (2026-10-19)

Use the already-approved v2 adoption envelope as durable migration intent. First
create and validate a coherent backup under the existing maintenance owner; then
publish the v2 envelope through the existing CAS/fsync primitive; finally apply
the qualified schema transaction to the original queue inode. A crash between
intent and SQL commit leaves normal clients refusing the version mismatch. The
explicit recovery operation validates the original adoption, backup manifest,
queue identity and pre-upgrade bytes before resuming. A completed version4 is
validated without restoring version3 over later work. Backup images are retained.

This replaces the proposed candidate-file swap, not the approved journal/schema
or paths. Candidate-only migration remains useful for isolated qualification and
shares the same SQL transaction. File swapping would require extra rename states,
preimage ownership and protection of hot journals. In-place DDL with durable
admission intent needs none of those. A transaction alone cannot atomically update
external JSON; publishing the requirement first makes the intermediate state
explicitly unavailable. No model request, package, service or runtime root is added.

Sources rechecked 2026-10-19: [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html),
[Python3.10 SQLite transactions](https://docs.python.org/3.10/library/sqlite3.html),
[PostgreSQL upgrade guidance](https://www.postgresql.org/docs/current/upgrading.html).
SQLite supplies the schema rollback boundary; Python's API requires explicit
transaction management; PostgreSQL is comparative guidance on preserving recovery
copies, not a dependency or proof of SQLite behavior. Qualification must include
process death at intent/DDL/commit boundaries and rejection of changed backup,
live owners, changed preimages and expired caller budgets.

The isolated `upgrade_offline` API now implements this sequence with existing
backup/fence/SQLite/publication primitives. It adds no online startup migration or
CLI installer path. Its explicit confirmation that every agent process is stopped
is required in addition to the registry fence: an old held client/SQL connection
cannot be retroactively given the new admission check. A regression deliberately
violates this precondition and verifies that changed queue bytes block recovery.
The public operation introduces no new time limit; existing backup validators
perform complete scans, so this API does not promise a caller-deadline bound.

Failure injection covers seven abrupt process-death boundaries plus rollback on
exceptions and owner loss before commit, corruption, valid but changed preimages, live ownership, backup ENOSPC,
new-client refusal during an incomplete intent, and preservation of work added
after completion. Recovery first refuses an unexpired dead owner's lease, then
uses the unchanged expiry-plus-verified-death rule. Tests advance the registry
clock past its recorded expiry while using the real process-liveness probe.

Before intent publication a failed/finished image is retained, never silently
erased; retry requires another empty directory. The image is owner-protected
plaintext staging, not an encrypted Restic recovery proof. Integrating encrypted
recovery, auditing inherited backup/queue limits and qualifying actual installation
remain prerequisites. No private vault migration has been attempted. The initial
test fixture omitted the existing staging directory and mistook enqueue's string
ID for a task object; both were corrected. Python3.10 also required the shared
ISO parser for the test clock, rather than newer datetime Z parsing.

Before activation, complete all of the following:

1. Deployment qualification of the isolated offline migration, including encrypted
   recovery integration, inherited-limit qualification and the stopped-agent
   procedure. Backup/image and intent/schema recovery are tested in fixtures;
   production installation and real-vault recovery are not yet qualified.
2. Complete intermediate-artifact retirement and orphan cleanup. Backup, full
   doctor and runtime deletion now account for retained journal evidence.
   The current no-delete trigger is fail-closed storage protection, not completed
   retention. It cannot be called a finished cleanup implementation.
3. One semantic transition controller used by CLI, MCP, queue and nightly; durable
   dispatch/answer/review states, receipts, preserved retry budgets and unknown
   provider-outcome handling. Storage alone does not enforce these transitions.
4. Audit every reachable limit. `queue_max_attempts=8` and backoff 30/3600 are
   documented in the pre-existing Reliability contract; their operational suitability
   must be distinguished from the separate SQL/API ceiling of 100 explicitly marked
   as unmeasured. Do not silently remove contractual values or inherit unknown ones.
5. Full useful-cycle comparison with the selected model, failure injection across
   integration boundaries, installation qualification, then deletion of replaced
   controllers and refresh of code navigation and documentation.

The running vault remains on its previous installed code. The old foreground
qualification was interrupted before recording this decision; it is not reported
as a successful rebuild. No migration or automatic repeat model expense was started.

### Retention reference race (2026-10-19, isolated implementation)

The adopted queue's ordinary result cleanup checks only task result references,
then closes its connection before unlinking. Compile events can retain the same
result, including through an older event. Moreover, checkpoint publication checks
the file before acquiring the queue write transaction. These two omissions can
leave a committed checkpoint pointing at deleted bytes. Existing foreign keys
protect task rows, not files.

The chosen correction uses the existing queue write transaction for reference
checks and unlink, and validates checkpoint bytes within that same serialization
boundary before appending. Check both task results and every retained event;
corrupt journal evidence refuses deletion. The capture-export cleanup shares the
same check because capture source artifacts also inhabit queue-results. This
protects cooperating queue API writers; it is not an OS filesystem sandbox.
No table, path, dependency, new limit or retention duration is introduced.

Alternatives: an independent file lock would duplicate queue ownership and need
all publishers to adopt it; a new reference table would add a migration and a
second projection to reconcile. Scanning the existing journal uses bounded memory
but costs linear reads per candidate deletion. This is an explicit initial tradeoff,
not a scalability measurement. A measured index/projection optimization can follow
without weakening the reference invariant. Terminal event retirement and orphan
collection still require the semantic controller's completion proof.

Sources checked 2026-10-19: [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
(single writer and BEGIN IMMEDIATE), [CPython sqlite3](https://docs.python.org/3/library/sqlite3.html)
(explicit transaction control), and [MITRE CWE-367](https://cwe.mitre.org/data/definitions/367.html)
(check/use synchronization). The implementation uses existing Python3.10-compatible
transaction helpers, not the newer sqlite3 autocommit interface. These sources
support the synchronization choice, not a claim of end-to-end product completion.

A second reproduced failure was batch-wide foreign-key rollback: a finished task
still named by a compile event entered ordinary purge selection, preventing
unrelated tasks from being purged. Selection now keeps and reports those task IDs.
This honors existing history protection; it does not invent a new retention period
or authorize deletion of completed events. Regressions cover succeeded/cancelled
deliveries, an older event's reference, a vanished file before the write transaction,
competing SQL writers at unlink, capture-source cleanup, corrupted journal evidence
and ordinary task references. The legacy v2 cleanup implementation cannot open the
adopted v4 database and remains only for pre-adoption compatibility; it has not been
qualified as a v4 consumer or silently granted the new journal contract.

Moving artifact validation under the write lock exposed another boundary in the
new implementation: a slow read could outlive the task lease. A failing regression
advances the clock during the actual artifact read. The append now rechecks the
same lease after validation before writing the event. No new lease duration or
retry allowance is introduced. This is tested ownership at append, not a claim
that an arbitrary filesystem operation can be interrupted at its deadline.

### Durable model-call controller (2026-10-19, isolated implementation)

The next controller slice reserves a call in the existing journal before invoking
its supplied, protected transport. The reservation and answer stay at the current
semantic stage; an answer alone cannot certify review or publication. A caller
supplies the verified source checkpoint and digest of its complete prepared request
(including model/settings, policy and dependency identities). It must not derive
that digest only from prompt text. The compiler integration that constructs that
identity and preserves correction budgets is still pending; this API is not yet
wired into the running compiler.

After reservation, any exception or missing answer leaves an uncertain call.
Recovery first attempts the deterministic answer artifact. Without one it raises
an explicit unknown-outcome refusal and never invokes the transport again. The
journal's running reservation is durable uncertainty evidence, not permission to
retry; the controller maps that refusal to the queue's existing blocked state.
A saved answer includes request/work/source identity and reported TokenUsage;
unknown usage remains unknown. This is not provider-side exactly-once execution.
A crash just before actual dispatch may conservatively require operator resolution.

A second ledger/service or transactional outbox would not atomically cover an
external model whose adapter offers no verified idempotency/recovery handle. The
existing journal plus deterministic artifacts therefore supplies the smallest
approved boundary. An automatic retry on timeout was rejected because it can pay
for the same work twice. No new count, retry limit, dependency or runtime location
is introduced. Call preparation/DLP and whole-work retry accounting must be
integrated before activation; a supplied callback is not a DLP bypass permission.

Primary sources rechecked 2026-10-19: [SQLite transaction boundary](https://www.sqlite.org/lang_transaction.html),
[AWS retry/idempotency guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/retry-backoff.html),
and [Temporal durable execution](https://docs.temporal.io/workflow-execution).
These support journal-before-effect ordering and cautious replay, not a claim that
SQLite makes the external provider atomic. No Temporal/AWS package is required.

`scripts/compile_controller.py` now implements this call boundary. The queue's
existing immutable redrive ancestry and lease/payload fence authorize continuation;
an unrelated task cannot take over. Recovery validates an orphan's canonical
answer envelope and request/source binding before publishing its event. No SQL
writer lock spans the provider call. Missing/corrupt saved answers refuse, and
unblocking delivery alone does not authorize redispatch. An empty returned answer
is retained with its known cost; it is not a validated plan. Semantic validation
is required before another call can be requested from that answer checkpoint.

Qualification includes real subprocess os._exit before an answer and after the
answer file but before its event, uncertainty on transport exceptions, checkpoint
reservation failure, stale/expired leases, redrive of answered/uncertain calls,
unknown usage, retained known cost of an empty answer, corrupt/foreign orphans,
and refusal of a changed request. Initial tests failed because the controller
module did not exist, not because they exercised an installed compiler fix.
One implementation function initially measured CCN6; removing an unnecessary
comprehension brought it within the required bound. Lint import ordering was
corrected; no guard was disabled.

Pending before activation: derive complete protected request/work identities in
all compiler entrypoints, persist correction budgets and semantic revisions,
connect explicit operator repeat authorization, bind review/publication to existing
receipts, qualify the inherited artifact byte bound including envelope overhead,
and complete the previously listed rollout/retirement/full-cycle checks. The
callback is a testable internal transport boundary, not a new public MCP tool or
an alternate permission to call a model. The old compiler remains the installed
consumer until the complete replacement is qualified, then must be removed.

### Explicit repeat decision (2026-10-19, internal API only)

`authorize_model_repeat` records an operator-supplied actor and reason bound to
the digest of the exact uncertain dispatch. It retains the original checkpoint
artifact and all prior events; unknown usage is not replaced with zero. The next
call must have the same complete request digest. Replaying the authorization
after its commit returns its existing checkpoint, not another allowance.

Before recording permission, recovery checks the deterministic answer artifact.
A valid answer is adopted and the repeat refused; corruption also refuses.
Lease/payload fences and next-sequence checks arbitrate concurrent event writers.
No SQL lock spans a model call. A late response from an earlier invocation remains
possible: operator consent accepts the risk of another expense, not exactly-once
execution. This API does not prove user identity or consent from a string; the
eventual explicit operator entrypoint must establish consent before calling it.
No installed entrypoint invokes it yet, and ordinary unblock/redrive does not.

Alternatives: treating queue unblock as consent loses the distinction between
delivery and paid work; a second authorization database duplicates journal
coordination; automatic repeat cannot disambiguate lost responses. The selected
implementation uses the approved journal and existing artifact/fencing APIs.
The three primary sources above were rechecked on 2026-10-19 for this transition.
SQLite provides local serialization only; AWS guidance requires care about
idempotency; Temporal is a comparison for retained execution history, not an
added dependency. No new numeric limit or package is introduced.

The initial two tests failed because the authorization API was absent. Coverage
now includes unchanged-request binding, saved/orphan answer preference, invalid
consent metadata, stale leases, redrive with retained history, and lost
acknowledgement after permission commit. Full compiler orchestration, operator
UI/CLI consent, correction accounting and deployment remain pending.

## Protected dispatch and semantic replay integration (2026-10-19)

The compile development branch now includes the installed central-model selection.
The shared `call_candidate` path accepts an internal dispatch callback only after
availability, DLP input protection, backend preparation and input admission. Ordinary
callers keep the same path. The callback receives a digest and a callable whose
result has passed the existing output DLP checks. No raw model response bypass is
added. The digest covers the protected inputs/schema, policy, model/inference
selection, endpoint, counting evidence and available prepared Codex identity.
An implicit model is refused for durable dispatch because it cannot identify the
request; ordinary unjournaled compatibility calls retain their existing behavior.

`run_candidate_call` connects this boundary to the existing journal. It also requires
the semantic caller's dependency digest: a protected-request hash does not establish
complete work identity. Saved candidate results retain descriptor, failure outcome,
input counting and `TokenUsage`. Empty returned answers and output-policy refusals
retain their known cost without retaining prohibited output. Provider timeout, exit
and other ambiguous transport failures leave an uncertain reservation and block
repeat, even though the common client represents them as returned error objects.
Earlier `BackendResponse` envelopes remain readable by the same controller; they
are an existing development/retained-artifact contract, not a second dispatcher.

The queue can read one historical event by indexed work ID and sequence with the
same content verification. Historical replay verifies both the dispatch and answer,
the current lease/payload fence and immutable redrive ancestry. It never starts a
new call merely because later checkpoints exist. `CompileReplay` advances a cursor
only after a completed journaled response; a preflight refusal cannot skip a saved
answer. Changed request order, policy, model or dependency identity refuses reuse.

`resolve_compile_plan` and `_CompileAttempt` now accept this shared call runner.
The existing parsers, draft repair, operation review, completeness review and final
normalization execute again over saved answers. An integration test stops after a
durable draft answer but before validation; a fresh resolver finishes the plan with
one total draft call and two review calls. This is deterministic replay of the
existing semantic code, not a claim that a saved answer passed a semantic check.

Five missing-boundary tests failed before implementation; two missing-replay tests
then failed before the replay cursor was added. Related existing tests also run.
One integration assertion initially expected the pre-normalization `slug` where
the final plan contract uses `path`; the assertion was corrected to the exact
expected note path. Import-order lint issues were fixed. No validation, durability,
DLP or existing test assertion was relaxed. Exact final results are in the private
execution checkpoint. Tests use isolated databases and synthetic model responses;
no real provider expense or live schema migration is claimed by this phase.

Primary sources rechecked on 2026-10-19: SQLite atomic commit, AWS idempotent retry
semantics and Temporal workflow replay (links above). The selected design reuses
our journal and validators; introducing Temporal would add a service and storage
contract, while replaying the provider directly would repeat uncertain expense.
There is no new package, runtime root, database, daemon or numeric workload cap.

Activation is still pending. The caller must construct and retain the complete
source/work/dependency identity and original snapshot; CLI, MCP, queue and nightly
must supply the same context and queue ownership. Replay currently re-runs local
preparation: missing providers or changed executable/configuration evidence can
refuse even when an answer is retained. Continuing across an operator-authorized
uncertain retry needs semantic-cursor integration; the internal authorization API
alone is insufficient. Whole-work accounting, receipt publication/recovery, event
retirement, inherited-cap qualification and encrypted stopped-agent deployment
remain required. This optional resolver seam is not installed production routing.

## Captured input artifacts (2026-10-19)

The controller can now encode, persist and restore `CompileInputs` through the
existing fenced queue artifact API. It retains original bytes, prompt projections,
targets, complete vault context, daily partitions, original entry boundaries,
native frames and compiled flags. Only the process-local partition cache is omitted;
the compiler already reconstructs its proofs when that cache is absent. Identical
byte strings share a digest entry within one self-contained artifact. Blob hashes,
original-source hashes, canonical roundtrip and the checkpoint's snapshot digest
are checked. Unknown fields or versions, changed bytes and lossy encodings refuse.
No source file is reread during restoration. This codec preserves captured evidence;
it does not replace source admissibility or semantic validation.

The model-oriented canonical JSON helper normalizes Unicode and rejects tuples.
The first implementation's tests exposed that it was unsuitable here. The snapshot
uses the journal's non-normalizing deterministic JSON representation; a regression
includes decomposed Unicode in native provenance and non-UTF-8 source bytes.

Research rechecked on 2026-10-19: [SQLite atomic commit](https://sqlite.org/atomiccommit.html),
[AWS idempotency guidance](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)
and [Temporal execution history](https://docs.temporal.io/workflow-execution).
Reopening live files would change the original work; serializing process objects
would couple recovery to executable object loading. Separate shared blob files
would need new recursive backup/retention reachability. The selected self-contained
format reuses existing artifact ownership and sacrifices cross-job deduplication.
No package, new directory or numeric limit is added.

Tests first failed for the absent snapshot API. Coverage includes lossless capture,
corruption, stale source digests, queue reopen, snapshot identity mismatch and the
real semantic compiler replaying a saved draft with inputs restored from its queue
artifact. Runtime schema adoption and entrypoint routing remain pending. The
inherited 16 MiB queue-artifact ceiling is explicitly **not** qualified by small
fixtures; live-context sizing is reported separately. Full work/dependency identity,
complete job capture/packing, uncertain-retry continuation, publication and deployment
remain unfinished. The installed compiler is unchanged.

The read-only installed-vault measurement found 7,472 snapshot records encode to
40,275,638 bytes **without daily sources**; encode/decode equality passed in 1.95 s.
That already exceeds 16,777,216 bytes. Activation is blocked by measured capacity,
not an assumed future workload. Raising only the writer bound would leave reader,
backup, recovery and retention bounds inconsistent; that common contract must be
resolved before installed routing. Related suites: 90 passed on Python 3.12 and
90 on Python 3.10; the updated real-compiler integration was additionally rerun on
3.12. The complexity check covers 23 changed/new functions, all within law 5.

## Streaming artifact I/O (approved 2026-10-19)

The owner accepted the follow-up proposal: keep small immutable queue references,
stream the local snapshot files, retain recovery/backup/cleanup guarantees, and
remove whole-snapshot materialization throughout the compiler. This approval
supersedes raising `queue.max_result_bytes` as the proposed solution. No new env
contract was introduced. Rechecked primary sources: AWS SQS large-message storage,
Temporal workflow input/result references, and Restic's content storage design:
https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-managing-large-messages.html
https://docs.temporal.io/references/workflow-task-errors
https://github.com/restic/restic/blob/master/doc/design.rst

The first implementation slice streams artifact writes into an owner-only temporary
file, fsyncs before atomic create-only publication, and checks an existing destination
by stable streamed SHA-256. Ordinary byte-result writes delegate to this same writer;
there is no second durability implementation. Interruption removes only that writer's
temporary file. A complete file left before an event commit can be adopted only with
its expected digest and current queue lease. A stale lease cannot consume input;
a lost lease cannot publish its checkpoint. Existing artifact references continue
to protect files from retirement. No new directory, database or dependency appears.

A separate verification operation reads bounded chunks and checks path/descriptor
identity before and after the digest, file permissions and the retained checksum.
Event admission, controller validation-only reads and the common full-health/backup
audit use it. Deadline expiration is preserved as expiration, not misreported as
corruption (a regression caught and corrected the OSError subclass interaction).
The standard-library I/O buffer controls the working set, not admitted file size.

Four missing-API regressions failed before code. Additional cases cover interrupted
writes, conflicting existing destinations, corruption, stale leases, event-commit
failure recovery, expired verification deadlines, and full doctor/backup validation
of a 20 MiB artifact. Legacy symlink/conflict and queue migration/corruption tests
remain in the related suite. A local isolated write+verify measurement, excluding
its reused 1 MiB input block, traced 23,556 bytes of Python allocations for a 32 MiB
file and 22,502 for a 128 MiB file (0.167 and 0.440 s). This demonstrates bounded
additional allocations for this I/O layer, not total RSS or whole-compiler memory.

This slice does not make the snapshot codec or source capture streaming. They still
materialize whole-input JSON/CompileInputs and retain the ordinary bounded bytes API.
Large snapshot encoding, lazy compiler consumption and safe deployment remain open;
no installed routing or complete point-7 claim follows from the I/O qualification.

The verifier reads only the extent observed at open, then rechecks identity; a
concurrently growing producer cannot extend the scan indefinitely. Per-operation
caller deadlines are supported for streaming writes, reads and orphan recovery;
no fixed workload timeout is introduced. A regression uses a producer that never
returns EOF and proves the initial extent bounds the reads. Final changed/new
function checks cover 32 functions, all CCN <= 5, at most two ifs and nesting <= 2.

Validation: broad related suite 138 passed on Python 3.12; Python 3.10 includes
an added large-artifact health/backup test and has 139 passed. The final extent
and deadline cases were rerun separately: 10 streaming tests passed on each
version. An intermediate additional 3.12 snapshot/health run had 45 passed.
Ruff, diff checks and dependency compatibility checks passed. The slower 3.10
run waited on filesystem journal commits; durability checks were not relaxed.

## Record-addressable snapshot container (2026-10-19)

The next slice replaces the uninstalled JSON/base64 snapshot codec with one
uncompressed ZIP64-capable artifact. Rechecked the three primary sources above
and Python 3.10's maintained standard-library contract:
https://docs.python.org/3.10/library/zipfile.html
ZIP_STORED avoids decompression and a compression dependency; individual records
and digest-named byte members permit selected reads. JSON/base64 duplicates
whole-input data; plain sequential JSON/tar requires scanning earlier records;
a separate blob tree would change backup and retention reachability. The selected
format retains the existing single-file contract. Its central directory and
record-digest manifest cost memory proportional to record count; selected source
bodies still cost their actual size. This is not a constant-memory claim for the
whole compiler. ZIP64 handles the format's large-file case without a new product
size setting. No extraction API is used, and compressed/encrypted or duplicate
members, unknown names, missing records and invalid digest lists are refused.

The producer callback runs inside the existing queue durability implementation.
Fixed container timestamps make retries byte-identical. The reader owns a verified
seekable descriptor for its context lifetime; the whole artifact is hashed in
bounded reads before use, and descriptor/path identity is rechecked on exit.
A logical checkpoint binds the canonical manifest of record digests; each record
binds original bytes, prompt projection and provenance. Native frame metadata is
also an addressed blob, so opening the manifest does not load all frame text.
Reading a record verifies its metadata and byte digests, including original daily
bytes. Existing DLP dispatch, publication and source-admissibility rules are not
replaced by container integrity.

Three initial missing-API tests failed before implementation. Additional red tests
exposed malformed record-list acceptance and missing original-digest validation;
both were corrected. Regression coverage includes a snapshot over the former
small-result ceiling, metadata-only opening, context lifetime, exact non-UTF8 and
Unicode provenance round trips, tampering, schema/canonicality checks, and retry
after artifact publication but before event commit. A completed journal event
remains immutable: callers resume it instead of appending the same revision again.

The old codec's repository uses were exclusively its own tests and the replaced
save/load pair. It was never installed, so it was removed rather than retained as
a parallel production implementation; its provenance and corruption tests now
exercise the replacement. The explicit `load_compile_inputs` convenience still
materializes a complete value for current semantic-replay callers. Source capture,
metadata-first batching and installed entrypoint integration remain open; this
slice does not close point 7 or certify the installed nightly pipeline.

Isolated measurements exclude already-captured input bytes. Writing, verifying
and opening metadata for a 32 MiB source used 64,690 additional traced Python
bytes (0.369 s); for 128 MiB, 23,187 bytes (1.415 s). The first run includes lazy
initialization. These are codec/I/O measurements, not RSS, full capture, model
cost or complete useful-cycle evidence. The source appears in two record groups
but its bytes are stored once. Test environments retain the locked package sets:
65 compatible packages on Python 3.12.3 and 67 on Python 3.10.20; no new package
was introduced.

Read-only live-context qualification captured 7,500 records (3,750 sources,
3,747 targets, three vault files, no dailies). The container occupied 32,994,280
bytes and round-tripped exactly; encoding added a traced peak of 7,226,869 bytes,
with encoding plus eager round-trip taking 3.038 s. This excludes source-capture
allocations and is not a direct before/after comparison against the earlier
7,472-record measurement. Descriptor replacement and symlink refusal tests passed
on both Python versions. All 67 changed/new functions satisfy CCN <= 5, at most
two ifs and nesting <= 2; repository Ruff checks pass.

## Context planning from the retained archive (2026-10-19)

A follow-up dependency trace found that a lazy archive reader alone was insufficient:
`_ContextRanking` copied all sources into a tuple, `_ByteBatchMeasure` retained all
context records (including as dictionary keys), and citation association first built
a tuple of all pages. These consumers would repopulate memory with decoded bodies.
The existing planner now accepts a context-managed archive planning view. Daily
parts still materialize for the current physical partition proofs; sources, targets
and vault context keep verified metadata paths and record indices. Path selection
occurs before body reads. Ranking retains lexical statistics and ordering indices;
byte sizing retains numeric frame sizes keyed by logical path. Non-additive
measurements still serialize selected records, and required citation association
still examines every relevant page. No citation or page is dropped to make a batch
fit. Existing snapshots remain supported as ordinary in-memory sequences.

Rechecked primary sources for the approved local-reference design: Python zipfile,
AWS SQS large-message references, and Restic's immutable storage design (links above).
The Python 3.10 documentation now labels that Python version unsupported upstream;
3.10 tests establish compatibility with the repository contract, not upstream support.
The installed interpreter remains 3.12. No package or operating-system dependency
was added. Rejected alternatives: a transparent byte proxy would hide I/O and break
identity-keyed partition caches; copying lazy records into tuples would defeat the
working-set objective; a second planner would duplicate grouping and citation rules.
This change reuses the existing planner with explicit indexed context collections.

Three initial regressions failed before code. A further red test proved that byte
measure initialization decoded context bodies; the corrected initializer reads
metadata only. Tests compare eager/archive batch sources, manifests, packing identity
and required citations, exercise context-pending and final planning, and prove that
numeric sizing retains no decoded context records. The semantic recovery test now
opens an archive view separately before and after interruption: the saved draft is
reused and the operation/source-completeness reviews still run (three calls total).

A broader regression exposed an already-failing publication test. Reproduction in
a clean detached worktree at 4394308f produced the same failure: the earlier target
snapshot check correctly refuses before the persisted transaction precondition.
The test now requires the specific `compile_snapshot_changed` code, unchanged
external edits, no published destination and no compile receipt. The production
refusal was not weakened or delayed. Related suites, including compile transaction
publication and recovery, passed 225 tests on Python 3.12 and 225 on Python 3.10;
Ruff, dependency checks and all 40 changed/new function complexity checks passed.

This does not activate the candidate controller. The archive context must remain
open for planning and semantic processing; source capture and daily partition
planning still materialize their inputs. Full work/dependency identity, all-entrypoint
integration, receipt/retention handling, recovery qualification, installation and
useful-cycle measurements remain completion requirements. It is not a point-7 closure.

A paired isolated measurement opened the same live-context artifact with one
synthetic simple daily source (no timestamp-choice layout), then ran the actual
planner. Eager restoration peaked at 141,994,104 traced Python bytes in 5.735 s;
selected restoration peaked at 83,584,274 bytes in 7.215 s. Batch manifest, selected
source paths/digests, required context and packing identity matched exactly. This
is about 41% lower traced peak at about 26% greater elapsed time in that scenario;
it is not an acceleration or a real nightly/token-efficiency qualification. Archive
metadata and lexical statistics still scale with corpus size. A first profiling
invocation was stopped after identifying a wrong result-field name in the measuring
script; it produced no claimed measurement. The corrected script and results are
retained in /tmp/measure-compile-context-selection.py and logs/step7-context-selection-measurement.*.

## Sequential capture into the same artifact (2026-10-20)

Fresh graph/source inspection at 6c023f85 confirmed two retention causes before
serialization: source/daily/target lists and the source-budget object's reference
to the entire source list. The common capture implementation now emits tagged
records one file at a time. The eager API collects this same iterator and supplies
its original partition-proof owner, preserving current callers and the parsing
reuse tests. Streamed capture uses a per-daily proof owner, releasing it after that
file's records; the budget object stores counts and byte totals only. Existing
vault/page selector helpers are generators, not competing collection algorithms.

The streamed archive writer emits raw blobs as records arrive and retains only
record metadata until the final manifest. It sorts source/target metadata by the
existing logical-path rule, so the digest and recovered CompileInputs equal the
eager snapshot. Metadata/digest bookkeeping still grows with record count; a single
physical source and its partition proof still need memory. The preexisting writer
for already-captured inputs retains its physical ZIP member order for deterministic
retry compatibility. Neither writer changes the container schema or read contract.

Rechecked primary sources on 2026-10-20: Python's zipfile documentation supports
sequential writes and requires successful closure for a complete archive; AWS SQS
and Restic support the already-approved reference/immutable-file design (links in
previous sections). https://docs.python.org/3/library/zipfile.html
A second full collection before serialization was rejected because it preserves the
original retention defect. Re-reading sources for a second identity pass was
rejected for this API because it introduces a new source-change window. Instead,
`write_compile_capture` returns the manifest digest only after complete archive
closure. It is a format/capture writer, not standalone queue admission: the existing
durable producer owns temporary-file cleanup and publication, and complete work
identity still must bind the returned manifest before installed entrypoint routing.
There is no new directory, database, package, daemon or numeric limit.

Five initial missing-API regressions failed before implementation. Checks now prove
logical snapshot equality, output before reading the next daily, released completed
daily source objects, fully receipted source handling, late source-error propagation,
and no durable artifact or temporary residue after a late capture failure. Native
container/projection, original bytes, symlink/size checks, publication transactions,
required context and semantic replay remain in the related suite. One initial test
incorrectly expected a fully receipted ordinary day to remain as an already-compiled
part; the existing contract omits it from daily work while retaining its source, and
the final test checks that contract. No receipt or admission rule was changed.

Validation: 235 related tests passed on Python 3.12 and 235 on Python 3.10. After
preserving the original pre-captured ZIP layout, the snapshot/protected-dispatch
subset passed 58 tests on 3.12; all seven final streaming tests passed on 3.10.
All 25 changed/new functions satisfy the mandated complexity bounds, and Ruff and
both locked-environment dependency checks passed. No dependency was added.

Paired capture+durable-write measurement on isolated fixed files (one MiB per note),
including source capture allocations, returned identical logical digests:
32 MiB: eager peak 34,703,549 bytes / 0.384 s; streaming 3,234,773 / 0.296 s.
128 MiB: eager peak 135,443,699 bytes / 1.553 s; streaming 3,459,241 / 1.035 s.
These are traced Python allocations, not RSS or nightly/token measurements. Settings
were not raised for the experiment. The inherited admission limits are still enforced;
their applicability to streaming workloads remains part of the law-9 cutover audit.
Source capture can now stream, but `planning_inputs` still materializes all daily
parts, and installed CLI/MCP/queue/nightly routing remains on the old controller.
No installation or point-7 completion is claimed.
