# The weekly pass has its own record

Dated 2026-09-24. Audit items A-2, A-3, B-11 and C-1 (`docs/AUDIT-2026-09-24-live.md`).

Files: `scripts/scheduled_weekly.py`, `scripts/scheduled_nightly.py`, `scripts/doctor.py`,
`tests/test_scheduled_weekly.py`, `tests/test_the_weekly_pass_has_its_own_record.py` (new),
`CHANGELOG.md`, `docs/research/2026-09-24-the-weekly-pass-has-its-own-record.md`.

## What was found

- **A-2, the nightly.** Since 2026-09-23 `record_scheduled_failure` writes a pass that failed
  before its first step into `last_nightly_status = failed`, and doctor's scheduler check
  turns that into an `error`; a pass that never ran at all is caught by the 26-hour interval
  (`NIGHTLY_FRESH_SECONDS`). Checked in the code on 2026-09-24: this half is closed.
- **A-3, the weekly.** The weekly writes nothing of its own:
  - a failure before its first step goes through `record_scheduled_failure`, which writes
    the *nightly's* field, and the next nightly overwrites it;
  - a failure inside the pass (`failures=1` on 2026-09-13) is recorded nowhere;
  - doctor has no weekly check (`grep -n weekly scripts/doctor.py` finds nothing).
  Live: `systemctl --user show llm-wiki-weekly.service` says `Result=exit-code`; the last
  successful weekly was 2026-09-06; its daily-archive step (added 2026-09-18) has never run,
  and two daily logs from April are still hot.
- **B-11.** `scheduled_weekly._run_weekly_body` calls `scheduled_nightly.run_nightly` as
  its Step 1, an hour after the nightly ran: a second compile, a second repository refresh
  (8 minutes on 2026-09-24), a second self-update and a second prune, and the nightly's
  record is overwritten by the weekly's copy of it.
- **C-1.** Nightly step labels are written by hand and drifted: "Step 3c" names three
  different steps, "3d" and "3e" two each, "3c''" runs before "3e". The module docstring
  still lists "the FTS5 index", retired on 2026-09-23. The weekly docstring says it runs
  "via Windows Task Scheduler" on every platform.

## Practice on this date

- A scheduled job is judged by two signals: an explicit failure marks it down at once, and
  silence past its period plus a grace time marks it late ("Period is the expected time
  between pings. Grace Time is the additional time to wait before sending an alert when a
  check is late" — Healthchecks.io, "Configuring checks", fetched 2026-09-24). Each job
  needs its own record for either signal to exist.
- Labels that a log reader uses to find a step should be generated from the order the
  steps run in, not maintained by hand beside it.

## The decisions

1. The weekly records its own terminal result in `run/state.json`
   (`last_weekly_status`, `last_weekly_at`, `last_weekly_failure`), both when its fence
   fails and when the pass ends; it never writes the nightly's fields.
2. Doctor's scheduler check reads both: a failed weekly is an `error`; a weekly whose last
   success is older than 8 days (a 7-day period plus a day of grace) is `degraded`; a
   vault on which the weekly has never run says so without degrading.
3. The weekly no longer runs the nightly. Its own steps (OKF sweep, queue status,
   archiving, prune, contradictions, reflection, tiers) run after it; the nightly ran at
   03:00 and the weekly starts at 04:00.
4. Step labels are numbered by the logger in the order the steps run
   (`Step 1`, `Step 2`, …); no message carries a number. The docstrings are rewritten to
   match the code.

## Cost, by rule 4

A weekly Sunday pass shorter by one whole nightly (about 10 minutes and a second compile
on the live vault). Doctor reads two more keys of a file it already reads.

## Sources

- Healthchecks.io, "Configuring checks" — https://healthchecks.io/docs/configuring_checks/ — fetched 2026-09-24.
- `docs/research/2026-08-22-scheduled-job-freshness.md`; `systemctl --user show llm-wiki-weekly.service`
  and `journalctl --user -u llm-wiki-weekly.service` on the live vault, 2026-09-24.
