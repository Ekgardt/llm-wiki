# The weekly archive says why a day stays, and goes on past one failure

Date: 2026-09-25. Audit item B-9 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and read-only on the live vault)

- `archive_daily.main` drops every ineligible day without a word
  (`_archive_one` returns `None`) although `Eligibility` carries `reasons`, and
  the first exception from `eligible` or `archive` ends the loop and the step.
- Asked read-only (`DailyArchiver.eligible`, no `--commit`), the live vault keeps
  `2026-04-13` and `2026-04-19` flat because of `nonterminal_compile_operation`,
  `transaction_retention` and `decision_evidence`. Nothing in the weekly output
  says so. Why those reasons never clear is a state question (old receipts whose
  transaction is gone count as retained for ever) and is left to the state-layer
  fixes; this note is about the run saying it and not stopping.

## Source

- Google SRE book, "Monitoring Distributed Systems",
  https://sre.google/sre-book/monitoring-distributed-systems/ (fetched
  2026-09-25): "Every page should be actionable." A report that lists the normal
  state buries the one that needs action, so only days that should have left the
  hot window are named, and a failure changes the exit code.

## Decision

- A day older than the hot window that stays flat is printed with its reasons
  (`Kept flat: 2026-04-13: nonterminal_compile_operation, ...`). Days inside the
  hot window are not listed; that is the normal state of 90 days.
- A day whose check or archive raises is printed as failed with the error's
  class and a redacted message, the loop goes on to the next day, and the run
  exits 1 when any day failed, so the scheduler records the weekly as degraded.

## Files

- `scripts/archive_daily.py`
- `tests/test_the_weekly_archive_says_why_and_goes_on.py`
- `CHANGELOG.md`
