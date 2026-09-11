# The navigation stand is named steps

Date: 2026-09-11. Trigger: audit finding L13, `benchmark/run_code_navigation.py`:
28 functions over the gate, led by `run_fixture_benchmark` (CCN 84,
504 lines: gold queries, performance sample, mutations, crash cycles,
ownership and the report in one body), `_evidence_complete` (75: one
seventy-line boolean), `_validate_schema_node` (64: a whole JSON-schema
subset in one function), `_operator_python_files` (22),
`_mutate_and_measure` (19), `_read_operator_source` (17).

## Sources

1. `tests/test_code_navigation_benchmark.py` (4 300 lines): the report
   shape, every error phase/code pair, the gate evaluation, the exact
   performance sample and the operator-corpus probe are asserted through
   fakes; the names the tests reach (`_operator_definition`,
   `_performance_queries`, `_peak_rss`, `_operator_python_files`,
   `_measure_warm_performance_pair`, `_current_citation`, `main`) are kept.
2. Rule 5's remedies: a schema validator becomes one function per
   keyword; a phase of the benchmark becomes one function that returns
   its measurements; a seventy-line conjunction becomes named predicates
   over the report sections.

## Decision

Reports, phases, error codes and gate results unchanged. The run keeps
its state in one `_FixtureRun` object; each phase (gold queries,
performance sample, mutations, crash cycles, ownership) is a method
that appends to the same `errors` list in the same order. Evidence
completeness is a list of named predicates; a `KeyError`, `TypeError`,
`ValueError` or arithmetic error anywhere still reads as incomplete.
Work lands in three commits: validators and small helpers, the runtime
and operator probe, then the run and the gates.

Files: `benchmark/run_code_navigation.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
