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

## Structural reference retained from the overview

### Offline fitting and answer representation owners

Existing paths remain: [offline lane fitting](fit-lane-score.md) reads saved
results and depends on lane_score; [answer budget](answer-budget.md) owns
answer shaping and estimates; [answer cost](answer-cost.md) depends on that
estimate and owns optional telemetry. Pure tests move unchanged; real recorder,
reader and MCP integration assertions remain. The test import guard gains explicit
benchmark roots without changing its scripts-only default. No production byte,
public command, threshold, environment contract, provider call or runtime path changes.
