# Code parity report aggregation

Owns benchmark/aggregate_code_parity.py and the complete original
tests/test_code_parity_aggregate.py. Both remain byte-identical at their existing
paths. The module reads supplied UTF-8 JSON reports and prints ranges for known
sides present in every report, then per-run token totals for tasks both selected
sides graded correct. It does not execute tasks or grade their correctness.

Existing column/side names, pair, min/max rendering, rounding and error propagation
remain unchanged. The current ratio substitutes one for a zero denominator; this
qualification preserves that behavior and does not claim it is a statistical or
billing measure. Input loading trusts the report shape; no new input bound, schema
or validation policy is introduced. Historical token values retain their original
measurement method rather than becoming model usage by virtue of aggregation.

The packet contains the full source, all eight original assertions, fixtures,
required instructions and the full August 29 research record. That record covers
other product findings too and remains shared context, not solely this owner's
property. The new guards enforce stdlib-only imports in benchmark and scripts
search roots and a complete packet below 100000 o200k_base tokens. Run
tests/test_code_parity_aggregate.py and tests/modules/code_parity_aggregate with
normal pytest bootstrap. Reconstruct every packet file exactly.

## Qualification research — 2026-11-03

The fresh built-in graph and complete source search confirm a standalone report
reader and its tests. Preserve the cohesive implementation; combining it with
benchmark execution would import unrelated lifecycle ownership, while dividing
its small aggregation pipeline provides no demonstrated benefit. Existing source
and schema contracts stay unchanged; Python 3.14 remains the approved runtime.

Primary sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[Python JSON behavior](https://docs.python.org/3.14/library/json.html).
These support explicit responsibility and preserving parsing/serialization behavior,
not any new claim about the historical benchmark's statistical validity.
