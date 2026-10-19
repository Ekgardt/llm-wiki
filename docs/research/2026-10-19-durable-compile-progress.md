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

Before activation, complete all of the following:

1. Versioned offline migration of the queue and adopted-pair manifest, including
   backup, interruption recovery, old-client refusal and bounded rollback rules.
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
