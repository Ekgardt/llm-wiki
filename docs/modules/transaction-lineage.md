# Transaction lineage and quarantine witnesses

`scripts/transaction_lineage.py` remains byte-identical at its existing import
path. It reads a supplied SQLite connection to derive parent/request lineage,
committed creation paths, resolved quarantine IDs and witness rows to retain.
It opens no database, acquires no lease, writes no rows and neither commits nor
rolls back the caller's transaction. Doctor, repair and coordinator keep admission,
consistent-snapshot/deadline control and all mutation/retention decisions.

## Preserved interpretation

Retry suffixes and checkpoint attempt/epoch pairs retain their exact normalization;
checkpoint payload digest and request identity remain significant. Parent walking
retains cycle termination. Existing SELECTs, returned collection types and error
propagation remain unchanged. No cache, source-omission contract or new schema is
introduced.

Create-outcome evidence remains the existing nonempty intended-create path subset
of committed-create paths. This helper does not itself compare content digests or
open staged files. Replace operations alone do not establish that outcome. Doctor's
stronger source-specific checks remain with doctor. A row-reader result does not
authorize deleting runtime state or bypassing the global retention contract.
Witness computation retains matching requests, parent chains and committed creates
that tie quarantined rows to outcomes; callers decide what may actually be pruned.

## Test ownership

Two original parameterized identity tests move intact from their mixed doctor
files. The AST-identical `_database` and `_add` fixtures have one original copy in
this owner's fixture module, imported by both existing integration suites. Original
DIGEST, REFUSED and COMMITTED declarations also have one fixture owner. Remaining
integration test/helper definitions and all assertions stay unchanged.

Additional direct SQLite checks use query-only mode to verify actual SELECT
behavior, cycles, request identity, complete create outcomes, witness retention,
transaction ownership and propagated schema errors. This is test enforcement of
an existing read-only boundary, not a new production database setting. Fixtures
model only the columns the original readers require; full coordinator/doctor
integrations remain separate, preserved obligations.

## Qualification — 2026-11-03

The approved autonomous plan permits this existing cohesive owner. Duplicating
fixtures or carrying the whole coordinator/doctor in every reader packet is
unnecessary. Keep concrete stdlib-only source and one shared fixture definition.
Research checked:
[Fowler](https://martinfowler.com/bliki/BoundedContext.html),
[Import Linter](https://import-linter.readthedocs.io/en/stable/contract_types/),
[PEP 544](https://peps.python.org/pep-0544/),
[SQLite transactions](https://www.sqlite.org/lang_transaction.html) and
[query-only mode](https://www.sqlite.org/pragma.html#pragma_query_only).
No runtime dependency, SQL policy, algorithm or format changes.

Both focused September27 quarantine/pruning papers remain complete and retain
their dated observations. Baseline: 84 tests in five existing lineage/pruning/
source-evidence integration files pass. Require exact source/test/fixture
preservation, actual CCN/shape checks, real reader and original integration tests,
full source/guide/ownership checks and a reversible packet of at most 100000
tokens. No whole-project completion, task-cost saving or live installation is
implied by this owner qualification.
