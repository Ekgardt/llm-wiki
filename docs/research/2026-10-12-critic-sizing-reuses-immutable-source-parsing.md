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
