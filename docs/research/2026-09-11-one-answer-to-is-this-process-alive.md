# One answer to "is this process alive"

Date: 2026-09-11. Trigger: audit finding OPS-08. PID liveness was written
five times with three failure policies: `memory_state._is_pid_alive`
(any `OSError` → dead, so a process owned by another user reads as dead
and its lock is stolen), `markdown_transaction._pid_alive` (a copy),
`doctor._pid_alive` (a third copy) beside `doctor._lsp_pid_state`, which
already answers `alive | dead | unknown` with the right policy
(`ESRCH` → dead, `EPERM` → unknown, Windows error 87/1168 → dead, anything
else → unknown), `memory_queue` (any exception → alive), and
`operational_ownership.process_identity_state` (start identity, PID-reuse
safe, `unknown` on doubt). The legacy locks (`compile.pid`,
`maintenance.lock`, `state.json.lock`, the doctor's `_live_owner`) trusted
the boolean copies.

## Sources

1. `kill(2)`: `ESRCH` means no such process; `EPERM` means the process
   exists and belongs to another user — it is alive.
   https://man7.org/linux/man-pages/man2/kill.2.html
2. Windows `OpenProcess`: `ERROR_INVALID_PARAMETER` (87) for a PID that no
   longer exists; `GetExitCodeProcess` returns `STILL_ACTIVE` (259) for a
   running process.
3. This repository: `doctor._lsp_pid_state` is the one three-state
   implementation; `operational_ownership` names the policy ("doubt
   refuses"); the compile-lock note of 2026-09-10 (a lock lives as long as
   its process).

## Decision

`scripts/process_liveness.py` holds the three-state probe lifted from the
doctor (`process_state(pid)`) and the one boolean the legacy locks need,
`pid_alive(pid)`: `dead` → False, `alive` or `unknown` → True, so doubt
never steals a lock. `memory_state._is_pid_alive`, `markdown_transaction._pid_alive`,
`doctor._pid_alive` and `doctor._lsp_pid_state` delegate to it; the copies
are gone. `operational_ownership` keeps the start-identity probe for owners
that recorded one; a PID-only marker cannot be made reuse-safe by any
probe, and the note of 2026-09-10 on the canonical fence says why the
registry is the answer there.

Files: `scripts/process_liveness.py`, `scripts/memory_state.py`,
`scripts/markdown_transaction.py`, `scripts/doctor.py`,
`tests/test_process_liveness.py`, `tests/test_maybe_compile.py`,
`CHANGELOG.md`, `docs/AUDIT-2026-09-10-operations-and-reliability.md`.
