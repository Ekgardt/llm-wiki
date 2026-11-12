# Coordinator schema declarations and inspection

Date: 2026-11-03. Internal transfer under the approved decomposition mandate.
The separately reproduced literal-normalization defect was repaired first in
243fe271. This transfer preserves that corrected behavior exactly.

Move six existing constants and ten functions into llm_wiki.coordinator_schema:
the v3 database contract, table declarations and their hash, optional index SQL
and lookup, expected table set; normalization adapter, table/index matching,
optional index creation, catalog inventory, table filtering/completeness and
migration-statement construction. Keep identity aliases in markdown_transaction.
No connection opening, adoption, migration runner, transaction lifecycle, writer
admission, data validation, pruning policy or filesystem IO moves with them.

Optional checkpoint indexes remain optional for old databases; history pruning
still creates them on the supplied connection before deletion. They remain outside
the historical schema hash, which binds only table declarations and header values.
Preserve the explanatory source comment with the index declarations. Schema
inspection rejects unknown indexes, triggers and views, missing tables and changed
managed declarations. The migration callbacks must retain their correctly bound
expected table names and SQL. Helpers neither begin nor settle a transaction.

The module depends on canonical bytes/hashing, SQLite contract values and the
qualified lexical normalizer. Those owners never import the coordinator. Direct
source searches supplement the partial graph for tests and monkeypatch consumers;
no patch of the transferred function names was found in the current tree. Preserve
existing caller aliases for migration/adoption, installed repair, doctor and tests.
They are retained compatibility surface, not a duplicated implementation.

Alternatives: move the whole coordinator (unbounded dependencies and multiple
responsibilities); split SQL from all inspectors into separate tiny owners (no
current independent need); change optional-index/hash semantics (breaks historical
adoption evidence). The chosen boundary owns one schema model and its inspection,
with caller-owned connection and transactions. No new dependencies, support floor,
runtime paths, database version, SQL text, schema hash or live installation.

Qualification: capture pre-transfer SQL/hash fixtures, prove exact AST transfer
and unchanged remaining definitions, real SQLite old/current schemas, unknown and
altered objects, rollback of optional-index creation, migration callbacks, historical
facade identities and existing adoption/recovery/prune integration. Measure actual
CCN and every complete source/guide/bootstrap packet against 100000 tokens.

Primary sources checked 2026-11-03 (three independent publishers):
- SQLite schema catalog object types and stored creation SQL:
  https://www.sqlite.org/schematab.html
- Python sqlite3 connection and transaction ownership:
  https://docs.python.org/3.14/library/sqlite3.html
- OWASP structured-input allowlist validation:
  https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html
Python 3.14 is now the approved runtime, implemented separately in 74bd1524.
This extraction makes no further runtime or support change.
