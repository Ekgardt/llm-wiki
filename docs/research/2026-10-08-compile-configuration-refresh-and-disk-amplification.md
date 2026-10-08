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

## Selected physical-page correction

The complete memory/code comparison preserves every column in every graph table,
passes integrity and foreign-key checks, and measures distributed source lookups.
The qualified operator has nine measured callables, maximum CCN 4 and compliant
if-count/nesting. Its earlier inline experiment is retained as exploratory evidence;
it is not the operator-complexity qualification.

For the memory graph, 8/16/32/64 KiB pages produce 574,005,248 / 560,250,880 /
553,910,272 / 549,650,432 bytes, versus 825,307,136 bytes. At 8 KiB, the distributed
warm source lookups are fastest among the measured alternatives. Larger pages save
only another 14–24 MB while those lookups get slower. For the code graph, all four
alternatives increase storage: 343,785,472 / 346,177,536 / 345,178,112 / 344,850,432
bytes, versus 339,603,456 bytes. Cold-disk latency is not established by this test.

The builder therefore uses 8 KiB physical pages for memory-only generations and
keeps code generations at 4 KiB. Direct database construction retains its existing
4 KiB default. The same exact v2 schema, hashes, sources, foreign keys, rollback
journal, FULL synchronization, paths and runtime root are retained. All SQLite
supported page sizes remain available to direct construction; unsupported values
are refused rather than silently ignored. Existing immutable generations are not
edited in place. Tiny memory generations may use more space because their empty
table/index pages are larger; the selected tradeoff addresses the measured installed
memory workload without imposing the same storage cost on code indexes.

The original 400-source regression produces identical 1,986,560-byte baseline and
builder databases and fails the required storage improvement. After the correction,
all 119 direct graph/builder/reuse tests pass. Broader corpus/generation/repository/
vector coverage passes 690 tests with six existing skips on CPython 3.10.20.
Thirteen changed callables pass actual complexity analysis, maximum CCN 5, and Ruff
passes. Installed-vault compaction and full-cycle qualification remain separate
steps; the measured database reduction alone does not claim them complete.

## Confirmed memory pressure and completed diagnostic cleanup

The kernel records global OOM kills on 2026-10-08 at 10:05–10:08, including the
user's D-Bus process, systemd user manager, codebase-memory processes and Python.
The system journal independently records user@1000.service killed by OOM and
failed with signal 9. This explains the unavailable scheduler, not a scheduler
backend configuration change. The nearly full 7.7 GiB diagnostic tmpfs consumed
RAM. Its contribution to memory pressure is established; its precise share of the
compile's disk reads is not.

Complete ownership checks qualified the finished Oct 5 pytest fixtures and the
completed derived ordinal-investigation index. Historical test reports and the
original failed test's generated files were preserved. Cleanup removed
2,565,902,336 allocated tmpfs bytes. A generated read-only BagIt fixture caused the
first attempt to fail; retry removed only that qualified test tree. Production
knowledge, operational state and historical capture/undo evidence were preserved.
The existing systemd user service was restarted, and both installed timers are
active/enabled with their original schedules. Backups remain owner-paused.

A separate operator mistake passed a partial plan to repository retention's private
helper, whose orphan-hint phase requires the complete plan. Ten disposable hint
tables were removed unexpectedly. All ten were reconstructed from their untouched
generations, and the removed-checkout set is checked against the restored set.
Subsequent retention must use the complete public plan or the repository-specific
retirement operation, with a check that unrelated retained hints survive. The
failed installation and partial-cleanup attempts are not counted as success.

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


## Repeated benchmark inputs and safe replacement

Research date: 2026-10-08. Five hundred staged question files from two completed
LongMemEval runs have identical SHA-256 and bytes, accounting for 257,915,665
avoidable duplicate bytes. Their historical result files differ and must stay.
At the initial qualification no copies had been removed or shared.

Both LongMemEval and consolidation runners wrote questions in place. A causal
regression links two staging inputs, updates one through the real runner and
checks the other remains byte-identical. Both original runners fail this check.
They now use the existing durable atomic_write boundary: complete sibling staging,
fsync, checked publication and atomic replacement. The correction also preserves
the existing stale-result deletion and failed-worker reporting. Forty-nine related
checks pass on CPython 3.10.20; the broader runner/reader/refusal/thread and atomic
writer selection passes 106 checks. Actual analysis of four changed callables reports
maximum CCN 3. This is a prerequisite for safe deduplication, not evidence that
production duplicate files have already been consolidated.

Alternatives: deleting historical inputs would lose calibration provenance;
sharing writable inodes before changing both producers would corrupt another
run; introducing a content-addressed directory would change paths and require
architectural approval. Atomic replacement reuses the established writer and
keeps every existing path and result format. Filesystem sharing, if subsequently
qualified, must verify all bytes, ownership, live consumers and every writer.
Active and fallback memory generation artifacts remain independent.

Primary sources, checked on the research date:

- [Python 3.10 os.replace](https://docs.python.org/3.10/library/os.html#os.replace):
  replacement and same-filesystem constraints.
- [Linux rename(2)](https://man7.org/linux/man-pages/man2/rename.2.html): other hard
  links retain the original file when a directory entry is replaced.
- [Microsoft hard links](https://learn.microsoft.com/en-us/windows/win32/fileio/hard-links-and-junctions):
  linked files share contents; writing through a link changes every linked view.

Separately, two pre-existing operator-owned systemd drop-ins overrode the installed
medium reasoning preference with max. They were updated to the owner's requested
medium with verified preimages; both services' effective environment confirms it.
A running Codex host still carries its older max environment, which the explicitly
approved provider-override contract preserves. That contract was not silently
changed, and a future host refresh remains necessary for that inherited setting.

## Completed cleanup and current corpus qualification

After both producers were installed, the complete inventory of 2,090 staged
benchmark questions found 500 duplicate groups spanning 1,476 paths. Exact-byte
sharing under the existing paths recovered 505,430,016 allocated disk bytes.
All 2,090 paths and SHA-256 digests compare identically before and after; historical
results remain separate and untouched. Complete initial and final ownership
inventories found no consumers and no unreadable processes. A refresh of the
ownership report immediately before cleanup failed because root could not replace
a user-owned file under sticky tmpfs; cleanup used the earlier complete inventory.
The refresh failure is retained explicitly, and the final complete check succeeded.
Fifty-nine related checks pass after cleanup. This operator mistake is not counted
as a successful pre-cleanup refresh.

One approximately 382 MiB derived code index belonged to a completed, installed
diagnostic. Its source checkout, unique SQL evidence and reports are retained.
Repository-specific fenced retirement removed only that index and its hint;
the active memory generation and all unrelated hints remained identical.
Twenty-eight retention and hint checks pass after retirement.

The measured current corpus has 107,490 selected sources, 224,426 chunks and
141,417,366 source bytes. Collection took 260.365 seconds and peak RSS was
786,628 KiB. Existing operator source/entry budgets were requalified from physical
inventory using the previous warning-share rule; the byte budget did not change.
This qualifies that inventory, not every future workload or every historical limit.

## Date-local dependencies during immutable quote measurement

The measurement cache used every selected day as a binding dependency. Adding an
unrelated day therefore repeated the original day's physical-byte binding. The
uncached binder and reference-cover check both depend on every selected part of
the evidence day, not other days. The cache now retains exactly those immutable
part objects while retaining the full date/time/quote key. Another part of the
same day, a replacement object, changed hashes and protected aliases still require
fresh checks. Final model answers retain the uncached binder.

A causal regression fails on the original because adding another day binds the
same original quote twice. The correction passes all three new checks and 100
related CPython 3.10.20 checks, including full prompt/schema equality and rejection
of ambiguity introduced by another part of the same day. Five changed callables
pass actual analysis, maximum CCN 4, with compliant if-count and nesting; Ruff
passes. The new test-file weight is measured from its JUnit durations. This is
an internal dependency correction, not a claim of whole-cycle token improvement.

Alternatives were disabling caching, caching by quote alone, or indexing the key
by the binder's actual immutable dependencies. Disabling repeats proven pure work;
quote-only reuse would lose source authority and ambiguity checks. Dependency-local
reuse preserves the complete request without a global cache or new limit.

Primary sources, checked on 2026-10-08:

- [Python 3.10 memoization](https://docs.python.org/3.10/library/functools.html#functools.lru_cache):
  keys distinguish arguments and retained references preserve their lifetime.
- [Bazel caching](https://bazel.build/remote/caching): reusable work requires its
  relevant inputs, and concurrent input changes invalidate assumptions.
- [Rust incremental compilation](https://rustc-dev-guide.rust-lang.org/queries/incremental-compilation.html):
  dependency tracking governs reuse, including indirect dependencies.

## Whole generation deadlines still need correction

A normal 900-second refresh deferred after 905.107 seconds. A manual 1,812-second
refresh then completed vector computation and wrote all declared artifacts, but
ran out of time during candidate validation: 262.638 seconds for capture, 6.222
for the parent and 1,543.146 in build. It deferred after 1,812.724 seconds, never
activated the candidate and removed its unfinished directory normally. The old
active generation remained intact. Completed artifact writes alone are not
publication or an installed reduction in storage.

The manual estimate was insufficient: it did not allow the complete validation
and source-publication stages. The installed two-minute post-compile refresh is
also shorter than current corpus collection alone. Repeated construction lost
work at the deadline; further qualification must measure the final stages rather
than treating another timeout or larger arbitrary constant as completion.

Complete deep validation of the retained parent in a fresh process took 52.784
seconds, 52.417 CPU seconds and peak RSS 358,204 KiB. It minted the normal
process-local validation capability without registering or activating anything.
Publication's source check verifies the captured manifest in memory; it does not
re-collect the live corpus, so another whole collection is not its measured cost.

A short test run in the live checkout passed nine assertions but failed the
session teardown because new native breadcrumb records appeared during the run.
That attempt is not green. The records are retained; validation moves to a fresh
isolated checkout at the same committed baseline without weakening the write guard.
The fresh isolated checkout then passes all 106 related and test-weight checks
on CPython 3.10.20, with only the two existing record_property warnings.

During this measurement a second native auto compiler inherited the old host's
max setting and performed approximately 4.73 GB of logical reads before a
cooperative, PID-identity-bound interruption. Its native configuration and source
records were preserved. The operator temporarily held the existing real compile
lock during the generation check and released it on normal process termination;
no permanent lock or new control plane was introduced.
