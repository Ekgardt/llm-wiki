# A tool failure is not a lost capture

Date: 2026-09-25. Audit item B-25 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and on the live vault, read-only)

- `mcp_server._record_tool_failure` records every failed MCP tool call through
  `capture_diagnostics.record_capture_failure` as kind `mcp_tool`, and `capture_failure_totals`
  counted every kind. The live session start said "53 capture(s) lost (mcp_tool 53)" and
  doctor's `capture` check was `degraded`, while no capture was lost: the entries are
  `ValueError: evidence reference is not canonical` from tool calls.
- The C-23 fix of today records failed telemetry writes the same way (`telemetry_event`), so
  it would have added to the same false count: one class of defect.

## Source

- Prometheus, "Metric and label naming", https://prometheus.io/docs/practices/naming/ (fetched
  2026-09-25): "Either the `sum()` or the `avg()` over all dimensions of a given metric should be
  meaningful (though not necessarily useful). If it is not meaningful, split the data up into
  multiple metrics." A sum of lost captures and failed tool calls is not a meaningful number.

## Decision

- `capture_diagnostics.OPERATIONAL_KINDS = {"mcp_tool", "telemetry_event"}`: they stay in the
  one bounded, redacted trail and counter map, but capture totals, the capture line and the
  seven-day live window read capture kinds only.
- Doctor gets a `tools` check: `degraded` when a tool call or telemetry write failed in the last
  seven days, `ok` otherwise. `capture_diagnostics.py` prints them apart; `--clear` retires both.

## Files

- `scripts/capture_diagnostics.py`
- `scripts/doctor.py`
- `tests/test_a_tool_failure_is_not_a_lost_capture.py`
- `tests/test_capture_diagnostics.py`
- `tests/test_doctor.py`
- `CHANGELOG.md`
