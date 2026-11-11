# Compile progress storage

`scripts/compile_progress.py` owns append-only checkpoint storage in the caller's
existing queue database. Public functions and class identities stay at that path.
There is no new database, connection, transaction owner or model transport. The
caller supplies the verified lease/payload/semantic transition and holds its write
transaction; append never begins or commits it. Migration alone installs exact
SCHEMA_STATEMENTS. Reads and appends never initialize missing schema.

CompileEvent fields are retained as lossless canonical JSON and bound by an event
digest. Work/source identity and consecutive sequence are immutable. SQL primary
key, immutability triggers and task foreign keys protect cooperative operations.
Hashes detect changed evidence, not malicious database rewriting. Source scopes
use logical path plus half-open byte spans; missing historical scope cannot prove
independence. A retained dispatch with no verified answer reserves overlapping
sources until resolved. The supplied answer verifier must validate its real
artifact and semantic binding, not just a hash or an arbitrary success callback.

Audits stream retained history under the caller deadline and validate exact schema,
sequence and artifacts. Retention checks protect superseded artifacts and tasks;
callers retain the write fence through subsequent deletion. Journal absence is
historically valid for schema 3; current schema admission is owned by the queue.
An audit is not a runtime deletion permit or certification of publication.

Root/work identity binds origin, sources, dependencies and request. Completion
reads the retained snapshot/preflight and compiler attestation; it does not create
receipts, recapture sources or execute a model. Whole-work semantics and receipt
revalidation belong to the compiler; queue settlement checks this retained proof.
Operator summaries expose a fixed set of identity fields, not arbitrary event details.

Dependencies: the full sqlite_deadlines and formats.source owners. Existing journal
tests stay unchanged and exercise rollback/reopen, process death before/after commit,
immutability/FK, corruption and refusal. Related real queue/controller/migration
consumers remain separately accounted integration obligations and are regression
checked; a small storage packet is not completion of the compiler's larger closure.

The packet retains exact selected history records and the complete provenance
catalog. The original historical file is unchanged; selected records do not claim
its unselected remainder. Run the storage packet, original journal tests and related
queue/controller/health/migration checks on Python 3.14 and the primary interpreter.
Native Windows/macOS skips and unavailable useful-model-cycle evidence stay explicit.
Source: ../research/2026-11-03-compile-progress-storage-packet.md.

Shared canonical contract: [SQLite deadlines](sqlite-deadlines.md).
Shared canonical contract: [Source formats](formats.md).
Shared canonical contract: [Exact record contexts](context-records.md).

## Structural reference retained from the overview

### Existing compile-progress storage qualification

The existing append-only SQLite checkpoint owner remains at
`scripts/compile_progress.py`. A complete exact-record catalog of the mixed
historical design preserves all original bytes while the storage packet carries
its necessary history. No schema, source implementation, public path or runtime
behavior changes. Full contract: [compile progress](compile-progress.md);
[qualification research](../research/2026-11-03-compile-progress-storage-packet.md).

Pure event values and validation now belong to compile_event_records; this storage
facade retains identical exports and historical class identity. SQL behavior is
unchanged.

Shared canonical contract: [Compile event records](compile-event-records.md).

Protected schema declarations and validation now belong to compile_journal_schema;
the same exports remain here.

Shared canonical contract: [Compile journal schema](compile-journal-schema.md).
