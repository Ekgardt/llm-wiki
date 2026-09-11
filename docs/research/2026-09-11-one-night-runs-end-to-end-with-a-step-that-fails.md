# One night runs end to end with a step that fails

Date: 2026-09-11. Trigger: the open half of audit finding OPS-17. Every
nightly test replaced `_run_steps`, the compile waits or `_nightly_steps`
itself, so no test ran `_run_nightly_body` with the real step runner
against a real child process. The report format of a failing step —
the stderr head on the report line, the artifact that keeps the full
output, the `failures=` line and the recorded state — was asserted only
in unit tests of `maintenance_helpers.run_step` with a fake logger, never
through the pass that writes the report a person reads in the morning.

## Sources

1. `maintenance_helpers.run_step` and `_log_step_output`: a non-zero exit
   puts the redacted stderr head on the report line and always names the
   owner-only artifact under `logs/maintenance/`.
2. `scheduled_nightly._run_nightly_body`: the terminal result is recorded
   through `update_state` in `finally`, so a pass that returns 1 must leave
   `last_nightly_status = failed` with the failure count.
3. The fence tests of 2026-09-10 already run `_run_nightly_body` with a
   fake runner; the state and report redirection they use is the pattern.

## Decision

One test in `tests/test_scheduled_nightly.py` runs `_run_nightly_body`
with every subprocess step pointed at one small script through `_script`,
where one step (`lint_memory.py`) writes to stderr and exits 3 and the
rest exit 0. The generation refresh, the health report, the code update
and telemetry compaction are stubbed: they act on the checkout, not on the
step runner under test. The test asserts the report line with the stderr
head, the artifact line and file, the `failures=1` line and the recorded
state. Nothing in the product changes.

Files: `tests/test_scheduled_nightly.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-operations-and-reliability.md`.
