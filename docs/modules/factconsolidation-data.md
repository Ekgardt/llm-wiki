# FactConsolidation parsing and fixture interchange

Owns benchmark/factconsolidation_data.py and the complete existing
benchmark/conflict-resolution-v1.json, both byte-identical at their public paths.
Numbered facts, frame matching, conflict grouping and local fixture interchange
form one responsibility. Resolver execution, model calls and upstream Parquet
acquisition remain with their consumers; this module imports only stdlib.

Preserve every regex frame and its spelling/order, handling of unnumbered and
unmatched lines, stable serial sorting, repeated observations, highest-serial
truth, gold-confirmed comparison, type coercions and version refusal. Do not
silently normalize ties or strengthen validation during a compatibility move.
The existing fixture contains161conflicts; that is preserved data, not a fresh
benchmark result or revalidation of upstream provenance.

Six original parser/version tests move unchanged. Twelve runner/fixture/product
integration tests and their resolver helper stay in tests/test_conflict_resolution_stand.py.
Run that file and tests/modules/factconsolidation_data. Added local tests exercise
fixture construction/serialization and reading the entire shipped fixture without
importing a resolver. Boundary tests reject runtime imports. The full historical
record ../research/2026-08-28-deterministic-freshness-measured.md stays in context;
its dated empirical and deployment statements are not rewritten as current facts.

The complete source, fixture, own tests, history, bootstrap and instructions must
fit <=100000 o200k_base tokens and reconstruct exactly. No source omission,
provider execution, new dependency or live installation is part of this owner.

## Qualification research — 2026-11-03

Fresh built-in indexc2dd7466, get_architecture, callers and full source/test reads
identify the builder, resolver CLI and test consumers. Baseline18conflict tests
pass (the combined baseline also has5ledger tests). A cohesive preparation owner
keeps numerical/data semantics together; merging execution couples it to providers,
while one owner per frame would fragment a single grammar.

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[JSON semantics](https://docs.python.org/3.14/library/json.html).
These support the boundary and interchange mechanism, not fresh effectiveness claims.

## Canonical structure contract — 2026-11-03

[FactConsolidation data](factconsolidation-data.md) owns existing frame
parsing, conflict grouping and the complete derived fixture. Resolver/model
execution and Parquet acquisition stay outside. Source, fixture bytes, public
paths and original assertions are preserved.
