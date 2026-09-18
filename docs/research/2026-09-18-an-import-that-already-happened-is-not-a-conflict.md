# An import that already happened is not a conflict

Dated 2026-09-18. Finding Q-L19 of the third audit (operational core, legacy queue migration).

Files: scripts/memory_queue.py,
tests/test_a_reimported_legacy_record_is_not_a_conflict.py

## What was found

`_import_legacy_records` does two things per record and cannot do them together:

```python
imported.append(_import_legacy_record(queue, record, source))
source.unlink()
```

The insert commits, then the staged file is removed. A process that dies between them — a
migration runs at session start, and the machine can be shut down — leaves the file behind, so
the next migration imports the same record again. `_import_legacy_record` is written for that:
`INSERT OR IGNORE`, then read the row back and compare it with the columns the record implies.

The comparison is the problem. It compares **all ten** columns, and six of them move as soon as
the queue does its job: `state`, `updated_at`, `available_at`, `attempts`, `last_attempt_at`,
`error_code`. The window between the commit and the crash is exactly the window in which that
task became claimable, so the likely case is that a worker has already leased it. The second
migration then reads a row that no longer matches, raises `legacy_import_conflict`, and — because
the source file is still there and nothing quarantines it — raises it again on every session
start, for ever. The finding is marked "unadopted vaults only", which is right: it is the vault
of somebody who has not migrated yet, and it is stuck before it can.

## Practice on this date

The insert is already idempotent by primary key; only the verification is not. The rule an
idempotent writer needs is that the check reads the part of the record the writer owns, not the
part the system is free to change afterwards. Stripe states both halves of the contract this code
is trying to implement — "A client generates an idempotency key, which is a unique key that the
server uses to recognize subsequent retries of the same request", and, on what the comparison is
for, "The idempotency layer compares incoming parameters to those of the original request and
errors if they're not the same to prevent accidental misuse"
([Stripe API, *Idempotent requests*](https://docs.stripe.com/api/idempotent_requests)). The
comparison is against the *request*, not against what the object has done since. The legacy
record's id is the key, its four immutable columns are the request, and a lease taken in the
meantime is neither.

## The decision

- The verification splits in two. A row whose **immutable** columns — `kind`, `payload_json`,
  `input_hash`, `created_at` — match the record is this record's row, and the import is complete
  whatever its state and attempts now say. Anything else, including a missing row, is still
  `legacy_import_conflict`: a different task holding this id is a real collision and must stop the
  migration.
- Nothing else changes. The insert stays `INSERT OR IGNORE`, the unlink stays where it is, and a
  record imported twice still lands exactly once.
