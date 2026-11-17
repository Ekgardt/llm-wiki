# Corpus discovery reuses canonical record parsing

Research and qualification date: 2026-10-13.

A 30-second sample of the owned read-only doctor process collected 2,963
successful samples and six observer errors. Every successful sample belonged to
the linked-session restoration phase of the second corpus discovery. This is
phase evidence, not a whole-cycle benchmark or an exact call count.

The public corpus collector reproduces a specific waste: restoring one small
complete event validates three distinct immutable protocol records eleven times
in each discovery. The existing canonical `_RecordParsing` reader is already
qualified for immutable record reuse, but this discovery path did not use it.

Sources checked before the change:

- [Python object semantics](https://docs.python.org/3/reference/datamodel.html#objects-values-and-types)
  distinguishes immutable values from mutable objects. Reuse binds exact bytes,
  rather than a filename, timestamp, or previously successful source lookup.
  [Python context variables](https://docs.python.org/3/library/contextvars.html)
  explains context-local state and token restoration. The existing reader uses
  Python 3.10-compatible explicit reset, not the newer token context manager.
- [MITRE CWE-367](https://cwe.mitre.org/data/definitions/367.html)
  describes changed objects between checks and uses. Physical reads, schema
  reads, source identities, links, complete content hashes, and membership
  reconciliation remain independent current checks.
- [SQLite isolation](https://sqlite.org/isolation.html)
  describes actual database snapshot guarantees. Markdown has no such MVCC;
  immutable parsing reuse does not claim a filesystem snapshot.

Each head restoration uses the existing parsing context if the caller already
owns one. Otherwise it creates a fresh reader local to this head, with the
discovery deadline as its active guard. Context restoration occurs on normal
and exceptional exit. Repeated record values remain fresh mutable decoded
objects; schema changes cannot reuse prior proof, and changed external bytes
are still read and verified. A caller's existing owner and schema guards are
preserved rather than replaced by a nested reader.

The first candidate always created a nested reader. A focused test exposed
loss of the outer owner's guard and the candidate was corrected before commit.
The final regression suite covers that path along with the pre-existing schema
drift, source damage, physical bounds, expiry, historical evidence, and complete
retrieval tests. The public repeated-validation test fails on unchanged code.

Increasing the deadline retains duplicate work. A global filename cache would
introduce stale authority; skipping protocol validation would weaken the source
contract. Reusing the existing exact-byte reader adds no new storage, dependency,
configuration, numerical ceiling, or parallel implementation. A new local
reader retains parsing proof only for one complete linked event; an existing
outer reader keeps its already-defined lifetime.

This correction reduces reproduced duplicate validation. Real complete corpus,
generation, installed maintenance, and health performance still require their
own qualification; a successful fixture is not point-seven completion.
