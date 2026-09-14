# A busy database is not a lost lease

Dated 2026-09-14. Items 3.2 and 3.4 of `docs/AUDIT-2026-09-14.md`. The research
before the fix.

## What was found

Seven threads keep a held lease renewed, and each decides on its own when a
failed renewal means the lease is gone:

| renewer | on a failed renewal |
|---|---|
| `doctor._MaintenanceHeartbeat` | `RuntimeError` → lost; anything else counted, lost after 2 in a row |
| `operational_ownership.heartbeat_owner` (nightly, weekly) | lost at the first exception |
| `MarkdownCoordinator._heartbeat_canonical_writer_gate` | lost at the first exception |
| `memory_queue._LeaseHeartbeat` | lost at the first exception |
| `memory_queue._SourceFenceHeartbeat` | lost at the first exception |
| `private_vault_backup._maintain_heartbeat` | lost at the first exception |
| `flush_memory._CaptureKeepAlive` (added today) | stops after 2 failed rounds |

A registry connection waits up to 10 seconds on a lock
(`operational_ownership`, `busy_ms=DEFAULTS.markdown_busy_ms`), and a locked
database raises `sqlite3.OperationalError`. So a writer holding the database for a
few seconds ends a nightly pass, a writer gate, a queue lease or a backup at once;
the Markdown gate's own comment says "a heartbeat that failed transiently is not a
lost gate" while its loop treats it exactly as one.

The doctor's "two misses" rule, which I adapted this morning to a lease's own pace,
does not hold either: reproduced with the real registry and a second connection
holding the lock for 11.5 s, a `doctor`-role lease (30 s, renewed every 10 s)
failed its beat at t=20 s, and the next beat, waited a full interval after the
failed one, started at t=30 s — when the lease had already expired — and the fence
was declared lost. Counting misses measures nothing about the lease; its expiry
does.

## Practice on this date

Kubernetes leader election, the reference implementation of lease renewal, keeps
three separate numbers: `LeaseDuration` (how long the lease is valid),
`RenewDeadline` ("the duration that the acting master will retry refreshing
leadership before giving up") and `RetryPeriod` ("the duration … clients should
wait between tries"), with `LeaseDuration > RenewDeadline > RetryPeriod`
([client-go leaderelection](https://pkg.go.dev/k8s.io/client-go/tools/leaderelection)).
A failed renewal is retried on the short period, and the holder gives up only when
the deadline, derived from the lease, has passed — never after a count of
failures. A renewal that failed because the lease was taken is final at once
([lease pattern](https://singhajit.com/distributed-systems/lease/)).

## The decision

One renewal loop, `scripts/lease_renewal.py`, used by all seven:

- `renew_until_stopped(renew, *, interval, lease_seconds, stop, transient)` renews
  every `interval`. A renewal that raises a transient error — by default
  `sqlite3.OperationalError`, the busy or locked database — is retried every
  `interval / 5` until the lease's own expiry, measured from the start of the last
  renewal that succeeded (or from entry, for the lease as acquired). Any other error
  is final at once: every "fence lost" in this codebase is a `RuntimeError`
  (`OperationalOwnershipError`, `MaintenanceFenceLost`, `QueueOperationError`,
  `LeaseFenceError`, `BackupError`). It returns the error that ended the lease, or
  `None` when stopped.
- Each renewer keeps what it does with the result (set `lost`, record `error`,
  raise on exit). The doctor's `_beat_once` stays the test seam it is today.
- Renewing past expiry is not attempted: the registry refuses a renewal of an
  expired row (`expires_at > now`), so the loop's deadline and the database agree.

Why not the alternatives:

- **Raise `MAX_HEARTBEAT_FAILURES`.** A count still says nothing about time; the
  failure came from waiting a full interval after a slow failure.
- **Shorten the busy wait.** It turns a slow renewal into a failed one sooner and
  leaves every loop's give-up rule as it is.
- **Fix only the doctor.** Six other loops have the same defect; the audit found
  them by the same shape.

Files: `scripts/lease_renewal.py`, `scripts/doctor.py`,
`scripts/operational_ownership.py`, `scripts/markdown_transaction.py`,
`scripts/memory_queue.py`, `scripts/private_vault_backup.py`,
`scripts/flush_memory.py`, `tests/test_a_busy_database_is_not_a_lost_lease.py`,
`docs/research/2026-09-14-a-busy-database-is-not-a-lost-lease.md`.
