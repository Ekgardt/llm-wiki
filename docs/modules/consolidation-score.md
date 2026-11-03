# Paired consolidation report computation

Owns benchmark/consolidation_score.py unchanged. It projects supplied paired rows,
excludes ungraded pairs, applies the existing deterministic scorer and reports arm
means, discordant outcomes, exact-binomial McNemar p and recorded consolidation
cost. It imports longmemeval_score, whose complete source and longmemeval_data
source remain in context together with their full guides. No worker, provider or
vault is started or changed here.

Preserve ARMS order, provider/arm/harness failure classifications, default shared
row fields, scoring, float conversion, rounding and None/zero behavior. The existing
finite-size arithmetic and error propagation stay as implemented; no new limit or
statistical library is introduced. Results describe supplied observations, not a
new empirical claim that consolidation is effective or experimentally unbiased.

Eleven pure original test functions (16parameterized cases) move unchanged with
all decorators. Their pure helper stays with them. Three synthetic row builders
have one location, imported by the nine retained runner/vault integrations in
tests/test_consolidation_stand.py. Assertions and function ASTs remain identical.
Run this package and that original integration suite. The full August28
consolidation and first-run histories stay shared context, not newly qualified
benchmark results. Packet acceptance requires all source/guide/fixture checks,
exact byte reconstruction and <=100000 o200k_base tokens.

## Qualification research — 2026-11-03

Fresh index049e3244, get_architecture, callers and full source/test/history reads
confirm this existing pure computation boundary;25baseline cases pass. Combining
it with execution couples statistics to providers; splitting by formula fragments
one report contract. Preserve the cohesive module in place, with no runtime change.

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[JSON semantics](https://docs.python.org/3.14/library/json.html),
[McNemar definition](https://www.statsmodels.org/stable/generated/statsmodels.stats.contingency_tables.mcnemar.html),
[exact binomial test](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.binomtest.html).
The statistical references describe the existing binomial interpretation; they do
not validate the historical run, add dependencies or justify changing its formula.
