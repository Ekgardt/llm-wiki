# A skipped night and a failed adoption say why

Date: 2026-09-11. Trigger: audit findings OPS-23 and OPS-20.

OPS-23: `scheduled_nightly` records `last_nightly_skip` with a reason
(`maintenance_lock_held`, or since 2026-09-10 the registry's own code such
as `owner_busy`), and the doctor puts it into `details` — but its message
after 26 hours says only "Nightly maintenance is stale", so an operator
reading the message does not learn that the pass ran and skipped, or why.

OPS-20: `install.sh` runs the V3 adoption check with `2>/dev/null` and the
adoption itself with `>/dev/null 2>&1`; on failure it prints a warning that
tells the user to run the same command blind.

## Sources

1. This repository's rule for failing steps (`docs/research/2026-09-10-a-failing-step-says-why-not-only-its-class.md`):
   the reason lands where the reader is.
2. `maintenance_helpers.run_step`: a subprocess's stderr head is kept on
   disk and quoted in the report — the shape for the installer too.

## Decision

- Doctor: when `last_nightly_skip` is newer than the last recorded run, the
  message reads `Nightly maintenance is stale; the last pass skipped:
  <reason> (<date>).` The status stays `degraded`.
- `install.sh`: the adoption check and the adoption write their stderr to
  `$STATE_ROOT/logs/install-adoption.err.log`; on failure the warning
  quotes the last lines of that file and names the path.

Files: `scripts/doctor.py`, `install.sh`, `tests/test_doctor.py`,
`tests/test_installer_bootstrap.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-operations-and-reliability.md`.
