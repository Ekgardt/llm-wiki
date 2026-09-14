# A lost lease is known by its expiry

Dated 2026-09-14. Item 0.5 of `docs/AUDIT-2026-09-14-2.md`, a gap in this morning's
`scripts/lease_renewal.py` (`docs/research/2026-09-14-a-busy-database-is-not-a-lost-lease.md`).
The research before the fix.

## What was found

- `renew_until_stopped` retries a transient failure while the lease has time left,
  and starts the retry even when one attempt cannot finish before the expiry. An
  attempt blocks on SQLite's busy wait: 10 s for the ownership registry and the
  Markdown coordinator (`DEFAULTS.markdown_busy_ms`), 5 s for the queue
  (`DEFAULTS.queue_busy_ms`) — values read from `scripts/reliable_memory.py` on this
  date. So a holder of a 30 s lease learns it is lost at up to 40 s; the existing test
  `test_a_lock_that_outlasts_the_lease_ends_it` allows exactly that (`now <= 40`).
- Between the expiry and that signal another process may take the lease. The
  registry refuses a renewal of an expired row and every completion is checked
  against its token, so the cost is duplicate work, not corrupted state — but the
  holder keeps working on a lease it no longer has.
- The code graph: `renew_until_stopped` has seven callers —
  `doctor._MaintenanceHeartbeat._heartbeat_loop` (registry),
  `operational_ownership.heartbeat_owner` (registry),
  `MarkdownCoordinator._heartbeat_canonical_writer_gate` (coordinator),
  `memory_queue._LeaseHeartbeat._run` and `_SourceFenceHeartbeat._run` (queue),
  `private_vault_backup._maintain_heartbeat` (registry),
  `flush_memory._CaptureKeepAlive._run` (queue, then the coordinator).

## Practice on this date

- Kubernetes leader election requires `LeaseDuration > RenewDeadline`: the holder
  stops retrying at the renew deadline, before the lease can be acquired by anyone
  else, and each try is itself bounded
  ([client-go leaderelection](https://pkg.go.dev/k8s.io/client-go/tools/leaderelection)).
  The margin between the two is what lets the old holder stop before a new one starts.
- SQLite's busy timeout bounds how long a statement waits on a lock
  ([sqlite3_busy_timeout](https://www.sqlite.org/c3ref/busy_timeout.html)).

## The decision

- `renew_until_stopped` takes `attempt_seconds`, the longest one renewal may block. A
  failed renewal is retried only if the retry can still finish by the expiry; when it
  cannot, the loop returns the error — so the loss is known by the expiry, not up to
  one busy wait after it.
- Each caller passes its database's busy wait: the registry and coordinator 10 s, the
  queue 5 s. The capture keep-alive renews both databases in one round and passes the
  longer, 10 s.
- The reproduced doctor case still survives: a lock held 11.5 s fails the beat at
  t=20 s, and the retry that starts at once (10 s left, 10 s needed) succeeds.

Limits, stated: a renewal that runs several statements may wait more than once, so
`attempt_seconds` is the usual bound, not a proof; the registry's own expiry check and
the token checks at completion stay the protection.

Why not the alternatives:

- **Shorten the busy wait.** It turns a slow renewal into a failed one sooner and
  still starts a retry that ends after the expiry.
- **Interrupt a blocked renewal from a watchdog.** A second thread per lease for a
  window the registry already fences.

Files: `scripts/lease_renewal.py`, `scripts/doctor.py`, `scripts/operational_ownership.py`,
`scripts/markdown_transaction.py`, `scripts/memory_queue.py`,
`scripts/private_vault_backup.py`, `scripts/flush_memory.py`,
`tests/test_a_busy_database_is_not_a_lost_lease.py`,
`docs/research/2026-09-14-a-lost-lease-is-known-by-its-expiry.md`.
