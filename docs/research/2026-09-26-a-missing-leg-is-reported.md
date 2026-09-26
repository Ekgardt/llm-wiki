# A missing leg is reported

Date: 2026-09-26. Audit 2026-09-26, finding C-10 (a GRAPH recall whose dense leg
is missing does not report it).

## What was wrong

The planner runs a question about relations as `GRAPH` and asks for lexical,
dense and graph (`retrieval.planned_request`). The envelope's freshness
components were built from the profile's declared signals
(`PROFILE_SIGNALS["GRAPH"] = ("lexical", "graph")`) plus the signals that ran. A
dense leg that was asked for and did not run was in neither, so no `dense`
component appeared and nothing said it was missing.

## Decision

- `RetrievalTrace` carries `signals_requested`, the signals the run asked for;
  the retrieval-trace schema gains the optional property, and the envelope's trace
  passes it through.
- `mcp_server._requested_signals` reads it first and falls back to the profile's
  declaration for a trace that predates it.
- Guard: a test fails when any `RetrievalTrace` field is missing from the trace
  schema or from the envelope's trace, so a field cannot be lost between layers.

## Source

Google, Site Reliability Engineering, "Addressing Cascading Failures", fetched
2026-09-26 from https://sre.google/sre-book/addressing-cascading-failures/:

- "Monitor and alert when too many servers enter these modes."
- "Graceful degradation shouldn't trigger very often—usually in cases of a
  capacity planning failure or unexpected load shift."

Conclusion (mine): a degraded answer can only be monitored if it says it is
degraded; an answer missing a leg it asked for must name that leg.

## Files

- `scripts/retrieval.py`
- `scripts/mcp_server.py`
- `scripts/schemas/retrieval-trace-v1.json`
- `tests/test_a_missing_leg_is_reported.py`
- `tests/test_mcp_server.py` (the trace key set gains `signals_requested`)
