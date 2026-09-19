# The scheduler log is bounded and the hard kill is named

Dated 2026-09-17. Findings I-A15 and I-A14 of the third audit (low, confirmed, both marked
«решение владельца»; the owner delegated the decision). The research before the fix.

## I-A15 — scheduler logs grow without bound

- `install_control` renders `logs/scheduled-{nightly,weekly}.log` (launchd
  `StandardOutPath`/`StandardErrorPath`) and `logs/cron-{nightly,weekly}.log` (cron `>>`).
  Both are append-only for the life of the vault.
- Retention exists for everything else the maintenance passes write:
  `prune_maintenance_output` applies age (30 days), count (60 files) and total size (32 MB)
  to each family in `MAINTENANCE_REPORT_PATTERNS`, plus the step artifacts. The two
  scheduler logs are named by no pattern, so nothing ever touches them.
- launchd's plist has no append/rotate option, and a cron line cannot date its own
  redirection safely here, because cron reads `%` as a newline and the renderer escapes it.
  So the bound has to come from the retention that already exists.

Decided: add `scheduled-*.log` and `cron-*.log` to `MAINTENANCE_REPORT_PATTERNS` — one line
in `scripts/maintenance_helpers.py`, another area's file, kept to the smallest change. The
age and count rules never fire for two files written nightly; the 32 MB family size does, and
that is the bound. A file deleted between runs is recreated by the next run; the readable
report of a pass is `logs/nightly-*.md`, which has its own retention.

## I-A14 — launchd and cron have no time limit

- systemd gets `TimeoutStartSec` and Windows Task Scheduler gets `ExecutionTimeLimit`, both
  3 h nightly / 5 h weekly, above the computed worst cases (2.41 h and 3.94 h).
- launchd has no equivalent: a LaunchAgent job runs until it exits. cron has none either.
  GNU `timeout` would wrap a cron line on Linux, but macOS ships no `timeout`, and the cron
  backend is the documented fallback on both, so a wrapper would be a bound on one platform
  and nothing on the other.
- A hung pass is not unbounded in every sense: it holds a maintenance lease, and a dead owner
  is reclaimed. A process that hangs while still heartbeating is the case no lease ends.
- The portable bound is a deadline the pass enforces on itself, inside
  `scripts/scheduled_nightly.py` — another agent's area in this round, and the same place the
  audit's M-B3 asks for the worst-case sum to be corrected (`worst_case_seconds` omits
  `_update_code` and `_prune_reports`).

Decided: no wrapper is added. The asymmetry is written down in `docs/USER-GUIDE.md` where the
scheduler limits are described, so an operator on macOS or cron knows there is no hard kill;
the deadline-in-the-pass fix is named for the area that owns that file.

## Practice on this date

- `crontab(5)`: "A '%' character in the command, unless escaped with a backslash (\\), will be
  changed into newline characters" (<https://man7.org/linux/man-pages/man5/crontab.5.html>) —
  which is why a dated redirection in the cron line is not the way to bound the log.
- `launchd.plist(5)`: `StandardOutPath` "specifies that the given path should be mapped to the
  job's stdout(4), and that any writes to the job's stdout(4) will go to the given file" — no
  rotation, no size. `ExitTimeOut` is "The amount of time `launchd` waits between sending the
  SIGTERM signal and before sending a SIGKILL signal when the job is to be stopped", which
  bounds the stop, not the run. (Fetched today from
  <https://keith.github.io/xcode-man-pages/launchd.plist.5.html>.)

Files: `scripts/maintenance_helpers.py`, `docs/USER-GUIDE.md`,
`tests/test_the_scheduler_log_is_bounded.py`,
`docs/research/2026-09-17-the-scheduler-log-is-bounded-and-the-hard-kill-is-named.md`.
