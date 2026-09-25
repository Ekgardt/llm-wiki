# The scheduler state keeps one clock

Date: 2026-09-25. Audit item C-30 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `scheduled_nightly` wrote `last_nightly_failure.failed_at` and `last_nightly_skip.skipped_at` as
  local time without an offset, and `last_nightly_at` as UTC with `+00:00`.
- `doctor._skip_is_newer_than_run` compared `skipped_at` with `last_nightly_at` as strings, so on
  a machine east of UTC a skip after the run could read as older, and one before it as newer, by
  the size of the offset.
- Dates (`last_nightly_date`, the claim's `date`) are the local calendar day the nightly is for;
  they stay dates.

## Source

- RFC 3339, https://www.rfc-editor.org/rfc/rfc3339 (fetched 2026-09-25), section 4.2: "Numeric
  offsets are calculated as 'local time minus UTC'. So the equivalent time in UTC can be
  determined by subtracting the offset from the local time." A stamp without an offset cannot be
  converted, which is the defect.

## Decision

- Every instant the nightly writes into the state is UTC with its offset (`_utc_now`).
- Doctor compares parsed instants; a stamp written before this change, with no offset, is read as
  local time, which is how it was written.

## Files

- `scripts/scheduled_nightly.py`
- `scripts/doctor.py`
- `tests/test_the_scheduler_state_keeps_one_clock.py`
- `CHANGELOG.md`
