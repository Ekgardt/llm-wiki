# A heartbeat beats at the pace of its own lease

Dated 2026-09-14. The nightly maintenance has failed every night since
2026-09-13, and the session context now opens with "The nightly maintenance is
failing". This is the research before the fix.

## What was observed

- `logs/nightly-2026-09-13.md`: Step 3c starts 03:03:23, traceback 03:04:08 —
  45 seconds. `logs/nightly-2026-09-14.md`: starts 03:01:02, traceback 03:01:46 —
  44 seconds. The step's budget is `REFRESH_ALL_BUDGET_SECONDS = 15 * 60`.
- `logs/maintenance/20260914T030102-repositories-445729.err.log` ends in
  `TimeoutError: Evidence Graph construction cancelled`, raised by
  `evidence_graph._check_build_stop` inside a build that
  `repository_worktrees._fenced_index` started through
  `repository_index.run_fenced`. The caller passed no `cancelled` of its own, so
  the stop came from the fence guard: `_MaintenanceHeartbeat.cancelled()` is
  "deadline reached or fence lost", and the deadline was fourteen minutes away.
- A manual `repository_index.py refresh <vault> --budget-seconds 3000`
  earlier the same day was cancelled at 46-47 seconds. Same shape.

## The mechanism, read in the code

- `run_fenced` acquires `REFRESH_ROLE = "doctor"` from the ownership registry
  (`operational_ownership.OwnershipRegistry.acquire`). The registry grants every
  role its timing from `_timing(role)`: `(120, 40)` for the long-lease roles
  (`queue-worker`, `compile`, `nightly`, `weekly`, `queue-operator`, `repair`) and
  **`(30, 10)` for every other role, `doctor` included** — a 30-second lease meant
  to be renewed every 10 seconds.
- It then hands that lease to `doctor._MaintenanceHeartbeat`, which renews on a
  fixed `MAINTENANCE_HEARTBEAT_SECONDS = 40.0`, a constant sized for the legacy
  120-second maintenance row.
- `OwnershipRegistry._heartbeat_in_transaction` renews only `WHERE ... AND
  expires_at > now`. At the first beat, 40 seconds in, the 30-second lease has
  already expired; the update matches no row and raises `owner_fence_lost`;
  `_beat_once` treats that `RuntimeError` as a fence taken by someone else and
  sets `_lost`; the next `_check_build_stop` cancels the build. 40 seconds plus
  the stop-check spacing is the 44-47 seconds seen in every log.
- The doctor's own fence does not have this bug by a comment, not by
  construction: `_V3_MAINTENANCE_ROLE = "repair"` was chosen because its
  "(120 s TTL, 40 s heartbeat) matches MAINTENANCE_LEASE_SECONDS and
  MAINTENANCE_HEARTBEAT_SECONDS".
- Why it appeared on 2026-09-13: every fenced build before then finished inside
  30 seconds, so no beat was ever due. On 2026-09-12 the vault itself became a
  repository, and its worktrees are followed; a generation of this repository
  takes minutes. I believe that is the trigger; I have not replayed the older
  nights to prove no earlier build crossed 30 seconds.

The class: **one guard renews leases of several timings at one fixed pace.** The
three writers through `run_fenced` — refresh, worktree follower, retention — all
hold a 30-second lease; the doctor holds a 120-second one.

## Practice on this date

- A lease holder "must renew it with a heartbeat before the timer runs out", and
  the TTL should be "at least 3x your typical heartbeat interval … so a single
  missed heartbeat does not lose the lease"
  ([lease pattern](https://singhajit.com/distributed-systems/lease/),
  [lock renewal with a heartbeat](https://oneuptime.com/blog/post/2026-03-31-redis-lock-renewal-heartbeat/view)).
  Kubernetes leader election states the same relation as three numbers of one
  lease: 15 s duration, 10 s renew deadline, 2 s retry
  ([etcd leases and TTL](https://adhdecode.com/articles/etcd/etcd-lease-ttl-setup/)).
- The renewal cadence is a property of the lease that was granted, which is why
  this registry already stores `heartbeat_seconds` on every `OwnerLease` and
  refuses a lease whose pair differs from `_timing(role)`. Every other renewer in
  the product reads it: `operational_ownership.heartbeat_owner`,
  `private_vault_backup`, `markdown_transaction` all wait
  `lease.heartbeat_seconds`. `_MaintenanceHeartbeat` is the one that does not.

## The decision

`_MaintenanceHeartbeat` renews at the cadence of the lease it holds: the
registry lease's own `heartbeat_seconds` when it holds one, the legacy
`MAINTENANCE_HEARTBEAT_SECONDS` when it holds the legacy row. The join on exit
uses the same number. Nothing else changes: the tolerance of two missed beats
(`MAX_HEARTBEAT_FAILURES = 2`) still ends before the lease does — 20 of 30
seconds for `doctor`, 80 of 120 for `repair` and the legacy row.

Why not the alternatives:

- **Switch `REFRESH_ROLE` to `repair`.** It would make this caller match the
  constant again, and leave the guard wrong for the next lease of another
  timing — fixing the instance, not the class. It would also change what the
  registry records and what `tests/test_repository_refresh.py` pins.
- **Lower `MAINTENANCE_HEARTBEAT_SECONDS` to 10.** Correct for `doctor`, but it
  quadruples writes to the legacy table and to the doctor's `repair` fence, and
  it is still one constant standing in for a per-lease fact.
- **Treat an expired lease as transient.** An expired lease can be taken by
  another process; carrying on would be the double-writer the fence exists to
  prevent.

## How it is checked

- A test on a really adopted vault: the guard `run_fenced` builds renews at the
  lease's own `heartbeat_seconds`, and two missed beats end before its
  `ttl_seconds`.
- A test on the legacy row: the guard still renews at
  `MAINTENANCE_HEARTBEAT_SECONDS`, so the existing heartbeat tests keep their
  meaning.
- The proof on the live vault is the next nightly's Step 3c finishing; a manual
  `repository_index.py refresh-all` from the main checkout shows it earlier.

Files: `scripts/doctor.py`, `tests/test_repository_refresh.py`,
`tests/test_doctor.py`,
`docs/research/2026-09-14-a-heartbeat-beats-at-the-pace-of-its-own-lease.md`.
