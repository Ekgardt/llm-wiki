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

### 2026-10-20 — physical-day read scope

Reproduction: sequential archive reads recreated the whole original daily byte
string for every partition. Two regression tests failed on the prior reader:
parts did not share their original bytes, and reading the second part reread the
physical source. The common sequential reader now scopes verified byte and parsed
JSON reuse to consecutive records of one logical day. It drops its caches before
advancing to another day. Random record access remains independent; no persistent
cache, numeric capacity, package or runtime path was introduced. Digest validation
and record restoration still use the common implementation. This follows the
already researched iterator/local immutable artifact design; it does not change
source grouping, native atomicity, citation ambiguity or provider admission.

A paired synthetic read of one 1-MiB day split into 16 parts returned equal records:
independent reads peaked at 17,839,111 traced Python bytes, sequential reads at
1,131,081. This is a mechanism measurement, not installed RSS, useful compile
quality, token cost or whole-corpus streaming qualification. The planning view
still holds the restored daily records; its full streaming conversion remains open.
The complexity checker accepted all ten changed/new functions. Ruff and installed
package consistency checks passed for both Python environments (65/67 packages).
The 60 archive-sharing, protected-dispatch and streaming-capture tests passed on
Python 3.12 and independently on Python 3.10. The installed autonomous compiler
was still live (PID 2129006); it was neither stopped nor replaced by this change.

### 2026-10-20 — incremental common batch algorithm

Dependency inspection at b16bc790 (fresh graph, with unresolved edges supplemented
by source reads) found three eager collectors: native atomic units, group sets,
and completed batches. They now share iterator implementations; existing public
and installed eager callers collect the same algorithm. The replaced native-unit
accumulator helper has no remaining source/test references and was removed.
Incremental batch iteration retains the current group and lookahead, while the
existing measure and citation association still retain the input daily records.
This is not yet whole-snapshot streaming or an installed controller switch.

Rechecked independent primary sources on 2026-10-20:
- https://docs.python.org/3/library/itertools.html — incremental consumption and
  the difference between iterators and consumers that materialize their input.
- https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-managing-large-messages.html
  — references to retained large payloads; no AWS dependency is introduced.
- https://github.com/restic/restic/blob/master/doc/design.rst — immutable storage
  and content identity; this is not evidence that our recovery is qualified.

The selected design reuses one grouping algorithm. A separate streaming planner
would duplicate admission/atomicity rules; transparent byte proxies would conceal
I/O and conflict with identity-bound partition proofs. No new numeric boundary,
package, provider setting, service or runtime location is introduced. The iterator
uses syntax and standard-library features available on supported project Python
3.10/3.12 environments; no new Python 3.12 itertools API is required.

Four initial tests failed before implementation. Final tests prove boundary-only
lookahead, complete physical tool-line grouping, identical batch outputs with only
one construction per next(), release of previous batch objects, and propagation
of late construction errors. The eager API still raises that error before returning
any batches. The new iterator must not simply replace the installed run loop:
that would move planning failures after earlier paid calls without complete durable
failure accounting. The global ambiguous-fragment citation index must also survive
future per-day loading; rebuilding it from one selected subset would be incorrect.

166 related tests passed on each Python version. After extracting the boundary
predicate to satisfy measured CCN (the first version measured 6), 16 directly
related tests passed again on each version. All 14 changed/new functions satisfy
CCN <= 5, at most two ifs and nesting <= 2. Whole scripts/tests Ruff, diff checks,
and 65/67-package consistency checks passed. Test output is retained in the private
logs/step7-iter-batches-py{312,310}-20261020.txt files. No useful model-cycle,
installation, backup/restore or point-7 completion is claimed.

### 2026-10-20 — streamed daily planning and global citation identity

The next dependency trace found that citation association retained its entire input,
all physical-part objects, and an EvidenceResolver whose immutable cache held every
resolved source. The regression proving snapshot retention failed on 5a896f7d.
Required-context construction now makes two replayable daily passes. The first
stores compact ordinal/path/digest/span identities, including part keys; global
fragment ambiguity is decided across all identities. Pages are verified and their
references associated with these identities. The second resolves physical evidence
with a resolver and, for an archive pass, partition proofs scoped to each day.
Ordinal identity and count checks refuse changed, omitted or added daily records.
The result retains no input snapshot, resolver or daily body. An existing measure
may still own its own immutable proofs; the association does not clear another
owner's cache or weaken native frame/partition validation.

CompileInputArchive.required_context() performs this scan without planning_inputs()
or materialize(). Its iter_batches() consumes sequential daily records and filters
all pending daily paths from optional context before planning. This is necessary:
merely presenting the current day's parts would incorrectly offer other pending
days as optional context. Target and vault context remain indexed archive views.
Both eager and streamed callers now use one group-state algorithm and the same
unit admission, physical joins, required/optional context selection, exact tokenizer
path, and final batch construction. The streaming measure holds current parts and
lookahead instead of all days. After a boundary it drops the previous group's
measure before handling the final group. A two-day weak-reference regression
caught the initial final-group retention; it failed before that correction.

This implements the already approved local immutable-artifact design. The three
independent primary sources rechecked for this phase are the Python iterator,
AWS large-payload-reference and Restic immutable-storage documents linked in the
preceding section. Alternatives: per-day isolation would change valid cross-day
packing and global fragment ambiguity; proxy bytes would hide I/O and invalidate
identity-bound proof assumptions; a second grouping implementation would duplicate
admission logic. Two passes trade additional local reads for a compact global
identity index while preserving proof rules. Metadata still scales with records
and citations, and one physical day/current atomic unit must fit the existing
resource contracts. No new numeric limit or dependency was introduced.

Tests cover archive/eager equality of all batch fields and materialized collections,
required ordinary/fragment/native citations, ambiguous repeated fragments, optional
context, qualified cross-day capacity, a non-additive tokenizer, invalid DLP, empty
work, lifetime of completed source objects, changed stream identity/count, and
release of the finished citation snapshot/resolver. Initial comparisons incorrectly
compared a lazy collection object directly with a tuple; final comparisons
materialize only the test's returned target/source/vault collections and compare
every field. The native test uses its measured required window, matching the
existing qualification fixture, rather than assuming the default budget admits it.

A synthetic citation scan with one MiB per day and equal resulting associations:
8 days: eager traced peak 9,516,377 bytes / 0.151 s, streamed 2,139,826 / 0.168 s.
32 days: eager 34,712,754 / 0.569 s, streamed 2,206,788 / 0.628 s.
These measure Python allocations while opening and scanning a prepared archive,
not installed RSS, model quality, paid tokens or capture cost. The separate full
planner measurement is recorded after the final lifetime fix below.

Installed entrypoints still use eager capture/planning. The streamed archive API
must be bound to complete work/dependency identity and durable preflight/failure
accounting before dispatch; direct substitution into the current run loop would
move errors after earlier model calls. Backup/restore qualification, migration,
installation, replaced-production-legacy cleanup and a useful full model cycle
remain open. No installed reliability or point-7 completion claim is made here.

Final validation: 210 related tests passed on Python 3.12 (48.90 s) and Python
3.10 (53.89 s), after the final-group lifetime correction. All 46 changed/new
functions satisfy the measured complexity bounds; whole scripts/tests Ruff,
diff checks and 65/67-package consistency checks passed. No package was added.
Logs: logs/step7-stream-planner-final-py{312,310}-20261020.txt and
logs/step7-stream-planner-complexity-20261020.json.

Final paired archive-open/full-planner experiment, each synthetic source admitted
as one batch, compares manifest digests, packing identities, required paths and
all selected source path/digest pairs. No input budget was raised:
32 days: eager peak 1,923,151 traced bytes / 3.776 s; streamed 1,065,708 / 4.408 s.
128 days: eager 5,435,147 / 14.786 s; streamed 1,559,339 / 17.239 s.
Metadata and recorded output signatures scale with source count. The streamed
path uses less memory but more local planning time; this is not a claim of lower
end-to-end latency or model cost. Measurements include archive opening and planning,
exclude construction of the prepared archive, and ran alongside the existing live
system. Raw results: logs/step7-stream-planner-final-measure-20261020.jsonl.

### 2026-10-20 — durable streamed source capture and task-family fencing

Fresh graph 92654f24 plus source inspection showed that the queue artifact name and
checkpoint need the source identity before publication. compile_capture_digest()
now computes that identity through the same capture/manifest iterator used by the
writer, retaining metadata rather than bodies. save_compile_capture() writes the
second pass through the existing atomic queue producer and requires the admitted
logical digest before publication. Changed daily bytes or a new context page refuse
publication; the existing producer removes its temporary file. Caller-supplied
deadlines are checked during identity computation and by the existing writer.
No new timeout, source ceiling, staging directory, database or package is added.

A committed snapshot checkpoint is verified and reused. A complete artifact left
between durable file publication and event commit is read through the queue's
stable owner-only descriptor, then its expected manifest and every referenced
record/blob are checked before event admission. This allows recovery without reading
changed live sources. Corruption and lost task ownership refuse admission and keep
the existing artifact as evidence. The expected-digest and format-validated recovery
methods share orphan lookup/fencing; no second file-writing implementation exists.
The first snapshot follows the pending event at sequence zero. This is a workflow
transition, not a numeric processing limit. Complete work/dependency binding is still
required from the semantic caller; a source digest is not described as that binding.

The integration tests exposed an independent shared-boundary defect: a different
compile task could append to a known work ID with a matching source digest. The
reproduction succeeded before the fix. Journal append and artifact create/recovery
now check the latest checkpoint's original/redrive task family under the existing
lease transaction. The direct event, writer, expected-digest recovery and validated
recovery boundaries all have regression coverage. Explicit redrive still reuses the
original snapshot and saved model answers. This concerns the uninstalled v4 journal
candidate, not evidence of a deployed v3 exploit.

Research rechecked on 2026-10-20 for source/work identity and recovery:
- https://bazel.build/remote/caching — action identity includes declared inputs and
  execution parameters; content identity and action identity serve different roles.
- https://nix.dev/manual/nix/2.35/store/derivation/ — derivations describe build inputs
  and outputs; a data-file digest alone is not a derivation identity.
- https://docs.temporal.io/workflow-execution — durable history and replay motivate
  preserving prior execution evidence; this does not make an external model
  deterministic or grant provider-side exactly-once execution.

Chosen tradeoff: two checked local passes avoid a new staging lifetime and an extra
snapshot copy. It can refuse when sources change between passes and spends an extra
local read; it never silently accepts a new snapshot under the old identity. Blindly
recapturing on resume was rejected because it loses the original context. An
unvalidated orphan was rejected because a pathname or ZIP directory alone does not
prove source bytes. No Bazel, Nix or Temporal runtime dependency is introduced.

Five initial missing-API tests failed before implementation. The full streamed
capture -> archive planning -> semantic validation -> durable-call replay test now
runs alongside its eager counterpart. After interruption following the saved draft,
the test provider is called three times total: one draft, operation review and source
completeness review. This is an isolated provider fixture with an explicit test
dependency identity, not paid-model, complete-identity or installed-cycle evidence.
A corruption test initially expected ProgressCorrupt; the shared snapshot validator
raises the existing exact ValueError('snapshot content digest mismatch'), and the
final test checks that refusal plus unchanged pending progress and retained evidence.

Final related suites: 193 tests passed on Python 3.12 (131.75 s) and 193 on Python
3.10 (196.22 s). All 42 changed/new functions passed actual CCN/if/nesting checks;
whole scripts/tests Ruff, diff checks and both package consistency checks passed
(65/67 packages). Logs: logs/step7-durable-capture-py{312,310}-20261020.txt and
logs/step7-durable-capture-complexity-20261020.json. Installed routing is unchanged;
work/dependency identity construction, durable full preflight and entrypoint routing,
backup/restore, installation, retirement and a useful real full cycle remain open.

### 2026-10-20: stable dependency-file fingerprint prerequisite

The installed-package inventory investigation confirmed that names/versions and
RECORD hashes alone are insufficient witnesses for actual installed bytes. The
current Python 3.12 environment exposes 65 distributions with file inventories;
this does not establish a complete execution dependency closure. The Python
stdlib metadata API can return no inventory and supports non-filesystem metadata,
so the eventual builder must account for these cases explicitly.

Primary references rechecked today: Python's
https://docs.python.org/3/library/importlib.metadata.html (the API used here must
remain compatible with 3.10), https://bazel.build/remote/caching and
https://nix.dev/manual/nix/2.35/store/derivation/. Content hashing is chosen over
version-only identity; the existing two-pass stable-file reader is reused instead
of implementing a second hashing path. No runtime dependency is added.

`fingerprint_regular_file` now provides that existing streaming consistency check
for ordinary readable code/dependency files. `fingerprint_runtime_file` delegates
to it with mandatory owner-only permissions, preserving the migration contract.
The regular-file primitive refuses leaf symlinks and containment escapes; a future
inventory builder must resolve legitimate package symlinks and verify resolution,
not mistake this low-level no-follow contract for a ban on such installations.
It accepts arbitrary file sizes unless its caller supplies a budget, obeys caller
deadlines and refuses concurrent content/metadata/path changes. This witness does
not lock arbitrary external writers or make hashing a live SQLite file safe.

Six new feature tests failed before implementation. All 19 fingerprint tests then
passed on Python 3.12 and 3.10, including original runtime permissions, deadlines,
short reads, growth/replacement and same-size/mtime mutation. Related reliable
memory and migration tests passed on both Python versions (114 passed, 3 skipped
each). Seven
changed/new functions passed measured CCN/if/nesting checks; whole scripts/tests
Ruff passed. The dependency/work manifest builder, entrypoint adoption and all
previously listed installation/full-cycle requirements remain open. This shared
primitive alone does not qualify dependency-complete replay.

### 2026-10-20: explicit dependency manifests and origin-scoped work identity

The candidate now builds canonical file/configuration manifests using the shared
stable reader, and work identities binding original queue task, source digest,
dependency digest and invocation parameters. Redrive callers must retain the
original task identity; independent tasks with identical sources must not collide.
A controller test uses actual helper-file bytes: an unchanged dependency replays
one saved answer, while changed bytes (without a version bump) refuse reuse and
cause no additional provider call. This test uses a fake provider, not paid-model
or installed-entrypoint qualification.

The product inventory traverses scripts (including schemas/data), pyproject.toml
and uv.lock. The package inventory hashes actual files from importlib.metadata,
not RECORD's stated digests; package versions and ordered sys.path are also bound.
Named file additions/removals, configuration changes and symlink retargeting change
the identity. Legitimate directory symlinks are followed; directory cycles are
refused because there is no finite tree traversal, not because of an arbitrary
count/depth cap. Cached bytecode is omitted only when its corresponding source
exists; source-less bytecode remains inventoried. This assumes normal Python
cache derivation and does not certify a hostile/modified bytecode cache.

Configuration values and resolved locations are hashed before being returned;
these hashes are identity witnesses, not encryption for low-entropy secrets.
No manifest includes plaintext configuration values or file bodies. File content
streams with caller deadlines and no added size cap. Missing package inventories
and editable packages with unenumerated external source trees refuse qualification
explicitly; they are not silently treated as fully covered. Supporting editable
packages requires an explicit complete source inventory, not an arbitrary allowlist.
Zip/custom metadata providers are not yet qualified by this filesystem builder.

Design basis (rechecked 2026-10-20): Python importlib.metadata's file-inventory
contract, Bazel's input-bound action caching and Nix's derivation identity, linked
above. Alternatives rejected: version-only/hash-from-RECORD identity (misses
changed installed bytes), source-digest-only work IDs (collides between independent
tasks), and persisting configuration values (unneeded disclosure). Conservative
whole-product/all-distribution inventories can invalidate work after unrelated
updates; narrower closure needs evidence of complete imports/resource dependencies.

Local measurement: two product passes over 250 files / 8,829,214 bytes agreed in
0.160 s; two installed-package passes over 8,772 files / 172,014,387 bytes agreed in
6.329 s. These are local measurements, not full task performance or a safe polling
interval. Logs: logs/step7-{product,package}-identity-measurement-20261020.json.
Eleven initial identity tests, three product-inventory tests and three package
inventory tests failed before their implementations. The final 18 focused tests
pass on both Python versions. All 35 changed/new functions pass measured complexity;
whole scripts/tests Ruff and diff checks pass.

Still incomplete: the execution environment must account for interpreter/stdlib,
external binaries and loaded code versus files changed after process startup;
complete effective runtime configuration/DLP/provider identity must be composed
with these components. Traversal is not a filesystem snapshot: admission must
re-inventory and refuse changes. These APIs do not yet wire the production entry
points, prove complete dependency closure, or solve durable full-batch preflight.
All previously recorded installation, recovery and real-cycle gates remain open.

Related dispatch/call/capture suites completed: 114 tests passed on Python 3.12
(159.79 s) and Python 3.10 (203.61 s), before the final package-specific additions;
the final 18 identity tests were then separately verified on each version (7.40 /
9.10 s). Logs: logs/step7-dependency-identity-py{312,310}-20261020.txt. No full-suite,
Windows/macOS, complete dependency closure or real paid-model claim is made.

### 2026-10-20: require live dependency verification at durable dispatch

A plain dependency digest supplied to run_candidate_call/CompileReplay could be
stale by dispatch time. Source inspection confirmed that the previous API never
recomputed it. Three new witness tests failed before implementation. The candidate
now requires CompileDependencies: an expected digest plus a live inventory
inspector. The unchecked digest-only internal API is removed; all its known
callers and tests use the witness, with no compatibility bypass. Workflow identity
still uses a digest; dispatch requires a means of verifying it.

Verification runs at call entry, after protected provider preparation immediately
before journal dispatch, and after the returned/saved result before semantic use.
A change before dispatch leaves the journal checkpoint unchanged and does not
invoke the model. A change during the model call retains the completed answer and
usage, then refuses further semantic use. Restoring the expected dependencies
replays that saved answer without another provider call. A replay cursor checks
its witness when constructed as well. These tests use explicit file inventories
and a fake provider, not a claim that the complete runtime inventory is solved.

This follows the action/input identity and durable-history sources already checked
above (Bazel, Nix, Temporal; 2026-10-20). Checking only at construction was rejected
because preparation/model latency allows intervening updates. Discarding a saved
answer on mismatch was rejected because it loses cost/recovery evidence. Reusing
the existing dispatch and artifact machinery adds no storage location, schema,
configuration limit, service or third-party dependency. Full re-inventory costs
local I/O; metadata-only optimization is not accepted as an equivalent byte witness.

The checks do not lock arbitrary writers. Existing fenced admission and coherent
code-update ownership remain necessary, especially between the final pre-dispatch
check and actual invocation. Previously imported code versus current disk contents
is still unresolved for the in-process MCP path; queue compile uses a fresh child
process. A complete environment inspector and entrypoint integration remain open.
No production installation or full-cycle completion is claimed.

Final focused tests: 22 passed on Python 3.12 (3.79 s) and 3.10 (4.90 s), including
mutation during preparation, mutation during the model call, retained answer and
replay after restoration. All 24 changed/new functions pass actual complexity
checks; whole scripts/tests Ruff and diff checks pass. Related suites and logs:
logs/step7-dependency-guard-py{312,310}-final-20261020.txt. Earlier red evidence:
logs/step7-dependency-guard-red-20261020.txt.
Related protected-dispatch/call/replay suites completed with 107 tests passed on
Python 3.12 (71.51 s) and 3.10 (79.65 s). The final restoration assertion was also
covered by the 22 focused tests reported above. No new runtime package was added.

### 2026-10-20: configured Python runtime dependency component

The candidate compile_runtime_dependency_manifest fingerprints the interpreter
executable, the configured installed shared Python library (when enabled), standard
and platform-standard library trees, and existing files directly on sys.path
(including normal archive entries). It binds Python version/implementation/cache
tag, platform/byte order, flags, xoptions, warning options, prefixes, sys.path and
sysconfig build configuration through the existing hashed-parameter manifest.
This uses the existing stable streaming fingerprint path, with caller deadlines
and no new size/file-count threshold.

Research rechecked today: https://docs.python.org/3.12/library/sysconfig.html,
https://bazel.build/remote/caching and
https://nix.dev/manual/nix/2.35/store/derivation/. sysconfig distinguishes stdlib,
platstdlib, purelib and platlib, and exposes build configuration rather than a
complete loaded-process dependency graph. The current Linux 3.12 environment
proved why blindly using LDLIBRARY is wrong: libpython3.12.so is absent, while
INSTSONAME names installed libpython3.12.so.1.0. Both tested runtimes have this
configured installed library. Version-only identity and guessed platform paths
were rejected; unsupported/missing shared-library metadata refuses qualification.
No new package, service, path contract or configuration option is introduced.

Normally package directories are handled by the separate package inventory.
A red regression showed that a valid installation scheme with identical stdlib
and package roots would drop the entire standard library. Roots that overlap are
now included conservatively, preserving bytes rather than pretending that the
components are disjoint. Directory exclusion is implemented by the shared tree
walker; its default product/package behavior is unchanged. Source-backed bytecode
continues to follow the previously documented normal-cache assumption.

Nine initial missing-API cases failed before implementation. The overlap regression
failed before its fix. The first complexity measurement rejected
_compile_standard_library_files at CCN 6; splitting path-layout calculation from
traversal brought all 18 changed/new functions within the law-5 thresholds.
Ruff for scripts/tests and diff checks pass. Related runtime/dependency/protected
call suites: 84 tests passed on Python 3.12 (43.03 s) and 3.10 (47.86 s). After the
complexity extraction and final unsupported-configuration check, all 33 focused
runtime/dependency tests passed on both (2.69 s / 3.05 s).

Two actual runtime inventories agreed on each interpreter: Python 3.12, 632 files /
40,305,226 bytes, 0.711 s total; Python 3.10, 1,705 files / 64,557,504 bytes, 2.335 s
total. Measurement logs: logs/step7-runtime-identity-py{312,310}-measurement-20261020.json;
regression logs: logs/step7-runtime-identity-py{312,310}-20261020.txt.

This is an on-disk configured-Python component, not proof of full execution
closure. Loaded modules/native libraries versus replaced files, transitive OS
libraries, custom import hooks and archive subdirectory import paths remain
unqualified. Windows/macOS layouts are unqualified; absent shared-library metadata
is explicitly refused, not misreported as a static build. Full configuration
composition, fenced startup/update handling, durable preflight, entrypoint adoption,
backup/recovery, installation/retirement and the useful real full-cycle gate remain
open. No change in installed routing is claimed.

### 2026-10-20: find original work before inspecting live sources

The candidate now resolves the original compile task under the existing lease,
payload and BEGIN IMMEDIATE boundary. Every lineage task must retain the same
compile kind, handler version and verified canonical payload. The shared lineage
reader used by compile and capture now rejects cycles and missing rows instead of
silently treating them as the end of history. Three corruption cases reproduced
that old behavior before the fix; four initial missing-API cases also failed.

begin_compile_work first looks up a stable root address derived from that origin.
Only absent root history permits its local preparation callback to read current
sources/dependencies. Immutable event zero binds the full semantic identity:
origin, source digest, dependency digest and invocation digest. The stable lookup
address is deliberately separate from this complete content identity. Otherwise a
changed source/code hash could select a new work key and bypass an old uncertain
provider call. Root metadata creation/validation is shared in compile_progress;
there is no additional table, index, runtime path or service.

Normal redrive finds the same initial event without calling preparation again.
Tests connect that root to streamed capture and recover the retained snapshot after
live sources change. A separate protected-call fixture interrupts after dispatch,
redrives, finds the old root, and refuses an uncertain provider outcome without a
second call. This uses an isolated queue and fake provider, not installed routing.
Independent tasks remain separate even with identical semantic inputs.

Primary research rechecked 2026-10-20:
- https://docs.temporal.io/workflow-execution — replay follows retained history;
  a stable execution identity is distinct from mutable current files.
- https://docs.aws.amazon.com/step-functions/latest/dg/redrive-executions.html —
  redrive preserves completed work rather than starting an unrelated execution.
- https://www.sqlite.org/lang_transaction.html — serialized write transactions
  provide the existing local boundary for publishing the initial record.

Alternatives rejected: deriving lookup only from current content (misses old work),
scanning all journal records on every recovery (unnecessary when origin is known),
and another registry/table (duplicates existing task lineage and journal ownership).
These sources do not imply provider-side exactly-once execution.

A related red regression found that the first root validator ran only when appending
an event: a writer could leave an artifact before rejecting a wrong origin, and
orphan recovery could return absence without rejecting that origin. The common
artifact-owner validator now checks the same root contract before filesystem work.
Tests assert both rejection and no newly created result file.

One initial test incorrectly attempted two public redrives. The existing
MAX_REDRIVE_GENERATIONS=1 policy rejected it. Its September rationale is in
2026-09-07-how-many-second-chances-an-intent-gets.md and
2026-09-14-one-second-chance-and-no-stuck-task.md. The public test now exercises the
supported redrive, while a separate saved-lineage fixture proves traversal has no
new depth cutoff. The production cap and its existing regression tests remain
unchanged; its law-9 qualification remains part of the overall limit audit, not
proven by this change. No policy override was used to make these tests pass.

Still open: complete environment/configuration composition and loaded-code/update
handling, durable full-batch preflight, CLI/MCP/queue/nightly adoption, retention and
limit audit, backup/restore, installation/legacy retirement and useful real-model
full-cycle evidence. This root lookup is a controller integration primitive; no
claim of an installed end-to-end controller is made.

Validation: 105 related queue/journal/identity/capture/redrive tests passed on Python
3.12 (44.83 s) and 3.10 (49.46 s). After tightening artifact-owner admission, 43
related origin/owner/capture tests passed on each (30.12 s / 33.54 s). The final
streamed-root test initially had a missing work fixture dependency; its fixture
wiring was corrected without changing production behavior, then all 17 origin
cases passed on both (6.27 s / 7.22 s). All 39 changed/new functions meet actual
CCN/if/nesting checks; whole scripts/tests Ruff and diff checks pass. Logs:
logs/step7-task-origin-py{312,310}-final-20261020.txt,
logs/step7-task-origin-owner-py{312,310}-20261020.txt and
logs/step7-task-origin-complexity-20261020.json.

### 2026-10-20: streamed admission reports refused units independently

The archive iterator previously stopped on the first oversized atomic unit, while
`_pack_for_run`'s eager partition already retained independent admissible units.
Four new regression cases failed before implementation (missing refusal interface).
The common admission iterator now supplies complete refused daily units to a
synchronous callback and continues admission. The archive keeps its global citation
index, including refused sources; it never rebuilds that index from accepted sources.
Without a callback the existing fail-on-oversize behavior remains. DLP, malformed
source, and callback persistence errors propagate rather than becoming capacity
refusals. No new capacity or retry limit is introduced; existing qualified-model
capacity and mandatory-context measurement remain authoritative.

Research checked on 2026-10-20:
- Python's [iterator composition](https://docs.python.org/3.10/library/itertools.html)
  supports lazy local stages without retaining the entire input. The page explicitly
  marks Python 3.10 unsupported upstream; compatibility tests do not change that.
- AWS [partial batch failure reporting](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html)
  separates failed records from successful ones to avoid repeating independent work.
- Microsoft's [pipes and filters](https://learn.microsoft.com/en-us/azure/architecture/patterns/pipes-and-filters)
  emphasizes explicit error policy, adequate context, and end-to-end checks.

Chosen here: one local admission generator reused by eager and streamed planning.
Rejected: aborting all independent work on one capacity refusal; materializing all
refusals before returning batches; catching arbitrary errors and labelling them as
oversize. Tradeoff: opted-in streamed admission measures a unit before the grouping
stage rechecks it, as eager partition-then-pack already does. This is a local
planning cost, not evidence of improved total LLM cost. No cloud dependency added.

Tests exercise first/middle/last refusal, complete native atomic-unit refusal,
all-refused input and released records, unchanged default refusal, protected-input
failure, callback failure, and existing context/grouping behavior. Initial complexity
measurement found CCN 7 in the native test; extracting its repeated daily-group
projection brought all 13 changed/new functions within the laws.

This remains an uninstalled controller primitive. It does not yet save a complete
preflight plan or connect the journal controller to production CLI/MCP/nightly.
The caller must durably record refusals and finish preflight before paid dispatch;
merely iterating this API is not that guarantee. Cross-task unknown-outcome
protection, full dependency closure, installation/restore and real-cycle evidence
remain open. Live compiler PID 2129006 was confirmed running at this inspection;
it was not restarted or bypassed.

Validation: 113 related tests passed on Python 3.12 (10.54 s) and Python 3.10
(11.64 s). Whole `scripts/` and `tests/` Ruff and diff checks passed. Evidence:
`logs/step7-stream-admission-py{312,310}-20261020.txt`,
`logs/step7-stream-admission-red-20261020.txt`, and
`logs/step7-stream-admission-complexity-20261020.json` in the installed vault.

### 2026-10-20: durable, complete preflight metadata

The previously missing step between captured sources and per-batch execution now
has an internal `save_compile_preflight` boundary. Seven feature regressions first
failed because the boundary did not exist. It saves canonical line-delimited JSON
through the existing queue artifact writer, then publishes one journal event.
No directory, database, service, configuration switch, package, or schema migration
is added. Its `pending/ready` event explicitly cannot authorize a model call.

The header binds the recorded snapshot event, original-work request digest and
original-work dependency digest. Every accepted or refused unit names the exact
archive daily-record digests, including original bytes, spans and native-frame
metadata identities. A counter verifies all captured records occur exactly once
before the completion record is written. Checking newly consumed records avoids
rescanning the whole source inventory per batch. Accepted entries retain complete
receipt descriptors, packing identity, model, required context and selected source
metadata/hashes; source bodies remain in the snapshot. Refusals remain failures,
not compiled receipts. This is O(source-count) compact metadata, plus one current
batch, not a complete retained source-body snapshot.

Ownership, source identity, and the original root work are checked before planning;
the dependency witness is checked before and after. A separate regression reproduced
accepting a different internally consistent dependency witness for an existing
root; the root binding now rejects it. A test initially expected the wrong exception
class after cancellation: the actual fence raised `LeaseFenceError`, and the test
now names that precise required error. The mixed-source test initially compared
against a helper's intentionally filtered input rather than the actual saved
snapshot; it now checks the exact multiset of saved record identities.

A crash during planning leaves no published artifact/event. A durable file left by
a journal-commit failure is recovered only if its bytes equal a complete local
replan of the immutable snapshot under unchanged dependencies and request. Recorded
plans are verified the same way. This costs local planning again on recovery, but
neither rereads live source files nor invokes the model. It avoids a second semantic
plan parser/validator while the consumer is unfinished. The injected planner is a
trusted internal function; callers must supply the complete effective dependency
inventory and request, and it must not dispatch models. This boundary does not
infer dependency closure or make arbitrary producer code safe.

Research checked 2026-10-20, three independent primary sources:
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): transaction
  atomicity relies on correct locking/fsync; a database transaction does not make
  an unrelated artifact write atomic. Keep existing durable-file-then-event order.
- [Temporal workflow execution](https://docs.temporal.io/workflow-execution):
  durable history supports recovery; replay needs deterministic work identity.
- [AWS Step Functions redrive](https://docs.aws.amazon.com/step-functions/latest/dg/redrive-executions.html):
  preserve execution history and successful work across retry.

Alternatives considered: materializing all batch bodies; persisting an incomplete
prefix as ready; accepting an orphan from its filename or digest alone; maintaining
a second plan decoder/semantic checker now. The chosen implementation streams
metadata and verifies source coverage, then reuses deterministic planning for
recovery. No new numeric work/retry/size ceiling is imposed. The 64 KiB read chunk
is transfer granularity, never an artifact-size limit. Caller deadlines apply to
artifact IO and plan-record writes; existing producer operations still need their
own inherited deadline checks, so this is not an interruptible CPU-task guarantee.

Still open: the preflight consumer and per-batch durable identities/execution,
independent-batch progress with unknown outcomes, cross-origin discovery, source
refresh/publication, complete production dependency inventory/update recovery,
retention/doctor/backup qualification, CLI/MCP/nightly installation and real-cycle
quality/cost evidence. The production entrypoints still use the installed compiler.
This change is not point-7 completion or a claim of installed health.

Validation: 176 related tests passed on Python 3.12 (107.73 s) and Python 3.10
(121.71 s), including 16 new preflight tests. All 46 changed/new functions passed
lizard and AST law-5 measurement; whole scripts/tests Ruff and diff checks passed.
Evidence: `logs/step7-preflight-{red,root-red}-20261020.txt`,
`logs/step7-preflight-py{312,310}-20261020.txt`, and
`logs/step7-preflight-complexity-20261020.json` in the installed vault.

### 2026-10-20: uncertainty belongs to the call; delivery blocking happens at exit

Inspection found `run_model_call` immediately called `queue.fail` for any unknown
provider outcome. That released the only delivery lease and prevented independent
work in the same delivery, contrary to the installed compiler's independent-batch
contract. Five new scope tests failed before the scope existed.

`CompileDelivery` now owns the lifetime of one active queue lease across calls.
Every call uses the same existing immutable reservation/replay path, DLP boundary,
request digest and ownership checks. An unknown outcome is re-raised and remembered;
its dispatch remains in the journal. A caller may catch that error to process
independent work, but leaving the scope blocks the task and raises if the caller
otherwise tried to return normally. Normal completion leaves the lease active for
semantic validation/publication; an answer is not a receipt. This scope retains the
fact of any observed uncertainty conservatively through its exit; explicit operator
resolution is a separate delivery, not an automatic recovery allowance.

The default `run_model_call` wraps one call in that same scope, preserving immediate
blocking for existing single-call consumers. `run_candidate_call` and `CompileReplay`
can carry an explicit active scope; there is no boolean skip-block flag. A closed
or previously entered scope cannot be reopened to forget uncertainty. Every call in a new scope
revalidates the current queue lease. Merely opening an empty scope grants nothing;
an additional origin lookup on entry was removed as redundant with the mandatory
per-call fence. Calls cannot borrow another owner, token,
payload or request identity. A genuine heartbeat-renewed copy of the same lease is
accepted; expiry is verified by the existing database fence rather than comparing
old and renewed timestamps. No new database, task kind, owner registry or dependency
is introduced. The caller still renews its lease.

Research checked 2026-10-20:
- AWS [partial batch responses](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html)
  distinguishes independent failed records from successful ones; ordering-sensitive
  work must not blindly continue. The scope does not prove semantic independence.
- Temporal [activity execution](https://docs.temporal.io/activity-execution)
  separates activity execution from its durable completion evidence. Retain the
  existing prohibition on automatic resend after uncertain transport.
- Microsoft [pipes and filters](https://learn.microsoft.com/en-us/azure/architecture/patterns/pipes-and-filters)
  calls for explicit error propagation, sufficient context and idempotency.

Alternatives: block the whole task immediately; add a flag suppressing queue
blocking; create separate child queue tasks and a new cross-task artifact grant.
The scoped lifetime keeps the existing queue and immutable call safety while
ensuring final failure cannot be swallowed. It does not establish independent
source selection: the forthcoming preflight consumer must do that. Separate child
queue tasks are unnecessary for this local sequential delivery and would add
ownership/retention obligations.

Tests cover independent success after uncertainty, no uncertain resend in the same
scope or after redelivery, replay of the independent saved answer, no success at
scope exit, active-lifetime and owner fences, lost ownership, heartbeat renewal,
and the real protected-client/replay path with redaction and token usage. No new
work/attempt/time/size limit is added. Existing call and queue limits are unchanged,
not thereby declared law-9-qualified. Production entrypoints remain uninstalled;
this is a delivery boundary, not completed batch orchestration or point 7.

Validation: the expanded scope/call/replay/origin/preflight/dependency set passed
150 tests on Python 3.12 (555.73 s) and Python 3.10 (617.58 s). After removing the
redundant origin lookup at scope entry, all 95 directly affected delivery, call and
protected-replay tests passed again on 3.12 (89.16 s) and 3.10 (99.00 s). These are
validation timings under changing disk load, not a controlled performance comparison.
All 24 changed/new functions pass lizard+AST law-5 checks, whole scripts/tests Ruff
and diff checks pass. Evidence: `logs/step7-delivery-scope-{red,green,focused}-20261020.txt`,
`logs/step7-delivery-scope-py{312,310}-20261020.txt`,
`logs/step7-delivery-scope-final-py{312,310}-20261020.txt`, and
`logs/step7-delivery-scope-complexity-20261020.json` in the installed vault.

### 2026-10-20: consume only the retained complete preflight

`iter_compile_preflight` now saves or verifies the complete plan before exposing
any batch. It then rebuilds batches from the immutable source archive using the
existing planner and compares each canonical metadata record byte-for-byte with
the retained artifact. The same `_PreflightWriter` produces and verifies records;
there is no second semantic decoder. Source coverage, refusal ordering, completion
counts and EOF are checked. This correctly rebuilds joined/projected native sources
whose prompt-content hashes need not identify standalone blobs in the capture ZIP.

Each yielded `PlannedCompileBatch` carries the batch, the saved preflight event,
the exact entry digest, and a work ID derived from the preflight-event digest plus
entry digest. IDs survive original-task redrive. They are scoped to that original
work, not a cross-origin overlap detector and not authorization to call a model.
The upcoming per-batch journal admission still must recover retained work before
refreshing mutable context or dispatching. No per-batch copy of the entire capture
archive is created here.

Refusal callbacks run only in checked consumption, after complete durable preflight;
initial planning and recovery validation have no caller refusal side effects.
Callbacks must preserve their own idempotency (the existing source-failure store is
the intended consumer). A callback failure propagates and leaves the plan intact.
The owner, dependencies and caller deadline are checked before each exposed batch
or refusal and at completion. This repeats dependency inventory per unit; that is
an explicit current local cost and remains to be measured in the complete cycle.
No source-count, artifact-size, batch-count or new time ceiling is added.

The generator owns its open archive and artifact descriptors; callers must close
it on early exit. Batch targets and vault records remain lazy and are usable while
the iterator is open. Advancing does not retain previous source bodies on behalf
of the caller. Already exposed metadata is not final execution or publication
progress, and a later consumer error must not be reported as overall success.

Research checked 2026-10-20:
- Python [contextlib](https://docs.python.org/3.10/library/contextlib.html): generator
  resource cleanup on exceptions/explicit closing, and error propagation. Python
  3.10 compatibility remains separate from upstream support (the page marks EOL).
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): a complete
  durable artifact and its journal publication remain separate controlled steps.
- [Temporal workflow execution](https://docs.temporal.io/workflow-execution): replay
  durable execution history with stable identity instead of buying effects again.

Alternatives: yield from initial planning before complete coverage; parse retained
metadata into a second independent batch implementation; directly resolve every
projected hash as an archive blob; duplicate whole source archives per batch.
Reusing the planner plus exact record comparison keeps physical-source proof and
avoids those parallel implementations. It pays an extra local planning pass rather
than claiming the saved preflight alone already executes work.

Seven new feature tests failed before the consumer existed. A later regression
proved returned mutable event details could change subsequent batch identities;
returning a deep event snapshot via the existing canonical snapshot helper fixes
that alias. Fourteen consumer tests cover complete-before-yield, stable replay and
redrive, late planner mutation, owner/dependency changes, refusal durability and
callback failure, no full materialization, released source records, deterministic
resource closing, native projection and returned-event isolation.

Validation: 95 related tests passed on Python 3.12 (44.14 s) and Python 3.10
(48.37 s); all 35 changed/new functions pass lizard/AST law-5 checks, whole scripts/
tests Ruff and diff checks pass. Evidence in the installed vault:
`logs/step7-preflight-consumer-{red,alias-red,green}-20261020.txt`,
`logs/step7-preflight-consumer-py{312,310}-20261020.txt`, and
`logs/step7-preflight-consumer-complexity-20261020.json`.

Still uninstalled. Per-batch admission and exact refreshed-context recovery,
publication/receipts, cross-origin uncertain work, full dependency/update handling,
CLI/MCP/queue/nightly routing, retention/doctor, backup/restore and real full-cycle
quality/token/time evidence remain open. This is verified batch consumption,
not a finished production workflow or closure of point 7.

### 2026-10-20 — complete refreshed context without repeated whole captures

The compiler's target collection cannot be narrowed to selected prompt pages:
`_existing_slugs_by_key`, `_require_current_compile_targets` and `_ApplyPlan` use
all saved targets for resolution, publication preconditions and preimages. A new
batch therefore needs a complete refreshed view, not a prompt-only snapshot.

Research checked 2026-10-20: [Restic repository design](https://restic.readthedocs.io/en/stable/100_references.html)
(immutable hash-addressed data and snapshots), [Git objects](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects)
(content reuse), and [Python ZIP archives](https://docs.python.org/3.10/library/zipfile.html)
(stream/file access and ZIP64). These inform the design; no Git/Restic runtime
compilation dependency is added. Python 3.10 remains compatibility-only, not a
claim of upstream support.

Alternatives: copy the complete archive for every batch; discard unselected
context; build a recursively referenced blob store. The first wastes disk, the
second breaks publication checks, and the third introduces extra lifetime and
recovery complexity. Within the approved existing queue-result artifact layout,
a context delta stores complete changed records plus explicit removed paths.
It names one complete base by logical manifest digest. It never follows another
delta. Changed record blobs are self-contained. A lazy overlay supplies the full
sources, targets and vault-files collections; immutable dailies remain in the
original capture. Changed prompt projections count even when source bytes match.

The no-chain condition follows the approved nonrecursive artifact contract, not
an arbitrary retry or size limit. No count/body-size cutoff is added. Metadata
indices and ZIP directory still occupy memory; computing the delta reads current
context bodies and base record metadata. This does not claim constant memory or
eliminate context scanning. Both archive lifetimes belong to the caller.

Seven tests failed before implementation. Expanded tests cover exact additions,
changes/removals, no duplicate unchanged bodies, lazy reads, deterministic bytes,
wrong base, malformed/contradictory removal lists, corrupted changed bodies,
projection-only changes, duplicate paths and rejection as a complete capture.
The format helper is not yet queue publication: both archive references must be
bound to the same work ownership, recovery, retention and backup checks before
production routing. Durable per-batch admission and all previously listed
full-workflow requirements remain open. This candidate is uninstalled.

Validation: 100 related tests passed on Python 3.12 (44.27 s) and 3.10 (50.20 s).
All 42 changed/new functions passed lizard/AST complexity; whole scripts/tests
Ruff and diff checks passed. Evidence in the installed vault:
`logs/step7-context-delta-{red,green,expanded}-20261020.txt`,
`logs/step7-context-delta-py{312,310}-20261020.txt`, and
`logs/step7-context-delta-complexity-20261020.json`.

### 2026-10-20 — durable batch admission from retained preflight

`begin_compile_batch` now records pending work for the stable planned batch key
before any mutable context capture. It proves the exact recorded parent, original
queue lineage and root identity, current dependencies, planned bytes and address.
An existing admission must match exactly (apart from the current redrive task ID)
and is returned unchanged. No model call is permitted by this pending admission.

Research checked 2026-10-20: [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html)
for journal publication under the existing transaction; [Temporal workflow execution](https://docs.temporal.io/workflow-execution)
for retaining execution identity/history; [AWS idempotent retries](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)
for stable intent identity and refusing changed parameters. Existing SQLite and
queue lease ownership remain the implementation; neither service is introduced.

Alternatives: prepare mutable context before looking up work; trust an in-memory
batch without proving its retained parent entry; decode the entire preflight for
each admission. Instead the verified consumer exposes its entry byte offset.
Admission seeks to and compares that exact canonical record. Source-record hashing
is shared with the preflight writer, avoiding a second encoder. Offsets are
nonnegative integers because they address bytes, not an operator limit. There is
no new batch count or file-size ceiling. The existing artifact boundary still
hashes the whole file on open: this avoids repeated semantic decoding, **not** all
repeated I/O. End-to-end cost remains to be measured and this overhead cannot be
claimed eliminated. Record size follows the actual planned batch metadata.

Nine red tests preceded implementation. Tests include replay/redrive without
mutable context capture, wrong work/entry/address, changed batch and dependency,
lost owner, unrecorded parent, conflicting existing admission and dependency change
during artifact verification. Existing successful full-preflight proofs are reused;
this admission helper does not itself execute compilation or close point 7.

Next, bind a refreshed context artifact to the admitted batch before paid calls,
recover that artifact before any fresh capture, and connect the verified overlay
to compiler replay and publication. Queue-level semantic admission constraints,
retention/doctor/backup references, cross-origin uncertain work, all production
entrypoints and full-cycle qualification remain required. Candidate uninstalled.

Validation: 75 related tests pass on Python 3.12 (51.11s) and 3.10 (74.28s).
All 23 changed/new functions pass lizard/AST complexity checks; whole scripts/tests
Ruff and diff checks pass. Logs: `step7-batch-admission-{red,green,py312,py310}-20261020.txt`
and `step7-batch-admission-complexity-20261020.json` in the installed vault logs.

### 2026-10-20 — retained batch context and orphan recovery

`save_compile_batch_context` joins the admitted batch, recorded original snapshot,
shared delta writer and existing queue artifact publication/recovery boundary.
It checks the recorded or orphan context before invoking the current-context
producer. The default producer is the compiler's `iter_compile_records(())`, not
a second capture implementation. A ready context event references the original
snapshot event and admission digest. The archive itself carries the hash of this
binding, so an orphan from another batch cannot be adopted under a new filename.
`open_compile_batch_context` validates the complete restored context and checks
owner/dependencies before exposing the lazy view under both open archive lifetimes.
Dailies remain in the original plan, not duplicated in the context artifact.

Design uses the same current primary-source research (2026-10-20):
[SQLite atomic commit](https://www.sqlite.org/atomiccommit.html),
[AWS retry identity](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/),
and [Temporal execution history](https://docs.temporal.io/workflow-execution).
The local choice remains one journal plus existing immutable files; no upstream
service/library is imported. Alternatives were recapturing on retry, trusting only
a filename, or copying the complete global snapshot per batch. Binding inside the
archive plus exact original references prevents those identity/cost problems.

The uninstalled delta format advances to v2 and replaces v1 outright, adding the
binding field. Format-only use may be explicitly unbound (null); durable batch
use requires the expected digest. No v1 runtime or migration compatibility is
claimed. The previously preflight-specific artifact recovery helper is shared
rather than duplicated. There are no new runtime paths, databases or size limits.

Eight feature tests were red first. A further red regression demonstrated that a
SourceSnapshot supplied in the targets group could publish an unreadable archive;
the writer now checks record type and declared source digest before publication.
Other tests cover committed/orphan restart without capture, partial source failure,
foreign batch files, changed dependencies and source base, redrive, corrupted
orphan preservation, actual default compiler capture, and owner loss. This guards
local data and recovery, not provider invocation or Markdown publication yet.

The restored view still needs semantic batch refresh/selection against these saved
bytes and model replay/publication/receipts wiring. Queue-level semantic reference
constraints, retention/doctor/backup traversal, cross-origin uncertain work, full
dependency/update handling, production entrypoints, installation/legacy cleanup,
and useful complete-cycle quality/tokens/time evidence remain open. Candidate
uninstalled; no point-7 completion claim. Complete artifact hashing and full-context
validation still incur local reads; full-cycle measurement must include them.

Validation: 86 related tests pass on Python 3.12 (66.68s) and 3.10 (75.16s),
including the real compiler capture iterator. All 43 changed/new functions pass
lizard/AST checks, whole scripts/tests Ruff and diff pass. Installed-vault evidence:
`logs/step7-batch-context-{red,type-red,green,expanded,py312,py310}-20261020.txt`
and `logs/step7-batch-context-complexity-20261020.json`.

### 2026-10-20 — semantic preparation from retained context

The installed refresh wrapper and `refresh_compile_batch_from_context` now share
one `_refresh_compile_batch_inputs` path. This preserves the existing provider
requalification, source-manifest, required-citation and capacity identity checks.
Only the live wrapper captures current pages. Daily source records and the supplied
context are joined through metadata positions; constructing the view does not load
archive bodies. Selected prompt sources are materialized by the existing packer,
while complete publication targets keep their caller-owned lazy view.
`open_prepared_compile_batch` keeps the saved context archives open across semantic
preparation and consumer work, with owner/dependency checks before yielding.

Research checked 2026-10-20:
[Python Sequence contracts](https://docs.python.org/3/library/collections.abc.html)
(index-based mixins and their complexity),
[Temporal execution](https://docs.temporal.io/workflow-execution) (reconstruct from
retained history), [SQLite isolation](https://www.sqlite.org/isolation.html) (the
scope of database snapshots). SQLite isolation does not make mutable Markdown a
snapshot; the existing verified files remain necessary. Python's current docs
refer to 3.15; this change uses longstanding Sequence methods and separately tests
3.10 compatibility, not its upstream support. No new package is added.

Alternatives: a separate replay packer; converting the restored context into a full
tuple; or continuing to call live refresh during replay. Sharing semantic checks
and merging metadata avoids duplicated logic and whole-context body retention.
The indexed view adds O(number of records) metadata and path sorting, rather than
a claimed constant-memory plan. There is no new record/token/retry/size cutoff.
The live path stays until installed durable routing is qualified; it now uses the
same semantic preparation implementation rather than a parallel algorithm.

Seven red tests preceded implementation. The saved path matches ordinary ready
packing and preserves all four tested identity-drift guards. A metadata-only source
fixture refuses body access during joining. An additional integrated test uses
actual semantic compiler validation plus CompileReplay: interrupt after a durable
draft, resume with live capture forbidden, then accept the supported plan with
only three simulated provider calls total (one draft and two reviews). This is
local regression evidence, not a real selected-model quality/token/latency run.

Next: route durable prepared work through resolution, publication, receipts and
repair children in the production execution loop. Prepared archives must stay open
through consumers of all publication targets. Required remaining work includes
queue semantic references, retention/doctor/backup, cross-origin unknown work,
complete dependency/update handling, all CLI/MCP/queue/nightly entries, installation,
legacy cleanup and real useful full-cycle evidence. Candidate remains uninstalled.

Validation: 125 related tests pass on Python 3.12 (66.55s) and 3.10 (74.09s).
All 27 changed/new functions pass lizard/AST law-5 analysis. Whole scripts/tests
Ruff and diff checks pass. Evidence: installed-vault
`logs/step7-saved-refresh-{red,green,semantic,py312,py310}-20261020.txt`
and `logs/step7-saved-refresh-complexity-20261020.json`.

### 2026-10-20 — accepted publication checkpoint before Markdown writes

`save_compile_publication` persists the accepted plan, action key, provider budget
and original publication timestamp under the existing queue artifact boundary.
Its journal binding includes recorded context, exact selected batch metadata and
source identity, effective dependencies and accepted content digest. An unresolved
model dispatch cannot become publication. Existing/orphan plans are validated
before producing a new document or timestamp. An actual compiler cache hit may
reuse the same accepted content; its transient cache-hit flag is not work identity.
The reader requires the exact recorded publication event and verified artifact.

Research checked 2026-10-20: [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html),
[Temporal workflow execution](https://docs.temporal.io/workflow-execution), and
[AWS idempotent retry contracts](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/).
Their relevant distinction is durable intent versus completed effect: queue history
and Markdown publication are different commits. Alternatives were treating model
acceptance as publication, regenerating the timestamp/plan after interruption, or
adding a second publication journal/database. Existing files and compile events
preserve intent; source receipts and the existing Markdown transaction remain
completion authority. No package, runtime layout, automatic repeat or new numeric
limit is added. The JSON publication document occupies one accepted batch's plan
in memory, not a whole source archive; no inherited small-result byte ceiling is
introduced on this writer path.

Four red tests preceded implementation. Seven checkpoint tests now cover original
orphan timestamp, changed plan rejection, actual cache reuse and redrive, recorded
reader identity, refusal to skip an unfinished dispatch, and real local Markdown
publication repeated from the saved document. The latter returns the same committed
transaction/sequence through receipts; page bytes and mtime stay unchanged, and
only the original three simulated model calls occurred. This is filesystem and
semantic regression evidence, not a real selected-model evaluation.

The queue event intentionally remains `publication_ready`: it is accepted intent,
not proof that sources completed. Source receipt completeness, authoritative
transaction lookup, terminal result journaling and quarantine outcomes still need
wiring. A committed quarantine transaction is not enough to mark source work done.
The installed `COMPILE_PUBLICATION_ATTEMPTS = 4` remains an inherited limit requiring
law-9 review; its old comment assumes a model-call window although current claim
assessment no longer calls evaluators. This investigation does not qualify that
limit or silently approve it. All earlier production/repair, reference-retention,
backup/doctor, dependency/update, cross-origin uncertainty, install/legacy and useful
real-cycle obligations remain open. Candidate uninstalled.

Validation: 107 related tests pass on Python 3.12 (73.54s) and 3.10 (87.25s).
The final expanded 9 checkpoint tests, including damaged orphan content/time,
pass on 3.12 (17.92s) and 3.10 (10.10s). All 27 changed/new functions pass lizard/AST
analysis, whole Ruff and diff checks pass. Evidence in installed-vault logs:
`step7-publication-checkpoint-{red,green,expanded,redrive,corrupt,py312,py310,final-py310}-20261020.txt`
and `step7-publication-checkpoint-complexity-20261020.json`.

### 2026-10-20 — source-backed publication completion

`confirm_compile_publication` reads the recorded accepted intent, checks batch,
context and dependency binding, then verifies source receipts under the compiler's
existing Markdown writer gate. The same gate covers appending completion to the
queue journal. It requires the queue, compiler and coordinator to name the same
vault/state/coordinator database. Receipt interpretation and transaction lookup
are shared with `_ApplyPlan._existing_receipts`, not independently reimplemented.
Compiled companion receipts must also remain authoritative.

Only a complete receipt set tied to the accepted action and a committed Markdown
transaction produces `published/completed`. No complete proof returns None and
leaves the accepted intent pending. This confirms one batch, never the whole queue
task. A previously recorded completion is revalidated: missing receipt or changed
transaction/action authority is an error, not permission to replay paid work or
delete history. Intent lookup can now follow its immediate completed successor
back to the exact original publication event. Redrive retains those identities.

Design continues the 2026-10-20 primary-source analysis of
[SQLite atomic commit](https://www.sqlite.org/atomiccommit.html),
[Temporal execution history](https://docs.temporal.io/workflow-execution), and
[AWS retry identity](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/).
Alternatives were trusting an apply-result state, trusting a queue completion
forever, or adding another receipt ledger. A committed quarantine transaction is
insufficient for source completion; authoritative source receipts plus shared
transaction lookup and cooperating-writer fencing are the existing local contract.
The databases remain separate; a crash between them is recovered by checking
receipts, not described as an atomic cross-database commit. No new root/database,
package or count/time/size limit is introduced. Coordinator identity enforcement
follows the approved single-vault/adopted-runtime ownership contract.

Six red feature tests preceded implementation. Another red test showed that a
foreign-vault coordinator was not explicitly refused; identity checks now reject
it before gate/receipt work. Nine tests cover no-receipt refusal, durable completion,
commit-before-journal failure, receipt disappearance after completion, wrong accepted
action, unrecorded intent, foreign coordinator, a committed transaction without
receipts, and redrive. Real Markdown transactions are used; model replies remain
simulated. No real-model or full production-cycle claim is made.

Still required: production execution and whole-task finalization; explicit handling
of quarantine/no-proof outcomes and repair children; queue semantic-reference and
retention/doctor/backup integration; cross-origin unknown work; complete dependency/
update support; inherited publication-attempt law-9 review; installation and legacy
cleanup; useful selected-model full-cycle quality/token/time evidence. Candidate
uninstalled. Completion checks depend on cooperating writer gates and ordinary local
filesystem/database correctness; they are not enduring deletion permits.

Further identity review reproduced a deeper defect before acceptance: the compiler's
existing action key identifies a model request, not its accepted plan. Two distinct
valid plans for one request could therefore satisfy the first completion check.
The new red regression actually publishes different normalized body text with the
same request identity and demonstrates the false confirmation.

Publication now derives its own action key from the original request key and the
complete accepted plan. The original request key remains explicitly stored as
`request_action_key`; cache identity is unchanged. The uninstalled publication
binding/document advances to v2 and replaces v1 outright. Its reader verifies the
plan-bound key, while ordinary receipt schema remains v4. Existing v4 receipts are
still readable; this candidate makes no claim that old request-only action keys
prove a new accepted publication intent. The completion regression now rejects the
other plan. Arbitrary same-user code that deliberately lies to the low-level apply
API is not made a security boundary; production routing must apply the verified
saved publication document, not caller-supplied unrelated bytes.

The earlier `_ApplyPlan._existing_receipts` docstring overstated exact-plan proof.
It now accurately describes source completion/transaction recovery. The new
controller additionally compares the plan-bound publication action before recording
its own completion. This strengthens identity without a second receipt format,
parallel cache, new runtime path or numeric bound. Shared source completion still
allows existing readers to recover historical completed sources.

Final validation after the plan-identity correction: 142 related tests pass on
Python 3.12 (50.32s) and 3.10 (54.79s). All 34 changed/new functions pass lizard/AST
complexity, whole scripts/tests Ruff and diff checks pass. Evidence in installed
vault logs: `step7-publication-completion-{red,green,owner-red,owner-green,expanded,plan-red,plan-green,final-py312,final-py310}-20261020.txt`
and `step7-publication-completion-complexity-20261020.json`. Earlier 141-test runs
preceded the request-versus-plan correction and are not final qualification.

### Saved publication execution (2026-10-20)

Current primary-source recheck: [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html),
[AWS retry identity](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/),
and [Temporal durable execution](https://docs.temporal.io/workflow-execution).
The existing Markdown transaction and queue journal are separate commits. Choose
receipt reconciliation before execution and after publication, rather than treating
an apply return value as durable completion or adding a distributed transaction.
This implements the approved journal/artifact design without new paths, dependencies,
limits or runtime contracts. The saved document supplies plan, plan-bound action key,
provider budget and timestamp; callers cannot substitute a freshly resolved plan.
A recovered committed publication skips apply entirely. No complete source receipts
means no completion, including quarantine transactions. Errors retain the intent.
The existing writer gate remains the mutation boundary; execution checks queue owner,
dependency identity and caller cancellation through the apply cancellation callback.
This does not make CLI/MCP/queue routing complete or replace the still-pending repair,
unknown-source overlap, dependency-closure and installation qualifications.

Execution qualification: four red tests first proved the absent execution path.
Final related suite: 138 passes on each Python 3.12 and 3.10; whole scripts/tests
Ruff, diff check and measured complexity of all 14 new functions pass. The first
expanded 3.12 run had two test-cleanup failures: admission fault injection outlived
its intended scope and failed the outer archive-owner exit check. Restricting the
fault patch to the attempted execution preserves that check; the final run passes.
Logs: `step7-publication-execution-{red,green,py312,py310,final-py312}-20261020.txt`
and `step7-publication-execution-complexity-20261020.json` in private vault logs.

Next integration evidence: the active worker's `_compat_task` drops lease authority,
then `_manual_compile_task` invokes a fresh subprocess without task identity.
MCP `_trigger_compile` calls `run_pending_compile`; nightly invokes the CLI.
Thus connecting only CLI would still lose the existing queue task's authority.
The worker handoff must preserve canonical ownership and original work identity,
without adding an ambient lease environment variable or allowing arbitrary payloads
to impersonate a lease. Repair children still refresh live context, so routing must
also retain their identities and feedback rather than hide this gap behind a wrapper.

### Worker lease handoff investigation (2026-10-20)

Rechecked primary sources: Python multiprocessing documentation
(https://docs.python.org/3/library/multiprocessing.html; current page describes 3.15,
only longstanding spawn/pickle behavior is used), SQLite isolation
(https://www.sqlite.org/isolation.html), AWS idempotent retry identity
(https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/).
The actual worker already owns child cleanup and lease heartbeats. Its processor
receives a compatibility task dictionary that intentionally lacks lease tokens.
Passing credentials inside user payload or new ambient environment variables is
rejected. Reopening a new compile task in the child would lose original identity.
Choose an explicit lease-aware processor adapter bound by the worker after claim,
passing the existing queue handle, immutable lease and absolute deadline through
its existing spawn argument channel. Ordinary processors keep their dictionary
contract. The adopted queue handle carries paths/contracts, not an open SQLite
connection; each transition opens and checks the active row and payload. No second
heartbeat, process supervisor, runtime directory, persistence format or numeric
limit is added. Parent settlement retains its current stale-fence handling when a
controller has already blocked uncertain work. Qualification must exercise real
spawn, saved-answer replay and stale/unknown outcomes, not only pickle round trips.
This is an internal handoff for the approved common controller; connecting the
compiler and complete-task accounting remain required before installation.

Handoff evidence: four initial red tests failed because no lease-aware adapter
existed. Real spawned-child tests now recover a stored answer without invoking its
producer twice, reject a stale transferred token before invocation, preserve an
unknown-outcome block across unblock/reclaim, and exercise parent settlement for
both completion and a controller-created block. The provider is simulated; this
is process/queue qualification, not useful real-model task evidence. Existing
ordinary-worker CLI tests remain included. Both Python 3.12 and 3.10 related suites
pass; all 10 changed/new functions meet measured complexity, whole scripts/tests
Ruff and diff checks pass. Logs: `step7-worker-handoff-{red,green,py312,py310}-20261020.txt`
and `step7-worker-handoff-complexity-20261020.json`. Qualification was on Linux;
Windows/macOS runtime operation is not claimed from these results.

### Joining prepared batch resolution and publication (2026-10-20)

Continue the approved controller with one planned-batch scope owning the saved
archive lifetimes, semantic resolution/replay, accepted-plan persistence and
receipt-confirmed publication. On resume, inspect the current event first: a
recorded accepted publication (including its confirmed child) bypasses resolution
and the model cache entirely. Before acceptance, replay existing answers through
the real validators; an uncertain dispatch remains blocked. Reuse existing
CompileDelivery when supplied so uncertainty does not prematurely release the
lease of a caller processing independently proven batches. Do not treat this
single-batch return as whole-task success, swallow repair-capacity exceptions,
or substitute live context for the saved context.

Sources and alternatives remain the current SQLite atomic commit, AWS retry
identity and Temporal execution references checked above. Choose saved intent
reconciliation over rerunning semantic resolution after acceptance: a cache is
not the authority for an accepted publication. No new retry count, directory,
schema, service or package is required. Whole-call process supervision still
belongs to the worker; the shared LLM client currently has per-call ceilings,
not a universal propagated cancellation/deadline contract. Transition cancellation
and the parent worker's deadline must not be represented as that missing guarantee.
The previous handoff private-note append encountered WriterAdmissionTimeout;
tracked research/commit are retained, and private synchronization remains pending.

Joined-batch qualification: seven initial red tests proved the missing execution
path; the final eight cases cover accepted and committed recovery without semantic
resolution, replay after draft-before-validation interruption, preserved repair
exception identity, caller delivery reuse, cancellation/expiry before calls and
unknown-outcome blocking across reclaim without repeat. Related suites pass 95
cases on Python 3.12 (86.45s) and 3.10 (103.69s). All 16 changed/new functions meet
measured complexity; whole scripts/tests Ruff and diff checks pass. Evidence:
`step7-planned-execution-{red,green,py312,py310}-20261020.txt` and
`step7-planned-execution-complexity-20261020.json` in private vault logs.
The earlier worker-handoff private note/log retry succeeded through the normal
writer gate and the publication-filtered index was rebuilt. This is still an
uninstalled batch executor with a simulated model in tests. Whole delivery and
repair-child execution, unknown-source overlap, dependency closure, all entrypoint
routing, retention, backup/restore and useful real-model qualification remain open.

### Durable repair partition decision (2026-10-20)

The existing `_repartition_compile_repair` preserves complete native source units,
selected context and validated proposal/review feedback. Its output is currently
only in memory. Extend the approved journal/artifact design by recording that
partition decision before any child call. A `review_ready/waiting` event names the
retained batch context, dependency identity, original feedback and deterministic
child descriptors. Waiting deliberately fails the ordinary ready-model-call gate.
No new stage enum, table, directory, daemon, package or numeric bound is needed.
A child work identity binds the exact recorded decision and complete child record.
Reconstruct/validate children using the existing partitioner over retained inputs;
do not add a second partition algorithm or trust stored metadata as executable
instructions. The parent archive scope owns borrowed bodies throughout consumption.

Current primary-source basis is the SQLite atomic-commit, AWS idempotent-request
identity and Temporal event-history references rechecked on this date above.
Alternatives: repartitioning without a retained decision loses correction identity;
new queue tasks lose the original uncertain-call/ownership lineage; storing full
child snapshots redundantly copies the already retained authoritative context.
Choose metadata plus exact references to the retained parent context/event, as with
the accepted-publication document. This is not a recursive blob archive: the full
base archive and one context delta remain the existing context contract.

Validate complete nonduplicated daily-record coverage and strict child progress,
retain indivisible refusal explicitly, and require a ready semantic cursor before
saving a new partition decision. An uncertain dispatch must fail before partition
computation or any new child authority. This stage records a decision, not child
execution or parent/task success. Child admission, context/feedback routing,
recursive completion and doctor/retention semantic reference checks remain needed.

The planned executor now records a capacity decision before propagating the
original exception. On recovery it validates that recorded decision and restores
its reason/feedback without invoking parent resolution. A separate red integration
case proved the prior loss of the decision. Nine checkpoint tests cover source
coverage, context/feedback preservation, stable child identities, orphan recovery,
changed-feedback rejection, refusal before repartition on uncertain dispatch,
recorded-parent checks, indivisible refusal, partition drift and ready-call denial
(the combined preservation case covers several of those invariants).

Qualification: 51 related tests pass on Python 3.12 (41.90s) and 3.10 (46.19s).
Measured complexity initially rejected
`test_repair_decision_preserves_complete_sources_context_and_feedback` at CCN9;
its independent assertions were extracted without removing any check. All 40
changed/new functions now satisfy CCN/if/nesting limits. Whole scripts/tests Ruff
and diff checks pass. Logs: `step7-repair-checkpoint-{red,green,py312,py310}-20261020.txt`,
`step7-repair-routing-red-20261020.txt`, and
`step7-repair-checkpoint-complexity-20261020.json` in private vault logs.
Child admission/execution and recursive outcome accounting remain explicitly open;
this is neither a completed task nor installed runtime qualification.

### Repair child admission and execution (2026-10-20)

Reuse the recorded parent context artifact by reference, as semantic replay already
does; do not create another context archive or reread mutable pages for a child.
The child admission must verify the exact recorded repair, match the full child
record/feedback, and compare inherited targets/vault context. Its sequence-zero
admission binds repair event and child record, and its ready context checkpoint
binds that admission plus the parent context while retaining the same immutable
artifact. The original queue task and lease remain the authority; no child queue
task is enqueued. Each later child call/publication uses its own work identity.

Execute through the same semantic replay and publication path, passing the retained
child feedback to resolve_compile_plan so its existing cache guard cannot accept
an old uncorrected plan. Capacity failures in a child may save another repair
decision; recursive scheduling/completion is separate. Before/after checks retain
caller deadlines, cancellation and dependency/owner verification, with the already
stated shared-client in-call deadline limitation. This follows the same current
SQLite/AWS/Temporal references and approved journal/artifact architecture. Passing
new ambient environment credentials or copying child snapshots was rejected for
unnecessary contracts/storage. No new numeric bound or runtime path is added.

Child execution qualification: initial seven red cases demonstrated missing
admission/execution. The final eight include altered work identity, entry digest,
feedback and target context, stale lease refusal, admission-before-context failure
recovery, exact context-artifact reuse, and real semantic validation/publication
with simulated provider responses. The inherited feedback is observed at the real
resolver; accepted recovery refuses any resolution or live snapshot read and still
succeeds. Root and child execution share one resolve/publication pipeline, and root
admission/replay checkpoint matching were reused rather than copied.

66 related tests pass on Python 3.12 (72.85s) and 3.10 (79.58s). All 26 changed/new
functions meet measured CCN/if/nesting limits; whole scripts/tests Ruff and diff
checks pass. One import-order finding was corrected. Evidence:
`step7-repair-child-{red,green,py312,py310}-20261020.txt` and
`step7-repair-child-complexity-20261020.json` in private vault logs.
This completes individual child execution, not recursive tree scheduling, parent
completion, whole-task settlement or installed CLI/MCP/nightly routing.

### Receipt-backed repair-tree completion (2026-10-20)

A split parent cannot use compile_batch_completion directly: its children have
different accepted-plan keys and committed transactions. Reuse the existing leaf
receipt/transaction validator under one canonical Markdown writer gate, and record
a composite parent completion binding the exact retained repair decision and all
immediate child completion event digests. Do not invent a shared transaction or
trust a previously stored child success without revalidating its receipts.
An iterative post-order proof walk handles nested repairs while the original saved
context stays open. Strictly smaller complete child work already enforces finite
partition progress; no recursion-depth, work-count or retry limit is introduced.

Primary-source basis remains current SQLite atomic commit/isolation, AWS retry
identity and Temporal execution/history references checked above. Per-child gates
could observe mutually inconsistent source states between confirmations. Holding
the existing cooperating-writer gate across the proof avoids that gap; it is not
an OS snapshot or protection against noncooperating editors. Confirming absent
children must not create admission or run models. Missing evidence yields no new
completion, and missing evidence for an already recorded completion is corruption.
The queue task is not completed by this proof; whole-delivery accounting and
execution scheduling remain separate required work. No new persistence root,
service, table, package, stage enum or numeric bound is needed.

Composite qualification: five initial red cases demonstrated the missing proof.
The final cases cover absent/incomplete children without admission, exact child
binding, nested proof, missing receipts after success, journal-failure recovery,
one writer-gate scope and deadline refusal. Additional inspection reproduced a
completed-repair reuse defect: save_compile_repair read the completion itself
instead of its original decision. The common completed-work cursor now resolves
both publication and repair parents, preserving exact prior decision identity.
The dedicated reuse regression failed before that correction.

Expanded suites passed 66 cases per Python version (3.12: 74.86s; 3.10: 82.38s).
After the reuse correction, all 36 directly related cases passed on each version
(3.12: 176.67s; 3.10: 197.39s). These timings are test-run evidence, not production
latency qualification. All 37 changed/new functions meet measured complexity;
whole scripts/tests Ruff and diff checks pass. The dataclass field import was
aliased to avoid shadowing the existing authorization-field loop.
Logs: `step7-repair-completion-{red,green,py312,py310}-20261020.txt`,
`step7-repair-completion-reuse-{red,py312,py310}-20261020.txt` and
`step7-repair-completion-complexity-20261020.json` in private vault logs.
Recursive execution scheduling, independent-sibling failure accounting, whole-task
settlement and installed routing remain open, as do the broader point-7 gates.

### Iterative execution of retained repair trees (2026-10-20, implementation underway)

Rechecked three independent primary sources today: SQLite isolation
(https://www.sqlite.org/isolation.html), AWS retry identity
(https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/),
and Temporal execution history (https://docs.temporal.io/workflow-execution).
Use their transaction visibility, exact-intent identity and durable-history
principles within the approved local queue; no remote workflow service is added.
The fresh builtin graph at 4bef75c8 identifies planned execution and publication
calls; unresolved method edges are supplemented by source inspection.

Execute the verified repair partition with an explicit stack under one delivery
scope, preserving the original open archive and inherited feedback. A capacity
failure already retained by the resolver expands its saved partition; an empty
partition remains unresolved. An uncertain child may leave disjoint verified
source siblings executable, but delivery exit still blocks the task and never
redispatches that child. Owner/dependency/corruption/storage/deadline failures
propagate immediately. Do not broadly catch semantic RuntimeError or invent a
new retry policy. Only receipt revalidation of the whole tree proves completion.

Alternatives: recursive traversal introduces Python's stack-depth boundary;
stop-at-first-uncertain wastes independent source progress; blanket exception
suppression conceals invalid authority. The explicit stack uses existing finite
strictly smaller partitions, no depth/attempt bound or second partitioner.
This is execution orchestration inside the previously approved controller,
not new storage, environment, runtime placement or task-settlement authority.

Qualification: seven initial red cases failed on the absent tree executor. The
first implementation passed their assertions but the uncertain-outcome test kept
an old archive ownership scope alive after blocking/reclaiming its task; teardown
correctly rejected that stale lease. The test now uses the planned-tree entry,
which owns each archive scope, and tests the real recovery boundary without
weakening lease checks. Final cases cover new and retained partitions, nested
repairs, saved-plan recovery without model calls, unknown-child sibling progress
and repeat refusal across reclaim, indivisible refusal, caller-owned delivery,
propagated storage/authority/semantic errors, and deadline/cancellation admission.

Related suites passed 44 tests each (Python 3.12:69.83s; 3.10:75.87s). After adding
explicit authority and semantic-error cases, all 11 tree tests passed on both
(24.83s/26.61s). All 27 changed/new functions pass measured CCN/if/nesting limits;
whole scripts/tests Ruff and diff checks pass. Installed package compatibility
checks pass for both test interpreters (65/67 packages); no package, lock, runtime
contract, model policy or numeric limit changed. This proves compatibility of the
test environments, not upstream support or the unfinished full dependency witness.
Evidence: step7-tree-execution-{red,green,py312,py310,final-py312,final-py310}-20261020.txt
and step7-tree-execution-complexity-20261020.json in private vault logs.
This candidate tree path is not yet installed or routed through production;
whole-task preflight/refusal accounting and final settlement remain required.

### Whole-preflight completion (2026-10-20, implementation underway)

Rechecked SQLite isolation, AWS retry identity and Temporal execution/history
primary sources linked above on 2026-10-20. Apply their stable intent/history and
consistent-read principles to the approved local journal. Completing the last
executed batch is insufficient: refused sources and earlier missing receipts must
also prevent whole-work success. Confirm the complete retained preflight under
one existing canonical Markdown writer gate, rechecking every admitted root tree.
Proof reads existing child contexts only; it must never admit missing children,
capture live context or call a model. An empty source manifest may complete, but
an all-refused or partly refused plan may not.

A compact completion event binds the exact preflight and an ordered streaming
hash of all receipt-validated root completion records plus the batch count. Full
root identities remain derivable from the retained preflight; leaf proofs remain
in the existing journal. This avoids a size-growing metadata list or another
artifact format. Counters alone, independently timed per-root confirmations and
a new aggregate database were rejected: respectively incomplete authority, mixed
writer snapshots and unnecessary storage duplication. Prior completion is always
revalidated; missing evidence after completion is an error. No new numeric bound,
runtime path, external dependency, stage or schema migration is introduced.
Queue settlement remains the existing worker parent's responsibility; it may use
this proof only after the controller finishes the complete delivery scope.

Fresh graph before code changes: generation-18e026640b733863-835a7fdb at11b6d4e5.
Builtin callees for iter_compile_preflight and confirm_compile_repair were fresh
but partial; source inspection supplied unresolved queue/owner/method edges.
Eight initial red cases reproduced missing aggregate execution/proof. Final
coverage includes absent admissions, one completed root, all-root completion,
partial and total preflight refusal, truly empty sources, lost final event recovery,
missing receipts after success, one writer gate, cancellation and unknown-first-root
progress/repeat refusal across redelivery. The existing streamed preflight consumer
is shared by execution and proof rather than adding another plan parser. Optional
refusal notifications do not remove retained refusal evidence or permit success.

All63 related tests passed on Python3.12 (99.40s) and3.10 (109.14s). All37 changed/new
functions pass measured CCN/if/nesting limits; whole scripts/tests Ruff and diff
checks pass. Package compatibility checks65/67 pass, with no package/lock changes.
These are regression-environment measurements, not real useful-cycle latency or
cost qualification. Logs: step7-work-completion-{red,green,py312,py310}-20261020.txt
and step7-work-completion-complexity-20261020.json in private vault logs.
Not installed. Queue-parent settlement integration and CLI/MCP/nightly routing,
full dependency closure, consent/repeat surfaces, semantic reference retention,
backup/restore, installation/legacy removal and real-model qualification remain.

### Queue settlement requires compile completion (2026-10-20, implementation underway)

Inspection found that the worker still accepts a truthy scalar from a compile
processor and can publish/acknowledge it without whole-work proof. The existing
spawned handoff test even confirms success after one saved answer. That was valid
for generic handoff, not for complete compilation. Enforce the approved durable
compiler boundary at publication and acknowledgement in queue schema4. Schema3
retains its historical contract until offline adoption; other task kinds retain
their result contract. No optional flag or hidden bypass is introduced.

Require the original lineage's root completion and its exact retained preflight
binding inside the same verified-lease SQLite transaction. This checks the durable
compiler's completion attestation, not a new live filesystem snapshot; the compiler
already revalidates all receipts under one writer gate before attesting. Refusal
must occur before storing result bytes and again when binding or acknowledging,
so an alternate caller cannot bypass it. Parent settlement preserves the refusal
as an explicit blocked task rather than counting success or spending retry attempts
on an invalid success response. Missing and malformed completion are distinct.

Primary-source rationale is the current SQLite isolation/atomic-write contract,
AWS exact request-intent validation and Temporal execution completion/history
references checked on2026-10-20 above. A callback-only assertion leaves direct
publication/acknowledgement paths open. Re-running compiler planning from inside
a queue write transaction reverses ownership ordering and duplicates proof work;
use the immutable attestation instead. No database, runtime path, schema migration,
package or new numeric limit is added. This enforces settlement; initial capture,
dependency preparation and all entrypoint routing remain separate unfinished work.

Schema3 result handling is retained because the installed vault and explicit
pre-upgrade migration/restore readers still require that contract. Remove it from
normal runtime admission only after installer cutover and all production entrypoints
use schema4, with offline migration/restore compatibility still verified. This is
an explicit unfinished legacy condition, not completion of law8.

Initial regression run: eight cases failed because missing, partial or malformed
attestations were accepted; two full-work success controls passed. After the guard,
27 related cases passed and two older expectations failed: transport-only success
and the schema4 generic-lifecycle fixture. They now require compile refusal and
separately exercise ordinary query settlement; full actual compile success and
spawned parent settlement have dedicated positive controls. Added concurrent
progress advancement between result storage/binding and before acknowledgement.

Final qualification: broad queue/CLI/race/progress suites yielded321 passed,
1 native-Windows skip and2 retention-fixture failures per interpreter
(3.12:237.65s;3.10:298.45s). The retained-success fixture now creates real empty-work
completion instead of bypassing compiler proof; its temporary compiler ROOT/state
must match the queue, and the ownership guard caught the first incomplete fixture
setup. The expired-lease regression injected into obsolete read_compile_artifact;
it now expires the lease at the actual streaming verify_compile_artifact boundary,
keeping its original refusal/no-commit assertions. No production guard was relaxed.
All9 retention cases then passed on3.12/3.10 (7.81s/8.74s), closing both failures.
The unchanged implementation is thus covered by323 passing cases across the broad
and corrected targeted runs, plus the explicitly unqualified Windows check.

All34 changed/new functions pass measured CCN/if/nesting limits; whole scripts/tests
Ruff, diff and package compatibility65/67 checks pass. No package/lock changes.
Evidence: step7-queue-completion-{red,green,py312,py310}-20261020.txt,
step7-queue-retention-{final,verified}-{py312,py310}-20261020.txt and
step7-queue-completion-complexity-20261020.json. The failed interim logs remain.
Fresh graph before code: generation-18e026eceefa2e36-3ecfa566 at6f339d2a; initial
queries while refresh was live were stale and were not used as permission to edit.
Fresh queries plus source inspection covered settlement and publication dependencies.
Candidate enforcement is not installed; capture/dependency preparation and production
routing, semantic audit/retirement, recovery qualification and other point7 gates
remain open. No native Windows/macOS or useful real-model cycle claim is made.

### Atomic initial capture admission (2026-10-20, candidate implemented)

The existing root-before-capture helpers expose a recovery gap: if the root's
source digest commits and the process stops before writing its snapshot, changed
live sources cannot recreate the admitted bytes. No model has run, but the job is
stranded. The production admission path should first durably publish the complete
streamed archive and then append root0 and snapshot1 together in the same queue
transaction. The already durable queue task remains the intent before capture.
A complete orphan archive must carry original task/payload/dependency/request
binding so recovery can adopt it without reselecting changed live sources.

Primary sources rechecked2026-10-20: SQLite atomic commit
https://www.sqlite.org/atomiccommit.html (rollback transaction atomicity and fsync
assumptions), Python3.10 zipfile
https://docs.python.org/3.10/library/zipfile.html (stored ZIP64/unseekable writing,
not a claim of current upstream3.10 support), AWS idempotent retry intent
https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/.
Use existing queue-results immutable publication and fenced queue transaction;
there is no new path, database, service, package, runtime root or numeric cap.
The initial archive adds a versioned admission manifest binding. Its digest
identifies that exact admitted snapshot; individual source hashes remain unchanged.
Ordinary stored input archives remain readable. The original approved one-journal
and streaming-artifact decisions authorize this implementation within their layout.

Rejected alternatives: retain live bytes in memory until both events finish;
recapture current sources under an old digest; reset an immutable original root;
add a second staging database. Orphan adoption validates every retained record and
the original lineage/payload binding, then atomically publishes both events. A
partially registered older root is never silently overwritten; only an existing
matching complete snapshot/orphan can repair it. Runtime routing and replacement
of the old independent-admission helpers remain required before installation.

Qualification: the initial seven regressions failed before implementation and
passed afterward. The full-flow test then exposed that context-delta validation
accepted only complete archive v1; accepting complete v2 bases fixed that shared
validator without permitting delta chains. A separate fault test proved a
compressed orphan manifest was read before rejecting compression; the common
archive-member check now precedes that read. All failed logs are retained.

Final related tests (atomic admission, streaming capture, context delta, streamed
archive batches and queue completion): 67 passed on Python3.12 in37.07s and
67 on Python3.10 in41.42s. The full-flow case uses a simulated provider with real
semantic validation, publication receipts and queue settlement; it is not a real
model qualification. Local lizard/AST checks passed all38 changed/new functions;
whole scripts/tests Ruff and diff whitespace checks passed. Logs:
`logs/step7-atomic-capture-final-py312-20261020.txt`,
`logs/step7-atomic-capture-final-py310-20261020.txt`,
`logs/step7-atomic-capture-complexity-20261020.json`.
This candidate is not installed and does not finish point7.

### Independent known resolution failures (2026-10-20, candidate implemented)

Source inspection before production routing found that the tree executor
continues after retained capacity partitions and unknown provider outcomes, but
the resolver's ordinary exhausted/invalid-plan outcome is a generic RuntimeError.
That exception stops independent siblings and later roots. Catching RuntimeError
would also hide programming or authority failures. The proposed correction is a
specific resolver-outcome exception, caught only at the independent leaf boundary.
The existing source-failure diagnostics must be written before continuing;
diagnostic write failures must propagate. No completion proof may cover the
failed source. Recovery uses the existing recorded model calls, without another
automatic provider attempt for the same request.

Primary sources checked2026-10-20: Python exception hierarchy
https://docs.python.org/3/library/exceptions.html (specific exception types permit
selective handling); SQLite atomic commit https://www.sqlite.org/atomiccommit.html
(recorded transitions must retain their existing transaction semantics); AWS
https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/
(retained request identity distinguishes retries from new intent). The choice
is an application of these mechanisms to this existing local queue, not a claim
that any source specifies this compiler workflow.

Alternatives rejected: catch all RuntimeError/Exception and continue; retry the
model blindly; count an incomplete tree as success; add a second failure ledger.
Use the existing source-failure store and diagnostic function. Unknown outcome,
storage failure, DLP authority errors escaping the resolver, deadline, cancellation
and dependency/ownership failures retain their existing semantics. No new
numeric limit, database, path, package or service is needed. Full production
routing and semantic evidence/retention qualification remain open.

Reproduction: three new tests failed before the correction; actual malformed
provider answers exhausted validation and raised RuntimeError before the next
child was admitted. The fourth capacity control already passed. The new
CompilePlanUnavailable remains a RuntimeError subclass; the existing
non-capacity test now asserts that precise boundary and continued exclusion
from capacity repartition. Arbitrary RuntimeError still stops the tree.

Qualification covers malformed first child and first root, preserved failure
keys, later source publication, refusal of queue success, replay without another
provider call, and diagnostic-storage failure stopping siblings. The root test
initially called publish_result with positional arguments; its actual keyword
contract was corrected without changing product code or the expected refusal.
Related3.12 suites:38 passed in48.48s, then16 passed with that one fixture failure;
3.10:50 passed with the same fixture failure in140.65s. All five resolution
regressions passed after the fixture correction on3.12/3.10 in30.17s/31.68s.
Together these cover51 distinct related cases on each interpreter. All17
changed/new functions pass measured complexity, and whole Ruff/diff checks pass.
Logs are retained as `logs/step7-resolution-failure-*20261020*`.

The fresh graph generation-18e028b9456ce6bf-dbc25795 covers d843b707. The MCP
tool's observation timed out; the same builtin get_architecture implementation
queried locally reported matching commits and returned resolver callers. Graph
coverage remains partial (111597 unresolved edges); source inspection covered
the tree, replay, proof, diagnostics and queue boundaries. This is an uninstalled
candidate with simulated-provider qualification, not completion of point7.

### Shared run planning preparation (2026-10-20, candidate implemented)

The legacy _pack_for_run resolves/probes provider candidates itself, while durable
preflight currently receives an arbitrary caller planner. Production routing
needs one preparation function using the authoritative model selection already
used by the compiler. A prepared value should own the ordered candidates and
selected planning model, expose the existing archive iterator with pending
context, and provide a digest-only request identity. Both eager compatibility
packing and streamed preflight must use that same preparation.

Primary sources checked2026-10-20: Python iterator protocol
https://docs.python.org/3/library/stdtypes.html#iterator-types (lazy iteration);
AWS https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/
(request identity must preserve intent); SQLite
https://www.sqlite.org/isolation.html (transaction isolation does not freeze
external configuration). Therefore candidate/configuration identity is explicitly
bound to the existing admitted-work request, not inferred from the queue lock.

Reject separate CLI/MCP/nightly model selectors, rediscovery during each preflight
replay, or persisting raw endpoints/configuration paths. Hash ordered canonical
descriptors, endpoint, resolution status and the existing Codex planning evidence
including executable identity. Reuse the provider's basis-identity encoder.
This binds planned behavior; it is not a substitute for the live dependency
witness or update fencing. Existing answer reservation and provider timeout
policies are reused unchanged, not requalified by this refactor. No new limits,
settings, paths, package or storage schema is introduced.

Implementation: CompileRunPlanning owns model/candidates and supplies batches()
and request(); prepare_compile_planning is now used by the existing _pack_for_run
and by the streamed-controller qualification. Expired preparation refuses before
discovery, and prepared replay performs no provider rediscovery. The controller
rejects a changed planning identity before a model call.

Testing found a shared serialization defect: Codex executable identity and
configuration-file pairs are tuples. Both the new request and the existing
protected-call digest fed those tuples to the strict canonical JSON encoder,
which correctly refuses them. A separate red test reproduced the protected-call
failure using actual CodexPlanningBasis/_PreparedCodex dataclasses. The common
provider evidence encoder now emits JSON arrays; executable encoding is shared
by protected calls and planning. No serializer rule was weakened. Configuration,
executable, model, capacity, CLI-version, environment and candidate-order changes
remain identity changes; raw private paths/endpoints are not in planning request.

Qualification:13 initial new-planning failures; intermediate50 passed/5 failed
per interpreter exposed the tuple defect, then the separate protected-call red
case confirmed the common cause. Final related suites162 passed on Python3.12
in50.58s and Python3.10 in56.27s. The strengthened capture-to-publication-to-queue
test also passed on both (4.89s/5.12s), including changed-request refusal and saved
work replay without another model call. It uses a simulated provider and fixture
dependency witness, not a real-model or complete-environment qualification.
All21 changed/new functions pass lizard/AST complexity; whole Ruff/diff pass.
Logs: `logs/step7-shared-planning-*20261020*`. Fresh pre-edit graph
generation-18e0294b2b045068-d9553015 matched0bf3d279; builtin graph showed the
packing and protected-digest dependencies, supplemented with source inspection.

Not installed. Production capture selection, full live dependency preparation,
CLI/MCP/queue/nightly routing and the remaining point7 gates are still required.

### Configuration and execution inventory composition (2026-10-20, candidate implemented)

The dependency boundary has separate product/package/Python inventories, but no
production composition including live configuration. Add a configuration digest
covering vault identity, settings bytes and effective numeric settings, current
authoritative/scoped model choice, the actual provider child environment and the
validated DLP policy. Collect twice and refuse a change rather than admitting
mixed observed configuration. Compose existing inventories and check configuration
again after their collection. This is an observation plus later re-verification,
not an atomic lock on external editors or installers.

Primary sources checked2026-10-20: Python subprocess documentation
https://docs.python.org/3/library/subprocess.html (env defines the child process
environment); SQLite https://www.sqlite.org/isolation.html (database transactions
do not freeze external files); SLSA provenance
https://slsa.dev/spec/v1.2/provenance (distinguishing execution parameters and
resolved dependencies). These support explicit inputs/inventory; they do not
prove this compiler's dependency closure.

Use provider_environment unchanged. It currently passes the entire process
environment with the automation marker, so excluding supposedly unrelated keys
would silently omit inputs available to a CLI. Any inherited environment change
therefore changes this witness, including benign changes. This conservative
behavior follows the existing child contract; reconsider it only alongside an
explicit provider environment contract, not a guessed allowlist. Secrets and
private paths enter only hashes. Invalid DLP/settings remain errors.

Reject package versions alone, raw environment/configuration persistence,
per-provider duplicated setting readers and caching by mtime only. No retry loop,
new numerical limit, directory, database, environment variable or package is
introduced. Measure real inventory cost before claiming efficient runtime use.
Loaded-code identity, non-package imports, transitive OS/CLI libraries and
cooperative update fencing remain separate required qualification; composing
these components does not certify them or finish production routing.

Read-only measurement on the candidate with the existing Python3.12 test
environment: product250 files/8935946 bytes in0.108s; packages8772 files/172014387
bytes in3.699s; Python632 files/40305226 bytes in0.375s. This is one observed
collection under current host load, not a latency bound or full-cycle efficiency
qualification. Repeated witness checks can multiply this cost and must be
included in the eventual real-cycle measurement. Evidence:
`logs/step7-dependency-inventory-cost-20261020.json`.

Implementation exposes compile_configuration_dependency_manifest,
compile_execution_dependency_manifest and capture_compile_dependencies using
the existing CompileDependencies verifier. It reuses settings.effective,
model_selection's scoped choice/file reader, provider_environment and the
validated DLP policy/identity encoder. Nine initial red tests preceded code.
The related suites then passed98 cases on3.12/3.10 (44.60s/49.60s); all10 focused
cases passed afterward (1.13s/1.20s), including the additional during-call
configuration change, retained answer and no-call replay after restoration.
Together99 distinct related cases per interpreter. All24 changed/new functions
pass measured lizard/AST complexity; whole Ruff/diff pass.

Real read-only composed capture on the candidate/test environment took4.1s;
re-verification took2.82s and returned the identical digest. No model was called
and no operational state was changed by that measurement. Evidence:
`logs/step7-execution-inventory-cost-20261020.json`,
`logs/step7-execution-inventory-*20261020*`. Pre-edit builtin graph
generation-18e029dd29ad8df7-67b0c5e3 matched12c2b418 and showed the product
inventory dependencies; partial graph coverage was supplemented by source.

Not installed or routed as a default worker yet. Full dependency closure and
the remaining routing, recovery, installation and real-cycle gates remain open.

### Leased queue compiler integration (2026-10-20, candidate implemented)

The default queue worker still runs legacy compile_memory.py in a nested
subprocess without passing its lease. On queue schema4 that path cannot produce
the required lineage-bound work proof, even if it publishes Markdown. Connect
schema4 compile tasks through the existing LeaseProcessor handoff to a compiler
entry that uses the shared compile lock, receipt scope and call ceiling, prepared
run planning, composed dependency witness, atomic source admission and whole-work
execution. The existing parent owns heartbeat, child deadline/cleanup and final
settlement. Other kinds and pre-adoption queues retain their current handler
until qualified migration removes that compatibility requirement.

Primary sources checked2026-10-20: Python multiprocessing
https://docs.python.org/3/library/multiprocessing.html (spawn/importable callables
and explicit resource lifetime); SQLite
https://www.sqlite.org/atomiccommit.html (retain existing transactional progress);
AWS https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/
(retain original task intent across retries). This implements the approved
one-journal controller inside the existing worker; no new process owner, daemon,
queue, path, environment variable or numeric bound is introduced.

Reject nested unowned compile subprocesses for schema4, treating exit status
alone as completion, or admitting live sources again when a retained snapshot
exists. Retain current selection/receipt maintenance on fresh capture only.
Preparation still revalidates current planning/dependencies on recovery; provider
discovery availability and benign inherited-environment changes can presently
prevent replay. That limitation and full loaded-code/CLI/OS dependency closure
must be addressed/qualified before installation. Per-batch diagnostic projection,
direct CLI/MCP/nightly queue ingress, broader semantic retention, full source
memory behavior and real-cycle qualification remain open; one integrated worker
is not point7 completion.

Implementation: queue CLI chooses LeaseProcessor(_manual_lease_processor).
Schema4 compile tasks enter run_leased_compile and the shared
_compile_under_lock executor boundary. Canonical lease/vault checks precede
compile start. The shared planner and dependency factory feed atomic capture
and execute_compile_work; retained snapshots bypass source selection/maintenance.
Verified group count determines the run outcome, without claiming a page count.
The child returns status; publication/acknowledgement remains in the parent.

Nine initial red tests preceded implementation. The first9 then passed in7.79s.
Expanded related suites48 passed per3.12/3.10 (43.65s/47.35s), including actual
spawned execution, replay and parent heartbeat/settlement. A further red test
showed handler version2 was silently executed as version1; admission now
requires the implemented compile handler v1 and refuses unknown semantics.
This is a format contract, not a workload limit; extend it only with an
implemented/qualified handler. Final13 focused cases passed per interpreter
(15.46s/16.73s), including empty work, version refusal and unchanged-payload
authority. Combined50 distinct related cases per interpreter. Simulated provider
and fixture product/package/Python inventories are used in worker integration;
real receipts, transactions, source selection, configuration witness, locks,
spawn, heartbeat and queue completion are exercised. This is not full dependency
or real-model qualification.

All27 changed/new functions pass measured complexity; whole Ruff/diff checks
pass. Evidence: `logs/step7-leased-entry-*20261020*`. Pre-edit builtin graph
generation-18e02a5adf040abb-f143e13f matched8009079b and showed both lock and
worker entry dependencies; source inspection supplemented its partial coverage.
The installed compiler PID2129006 remained live and was not interrupted.

### 2026-10-20 — receipt discovery source lifetime

Fresh built-in `get_architecture` callees for `_source_parts` used generation
`generation-18e02b32effb905f-aa6f21c6`, matching `bbeba73a`; graph partial
(111870 unresolved), supplemented with all source callers and receipt tests.
The regression `test_receipt_scan_releases_previous_daily_before_reading_next`
failed before the fix: weak references still saw the preceding `DailySnapshot`
when the next daily was read. Both source maps retained all visited bodies.

Primary sources inspected on 2026-10-20:
- Python weak-reference documentation explains that ordinary dictionary values
  keep their referents alive, while weak references allow lifetime observation:
  https://docs.python.org/3/library/weakref.html
- Oracle's Java stream contract distinguishes incremental pipelines from stateful
  operations that require buffering; used as an independent design comparison,
  not a Java dependency: https://docs.oracle.com/en/java/javase/25/docs/api/java.base/java/util/stream/package-summary.html
- Apache Arrow documents record-batch streaming versus collecting a full table:
  https://arrow.apache.org/docs/python/ipc.html

Chosen implementation: own only the current source's parts, keyed by logical path
and the existing file identity. Release the previous view before reading the next.
No corpus, file, or receipt is excluded; no numeric cache capacity, new dependency,
path, environment contract or durable format is introduced. Switching back rereads
that source; repeated queries within the current unchanged source reuse the parse.
A failed replacement read must leave no usable cached identity. Alternatives:
unbounded memoization reproduces corpus retention; a configurable LRU adds policy
and still retains multiple bodies; weak-value caching makes reuse depend on unrelated
object ownership and garbage collection. The deterministic current-source lifetime
matches this sequential consumer. Receipt metadata and prefix hashes remain retained;
this change does not claim constant total memory or streaming within a single daily.

Validation: the new regression failed on the original implementation (1 failed,
3 passed), then the source-lifetime, compile-transaction and leased-entry suites
passed all 113 cases on Python 3.12 (31.56s) and 3.10 (34.02s). Coverage includes
same-source reuse, changed bytes, read error/retry, expired discovery deadline,
real receipt authority/identical occurrences and spawned queue execution.
Lizard plus AST checked all 15 changed/new functions with no violations of CCN5,
two-if or nesting2; whole scripts/tests Ruff and diff whitespace checks passed.
Evidence: live logs `step7-receipt-lifetime-{red,py312,py310}-20261020.txt`
and `step7-receipt-lifetime-complexity-20261020.json`.
This candidate fix is not installed; full ingress, recovery and installation
qualification remain outstanding as recorded above.

### 2026-10-20 — retain planning observations for replay

Source inspection finds that `_run_leased_compile` discovers/plans candidates
before asking `admit_compile_capture` whether work is already retained. Only a
planning digest is stored, so recovery cannot reconstruct the original planning
observation when discovery fails or a remote catalog changes. A second, separate
availability check in `_CompileAttempt.resolve` can still block partial answer
replay; qualification must cover both boundaries, not just completed publication.

Primary sources checked on 2026-10-20:
- Temporal replay compares commands with retained history and resumes from saved
  events: https://docs.temporal.io/workflow-execution
- Microsoft Durable Task requires nondeterministic configuration/network inputs
  to be recorded rather than reread inside replay:
  https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-code-constraints
- AWS retains original parameters alongside an idempotency token and refuses
  changed intent: https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/

Chosen direction within the approved snapshot/journal architecture: retain the
request plus original planning observations in the existing immutable admission
archive. Recover that archive before discovery. Reconstruct provider objects from
local configuration and saved planning evidence, checking exact identity; never
store endpoint credentials or raw environment/configuration content. Continue
verifying current dependencies and DLP; persisted observations are not authority
to invoke an obsolete executable or ignore a changed policy. Digest-only legacy
admissions remain readable evidence, not permission to invent a replacement plan.
No new database, directory, service, provider package or workload limit is needed.
Alternatives rejected: repeat remote discovery (nondeterministic recovery), skip
identity checks (could silently change work), or add another persistent planning
store (duplicates the existing crash-safe capture and ownership boundary).

Fresh graph generation `generation-18e02ba4cb991788-8cef79e3` matched d2cd6843
for leased ingress/admission calls; partial graph (111899 unresolved) was
supplemented by source inspection. Initial recovery regressions failed (7 cases,
including two real worker paths failing on repeated discovery). After retaining
planning, a separate fault at `save_compile_publication` reproduced the second
boundary: three model answers were saved, but a subsequently unavailable probe
made recovery fail before reading them (`step7-partial-probe-red-20261020.txt`).

Provider readiness is another nondeterministic observation, so its boolean result
must also be journaled in candidate order. Store it as a probe observation on the
existing artifact-backed checkpoint chain, binding the preceding event and full
candidate identity by digest. No external-call dispatch intent is needed for the
read-only probe: an interrupted unrecorded probe may be repeated, while an actual
model call retains the existing dispatch/answer/unknown-outcome contract. Replay
uses recorded probe results; new work probes normally. This also preserves an
originally unavailable first candidate when a fallback already produced answers.
Skipping every probe or checking only for any saved answer was rejected: either
could silently change fallback ordering or bypass an unrecorded availability step.

The final probe record is one ready selection plus its preceding unavailable
candidates, published together before any model dispatch. An all-unavailable
pass records no selection and may recheck readiness next time; otherwise it would
freeze a transient outage into the immutable work. If model resolution itself
failed for every candidate, initial capture is refused before admission, allowing
normal discovery retry. A saved ready selection preserves fallback ordering even
if availability changes during recovery. Configured local-only transport checks
still run; a saved observation never overrides runtime safety enforcement.

No production limits were added. The existing artifact/schema reader continues to
accept digest-only admission v1; execution requires retained planning rather than
silently manufacturing it. Admission v2 is inside the same uncompressed ZIP64 and
atomic root/snapshot transaction. Planning restoration uses only local configured
candidates and stored Codex evidence, compares their exact serialized identity,
and leaves request()/diagnostic identities digest-only. Fresh capture now inventories
dependencies before storage and again when opening the saved execution, an added
verification cost that remains part of the full-cycle qualification requirement.

Qualification evidence: initial discovery regressions failed 7 cases; the separate
partial-answer/outage reproduction failed after three saved responses. A broad
intermediate regression pass succeeded in 215 cases on each Python 3.12 (338.56s)
and 3.10 (441.10s). After refining all-unavailable retry semantics, the final 27
focused cases passed; all 82 affected semantic/recovery/worker cases passed again
on Python 3.12 (103.36s) and 3.10 (112.20s). The final tests cover crash after one
saved answer (one original plus two remaining simulated calls, three total),
publication interruption after all three answers (no additional calls), fallback
ordering, unavailable-to-ready recovery, failed-resolution retry, orphan capture,
changed dependencies, malformed planning, Codex evidence round trip, stale owners,
invalid probes and uncommitted probe retry. One measured CCN6 helper was split;
final lizard/AST verification covers all 56 changed/new functions with no failures.
Whole scripts/tests Ruff and diff whitespace checks pass.
Logs: `step7-retained-planning-{red,py312,py310,final-focused,final-py312,final-py310}-20261020.txt`,
`step7-partial-probe-red-20261020.txt`, `step7-retained-planning-complexity-20261020.json`.
The providers in these fault tests are simulated. Native Codex transport, full
dependency/update closure, cross-origin source uncertainty, retention/doctor/backup,
other ingress routes, diagnostics projection and safe installation remain open.
This does not establish point 7 completion or installed behavior.

### 2026-10-20 — cross-work source uncertainty

Fresh built-in graph generation `generation-18e02cf0a34023a4-4ddcb556` matches
152247c9. `_append_compile_event`, `_batch_admission_event`, repair-child admission
and `_run_reserved_call` were inspected through the graph; partial coverage
(112070 unresolved) was supplemented with source and queue ownership code.
Current reservation protects one work identity; another task/work can dispatch
against the same source after the original outcome becomes uncertain.

Primary research inspected on 2026-10-20:
- SQLite transactions permit one writer; BEGIN IMMEDIATE makes check/reservation
  one serialized operation: https://www.sqlite.org/lang_transaction.html
- PostgreSQL range/exclusion documentation illustrates non-overlap invariants
  and half-open intervals; used as a design reference, not a new dependency:
  https://www.postgresql.org/docs/current/rangetypes.html
- AWS idempotency guidance distinguishes request identity from changed intent:
  https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/

Chosen implementation direction: add source scope metadata to existing batch
and repair-root journal events, derived from verified source descriptors. Check
other unresolved calls inside the existing queue write transaction immediately
before appending dispatch. No table, index, database, runtime directory, external
service or persistent lock is added. Existing lease/payload fencing remains.
A ready result whose retained bytes are missing/corrupt cannot silently count as
recovered. An orphan answer is conservatively unresolved until its original work
adopts it. Repeat consent permits the original call's recovery path; it must not
open a race for another work before a durable answer exists.

The source exclusion is logical path plus overlapping half-open byte span, not
only a whole-file or request digest: repartitioning and appending a day must not
bypass uncertainty. Daily logs are append-only evidence. Rewriting the overlapping
part does not prove independence and requires resolving the original uncertainty;
non-overlapping new spans and different paths remain independent. This is an
explicit safety constraint, not a workload/volume limit. Missing historical scope
cannot establish independence and therefore fails closed for competing uncertain
work. Reconsider the representation only with verified revision/byte lineage that
proves independence. Alternatives rejected: a whole-vault stop (blocks unrelated
sources), prompt-hash-only dedupe (context changes bypass it), and a preflight-only
check (race before dispatch). A future lookup index would require measurement and
the existing explicit migration process, rather than unapproved runtime DDL.

Initial fault reproduction: six cross-work cases failed, including changed spans,
changed bytes at the same address, missing scope and a lost answer. After the guard,
two older independent-delivery fixtures failed because neither declared source
scope. Their existing assertions were preserved; fixture roots now name the actual
fixture bytes and different logical paths. The focused pass then succeeded in 23
cases, including a two-thread/two-connection dispatch race and repeat-consent timing.
A broad intermediate pass succeeded in 177 cases on Python 3.12 (427.00s) and 3.10
(494.58s). Review additionally reproduced an invalid-but-hash-valid answer releasing
a competing work (one failing test). The queue now reuses the existing canonical
answer decoder and call-binding validator, not only the artifact hash. Tests also
cover a syntactically valid answer belonging to another work, an orphan answer,
actual worker source spans, and a fake published status after an unknown dispatch.
Final expanded regressions finished in `step7-source-overlap-final-py312-20261020.txt`
and `step7-source-overlap-final-py310-20261020.txt`: each passed 178 cases and failed
only the mistaken foreign-answer fixture described below (476.46s / 553.92s).
All 48 changed/new functions pass lizard+AST complexity checks; Ruff/diff pass.

A separate local rollback-journal/FULL measurement over 10,000 disjoint historical
works / 20,000 rows took 0.8517s with a warm cache. It measures metadata scanning,
not answer-artifact I/O, model work or real end-to-end cost. The scan is linear in
retained work count; semantic retention and full-cycle scaling qualification remain
open. Evidence: `step7-source-overlap-scan-cost-20261020.json`,
`step7-source-overlap-{red,answer-red,green,focused,py312,py310}-20261020.txt`,
`step7-source-overlap-complexity-20261020.json`. No production bound was inferred
from the benchmark size. The lazy queue-to-controller decoder dependency uses the
one existing response protocol; it adds no package and invokes no provider.

The final run exposed a fixture mistake in the new foreign-answer case: changing
only task_id does not change `_call_binding`, deliberately, because redelivery can
change ownership while preserving the same work/call. The test now changes work_id
instead; the original refusal assertion remains. This corrects the evidence being
injected, not the validator or the acceptance condition. The two binding cases are
rerun on both Python versions in `step7-source-overlap-binding-py{312,310}-20261020.txt`:
2 passed on Python 3.12 (11.95s) and 2 passed on Python 3.10 (15.33s).
Production code did not change between the expanded runs and those reruns; only
the foreign work identity in that fixture changed. Together these runs qualify
all 179 distinct cases on each version, without claiming either original broad
run was green. Final Ruff, whitespace and all 48 function complexity checks pass.
The implementation remains a candidate: no installed-vault behavior or real-model
full-cycle cost is established by these simulated-provider regression cases.

### 2026-10-20 — ingress ownership and immutable request binding

Built-in generation `generation-18e02f3467fc4db9-18e6eb45` matches 82bbb83f.
Graph callees for `_with_verified_lease`, `compile_origin`, `_require_leased_row`
and `_validated_payload_bytes` were inspected; partial coverage was supplemented
with the transition callers, serializer and lease/queue source.

Before connecting CLI/MCP/nightly to the leased executor, source inspection found
that `_with_verified_lease` validates the stored payload and live owner/token but
does not compare the lease object's kind, handler version, input hash and payload
with that stored task. A compatibility dictionary compared with the same lease
object cannot detect a changed lease object. Selection options must not be read
from such an unchecked object.

Primary research checked on 2026-10-20:
- SQLite's single-writer transactions support checking ownership and input
  identity in the existing transaction: https://www.sqlite.org/lang_transaction.html
- AWS recommends binding idempotent intent to request identity and treating
  changed parameters separately:
  https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/
- Python documents fresh-interpreter spawn and serialization requirements;
  existing parent-owned worker supervision should be reused:
  https://docs.python.org/3/library/multiprocessing.html
  and https://docs.python.org/3.10/library/multiprocessing.html .
  The latter is now labelled unsupported upstream. Compatibility testing on
  3.10 is not a claim of upstream security support; changing the product's
  supported Python floor is outside this change.
- MCP cancellation is cooperative and can race completion; it is not proof
  that a remote model did no work:
  https://modelcontextprotocol.io/specification/2025-06-18/basic/utilities/cancellation .
- Additional review of canonical hash comparisons:
  https://www.rfc-editor.org/rfc/rfc8785 . This is a design reference for stable
  representations, not a claim that the project's restricted canonical JSON
  implements JCS. Existing NFC normalization and refusal of floats stay intact;
  replacing that persisted identity contract would require a separate migration.

The chosen immediate correction is one immutable-input check at the existing
verified-lease transaction boundary, after stored-payload validation. This
preserves corruption demotion and prevents changed in-memory task input from
authorizing protected transitions. Compare kind, handler version and input hash,
then recompute the payload hash with the existing canonical serializer. Do not
add a second request store, lock, table, service or package. Reject a mismatched
caller without modifying a valid stored task, so its legitimate owner can
continue. Alternatives rejected: trusting the lease's hash without recomputing
its payload, validating only the compatibility dictionary, or adding compiler-only
checks while result publication and heartbeat remain permissive.

The remaining ingress work must bind file/closed-day/trigger selection, preserve
spawned-lock ownership, supervise cancellation/deadlines through the existing
parent worker and keep dry-run semantics explicit. None of those requirements
is considered complete merely by strengthening the lease boundary.

Reproduction before the correction: 22 failures and one passing canonical-order
control in `step7-lease-input-red-20261020.txt`, plus a failing compile-entry
test in `step7-lease-input-compile-red-20261020.txt`. The entry test confirms
that a forged lease reached the compile-lock runner before correction.
The initial focused pass succeeded in 37 cases (22.31s). Additional cases cover
redaction-equivalent changed secrets and stored-corruption demotion. The canonical
comparison does not call the ingress redactor again. Broad checks passed all 142
cases on Python 3.12 (270.20s) and Python 3.10 (362.13s), recorded in
`step7-lease-input-py{312,310}-20261020.txt`.
All 11 changed/new functions pass lizard/AST checks; whole Ruff and diff checks
pass. No new resource limit or external dependency is introduced.

Next ingress contract to implement under the already approved common-controller
scope: a versioned `compile_request` inside the existing task payload binds a
vault-relative daily file (or all pending sources), `closed_days_only` and
`trigger`. Validate explicit field types and unknown fields, preserving the
current rule that an explicit file takes precedence over the closed-day filter.
The spawn lock token is ephemeral ownership, not persisted request intent.
Resolve a CLI-supplied path once when forming intent; on recovery, validate the
logical path without rereading or requiring the saved source to remain live.
Fresh capture still performs the existing containment and source checks.
Historical tasks without this request retain their original all-pending/auto
meaning; new requests must be bound into retained execution identity rather than
silently recovered under historical defaults. The actual routing and tests are
still unimplemented at this checkpoint. Do not claim this plan as completed code.

### 2026-10-20 — source selection enters retained execution intent

Generation `generation-18e02ff06f35ee33-e51b1a0c` matches 8da46356. Built-in
callees for `run_leased_compile`, `_leased_compile_request`, `select_dailies`,
`read_compile_execution` and `_require_canonical_daily_id` were inspected.
Partial graph coverage was supplemented with source selection, snapshot admission,
CLI parsing, nightly invocation and MCP entry code. The ingress research and
alternatives above apply; no new runtime location or dependency is proposed.

The original leased entry hardcoded file=None, closed_days_only=False and
trigger=auto. Seventeen tests failed before correction, demonstrating ignored
intent, invalid parameters reaching the lock, wrong capture scope and missing
retained selection. The leased executor now validates a versioned request from
the already fenced lease, maps it into existing source selectors and retains it
as `leased-compile-request/v2`. The source path grammar and date validation are
the existing canonical daily-source contract, not a new workload restriction.
The request parser calls the queue's existing canonical-day validator lazily;
it adds no package or module-initialization cycle.

The encoder is ready for subsequent frontend routing. It excludes spawn-lock
tokens and refuses dry-run rather than producing a mutating task from a preview.
That dry-run omission was separately reproduced before adding its refusal.
Existing tasks without the versioned request keep their exact v1 all-pending/auto
identity, necessary for recovery of retained work. They are not silently assigned
new caller options. Full frontend routing, targeted claiming and parent deadline/
cancellation supervision remain open; this is not a claim of unified ingress.

The first focused pass succeeded in 17 cases (13.40s), including actual source
capture, explicit-file precedence and recovery after the selected live file was
deleted, with no extra simulated provider call. Expanded regressions including
the dry-run case passed all 94 cases on Python 3.12 (108.23s) and Python 3.10
(142.65s). All 19 changed/new functions
pass lizard/AST complexity checks; whole Ruff and whitespace checks pass.
Evidence: `step7-request-selection-{red,dry-red,focused,py312,py310}-20261020.txt`
and `step7-request-selection-complexity-20261020.json`.
The real selected medium provider and installed behavior remain unqualified.

### 2026-10-20 — selecting a foreground task without bypassing queue admission

The existing adopted claim path chooses the highest-priority eligible task.
A foreground compile must be able to choose its own enqueued task, while keeping
the same live-owner, payload, capture-link, source-fence, availability and attempt
checks. Processing arbitrary queued tasks until the desired one appears changes
the caller's requested work; constructing a lease directly bypasses admission.
A separate foreground queue or database would duplicate authority.

The selected design adds an optional task ID to the existing adopted `claim`
path and appends a parameterized primary-key predicate to the shared eligibility
query, inside the same BEGIN IMMEDIATE transaction. The ordinary worker order
is unchanged when no ID is supplied. Missing/ineligible targets return no lease;
they never fall through to unrelated work. Corrupt targets still follow normal
demotion; valid unrelated tasks remain ready. This is an input selector, not a
new runtime component or a bypass/retry capability.

Research basis checked on 2026-10-20 is the SQLite transaction, AWS idempotent
intent and Python process-ownership documentation cited in the ingress section
above. The same existing claim/lease/worker mechanisms remain authoritative.
Use the existing task-ID validator: its 1..256 UTF-8-byte constraint matches
the current `tasks.id` SQL CHECK, rather than introducing a new arbitrary limit.
The existing queue attempt/lease policies remain and still need the complete
point-7 law-9 audit; this change does not claim to finish that audit. Targeted
worker supervision and frontend routing must follow before ingress is complete.

The worker supervision step below is now qualified; frontend routing remains open.


The existing `_work_one` now forwards the optional selector through
`_claim_when_reachable`; all parent heartbeat, child supervision, retained-result
adoption and whole-work proof checks are reused. The option is omitted entirely
for ordinary calls, preserving legacy/custom claim interfaces. A spawned real
compile processor with a fake semantic backend publishes and settles its chosen
task while a higher-priority unrelated task remains untouched. A canceled target
starts no processor. No frontend calls this new route yet.

Before implementation, targeted claim tests reproduced 17 failures and targeted
worker tests reproduced three failures from the missing selector interfaces.
Final claim/related checks passed 129 tests on Python 3.12 (111.59s) and Python
3.10 (160.10s). Separate worker/related checks passed 24 tests on Python 3.12
(22.93s) and Python 3.10 (24.34s), after worker forwarding was added. These are
separate runs, not one full-suite result. All 26 changed/new functions passed
lizard/AST complexity checks. Whole Ruff and whitespace checks passed.

Built-in navigation used generation `generation-18e030951782eaed-7494588d`
at base `1f165b3a`; partial graph results were supplemented by source inspection.
SQLite EXPLAIN QUERY PLAN on the candidate schema confirms target selection uses
`sqlite_autoindex_tasks_1 (id=?)`; it is not a full-cycle performance measurement.
Evidence: `step7-target-claim-{red,py312,py310}-20261020.txt`,
`step7-target-worker-{red,py312,py310}-20261020.txt`,
`step7-target-claim-complexity-20261020.json` and
`step7-target-claim-query-plan-20261020.json` under the private vault logs.
Caller deadlines/cancellation and ephemeral compile-lock handoff remain ingress
work. These fake-provider tests do not qualify installed behavior or medium-model
quality, token cost, recovery cost or completion of point 7.


### 2026-10-20 — ephemeral compile-lock handoff through supervised execution

Before foreground routing, the existing leased entry must accept the compile
lock already held by its parent. Reacquiring it without the token refuses the
child; releasing it before dispatch opens an unnecessary competing-writer
window. Persisting the token in task payloads would mix execution ownership with
retained user intent and leave stale ownership on recovery. Select the existing
spawn-token contract instead: pass the token as a spawn-picklable processor
argument, then through the leased entry into the existing lock acquisition.
The child must leave the parent's lock intact after success or failure; wrong
or absent tokens must not authorize an unrelated child. No new lock, database,
runtime path, environment variable or durable request field is introduced.

Primary sources rechecked 2026-10-20:
- Python multiprocessing documents explicit spawn arguments and parent-owned
  process lifecycle: https://docs.python.org/3/library/multiprocessing.html
- MCP cancellation specifies stopping requested work and releasing resources;
  it does not prove a remote model never executed:
  https://modelcontextprotocol.io/specification/2025-06-18/basic/utilities/cancellation
- AWS distinguishes caller intent from retries and late outcomes:
  https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/

These sources support the ownership/intent separation; they do not establish
this implementation's correctness. Existing explicit spawn remains in use,
without adopting newer Python-only APIs. A subprocess regression must exercise
correct and wrong ownership tokens and retained parent ownership. Cancellation
propagation is a separate remaining integration requirement, not implied by a
successful token handoff.


Built-in navigation used `generation-18e031785b314715-af55e8ad`, fresh at
`89ad6437`, for the leased entry, queue processor, lock acquisition and child
fixture. Partial graph results were supplemented by source inspection. The
three new subprocess scenarios failed before implementation with the worker's
processor-exception envelope because the handoff keyword was unsupported. After
adding the ephemeral parameter to the two existing functions, all three passed:
correct token executes and permits parent settlement, missing/wrong token calls
no model, and every scenario preserves the parent's exact lock record. The
task payload contains no token. Related regression suites passed 55 tests on
Python 3.12 (50.45s) and Python 3.10 (55.64s); all six changed/new functions pass
complexity checks. Whole Ruff and whitespace checks pass. Evidence:
`step7-parent-lock-{red,focused,py312,py310}-20261020.txt` and
`step7-parent-lock-complexity-20261020.json` in private logs.

The running legacy compiler still owns its lock; it was neither stopped nor
replaced. Its log reports target snapshot conflicts on the private development
checkpoint page, which the agent was also appending to during compilation.
Further checkpoint-page writes are deferred until that process is terminal to
avoid more competing edits. Read-only findings are retained separately in
`logs/step7-live-compile-conflict-20261020.json`; input-budget and validation
failures are also present, so page contention does not explain all failures.
The exact contribution of each append to each failed batch was not traced.


### 2026-10-20 — caller cancellation through the existing worker lifecycle

Source inspection after the parent-lock change shows that `run_worker` accepts
cancellation but `_drive_worker` checks it only between tasks. The running child
waits for a result, deadline or lease loss. A foreground MCP request therefore
cannot yet stop its requested work through this lifecycle. Wrapping the child
runner in a partial would bypass `_interruptible`'s identity check and silently
remove heartbeat-loss interruption; this alternative is rejected. A second
foreground process supervisor would duplicate cleanup and settlement authority.

Extend the existing worker argument chain and child-run state with an optional
caller cancellation callback. The callback stays in the parent, never in the
spawned processor payload. Preserve lease-loss checking alongside cancellation.
Check bounds before claiming and spawning, while retrying busy claims, and while
waiting for the child; refuse a new claim once cancellation/deadline is observed.
A result already available still goes through normal result/cleanup validation.
Cancellation does not erase durable model dispatch or answer records and does
not prove the remote request did not execute. Existing unknown-call recovery
must block another model invocation after a stopped child dispatched a call.

Research uses the three primary sources checked in the preceding section on
2026-10-20 (Python process lifecycle, MCP cancellation, AWS retry identity).
Reusing the existing child lease-check interval avoids a new polling policy;
its original documented basis and remaining worker limits still belong to the
full law-9 audit. No new worker, queue, persisted state, environment variable or
runtime directory is introduced. Injected custom runners remain responsible
for their own synchronous execution; the standard spawned runner provides the
interruptible lifecycle. This design requires regression proof, including a
real child stopped after durable model dispatch and recovery without a repeat.


Cancellation of already claimed work settles through the existing failure path
as `worker_cancelled` with capability `caller_cancelled`. This blocks automatic
retry until explicit unblock, rather than silently rescheduling canceled work.
Any dispatched model call remains unknown until its own durable recovery resolves
it; unblock is not repeat consent. Before claim, cancellation or expiry returns
no lease and spends no task attempt. The existing deadline is also checked before
the first claim, fixing the previous first-attempt exception. The former busy
retry pause is capped to the remaining deadline; its inherited 50 ms policy has
not been justified by this change and remains in the limits audit.

Built-in navigation used generation `generation-18e031df2939ec44-c58c0efb` at
`8af8f95d`, with source supplementation for the partial graph. Seven new scenarios
failed before implementation from unsupported callback interfaces; a separate
baseline expired-claim regression reproduced a claim after the deadline. The
public-worker cancellation scenario was not run against the old implementation:
its known missing propagation would wait out the full hosted-platform hang bound.
Focused qualification now passes ten scenarios, including a real child stopped
after a durable model dispatch and an explicit unblock that still cannot repeat
that call. The existing lease-loss regression now expects both parent callbacks,
and a separate test proves neither replaces the other. Cancellation during child
exit cleanup does not discard an answer already received. This remains a local
fake-provider qualification, not an installed full-cycle or cost claim.


Final related regression qualification passed 239 tests on Python 3.12 (143.92s)
and 239 on Python 3.10 (148.52s), covering the core queue/CLI, targeted claim and
worker, compile dispatch/parent lock, lost leases, child-tree cleanup, answers
arriving at a deadline, SQLite failures, heartbeat stop and processor errors.
All 31 changed/new functions pass lizard/AST complexity checks; whole Ruff and
whitespace checks pass. Evidence: `step7-worker-cancellation-{red,expired-red,
focused,py312,py310}-20261020.txt` and
`step7-worker-cancellation-complexity-20261020.json` in private vault logs.
The callback check surrounds claim attempts; it does not interrupt a SQLite
statement already executing inside the queue's existing busy-timeout contract.
Full dependency/update fencing, limits qualification and frontend routing remain
open, as do installation, encrypted restore and the real medium-model full cycle.


### 2026-10-20 — resume the same unfinished foreground request

Connecting a foreground call by always enqueuing a new task would abandon the
original durable work identity on every retry. A permanent dedupe key derived
only from selection would instead keep returning a completed task forever,
preventing compilation of later source edits. A source digest in the caller's
request would require recapturing inputs before the durable controller restores
its saved capture, and would turn an edit during recovery into a different job.

Choose an atomic lookup-or-create in the existing adopted queue transaction:
for the same compile handler and canonical payload, select the oldest unfinished
leaf of a redrive lineage. Ready, leased, blocked and dead work remain the same
request; the foreground does not unblock, redrive or reset its attempt count.
Succeeded and canceled leaves permit a later request. Explicit redrive children
replace their dead ancestors for selection. Completed results/receipts and the
cross-work unknown-source gate remain authoritative when new work is created.
Matching checks both payload bytes and their stored digest; hash-only or bytes-
only corruption of a matching task refuses rather than silently replacing it.
The same source fence, canonical payload validation and task-row insertion remain
in use. No new table, index, directory, schema or persistent pointer is added.

This restores saved work, not a guarantee that a resumed capture contains source
edits made after it was admitted. Fresh pending sources still require subsequent
work; frontend status must describe that distinction. The query may examine
unfinished rows, so full-scale cost qualification remains open. Source rationale
is the SQLite transaction and AWS retry-intent documentation already checked on
2026-10-20, plus Python's parent-owned execution contract; caller cancellation
and deadline use the existing queue mutation guard. Built-in graph generation
`generation-18e032760b096a15-93f9911a` is fresh at `c0293e15`; partial graph results
for enqueue, dedupe, row insertion and redrive were supplemented by source.


The ingress connection retains `_compile_under_lock` for all callers. Its default
mutating executor becomes the queued foreground executor; dry-run keeps the
existing nonpublishing preview and leased children supply their explicit durable
executor. This avoids recursion and keeps CLI/MCP/nightly on the same selection.
A foreground request requires the adopted compile-journal schema before enqueue.
It projects a queue-worker owner through the existing registry and renews that
owner plus queue projection with the existing owner-heartbeat context using the
queue's renewal method. The owner and compile-lock token are passed ephemerally
to the leased child; publication projects the same verified owner into its writer
gate. Caller cancellation and owner loss stop supervised execution, and cleanup
releases only ownership acquired by this caller. No new runtime actor type or
marker is introduced. The registered queue-worker covers the foreground
execution; the existing compile.pid remains the shared compile exclusion lock.

Completion is read from the task after supervised settlement. A blocked, leased,
ready or dead task must not be reported completed merely because no child ran.
The task ID and retained state are reported for follow-up. CLI generation refresh
keeps its existing behavior, including previously committed batches on failure.
This connection must be proven with a spawned semantic-fixture compiler and the
real adopted registry, then qualified with the actual medium provider later.


Qualification exposed a race in the initial separate enqueue/claim connection:
a competing worker could acquire the new foreground task before its parent,
then refuse the parent's compile lock and spend an attempt. The dedicated
regression reproduced this before the correction. Final admission selects or
creates the request and invokes the ordinary `_claim_in` in one BEGIN IMMEDIATE
transaction. `claim` and `claim_compile_request` share that implementation,
including payload, source/capture fences, expiry recovery, attempt policy and
lease CAS. The worker's existing post-claim supervision was extracted unchanged
into `_work_claimed`, shared by ordinary and atomic foreground claims. The
uninstalled enqueue-only prototype was removed; no parallel admission path is
retained. Concurrent identical requests produce one task and exactly one lease.
Cancellation during admission rolls back both new task and claim.

The new foreground tests reproduced five failures through the legacy executor,
while dry-run already passed. Three initial entrypoint integrations then passed
with real subprocesses, canonical queue/coordinator owners and a fake semantic
backend. Extended coverage verifies later work buys no additional model calls
for already receipted sources (three calls on the first task, zero on the next),
retains a supplied owner after publication, refuses an older queue schema, and
never marks source hashes compiled for invalid model output. The source of the
nightly call was inspected: `scheduled_nightly._compile_step` invokes the same CLI.
This is routing evidence, not a successful real nightly pass.

Four older boundary tests initially failed because they intercepted `_run` as
the default or used unadopted fixtures. The lock/ceiling and owner-forwarding
unit tests now intercept the queued executor; the legacy batch failure/continued
batch tests explicitly select their retained legacy executor. Their original
failure, hash, log, lock and source-failure assertions remain. New adopted
foreground failure tests cover the default path, rather than treating schema
refusal as proof of model-failure handling. Legacy execution remains only for
preview and explicit retained tests during this uninstalled migration; removal
still requires qualified installation and the law-8 usage audit.

### 2026-10-20 — isolate worker diagnostics from MCP transport

The foreground MCP regression reproduced non-protocol text on stdout
(`logs/step7-foreground-stdout-red-20261020.txt`, one failure). The worker
returns its outcome over a dedicated multiprocessing pipe; stdout is not its
result channel. Parent success reporting and inherited child descriptors were
still directed to the protocol channel.

Primary sources reviewed today: [MCP stdio contract](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports),
[Python 3.12 os.dup2](https://docs.python.org/3.12/library/os.html#os.dup2),
and [Linux dup(2)](https://man7.org/linux/man-pages/man2/dup.2.html).
MCP reserves stdout for protocol messages and permits stderr diagnostics.
Descriptor duplication changes the child's destination without changing the
parent's descriptor and covers inherited native writes, unlike Python-only
stream redirection. Choose child-only fd 1 → fd 2 before executing the handler,
and explicit stderr for the parent's completion diagnostic. Global parent
redirection would race with MCP writers; suppressing output would lose evidence;
patching each handler would leave native/subprocess output uncovered.
No new limit, dependency, path or persistence contract is introduced. Linux
regressions cover Python/native/inherited subprocess output; Windows behavior
remains to be qualified on Windows. Failure to redirect prevents handler entry.

Validation: before the output-channel fix, the extended queue/compiler/MCP suite
passed 774 tests with two native-Windows skips on each of Python 3.12 (364.78 s)
and 3.10 (403.65 s). After the fix, the foreground/request-resumption/targeted-worker/
cancellation/child-tree suite passed 43 tests on each (43.29 s and 46.93 s).
The original MCP stdout regression now passes; the spawned-worker regression
also covers Python print, direct fd writes, descendant output and unchanged
parent stdout. An initial new test harness called `_work_one` without its required
arguments; that harness was corrected to exercise `_run_processor_child` directly
before these final runs. Ruff and whitespace checks pass. Lizard plus AST checked
all 52 changed/new functions against c0293e15: no complexity, if-count or nesting
violations. These are Linux semantic-fixture qualifications, not an installed
nightly run, Windows qualification, paid-provider quality result or completion
of point 7.

### 2026-10-20 — do not retain every daily part during source capture

Source inspection found `_daily_capture_records` requests `_CapturedPartitions.daily`,
which builds every `DailySnapshot` and holds them in both a list and the proof
context. This duplicates the complete daily's bytes in part bodies before the
first record is written. It also computes receipt disposition for all parts
before emitting any. The full original daily and entry/frame metadata are a
separate remaining materialization boundary; this step must not claim to remove it.

Sources reviewed today: [Python iterator/grouping semantics](https://docs.python.org/3/library/itertools.html#itertools.groupby),
[Arrow streaming versus collected tables](https://arrow.apache.org/docs/python/ipc.html),
[Java stream stateful buffering](https://docs.oracle.com/en/java/javase/25/docs/api/java.base/java/util/stream/package-summary.html).
These independently distinguish incremental consumption from collecting and from
operations requiring retained groups. Python implementation must stay compatible
with 3.10; no recent `batched` API or Arrow/Java dependency is needed.

Choose an iterator for daily part construction and receipt filtering, using the
existing physical native/tool atomic-unit grouping. Only the current indivisible
unit plus lookahead need be buffered by filtering. The existing list-returning
API remains a wrapper for callers that explicitly need the complete collection.
The streamed capture must not populate the eager caller's per-part proof registry.
Rejected: lowering the daily byte cap loses valid input; dropping completed
companions breaks native/tool evidence; replacing the immutable original with a
live file read breaks retained-source identity. No numeric cache limit or format,
path, dependency or runtime-root change is required. Full original bytes, parsed
entry/frame metadata and a possibly large indivisible event still remain; their
streaming requires the next source-access change, not an unsupported memory claim.

Pre-edit builtin graph `generation-18e0343b80e59169-a9649953` matched
5eb2c447; queried capture, part construction, filtering, partition context,
atomic grouping and manifest writing. The graph was partial (112631 unresolved),
so actual source and consumers supplemented it. Weak-reference regressions failed
for both receipt-free and receipt-filtered capture before the fix (2 failed,
1 passed). The same first part is now released while its original daily is still
being consumed. Shared eager wrappers preserve existing part identity, ordering,
receipt flags, source spans and atomic native/tool grouping.

Validation: 40 focused tests passed first. Final extended suites passed all 264
cases on Python 3.12 (66.16s) and 3.10 (71.50s), including streamed archive round
trips, durable capture, citation context, native/tool completeness, continuation
identity, transaction recovery and foreground worker execution. Ruff and diff
checks pass; lizard plus AST measured 12 changed/new functions with no failures.
Evidence: `logs/step7-daily-parts-*20261020*`. These checks establish part lifetime
and retained contracts, not bounded whole-daily memory or real-provider efficiency.
The within-day archive blob cache also still retains part bytes; its streaming
replacement remains required alongside the original-daily read.

The builtin repository-retention dry run identified three obsolete candidate
index generations, retaining its current and previous generations. Applying that
plan succeeded; disk availability increased from approximately 2.0 to 4.4 GiB.
No `run/` state or private knowledge was removed. Private checkpoint-page updates
remain deferred because the live installed compiler has targeted that page.

### 2026-10-20 — archive replay retains only shared record fields

Inspection found `_restore_archive_day` wraps both readers in unbounded
`functools.cache`. The bytes cache retains each distinct part body until the day
ends, even after the consumer releases the corresponding DailySnapshot.

Primary sources: [Python cache lifetime](https://docs.python.org/3.12/library/functools.html#functools.cache)
(reviewed today), and the independently authored Arrow IPC and Java stream
references reviewed above on this date. All distinguish retained state from
incremental consumption; Python explicitly documents that cached results remain
strongly referenced. No Arrow/Java runtime dependency is proposed.

Choose field-driven reuse: retain only blob identities named by the current
record's shared original-content and entry/frame fields. A following record
reuses matching verified values and releases identities it no longer names.
Read part content through the existing verified blob reader without memoizing
it. This preserves reuse of a large original across its parts, canonical JSON
checks, original/part hash checks and same-path different-original records.
No cache count, byte threshold or source exclusion is introduced. Re-reading
all shared fields for every part would waste I/O; a numeric LRU would add
unjustified policy; retaining every digest recreates the defect. This removes
archive accumulation of part bodies, while full original bytes and metadata
still require the later streaming source-access work.

Fresh pre-edit navigation used generation `generation-18e034c9184f4b78-cb3d3383`
at ec2b68f2 for day restoration, field restoration and both verified readers.
Partial graph coverage (112651 unresolved) was supplemented by source reads and
all reader call sites. The lifetime regression failed before implementation
(1 failed, 2 passed): a bytes-subclass lifetime observer on verified part bodies
was never released while its day remained open. It does not instrument shared
original bytes or change digest checks. After the fix, the focused archive,
planning and required-context suite passed 33 cases. Additional coverage checks
same-path A/B/A original versions: returning to A reads it again rather than
keeping every historical version.

Tradeoff: repeated identical part bodies may now be read again when they recur;
shared original and entry/frame blobs retain reuse. Complete model-cycle cost
has not yet been measured. Original bytes are still loaded whole, and
`_restored_archive_record` still hashes that original for each part; neither
remaining full-daily memory nor repeated-hash cost is claimed solved here.

Final related qualification: all 153 tests passed on Python 3.12 (92.89s) and
3.10 (101.65s), covering original sharing, streamed planning, corruption refusal,
protected dispatch/recovery, context deltas and spawned foreground execution.
Ruff and whitespace checks pass. Lizard plus AST measured all 13 changed/new
functions with no violations. Logs: `step7-archive-parts-*20261020*` under the
live vault's private logs. This remains an uninstalled candidate; the installed
compiler PID2129006 was observed live and was neither stopped nor bypassed.

### 2026-10-20 — reuse an original digest only for the same immutable bytes

The restored daily validator hashes `original_content` for every part although
archive blob loading already validates it and consecutive parts share the same
immutable bytes object. For N parts this adds N complete-original hash scans.
The per-record declared original hash must nevertheless be checked every time.

Primary sources reviewed today: [Python frozen dataclasses](https://docs.python.org/3.12/library/dataclasses.html#frozen-instances),
[Git content-addressed objects](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects),
[Nix content addressing](https://nix.dev/manual/nix/2.24/store/store-object/content-address).
The Git/Nix references motivate content-derived identity rather than trusting
names; they are not implementation dependencies or claims that their formats
match ours. Python documents frozen fields as emulated immutability, not a
sandbox against hostile Python code. Existing exact-bytes checks exclude
mutable buffers from a reused digest.

Choose an internal frozen digest proof constructed from the actual original
bytes, with its digest computed rather than supplied by a caller. Reuse it only
for that identical bytes object; check each record's declared original hash
against it. A changed original creates a new proof and releases the previous
one. Ordinary restoration without a proof still hashes the supplied bytes.
Reject proof-shaped arbitrary objects and proofs for other byte objects. No
serialized boolean, unchecked digest, process-global memoization, new persistent
format, dependency or numeric cache limit is introduced. Always rehashing is
correct but repeats a linear scan for every part; trusting the manifest string
alone would not establish actual content identity. The archive's own blob hash
check remains, so a newly loaded original is still hashed there and once for
this scoped proof. Full original loading remains a separate unfinished task.

Pre-edit builtin navigation: `generation-18e0354fac46341e-503996c9` matched
9ca91ca1; queried day restoration, record validation, original-digest comparison
and the affected lifetime-observer test. Partial graph coverage (112686
unresolved) was supplemented by exact callers. The red regression counted 16
complete-original hashes in record validation for a 16-part daily, instead of
one. After the fix that counter is one; the separate archive blob verification
still hashes the newly loaded original as before. This is a measured hashing
work reduction, not a full-cycle latency/token result.

Focused proof/archive/planning tests passed all 30 cases. Final related suites
passed all 158 cases on Python 3.12 (94.50s) and 3.10 (104.39s), including changed
record digests, changed bytes, proof-shaped arbitrary objects, mutable buffers,
late blob corruption, shared-source release, protected dispatch/recovery and
foreground execution. The lifetime test observer now forwards the verifier's
keyword argument; all its original release assertions remain. Ruff and diff
checks pass; lizard plus AST measured all 12 changed/new functions without
violations. Evidence: `logs/step7-original-digest-*20261020*`. This code is still
uninstalled. Full-original memory, dependency/update qualification, retention,
restore/install/legacy cleanup and the real medium full cycle remain open.

### 2026-10-20 — validate retained capture bodies without materializing them

Source tracing found that both saved-capture validation and orphan admission
iterate fully restored records, loading original daily bodies just to verify
hashes. This is independent of the planner's still-unfinished original-range
access. Replace this validation path with metadata validation plus complete
streamed verification of every referenced byte blob. Do not instantiate fake
snapshots or skip record structure/native-frame conversion.

Primary sources reviewed today: Python 3.10 [ZIP member streams](https://docs.python.org/3.10/library/zipfile.html#zipfile.ZipFile.open)
and [signature binding](https://docs.python.org/3.10/library/inspect.html#inspect.Signature.bind);
independently, the Arrow IPC streaming and Git content-addressed object references
reviewed earlier today. Streaming avoids collecting complete bodies; content
addresses require verification of actual bytes, not filenames. Signature binding
can check the existing dataclass constructor's accepted/required fields without
allocating a content-bearing instance. Current snapshot dataclasses have no
post-init validators; native-frame and entry metadata conversion must stay active.
Python 3.10 is an inherited compatibility target, not a claim of upstream support.

Use the existing IO_CHUNK_BYTES transfer buffer, not a new source-size limit.
Check the caller deadline between reads and after validation. Keep only verified
digest identities for this validation pass, and only current shared JSON metadata;
never keep all blob bodies. Mark a digest verified only after EOF and a matching
SHA-256. Both existing recovery entrypoints must use the same validator, and their
queue ownership/admission checks remain outside it unchanged. Reject metadata
whose content/original digest disagrees, malformed constructor fields/native
frames, missing blobs and corruption at any position. Alternatives: eager restore
retains the problem; hashing only ZIP bytes omits semantic record checks; trusting
member names skips actual content verification. No new format, directory, runtime
root or package is needed. JSON manifests and entry/frame metadata still load in
memory; this is not completion of streaming compilation or a constant-total-memory
claim.

Fresh builtin graph `generation-18e035be3f7695e5-2edd1e36` matched 1ca1f466;
queried both recovery validators and metadata conversion. Partial coverage
(112710 unresolved) was supplemented by the actual queue artifact opening,
whole-file digest and descriptor-identity guards. These outer guards are retained;
this validator does not promise safety for arbitrarily mutable caller-owned
archives or add a new filesystem isolation boundary.

Initial regressions exposed whole-member reads and restored-record construction.
A third initial failure was the test expecting only ValueError/TypeError for a
missing path; the existing reader raises KeyError, which the rejection test now
also accepts. Five original negative controls passed. An additional red test
showed expired validation opened the ZIP before checking its deadline; both
recovery entrypoints now check before opening. Deadline advancement after the
first original-body chunk interrupts before reading the next one. Hash mismatch
at the end of the original, missing/unknown fields and malformed native frames
all still refuse recovery. The existing queue lease-loss and orphan-retention
checks remain unchanged.

Final related suites: 187 passed on Python 3.12 (197.41s), and 187 passed on
3.10 (235.23s). Ruff/diff checks pass. Lizard plus AST measured all 24 changed/new
functions without violations. Logs: `step7-stream-validation-*20261020*`.

A separate three-pair comparison loaded the committed 1ca1f466 controller and
candidate against the same retained ZIP containing an 8 MiB original. Input
construction was outside tracing and released before measurement. Median peak
additional Python allocations during validation fell from 8,406,686 to 149,957
bytes; median measured validation time was 0.04116s versus 0.01417s. Full raw
measurements and scope are in `logs/step7-stream-validation-memory-20261020.json`.
This is a local tracemalloc/fixture result, not RSS, production throughput,
real-provider tokens, useful output quality or completion of point7. Source
capture, daily parsing, native-frame metadata and planning still need the
coordinated original-range refactor. No installation was performed.

### 2026-10-20 — parse entry identifiers from ranges, not copied entry bodies

The canonical `daily_entries` parser slices every complete entry before checking
whether the heading already declares its identifier. For operation markers it
then decodes and splits the complete entry, although only the first nonempty
content-line prefix determines its time. This creates full-entry copies on the
capture, citation, claim and archive paths.

Primary sources reviewed today: [Python splitlines and strip semantics](https://docs.python.org/3.12/library/stdtypes.html#str.splitlines),
[Unicode line breaking](https://www.unicode.org/reports/tr14/), and
[RFC 3629 UTF-8](https://www.rfc-editor.org/rfc/rfc3629). Python's documented line
boundary set is the compatibility contract here, not the whole UAX14 algorithm.
UTF-8 may span transfer boundaries, so per-chunk independent decoding would
change malformed/partial sequences. Keep the existing replacement-error behavior
through an incremental decoder.

Choose range-based identifier lookup: declared heading IDs require no entry body
slice. Marker lookup decodes existing IO_CHUNK_BYTES transfers, skips the first
Unicode line and leading whitespace/blank lines, then examines only the first
nonempty content line. Normalize only the already-allowed bullet whitespace;
retain the existing timestamp regex as authority. The remaining candidate needs
at most the grammar width of an optional backtick plus `[00:00:00]`, not an
arbitrary text cutoff. Leading/bullet whitespace remains unbounded and is scanned
incrementally. Ordinary `readline` would change Unicode separator behavior;
collecting whole lines would retain the giant-line defect; a custom parser state
machine is unnecessary when iterator composition preserves the existing grammar.
No source, line or record is truncated. Entry start/end detection is unchanged.
No new path, dependency, runtime root or persistent format is introduced. The
original source buffer and boundary metadata still remain; this is a prerequisite
for range-backed originals, not completion of whole-daily streaming.

The first incremental implementation still iterated discarded text character by
character. An 8 MiB long-header/bullet-space fixture took 0.479/0.621 seconds,
versus 0.092/0.120 seconds in the original parser. A regression that rejects
character iteration of discarded chunks failed before the correction (82 other
cases passed). Replace that traversal with regex line-boundary search and
`lstrip` on bounded decoded chunks; preserve the first-content-line boundary
before normalizing bullet whitespace, so a timestamp on a later line never
becomes valid by accident. No new numeric limit is introduced.

The corrected paired fixture compares actual entry IDs and byte spans against
the committed 00d11b6a parser. For the long header, additional traced Python peak
allocations fell from 25,167,069 to 199,174 bytes; for long bullet whitespace,
from 25,167,075 to 396,739 bytes. Times were 0.093/0.112 seconds and 0.127/0.136
seconds respectively (baseline/candidate). These single local pairs ran while
regression suites were active: they establish neither a throughput improvement
nor RSS/full-cycle performance. The earlier fivefold slowdown no longer appears
in this measurement. Raw first/final measurements are retained separately in
`logs/step7-entry-ranges-memory-first-20261020.json` and
`logs/step7-entry-ranges-memory-bulk-20261020.json`.

The fresh built-in graph at generation-18e03698cde9ac31-e46ff3a1 matches
00d11b6a, but remains partial (112770 unresolved). Direct parser/caller inspection
supplements the graph for compilation, claims, archive and citation consumers.
Ruff and diff checks pass; Lizard plus AST measured all 21 changed/new functions
without violations. Installation and the complete real-provider cycle remain
outstanding.

Final related suites pass on both supported interpreters: 400 tests on Python
3.12 (82.76 seconds), and 400 on Python 3.10 (90.65 seconds), including archive,
claims, citation, source-choice, transaction and foreground queue consumers.
Logs: `step7-entry-ranges-bulk-py312-20261020.txt` and
`step7-entry-ranges-bulk-py310-20261020.txt`.

### 2026-10-20 — remove the canonical boundary parser's buffer requirement

Fresh built-in callers at generation-18e037dab3164973-9893fdda match be067a7a.
The graph is partial (112821 unresolved); source inspection confirms that
`daily_entries` still calls `re.finditer` on the whole original, although its
identifier reader now uses ranges. The splitter already needs only length,
searches and bounded slices. A non-buffer range source reproduces the blocker:
the canonical parser raises TypeError before it can discover any entry.

Primary sources rechecked today:
[Python regex anchors and byte patterns](https://docs.python.org/3.12/library/re.html),
[Unicode line breaks](https://www.unicode.org/reports/tr14/), and
[UTF-8 byte encoding](https://www.rfc-editor.org/rfc/rfc3629).
Delimiter matching here is the existing ASCII-byte/LF grammar; Unicode
splitlines applies only to the marker's content lookup. Replacing both by a
generic Unicode line reader would change stored byte boundaries. Strict UTF-8
and the existing block-ID validator remain authoritative for declared IDs.

Select range-based prefix discovery and line-ending validation. Find heading
and marker prefixes only at offset zero or immediately after LF, locate LF and
the first preceding CR, and require EOF, LF or CRLF exactly as the old regex
does. Find the heading's closing bracket within that valid line and retain its
existing ID validation. Scan a marker's lowercase hexadecimal identifier in
existing I/O chunks; do not copy its unrestricted tail or impose a new ID limit.
Use one implementation for byte buffers and future verified file ranges.

Alternatives: retaining regex would still require a buffer; independently
applying it to chunks loses split delimiters; accumulating lines keeps the
giant-line memory problem; adding a partial-regex dependency is unnecessary for
these two fixed delimiter grammars. The tradeoff is more explicit parsing code
and additional searches, so compare semantics against the committed regex and
measure representative runtime before acceptance. A follow-up regression found
that a malformed giant heading ID was still copied in full. Validate its UTF-8
incrementally through the complete ID, retaining the candidate only while the
existing block-ID regex accepts it. Even after syntax/length rejection, continue
strict decoding so a later malformed UTF-8 byte keeps the previous error priority.
This uses the existing ID grammar, not a new source/line restriction. This stage
does not claim the file reader, native journal parser or full source path is
streamed.

The first range-source regression fails on be067a7a with the expected buffer
TypeError. A second regression rejects full materialization of giant malformed
IDs; both ASCII-invalid and trailing-invalid-UTF-8 variants fail before the
incremental validation change. Differential cases freeze the former regex and
whole-ID decoder independently of the new implementation. They cover EOF/LF/
CRLF/stray CR, mid-line prefixes, malformed markers, invalid IDs, exact spans,
unrestricted marker identifier length, and UTF-8 split across 1/2/3-byte chunks.

Three local timing pairs against be067a7a preserved identical entry IDs and byte
spans. An 8 MiB heading tail changed from median 78.5ms to 3.18ms; an 8 MiB marker
tail from 110.3ms to 34.6ms. The 20,000-short-entry fixture initially changed from
62.9ms to 101.4ms after full incremental ID validation. A single-chunk decoding
path avoids creating an incremental decoder when the ID has only one transfer;
both paths share strict decoding/error handling and the same ID validator. A
fresh three-pair short-entry check then measured 65.4ms versus 93.9ms. The
remaining approximately 29ms overhead is an explicit tradeoff for range-based
parsing, not a claimed universal speedup. It introduces no new data limit or
package. These are parser fixtures, not real-provider full-cycle measurements.
Raw records: `step7-range-boundaries-final-timing-20261020.json` and
`step7-range-boundaries-single-chunk-timing-20261020.json`.

Remaining original-range integration, confirmed by direct source inspection:
`_daily_capture_records` still calls `_read_daily_source`; `_restore_archive_day`
still asks `CompileInputArchive.blob` for the entire original; `_blob_digest`
and partition/journal proof caches require exact `bytes`; `_archive_record_value`
uses recursive `asdict`; `_native_journal_index` still requires a regex buffer
and retains matched bodies; native frame metadata retains encoded/text values.
These consumers must change together with owned range lifetime and read-time
integrity verification. Mapping a mutable live file is not an immutable capture.
Removing the type guards alone would weaken proofs and is not an acceptable
integration. Existing per-page/source/part/ID limits still require the broader
law-9 audit; this parser change does not certify those inherited limits.

Final related suites after the single-chunk change: 849 passed on Python 3.12
(80.22 seconds) and 849 on Python 3.10 (87.38 seconds), including compilation,
claims, archives, source choices, historical partitions, transaction and
foreground queue consumers. Ruff/diff checks pass. Lizard plus AST measured all
25 changed/new functions without violations. Evidence is retained in
`logs/step7-range-boundaries-qualified-py312-20261020.txt`,
`logs/step7-range-boundaries-qualified-py310-20261020.txt`, and
`logs/step7-range-boundaries-complexity-20261020.json`. No installation or
real-provider qualification has been performed for this change.

### 2026-10-20 — owned verified original ranges and part provenance

Fresh built-in callees at generation-18e038c8049b17be-516be737 match 5b390aff;
the partial graph has 112859 unresolved edges and is supplemented by the
validator, archive reader, queue artifact guard and partition proof source.
The retained-capture validator checks each blob hash independently, but does not
prove a daily part equals the declared range of its original. Replacing one part,
its blob hash, record hash and manifest hash with a self-consistent foreign body
is accepted today. A new regression reproduces that acceptance before any change.

Primary sources reviewed today:
[Python ZIP member streams](https://docs.python.org/3.12/library/zipfile.html),
[Restic content-addressed blobs and integrity](https://restic.readthedocs.io/en/stable/100_references.html),
and [NIST secure hash standard](https://csrc.nist.gov/pubs/fips/180-4/upd1/final).
NIST's page notes planned revision; this uses the project's existing SHA-256,
not a new cryptographic algorithm. Content hashes depend on the already trusted
queue checkpoint/manifest; they are not independent authentication or an OS
sandbox. Restic's design motivates per-blob verification, not a claim that this
unencrypted archive inherits Restic's encryption guarantees.

Choose a context-owned original member reader. Scan its full declared extent
and EOF, verifying the existing whole hash while recording hashes of existing
IO_CHUNK_BYTES blocks. Subsequent ranges reread and verify touched blocks;
one current verified bytes block is retained as the I/O buffer. The scope closes
the member even on failure; closed/expired scopes cannot read cached bytes.
Use the resulting range iterator to hash each part's declared original span and
compare with its content identity before accepting the capture. Reuse the current
original only while consecutive records name that same digest.

Alternatives: whole bytes keep the memory problem; copying to temporary files
adds disk space and lifecycle obligations; mmap of live/mutable files does not
make immutable evidence; reading ZIP private offsets introduces a second format
parser. Standard member streams preserve the current ZIP validation. Inspected
Python 3.10 and 3.12 ZipExtFile.seek implementations differ: 3.10 may read large
amounts internally to seek forward. Therefore rewind only to zero and advance
with explicit bounded reads and deadline checks. This can cost extra sequential
I/O for backward access; measure the actual validation/canonical-parser paths
before qualification. No new runtime path, package, archive format or data cap.
The full capture/planner/native-frame range migration remains unfinished.

Implemented the range view inside the existing controller and used it in the
shared retained/orphan capture validator. It retains computed block digests and
the current verified block, not the original body. The canonical entry parser
and partitioner run against the actual ZIP-backed view in a regression, without
a buffer adapter. Range/index/search tests cover backward access, cross-block
matches, partial underlying reads, empty originals, changed blocks, declared
extent mismatch, and closed/expired scopes. Capture tests reject self-consistent
foreign parts and invalid ranges; consecutive parts reuse one member and both
success and validation failure close it. Native frame metadata and the planner's
existing bytes-only proof consumers have not yet migrated.

Related suites passed 227 tests on Python 3.12 (81.51s) and 227 on 3.10 (94.38s).
After inlining an unnecessary scope iterator helper and adding the empty-original
case, the final focused suites passed 57 tests on each interpreter (0.90s and
0.86s). Ruff/diff checks pass; Lizard plus AST measured all 41 changed/new
functions without violations. Evidence: `step7-original-ranges-*20261020*`.

Three local pairs against 5b390aff used the same archive with four pending parts
of an 8 MiB original, constructed outside tracing. Median additional traced
Python allocations were 149,446 bytes before and 161,676 after. Median validation
time was 14.10ms before and 21.79ms after. The extra block hashes and part-range
proof therefore have measured CPU/metadata cost, while avoiding a whole-original
allocation. Raw measurements and scope are in
`logs/step7-original-ranges-memory-20261020.json`. This is neither RSS nor
real-provider full-cycle performance, token cost or output quality. No installed
source, runtime ownership, private page or live compiler was changed.

### 2026-10-20 — native journal index retains ranges, not matched bodies

Fresh callers at generation-18e03a2ea9099e04-36317867 match c9a89060.
The graph remains partial (113013 unresolved); direct inspection covers the
compiler's journal cache, frame verification and fact-key source authority.
`_native_journal_index` currently copies every complete matched block into its
dictionary and requires a regex buffer for the entire source. Two regressions
reproduce rejection of a non-buffer range reader, including the real verified
ZIP original range introduced above.

Sources reviewed today:
[Python Mapping contracts](https://docs.python.org/3.10/library/collections.abc.html),
[Unicode line breaking](https://www.unicode.org/reports/tr14/), and
[RFC 3629 UTF-8](https://www.rfc-editor.org/rfc/rfc3629.txt).
The Python 3.10 documentation now labels that upstream version unsupported;
the project's existing compatibility floor is retained here, not presented as
upstream support. Broader dependency/support qualification remains open.

Use a read-only Mapping from physical body offset to source head and byte
span. Indexing a value reads that complete block on demand; the index retains
only paths and offsets. Head-set discovery reads metadata directly. Immutable
bytes are retained by reference; other buffer inputs are copied once so an
existing mutable-buffer caller cannot change an already-created index. Verified
non-buffer ranges retain their owner's read-time integrity/lifetime contract.

Discover candidates with byte searches. Apply the existing fixed-width operation
and timestamp grammar to only its fixed header, then check both link lines,
the blank separator, and the JSON-shaped physical line through ranges.
Preserve LF-only delimiters, strict head-path UTF-8 and regex non-overlap;
decode the head only after a complete structural match, so an invalid later body
does not introduce a new early UTF-8 failure. The operation/timestamp widths come
from the existing grammar, not new data caps. No path, body or event is truncated.

Alternatives: caching regex matches avoids body copies but still requires the
whole buffer; independently matching chunks loses split events; collecting
physical lines retains the giant-event allocation; a new streaming-regex
dependency is unnecessary for this fixed grammar. A single selected block still
materializes when a caller needs its complete physical proof, and complete head
paths remain metadata. NativeCompileFrame's encoded/text tuples and frame
serialization still need their own coordinated streaming migration.

Qualification: 274 related tests passed on Python 3.12 (33.70s) and Python
3.10 (36.39s). The final suite covers the frozen previous regex grammar,
mutable-buffer snapshots, shared-newline non-overlap, malformed/Unicode
headers, and verified ZIP ranges at both seven-byte and normal transfer sizes.
Lizard plus AST checks covered all 27 changed/new functions with no law-5
failures; Ruff and whitespace checks passed.

Three final local measurement pairs against c9a89060 separated untraced timing
from allocation tracing. For sixteen 512 KiB events, median retained additional
Python allocations fell from 8,396,663 to 4,962 bytes; construction time fell
from 37.94ms to 0.62ms. Requesting one block then allocates about 524,580 bytes.
For 10,000 short events, retained allocations fell from 4,952,903 to 2,674,390
bytes, but construction increased from 14.15ms to 32.67ms. The range parser
therefore trades some small-event CPU for bounded body retention and support
for verified non-buffer originals. These are additional Python allocations,
not RSS, end-to-end compilation performance, model quality or token costs.
Raw evidence: logs/step7-native-range-index-final-measurement-20261020.json
and logs/step7-native-range-index-final-py{312,310}-20261020.txt. The installed
vault and live compiler remain unchanged; point 7 remains incomplete.

### 2026-10-20 — shared verified source authority for evidence resolution

Fresh graph generation-18e03be16f2d3c6f-df70873d matches 8bbcd7b2.
The graph is partial (113073 unresolved): direct inspection additionally covers
archive-range construction, every original-range consumer, entry metadata,
canonical partition guards and live daily reads. Eight initial regressions
reproduced EvidenceResolver rejecting verified ranges with TypeError. Simply
loosening the bytes guard would lose the source proof rather than fix the seam.

Sources inspected today:
[Python incremental codecs](https://docs.python.org/3/library/codecs.html#incremental-encoding-and-decoding),
[RFC 3629](https://www.rfc-editor.org/rfc/rfc3629), and
[Restic repository design](https://restic.readthedocs.io/en/stable/100_references.html).
Incremental codecs preserve decoding state across chunks; finalization rejects
incomplete suffixes. Strict UTF-8 validation covers the whole source, including
bytes outside a citation. Content-addressed block verification binds returned
bytes to the initially verified source; it is not encryption or authentication
of the source's author. The existing Python 3.10 compatibility floor still needs
local validation and does not imply upstream support.

Move the existing implementation into bounded_io, already the shared I/O layer,
and remove its controller-local copy. Freeze identity/extent/block geometry;
keep one bounded mutable cursor. The resolver accepts exactly this verified type
or its existing bytes contract. It checks owner activity even for cached entry
metadata. Stream full-source UTF-8/newline indexing and complete-block hashes;
retain exact entry offsets and materialize only the requested citation. All
reference digest, block, span and UTF-8-boundary checks remain in place.

Alternatives: mmap would require a stable backing file and cannot directly map
compressed ZIP members; arbitrary slice/digest interfaces do not prove source
identity; importing the compiler controller into the resolver reverses the
low-level dependency direction; duplicating the range reader duplicates security
logic. No new file layout, runtime format, provider call, dependency, configurable
limit or truncation is introduced. Transfer width remains the existing I/O step,
not a maximum source size.

A mutation test initially expected rejection while the cited bytes were still
in the immutable captured block cache. That expectation was wrong: the cache
correctly returned the original bytes. The reload regression now evicts that
block before changing the underlying stream and confirms the changed reload is
rejected. This retains the established immutable snapshot semantics.

The live source capture, archive record restoration, partition-identity guards
and retained native frame payloads still need coordinated adoption. This stage
makes that migration possible without making arbitrary objects authoritative;
it does not claim the full compiler or installed vault has already migrated.

Qualification: 842 related tests passed on Python 3.12 (46.87s) and Python
3.10 (51.07s), with one Windows reparse-point test skipped on Linux. The final
24-test range-evidence suite also passed on both (0.44s/0.37s), including an
unverified subclass that cannot bypass exact-type source admission. Ruff and
whitespace checks passed. Lizard plus AST measured all 50 changed/new functions;
none exceeds the law-5 thresholds. Old private controller implementation and
its now-unused helpers were removed, with consumers updated to the one shared
implementation. No installed legacy or private runtime data was deleted.

Three local measurement pairs compare initial source proof plus one citation;
inputs exist before measurement, and range measurements include complete initial
stream/hash verification. For one 8 MiB event, median additional Python peak
allocations were 8,391,120 bytes before, 134,770 for candidate bytes, and 211,587
for candidate verified ranges. Times were 25.50ms, 25.96ms and 48.37ms respectively.
For 10,000 short events, peaks were 2,947,903 / 3,747,725 / 3,811,361 bytes and
times 25.75 / 26.75 / 152.72ms. Range scanning has real seek/hash/Python overhead,
and entry/newline metadata still scales with event/line count. The bytes path
also changes metadata construction order and has a higher short-event peak;
this is not reported as a universal memory or speed improvement. Whole-source
body allocations are removed from the range evidence path without dropping
proofs, content or references. These measurements are not RSS, ZIP end-to-end
compile latency, model quality or token cost; full-cycle qualification remains
required. Raw measurements: logs/step7-evidence-ranges-measurement-20261020.json.

### 2026-10-20 — retained daily originals enter the actual archive planner as ranges

The fresh graph generation-18e03c95b895cc99-559c642a matches 1e663f7f;
113176 unresolved edges require supplementary source inspection. Read the actual
preflight writer/consumer, batch/context/repair ownership, original-part identity
checks, archive serializer, tool-line boundaries, and evidence resolver. Three
new regressions first failed: normal batch planning reads the entire original,
there is no reader-level close for retained originals, and completed day objects
are bytes rather than releasable verified views.

Sources reviewed on this date: [Python weakref ownership/finalizers](https://docs.python.org/3/library/weakref.html#finalizer-objects),
[Python ZIP member lifetimes](https://docs.python.org/3/library/zipfile.html#zipfile.ZipFile.open),
[RFC 3629](https://www.rfc-editor.org/rfc/rfc3629), and
[Restic content-addressed design](https://restic.readthedocs.io/en/stable/100_references.html).
The Python pages are one independent authority, not two. Weak values avoid a
reader retaining every completed source. Finalizer callbacks must not retain
the referent. An open ZIP member can outlive the ZIP object, so stream.closed
alone does not enforce the archive owner's scope. UTF-8 bytes/offsets and
verified content hashes remain authoritative throughout the change.

Each archive reader owns a weak registry keyed by logical path plus original
digest. Parts keep their verified original alive across batch lookahead; a
reader close closes every live source, and queue-owned open_compile_inputs
closes that reader before releasing the artifact context. Released sources close
their streams; finalization is fallback cleanup, not a replacement for the
explicit production close. Source reads also check that their ZIP owner remains
open. No daemon, runtime path, persistent format, model call or package is added.

Alternatives: closing on day advancement invalidates lookahead/current batches;
retaining every strong original view retains all ZIP read buffers; reopening per
slice repeats full-source verification; trusting declared digests permits forged
source interfaces. Keep the one existing verified source implementation and
its canonical byte/frame/entry evidence checks. Whole-source equality uses exact
immutable ownership or computed hash plus extent, never a duck-typed digest.
The original-source range and selected part bytes are distinct fields even when
their content hashes coincide. A first integration run exposed this aliasing:
the former shared blob map put a range into the part's bytes field. Restore now
uses source proof only for original_content and reads the selected part bytes
separately. This corrects the representation boundary instead of weakening the
part buffer contract.

Canonical record metadata is now assembled from dataclass fields rather than
recursively copying the entire original view. Frame JSON remains canonical and
retained as before; its streaming migration is still open. ZIP writes can stream
a verified source with its known extent while preserving record/manifest identity.
Explicit eager compatibility methods still produce complete bytes; ordinary
archive records, preflight, context verification and batching use ranges.
Tests that compare representations normalize ranges back to bytes only inside
the test's live owner scope, comparing all original fields and contents. New
production-path tests forbid whole-original archive.read and separately compare
complete preflight source records and batch manifests with the eager baseline.

The next integration run identified the receipt selector's remaining buffer-only
prefix hash (memoryview). It now uses the shared evidence range hash and checks
source scope before reusing a prefix proof. Regression coverage includes full
and historical-prefix identity, changed declared hash and a cached proof after
reader close. Core tree execution and complete-work recovery then passed 30
related tests, including recovery without another model call. These are test
fixtures, not the required real subscribed-model full-cycle qualification.

Expanded qualification on Python 3.12 and 3.10 each passed 969 tests and skipped
one Windows-only reparse-point test. Each run had one remaining representation
comparison failure in a refusal observer: the test retained range-bearing records
past the owner's scope and compared them with eager bytes. The observer now
copies and compares every original byte while its scope is open, preserving its
assertions that complete durable preflight precedes refusal effects. The entire
14-test consumer file subsequently passed on both versions (32.58s / 34.66s).
Broad-run durations were 164.98s / 227.35s. No production code changed after those
broad runs began; this final correction was test observation only. Together these
runs qualify 970 distinct tests on each version. Linux qualification does not
claim Windows execution. Ruff and whitespace checks pass; all 74 written/changed
functions pass measured Lizard and AST complexity limits without exclusions.

The first large-source benchmark was deliberately interrupted with SIGINT after
several minutes of active CPU work to inspect its Python stack; it was not
restarted because of an observation timeout. No live compiler or provider call
was involved. The stack identified source-choice row checks calling line_bounds
and range.rfind, which rescanned and rehashed the entire preceding source for
each row. An independent byte-count regression reproduced the quadratic work
and failed before the fix: initial verification, newline indexing, and monotonic
row reads must fit three complete source passes, regardless of row count.

VerifiedByteRange now computes a lazy immutable LF-offset index once per owned
source. Forward/backward LF searches use binary search; other byte searches keep
the generic streaming algorithm. LF is the compiler's existing physical-line
delimiter, not a content-dependent special case. EvidenceResolver reuses this
same index, and full UTF-8 validation is separately tracked: a raw newline search
does not certify text, and later validation still rejects malformed bytes or an
incomplete UTF-8 suffix. All cache accesses check source scope. The index is
metadata proportional to line count, with no cap or content truncation; its
memory/time cost must be included in measurement. No source-body cache is added.
Earlier 969/14-test results above predate this measured performance correction;
final qualification below must cover it before the change is accepted.


Final range-reader qualification (2026-10-20): 1071 tests passed on each of
Python 3.12 and 3.10 (330.58s / 379.06s), with one Windows-only test skipped.
Subsequent adversarial receipt tests exposed a cached-prefix identity defect:
a different source claiming the previous digest could borrow its prefix proof.
Both bytes and range reproductions failed before the correction. The selector
now verifies the actual immutable source digest and binds its cached identity
to that exact source. Related receipt, tree and completion tests passed on both
versions (53 tests, 57.51s / 62.64s). A further weak-reference reproduction showed
that this proof retained the preceding original while the next source was read.
Source advancement now releases the prefix owner before reading the next source.
All 18 range-restoration and receipt-lifetime tests then passed on Python 3.12
and 3.10 (1.40s / 2.79s). The 1071-test runs preceded these final localized fixes;
they are not represented as full-suite reruns of the final state. All 94 changed
functions pass Lizard/AST limits, and Ruff passes without exclusions.

Completed paired measurements are in the private runtime log
logs/step7-range-restore-measurement-20261020.json. Three-run medians:
restoring an 8 MiB source in 513 parts changed from 3.461882s and 8,880,282
peak traced bytes to 0.094207s and 519,816 bytes. Actual planning of a 512 KiB
source in 33 parts changed from 1.709264s and 4,979,300 peak bytes to 2.638847s
and 3,524,079 bytes. Planning is slower despite lower peak allocations; this
remains a performance tradeoff requiring full-cycle assessment. The previous
controller uses the same current planner to isolate archive restoration. Every
emitted canonical record and part count agrees; fixture construction is outside
measurement, timing is separate from allocation tracing, and these are not RSS,
model-token, quality, installation or real full-cycle results. No provider calls
were made. Live capture and native-frame payload streaming remain unfinished.


Live-source transition investigation (2026-10-20, not yet implemented): direct
measurement of _read_daily_source returned full bytes and allocated 2,133,378
peak bytes for a 1 MiB file and 8,397,332 for a 4 MiB file. The fixture used real
regular-file reads; no provider calls. Evidence is retained privately in
logs/step7-live-read-baseline-20261020.json. The common reader joins its chunk
list, so both the list contents and final bytes coexist. A streaming archive
writer alone cannot fix this upstream allocation.

Consumers requiring a coordinated transition are _daily_capture_records,
_ContextReceiptSelector._source_parts, _readable_daily and its receipt-owner
scanner, _explicit_daily, _daily_already_compiled, and _whole_daily_digest.
The selector additionally retains all selected part bodies for its current day.
SourceSnapshot records currently reach _require_snapshot_digest, which still
requires the buffer protocol; accepting a range only in original_content is
therefore insufficient for live source capture. Eager snapshot_compile_inputs
must preserve its explicit byte-owning contract without returning expired views.
Native frames still retain all payloads, independently of the original buffer.

Sources reviewed before choosing the transition, on 2026-10-20:
- Python 3.12 os documentation: https://docs.python.org/3.12/library/os.html
  describes descriptor-based fstat and platform-specific open flags. Use the
  supported floor's interfaces; do not infer portable guarantees from 3.15 docs.
- Go's primary traversal analysis: https://go.dev/blog/osroot identifies the
  check-then-open race and explains descriptor-relative traversal and platform
  differences. Existing Python identity/no-follow checks must remain; a path
  precheck alone is not an immutable source guarantee.
- Restic backup/change detection: https://restic.readthedocs.io/en/stable/040_backup.html
  explains why Unix ctime matters when mtime can be restored and documents the
  different Windows metadata contract. Metadata alone is not a content proof.

Alternatives under evaluation: mmap retains a mutable external file view;
a second temporary complete copy increases disk requirements and needs ownership
and cleanup; the existing verified block range can reuse an owned descriptor
and computed hashes without retaining the source body. The last option fits the
approved streaming layout but requires admission, before/after identity checks,
per-block proof, final validation before publication, explicit close and error
propagation. No new runtime path or dependency is proposed. The current 16 MiB
constant and configurable 32 MiB total/2000-source limits remain inherited;
settings still justify them by whole-memory retention. Removing that retention
requires re-evaluating these justifications, not silently retaining or removing
the caps. No claim of law-9 completion is made here.

The refreshed built-in graph at c5677485 (generation-18e03fb1dfc94cb9-665c7901)
confirmed the six compiler consumers and identified the dynamic seventh caller,
maybe_compile._daily_receipted. Its source was inspected directly; it also reads
the complete daily before checking receipts. Graph coverage remains partial
(113363 unresolved edges), so it is not evidence of exhaustive dependencies.

Three new source-record range regressions failed first at the buffer-only
snapshot digest check: canonical complete archive identity/roundtrip, wrong
claimed digest and closed owner. That shared check now delegates to the existing
immutable_source_digest used by original-source verification. Exact bytes are
hashed; exact verified ranges require an active owner and carry a digest computed
from the initial complete source pass. It does not accept arbitrary objects with
a digest attribute. This removes a prerequisite blocker for live capture but
does not itself switch live daily reads or resolve all inherited limits.

Source-record qualification: 60 tests passed on Python 3.12 (33.87s) and
Python 3.10 (37.33s), including durable streaming capture, eager/stream
identity, context refresh and batch context. Four changed functions pass
Lizard/AST limits; Ruff passes. This small shared-check change remains part of
the uninstalled live-source transition, not completion of streaming capture.


Live snapshot capture implementation (2026-10-20, still uninstalled):
open_stable_range shares file admission with read_stable_bytes and owns an
unbuffered descriptor. VerifiedByteRange accepts an explicit absent expected
hash only to compute its immutable digest and per-block hashes from every byte
through EOF; provided expected hashes are still checked. This is a computed
proof, not trust in metadata or a caller-supplied digest property. The streamed
capture path now uses that owner; the explicit eager snapshot API retains bytes.

Initial mutation tests exposed a real metadata-resolution issue: an equal-size
rewrite sometimes leaves both observed mtime and ctime unchanged. Re-reading
an uncached block detected changed content, but a cached immutable block could
still return captured bytes. Successful owner exit now re-hashes the complete
unbuffered stream against the initial digest before allowing manifest publication.
A deterministic regression freezes fstat metadata and changes same-size content;
exit still refuses it. This intentionally costs another full sequential pass.
It retains the existing cooperating-writer model, not an adversarial filesystem
snapshot guarantee. No sleep, arbitrary retry, truncation or new cap was added.

A failing ZIP blob writer also reproduced an ownership leak: the retained
exception traceback kept the suspended capture iterator and its file alive.
_capture_manifest now explicitly closes its generator on every exit. The new
regression holds the exception traceback and source reference and requires the
descriptor to be closed. A separate mutation-at-source-exit regression requires
failure and absence of manifest.json even after source records were emitted.

Actual reader measurements, including initial proof, consumption of all chunks
and final complete hash, are retained in logs/step7-live-range-measurement-20261020.json.
Peak traced allocations were 235149 bytes for 1 MiB and 209063 for 4 MiB,
versus the prior whole-byte reader's 2133378 and 8397332 bytes. These are synthetic
reader allocations, not full compilation/RSS/model-token evidence. Traced times
were 0.01060s / 0.02245s; timing is instrumented and not a comparative latency
benchmark. Source metadata still scales with blocks. Receipt selection and its
current-day part list, native-frame payloads, explicit daily selection, mirrors
and maybe_compile retain whole-byte paths; actual compile with a receipt callback
can still allocate a whole daily separately. The inherited 16 MiB daily and
configurable corpus ceilings remain pending coordinated consumer migration.

Final live-capture qualification: 261 passed and one Windows-only test skipped
on Python 3.12 (31.58s) and Python 3.10 (34.38s). All 37 changed functions
pass Lizard/AST limits, Ruff and whitespace checks pass. These checks cover the
shared reader, evidence ranges, archive proofs, durable/streaming capture and
context refresh/batching; they do not qualify installation or the full product.


Receipt-source transition, investigation dated 2026-10-20: the actual current
_source_parts retained 8338828 traced bytes for a 3840000-byte synthetic daily
(250 parts, whose bodies total another 3840000 bytes). The log is
logs/step7-receipt-scan-baseline-20261020.json. This confirms both the full source
and every part are retained. The normal capture already calls selector.matches
with the captured part via _snapshot_compiled; it does NOT use _source_parts
for each part. Earlier language above about capture's callback potentially
loading a second whole daily was too broad. The whole-day paths remain in
fallback digest lookup, saved-source recovery, diagnostic cleanup and selection.

Using the Python descriptor/ownership, Go traversal and Restic change-detection
sources reviewed above, the next internal change uses an explicit source scope
and lazy part iteration for fallback lookup. Alternatives: a persistent open
owner cache requires close propagation across every selector consumer and can
hold stale live sources; metadata-only cache reuse cannot prove unchanged bytes
when timestamps collide; a whole-byte cache preserves the measured retention.
A fresh verified owner per fallback query is simpler and detects current content,
at the cost of repeated full verification when fallback queries repeat a day.
This tradeoff must be measured and reported; the normal captured-part path stays
unchanged. No new paths, environment contracts, runtime root or dependency are
proposed. Historical prefix and transaction-authority validation remain required,
and final source validation must run even when any/all short-circuits.


Fallback receipt lookup now owns an explicit source scope and iterates parts;
digest lookup, saved-source recovery and committed-failure resolution evaluate
inside that scope. Empty matches remain false, every matching occurrence must
be covered, and final source validation runs before returning even after early
any/all completion. The selector no longer keeps a whole-day body/list cache.
Tests that inspect parts after scope exit explicitly materialize every original
byte inside the test scope; authority checks and full descriptor comparisons
remain. The previous cache-identity assertion is replaced by fresh-read/current
content assertions because cache retention is intentionally removed, not because
a mismatched result is accepted. The four new regressions first produced three
failures (eager read and missing scoped ownership) and one pass (empty match).

The first allocation measurement contradicted a peak-memory improvement:
8870250 peak bytes and 2.29465s with tracing, despite only 276059 retained bytes.
A profile showed daily_entries/_heading_starts dominated time, with 34660 owner
checks and redundant fstat/lstat observations. The LF index retained one Python
integer per newline. It now uses fixed-width offsets in immutable bytes, exposed
as a read-only view. The regression requires all offsets unchanged, compact
storage and refusal of mutation; it failed on the prior tuple. File ownership
checks now reuse each stat observation within the check (one fstat, one lstat),
while preserving each check, initial proof, per-block proof and final full hash.
No time-based check cache or check suppression was added.

Additional representation research on 2026-10-20:
https://docs.python.org/3.12/library/array.html and
https://docs.python.org/3.12/library/stdtypes.html#memoryview.cast describe native
fixed-width arrays and read-only byte-backed views;
https://arrow.apache.org/docs/format/Columnar.html describes contiguous fixed-width
primitive layouts and their random access properties;
https://numpy.org/doc/stable/reference/arrays.scalars.html documents fixed-width
numeric scalars versus Python scalar types. Python's standard library is sufficient;
adding Arrow/NumPy for this metadata would add an unnecessary runtime dependency.
The 64-bit unsigned representation covers offsets addressable by supported
32/64-bit Python file readers; it adds no user-configurable input ceiling.
The native representation remains in-process only, not a new persisted format.

After compact offsets/stat reuse, the same complete 3.84 MB / 250-part scan
peaked at 3414484 traced bytes and retained 275517; traced duration was 1.39714s.
Separate uninstrumented three-pair fresh-scan measurements gave median eager
0.020827s versus scoped 0.384685s. All canonical part identities and counts match.
This is a significant latency regression despite better memory retention and
peak. It remains unresolved; do not claim the receipt transition qualified for
installation or overall efficiency. The next optimization target is repeated
per-heading range operations in the shared entry parser. Repeated old cache hits
were not measured and would make the latency tradeoff larger. Private evidence:
logs/step7-receipt-scope-measurement-20261020.json,
logs/step7-receipt-scope-optimized-measurement-20261020.json,
logs/step7-receipt-scope-paired-20261020.json.

Current receipt-scope correctness qualification: 909 passed, one Windows-only
skip on Python 3.12 (101.51s) and Python 3.10 (112.96s). All 31 changed
functions pass measured Lizard/AST constraints. Ruff required one test import
formatting correction, now applied. The candidate remains uncommitted pending
the explicitly unresolved latency issue; correctness is not efficiency evidence.


Entry-window optimization research, 2026-10-20, before implementation:
Python 3.12 re (https://docs.python.org/3.12/library/re.html), Rust regex
(https://docs.rs/regex/latest/regex/) and Perl's own regular-expression reference
(https://perldoc.perl.org/perlre) distinguish buffer/line boundaries, anchors and
engine semantics. Python's existing grammar and differential oracle remain
normative; the other engines are alternatives, not interchangeable validators.
GNU grep documentation fetches failed, so those were not used as evidence.
Chosen approach: run the existing delimiter parser on bounded verified byte
windows ending at a physical LF or real EOF. This avoids per-heading ownership
calls without disabling any source check. If a physical line cannot fit the
existing transfer window, fall back to the existing range parser for the entire
source; discard accumulated window results, preserving all entries and errors.
No line is rejected or truncated, no new engine/dependency is added, and the
transfer size remains an I/O geometry, not an admissible-line-length limit.
A bespoke streaming grammar or second regex implementation would duplicate
validation; persistent metadata check caching would weaken ownership checks.
The new cost regression reproduces per-heading owner calls independently of
wall-clock time, alongside differential boundary/long-line tests.


Window parsing passed 584 grammar/identifier tests before the next correction.
The 3.84 MB / 250-part measurement improved from 0.384685s to 0.150379s median.
Inspection then found that _iter_daily_parts sliced each part twice, once for
content and again for hashing. Crossing a verified block boundary caused the
second slice to rewind/re-verify preceding blocks. A dedicated regression first
observed two reads per part. _daily_part_record now reads once and hashes that
exact payload; canonical source/part identities remain unchanged.

The final three-pair fresh-scan median is eager 0.017887s versus scoped 0.052530s.
A separate allocation pair measured peak 8082802 versus 534299 bytes. Timing is
uninstrumented except the separate allocation pair; all 250 part identities
match across runs. The eager comparison uses the current helper with the same
single-read correction, so this does not overstate the old implementation's
cost. The scoped scan remains slower because it verifies a live source through
exit and never reuses an entire day across queries. Full-cycle cost/quality
qualification, particularly repeated recovery lookups and long-line fallback,
remains open. Evidence: logs/step7-entry-window-final-paired-20261020.json.

An additional ownership regression found that an empty closed range could skip
the new window loop and return an empty result. The regression failed for empty
input while nonempty input already refused. The parser now checks active scope
before iteration, preserving closed/expired source behavior for every extent.
This last guard was added after the broader test runs began; separate final
window/evidence tests qualify it rather than calling those runs a full rerun.

Final receipt/window qualification: 980 passed plus one Windows-only skip on
Python 3.12 (98.48s) and Python 3.10 (110.11s), followed by all 100 final
window/evidence tests on each version (0.75s / 0.64s) after the empty-owner
guard. All 45 changed functions pass Lizard/AST limits; Ruff and whitespace
checks pass. The built-in retirement command removed five obsolete code-index
generations after a dry-run review, preserving current/previous generations and
other checkouts, increasing free disk from 1.9 to 4.0 GiB. No run state or
private knowledge was removed. Installed vault and real-cycle qualification
remain unchanged and incomplete.


Service-reader/error-boundary investigation, 2026-10-20: a real temporary queue
retaining the current daily digest lost that failure when _read_daily_source
raised PermissionError. _readable_daily catches all OSError/ValueError and
returns None; _current_source_digests treats None as no current content, and
_retire_stale_source_failures deletes the evidence. Reproduction is retained at
logs/step7-unreadable-failure-red-20261020.json. No live state was changed.

Before implementation, reviewed primary error contracts:
https://docs.python.org/3.12/library/exceptions.html#FileNotFoundError,
https://pkg.go.dev/io/fs#ErrNotExist,
https://doc.rust-lang.org/std/io/enum.ErrorKind.html.
All distinguish absence from permission/I/O failure. Use Python's explicit
FileNotFoundError for optional source admission; propagate permission, timeout,
corruption and source-change failures. A context owner must catch only admission
failure, not exceptions from receipt authority or final source validation.
Alternatives: broad failure-to-empty conversion falsely retires evidence;
blanket refusal of genuinely missing files breaks the existing missing-source
retirement contract; a second error-status schema is unnecessary when existing
typed exceptions already carry this distinction. Existing verified source scopes
and ExitStack are sufficient, without a new path, dependency or runtime service.
The migration must also replace whole-part-list construction in daily_is_compiled
and preserve receipt authority, digest provenance and consumer deadlines.

The service migration now covers explicit and automatic daily selection, receipt
owner discovery, whole-day mirror digests, source-failure retirement, and both
current and legacy doctor receipt checks. Doctor keeps its caller's vault root
and deadline; the legacy digest and receipt checks share one owned source rather
than reading the file twice. Daily receipt evaluation consumes parts incrementally.
Only the explicitly eager snapshot compatibility path still calls
`_read_daily_source`.

Regression coverage uses a real temporary queue: permission, I/O, timeout and
validation errors preserve its source-failure row. Failure during source-scope
release also preserves the row, including when its recorded digest differs from
the current file. A consumer's missing receipt error is not confused with a
missing source at admission. Doctor checks are tested against a forbidden eager
reader, including the legacy path. No live queue or private knowledge is modified.
The inherited daily-size ceiling remains unchanged and still needs the larger
law-9 audit; native-frame payload retention and the full installed real-cycle
qualification remain unfinished.

Final service-reader qualification: 129 tests passed on Python 3.12 (145.33 s)
and Python 3.10 (162.06 s), including large-day committed receipts, historical
source outcomes, context authority, secondary receipt consumers, automatic
selection and compile hardening. Lizard plus AST inspection measured all 30
changed/new functions with no CCN, if-count or nesting violations. Ruff and
`git diff --check` passed. These results qualify this candidate change only;
they do not prove installation, full dependency qualification or the real model
cycle.

Native-frame retention investigation, 2026-10-20: an isolated 521,960-byte
daily containing 16 valid repeated captured events retained 1,151,328 bytes
after `_native_daily_frames` (peak 1,396,600). Separately encoding the native
array produced 1,029,784 JSON bytes with a 2,064,546-byte peak. These are
stage-local tracemalloc measurements, not whole-process or full-cycle evidence:
`logs/step7-native-retention-baseline-20261020.json`.

Before choosing the serialization change, reviewed independent primary APIs:
Python 3.12 `json` (https://docs.python.org/3.12/library/json.html),
Rust `Read` (https://doc.rust-lang.org/std/io/trait.Read.html), and Go
`encoding/json` (https://pkg.go.dev/encoding/json). Python exposes incremental
encoding; Rust distinguishes incremental reads from reading to the end; Go
offers token/stream decoding and writer encoding. A stream-shaped API alone
does not prove bounded internal memory. No new runtime dependency or language
is justified here. Alternatives: retaining the complete serialized array keeps
the observed allocation; a new per-frame archive format changes snapshot
identity and recovery compatibility; a temporary spool adds disk ownership
and cleanup obligations. Prefer replayable canonical array chunks within the
existing snapshot writer, keeping exact delimiters, item encoding, order and
blob hashes. Known-length ZIP records need a complete length/hash pass, so
this trades repeated serialization for bounded aggregate allocation.

A separate Unicode-heavy synthetic prototype compared whole-array encoding
with canonical item-by-item encoding of the same 16 frozen frame records.
Both produced 2,306,299 bytes and identical SHA-256. The eager peak was
10,258,594 bytes; the prototype peak was 644,043 bytes. This is only a
prototype, not integrated code:
`logs/step7-native-json-prototype-20261020.json`. The next implementation must
verify immutable/replayable input, exact byte identity, archive round trips,
error propagation and the existing verified-range path. It must also address
the separate retained `encoded`/`text` frame collection and eager metadata
restoration; streaming array output alone does not finish source streaming.

Canonical array serialization is now implemented inside the existing archive
writer. Each item retains the old canonical encoder. A first pass binds length
and SHA-256; the emission pass verifies both before completing the blob. Changed
replay data raises instead of silently publishing under the earlier digest.
No archive schema, record name, recovery format, dependency or runtime path
changes. The integrated two-pass synthetic measurement preserved the prototype's
exact bytes/digest: peak 646,468 bytes versus eager 10,258,594; 22.27 ms versus
12.81 ms. This is a measured memory/CPU tradeoff for one metadata array, not
proof of whole-cycle performance (`logs/step7-native-json-integrated-20261020.json`).

Validation: 120 archive/capture/range/protected-dispatch tests plus 78 context,
preflight and whole-native-container tests passed on each of Python 3.12 and
3.10. All 14 changed/new functions passed Lizard/AST complexity gates, Ruff and
diff checks. Remaining: retained native frame payloads, eager JSON metadata
restoration, original full point-7 qualification and installation.

Source-backed native frame design, 2026-10-20: source inspection found two
producers, `_native_daily_frames` for historical doctor work and
`_CapturedPartitions.iter_daily` for normal capture. Both eagerly retain the
encoded container and extracted text of every frame. Consumers also inspect
frame positions repeatedly for part grouping and overlap; simply regenerating
every full frame for every position test would introduce avoidable repeated
decoding.

Reviewed independent primary ownership/view references:
https://docs.python.org/3/library/stdtypes.html#memoryview,
https://arrow.apache.org/docs/format/Columnar.html, and
https://doc.rust-lang.org/book/ch04-02-references-and-borrowing.html.
They inform the distinction between payload copies, offset-based views and
source lifetime; this Python implementation must enforce lifetime explicitly,
not claim Rust's compile-time guarantees. No Arrow dependency is needed.
Keep the existing initial verification of every native frame and its linked
permanent source, then retain only immutable coordinates/timestamps with the
owned original source. Reify an ordinary `NativeCompileFrame` only when its
payload is requested; positional grouping should read coordinates directly.
Preserve explicit eager APIs, tuple-compatible comparisons/indexing, archive
wire identity, full-container citation and renewed permanent-source checks.
Alternatives: retaining strings preserves the measured duplication; fully lazy
initial validation risks hiding malformed unselected captures; a separate
payload store adds an unnecessary ownership/cleanup contract.

Implemented the shared source-backed sequence in both producers. Initial
verification still consumes every candidate and permanent-source proof, then
releases decoded frames. Coordinates are immutable; indexing reifies the same
ordinary frame type from the owned source. Sequence access requires an active
source. Grouping and overlap use coordinate spans without decoding unrelated
payloads; renewed permanent-source validation in semantic consumers remains.
Archive field names and canonical frame JSON remain unchanged.

On the same 521,960-byte, 16-event isolated fixture, retained memory fell from
1,151,328 to 121,384 bytes and peak from 1,396,600 to 496,474 bytes; initial
verification took 178.36 ms versus the earlier 177.73 ms observation. These
single-stage measurements are not whole-cycle timing evidence:
`logs/step7-native-views-measurement-20261020.json`.
Both Python 3.12 and 3.10 passed 145 related regressions (60.03/66.68 s),
then all six final lifetime/position/initial-proof/archive round-trip tests.
Lizard/AST checks passed for 19 changed/new functions, as did Ruff and diff
checks. Full archive JSON metadata restoration still eagerly materializes
arrays; historical doctor's complete daily snapshot also remains eager.
Neither full streaming nor installed point-7 completion is claimed.

Archive metadata restoration design, 2026-10-20: `json_blob` currently calls
`archive.read`, decodes the entire array, then `_restore_snapshot_daily` creates
all native frames. Source inspection confirms its internal consumers are the
two array-valued metadata fields. Reviewed primary JSON grammar
(https://www.rfc-editor.org/rfc/rfc8259), Python's decoder and canonical encoding
APIs (https://docs.python.org/3.12/library/json.html), and simdjson's documented
on-demand traversal/lifetime approach
(https://simdjson.org/api/2.0.0/md_doc_basics.html). The last is an older
conceptual reference, not a current-version dependency recommendation.

Use the existing verified archive range to retain only top-level element spans.
A structural-token scan tracks nesting, quoted strings and escaping across
transfer boundaries; stdlib JSON decoding and exact canonical re-encoding still
validate every complete element before the array is admitted. Do not replace
semantic JSON validation with the scanner. Native record construction must also
validate every element before returning a lazy restored sequence. Explicit eager
snapshot APIs must detach those sequences while their source remains open.
Alternatives: `json.load` still materializes the whole value; repeated
`raw_decode` retries can repeatedly parse a growing long element; the existing
vector reader only supports scalar columns; a new C/C++ parser dependency adds
packaging and compatibility work without removing the archive ownership need.
The chosen internal representation changes no archive schema or runtime path,
adds no record-size/count limit, and must retain full canonical/hash validation,
closed-owner refusal and caller deadlines.

Implemented the owned array view and lazy restored native sequence. Every array
element is decoded and canonically compared at admission, every native record
is constructed once for shape validation, and only spans survive. Positional
native consumers reuse those verified spans without decoding unrelated
payloads. Explicit `planning_inputs`/`materialize` detach native records while
the archive is open. Slice/sequence access retains source ownership checks.

Regression development exposed three comparisons using a test helper which
materialized the original source but left the new metadata view attached to a
closed archive. That helper now copies both fields; complete-value assertions
and closed-owner refusal remain. The sharing assertion now requires zero
whole-blob reads and exactly one stream open. The old lifetime observer injected
a bytes subclass forbidden by strict immutable-source validation. It now counts
references to actual bytes, outside pytest's assertion rewrite (which itself
temporarily retains arguments); the sole-probe assertion still proves released
part payloads. No production type check was weakened.

The isolated 1,024,503-byte metadata measurement observed peak allocation
5,937,795 bytes for eager decoding/canonical checking and 437,608 for the owned
array, taking 8.76/15.26 ms respectively. These are stage-local measurements,
not real-cycle token or latency evidence:
`logs/step7-json-restore-measurement-20261020.json`. A reviewed native index
retention plan retired four obsolete development generations, preserving current
and previous and all other checkout generations, restoring free disk from
2.3 to 4.0 GiB. No knowledge or run state was removed.

Final qualification after the helper fixes: 216 tests passed on Python 3.12
(88.89 s) and Python 3.10 (101.58 s), including archive/capture integrity,
native payload lifetime, preflight replay, context refresh/deltas and original
digest ownership. Every one of 40 changed/new functions passed Lizard/AST
complexity gates; Ruff and diff checks passed. This does not complete historical
doctor's eager source-work layout, inherited law-9 limits, dependency/recovery
qualification, installation or the required real medium-model full cycle.

Historical doctor source-work design, 2026-10-20: the remaining eager path is
`_CurrentHistoricalSourceWork._capture`, which retains whole daily bytes plus
all part bodies and rereads whole files in `_recheck_sources`. Its sole
production entry is `verified`; three tests directly use the private capture
helper. Retain the exact-current-work and unique-containment semantics while
scoping all original ranges to one proof and caching only replayable layout
metadata. Selected witness units may retain their own atomic payloads; unrelated
day parts must be released as iteration advances.

Reviewed primary deterministic resource-lifetime contracts:
https://docs.python.org/3.12/library/contextlib.html#contextlib.ExitStack,
https://doc.rust-lang.org/std/ops/trait.Drop.html, and
https://isocpp.github.io/CppCoreGuidelines/CppCoreGuidelines#Rr-raii.
Use explicit Python scopes, not garbage-collection timing. The existing
`open_stable_range` rehashes its original descriptor on normal scope exit,
before a successful verification result can escape, and closes on exceptions.
This can replace the eager second full-file buffer while preserving final
content verification. Pure native parsing remains reusable within the proof;
permanent evidence and receipt/output authority remain freshly checked.
Alternatives: keeping complete tuples preserves the memory problem; reopening
for each requested unit repeats proof work; weak references or finalizers alone
cannot make validation failure part of the returned verdict. No runtime path,
schema, worker, dependency or new numeric ceiling is introduced.

The pre-change regression demonstrated a real final-witness gap: changing a
daily after the last witness check still returned `True`. The owned proof scope
now rejects that mutation during final descriptor validation and closes every
source even when validation raises. Capture outside an active proof scope is
refused. Selected units, as well as full layouts, retain coordinates only;
an initial size/hash index avoids rescanning all part payloads per historical
source request. Candidate units still undergo the complete descriptor,
unique-containment, receipt and permanent-evidence checks. Source mutation is
now refused on the first affected access; the existing mixed-version regression
requires this earlier refusal instead of waiting for a second capture.

The isolated 1,025,598-byte/64-part experiment, with dependencies imported
before tracing both cases, observed retained allocations 2,229,349 bytes for the
eager layout and 139,865 for the owned layout plus unit index. Peaks were
2,231,453/1,225,126 bytes, preparation 11.08/42.88 ms. A separate cold observation
includes module-import costs and is retained rather than silently discarded.
Neither measurement includes a complete doctor/model cycle:
`logs/step7-historical-ranges-warm-measurement-20261020.json` and
`logs/step7-historical-ranges-measurement-20261020.json`.

Final qualification: 127 historical/source/quarantine/receipt-authority tests
passed on Python 3.12 (76.04 s) and Python 3.10 (85.44 s). All 29 changed/new
functions passed Lizard/AST gates, Ruff and diff checks. The installed compiler
was separately observed alive with a Codex medium child; its log contains both
published batches and validation/input-budget failures. It was not interrupted.
This remains an uninstalled candidate; full dependency/upgrade, law-9, recovery,
installation and real-cycle evidence are still required for point 7.

### Point 7 integration reconciliation — 2026-10-20

Candidate `640c2619` and installed `e95f6a4a` are different releases. The
candidate's ordinary CLI and MCP requests reach the leased controller through
`_compile_under_lock`; queue workers use `run_leased_compile`, and nightly calls
the same CLI. Dry runs intentionally do not enqueue work. This routing is
implemented, not an outstanding design task. Its installation is outstanding.

Controller/dependency/answer-recovery/retention/origin/admission tests passed:
117 on Python 3.12 in 59.23 seconds. Inventory/foreground/cutover/migration tests
passed: 71 on Python 3.12 in 68.90 seconds. The combined 188 tests passed on
Python 3.10 in 290.87 seconds. These are bounded test scenarios, not evidence of
a completed useful full-vault model cycle. Package compatibility checks passed
for the 65-package Python 3.12 and 67-package Python 3.10 test environments.

Using the installed interpreter against candidate source, real execution
identity collection completed in 37.57 seconds: 250 product files, 30,547
package files and 632 Python files, plus configuration identity. This establishes
that the actual inventory can be collected in this environment; it does not
measure full-cycle overhead, guarantee every provider update, or claim closure
over operating-system libraries. Evidence:
`logs/step7-real-dependency-inventory-20261020.json`.

The refreshed built-in graph matches candidate `640c2619`. Its callers view for
`upgrade_offline`, supplemented by direct installer/repair source searches,
shows only test callers. The graph is partial, so absence from it alone is not
the finding. The existing shell/PowerShell installers and repair command do not
invoke compile-progress migration. Integration into the supported upgrade path
is therefore a concrete remaining requirement.

A real Restic experiment qualified the existing migration/backup/restore APIs
on an isolated adopted vault. Restic 0.19.1 was downloaded from its official
release and its compressed Linux amd64 artifact checked against the release's
SHA256SUMS (`f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c`).
Source: https://github.com/restic/restic/releases/tag/v0.19.1.
A generated local password file had mode 0600; no credentials or private vault
content were included in the public evidence. The experiment enqueued work,
performed offline migration with a retained preimage, created an encrypted
snapshot of the migrated fixture, and restored the receipt-bound snapshot to
an empty separate directory through the production API. Manifest validation
accepted 14 entries and two databases. Markdown bytes and the retained queue
payload matched exactly after restoration. The sequence took 6.24 seconds.
Evidence: `logs/step7-real-restic-cutover-20261020.json`. This was a small fixture
with a real external binary, not a backup of the installed private vault, a
restore publication into a running installation, or crash qualification for
Restic itself. The isolated evidence and password remain outside the vault.

Remaining gates: supported installer cutover; actual private-vault recovery and
safe installation under exclusive ownership; inherited source/body limits and
their law-9 basis; useful medium-model full-cycle quality/tokens/time/recovery;
then qualified removal of replaced compatibility code and documentation/index
sync. The installed compiler was observed alive for over 17 hours and its log
still contains validation/input-budget errors. Neither that process nor its
operational state was removed. Its lifetime is not proof of a dead owner and
cannot authorize offline migration. Historical lost-capture and maintenance
failures are not declared repaired by any of these checks.

### Explicit repair-command migration entry — design 2026-10-20

Reproduction: the existing repair CLI rejects `--upgrade-compile-progress
--backup-image ...` as unknown arguments before touching any root. The approved
migration is reachable only by importing its Python API. Add a mutually
exclusive repair operation to the existing command, carrying the already approved
external backup-image argument. Require `--apply`, the complete offline
confirmation, and an image argument together; reject an image supplied to any
other operation. Delegate exclusively to `upgrade_offline`, preserving its fence,
preimage binding, resume semantics and refusal of live owners. Do not silently
adopt legacy/fresh databases or choose a backup path. Success reports a validated
upgraded admission, not encrypted recovery or permission to delete its preimage.
Errors pass through the existing redacted CLI boundary.

Primary sources checked on 2026-10-20:
- SQLite rollback-journal atomic commits and hot-journal recovery:
  https://www.sqlite.org/atomiccommit.html . Keep transactional migration in the
  existing database; do not replace a database behind an open connection.
- PostgreSQL restore handling and failure semantics:
  https://www.postgresql.org/docs/current/backup-dump.html . A restore command
  can fail independently of having a backup; checking the restoration outcome is
  essential. This is comparative guidance, not a new PostgreSQL dependency.
- Restic explicit snapshot restoration into a selected target:
  https://restic.readthedocs.io/en/stable/050_restore.html . Keep backup identity
  and restore validation separate from schema-upgrade success.

Alternatives: automatic migration on queue admission cannot establish stopped
old clients; copying/restoring a preimage over the active queue can lose newer
work; another standalone command would duplicate the existing repair interface
and error rendering. The thin explicit repair operation is the smallest supported
entry to the approved migration. It adds no schema, environment variable, runtime
location, package, numerical bound or new architecture. Shell/PowerShell installer
orchestration and production recovery qualification remain separate next steps.

Repair CLI implementation qualification: 41 CLI/adoption/offline-cutover tests
passed on Python 3.12 in 25.40 seconds and Python 3.10 in 26.88 seconds. Actual
CLI subprocesses proved successful migration, exact backup certification,
idempotent repeat preserving subsequently enqueued work, refusal of a live
registered owner despite explicit confirmation, refusal of a different recovery
image, and successful resume of an interrupted intent with its bound image.
Seven invalid flag combinations were rejected before creating roots. All nine
changed/new functions passed Lizard/AST complexity gates; Ruff and diff checks
passed. The pre-change repair CLI's eight baseline tests passed separately.
The code index used for this change was generation
`generation-18e0460729fb1300-ac48765c` at `837a3d7a`; `_repair_report` callers were
confirmed in the partial graph and its CLI/report/migration dependencies inspected
in source. No live-vault migration or model call was performed. Installer wiring
and the other deployment/full-cycle gates remain open.

Installer integration design, 2026-10-20: read-only inspection currently reports
both adopted queue versions only as `adopted`, so installers cannot distinguish
an old queue from an upgraded one. Expose `details.compile_progress` only after
complete admission validation, deriving `upgrade-required`/`upgraded` from the
validated adoption envelope. Do not infer readiness merely from an event table
or a raw PRAGMA. Preserve the original adoption status and report contract.

Add an explicit backup-image argument to both installers, carried unchanged
through remote-bootstrap reexecution. Already-upgraded installations need no
image. All other cases either invoke the existing repair operation with an
explicit image and offline confirmation, or report that compilation still needs
migration. A supplied image without confirmation is rejected before installation
side effects. Neither installer chooses a staging path, silently grants offline
confirmation, copies SQL, or adds automatic startup/nightly migration. A failed
attempt remains a warning with its retained diagnostic report, consistent with
existing resumable adoption. Fresh v3 adoption remains independently supported;
the subsequent compile migration has the same explicit gate as an existing vault.

The previously reviewed SQLite atomicity, PostgreSQL restore-failure and Restic
explicit-target contracts above also govern this thin orchestration. This extends
the approved migration's supported invocation, not its storage architecture.
Alternatives rejected: duplicate migration code in shell/PowerShell; assuming
`adopted` means the compile journal exists; automatic selection/deletion of a
backup location. The tradeoff is an explicit offline step for a new queue as
well as an existing one, until initial adoption itself can establish the complete
new contract without weakening old-client exclusion. No arbitrary limit is added.

Installer integration qualification: 81 tests passed on Python 3.12 (18.94 s),
then 39 additional bootstrap/warning/invalid-readiness tests (8.34 s); Python
3.10 passed 52 integration/adoption/CLI tests (16.11 s) and the additional
invalid-readiness regression (0.83 s). Bash and native PowerShell each executed
their extracted installer functions against a real adopted fixture and the
production repair CLI, reaching validated upgraded admission. Scenario tests
covered already-ready/no-image, missing image, missing confirmation, valid
migration, unknown inspection followed by explicit recovery, and migration
failure. Paths containing spaces arrived as a single unchanged argument. Invalid
queue version never produced the readiness field. The complete installer rejects
an image supplied without offline confirmation before filesystem side effects.
Windows remote-bootstrap argument preservation has a source assertion alongside
the general bootstrap suite; the Windows operating system itself was not tested.

Actual AST measurements of the three new Bash functions: CCN 3/2/4, nesting
0/1/1, if counts 0/1/2. The three PowerShell functions: CCN 2/2/4, nesting 0/1/1,
if counts 0/1/2, no elseif chains or complex ternaries. All ten changed/new Python
functions passed Lizard/AST gates. Ruff, Bash syntax and diff checks passed.
PowerShell 7.6.7 was a temporary validation tool downloaded from the official
PowerShell release API; the linux-x64 archive matched release SHA256
`6896f9e3a6b0a354bf470835a96da76d2f801cd50dcf047ad7c4563bf93bf431`.
It is not a new product dependency. The built-in callers view for
`_inspect_adopted` used generation `generation-18e0466408cd3a5c-fc6dfb9f` at
`edd4e309`, supplemented by direct shell/PowerShell and admission source reads.
Evidence logs use `step7-installer-migration-*-20261020` under private `logs/`.

This wires the supported explicit operation into both installers; it does not
constitute an actual full installation, full-vault encrypted recovery, removal
of migration compatibility, or the required useful medium-model full cycle.

### Law-9 aggregate compile quotas — 2026-10-20

Reproduction on a temporary empty settings root: `_SourceBudget` refuses source
2001 even when each source is one byte; it separately refuses the 33rd 1-MiB
source while previous source bodies are released. The defaults are 2000 sources
and 32 MiB, with the explicit registry rationale that the whole input is held in
memory and the values predate measurement. The streamed production ingress no
longer justifies these aggregate cutoffs on that basis. Evidence:
`logs/step7-source-budget-reproduction-20261020.json`.

Primary contracts reviewed on 2026-10-20:
- Python iterators yield one element at a time, but converting an iterator to a
  collection materializes its input:
  https://docs.python.org/3.12/howto/functional.html#iterators .
- Node.js's stream/backpressure documentation distinguishes bounded buffering
  from accepting a total input size; ignoring consumption can exhaust memory:
  https://nodejs.org/learn/modules/backpressuring-in-streams . No Node dependency
  is introduced; this is an independent resource-management comparison.
- SQLite distinguishes implementation limits and explicitly adjustable limits:
  https://www.sqlite.org/limits.html . Removing an unrelated source-count cutoff
  does not remove SQLite's real constraints or the queue's integrity checks.

Under the owner's instruction to remove unjustified limitations, use the existing
numeric compile settings as optional aggregate admission quotas: default 0 means
no aggregate quota, a positive value retains the explicit operator-selected
count/byte limit. Preserve existing names and environment/file precedence; do not
silently ignore a positive configured quota. State the disk/work-volume purpose
and review conditions, instead of claiming a nonexistent measured memory bound.
Doctor must omit disabled quotas from near-ceiling division while still reporting
explicit overrides. Daily reading must distinguish no aggregate quota from a
zero-byte source allowance. Bind both budget settings to the compilation root.
No new environment name, runtime location, package or arbitrary ceiling is added.

Alternatives: raising defaults merely moves the same unsupported cutoff; treating
source volume as simultaneous heap use is incorrect for streaming; removing
operator quotas would discard explicit local policy. The remaining per-source
4-MiB, daily 16-MiB, archive-member, output and model-input limits require their
own consumer/contract analysis. In particular BagIt resolution still returns a
whole payload and derives its receipt-member bound from the daily bound; this
step must not advertise unrestricted daily/archive processing. Eager compatibility
callers likewise remain outside the streamed-memory claim.

Aggregate-quota qualification: 182 source/settings/health/hardening/transaction
tests passed on Python 3.12 (26.43 s) and Python 3.10 (27.37 s). Regression tests
consume the actual `iter_compile_records` path with 2001 separate source files
and with 33 one-MiB files; both now finish with unchanged source digests. Positive
operator count/byte quotas still refuse the next source, settings remain bound
to the compilation root, negative values fail, environment zero overrides a
positive file quota, daily reads with zero aggregate quota still read their
contents, and doctor skips disabled divisions without hiding configured values.
The registry-default assertion was updated to the intended zero semantics;
explicit-limit enforcement tests remain active.

A warm isolated capture of 33 x 1,048,576-byte files (34,603,008 bytes) used
3,179,448 peak traced
Python allocation bytes, 1,055,064 retained bytes (including the last record)
and 0.0589 seconds. This measures only source capture, not total process RSS,
archive restoration, downstream index or a complete model cycle. Evidence:
`logs/step7-source-quotas-memory-20261020.json`.

All eleven changed/new functions passed Lizard/AST gates; Ruff and diff checks
passed. The partial graph at `59dff0d6` returned no resolved class callers for
`_SourceBudget`; direct source inspection found its `iter_compile_records` caller
and the settings/daily/doctor consumers. The installed private settings currently
choose positive quotas (4384 sources and 176307105 bytes); they were not changed
and their necessity is not established by this candidate default change. The
per-file/archive and downstream index gates remain open. No live installation,
private-source rewrite or paid model call was performed.

### Index publication consumes one source body at a time — design 2026-10-20

`build_index_bytes` still materializes every note body in `_virtual_pages`, then
copies references through publication filtering and sorts `(path, body)` items.
A warm 16-page input of 16,778,128 bytes produced a 1,189-byte index but used
17,838,718 peak traced Python allocation bytes (0.1055 s). This is a remaining
whole-corpus payload materialization in the compiler's publication path.

The current Python iterator, Node.js backpressure and SQLite limit contracts
reviewed above support a one-pass overlay pipeline. Read each base page once,
count it against the original quota, select its pending after-image or deletion,
then count the resulting virtual page against the final quota. Process additions
after the original stream. Check all pending paths and individual sizes before
rendering. Keep the original and resulting quota checks, private-page filtering,
retired-page exclusions, UTF-8 behavior and deterministic metadata ordering.
Collect only extracted index entries for ordering/rendering; never collect source
bodies to sort them. Existing supplied base mappings stay owned by their caller.

Alternatives: a lazy mutable mapping would repeat file reads and require extra
identity checks between passes; caching its reads recreates the payload problem;
filtering private pages before input admission would change existing validation
and quota semantics. A one-pass source/overlay/filter/metadata pipeline preserves
read-once bodies without adding an ownership lifetime or runtime schema. The
settings registry's index aggregate defaults can then use the same optional-zero
quota semantics as compile capture, with positive operator policies preserved.
Per-page/output bounds and claim-tree snapshot consumers remain separate gates;
standalone rebuild's `_rebuild_plan` still requests a content-bearing claim-tree
snapshot and is not made streaming by changing only this renderer.

Index-stream qualification: 181 index/publication/settings/transaction tests
passed on Python 3.12 in 47.83 seconds and Python 3.10 in 51.60 seconds. The
initial run had 177 passes and three test expectation failures: the new link
counter also counted the footer's prose path (corrected to count actual wiki-link
bullets), and the old settings test expected zero to be invalid (its invalid
boundary is now -1; explicit zero acceptance/doctor coverage was added). No
positive-quota, private-page, mutation or transaction protection was removed.
The new memory regression distinguishes retaining all sixteen page bodies from
holding a few transient pages; the old implementation's measured peak exceeded
its half-corpus threshold. Caller base/pending mappings remain unchanged, source
reads occur once, and deletions cannot bypass the original-input quota.

The same warm input of 16,778,128 bytes now produced the same 1,189-byte output
with 3,161,920 peak traced allocation bytes in 0.1060 seconds, versus 17,838,718
bytes / 0.1055 seconds before. These are renderer-only observations, not total
RSS or standalone snapshot/transaction/model-cycle measurements. Executing the
previous committed renderer alongside the new one with explicit permissive
quotas also produced byte-identical output for ordinary and pending-add/delete
cases with a private non-UTF-8 source correctly excluded. Evidence:
`logs/step7-index-stream-{before,after,parity}-20261020.json`.

All twenty changed/new functions passed Lizard/AST gates; Ruff and diff checks
passed. The first complexity check found `_virtual_pages` at CCN 6; extracting
the pending-addition filter reduced it below the required ceiling. The fresh
partial graph for `build_index_bytes` was at `722e9fd6`, generation
`generation-18e0474f09db2365-a89b4232`; source inspection supplemented its callers
and confirmed no other use of the replaced private eager helpers. Those helpers
were removed from the candidate. Standalone claim-tree materialization,
per-source/daily/archive/output limits, actual-vault recovery/installation and
full useful-cycle qualification remain open.

### Manifest-only claim capture and standalone index planning — design 2026-10-20

`claim_tree_manifest.snapshot_claim_tree` currently obtains every content body
only to discard the content dictionary. Standalone `_rebuild_plan` deliberately
retains that dictionary to bind rendered content to its claim manifest. Warm
measurements with 16,778,128 input bytes showed peak traced allocations of
17,840,583 bytes for manifest-only capture and 17,840,559 for rebuild planning;
these are retained-body costs even after the renderer itself became streaming.
Evidence: `logs/step7-manifest-stream-before-20261020.json`.

Primary contracts reviewed 2026-10-20:
- Git content-addressed objects distinguish content identity from a filename:
  https://git-scm.com/book/en/v2/Git-Internals-Git-Objects . Keep SHA-256 evidence
  as the existing contract; do not adopt Git's object format or perform writes.
- Python Mapping supplies `items()` over key/value access:
  https://docs.python.org/3.12/library/collections.abc.html#collections.abc.Mapping .
  A read-only mapping can load one body on demand without a payload dictionary.
- SQLite isolation covers database transactions, not unrelated Markdown files:
  https://www.sqlite.org/isolation.html . Keep Markdown content checks and the
  existing claim-tree transaction precondition; do not infer file immutability
  from SQLite alone.

Add an internal retention choice to the shared hash collector. Manifest-only
claim capture stores digests but releases bodies; callers explicitly requiring
content retain the existing exact content-bearing result. Standalone index
planning consumes a read-only mapping of note paths to the manifest's digests;
each body is read once for rendering and must match its recorded digest before
use. Existing commit-time claim membership/content preconditions remain intact.
The bytes cannot merely be reread without comparison: a change between manifest
capture and rendering, even if later reverted, must not publish an index from
bytes not bound by its manifest.

Alternatives: unchecked rereads weaken provenance; retained complete bodies
preserve the memory problem; new snapshot files/locks add an unnecessary storage
or ownership mechanism. The selected mapping retains only digest metadata and
uses existing stable reads and CAS. It adds a second read/hash for index pages,
a measured I/O/latency tradeoff for removing corpus-sized heap retention. Existing
claim quotas, manifest formats, guardrail content consumers and per-file bounds
are preserved; their independent law-9 review is not declared complete here.

The shared collector fix also covers manifest-only guardrail readers, which had
exactly the same retain-then-discard path. Content-bearing claim and guardrail
APIs preserve their outputs and existing admission checks. Qualification: 265
tests passed on Python 3.12 (86.74 s) and Python 3.10 (89.77 s), including
manifest/explicit-body parity, input-mutation refusal, unlisted-path refusal
before any read, full automatic-writer integration, index tree-race retries,
committed index-drift repair, guardrail readers and compile transactions.

For the same warm 16,778,128-byte input, manifest-only capture now peaked at
3,158,859 traced bytes (0.0387 s), versus 17,840,583 (0.0426 s). Standalone
rebuild planning peaked at 3,160,117 bytes (0.1508 s), versus 17,840,559
(0.1231 s). Guardrail manifest-only capture peaked at 3,160,501 bytes (0.0362 s).
The extra index reread/hash trades about 27.7 ms in this experiment for avoiding
whole-tree body retention. These are isolated planning measurements, not full
publication/model-cycle or total RSS measurements. Evidence:
`logs/step7-manifest-stream-{before,after}-20261020.json`.

All twenty changed/new functions passed Lizard/AST gates; Ruff and diff checks
passed. Fresh partial graph caller views at `ef09484b` covered
`snapshot_claim_tree`, `snapshot_guardrail_sources` and `_rebuild_plan`, with
source inspection of the shared hash collector and content-bearing consumers.
The original transaction manifest and index-before-state preconditions are
unchanged. No private-vault publication or installation was attempted. Remaining
work includes source/archive/output and claim/guardrail limits, operator handling
of unrecoverable model outcomes beyond the internal authorization API, actual
private-vault recovery/installation, qualified compatibility removal, and complete
useful-cycle quality/token/time evidence on the agreed medium model.
