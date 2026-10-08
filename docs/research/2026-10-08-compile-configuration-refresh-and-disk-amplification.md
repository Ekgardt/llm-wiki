# Configuration refresh and measured disk amplification

Research date: 2026-10-08. Point 7 remains open.

## A configuration-only change survives batch refresh

The compiler refreshed a Codex planning basis only when the process environment
changed. Configuration files are independently bound into that basis, and dispatch
correctly rejects a changed file. A human trusting hooks can change configuration
without changing the compiler's environment. Refresh then kept the obsolete basis;
subsequent attempts were rejected before dispatch while context rebuilding continued.

An isolated reproduction changes only a configuration file. The original refresh
returns the invalid basis and preparation refuses it. Three regression cases cover
changed, newly created, and deleted configuration files; all fail on the original
implementation. Refresh now checks both environment and the existing stable file
digests, resolves a new basis if either changed, and retains the existing provider,
model, settings and dispatch checks. It introduces no persistent cache or limit.
An intervening change after refresh is still rejected by dispatch.

Alternatives were restarting a whole compile after every configuration change,
ignoring configuration hashes, or refreshing from the evidence already owned by each
batch. The last is the smallest correction; ignoring hashes would weaken protection,
and restarting repeats unrelated work. This is not a claim that every error in the
observed live process has independently been attributed to this defect.

Primary sources, checked on the research date:

- [Codex hooks](https://learn.chatgpt.com/docs/hooks): human review persists trust
  against exact hook definitions. Trust belongs to the native operator flow.
- [Python 3.10 hashlib](https://docs.python.org/3.10/library/hashlib.html): compare
  SHA-256 digests of bytes, using the existing identity-checked reader.
- [MITRE CWE-367](https://cwe.mitre.org/data/definitions/367.html): a fresh planning
  check cannot replace the check at use. Dispatch validation remains intact.

## Validation and limitations

CPython 3.10.20: 39 direct configuration/environment/budget/model checks pass in
an isolated checkout. The broader compile selection passes 801 tests, with one
existing skip, using the fake provider that the CI workflow explicitly selects.
Five changed callables pass the actual source-diff complexity analysis; maximum
CCN is 3, with the required if-count and nesting limits. Ruff passes.

The first live-checkout test attempt also saw concurrent native capture writes and
an inherited explicit model incompatible with a synthetic resolver fixture. The
first broader isolated attempt inherited the production Codex provider: seven
tests failed. The same seven fail with the previous compiler in the same
environment. With the CI fake-provider environment, the complete selection passes.
These failed attempts remain evidence; none is counted as a successful check.

The observed normal medium-reasoning compile was interrupted cooperatively after
65 minutes of repeated failures. Its profile and normal finally handlers were
preserved. It had 78 provider attempts; 20 reported 1,663,730 input and 24,856 output
tokens. Usage is unknown for the other 58 attempts, not zero. Successful completion,
whole-cycle efficiency and semantic qualification are not established.

Before interruption, process counters reported 52,472,504,274 logical read bytes and
25,214,152,704 disk-read bytes. These cumulative counters are not stored corpus
size. The profile records 48 full executable bindings, including the 47 nested
dispatch checks, of the 289,101,384-byte Codex executable: about 13.88 GB of logical
reading. The nested checks must not be counted again. Hash verification is retained;
the remaining reads have not yet been fully attributed. Context packing consumed
about 2,797 inclusive seconds, including 68 refreshes; inclusive times overlap.

## What occupies storage

The installed vault occupied approximately 19 GB: 12 GB cache, 3.4 GB operational
state, 1.7 GB diagnostic logs, 637 MB knowledge, and 1.4 GB environment. These are
dated measurements, not product limits. The filesystem and diagnostic tmpfs were
both nearly full. Historical capture, undo and recovery evidence cannot be treated
as disposable merely because it is large.

The inspected memory generation occupies about 1.7 GB and contains 98,755 sources:
1,809 notes, 98 project documents and 96,848 verified session documents/parts. Raw
source content totals 124,965,207 bytes; all 96,848 physical source digests differ.
There are 206,751 search chunks. Vectors, search content, graph rows and provenance
metadata are separate derived representations. Two retained memory generations and
retained code generations of multiple existing checkouts multiply the footprint.

A concrete storage amplifier is the source table's WITHOUT ROWID layout with
4 KiB pages. Its overflow pages occupy 413,052,928 bytes, of which 297,637,127 bytes
are unused. The source table and unique-path index together occupy 497,246,208 bytes.
This is page-layout waste, not additional user content and not free pages that an
ordinary VACUUM necessarily eliminates.

Complete diagnostic copies compare every column of all 98,755 source rows and pass
integrity_check. An ordinary rowid layout, explicitly retaining NOT NULL on the
primary key, takes 245,329,920 bytes including its indexes. Keeping the exact schema
and trying 8/16/32 KiB pages takes 246,005,760 / 229,474,304 / 221,544,448 bytes for
this source-only specimen. These are not whole-generation, lookup-latency or
installation proofs. One earlier EXCEPT-based validation exhausted spare disk;
the temporary copy was removed and complete streaming comparison succeeded.

Three abandoned temporary vector matrices were byte-identical to the retained,
sealed generation artifact. Complete process file-descriptor, cwd and mapping
checks found no owners, with no unreadable processes. Exact-path cleanup verified
file and directory identities again and removed 952,708,992 bytes. The generation,
sources and operational state were preserved. All 20 vector build/reuse/read tests
pass after cleanup. No age-only deletion rule or new product limit was introduced.

Storage alternatives still require whole-generation and query measurements before
selection. Changing source-table schema would also require exact legacy-reader
compatibility and the owner's architecture approval. Larger pages preserve the
schema but can increase small random-read cost. Separating or compressing content
adds a representation and recovery contract. PostgreSQL adds a server outside this
product's contract; LMDB would replace SQL constraints and joins with additional
application machinery. None has been installed.

Relevant primary references:

- [SQLite WITHOUT ROWID](https://www.sqlite.org/withoutrowid.html): clustered tables
  favor small rows; document/blob workloads require measurement.
- [SQLite file format](https://www.sqlite.org/fileformat2.html) and
  [dbstat](https://www.sqlite.org/dbstat.html): page allocation, payload and unused
  space are measured separately.
- [PostgreSQL TOAST](https://www.postgresql.org/docs/current/storage-toast.html):
  large values require a deliberate physical representation.
- [LMDB binding documentation](https://lmdb.readthedocs.io/en/release/): mapped
  transactional key/value storage is an alternative, not a SQL-schema substitute.

Private reproduction and measurements are retained under logs/step7-*20261008*
and the corresponding /dev/shm test reports. No source content is published here.
