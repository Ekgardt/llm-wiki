# A weekly pass counts what failed

Date: 2026-09-25. Audit item C-32 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `scheduled_weekly._reflect` and `_build_tiers` caught every exception, logged it and returned
  nothing, so `failures` stayed 0 and the weekly recorded `success` with a failed step.
- A weekly that found the maintenance fence held (a long nightly) printed "skipping", exited 0 and
  wrote no record; doctor later said only "Weekly maintenance is stale."

## Source

- PEP 20, https://peps.python.org/pep-0020/ (fetched 2026-09-25 for C-23): "Errors should never
  pass silently." / "Unless explicitly silenced." A step failure the pass reports as success is
  silenced without being explicit about it.

## Decision

- Both steps return 1 on failure and the pass adds them to `failures`, so the terminal record is
  `failed` and doctor's existing "Last weekly maintenance failed." applies.
- A skipped weekly writes `last_weekly_skip` (UTC time and reason); a stale weekly names the last
  skip. A skip still exits 0: the run did what it could.

## Files

- `scripts/scheduled_weekly.py`
- `scripts/doctor.py`
- `tests/test_a_weekly_pass_counts_what_failed.py`
- `CHANGELOG.md`
