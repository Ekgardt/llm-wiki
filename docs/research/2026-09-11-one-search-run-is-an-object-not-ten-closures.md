# One search run is an object, not ten closures

Date: 2026-09-11. Trigger: the open half of audit finding L8.
`retrieval.retrieve_via_search_memory` is 290 lines: eleven closures over
twenty-odd locals and four `nonlocal` variables (`use_generation`,
`corpus_generation`, `generation_fallback`, `legacy_fallback`). lizard
scores each closure separately, so the gate passes, but the function is
read as one unit and every closure's behaviour depends on state that is
assigned in three other closures.

## Sources

1. Rule 5's remedy for complex logic: a pipeline of simple steps; a step
   that needs shared state takes it by name, not by capture.
2. Python's `nonlocal` assignments inside nested functions are the one
   place a reader cannot tell which function last wrote a variable; a
   dataclass field written by a method is greppable.
3. The audit's own fix direction: lift the closures to module functions
   taking a small context object.

## Decision

A private dataclass `_SearchRun` holds the arguments of one call and the
four pieces of state the backends share; every former closure is a method
with the same name and body. `retrieve_via_search_memory` keeps its
signature and docstring and becomes the pipeline: resolve the encoder,
analyse the query, build the run, open the generation, run under the seal,
report. Behaviour and the trace are unchanged; the existing retrieval,
fallback, seal and telemetry tests are the regression suite.

Files: `scripts/retrieval.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
