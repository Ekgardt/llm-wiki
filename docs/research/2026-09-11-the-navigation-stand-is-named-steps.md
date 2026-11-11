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

## 2026-10-06: authored citation validation before compilation publication

Installed HEAD 974ab62a binds each evidence entry, but the renderer also copies
model-authored titles, summaries, body text, claims and related links. A complete
reference in the evidence section did not prevent a shortened reference elsewhere
in the page. The isolated regression reproduced five incorrect acceptances while
keeping two positive controls. Existing strict `extract_evidence_references`
checks the newly authored strings before normalized-plan acceptance. Every parsed
reference must additionally belong to this operation's already verified bindings.
Old target text is neither edited nor granted new authority. This prevents fresh
malformed pages; it does not repair the three already stored pages, including an
immutable decision. The normalization cache identity changes; no source format,
path, provider contract, resource limit or persistent authority is added.

Research verified 2026-10-06:
- W3C PROV-DM: https://www.w3.org/TR/prov-dm/ — provenance links identify actual entities and derivations.
- OWASP Input Validation: https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html — both syntactic and semantic validation are required.
- OpenAI Structured Outputs: https://developers.openai.com/api/docs/guides/structured-outputs — a schema does not establish semantic correctness; this is not a claim that Codex CLI implements API grammar guarantees.

Prompt-only correction would not enforce the invariant. Renderer-only syntax
checking would accept a complete unrelated reference. Automatic shortening or
replacement would invent provenance. Reusing the existing parser plus exact
operation-owned bindings is the smallest verified change. A new prose citation
requires its corresponding evidence entry; existing target prose is preserved.

Qualification: five genuine failures and two positive controls before the fix;
seven new tests and the complete transaction suite pass (90 total). Actual Lizard
and AST analysis of all nine changed/new callables measured CCN at most four,
no more than two if statements and at most two control-flow nesting levels.
The broader regression and installed validation remain separate requirements.
