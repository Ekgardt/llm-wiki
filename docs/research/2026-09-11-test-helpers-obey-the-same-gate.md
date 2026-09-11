# Test helpers obey the same gate

Date: 2026-09-11. Trigger: audit finding M12. Seven functions in two test
files sit over the complexity gate the product code lives under:
`tests/test_evidence_graph.py` `_damage_v3` (CCN 26, an `if/elif` ladder
of 24 arms mapping a damage name to SQL), the schema test (10: nested
comprehensions and `any`/`all`), the bounded-query test (8: seven
`[row["node_id"] for row in ...]` comprehensions);
`tests/test_context_compiler.py` `_resolved_call_targets` (19: import
binding, a recursive resolver and a fixed-point loop in one body),
`_producer_boundary_errors` (6), the stale-item test (6) and the
L2-outranks-L1 test (7). Re-measured 2026-09-11 with lizard: the other
files the audit named (`test_claims`, `test_generation_catalog`,
`test_project_journal`, `test_claim_schemas`, `test_retrieval_telemetry`)
no longer report a function over 5.

## Sources

1. Rule 5 as enforced on this machine: lizard CCN ≤ 5, nesting ≤ 2; the
   gate fires on every edited file, so a test file that is touched for any
   other reason is refused until its helpers pass.
2. Rule 5's own remedy: a ladder of more than two `if` arms becomes a
   table; complex logic becomes a pipeline of small steps.

## Decision

Behaviour is unchanged; every parametrised damage name still applies the
same SQL. `_damage_v3` becomes one table of `name → (sql, params)` for the
single-statement damages, three named functions for the multi-statement
ones, and one prefix rule for `eof-<table>`; an unknown name still raises
`AssertionError`. The comprehension-heavy tests get small named helpers
(`_names`, `_unique_index_columns`, `_ids`, `_representations`,
`_item_ids`). `_resolved_call_targets` is split into import binding,
resolution, assignment propagation and call collection.

Files: `tests/test_evidence_graph.py`, `tests/test_context_compiler.py`,
`CHANGELOG.md`, `docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
