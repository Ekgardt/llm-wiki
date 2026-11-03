# Answer shaping and budget

Owns `scripts/answer_budget.py` at its existing path. The standard-library-only
owner estimates serialized answer bytes, removes only designated opaque/recoverable
information, compacts profitable tables, and applies the existing reduction ladder.
Keep protected citation and graph-honesty fields, explicit omission reports, row
counts and named too-small refusal. The estimator is UTF-8 bytes/2 rounded up;
it is neither exact tokenizer usage nor billing. Existing bounds are unchanged.

Tree traversal preserves shared acyclic children without mutating inputs and refuses
cycles. Table constants are type-strict, prefixes and column headers apply only when
the complete representation is cheaper. Preserve traversal and floating-point/JSON
operation order; no formatting or representation redesign occurs in qualification.

Pure tests move exactly from the mixed MCP file. Its integration tests retain their
assertions and import the single shared test table decoder; deep-tree and prefix
regressions are owned in place. Full historical rationale travels with the packet;
its dated measurements are not new performance results.

Qualification: [research](../research/2026-11-03-answer-budget-cost-owner-qualification.md).
Run restoration/cycle/budget and actual MCP consumer checks, Python 3.14, complexity
and full maintenance-packet reconstruction. Runtime estimation and maintenance
packet tokenization remain separate contracts.

## Structural reference retained from the overview

### Offline fitting and answer representation owners

Existing paths remain: [offline lane fitting](fit-lane-score.md) reads saved
results and depends on lane_score; [answer budget](answer-budget.md) owns
answer shaping and estimates; [answer cost](answer-cost.md) depends on that
estimate and owns optional telemetry. Pure tests move unchanged; real recorder,
reader and MCP integration assertions remain. The test import guard gains explicit
benchmark roots without changing its scripts-only default. No production byte,
public command, threshold, environment contract, provider call or runtime path changes.
