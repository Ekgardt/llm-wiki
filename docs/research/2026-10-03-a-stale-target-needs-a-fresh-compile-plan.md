# A stale target needs a fresh compile plan

Decision and research date: 2026-10-03. Installed runtime: Python 3.12.3,
SQLite 3.45.1, rollback journal. Python 3.10 compatibility is retained. No
database engine, journal, runtime path, model, configuration or architecture
contract changes.

## Reproduction and cause

Publication reconstructs its claim assessment after a failed precondition but
retains the immutable target snapshots that the model's plan used. When an
existing note changes, a fresh claim-tree assessment cannot restore its old
target hash. The old code nevertheless assessed that plan four times. Two
regression scenarios reproduced this: a change before assessment, and a
change during the first apply. Both failed their original assessment-count
assertions (four instead of zero or one); neither permitted a stale write.

Two actual retained refused transactions were checked against the current
tree without changing them. Both have changed original targets. The new
guard rejects them before assessment in a combined 0.4085 seconds, with no
model call. Their private identities and paths remain in the ignored local
measurement, not this public document.

## Primary sources and alternatives

- [SQLite isolation](https://www.sqlite.org/isolation.html): a stale snapshot
  cannot become writable merely by retrying the write; a new transaction is
  needed. This source describes both rollback and WAL; WAL is not selected.
- [PostgreSQL 18 serialization failure handling](https://www.postgresql.org/docs/current/mvcc-serialization-failure-handling.html): retry the whole decision,
  including the logic that selected the values. This is concurrency guidance,
  not a proposal to install PostgreSQL.
- [AWS DynamoDB optimistic locking](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DynamoDBMapper.OptimisticLocking.html): a version conflict
  protects newer values; refreshing the item is distinct from disabling the
  version check. This is concurrency guidance, not a cloud dependency.

Checked current official documentation on the decision date. Retrying the
same assessment cannot repair changed immutable inputs. Dropping the target
precondition or rebasing an old model plan could publish decisions made
against stale context; both are rejected. Holding the writer gate across
model work blocks capture and is also rejected. Immediate recursive model
replanning inside publication would need a separately qualified complete
cost and termination policy; it is not introduced here.

## Selected behavior and limits

Use the already-read claim-tree manifest to compare immutable note targets
before claim assessment. A changed target raises `compile_snapshot_changed`,
which is not the transient `precondition_failed` branch. No extra scan,
arbitrary retry limit, new cache or second implementation is added. An
inserted page can still trigger the existing reassessment retry; an unchanged
target plan still follows the existing publication path. Final transaction
preconditions remain authoritative, including changes after this check.

The failed source remains unreceipted and its diagnostic is retained. The next
ordinary compile snapshots and resolves it against fresh context. The tests
also prove that a fresh snapshot subsequently commits. This change removes
futile assessment work; it does not establish successful processing of every
retained source, complete token savings, or completion of the audit. No
whole-system quality claim follows from a cheaper failed attempt.

## Validation

Two original failures reproduced before the fix. The complete transaction
module passed (77 tests); after adding fresh-snapshot recovery assertions,
187 related tests passed. Actual changed-function Lizard records match AST
start lines; maximum CCN is 5, and the repository's shape guard passes. Ruff
passes. Installed verification and fresh code-index evidence are recorded
separately after installation; this document does not predeclare them.

Source: `scripts/compile_memory.py`, `tests/test_compile_transactions.py`.
