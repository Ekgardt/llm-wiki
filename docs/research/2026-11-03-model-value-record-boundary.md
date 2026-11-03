# Provider value records before transport

Date: 2026-11-03. Internal refinement under the approved decomposition mandate.
The retained-answer verifier in memory_queue imports compile_controller, whose
value-type import loads llm_client and the DLP/provider transport dependencies.
The first prerequisite is a real downward data boundary, not an import exception.
Move exactly ProviderDescriptor, LLMResult, BackendResponse, _CodexExecutable and
CodexPlanningBasis to scripts/llm_wiki/model_records.py. The basis and executable
records belong together with the descriptor because their annotations reference
each other; leaving an annotation-only back import would preserve the cycle.
_PreparedCodex and every selection, transport, DLP and executable verification
operation stay in llm_client. No schema or default changes.

Keep direct llm_client aliases for all five actual public/internal consumers and
the historical __module__ identity for persisted pickle compatibility. They can
be removed only after those imports and retained serialized objects no longer
require them; replacing them with duplicate classes would break isinstance and
dataclass equality. The data module imports only context_budget and canonical
formats plus the standard library. Its frozen dataclasses do not imply deep
immutability of supplied mappings. This transfer does not add new validation.

Primary sources checked 2026-11-03, three independent publishers:
- Python 3.10 dataclasses: field order, factories, frozen and exact class equality.
  https://docs.python.org/3.10/library/dataclasses.html
- pytest monkeypatch: bindings must be checked where consumers access them.
  https://docs.pytest.org/en/stable/how-to/monkeypatch.html
- Cockburn's original ports/adapters article: independently testable application
  responsibilities and tests against boundary leaks.
  https://alistair.cockburn.us/hexagonal-architecture
These are established semantics and principles, not a newly measured performance
claim. Python 3.10 is upstream unsupported but remains this project's required
compatibility target; no dependency, SDK, provider version or support policy changes.

Alternatives: a Protocol cannot replace the existing nominal dataclass/isinstance
contract; dict conversion changes semantics; importing llm_client only under
TYPE_CHECKING still retains a conceptual reverse dependency and breaks complete
runtime hint resolution. Moving the five connected value records preserves their
ASTs and lets future answer-codec work depend only on data.

Before implementation: fresh built-in index and graph, source and consumers,
unchanged baseline, original pickle/field representation fixtures, missing-module
regression. Verify exact class ASTs and all retained top-level definitions, fields,
canonical output, default factories, frozen assignments, hint resolution, legacy
alias identity and original pickle bytes. Keep real provider/DLP/compile recovery
consumer tests unchanged. Run actual CCN/shape, Python 3.12/3.10, full module gates,
and exact packet reconstruction <=100000 tokens. Native platform evidence remains
separate. Do not claim this prerequisite removes every queue/controller cycle.

## Recorded qualification

The fresh built-in index names db6baf57 and generation
generation-18e4796d321a2737-f5497f8b. The incomplete graph reports no callers for
the selected class names; source searches independently identify client, compile,
model chooser and tier consumers, so zero graph callers are not claimed as absence.
Baseline: 238 provider/compile tests plus 51 planning/refresh tests passed.
The first new budget test used a nonexistent helper and failed collection; this
authoring error was corrected before the intended red proof. Then all three
missing-module/manifest boundary checks failed as expected. Initial green: 24 tests.
Five moved class ASTs and all 250 retained client definitions match their originals.
Five original synthetic pickle objects, reprs, signatures and field lists are
retained in the fixture; they contain no operator data or credentials. Pickle is
used only by the compatibility test, never introduced as a new runtime format.
Python's original import-name requirement was checked at
https://docs.python.org/3.10/library/pickle.html.

Actual CCN and shape analysis passed for moved methods and every new test/helper;
Ruff passed. All 78 packets reconstruct every original path and byte and stay below
100000 tokens; formats is currently the largest at 97095. The global module,
ownership, structure and policy selection passed 1917 tests with 2 skips.
Ownership remains incomplete: 652 of 3346 files are owned, 2694 unassigned.
Sources: private logs/model-records-{baseline,basis-baseline,boundary-red,global}.txt,
model-records-transfer-proof.json, model-records-graph.json and
context-bundle-comparison.json. Consumer runs on both Python versions are tracked
separately; full regression db6baf57 predates this production transfer.

The complete related selection passed 313 tests on Python 3.12 (297.84 s),
including unchanged provider/DLP tests, compile restart/overlap/delivery checks,
planning identity and configuration refresh, plus new value and pickle tests.

The same complete selection passed 313 tests on Python 3.10 (433.76 s);
no assertion was relaxed. Logs: model-records-green.txt and model-records-py310.txt.
