# Emitted layouts own their immutable parsing

Date: 2026-10-13. The installed compiler remains on `4580a9ba` while its full
cycle runs; this is a separately qualified candidate change.

Live stack samples reached the native journal and daily-entry parsers while
constructing actual draft requests. This observation is a limited sampled
window, not a measurement of their share of the whole compile cycle. Source
inspection found that sizing already owned pure parsing indexes, whereas the
emitted draft and critic layouts did not own that same lifetime.

Two corrected behavioral regressions fail against the immutable `ebcb8e98`
Git source: a draft containing two native frames parses their physical journal
twice; a critic containing two citations also parses one physical journal
twice. These cases require one parse, preserve the complete original request
and schema, and repeat an independent layout to require a new proof.

Early probes had two wrong assumptions: the native draft does not call the
daily-entry parser in the one-frame fixture, and that same fixture makes only
one permanent-head check. Their failed results are retained separately. They
are not evidence of production defects. The final regressions exercise the
two-frame draft and the critic's actual repeated parsing instead.

## Research and alternatives

Primary sources checked before choosing this change on 2026-10-13:

- [Python immutable memoization](https://docs.python.org/3.10/library/functools.html)
  explains reuse of pure computations and retained cache arguments. A process
  cache would extend large source lifetimes unnecessarily. The compatibility
  documentation now marks Python 3.10 unsupported; this change adds no new
  Python feature or version requirement.
- [SQLite isolation](https://www.sqlite.org/isolation.html) distinguishes a
  captured view from changing live state. Parsed byte positions are a view of
  their held byte object, not permission to reuse an earlier live-source check.
- [MITRE CWE-367](https://cwe.mitre.org/data/definitions/367.html) explains why
  resource state can change after a check. Live permanent-head validation and
  hash checks remain in their existing call sites; file metadata alone is not
  substituted for them. This does not claim an OS sandbox or atomic protection
  against hostile external edits.

[Python context variables](https://docs.python.org/3.10/library/contextvars.html)
additionally document token restoration. This is the same first authority,
not an additional independent source.

The existing journal and partition indexes, and the existing byte evidence
resolver, are composed into one operation-owned parsing context. Drafts,
critics and repair partitioning all use it. It holds strong references to
unchanged immutable bytes and restores the enclosing context on success or
failure. It creates no live-authority memo and does not populate the sizing
shortcut that bypasses projection reconstruction. Existing permanent-head
checks still run, complete quotes and citation spans are still checked, and
the protected request role, DLP policy, schema and transport remain unchanged.

Alternatives: reparsing every citation was the baseline; a global cache risks
stale authority and unnecessary retention; copying a second parser duplicates
the existing implementation; caching successful live checks would weaken
source-presence guarantees. The selected context adds no setting, numerical
limit, dependency, runtime location or persistent state.

## Qualification

The tests compare requests with the original unscoped body, not with an
abbreviated fixture. Negative cases change a permanent head between layouts
and during the critic's actual repeated checks, and require refusal plus
context restoration. Existing sizing, incomplete-quote, selected native claim
origin, source replacement and repair feedback tests remain applicable.

Full platform CI, installation and a real whole-cycle speed comparison are
separate requirements; local parser counts do not qualify them.

Local results: 144 related and structure/weight checks passed in 20.46 seconds;
a separate protection, role and complete-quote selection run passed 124 tests
in 5.62 seconds. These runs overlap and are not added together. Ruff passed.
Lizard measured maximum CCN 4 across the changed functions and new test
functions, including the nested failure injector. AST measured at most two
`if` statements and nesting depth two. Two causal cases fail on the original
Git source; the four remaining source-presence and independent-layout cases
pass on that original source.
