# A timeout is a hang bound, not a stopwatch

Date: 2026-09-10. Status: research, then applied (see "Applied").

## Question

Three fixes today (f07d672, d9f5fa0, fb704bf) each made a red job green. The
owner asked whether they are crutches. This note asks what the field does
about the two things they touched — waits inside tests over real disk
work on slow runners, and a size cap two readers restated — and rewrites
the fixes to match, or says why not.

## What the field does

**CPython's test suite** keeps every wait in two named constants in
`test.support` and forbids literals. `SHORT_TIMEOUT` (30 s) is "to mark a
test as failed if the test takes 'too long'"; `LONG_TIMEOUT` (5 min) is
"to detect when a test hangs … long enough to reduce the risk of test
failure on the slowest Python buildbots. It should not be used to mark a
test as failed if the test takes 'too long'." Both scale with the
`regrtest --timeout` option, and the documented remedy for a random
failure on a slow buildbot is to move the wait from SHORT to LONG, not to
tune a number (https://docs.python.org/3/library/test.html,
https://bugs.python.org/issue38614).

**Bazel** gives a test a size, and the size a timeout — short 60 s,
moderate 300 s, long 900 s, eternal 3600 s — with two rules: "you should
generally set your timeout as tight as you can without incurring any
flakiness", and a test is never penalised for an overgenerous one, only
warned. A loaded machine is handled by `--test_timeout` at the
invocation, not in the BUILD file
(https://bazel.build/reference/test-encyclopedia).

**Fowler, "Eradicating Non-Determinism in Tests"**: never a bare sleep;
poll with a small interval and a wait limit high enough that hitting it
means "something serious has gone wrong"; and "the time values, in
particular the waitLimit, should never be literal values" — one
configurable constant for all tests
(https://martinfowler.com/articles/nonDeterminism.html).

**GitHub's hosted Windows runners** are the slowest supported machine and
getting slower: disk I/O on the C: drive is several times slower than on
the D: drive that windows-2025 images no longer have; a Bundler job went
from 1 h 10 min to 1 h 25 min between 2025-07-15 and 2025-07-24
(https://github.com/actions/runner-images/issues/12647,
https://github.com/actions/runner-images/issues/8755,
https://github.com/actions/virtual-environments/issues/3577). Every
commit in this product is `synchronous=FULL` with directory fsyncs, so a
fixture build that costs 0.1 s here is disk-bound there.

**Event sourcing** (Azure Architecture Center, Kurrent): the event log is
append-only and write-optimised; reads go to projections and snapshots;
"snapshots are an optimisation technique for the write model" and
coupling them to read models is a design smell
(https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing,
https://kurrentdb.kurrent.io/blog/snapshots-in-event-sourcing/). A
content index that scans the raw log is exactly that coupling.

## What this repository does today

- Waits in tests are literals: 5 s in the five tests that paused a thread
  inside SQLite work, 60 s in sixty-three fixture builds, 120/180 s where
  `01f5c3d` and `9c88bbf` had already failed once. Three rounds of "raise
  the number that failed".
- The CI job timeout (20–40 min per shard, `.github/workflows/tests.yml`)
  is the only hang bound that is not a literal in a test.
- A deferred generation build reports `reason=time_limit` and nothing
  about where the time went; on the Windows runner a two-file build hit
  60 s and no one can say in which phase.
- The claim index and lint scan `knowledge/projects/*/journal.md`, an
  append-only JSON event log that carries no claim ledger (checked on the
  live 4.2 MB journal: no `## Claims` section, the ledger parser finds
  nothing), and two of the three readers capped it below what the journal
  itself allows.

## Decision

1. **Two named waits, never literals.** `tests/slow_machine.py` becomes
   CPython's pair: `LONG_TIMEOUT` (300 s) for every wait a test asserts
   on — a thread to join, an event another thread sets, a build the test
   expects to finish — and `SHORT_TIMEOUT` (30 s) only where a test
   expects a refusal and must not wait five minutes for it. A worker's
   own pause is `PAUSE_TIMEOUT = 2 × LONG_TIMEOUT`, so the worker can
   never time out before the test that is waiting for it. All three scale
   by `LLM_WIKI_TEST_TIMEOUT_SCALE` (Bazel's `--test_timeout`, CPython's
   `--timeout`) for a machine known to be loaded. The literal budgets of
   today's earlier commits are replaced, not renamed.
2. **A deferred build names its phases.** `run_generation_maintenance`
   records the seconds spent in each phase — repository scope, corpus
   snapshot, parent lookup, build — and returns them in `details` on both
   a built and a deferred outcome. The nightly already prints `details`,
   so tonight's 15-minute deferral (task #10) and the next Windows
   deferral both say where the time went. The test budget stops being the
   only instrument.
3. **Journals stay out of the claim readers — proposed, not applied.**
   `PROJECT_CLAIM_FILES` naming `journal.md` is a contract
   (`2026-09-09-a-journal-rolls-by-size-too.md`); dropping it changes what
   the claim tree hashes and needs the owner's yes. Until then the three
   ceilings stay equal (d9f5fa0), which is consistent, not a crutch.

Not chosen: retrying flaky tests (hides the class); a per-test timeout
plugin (a new dependency; the job timeout already bounds a hang);
Defender exclusions or the D: drive on Windows (GitHub disables Defender
already; windows-2025 has no D:).

Open, recorded: `test_concurrent_workers_claim_each_row_once_per_lease`
failed once at 120 s under load 9–13 on this machine and passed 3 of 3
alone; with `LONG_TIMEOUT` it is bounded, not explained.

## Applied

Files: `tests/slow_machine.py`, `tests/test_memory_queue_migration.py`,
`tests/test_claims.py`, `tests/test_markdown_transaction_recovery.py`,
`tests/test_code_graph.py`, `tests/test_queue_v3_capture_links.py`,
`tests/test_generation_maintenance.py`, `tests/test_memory_queue_races.py`,
`tests/test_slow_machine.py`, `scripts/doctor.py`,
`tests/test_doctor.py`, `docs/ISSUES-2026-09-10.md`.
