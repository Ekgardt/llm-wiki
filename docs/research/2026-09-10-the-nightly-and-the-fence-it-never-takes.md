# The nightly and the fence it never takes

Date: 2026-09-10. Trigger: audit findings OPS-02 and OPS-03. This note is a
proposal; it changes no code. The change it describes touches the ownership
control plane the owner approved on 2026-08-15 and 2026-08-27, so it waits
for the owner's yes (CLAUDE.md, "Architecture changes require explicit
sign-off").

## What is true today (2026-09-10)

- `scheduled_nightly.main()` and `scheduled_weekly.main()` take the legacy
  marker `run/maintenance.lock` (PID, stolen after 30 minutes when the PID
  is dead) and call `run_nightly(ownership=None)`. `_require_nightly_owner`
  is a no-op for `None`; `_owned_step_runner` and `_generation_result`
  pass a lease only if `inspect.signature` finds an `ownership` parameter,
  and neither `run_step` nor `run_generation_maintenance` has one. The
  lease plumbing is dead in production; it runs only in tests.
- The canonical registry has the roles `nightly` and `weekly`
  (`_MARKER_ROLES`), `acquire_scheduled_owner(role, state_root)` publishes
  the same marker path with `O_EXCL` and then acquires the row; the
  heartbeat refreshes a 120 s lease every 40 s; a dead owner's row is
  reclaimed only with proof (`_expired_owner_is_dead`: lease lapsed and the
  process probe says dead). The doctor counts live maintenance owners from
  those rows, so a running nightly is invisible to the `run/` deletion
  contract today.
- `heartbeat_owner` (the context manager the nightly would run under) keeps
  a lost fence in a list the body cannot see; it is raised only in `finally`
  and only when the body did not raise itself. `_run_nightly_body` records
  `success` in its own `finally` first. The doctor's `_MaintenanceHeartbeat`
  has the opposite policy: `_lost` is an event and `check()` raises between
  steps.
- The canonical marker path has no reclaim for the marker file: a nightly
  that dies without releasing leaves `run/maintenance.lock`, and
  `_publish_marker` fails with `FileExistsError` on every later run before
  the registry can prove the owner dead. The legacy path steals such a
  marker after 30 minutes. Wiring the nightly to the registry as it stands
  would trade a false "running" for a permanent "busy".

## Sources

1. Kubernetes leases (concepts/architecture/leases): a holder renews
   `spec.renewTime`, the lease is valid for `leaseDurationSeconds`, and the
   `holderIdentity` is recorded — the same shape as the registry's row.
   https://kubernetes.io/docs/concepts/architecture/leases/
2. client-go `leaderelection`: `OnStoppedLeading` "is always called when the
   LeaderElector exits"; `RenewDeadline` bounds how long a leader retries
   before giving up; and "this implementation does not guarantee that only
   one client is acting as a leader (a.k.a. fencing)" — the body must stop
   itself when told. https://pkg.go.dev/k8s.io/client-go/tools/leaderelection
3. This repository: `doctor._MaintenanceHeartbeat` (`_lost` event, `check()`
   between steps, two missed beats tolerated), `operational_ownership`
   ("doubt refuses"), and the compile-lock note of the same day (a lock lives
   as long as its process).

## Proposal (for the owner's decision)

1. `scheduled_nightly.main()` and `scheduled_weekly.main()` take
   `acquire_scheduled_owner` on an adopted vault and keep the legacy marker
   only where no V3 coordinator exists — the doctor's own
   `_acquire_maintenance_owner` shape. `owner_busy` is recorded as the skip
   reason by name.
2. Marker reclaim: when `_publish_marker` meets an existing
   `run/maintenance.lock`, the registry decides — a row whose owner is
   provably dead releases the marker (the same proof `_reclaim_or_refuse`
   already demands), any doubt refuses by name. No age-based stealing.
3. `heartbeat_owner` exposes the loss: a `threading.Event` set by the
   heartbeat thread, checked between nightly steps; a lost fence ends the
   pass with `owner_fence_lost` as the terminal error, recorded instead of
   `success`.
4. The `inspect.signature` plumbing goes: steps that need the lease take it
   explicitly, the rest take none.

Files this would touch: `scripts/scheduled_nightly.py`,
`scripts/scheduled_weekly.py`, `scripts/operational_ownership.py`,
`tests/test_scheduled_nightly.py`, `tests/test_operational_ownership.py`,
`docs/STRUCTURE.md`, and a decision page under `knowledge/notes/`.

## What is not settled

Whether `run_generation_maintenance` should run under the nightly's lease
or keep its own `repair` owner (today it takes its own, and the two roles
coexist). Whether a weekly pass that calls the nightly body should pass its
`weekly` lease down (today `_require_nightly_owner` accepts both).
