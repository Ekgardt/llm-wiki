# Coordinator schema declarations and inspection

`llm_wiki.coordinator_schema` owns the v3 coordinator table declarations, database
header contract, historical table hash, optional checkpoint indexes, catalog
inspection and migration-statement construction. It uses a supplied SQLite
connection and neither opens/closes it nor starts/commits/rolls back transactions.
Its optional-index helper executes the existing CREATE INDEX IF NOT EXISTS commands;
only the caller decides when this is permitted and how the transaction settles.

A complete schema has exactly the declared tables, each matching its declaration,
and only the two recognized optional indexes. Unknown indexes, views and triggers
are refused. Older databases without the optional indexes remain complete. History
pruning still creates them before deleting records. Optional indexes stay outside
COORDINATOR_V3_SCHEMA_SHA256, preserving recorded adoption evidence. Autoindexes
whose names start with sqlite_ remain omitted by the existing catalog query.

Statement callbacks bind each table name and SQL independently. No migration or
row-level invariant validation is performed here. markdown_transaction re-exports
identical values/functions for existing migration, repair, doctor and other
consumers; these compatibility aliases remain until a separately qualified consumer
migration. There is one implementation. SQL, hashes, versions and public commands
remain unchanged.

Dependencies: [SQLite contracts](sqlite-contracts.md),
[canonical formats](formats.md), [schema comparison text](sqlite-schema-text.md).
Source: [boundary research](../research/2026-11-03-coordinator-schema-boundary.md).
