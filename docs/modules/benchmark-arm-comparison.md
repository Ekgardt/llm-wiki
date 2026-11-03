# Benchmark arm comparison

Owns benchmark/compare_arms.py at its existing path, byte-identical. It reads local
reports, summarizes supplied numeric category metrics and computes the existing
win/loss/no-difference-measured decision. Mean differences must exceed the baseline
spread; a category loss makes the CLI return one. The CLI optionally writes JSON.
The existing three-run warning remains a warning, not an added execution quota.

Metric defaults, category ordering, rounding, handling of missing measurements,
zero spread and accepted Python numeric values are preserved. The module does not
run a benchmark, validate a dataset, call a judge, establish statistical significance
or measure actual model usage. Assigning ownership does not make historical claims
current. The complete August 31 decision record retains its dates and limitations.

Eleven original comparison tests and their three helpers move with unchanged ASTs
to tests/modules/benchmark_arm_comparison/test_contract.py. All three independent
LongMemEval answer-cost tests remain in tests/test_the_decision_rule_is_computed.py;
they are not discarded or claimed by this owner. Source, tests, fixtures, instructions
and history must fit 100000 o200k_base tokens and reconstruct every byte. New import
guards reject product executors in both scripts and benchmark search roots.

Run tests/modules/benchmark_arm_comparison and the retained original test file with
normal pytest bootstrap. No production path, public command, dependency, runtime
root or installation changes. Python 3.14 is the approved runtime.

## Qualification research — 2026-11-03

A fresh built-in graph at 6e23c42e shows comparison consumers; name-ambiguous verdict
edges are checked against the complete sources. Baseline comparison and parity
suites pass 22 tests. Keep the existing cohesive comparison separate from execution
and unrelated report formats; a protocol framework or arbitrary function split
would not improve this boundary.

Primary sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[Python JSON behavior](https://docs.python.org/3.14/library/json.html),
[PEP 544](https://peps.python.org/pep-0544/). These support explicit dependencies;
no new interface abstraction or statistical rule is introduced by this work.
