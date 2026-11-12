# Receipt authority reads only recorded write evidence

Research and measurements: 2026-10-12. The completed installed compiler cycle
returned success, including its normal generation refresh, in 3,210.879 seconds.
Its kernel counter recorded 322,063,584,761 cumulative read-family bytes and
6,995,050,496 storage-read bytes. These are process I/O accounting, including
observation code, not storage occupancy. The remaining five initial packages
became six successful complete partitions; all sixteen backend calls reported
usage. This completed remaining-work pass is not a matched comparison with the
previous fifty-package pass.

A receipt reader asks whether a committed transaction wrote the receipt and its
operations. `committed_attempt` returned the entire transaction, including its
recovery preconditions. Six real new compile records carry 390,843–850,810 bytes
of those preconditions. A real production lookup read 992,437 bytes even with
zero storage-read increment. This proves one repeated-read mechanism, not an
exclusive explanation for the whole 322 GB.

The repair adds an explicitly smaller `CommittedWriteAuthority` value: identity,
operation identity, committed state and every recorded write operation. A fresh
operational connection retains the contract and caller deadline. Exact identities
still take precedence; otherwise the existing literal ordinal range chooses the
latest committed attempt. Each receipt still validates its complete schema,
canonical bytes, filename, committed writer, receipt after-hash and operation
integrity. No source-presence, quote, live-head or receipt checks are removed.
The ordinary complete transaction reader is unchanged; it still returns the
preconditions needed by recovery and mutation callers.

The actual candidate API against the same real v3 database read 88,774 bytes in
0.001473 seconds, with zero storage-read increment. Identity, state and all 31
write operations matched the complete reader. This is a local API measurement,
not a whole-cycle speed claim. No schema, index, model setting, runtime path,
persistent cache or MCP tool is added.

The regression publishes a real continuation receipt, then uses SQLite's read
authorizer to forbid only the unused `preconditions_json` column. The old receipt
reader fails on its `SELECT *`; the candidate returns the identical authoritative
receipt. Further cases cover complete/projection agreement, literal wildcard and
Unicode ordinals, newest commit, prepared-work refusal, expired deadlines and
real exclusive-lock waiting. The locked-read wall bound is unchanged from the
existing authority tests.

Alternatives: memoizing committed records would need lifetime and invalidation
proofs under operational retirement; filling a complete record with empty
preconditions would misrepresent its data; removing authority validation would
lose the reliable mutation contract. Explicit SQL projection avoids all three.
SQLite and Python 3.10 already support the required selected columns and bound
parameters. PostgreSQL's index-only discussion independently explains why the
needed column set matters, but no PostgreSQL feature or dependency is used.

Primary sources inspected on 2026-10-12:

- [SQLite SELECT result columns and read-only semantics](https://www.sqlite.org/lang_select.html).
- [Python 3.10 SQLite connections, parameters and read authorizers](https://docs.python.org/3.10/library/sqlite3.html).
- [PostgreSQL 18 needed columns and index-only scans](https://www.postgresql.org/docs/current/indexes-index-only-scans.html).
- [Linux process I/O accounting](https://www.kernel.org/doc/html/latest/filesystems/proc.html).

Final local qualification: 911 related compiler, receipt, recovery and adopted
coordinator cases passed, with five platform skips. Forty-eight structure and
shard-weight guards passed. Written or changed functions have measured CCN at
most four, at most two if statements and at most two control nesting levels.
Ruff passed.

Installation still requires completed qualification. The successful earlier
compiler pass and its retained artifacts remain the baseline; they are not
relabelled as proof for an uninstalled repair.
