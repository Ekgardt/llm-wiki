# Bounded input module

Qualified in its existing location on 2026-11-02. `scripts/bounded_io.py` remains
its sole implementation; production code, consumer imports, runtime paths, byte
limits and error contracts are unchanged. This qualification does not move ACL
or publication code into this reader.

## Responsibility

The module reads bounded regular files, checks file identity before/after reads,
rejects unsupported path indirection and checks cooperative monotonic deadlines.
`read_stable_bytes` returns complete bytes. `open_stable_range` holds a descriptor
and exposes `VerifiedByteRange`; consumers must finish reading inside its scope
and must wait for successful context exit before publishing derived artifacts.

A range records the original content digest and per-block hashes, verifies
reloaded blocks, and rechecks live identity while its owner is active. Its cached
block holds captured immutable bytes. Normal context exit rechecks content and
closes the descriptor even when validation fails. The source owner controls the
stream lifetime; constructing a range around an arbitrary stream does not add
filesystem ownership or prevent another writer from modifying that stream.

Range operations support byte indexing/slicing, forward/reverse search, range
hashing and a read-only newline-offset index with optional strict UTF-8 checking.
They do not interpret citations or compile receipts. Mutable read cursors do not
make the source identity mutable and do not promise concurrent thread safety.

## Existing safety contract and limits

Leaf symlinks and reparse points are refused. Ancestors must be directories or,
on supported POSIX paths, administrator-controlled system symlinks satisfying the
existing ownership checks. This accommodates macOS system paths; it is not an
allowance for arbitrary user links. Descriptor/path identity changes are refused.
The API is not an operating-system sandbox or a universal defense against a
privileged writer. Kernel calls are not preempted by the cooperative deadline.

The existing 8 MiB knowledge-page ceiling and 64 KiB chunk size remain unchanged.
Callers supply their own byte bound. These are byte/resource contracts, distinct
from the user's 100,000-token maintenance-context ceiling.

## Complete maintenance context

`bounded-io-context.json` lists complete source, both own test files and inline
data, required instructions, canonical structure, modularity design and relevant
reader history. Tests are not sliced or omitted to fit the cap. Static import
checks reject product and relative imports, including imports inside functions;
negative examples demonstrate the guard. It does not detect arbitrary dynamic
code execution or replace behavioral tests.

```
PYTHONPATH=scripts uv run pytest --noconftest tests/test_bounded_io.py tests/test_stable_source_ranges.py -q -s
```

The original isolated baseline passed 14 cases and skipped one Windows-only case
on Linux. Tests cover parent substitution, symlink policy, deadlines, changed
live files, cached ranges, final validation and cleanup. Added byte-equivalence
cases exercise chunk boundaries, reverse slices, empty/missing searches and
negative bounds; mutation and UTF-8 checks exercise the independent range API.

## Integration boundary

The AST at e0bf373d found 52 direct import sites in 48 product files. The indexed
graph found 89 callers of read_stable_bytes; general graph completeness is not
assumed. Compilation, archive validation, transactions, receipts and original
source reconstruction remain integration scenarios under their existing owners.
Their dependencies are not presented as part of the isolated reader, and changes
to those workflows require their own context and tests as well.

Existing production source bytes are preserved. This establishes a bounded
maintenance unit and a guard against future executor coupling; it does not prove
repository-wide modularity, live-install qualification or reduced LLM task cost.
Evidence: local logs/bounded-io-baseline.txt, logs/bounded-io-graph.json,
logs/bounded-io-module-graph.json and logs/bounded-io-consumers.json.

Qualification: 53 own cases passed and one Windows-only case was skipped on
Linux, on both Python 3.12 and 3.10. Related range/compile/archive and global
complexity checks: 273 passed, 1 skipped. Source bytes match 1e3e5d89; Ruff passed.
The initial mutation fixture moved the shared stream cursor as well as changing
bytes and therefore tested truncation; the fixture now restores its cursor before
asserting hash rejection. No production behavior or assertion was weakened.
