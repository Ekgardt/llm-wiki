# The LSP sweep reaches every dead root

Date: 2026-09-25. Audit item C-39 (docs/AUDIT-2026-09-25-full.md).

## Fact
- `lsp_process._sweep_owner_parent` stops after the first 128 `scandir` entries of
  `run/lsp`. Roots that hold `failure.json` are never swept and still use up that
  window, so with more than 128 retained roots the dead roots past it are never
  looked at. The live vault held 80 roots on 2026-09-25, all failure evidence.
- `_owner_root_is_dead` proves death by pid only (`process_state(pid) == "dead"`).
  `owner.json` and `lease.json` carry no process start identity, so a pid the OS
  handed to another process keeps a dead root forever.
- `_unseal_for_removal` calls `os.chmod` on every name `os.walk` lists; `os.chmod`
  follows symbolic links.
- `process_liveness.process_start_identity` / `owner_alive(pid, identity)` already
  exist and are used by the other operational locks: with a recorded identity, a
  reused pid reads as dead.
- Doctor judges an LSP owner live only while its lease has not expired
  (`_lease_still_live`), so pid reuse does not reach doctor beyond 30 s.

## Sources (fetched 2026-09-25)
- proc_pid_stat(5), https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html:
  "(22) starttime %llu — The time the process started after system boot." With the
  boot id this is the identity `process_liveness` records on Linux; a reused pid has
  a different start time.
- chmod(2), https://man7.org/linux/man-pages/man2/chmod.2.html: for `fchmodat`,
  "AT_SYMLINK_NOFOLLOW — If path is a symbolic link, do not dereference it", and
  "ENOTSUP (fchmodat()) flags specified AT_SYMLINK_NOFOLLOW, which is not
  supported." So on Linux a link cannot be chmodded without following it; the
  only safe choice is not to chmod links at all (a link needs no mode to be
  unlinked).

## Decision
- The sweep scans up to 4 096 entries and examines at most 128 candidate roots;
  a root with `failure.json` is skipped with one `stat` and costs no budget.
- `owner.json` gains optional `owner_start_identity`, `lease.json` optional
  `manager_start_identity` and `server_start_identity` (written when the platform
  can tell, omitted otherwise). A pid with a recorded identity is dead when it is
  gone or now carries another identity; without one, the pid probe decides as
  before. Doctor accepts the optional fields and is otherwise unchanged.
- `_unseal_for_removal` skips symbolic links (`os.lstat`).

## Uncertainty
Records written before this change carry no identity; their roots keep the
pid-only judgment, which is conservative (a root is kept, never wrongly removed).

## Files
- scripts/lsp_process.py
- scripts/doctor.py
- tests/test_the_lsp_sweep_reaches_every_dead_root.py
