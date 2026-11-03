# Coordinator supplied-connection invariants

Date: 2026-11-03. Internal decomposition under the approved whole-project mandate.

The schema declarations already have an independent owner. The next responsibility
is read-only validation of the rows on an already-open connection: the migration
error factory, row counts, base projection/epoch invariants, blackboard invariants,
the combined invariant, SQLite integrity/foreign-key checks and operation-shape
violations. Move these eight definitions exactly into
`scripts/llm_wiki/coordinator_invariants.py`, retaining old aliases/serialized
identities in markdown_transaction.py.

The new owner receives a connection; it opens/closes none, owns no transaction,
and executes only the current read queries/PRAGMAs. The caller keeps containment,
owner-only access, connection headers, schema admission, deadlines, migration,
publication, repair and rollback. No SQLite flags, tables, SQL text, epochs,
errors or return formats change. This reduces a real responsibility inside the
coordinator; it does not by itself remove its adoption/ownership composition cycle.

Before implementation, use the fresh builtin index and direct source inspection
of validation, migration, adoption and operational_ownership. The before suite
(adoption, operational migrations, blackboard and release retry) passes74tests.
All351coordinator definitions and18adoption test/helper definitions are recorded
in logs/coordinator-invariants-before-ast.json.

Move the two existing real SQLite integrity-corruption regressions into the new
packet without weakening assertions. Keep the rest of adoption integration and
run it with migrations, registry and actual transaction recovery. Add supplied-
connection tests against the original code before extraction: valid/mismatched
owner projections, epochs, capture bindings, blackboard claim sets, operation
positions and preservation of caller transaction state. Confirm all eight moved
ASTs and all343remaining definitions remain unchanged.

The full packet includes schema/contract/canonical dependencies, their complete
sources and guides, original corruption fixtures, instructions and research. Do
not pass validation by excluding a failed invariant or using a fake database.
Opening databases in the new module, importing the coordinator back, merging
mutation/validation into one generic framework, or replacing integrity_check with
a cheaper weaker probe are rejected. Existing aliases remain until consumers and
serialized identities have a compatible migration; they share one implementation.

Three independent primary publishers checked2026-11-03:
- SQLite PRAGMA documentation: https://www.sqlite.org/pragma.html — integrity_check
  does not cover foreign-key errors, so both existing checks remain.
- Python3.14 sqlite3: https://docs.python.org/3.14/library/sqlite3.html — connection
  transaction state and caller-owned commit/rollback must remain unchanged.
- Fowler bounded context: https://martinfowler.com/bliki/BoundedContext.html —
  explicit responsibility boundaries justify separating row validation from file
  publication; this principle is not a proof of SQL correctness.

No dependency update, schema migration, runtime-root change, release or installation.
The whole-project integration ownership obligation remains separate and explicit.

Fresh builtin evidence before implementation: ee6413a5, generation
`generation-18e48f1e8f3186e6-a620caa4`; error factory25callers, remaining validators
1-3each. Direct source and monkeypatch searches supplement the partial graph.
Evidence: logs/coordinator-invariants-graph.json.
