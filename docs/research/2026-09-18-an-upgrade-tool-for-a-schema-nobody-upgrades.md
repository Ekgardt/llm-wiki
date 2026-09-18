# An upgrade tool for a schema nobody upgrades

Dated 2026-09-18. Continues the round-two queue work of 2026-09-17 past midnight; the
findings are Q-L15 and the coordinator half of section 4 of the third audit.

Files: scripts/markdown_transaction.py, tests/test_operational_migrations.py

## What was found

- `upgrade_coordinator_v3_candidate` and its thirteen helpers (`_coordinator_v3_base_schema_complete`,
  `_upgrade_object_names_valid`, `_coordinator_schema_upgrade_objects_valid`,
  `_bounded_table_rows`, `_coordinator_base_rows_match`, `_coordinator_v3_upgrade_source_healthy`,
  `_require_coordinator_v3_upgrade_schema`, `_validate_coordinator_v3_upgrade_source`,
  `_copy_coordinator_v3_source`, `_require_upgrade_candidate_matches`,
  `_require_empty_blackboard_tables`, `_blackboard_schema_statements`,
  `_upgrade_coordinator_candidate_database`) plus `_BLACKBOARD_V3_TABLES` and
  `_COORDINATOR_V3_BASE_TABLE_SQL` have exactly two references each: their own definition and
  one another. Outside the module the only caller is one test.
- What it does: copy a coordinator-v3 database that predates the blackboard tables into a new
  candidate and add those two tables. The product has no such database. The adopted coordinator
  is validated against the full `_COORDINATOR_V3_TABLE_SQL` on every open
  (`validate_coordinator_v3_database` ← `MarkdownCoordinator._from_v3_candidate` ←
  `active_markdown_coordinator`), and `initialize_coordinator_v3_candidate` builds the complete
  schema for a fresh one. A partially-built candidate is repaired by
  `_repair_partial_coordinator_v3_schema`, which is reached from the initialiser, not from here.
- Finding Q-L15 is a defect of this tool alone: a crash during `_copy_coordinator_v3_source`
  leaves a candidate file that the next run believes, because the copy is skipped when the
  destination exists (`if candidate.exists(): return`). Nothing else in the module trusts a
  half-copied file that way.
- `_reject_coordinator_source_alias` and `_same_existing_file` look like part of the island but
  are shared: the two live candidate initialisers call them (lines 1639, 1662, 1673). They stay.

## Practice on this date

- SQLite's own guidance for changing a table beyond what `ALTER TABLE` allows is to build a new
  table and copy into it, under a transaction, and it states the danger this tool walks into:
  "the schema change is not fully applied until the transaction commits"
  ([SQLite, ALTER TABLE — making other kinds of table schema changes](https://www.sqlite.org/lang_altertable.html#otheralter)).
  A copy that leaves a file behind on the way is exactly the state the procedure exists to avoid,
  and this tool re-opens the leftover instead of discarding it.
- The owner's standing rule for this round: «всё что ненужно и бесполезно удаляй, главное ничего
  не сломай». A crash-unsafe tool for a migration the product cannot be in is unneeded twice
  over.

## The decision

- The island is removed, with the single test that reaches it. Q-L15 closes with the code that
  carried it: making an unused upgrade path crash-safe would be maintaining a feature for a
  database this product never has. Should a future schema step need it, the pattern to follow is
  SQLite's own — build the new database, copy under one transaction, publish by rename — not this
  resumable copy.
- `_reject_coordinator_source_alias` and `_same_existing_file` are kept; they guard the live
  initialisers.
- No contract text mentions the tool, so `CLAUDE.md`, `AGENTS.md`, `docs/STRUCTURE.md` and the
  READMEs are unchanged.
