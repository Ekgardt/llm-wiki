# A report link outlives no report

Date: 2026-09-25. Audit item C-31 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and on the live vault, read-only)

- `maintenance_helpers.prune_maintenance_output` applied the report rules (30 days, 60 files,
  32 MB) to the step artifacts under `logs/maintenance/` too. The live directory held exactly 60
  artifacts (260 KB) from 2026-09-23 to 25: 13, 15 and 32 a day. A `nightly-*.md` report is kept
  30 days and names the artifact holding each step's full output, so after about two nights its
  pointers led nowhere.

## Source

- logrotate(8), https://man7.org/linux/man-pages/man8/logrotate.8.html (fetched 2026-09-25):
  "Log files are rotated _count_ times before being removed" and "maxage count — Remove rotated
  logs older than <count> days." A count and an age are two separate bounds; the count must be
  sized for the age it is meant to allow.

## Decision

- Artifacts keep the report age (30 days) and the same 32 MB family bound, with their own count
  bound sized for the age: `REPORT_RETENTION_DAYS × 64` (twice the busiest measured day).

## Files

- `scripts/maintenance_helpers.py`
- `tests/test_a_report_link_outlives_no_report.py`
- `CHANGELOG.md`
