# The installer says "owned" only after the transaction committed

Date: 2026-09-10. Trigger: audit finding OPS-05. `install.ps1` catches a
failed install-ownership transaction in step 6, prints a warning, sets
`$schedulerWarning`, and continues; step 7 then prints
`[OK] Claude settings owned by the install transaction` and lists
`Claude Code: active automatic` from a branch that never looked at the
transaction result. `install.sh` calls `fail` at the same step.

## Sources

1. `tests/test_sync_memory.py::test_powershell_installer_verifies_registered_scheduler_state`:
   the Windows installer's contract on a failed step 6 is "warn, print the
   whole summary with `Maintenance: not registered`, exit 1". The contract
   is deliberate — a Windows operator reads the summary — and this note
   keeps it.
2. Rule 3 (honesty): a success line must describe something that happened.
   The settings file is written by the transaction; when the transaction
   failed, the line is false and the integration state "active automatic"
   with it.
3. Local limit: PowerShell 7 is not installed on this machine, so the
   behaviour is verified by a source-level test here and by the Windows
   installer job in CI.

## Decision

In step 7, Claude is "automatic" only when step 6 committed:
`$claudeAutomatic = -not $schedulerWarning`. On a failed transaction the
line reads `[WARN] Claude settings not written: the install ownership
transaction failed` and the integration state says so. Nothing else in the
flow changes. A source-level test holds the guard in place.

Files: `install.ps1`, `tests/test_installer_bootstrap.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-operations-and-reliability.md`.
