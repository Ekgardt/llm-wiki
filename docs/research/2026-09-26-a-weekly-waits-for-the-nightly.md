# A weekly waits for the nightly

Date: 2026-09-26. Audit 2026-09-26 B-20.

## Facts

- Nightly and weekly share one maintenance fence. `scheduled_weekly.main` asked
  for it once; a nightly still running returned `None` (legacy) or raised
  `owner_busy` (canonical), and the weekly recorded a skip and exited 0 — the week
  was lost, and nothing asked again until the next Sunday.
- `doctor._weekly_is_stale` never judged a vault with no weekly run on record, so
  a weekly that had only ever been skipped stayed green.
- Bounds measured on this checkout (2026-09-26): nightly worst case 11 790 s,
  weekly 6 720 s; the installed scheduler limits are 4 h and 6 h
  (`install_control.SCHEDULER_LIMIT_HOURS`).
- systemd.service(5) (https://man7.org/linux/man-pages/man5/systemd.service.5.html,
  fetched 2026-09-26), `TimeoutStartSec=`: "If a daemon service does not signal
  start-up completion within the configured time, the service will be considered
  failed and will be shut down again." The units set it explicitly, so a longer
  weekly must still fit under 6 h.

## Decision

- The weekly asks for the fence every 60 s for as long as the nightly can run by
  its own bounds (`scheduled_nightly.worst_case_seconds()`), then skips as before.
  Any refusal other than `owner_busy` still skips at once. The weekly's
  `worst_case_seconds` counts the wait: 5.16 h, under the 6 h limit, which a test
  holds.
- Doctor reports a weekly that has a recorded skip and no recorded run as
  degraded, "Weekly maintenance has never completed", with the skip's reason.

## Files

- `scripts/scheduled_weekly.py`
- `scripts/doctor.py`
- `tests/test_a_weekly_waits_for_the_nightly.py`
- `CHANGELOG.md`
