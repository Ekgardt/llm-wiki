# SQLite operational connection policy

llm_wiki.sqlite_pragmas applies and reads back the existing policy on a supplied
connection. Writable connections require rollback DELETE journal, synchronous FULL,
foreign keys enabled, trusted schema disabled and the caller's busy timeout. Readonly
policy additionally enables query_only and checks the existing journal mode. The
same row factory and refusal order are retained. Unknown/missing/noninteger readback
values fail the contract instead of silently passing ignored SQLite pragmas.

Header validation compares application_id and user_version. Explicit initialization
retains the existing unstamped (0, 0) condition; conflicting identity is refused.
This does not grant admission to stamp a database: that remains the caller's policy.
The owner neither opens/closes connections nor changes transaction grouping,
filesystem permissions, identity checks or deadlines. It imports only sqlite3 and
sqlite_contracts. reliable_memory keeps identity aliases, with no duplicate bodies.

Tests use real temporary SQLite databases for readback and actual readonly refusal,
plus header conflicts, unknown pragmas and nondurable in-memory journal refusal.
Existing IO/adoption/transaction tests continue checking the outer ownership layer.

Run `uv run pytest tests/modules/sqlite_pragmas -q -s`.
See ../research/2026-11-03-path-and-pragma-boundaries.md.

## Canonical ownership reference

`llm_wiki.restricted_paths` owns lexical POSIX relative-path and allowed-root
validation. `llm_wiki.sqlite_pragmas` owns settings and contract-header readback on
supplied SQLite connections. Filesystem containment and ownership remain with the runtime filesystem owners;
SQLite opening and closing belong to sqlite_io. Old APIs retain identity aliases.
