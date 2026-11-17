# The first module: formats and source identity

2026-11-02. Implementation of the approved bounded pilot on the base `3e1def7e`.
This is not the completion of splitting the whole project and not an installation of the unfinished
compile branch into the working vault.

## What changed

`scripts/llm_wiki/formats/` now holds the canonical JSON bytes and SHA-256,
the existing closed JSON-schema validator, source descriptors and reading of
v2/v3/v4 receipts. The module has no dependency on the compiler, cache, queue, transactions or LLM.
Doctor and the resolver get the parsers directly from their owner. The check of
the authority of a published operation stays with the existing executors.

The old definitions were removed. The existing public imports return the same
objects, not copies or wrappers. This was checked separately for functions, exceptions
and source types. Keeping the names is needed by the current consumers; the removal
condition is the migration of all consumers and an explicit end of the compatibility contract.

84 moved definitions match the originals by AST after normalizing
two renames (`validate_rule`, `receipt_source_fields`). Schema paths
were adjusted to the new package depth; the schemas themselves and the wire formats were not changed.

## Context and protection against recurrence

[The module package](../modules/formats-context.json) contains all its sources,
its own tests and byte samples, schemas, documentation, mandatory instructions,
the dependency declaration and the shared timeout helper. With the full `docs/STRUCTURE.md`, both preceding studies and this report,
the size is about 77 thousand `o200k_base` tokens; an automatic test prints the exact value.
The 100,000 limit is set by the user. Added files are counted automatically.

An isolated run does not need global integration fixtures; a full run
keeps all shared checks. Joint scenarios were not removed for the budget.
The size of the whole repository grew at this step because of the research and the test
protection; reducing the maintenance context of one module is not proof of
savings over the full cycle of an LLM task. These measurements are still needed for later modules.

The boundary test before the move found three failures: two calls to the parser through
the compiler and the missing package. After the move, 40 isolated tests pass.
A fresh process parses all three formats while an audit hook forbids imports of executing subsystems,
creating databases, child processes, network connections and file writes.
An AST check limits dependencies to the standard library and the module itself.

CI used to look for test files only in the root of `tests/`. Now it finds nested
modules and keeps their relative paths in the table of measured cost. Separate
regressions check nested paths, classes in JUnit and the exclusion of input fixtures.

## Checks of the initial move (bdd7dba7)

- 40 isolated tests: canonicalization, schemas, path boundaries, fixed
  v2/v3/v4 receipts, corruption, source identity, dependencies and budget.
- Python 3.10: 207 module and integration checks passed.
- Python 3.12.3: 91 targeted checks passed; facade compatibility was checked
  separately (58 passed, 3 platform skips).
- Ruff passed. The shared law 5 check passed; a later measurement of 185 functions
  of the affected set gave a maximum CCN of 5.
- The fixed byte samples and the expected records were checked with the source parsers
  before the extraction. The full v3 sample was taken from an existing synthetic archive;
  private memory was not included in the tests.
- The graph of the new `receipts.py` shows dependencies only on `canonical.py`,
  `schema.py` and `source.py`. General completeness of the graph is not claimed: the built-in graph
  reports unresolved links outside this slice; the boundary was additionally checked by AST.

The full regression was run in two parts because the disk filled up; the interrupted
file was rechecked whole. By the merged JUnit results and the repeated checks,
all 14 962 collected scenarios are represented: **14 711 passed, 243 skipped,
8 also fail on the source version `3e1def7e`**. One parameter contains the identity
of the running Python process and was matched by the same scenario and the value `False`;
this is reflected in the local coverage report. Skips are not declared checked.

Earlier failures:

- `test_streamed_long_daily_preserves_all_parts_and_receipt_flags`;
- `test_duplicate_daily_parts_are_still_refused`;
- `test_a_batch_holding_two_parts_of_one_day_is_refused`;
- `test_lost_context_fails_its_child_without_hiding_or_discarding_its_sibling`;
- two checks of literal timeout/deadline in `test_slow_machine.py`;
- two parameters of `test_admission_reads_use_existing_store_waits`.

They were reproduced in a separate copy of the unchanged source version. Their expectations
were not weakened. The pilot is not declared fully qualified for installation
while the shared regression is red; the errors belong to the previously unfinished branch.

During the check, a remaining test import of the internal validator
by the old path was fixed. For 89 earlier files without a weight, actual measurements
from this run were added; the existing CI weights were kept. The final repeated check
of the fixed parts, the weight table, the facades and the module: **88 passed**.

## Requalification: current test contracts

After the user's command "continue", the causes of all eight failures were checked
against the current code and a fresh built-in graph. Production code was not changed.

- The streaming original is a borrowed `VerifiedByteRange`, valid until
  the end of reading the source. The test now takes the exact bytes while it owns them
  and compares all fields with an eager snapshot; the memory limits of the production code are kept.
- The duplicate check uses a real canonical part, so that the refusal is
  caused by the overlap, not by missing source bytes in an artificial fixture.
  Unverified parts are still rejected; the allowed merge of verified
  neighbouring parts stays covered by the existing positive tests.
- The repair fixture passes the mandatory `dry_run`. Both modes are checked separately,
  including the ban on writing an error in a trial run and the preservation of an independent sibling.
- SQLite observation was moved to the shared path of opening adopted databases: the queue was already
  opened by its specialized reader. The real values of
  `PRAGMA busy_timeout` of both databases and the limit by the remaining deadline are checked.
- Five waits and two deadlines in the tests use the existing `LONG_TIMEOUT` /
  `PAUSE_TIMEOUT`. Production deadlines, security policies and expectations
  of a successful result were not weakened.

A gap in the static protection was reproduced as well: a nested test module
was not included in the timeout/deadline check. Two new checks failed at first.
Now nested sources and helpers are scanned, and diagnostic keys keep
relative paths: an identical file name does not hide another file's violation. Input
fixtures are excluded as data, as in the other source code checks.

On Python 3.12 and Python 3.10 all **119** affected checks passed.
**New full run: 14 722 passed, 244 skipped, 0 failures in 1 817.98 s.**
It was run whole on Python 3.12.3; the result is not assembled from different runs.
The number of skips includes a module skipped at test collection. Five warnings
belong to the existing `record_property` with the JUnit xunit2 format; they are not hidden.
Ruff and the shared complexity check pass. The new checks of nested modules first
gave two expected failures on the earlier traversal and passed after it was fixed.

The earlier eight failures no longer block this pilot. Skipped platform
scenarios are not declared checked. A green regression does not mean installing the whole
unfinished compile branch, finishing the earlier item 7 or proving
token savings over the full cycle of LLM tasks.

Source: `logs/formats-followup-full.txt`, `logs/formats-followup-full.xml`,
`logs/formats-followup-targeted.xml`, `logs/formats-followup-python310.txt`,
`logs/formats-followup-nested-before.txt` (local reports, not published).

## Direct consumer imports

The next step of the same approved pilot moves 58 production files to
the formats owner. Before the change, an AST inventory found 78 imports of moved
names through the old modules and two qualified calls in doctor. After
the move there are no such calls. In 27 files, the direct dependency on the old
module as a whole disappeared; the others keep the needed operational imports. This is a measurement
of direct links, not a claim that all transitive dependencies disappeared.

The new protection found violators before the move. It checks all production
Python code, including local imports, aliases and star imports. Ten small
scenarios check recognition of forbidden calls and allowed operational
imports. All 51 isolated module checks pass on Python 3.12 and 3.10.

An AST comparison of all 58 changed production files after excluding imports and
normalizing the two doctor calls confirms the rest of the code is unchanged.
Public re-exports are kept as the current compatibility contract and still
name the same objects; there is no second implementation. The removal condition is an explicit
end of this public contract after checking external consumers.

Two tests instrumented the old location of the validator's lazy import. Both
failures were reproduced: the observer saw zero calls. The observation was moved to
the schema owner's namespace; all assertions about repeated reading of sources
and single validation of each exact record are kept. All 27 scenarios of the two
files pass. The other places where moved functions are patched were checked in the code.

Full run on Python 3.12.3: **14 732 passed, 244 skipped, one failure in
1 818.64 s**. The only failure is the second test observer, fixed above;
its file and the related file were rechecked whole: **27 passed on Python 3.12 and 3.10**.
JUnit matching confirms that the only failed scenario is part of
the successful repeated check. This is a full run plus a targeted recheck,
not one fully green run. The five earlier `record_property`
/ JUnit xunit2 warnings are kept; skips include the module at the collection stage.

An additional integration set on Python 3.10: 112 passed, 3 skipped.
Ruff and the shared complexity checks pass. All 9 582 functions
of the changed Python files were measured separately: the maximum CCN is 5. The weight of the extended boundary test
was updated by the actual time; the other CI weights were kept.

Source: `logs/formats-consumer-baseline.json`,
`logs/formats-consumer-boundary-before.txt`,
`logs/formats-consumer-removed-edges.json`,
`logs/formats-consumer-ast-equivalence.json`,
`logs/formats-consumer-instrumentation-before.txt`,
`logs/formats-consumer-instrumentation-after.txt`,
`logs/formats-consumer-corpus-observer-before.txt`,
`logs/formats-consumer-observers.xml`, `logs/formats-consumers-final.xml`,
`logs/formats-consumer-reconciliation.json`,
`logs/formats-consumer-complexity.json` (local reports).

## Compressing the opaque integration sample

The next stage of 2026-11-02 keeps the path and all the data of
`tests/fixtures/compile-receipt-v3-foreign-body.json`. The outer JSON still
contains readable description, metadata and logical paths. The files values now
have an explicitly marked `base64+zlib` representation; a separate SHA-256 pins
each restored file. The decoder lives with the only consumer test.
Production formats, runtime archives, the source memory and production code do not change.

Source sample: 297 823 bytes, 94 311 o200k_base tokens. New: 20 701 bytes,
12 566 tokens. All 22 paths, 221 399 restored bytes, description
and metadata are kept. Each file was compared directly with the decoded original
from `31e5a6ae`, not only with the new digest. All 22 match byte for byte; including
SQLite, source texts, receipts, saved preimages and transaction records.

Minified JSON, zlib, bz2 and lzma were compared. Removing whitespace saves only 101 tokens
and worsens the readability of metadata. On this sample zlib gives a smaller token size
and a lower measured cost of encoding/checking than bz2/lzma. Of the zlib levels
1–9, level 7 was chosen: 12 566 tokens; level 9 gives fewer bytes, but 12 584 tokens.
This is a choice by measuring a specific immutable sample, not a universally best
level. zlib 1.3 was used; decoding does not need an identical compressor.

The source two scenarios ran successfully before and after the conversion. Their assertions
were not changed. The decoder requires a finished single stream without a tail and an exact
SHA-256 before the file is written. Six more checks reject bad base64, invalid zlib,
truncation, a tail, a second stream and changed content with valid compression.
This is a trusted test sample, not a new shared API for unpacking external archives.

The compressed transport does not replace the needed unpacked context when investigating
the scenario. It is not claimed that the integration package now fits entirely
in 100,000 tokens or that the full LLM task became cheaper. Readable production code
is not minified; Git keeps the source version of the sample.

Primary sources checked on 2026-11-02:
[Python zlib, Python 3.10 API](https://docs.python.org/3.10/library/zlib.html)
(decoder compatibility; the documentation marks the end of support of this branch),
[RFC 1950](https://www.rfc-editor.org/rfc/rfc1950)
(lossless compression format),
[Git attributes](https://git-scm.com/docs/gitattributes)
(opaque text representations and diff options). A binary file with textconv
was not chosen: it would need a new path and a view setting; the existing JSON
keeps visible metadata and hashes. No new dependencies were installed.

Evidence: `logs/fixture-compression-equivalence.json`,
`logs/fixture-compression-alternatives.json`, `logs/fixture-compression-levels.json`,
`logs/fixture-compression-before.txt`, `logs/fixture-compression-after.xml`.

## Local side operations

A mandatory private decision page was added to the working vault. Its creation
reached the index limit: 4 316 pages with a limit of 4 315. A temporary rehearsal of the standard build
measured 30 467 842 bytes, 0.855 seconds, process peak RSS 499 420 KiB.
By the existing 80% fill rule, the local `index.max_pages` was set
to 5 395; the byte limit was kept. A repeated standard build confirmed that
the publication-filtered index is current. The private config is not published.

The first part of the full regression ran into an almost full disk. Its results
were kept; the remaining checks use a separate local tmpfs. Temporary
data of the finished process were deleted after checking that the owners had finished;
samples of the two failures were kept. The standard fenced retention removed three outdated
index registrations of this checkout, keeping the last two; this did not free
significant disk space. The live `run/` and the source memory were not deleted.

## Sources

- [Research of boundaries and laws 4, 6, 7](2026-11-02-module-boundaries-validation.md).
- [The module contract](../modules/formats.md), `tests/modules/formats/`.
- Private decision: `knowledge/notes/isolated-formats-module-decision-20261102.md`.
- Local reproducible results: `logs/formats-pilot-*.txt`,
  `logs/formats-pilot-*.xml`, `logs/formats-pilot-*.json` (not published).
