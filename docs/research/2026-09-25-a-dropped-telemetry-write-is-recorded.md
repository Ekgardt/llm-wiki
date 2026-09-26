# A dropped telemetry write is recorded

Date: 2026-09-25. Audit item C-23 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `retrieval_telemetry.best_effort_record_events` caught every exception and returned
  `False`. None of its five callers (`retrieval`, `query_memory` twice, `access_tracking`,
  `mcp_server` three times through one helper) reads the return value. The MCP server's
  `except` around the call (`_count_dropped_telemetry`, audit OPS-21) therefore never saw a
  write failure: it only ever caught event-construction errors.
- `capture_diagnostics.record_capture_failure` already classifies a busy database as
  `deferred` and any other error as `lost`, never raises, and is what doctor and the session
  start read.

## Source

- PEP 20, https://peps.python.org/pep-0020/ (fetched 2026-09-25): "Errors should never pass
  silently." / "Unless explicitly silenced." Telemetry stays best effort — a failed write
  never fails the answer — but it is recorded rather than silenced.

## Decision

- The best-effort writer records a failed write itself, as kind `telemetry_event`, through
  `record_capture_failure`. Every caller is covered by the one place.

## Uncertainty

- How often the live telemetry write fails is unknown: nothing recorded it until now.

## Files

- `scripts/retrieval_telemetry.py`
- `tests/test_a_dropped_telemetry_write_is_recorded.py`
- `CHANGELOG.md`
