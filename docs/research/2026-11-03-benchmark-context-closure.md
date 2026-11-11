# Python context closure must include benchmark code

Date: 2026-11-03. A prerequisite for qualifying benchmark owners under the approved
whole-project decomposition mandate. This strengthens test-only packet validation;
no product import path, runtime behavior, dependency version or deployment changes.

## Reproduction and cause

A temporary packet declared benchmark/owner.py containing `import dependency` but
omitted the real scripts/dependency.py. measure_context accepted it (227 tokens,
10 files). The same omission from scripts/owner.py is already rejected. The cause
is the scripts/tests-only predicate in _context_python, plus those same zones in
initializer/relative resolution and a scripts-only absolute import fallback.
The live product was not involved; logs/benchmark-context-omission-reproduction.json
records the experiment. No existing packet owns a benchmark Python file yet.

## Proposed correction

Inspect every declared contained .py source, irrespective of its top-level folder.
Follow package initializers up to the repository boundary. Relative imports must
stay inside that boundary. For absolute imports conservatively inspect the real
project's launch roots scripts/, tests/, benchmark/, the repository root and the
importing file's parent; when a name has multiple local candidates, include them
all instead of assuming which sys.path wins. These are the current Python source
roots (238 product, 1501 test, 41 benchmark files observed), not a new product quota.
A future added launch root needs an explicit extension and regression; computed
sys.path mutations and dynamically constructed module names are not proven by AST
closure and still require source/runtime investigation. No installed dependency
source is vendored and no bootstrap exemption is widened.

## Current primary sources and alternatives

Three independent publishers checked on this date:
- Python's import machinery, packages, relative imports and path-based lookup:
  https://docs.python.org/3/reference/import.html
- pytest's import modes and test path insertion:
  https://docs.pytest.org/en/stable/explanation/pythonpath.html
- Microsoft's Pyright workspace/extra-path and relative import resolution:
  https://github.com/microsoft/pyright/blob/main/docs/import-resolution.md
The latest documentation is used for the import model, not as permission to upgrade
this Python >=3.10 project or install a new analysis dependency.

A benchmark-only exemption would conceal the missing context. Requiring all product
code in every benchmark packet would conceal the actual boundaries. Executing arbitrary
module imports to discover dependencies would introduce side effects into measurement.
Use conservative static local candidates and keep its limits explicit; this is neither
a complete Python interpreter nor a proof about dynamic imports.

Before implementation: fresh built-in graph, a regression that fails on the reproduced
omission, and related lazy/relative/package/sibling/ambiguous-name checks. Preserve
all earlier scripts/tests containment and closure tests. Requalify all packets,
actual CCN <=5, Python 3.10 and exact serialized-byte reconstruction. If the stronger
gate reveals missing sources, include or properly isolate them; do not waive it.

## Qualification evidence

Fresh built-in generation generation-18e478215c000e0c-99dbed94 binds 7bf0a2ec;
graph callers confirm the shared predicate, absolute resolver and closure walk.
Source inspection supplements the partial graph. The expanded suite reproduced
15 missed dependency/containment cases before the fix, with 32 existing/control
cases passing. After the shared correction, 292 closure/bootstrap/contract/tooling
checks pass on both Python 3.12 and 3.10. The first Python 3.10 run hit the unchanged
ownership guard because the new research file had not been staged; after staging
that declared file, the same selection passed. No ownership check was weakened.

All changed functions pass actual CCN <=5 and shape checks. All 74 serialized
packets reconstruct original paths and bytes under the stronger closure; largest
formats packet 96863 tokens before this evidence paragraph. No existing packet
needed an exception or omitted dependency. Ownership: 609/3315 tracked paths,
2706 unassigned. This is validation of packet completeness for static imports,
not a promise about computed imports or a full-cycle LLM efficiency result.

Global module, closure, ownership, documentation, structure, privacy and complexity
gates: 1828 passed, 2 skipped. No production source changed in this correction.

## Follow-up: complete owner guides outside scripts (2026-11-03)

At d907b9f3, _required_guides returns an empty set for the owned
benchmark/fit_lane_score.py because _source_owners filters to scripts/. The
actual fitter manifest has its guide, but removing it would evade this guard.
This is distinct from the earlier corrected Python implementation closure.
Reproduction: logs/benchmark-guide-omission-reproduction.json.

Use the explicit ownership registry for every owned context entry, not a directory
or filename-extension guess. Each included owned source, test helper, fixture,
resource, guide or historical document requires its owner's complete guide and
recursive shared contracts. Unowned entries remain visible in the separate
whole-project completeness report; this fix does not invent an owner. This also
protects non-Python artifacts and avoids repairing benchmark alone while leaving
integrations/root/test support with the same omission. No source/bootstrap or
measurement exception changes.

Preflight of the stronger ownership rule found five missing guide references in
four existing packets: temporal-anchor needs evidence-sufficiency and lane-score,
evidence-sufficiency needs lane-score, lsp-paths needs verified-artifacts, and
context-tooling needs formats. These arise from shared owned historical/support
material and are to be added in full. Existing files remain declared. Evidence:
logs/all-owned-guide-preflight.json. Measure resulting complete packets rather
than exempting these shared artifacts to keep old counts.

The already checked primary Python import and pytest sources still govern source
and test loading; the current Diataxis/Google/Microsoft documentation review in
module-contract-documentation supplies the independent reference-organization
basis. Full ownership determines the guide, not the incidental source directory.
Add parameterized regressions for benchmark, integrations, root source, test
helpers and non-Python data/docs, including an unowned-entry control. Confirm the
original guard fails those cases before replacing its scripts-only filter.

Selected logical records have their own ownership registry, so complete entry
coverage must also include the names returned by the existing validated record
selector. Merge the whole-file and record owner maps for guide lookup; keep
selection validation in its current single implementation. A record's catalog
owner is not a substitute for the selected record's semantic owner. Current
record ownership adds no missing guide beyond the five preflight references.

Guide correction qualification: fresh built-in index d907b9f3 and generation
generation-18e47b776d6fc570-da88eedd; relevant graph edges were checked and
supplemented with source. Seven path/type regressions failed under the original
filter; the selected-record extension also failed because that input was absent
(8 failed, 164 passed). With registry lookup enabled, the four real manifests
then failed precisely for their missing guides (4 failed, 168 passed). Adding
the five whole guides produced 172 passing contract checks. No existing assertion
or context file was removed.

Focused guide, source-dependency, ownership, record and tooling checks passed
260 tests on Python 3.12 and 260 on Python 3.10. Actual CCN/shape and Ruff passed.
The broad module/structure/policy selection passed 1965 tests with 2 skips. All
79 actual bundles reconstruct every path and byte; largest is 98221 tokens.
This strengthens detection of missing context, not a claimed cost reduction.
Evidence: logs/owner-guide-completeness-{red,manifests-red,green,py310,global}.txt,
owner-guide-completeness-graph.json and context-bundle-comparison.json.
Whole-project ownership, native platform CI and measured full-cycle LLM benefit
remain unfinished. The c43455da full regression is still running separately.
