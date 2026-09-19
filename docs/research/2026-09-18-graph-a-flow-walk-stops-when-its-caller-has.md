# A flow walk stops when its caller has

Date: 2026-09-18. Audit 3, B3 (the `code_graph` half; the `mcp_server` half
belongs to the navigation area, which asked for this).

Files: `scripts/code_graph.py`,
`tests/test_a_flow_walk_stops_when_its_caller_has.py`

## What was found

`scripts/mcp_server.py`'s `_data_flow_architecture_call` and
`_cross_service_architecture_call` both begin with `del deadline` and then call
`code_graph.find_argument_flows` / `code_graph.find_service_paths`, because
neither function could be given one. Both walks are bounded by seeds
(`FLOW_MAX_SEEDS = 20`), rows (`FLOW_MAX_ROWS = 1000`) and depth
(`FLOW_MAX_DEPTH = 8`) — and by nothing in time. After the MCP caller's
deadline has passed the worker thread keeps walking and keeps one of only four
worker slots, which over HTTP every agent shares.

Every other bounded reader in this module already takes the pair: the
generation opener is `_active_evidence_graph(directory, *, read_only, deadline,
cancelled)` and `_check_generation_stop(deadline, cancelled)` raises
`TimeoutError` on an expired deadline or a live cancellation.

## Sources

- In-repository contract, `scripts/code_graph.py:1003-1015`:
  `_require_generation_deadline` raises `TimeoutError("generation catalog
  deadline reached")` and `_check_generation_stop` raises
  `TimeoutError("generation catalog operation cancelled")`. This is the shape
  every deadline-aware path in the slice already uses, and the audit's "checked
  and found clean" section names it: "deadline/cancel plumbing in
  `_generation_catalog` and `_leased_active_graph`, `TimeoutError` re-raised,
  `graph.close()` in `finally`".
- The audit's own finding B3: "`data_flow` / `cross_service` `del deadline`
  (5389, 5403); bounded by seeds/depth but not time, so a slow graph keeps one
  of 4 worker slots after the caller timed out."
- Python documentation,
  [`time.monotonic`](https://docs.python.org/3/library/time.html#time.monotonic)
  (fetched 2026-09-18): "Return the value (in fractional seconds) of a monotonic
  clock, i.e. a clock that cannot go backwards. The clock is not affected by
  system clock updates. The reference point of the returned value is undefined,
  so that only the difference between the results of two calls is valid." This
  is the clock the deadline is measured on — `_require_generation_deadline`
  compares against `time.monotonic()` — so a deadline crossing a walk is safe
  against a system-clock change mid-query, and the parameter must carry a
  monotonic value, never a wall-clock timestamp.

## Decision

`find_argument_flows` and `find_service_paths` grow two optional keyword
parameters, `deadline: float | None = None` and `cancelled=None`, defaulting to
None so every existing caller and test is unaffected. They are forwarded to
`_active_evidence_graph`, which already accepts them, and checked once per hop
inside `_flow_rows` and `_service_walk` — the natural granularity, because a
hop is one bounded graph query plus at most `FLOW_MAX_ROWS` node reads, and a
walk is at most `FLOW_MAX_DEPTH` hops. An expired deadline therefore raises
`TimeoutError` out of the walk instead of running it to completion, and
`graph.close()` still runs in the `finally` that is already there.

The MCP side (removing `del deadline` and passing the operation's deadline and
cancellation token) belongs to `scripts/mcp_server.py` and is made there.
