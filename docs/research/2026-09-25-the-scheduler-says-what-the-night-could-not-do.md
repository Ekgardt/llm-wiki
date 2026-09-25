# The scheduler check says what the night could not do

Date: 2026-09-25. Audit items A-18, B-23 and B-24 of `docs/AUDIT-2026-09-25-full.md`.

## Question

Three silent states sit in the scheduler: the systemd limit is below the pass's
own worst case, the installed units are older than the release that added the
limit, and the nightly code update reports what happened only in that night's log.
How does each become visible, and how is the limit kept above the worst case?

## Sources

- systemd.service(5), `TimeoutStartSec=` (fetched 2026-09-25,
  https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html):
  for `Type=oneshot` there is no start timeout by default; when set and exceeded
  the service is considered failed and is shut down.
- `docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md`: the pass's
  worst case counts every candidate of the provider order in auto mode (about
  3.2 h nightly, 4.9 h weekly); the Windows tasks were raised to 4 h and 6 h.

## Findings (facts)

1. `install_control.SYSTEMD_START_LIMITS = {"nightly": "3h", "weekly": "5h"}`,
   `WINDOWS_TASK_LIMIT_HOURS = {"nightly": 4, "weekly": 6}`: two tables for one
   contract. The installed units on this machine run with no forced provider, so
   the auto-mode worst case applies (`scheduled_nightly.worst_case_seconds()` =
   11 490 s). The test compares against the `fake` provider the test harness
   forces (10 770 s), which is why it passed.
2. `systemctl --user cat llm-wiki-nightly.service` on this machine has no
   `TimeoutStartSec`: the units were rendered 2026-09-13, the limit came 2026-09-14,
   and nothing compares an installed unit with what the release renders.
3. `scheduled_nightly._update_code` only logs the outcome. `fetch_failed`, `error`,
   `dependencies: stale`, `resources: rerun_installer` and
   `not_on_default_branch` never reach doctor.

## Decision (conclusion)

- **One table of limits**, `SCHEDULER_LIMIT_HOURS = {"nightly": 4, "weekly": 6}`,
  renders both the systemd units and the Windows tasks. The tests compare it with
  the worst case in auto mode, the mode an installed scheduler runs in.
- **The nightly records its update outcome** in `run/state.json`
  (`last_update`), and doctor's `scheduler` check is degraded, with the reason
  named, when the update failed, left dependencies stale, needs the installer, or
  cannot run because the checkout is not on its default branch.
- **Doctor reads the installed systemd units** and is degraded when their
  `TimeoutStartSec` is not the one this release renders, naming the installer as
  the fix. A maintenance pass does not rewrite them itself: owned scheduler
  resources are the installer's (2026-09-17 decision).

## Edited files

- `scripts/install_control.py`, `scripts/scheduled_nightly.py`, `scripts/doctor.py`
- `tests/test_ci_and_scheduler_gaps.py`, `tests/test_the_scheduler_outlasts_the_pass.py`,
  `tests/test_the_scheduler_says_what_the_night_could_not_do.py` (new)
