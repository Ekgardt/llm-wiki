# Repeated transaction reads during compilation

Measured on 2026-10-08 against the installed vault; the compiler was stopped
normally with SIGINT to preserve its profiler output. This is a partial-run
measurement, not a successful full-cycle qualification.

The previous process accumulated 117,513,277,369 `rchar` bytes and
17,442,643,968 `read_bytes` bytes. The first counts bytes returned by read-family
system calls, including cached database reads. It is neither corpus size nor
model traffic. The second measures storage reads. The selected eight closed
days in that process occupied about 61 MB.

A new run with Codex reasoning set to `medium` was profiled for 75 seconds
before model work. It performed 2,713 transaction-authority lookups and 74
ordinal lookups. Receipt selection accounted for 34 seconds; ordinal lookup
alone accounted for 5.87 seconds. Other substantial work included daily
partitioning, receipt parsing, and context packing. Therefore the ordinal
lookup explains substantial repeated reads, not the entire wall time.

The old ordinal query used `operation_id LIKE ? ESCAPE ?`. SQLite's default
ASCII-insensitive LIKE cannot use the existing BINARY unique operation-ID index.
The actual plan was `SCAN transaction`. A read-only missing-identity lookup on
the real database returned no rows but increased `rchar` by about 200 MB and took
0.0667 seconds. Its storage-read increment was zero.

The replacement uses bound BINARY range predicates: `operation_id >= id + '#'
AND operation_id < id + '$'`. These select the literal ordinal prefix because
`$` immediately follows `#`; wildcard characters in the ID are ordinary text.
Identity comparison now follows the exact, case-sensitive identity contract of
the direct operation lookup. The committed-state predicate, latest-created and
row-ID ordering, record integrity verification, and caller deadline are retained.
No schema, index, runtime path, cache, or retention limit is added.

The same real missing-identity query used the existing operation-ID index,
increased `rchar` by about 16 KB, and took 0.000082 seconds. This is a query
measurement, not a promised full-compiler speedup. The new regression observes
the actual authority query and asserts indexed search; it fails on the old
implementation. Literal wildcard and Unicode IDs and latest-commit selection
are also checked. Obsolete LIKE escaping code is removed.

Alternatives considered: adding a NOCASE index would add storage and preserve
undesired case folding; changing the connection-wide LIKE pragma would change
unrelated queries; caching authority results would require separate invalidation
proof under concurrent writers. The bound range uses the existing exact-ID
index and keeps every authority verification.

Related queries were inspected. Doctor's receipt lookup also uses LIKE and
joins operation evidence; its actual plan and correctness need separate
qualification. Retention's exclusion query intentionally enumerates whole
families and is not an ordinal authority lookup.

Sources, inspected 2026-10-08:

- SQLite optimizer: https://www.sqlite.org/optoverview.html#the_like_optimization
- Linux kernel counter definitions: https://www.kernel.org/doc/html/latest/filesystems/proc.html
- CPython parameter binding and read-only connections: https://docs.python.org/3/library/sqlite3.html

Local evidence: `/dev/shm/step7-full-compile-medium-20261008.prof`,
`logs/step7-full-compile-medium-20261008.log`,
`logs/step7-full-compile-medium-20261008.err.log`, and the new regression
`tests/test_ordinal_authority_uses_the_operation_index.py`.
