# Today is not a compile backlog

Date: 2026-09-25. Audit item C-24 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `mcp_server._compile_backlog` counted every `knowledge/daily/YYYY-MM-DD.md` whose hash
  differs from the compiled one. Every capture appends to today's log and the nightly pass
  compiles it, so from the first capture of the day the health resource (and
  `vault_status`) reported "Compile backlog contains 1 daily file(s)" and `partial: true`
  until the night — a warning with nothing to do.
- A capture names its day from `captured_at[:10]` (`session_evidence._capture_day`); which
  clock that stamp uses differs by writer, so "today" can be the local or the UTC date near
  midnight.

## Source

- Prometheus, "Alerting", https://prometheus.io/docs/practices/alerting/ (fetched
  2026-09-25): "keep alerting simple, alert on symptoms, have good consoles to allow
  pinpointing causes, and avoid having pages where there is nothing to do."

## Decision

- The backlog counts only closed days: today's log, by the local and by the UTC date, is
  left out. A past day that changed after its compile still counts, so a missed nightly is
  still visible the next morning.

## Files

- `scripts/mcp_server.py`
- `tests/test_today_is_not_a_compile_backlog.py`
- `CHANGELOG.md`
