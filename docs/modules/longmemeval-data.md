# LongMemEval question preparation

Owns benchmark/longmemeval_data.py unchanged at its public path. Loading local
JSON, category assignment, deterministic sampling, evidence labels, twin creation
and tune/decide assignment share the question shape and belong together. No model,
retriever, worker or network operation is imported or started. The cache path and
missing-dataset refusal remain unchanged; there is no synthetic dataset fallback.

Preserve the shallow nonempty-list/first-record shape check, suffix rules, LoCoMo
reference matching, positional alignment, existing aliasing/copying behavior and
error propagation. The fixed conversation split, SHA256 parity, sorted strata,
largest remainder allocation and seeded Random selection remain as implemented.
This is compatibility qualification, not a new statistical or malicious-input
validation claim. No new bounds, download or benchmark run are introduced.

Twelve pure tests move unchanged: five loading/sampling, five twins/split and two
label cases. Twin and LoCoMo synthetic fixtures have one owner and retained
integrations import them. Sampling helpers stay with their tests. The remaining
17 benchmark, four twin and two label integration tests stay in their original
files. All original function and assertion ASTs are preserved; no test is dropped.

Run tests/modules/longmemeval_data, tests/test_longmemeval_benchmark.py,
tests/test_a_twin_question_expects_silence.py and
tests/test_a_locomo_question_carries_its_evidence_labels.py. Complete source,
owner tests, fixtures, instructions and full August28/September22 historical
records are included in the <=100000 o200k_base packet, with exact reconstruction.
These multisubsystem records are shared context and retain dated claims.

## Qualification research — 2026-11-03

The fresh built-in source index at8628c69e, get_architecture, callers and complete
source search identify runner, scorer, calibration and vault consumers. The next
commit0e064e11 only records CI evidence. All35baseline tests pass. Keep the existing
cohesive preparation module; combining it with runner execution would enlarge the
dependency surface, while splitting each operation adds no demonstrated benefit.
Python3.14 and all public contracts are unchanged.

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[JSON semantics](https://docs.python.org/3.14/library/json.html).
The sources justify explicit responsibilities and interchange behavior, not the
historical dataset-version claims or fresh benchmark/token-efficiency results.

## Canonical structure contract — 2026-11-03

[LongMemEval data](longmemeval-data.md) owns the existing local loader,
stratified selection, evidence labels, twins and tune/decide assignment. Runner,
scorer and coverage integrations remain separate. Dataset paths, sample selection,
source bytes and original assertions are preserved; no dataset is downloaded.
