# The archive publication guard reads each source once

Research and local qualification date: 2026-10-07.

The full Python 3.10 shard 3 at source revision `11a2356ff6d579410d424f485ded5b4220c34920`
finished with 2,877 passed, 49 platform skips, and one diagnostic warning in
1,367.74 seconds. Its archive publication guard took 637.72 seconds. The existing
600-second fault-handler diagnostic located the main thread in
`ast._splitlines_no_ff`, called by `ast.get_source_segment` for each assignment.
This is verified local evidence. It is not a traceback from the cancelled CI
jobs: their logs and progress artifacts were unavailable. That workflow ended
with 39 successful jobs, 18 cancelled full-suite jobs, and a failed aggregate.

The guard parses every script and refuses directory publication outside the
daily archiver. Extracting each assignment with `get_source_segment` repeatedly
split the entire module. A new causal control on the original scanner measured
21 complete source preparations for 21 assignments and failed its invariant
that source preparation must not grow with assignment count.

The scanner now prepares physical UTF-8 lines once per module and slices the
same parser-provided byte positions. It preserves CR, LF, CRLF, form feeds,
multiline source, missing-location handling, lexical inspection, the complete
script set, and the original offending-publisher assertion. There is no global
cache, new dependency, runtime change, discarded assignment, or data limit.

Alternatives considered were raising CI deadlines, excluding this guard, using
`ast.unparse`, and caching source globally. Raising a deadline would leave the
repeated work; excluding the guard would lose protection; unparsing would change
the physical source it inspects; global caching would add invalidation work.
Per-module preparation preserves the existing evidence with a simple bounded
lifetime.

Python 3.10 AST and Lizard measured every function in the changed test module
before execution: 57 callables, maximum CCN 4, at most two `if` statements,
maximum `if`/`for`/`while` depth two. Ruff passed. Twenty-one focused controls
passed, including comparison with the standard source extractor for Unicode,
all three physical line endings, form feeds, multiline source and annotations.
The full security invariant module and the global Python/JavaScript branching
checks passed together: 93 passed in 52.33 seconds. This is a related local check,
not full-suite or cross-platform CI completion. Both the original slow run and
the failing causal control are retained.

Primary sources checked on the research date:

- [Python 3.10 AST positions and source extraction](https://docs.python.org/3.10/library/ast.html):
  parser columns are UTF-8 byte positions, so character-based slicing is wrong
  after non-ASCII text.
- [Python 3.10 text streams](https://docs.python.org/3.10/library/io.html#io.StringIO):
  `StringIO` with `newline=""` recognizes universal line endings without
  translating their original bytes when the lines are subsequently encoded.
- [pytest fault-handler diagnostics](https://docs.pytest.org/en/stable/how-to/failures.html#fault-handler):
  the existing timeout emits thread stacks; it does not establish a failed test
  or justify removing its assertions.
- [GitHub Actions job deadlines](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idtimeout-minutes):
  the configured deadline cancels a job. A cancelled matrix lane is not a pass.

Python's two pages are one independent source; pytest and GitHub are the other
two. The installed qualification interpreter was CPython 3.10.20. The consulted
3.10 documentation reports 3.10.22; the relevant positions and newline behavior
were checked on the installed interpreter by the parity controls.

Installed-tree verification initially completed all 93 assertions but failed its
session teardown because concurrent lifecycle capture created six new private
session breadcrumb files in the running vault. The failure is retained; it is
not a successful run. The live-vault write guard remains unchanged. A separate
verification uses the byte-identical installed test file and the same baseline
product sources in an isolated checkout, away from live capture. An initial
isolated launch used the wrong relative copy source and was interrupted; that
launch is also retained and excluded from acceptance.

The corrected isolated verification of the byte-identical installed test module
completed with 93 passed, zero failures, errors or skips in 51.18 seconds on
CPython 3.10.20. This is the installed component's acceptance scope; it does not
replace full-suite or hosted cross-platform qualification.
