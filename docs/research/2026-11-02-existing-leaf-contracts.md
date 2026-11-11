# Existing pure boundaries are qualified in place

2026-11-02, approved completion mandate. Source baseline f187e32c.
No production source moves or changes: python_parse, calendar_pass, iso_time,
reply_json, page_status and code_intelligence already have stdlib-only boundaries.
They represent safe Python parsing, calendar arithmetic, timestamp normalization,
model reply parsing, page lifecycle vocabulary and navigation value types.

Built-in graph queries resolved parse_python, computed_gaps, parse_instant,
reply_document, is_retired and PositionRange against the current f187e32c index.
Reverse consumers include repository indexing, grounded answers, contradiction
processing, corpus/search filters and navigation. Direct source inspection confirms
no project imports. This is a static finding, supplemented by isolated subprocess
imports and existing integration qualification. Evidence:
logs/module-bootstrap-and-leaves-graph.json and
logs/decomposition-leaf-test-map.json. The graph explicitly remains incomplete.

Owned tests stay at their existing path when already isolated (python parsing and
reply_json). Pure assertions are moved without changing their bodies out of mixed
calendar/ISO/page-status files; integration scenarios remain at their old paths.
The code-intelligence test file contains only PositionRange tests; unused old
corpus imports/constants can be removed after confirming no external users.
No test, parameter or fixture behavior is dropped. New tests/modules/<name>/
directories carry boundary/budget guards and the moved pure tests, with explicit
__init__.py to preserve joint pytest collection. Shared test-only import guards
live in tests/module_boundaries.py and are counted in every consuming packet.

Each module has its own guide/context manifest. Every own test, its helpers and
relevant historical rationale is included alongside required instructions and
pytest bootstrap. Additional integration qualification is reported separately,
never hidden as an owned module test with omitted dependencies.

Research: the completion plan's independently verified Fowler bounded-context,
Import Linter contract and PEP 544 sources apply. A new package layer would add
no needed boundary to these already independent functions/types. Preserve paths,
imports, API, errors, time semantics and existing technical limits. No dependency
or Python-support change; existing behavior is not automatically certified as
universally correct by this qualification.

## Results

Before separation: 41 passed. Afterwards: 67 passed, comprising the same 41
assertion scenarios plus 12 isolation checks, six budget checks and eight import
checker regressions. All 22 moved/retained test/helper function ASTs match their
original bodies and decorators. No production source changed. Python 3.10:
61 passed (the same selection without six tokenizer-budget tests, whose tokenizer
is installed in the main qualification environment). Joint module/guard run:
744 passed, 2 skipped. Ruff/diff checks and all 22 packet measurements passed.
Evidence: logs/leaf-contracts-{baseline.txt,related.txt,python310.txt,weights.txt},
logs/leaf-contract-test-preservation.json,
logs/module-context-and-leaves-final.txt. Native Windows/macOS are not claimed.
Current serialized counts are in logs/module-context-complete-budgets.json;
final tests emit the count including this report's current text.
