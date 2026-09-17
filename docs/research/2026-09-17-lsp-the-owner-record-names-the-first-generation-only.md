# The owner record names the first generation only, and dead owner roots are swept

Dated 2026-09-17. Finding K-A10 of the third audit, both halves. The research before the
fix.

Files: `scripts/doctor.py`, `scripts/lsp_process.py`, `tests/test_doctor.py`,
`tests/test_a_restarted_server_reads_as_healthy_and_its_dead_neighbours_are_swept.py`.

## What was found

- **Doctor misreads a healthy restarted server.** `owner.json` is create-only
  (`_OwnerDirectory.write_record` uses `O_EXCL`; `CLAUDE.md` calls it "immutable
  create-only") and is written once at startup, naming generation 1's nonce and, through
  `owner_pid`, generation 1's server process. A recovery restart
  (`_adopt_candidate_locked`) installs generation 2 and republishes the lease, which then
  names the new nonce and the new server pid; nothing rewrites `owner.json`, and nothing
  may. `doctor._lsp_nonces_match` requires `owner["generation_nonce"] ==
  lease["generation_nonce"]` and `_lsp_records_match` requires `owner["owner_pid"] ==
  lease["server_pid"]`, so for a restarted server `matching` is False → `live: False`,
  `unreadable` → `lsp_state_unreadable`, and when the second generation later fails,
  `_failure_identity_mismatch` compares the failure's generation nonce and pid with
  generation 1's and adds `lsp_failure_evidence_retained` over a contradiction that is not
  one. Those findings are what makes the SessionStart advisory inject degraded health.
  Two tests pinned the wrong rule
  (`test_doctor_lsp_rejects_lease_generation_identity_mismatch`,
  `test_doctor_lsp_rejects_failure_generation_identity_mismatch`); doctor's other fixtures
  never restart, which is why this was invisible.
- **Nothing sweeps dead owner roots.** A controlled close removes `run/lsp/<nonce>/`
  itself (`_remove_success_scratch_posix`). A failure or an abrupt death leaves it, with
  its `owner.json`, its expired `lease.json`, and any `launch-<hex>/` tree sealed `0o500`
  directories and `0o400` files. No code in `scripts/` removes any of it, so the directory
  grows one entry per crash for the life of the installation.

## Practice on this date

- The cross-check that was lost is not the binding one. Every record in the directory
  already has to carry `owner_nonce` equal to the directory's own name
  (`doctor._valid_lsp_owner`, `_lsp_lease_identity_valid`, `_valid_lsp_nonces`), and the
  live record — the lease — carries its own expiry, heartbeat and pids, which doctor
  probes. The generation nonce in `owner.json` is a record of how the owner *started*, not
  a statement about what it is running now.
- Proof of death for a sweep: `kill(pid, 0)` answering `ESRCH` is the only cheap proof, and
  POSIX bounds the reuse that would spoil it — "A process ID shall not be reused by the
  system until the process lifetime ends" (Base Specifications Issue 7, XBD 4.14 Process
  ID Reuse, <https://pubs.opengroup.org/onlinepubs/9699919799/basedefs/V1_chap04.html>,
  fetched today). Reuse can only make a dead owner look alive, which is the safe
  direction: such a root is skipped and tried again next time.
- `run/` deletion contract (`CLAUDE.md`): `run/` must not be deleted while doctor reports
  "any retained queue task or result, or while a project lease, writer, queue worker, or
  maintenance owner is live", and the LSP slice adds "live LSP owners and retained LSP
  failure evidence". A sweep that removes only proven-dead owners holding no
  `failure.json` takes nothing the contract protects.

## The decision

1. `owner.json` and `failure.json` stay immutable create-only. Doctor stops requiring the
   owner record's generation nonce and pid to equal the lease's or the failure's: the
   owner record names the owner and its first generation, the lease names the live
   generation, the failure names the generation that failed. What still binds them is the
   `owner_nonce` every record carries and the time ordering doctor already checks
   (`owner.started_at <= heartbeat_at <= now`, `owner.started_at <= failed_at <= now`).
   The two tests that pinned the old rule are rewritten to pin the new one, and a new test
   walks a real restart through `_lsp_runtime_check`.
2. Dead owner roots are swept when a new server is started — the one moment the product is
   already working in `run/lsp/`, and no daemon is needed. A sibling root is removed only
   when it holds no `failure.json`, its `owner.json` is readable, and every pid its
   `owner.json` and `lease.json` name is proven dead. Sealed `launch-` trees are made
   writable before removal. The sweep is bounded to the same 128 entries doctor scans,
   never raises, and never touches a root whose liveness it could not settle.
3. Roots that *do* hold failure evidence stay. They are what an operator reads after a
   failure, doctor already reports them with their age, and deleting them would be
   deleting evidence inside its retention window. Removing those stays an explicit
   operator action.

Rejected: adding a `generation_index` to the lease and to the failure record so doctor
could keep the equality check for first generations (a second piece of state defending an
invariant that is not the binding one, forgeable by anything that could forge the nonce);
a sweep in `doctor --repair` only (an installation that never runs doctor never sweeps);
re-implementing doctor's full liveness judgement inside the sweep (one judgement, two
implementations).
