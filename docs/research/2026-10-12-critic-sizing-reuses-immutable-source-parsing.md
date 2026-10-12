# Critic sizing reuses immutable source parsing

Date: 2026-10-12. This is a candidate qualification, not an installed fix or
completion of point 7.

## Observed cause

The installed full compile's live stack entered `daily_entries` through
`_sole_block_span`, `_bound_native_evidence`, `_critique_prompt` and
`_CompileAttempt._batch_fits`. Greedy critic packing repeatedly renders growing
operation batches. Each rendering reparsed the same captured source bytes.
The original regression reproduces repeated journal and daily-entry parsing.
The corrected original test run failed two assertions and passed one; a previous
run with an incorrectly constructed provider descriptor is not causal evidence.

The first candidate still reparsed entries through structured claim validation.
The common captured-byte resolver now covers that path too. The five related
test files pass: 45 tests. This establishes removal of redundant parsing in the
tested sizing path, not the speed of a complete compile. Ordinary batch refresh,
publication, provider calls and generation building have separate costs.

## Research and alternatives

Primary sources checked on 2026-10-12:

- [Python caching semantics](https://docs.python.org/3.10/library/functools.html):
  retained arguments and invalidation matter. Python 3.10 is now unsupported
  upstream; this project nevertheless promises compatibility with it.
- [Java identity-map semantics](https://docs.oracle.com/en/java/javase/25/docs/api/java.base/java/util/IdentityHashMap.html):
  object identity differs from value equality; identity keys require retaining
  the actual objects. This is a semantic reference, not a proposed dependency.
- [Guava cache guidance](https://github.com/google/guava/wiki/CachesExplained):
  cache ownership, retention and loading scope require explicit choices. No
  Guava dependency or global cache is introduced.

Reparsing everything preserves freshness but repeats pure work during sizing.
A process-global cache complicates lifetime and mixes independent attempts.
Caching permanent breadcrumb authority would miss external changes and is
rejected. The candidate instead reuses the existing `_ByteBatchMeasure` inside
one `_CompileAttempt`, only while measuring critic capacity. It keeps strong
references to unchanged captured bytes. It adds no numerical limit, runtime
location, environment contract, provider request, prompt format or dependency.

## Preserved checks and remaining qualification

Each sizing call still verifies native frame and permanent head authority,
source identity, byte span and complete literal quote. Changing a permanent
head after the first sizing call still refuses. Separate attempts do not share
parsing. Context variables reset after sizing; actual critic rendering outside
that scope reparses independently and produces the identical serialized prompt
in the regression fixture. Unresolved sources must still fail; this change
does not manufacture empty successful plans.

Local Lizard and AST analysis passes the user's CCN, branch-count and nesting
requirements. The new test file has a locally measured shard weight. The
candidate remains in an isolated checkout while the installed full compile
runs. Expanded source and partial-quote guards pass: 52 tests on Python 3.10.

Paired offline timing with identical serialized prompts measured 100 sizing
calls on a small fixture at 0.609 seconds originally and 0.684 seconds with the
candidate: the small case regressed. On an immutable 5,000,713-byte physical
fixture, ten calls measured 2.054 seconds originally and 0.144 seconds with the
candidate. This is a 14.23-fold improvement of that component only. Neither
measurement calls a model or publishes knowledge. A broader compiler test run
initial broad run found eight failures. Two tests constructed attempts through
`__new__` and bypassed initialization; they now use the real constructor while
retaining their capacity and refusal assertions. Six also reproduced on the
installed original; the process inherited `MEMORY_LLM_PROVIDER=codex`, whereas
these deterministic tests require `fake`. The hermetic rerun, with that
documented test provider explicitly selected, passes 666 compiler tests on
Python 3.10. This does not replace a production model test.

## Optional-context selection

Read-only profiling on a sealed real daily source found 2,846 optional-context
proposals, each deriving the same complete source choices again. The profiled
run took 54.87 seconds; profiling overhead means this is not a timing baseline.
The choice collector accounted for 40.86 seconds including its callees.

The candidate retains only the last computed choice tuple within the existing
measurement scope. Every lookup first scans the current full protected base,
validates selected source projections and original part bytes, and keys the
result by retained part/source identities, selected ranges, aligned original
and protected rows, and their actual prompt line numbers. Owners remain strongly
referenced. Returned dictionaries and native selector dictionaries are copied;
caller mutation cannot corrupt retained rows. Context before a daily can change
line numbers without changing character offsets, so those numbers are explicitly
part of the key. Unscoped layouts still derive choices independently. Final
prefix protection, actual model requests and publication checks remain fresh.

A causal regression with the installed original collector repeats derivation
63 times for 21 source rows across three optional-context proposals; the
candidate derives them once. Tests also cover changed protected aliases,
forged source bytes, mutable returned native selectors and equal-length context
with different newline counts before a daily source.

An unprofiled paired measurement used exactly the same immutable real input:
14,246,417 source bytes and 2,848 context pages. Original selection took
26.319 seconds of wall time and 26.247 seconds of CPU; the candidate took
15.461 seconds of wall time and 15.459 seconds of CPU. Both selected the same
page and produced the identical serialized prompt. The reduction is about
41 percent for this selection component. This is not a full-refresh or
whole-compile speedup, and there were no model calls or knowledge writes.

The profile also showed repeated general-format calendar parsing. The candidate
uses the standard library's ISO calendar parser with canonical round-trip
validation, keeping the existing Gregorian date and spelling contract. Research
on 2026-10-12 checked [Python 3.10 datetime](https://docs.python.org/3.10/library/datetime.html),
[RFC 3339](https://www.rfc-editor.org/rfc/rfc3339), and
[Microsoft exact date parsing](https://learn.microsoft.com/en-us/dotnet/api/system.dateonly.parseexact?view=net-10.0).
Alternatives were retaining general format parsing, adding a persistent date
cache, or writing calendar rules manually. The standard-library parser requires
no cache, dependency or new limit; strict round-trip checking prevents broader
ISO spellings accepted by newer Python versions from widening the contract.
Tests cover the complete 400-year Gregorian cycle, supported year boundaries,
invalid leap days and noncanonical spellings.

Installation, complete regression/CI, code-index refresh, cleanup and a real
whole-cycle qualification remain required. The installed compiler file has not
been changed while its observed full pass remains live.

## Complete local run and qualification harness

The complete local Python 3.10 run for `ffa3d717` returned two failures,
12,573 passes and 236 skips in 2,498.74 seconds. Both failures inherited the
operator's explicit `MEMORY_CODEX_MODEL` while testing implicit model selection.
The two test files passed all 49 cases when that inherited setting was removed.
The affected tests now explicitly clear it within their own monkeypatch scope;
the operator's runtime configuration and model-identity refusal remain intact.

CI run 38259514455 failed the concurrent project-journal test at ordinary writer
admission. Its loop already gives a caller `LONG_TIMEOUT`, but `checkpoint`
received no `writer_wait_seconds`, so publication used the product's ordinary
five-second wait. The failing case took 10.21 seconds under CI load. A spy
regression observes the real publication call and verifies that the caller's
existing wait actually reaches it. The original helper failed this invariant;
the amended helper and unchanged concurrent journal postconditions passed five
prototype cases. All 152 project-journal and related Codex cases pass with the
fixture corrections. A first probe that injected refusal into every publication
entered repeated recovery and was terminated; it is not qualification evidence.

The helper now passes the remaining duration of its existing caller deadline.
The transient-contention wrapper forwards that duration. No timeout constant,
production default, exception filter, retry count or append-once assertion was
changed. Research on 2026-10-12 checked independent primary guidance from
[Python futures](https://docs.python.org/3.10/library/concurrent.futures.html),
[Go context](https://pkg.go.dev/context), and
[Rust condition variables](https://doc.rust-lang.org/std/sync/struct.Condvar.html).
Increasing global defaults, swallowing arbitrary timeouts, and subtracting
matching base/candidate failures were rejected. Passing the caller's existing
budget expresses the scenario the test already declares. This is a correction
of the test caller, not a claim that production writer admission was changed.

The full local run initially produced about 2.2 GiB of disposable pytest data.
Normal pytest temporary-directory retention removed that completed run on the
subsequent invocation; disk availability recovered from 1.8 to 3.9 GiB. No
installed operational state or private knowledge was removed for this check.
The revised CI must finish successfully before installation.

## Windows shard costs

Run 38265297933 cancelled Windows Python 3.12 shard 3 at its existing
60-minute job bound, after 3,094 passes and 61 skips. Its ten-minute diagnostic
stack sampled SQLite commit during publication of genuine large-day receipts.
The fixture subsequently completed: setup cost 791.93 seconds and the complete
module cost 794.238 seconds. This is not evidence that one SQLite commit hung
for ten minutes. Successful Windows Python 3.14 and 3.13 runs measured the same
module at 301.464 and 410.771 seconds. Its recorded shard weight was 78.35 seconds.

The candidate refreshes scheduling data from 34 completed current-run JUnit
reports. From the interrupted report, only the fully completed large-day module
is admitted; the interrupted final test file is excluded. Values increase to
the measured maxima and none decrease. The existing longest-processing-time
planner then balances its four groups at about 3,222.78 seconds each. This is
a scheduling estimate, not a promise that a future runner finishes in that time.
The job bound, tests, assertions, SQLite durability and complete source fixture
are unchanged. The preparation still creates genuine transaction-authoritative
receipts; replacing them with synthetic success was rejected. The new scheduling
data requires a fresh complete CI run before installation.
