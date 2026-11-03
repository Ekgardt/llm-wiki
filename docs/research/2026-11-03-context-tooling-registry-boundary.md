# Tooling unit tests and whole-repository registry checks

Date: 2026-11-03. Internal refinement under the approved completion mandate.

The context-tooling packet still owns tests that enumerate every module manifest
or boundary source in the checkout, then read files absent from its own manifest.
The previous leaf-boundary correction did not remove this separate tooling-level
input dependency. Round-trip and token counts alone do not prove isolation.

Move only the real-repository assertions from test_module_context_bootstrap,
test_module_context_dependencies, test_module_contract_docs,
test_module_ownership and test_module_context_reads to
 tests/test_module_registry_integration.py. Retain every assertion and exact test
body, the ownership fixture, and shared helper implementations in their existing
files. The integration file imports the existing pure helpers; no duplicate logic.
Remove unused global manifest discovery from the pure test files. Their synthetic
unit cases and declared-source import guards stay in context-tooling.

The new integration file remains a visible unassigned whole-project obligation;
it is not a newly qualified <=100000-token packet. It still runs in ordinary full
pytest and explicit qualification. Do not count this relocation as project
completion or full-cycle token savings. Preserve original global test count and
add a regression against direct registry globbing in the pure tooling test files.
That AST check is intentionally narrow; it does not prove arbitrary IO/dataflow.

Alternatives: include every inspected source in the tooling packet (defeats the
100000 limit), suppress missing inputs (incorrect), or remove global assertions
(loses coverage). Separate the actual scopes and retain the full integration work.

Primary sources checked today:
- https://bazel.build/basics/dependencies — internal data/artifact dependencies
  matter independently of source imports; module granularity is a tradeoff.
- https://docs.pytest.org/en/stable/explanation/goodpractices.html — test import
  layout and collection are part of qualification, not incidental filenames.
- https://docs.python.org/3/library/pathlib.html#pathlib.Path.glob — glob enumerates
  a filesystem tree; its result depends on files outside a selected packet.

Before changing tests: fresh builtin index/callers and direct source reads; save
original ASTs and assertion counts. Run an original-failure regression, exact
transfer proof, all affected unit/integration cases on 3.12/3.10, actual CCN/shape,
ownership/guide/bootstrap gates and all serialized packet round trips.
No production implementation, format, limit or installation changes.
