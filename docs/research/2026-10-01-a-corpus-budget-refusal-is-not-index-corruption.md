# A live-corpus budget refusal is not index corruption

Date: 2026-10-01. Scope: diagnostic classification; no budget or settings contract changes.

A valid immutable generation followed by a live-corpus scan past its file-count
or total-byte budget returned `error: catalog or active artifacts are invalid`.
The artifacts had already passed validation. Two old-code regressions reproduce
this on real generated SQLite artifacts (2 failed). A deferred source read, used
by pathname traversal, separately reproduces the same untyped byte refusal.

Use `CorpusCapacityExceeded`, a subclass of the existing ValueError contract,
at all three enforcement points for the two existing configurable budgets. Keep
the existing refusal messages and thresholds. Carry the setting, its limit and
the minimum already observed. Doctor handles this type only after validating the
selected generation: report degraded, incomplete freshness, the exact exhausted
budget and a request to review that budget. Do not claim freshness, successful
collection or a complete index refresh. Do not rebuild validated artifacts solely
because this diagnostic encountered a capacity refusal. Malformed manifests,
damaged artifacts, unexpected ValueErrors and security refusals retain their
existing handling. No source, generation, quota, timeout, schema, runtime path,
environment name or MCP tool is removed, raised or added.

Sources checked 2026-10-01:

- [Python 3.10 exceptions](https://docs.python.org/3.10/library/exceptions.html)
  describes exception subclasses and preservation of parent exception handlers.
- [Microsoft health endpoint monitoring](https://learn.microsoft.com/en-us/azure/architecture/patterns/health-endpoint-monitoring)
  distinguishes checked components and meaningful diagnostic information.
- [Google SRE monitoring](https://sre.google/sre-book/monitoring-distributed-systems/)
  separates observed symptoms from causes and discusses saturation signals.

These sources support explicit failure categories and honest incomplete checks;
they establish no numeric optimum for the existing budgets.

Alternatives: increasing limits hides the diagnostic defect and needs a numerical
basis; rebuilding an intact index repeats work without fixing a quota; accepting
all ValueErrors as degraded would conceal actual corruption; matching error text
would couple behavior to incidental message spelling. A compatible exception
subclass distinguishes the exact resource refusal without relaxing validation.

Candidate qualification: 173 related corpus/generation/whole-source complexity
and branch tests passed, 3 skipped. New tests cover both budgets, public code-source
collection and a real sealed deferred source read. Existing corruption controls
remain unchanged. Changed/new functions measured by actual Lizard have CCN 1-4;
Ruff passed after correcting import ordering. Initial measurement-harness function
counts were incorrect and their failures are retained; the successful measurement
and the separate complete branch/complexity guards are the qualification.

This does not establish the cause of the installed Doctor's transient 16:46
catalog-invalid observation. An exploratory source-count probe used an explicit
2000-file bound, not the configured 10000; the corrected configured-bound probe
validated the selected generation and reported it stale. Preserve those distinct
observations. The later installed generation rebuild exhausted its existing
900-second budget with `generation retrieval cancelled`; it did not report
successful repair. The complete local regression snapshot f7b34186 predates this
candidate and had one deadline-test failure in its first parallel pass; its
unchanged failed-shard repeat is tracked separately. No complete current-source
regression pass is claimed by the candidate qualification above.


## Source-backed chunk sequence, approved 2026-10-04

The owner approved sequential derived-generation processing before this code was
written, recorded in the private streamed-derived-generations decision and
`docs/STRUCTURE.md`. A count-only global ceiling had refused the real corpus above
100,000 chunks. Simply removing it was rejected: 600,006 bytes of two valid
Markdown sources expanded into roughly 60 MB of retained chunk objects, and an
accepted repeated-source shape could exceed this machine's memory.

The collector now keeps immutable captured source bytes and one plan/count per
source. `CorpusSnapshot.chunks` remains a read-only sequence with stable length,
order, fields, hashes, integer access, and lazy slices. Iteration derives only one
source batch at a time and releases it before deriving the next. No chunking read
reopens a live file. Saved counts are checked against rederivation. Physical
source/span hashes and chunk identities still use the unchanged extractor rule.
`iter_snapshot_chunks(snapshot, deadline=..., cancelled=...)` passes the current
caller's clock into source derivation as well as successive rows. Its tuple-fixture
fallback checks that same clock. A collection's old deadline is not stored in a
snapshot and cannot expire later consumers.

Fresh primary research checked on 2026-10-04: [Python 3.10.22 read-only Sequence
protocol](https://docs.python.org/3.10/library/collections.abc.html), [SQLite FTS5
current documentation](https://sqlite.org/fts5.html), and [OWASP Denial of Service
current guidance](https://cheatsheetseries.owasp.org/cheatsheets/Denial_of_Service_Cheat_Sheet.html).
Python's sequence mixins can repeatedly index and become quadratic when indexing
is expensive; iteration and sliced iteration therefore have explicit one-source
batch traversal. Successive FTS consumption belongs to the shared writer/reader
qualification, not proof from the collector alone. OWASP's resource-admission
principle supports bounding retained working data rather than silently truncating
sources. An eagerly materialized tuple, filesystem rereading, a second database,
new limit settings, and persisted derived projections were rejected.

Controlled comparison used two real Markdown sources, 100,001 canonical chunks,
and equal complete chunk digests. Tracemalloc measured 1,230,984 bytes retained
after capture and 1,228,121 after streaming, versus 63,611,979 when an explicit
comparative consumer retained every chunk. Streaming peak was 39,886,744 bytes;
one source batch remains a real memory cost. Tracing timings were collection
10.05 s, streaming 9.89 s, and eager comparison 9.56 s: this establishes memory
behavior and byte parity, not a CPU speedup. Eager downstream tuple/dictionary
consumers must be removed or qualified before installation. Full real-corpus and
generation/vector qualification remain separate acceptance requirements.

File/source byte, membership, depth, per-source heading/span count, deadlines,
cancellation, native framing, and exact physical-byte checks remain. The inherited
per-source 100,000 count and other numerical bases are still explicitly under
review; removing the global retained-object ceiling does not justify them or
close the complete original audit. No persistent format, schema, runtime path,
environment contract, dependency, or extra database is introduced.

2026-10-04 downstream candidate qualification: two original-code failures proved
that authoritative FTS rederivation retained every row before comparison and that
its count stopped at 100,001. Rederivation now yields ordered source rows and
compares them successively, including missing/extra expected rows and source
count agreement when stored bytes were previously validated. The existing
in-memory unique-ID set and FTS build admission ceiling are still retained pending
whole-pipeline resource qualification; this is not complete streaming acceptance.

Two additional original-code failures proved that an answer shortlist retained
unrelated chunks and that cited-position resolution retained every chunk of a
page. Resolution now retains requested IDs only and finds the holding span
successively while preserving the first-page-chunk fallback. A third failure
proved that metadata-only context preparation expanded the entire chunk corpus.
The unused private parent chunk field had no consumers in scripts/tests; it was
removed, and evidence lookup retains requested IDs only. Broad metadata and the
existing context packing/citation behavior remain. The combined relevant suite
passed 122 tests in 4.05 seconds. An expanded generation suite also exposed a
source-descriptor alias regression in lazy collection (231 passed, 3 skipped,
1 failed); that regression must be fixed and reverified before installation.
These candidate changes are not installed or audit closure.

2026-10-05 collector follow-up: the real language reclassification regression
was reproduced and corrected by copying captured SourceRecord and SourceMetadata
values into the private source plans. Immutable content bytes remain shared.
A caller that reclassifies a public descriptor cannot change captured chunk
fields, hashes, or citation spans. Python 3.10.22
[dataclasses.replace](https://docs.python.org/3.10/library/dataclasses.html) and
the Sequence, SQLite FTS5, and OWASP sources above were freshly reviewed again.
Deep-copying all bytes or retaining expanded chunks would add unnecessary cost.
Source filtering now returns a lazy source-plan view; it retains ordering and
exact counts and materializes no chunks merely to select paths.

The original alias failure and source-view failures are retained. The related
suite passed 124 tests with 3 existing skips in 6.26 seconds; all 40 changed
collector/test callables passed actual Lizard and AST qualification, maximum
CCN 5, at most 2 if statements and branch/loop depth 2. Ruff passed.
The repeated 100,001-chunk comparison retained 1,231,560 bytes after capture
and 1,228,601 after streaming; explicit eager retention was 63,612,459 bytes.
The complete chunk-ID digests agreed. Collection/streaming/eager tracing times
were 9.846/9.810/9.693 seconds; no CPU speedup is claimed.

The attempted real whole-vault collection refused with CorpusChanged because
membership did not hold still. It used 590.829 seconds and peak RSS 687,892 KiB.
No generation was published and no model called. This is a retained failure,
not a successful complete-corpus qualification. Another whole-vault attempt
requires coherent membership and complete downstream qualification first.
