# One explicit SQLite transaction primitive

2026-11-02, completion mandate. The first complete schema-operations packet was
101618 tokens when it included reliable_memory to supply begin_immediate. That
failed the owner limit. The dependency also unnecessarily coupled explicit SQL
transactions to filesystem IO. Neither omitting required files nor increasing the
limit is accepted as the fix.

Move the existing contextmanager `begin_immediate` unchanged into
`llm_wiki.sqlite_transactions`, stdlib-only. The reliable_memory old name remains
an identical alias. Existing consumers import the new owner at the same lexical
scope (including model_selection's local import); no callback/state forwarding.
The caller still owns the connection and supplies before_commit. No nested
transaction abstraction, retry, connection opening, ownership or new clock is
added. BEGIN IMMEDIATE / yield / before_commit / commit / rollback-on-BaseException
and preservation of rollback error context are unchanged.

Three existing isolated tests of precommit failure/success and rollback failure
move with unchanged AST from test_reliable_memory into owned tests; the tests that
exercise operational database creation and locking remain integration tests.
Additional own tests cover KeyboardInterrupt rollback and connection ownership.
Old import/function identity and historical function pickle lookup remain tested;
__module__ stays reliable_memory on the exported wrapper for compatibility.

The schema-operations packet then depends on the complete tiny transaction owner,
not the entire filesystem module. Its test/import guards reject that old dependency.
Queue/schema/migration/ownership and transaction recovery qualification remain
required. Source statements, SQL byte identities, defaults and error classes do
not change.

Current primary sources from the preceding boundary apply: SQLite atomiccommit,
Python 3.10 sqlite3 transaction handling, and Fowler bounded contexts, all checked
2026-11-02. A new ORM/unit-of-work layer is not justified; passing the existing
connection and a small contextmanager preserves the real boundary. Python>=3.10
and dependency versions are unchanged. Evidence: logs/sqlite-transactions-consumers.json,
logs/queue-schema-operations-budget.txt (red 101618), refreshed built-in graph.

## Qualification

The refreshed built-in graph resolved begin_immediate with 129 incoming direct
edges; source imports found eleven production consumers, including one local
import whose lexical scope was preserved. The function AST is identical. The
remaining implementations of reliable_memory and nine non-queue production
consumers match after excluding imports; the queue remainder is independently
checked in the schema-operations preservation record. Three old test bodies moved
unchanged. Historical function pickle bytes, old import identity and wrapped
module metadata are preserved.

Baseline: 4 passed, 58 deselected. Boundary red: 3 failures. First owner/related:
117 passed, 3 skipped. Combined final qualification: 841 passed, 8 skipped;
Python 3.10: 202 passed, 3 skipped. Extra compatibility/weights: 20 passed.
Full-project complexity, structure, CI policy, Ruff and diff checks passed.
Evidence: logs/sqlite-transactions-{graph.json,baseline.json,preservation.json},
logs/schema-transactions-{related.txt,python310.txt,compatibility-final.txt}.
All 24 current serialized packets fit 100000 tokens. The separate full regression
is pinned to df42c0e2, not these later changes. Native Windows/macOS and deployment
remain unclaimed.
