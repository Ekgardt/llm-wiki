# Maintenance context must account for test imports

Date: 2026-11-03. Reproduced at cf3709e8 during queue-worker boundary preparation.

The existing source-closure guard walks production Python imports only. Five
packets contain test imports of a consumer missing from the declared context:
sqlite_io→reliable_memory, queue_artifacts→memory_queue,
path_ancestry→bounded_io, runtime_contracts→reliable_memory, and
lsp_credentials→lsp_security. Evidence: logs/module-test-import-gaps.json.
The earlier artifact byte round trips are true, but completeness of these five
test contexts was overstated. This is a measurement defect, not token savings.

Extend the common measurement guard to follow test/support Python imports and
require their local production dependencies with transitive source closure.
Missing local test support must also fail, including relative imports. Current
pytest bootstrap remains explicitly treated as infrastructure: include the full
conftest/plugin sources, without requiring their entire runtime dependency tree
for pure tests that do not use their integration fixtures. No new per-module
exclusion list or relaxed token ceiling. Preserve existing source closure.

Move the mixed alias/pickle assertions intact into integration test files. Pure
boundary assertions stay with owners. The runtime compatibility file already is
an integration test and remains unchanged; it leaves the pure value packet and
remains an explicitly unassigned integration obligation. SQLite's legacy pickle
fixture must remain one shared file; update only its path expression when its test
moves. No assertion, fixture byte, production implementation or integration test
is removed. Test ownership and manifests must expose the separation honestly.

Fresh built-in generation-18e46f9894b7bf8b-d66aa3e9 and source inspection precede
code. Add failing synthetic tests for eager/lazy/transitive product imports from
tests, shared/relative test helpers, package initializers and outside-root links.
Then implement the common guard, relocate affected integration checks, rerun all
owner/global checks and actual artifact reconstruction. The worker transfer waits
until this measurement correction is complete.

## Current sources and alternatives

Three independent publishers checked 2026-11-03:
- pytest plugin documentation describes conftest/plugin discovery; their explicit
  infrastructure treatment is retained, not expanded to arbitrary test files.
  https://docs.pytest.org/en/stable/how-to/writing_plugins.html
- Python's import reference describes absolute/relative package resolution and
  package initialization. Dependency inspection must follow test helpers too.
  https://docs.python.org/3/reference/import.html
- Import Linter documents forbidden/layered/independent import contracts. A
  reproducible common guard addresses the class of omission; handwritten
  per-manifest exceptions would hide it.
  https://import-linter.readthedocs.io/en/stable/contract_types/

Alternatives: include entire consumer trees in leaf packets (exceeds meaningful
owner responsibility and often budget), drop compatibility assertions (unsafe),
or ignore test imports (repeats the defect). Choose explicit integration placement
plus strict source/support closure for the tests actually in each packet.

The stronger recursive check also found two omitted imports in the LSP position
fixture package (__init__.py and api.py). Both original files are now included
whole, without changing their bytes. Ten synthetic regressions failed before
the common fix; 120 dependency/bootstrap/compatibility checks then passed.

Final checks: 120 dependency/bootstrap/alias/pickle tests passed on Python 3.12
and 3.10. Global guards 1451 passed/two skipped, 147.74 seconds. Ruff and actual
CCN/shape passed; the initial CCN=6 in the closure loop was corrected by excluding
bootstrap from its pending set, without weakening a branch or test. All 59 actual
packets round-trip and remain under the ceiling; largest 97671 before this final
evidence. Registry ownership 463/3194, leaving 2731 unassigned, including retained
integration checks. Production files and original fixture bytes are unchanged.
Full 1214d765 continues and predates this correction. Native CI is unconfirmed.
