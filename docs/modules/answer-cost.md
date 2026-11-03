# Answer cost telemetry

Owns `scripts/answer_cost.py` at its existing path. Builds the optional existing
answer_cost block from the shared estimate, supplied elapsed/budget time and the
caller's component states. Missing estimates remain missing, existing stage labels
and reasons are reused, and the auto/always/never environment contract is unchanged.
The self-cost gate keeps its measured threshold; telemetry is not provider billing.

It imports the full answer_budget implementation, never the MCP executor. Pure block
tests move with unchanged assertions and helpers; real MCP wiring, envelope schema
and telemetry-failure fallback stay in their original integration file. No new
OpenTelemetry implementation or claim of current-schema conformance is introduced.

Qualification: [research](../research/2026-11-03-answer-budget-cost-owner-qualification.md).
Run pure and real MCP tests, Python 3.10, actual complexity and complete context
reconstruction. Production bytes and all public imports remain unchanged.

Shared canonical contract: [Answer budget](answer-budget.md).
