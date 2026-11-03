# Classification benchmark scoring

Owns benchmark/flush_classification_metrics.py and the existing nine-case public
flush-classification-v1.json fixture. CaseOutcome, confirmed-label selection,
per-case grading, metric aggregation and gate evaluation form one pure scoring
boundary. It imports the same classification_protocol leaf used by capture.
Provider adapters, schema/file IO, run orchestration and CLI remain in
run_flush_classification.py. The runner keeps direct aliases and adds its own
benchmark directory to its existing scripts import root for neutral-directory
script and namespace-module invocation.

All executable definitions and constants retain their original AST. Preserve
lowercase tier interpretation, exact case-sensitive marker containment, filtering
of contested labels, six-decimal rates, the existing empty-denominator values,
inclusive threshold comparison and the mandatory nonempty-labelled gate. Empty
input can have nominal rates of 1 while still failing the labelled gate; do not
turn that compatibility rule into a successful empty evaluation. The public
fixture is synthetic test material, not evidence of real model performance.

Old aliases remain for supported callers of run_flush_classification. They may
be removed after those consumers migrate; the runner itself still calls them.
CaseOutcome retains one class identity across aliases; its definition-module
metadata changes naturally, with no repository pickle consumer found. No
replacement implementation, model call, runtime state, schema or limit is added.

Original runner integration tests remain and are run unchanged alongside direct
scoring tests. The full packet retains the shared protocol source and guide,
complete fixture, both input schemas, historical rationale, tests and normal
bootstrap. Assertions cover entire fixture scoring, missing markers, false
promotion, disputed inputs, empty corpora and threshold equality. Import guards,
actual CCN/shape, Ruff and exact reconstruction <=100000 tokens remain required.

## Boundary research — 2026-11-03

The approved decomposition mandate permits a pure scoring owner separate from
provider execution. Keeping it in the runner couples grading to IO; splitting
individual rate helpers would fragment one metric definition. Dataclass fields
and source function bodies move intact, preserving the existing parser binding.

Sources reviewed this session: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[pytest integration](https://docs.pytest.org/en/stable/explanation/goodpractices.html),
and [Python 3.14 dataclasses](https://docs.python.org/3.14/library/dataclasses.html).
They support the boundary, real consumer tests and class identity preservation;
no fresh classifier quality claim follows from this refactor.

## Canonical structure contract — 2026-11-03

The [classification benchmark metrics](flush-classification-metrics.md)
owner grades confirmed cases through the shared protocol and evaluates the
existing gates. Provider adapters, corpus IO and CLI remain with the runner.
