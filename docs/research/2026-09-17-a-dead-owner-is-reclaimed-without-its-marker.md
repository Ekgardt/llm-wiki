# A dead owner is reclaimed without its marker

Dated 2026-09-17. Finding Q-H2 of the third audit (high, reproduced). The research before
the fix.

Files: scripts/operational_ownership.py, tests/test_a_dead_owner_is_reclaimed_without_its_marker.py

## What was found

- Nightly and weekly share one marker file, `run/maintenance.lock`, but hold separate
  `(role, scope)` rows in `maintenance_owners`.
- `OwnershipRegistry._reclaim_or_refuse` proves the owner dead (lease lapsed and the OS
  agrees) and then calls `_lease_marker`, which demands that the marker file still be the
  exact file the dead row recorded. That demand belongs to a *live* owner (heartbeat,
  require, release). For a dead one it turns a missing or replaced marker into a permanent
  refusal: `marker_identity_invalid`, on every later attempt.
- The marker goes missing in ordinary life: nightly crashes, weekly finds no weekly row and
  a dead PID in the marker, removes it as an orphan, runs and releases. Every later nightly
  publishes a fresh marker, meets its own dead row, and is refused. `scheduled_nightly`
  records a skip and exits 0, so maintenance silently stops. An operator deleting the lock,
  or a commit failing after the unlink in `reclaim_dead_marker_owner`, gives the same wedge.
- Same shape, same function: a marker role reclaimed through `acquire` (`_require_admission`)
  can never pass the check, because the new owner has already published its own file at the
  same path.
- Code graph: `_reclaim_or_refuse` ← `_require_admission`, `_settle_deletion_check`,
  `reclaim_dead_marker_owner` ← `_publish_marker_reclaiming` ← `acquire_scheduled_owner`;
  also reached from `MarkdownCoordinator._claim_canonical_lease` and
  `ProjectStore._canonical_ownership` through `acquire`.

## Practice on this date

- Safety of a lease system rests on the fencing token, not on side files: "a fencing token
  is simply a number that increases (e.g. incremented by the lock service) every time a
  client acquires the lock", and the resource must take "an active role in checking tokens,
  and rejecting any writes on which the token has gone backwards" (Martin Kleppmann, *How to
  do distributed locking*, 2016,
  https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html). The registry
  already does this: epochs are monotonic and survive row deletion. The marker is a
  projection for readers that cannot open the database; it is not part of the proof.

## The decision

- Reclaim keeps its proof unchanged: expired lease and a provably dead process, doubt
  refuses.
- After the proof, the dead owner's marker is *retired*, not demanded: the file is removed
  only when it is still exactly the file the dead row recorded (same identity, same digest).
  A missing file, or a file that belongs to someone else, is left alone and does not block
  the reclaim. A live weekly's marker therefore survives a nightly reclaiming its dead row,
  and the nightly then refuses with `owner_busy` as it should.
- `reclaim_dead_marker_owner` no longer unlinks unconditionally after the reclaim; the
  retire step has done exactly that, with the check.
- Live-owner paths (heartbeat, require, release) keep the strict marker check.
