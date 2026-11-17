# Queue timing: time normalization and retry calculation

Date: 2026-11-02. Status: This note cites no owner message, so it records no owner approval. Research: 1a1f50b4; move: after the fix 2ea7fa24.

## The proposed move

Names as of 2026-11-17: the split of the queue that followed this move made the
functions public in `scripts/llm_wiki/queue_timing.py` (`as_utc`, `timestamp`,
`parse_timestamp`, `retry_after_seconds`, `retry_available_at`, `RETRY_RANDOM`),
`utc_now` lives in `scripts/llm_wiki/queue_store.py`, and the identity aliases and
`MemoryQueue._retry_after_seconds` named below no longer exist. The text below
records the move as it was proposed.

The new scripts/llm_wiki/queue_timing.py owns the existing _as_utc,
_timestamp, _parse_timestamp, eight functions that read Retry-After and
_retry_available_at. _RETRY_AFTER_READERS,
_MAX_RETRY_AFTER_SECONDS and _RETRY_RANDOM move with them. 12 functions in total, with unchanged bodies.
Dependencies: datetime, email.utils, math, random and the already extracted DEFAULTS.
No imports of the queue, storage, SQLite, filesystem/network IO.

_utc_now stays with the queue: reading the current time is the source of the value,
and the module computes the result from the passed now. This way the current
clock monkeypatches in the corruption/capture tests keep working.
The queue keeps the SQL, the attempt count and the ready/dead/blocked transitions;
MemoryQueue._retry_after_seconds stays as the existing compatibility method.
The old function names are available through direct identity aliases. In the
failed capture test the monkeypatch of _RETRY_RANDOM moves to the new owner;
no redirection of globals and no repeated random number generators are added.

Preserved: naive-as-UTC, timezone conversion, microsecond formatting,
None parsing, the bool-before-int order, HTTP-date parsing, the seven-day bound,
full-jitter, DEFAULTS and max(jitter, Retry-After). For non-ready, the same now
is returned without computing a delay. The legacy/adopted queue policies are not merged:
legacy uses its own configurable random/retry policy, adopted uses its current one.
There are no new limits, timers, daemon, or schema/env/runtime-path changes.

## Alternatives and grounds

Leaving everything in the queue is possible, but it keeps an executor dependency for calculations
over values. A copy of _as_utc in a retry module would break the single owner.
Separate modules for each of the three small time functions would increase the number
of boundaries without an independent responsibility. A new shared scheduler, Tenacity/asyncio
or a change of the jitter strategy is not needed to keep the current semantics.
The choice is one small module of queue time values, without performing work.

Research of independent primary sources on 2026-11-02:

- [RFC9110 §10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3):
  Retry-After allows an HTTP-date or a non-negative integer number of seconds.
  The current API additionally accepts datetime/float; this is an existing contract,
  and it is not narrowed to the HTTP grammar during a structural move.
- [AWS Exponential Backoff And Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/):
  compares full/equal/decorrelated jitter; full jitter reduces the combined
  load compared with retries without jitter. This is a 2015 study,
  not a reason to change the current strategy or to promise a speedup without measurements.
- [Python 3.10 datetime](https://docs.python.org/3.10/library/datetime.html):
  a naive datetime does not define a timezone by itself; interpreting naive as UTC here
  is the application's contract, not behaviour guaranteed by the standard.
  Python 3.10 is unsupported upstream; the project floor is kept and checked.

## Evidence and verification plan

The index 1a1f50b4 is current (changes:stale=false). The built-in code_graph from a new
process examined the callers of _as_utc, _retry_after_seconds and _retry_available_at;
the graph is incomplete, and the sources supplemented the imports, the current legacy glue and monkeypatches.
Evidence: logs/queue-timing-graph.json; logs/queue-timing-inventory.json.
Baseline memory_queue, adopted Retry-After, small integrity gaps and failed capture:
127 passed, 2.96s. Evidence: logs/queue-timing-baseline.txt.

Order: record the private decision and STRUCTURE before the code. Move
the bodies without changes and confirm the AST; delete the old definitions. Add
tests/modules/queue_timing, docs/modules/queue-timing.md and a context manifest.
Own tests: timezone/naive/None/microseconds; bool, negative, NaN/Inf, HTTP-date,
past/future/invalid Retry-After; exact jitter bounds on a controlled generator;
non-ready does not touch the generator; an isolated import without runtime.
The boundary guard must fail before the move and pass after it. The integration checks
aliases, the old clock substitutions, legacy/adopted retries and an independent next capture.

The final package includes all own tests, documents, instructions and the DEFAULTS source;
100000 o200k_base is the owner's ceiling, not an estimate of the size of a package not yet created.
Ruff, real global CCN/shape, CI weights/discovery, Python 3.10 and the related tests
are mandatory. After commit/push, update the index and the private log. Do not claim a live rollout,
the full suite or native platforms without actually running them.

The installed system stays on e95f6a4a; its known nightly/capture problems
are not solved by this move. The candidate worktree is separate; private knowledge is not
published. The size of the queue source before this step is 120706 tokens; this move
does not by itself complete the overall decomposition.

## A numeric Retry-After error found before the move

Research of edge input on the original 1a1f50b4 reproduced a ValueError for
the string `²` and an OverflowError for the integers ±10**400. Unicode isdigit accepts characters
that float cannot read; the numeric conversion ran outside the protected section.
The second case is caused by converting an arbitrary Python int to a bounded
float before the existing bound was applied. The error occurs in queue.fail,
so the transition out of leased does not complete. This is an existing defect, not a result of
the move. Reproduction: logs/queue-timing-edge-baseline.json and
logs/queue-retry-numeric-red.txt (6 failed, 7 passed, 112 deselected).

A separate fix in the current memory_queue.py does not change the structure:
an integer is bounded before float, and the conversion of numeric text is included
in the already existing parsing error handler. A huge positive integer
gets the old cap of 604800 seconds; a negative one stays invalid;
an unconvertible string does not set Retry-After. The other finite/NaN/Inf and HTTP-date
rules are kept. No new limit was added. A rejected delay keeps
the old jitter; the task's failure is not masked as a successful run.

Regressions check the shared parser and both queue implementations: the task leaves leased
for ready, drops the lease token, keeps the failed attempt and respects the retry time.
The project test of an independent next capture is included. At the time of the separate fix, the structural move
of queue_timing was not yet made; its AST baseline must be taken after
this separate fix, not pretend to match the faulty version.


Qualification of the separate fix: 147 passed, 36.06s (including global CCN/shape);
Python 3.10: 136 passed, 3.43s. Ruff is clean. Evidence:
logs/queue-retry-numeric-related.txt/.xml; logs/queue-retry-numeric-python310.txt.
This is not the full suite, not native Windows/macOS and not a live rollout.


## The move

The private decision and STRUCTURE were recorded before the code. The index 2ea7fa24 was refreshed before the move. The AST of all
12 functions and three constants was kept; the rest of the queue AST matched after excluding imports.
The boundary guard first failed on the old definitions and passed after the move.
The only substitution of _RETRY_RANDOM moved to the owner; _utc_now stayed
in memory_queue. The old function names are checked as identity aliases.

48 own tests passed: UTC/offset/microseconds/None, the kept and fixed
Retry-After cases, deterministic edge points of jitter and cap, the stated floor,
non-ready without RNG and an isolated interpreter without executor/storage imports.
Initial full package:72065 o200k_base tokens/26 files.
Evidence: logs/queue-timing-preservation.json; logs/queue-timing-boundary-red.txt;
logs/queue-timing-own.txt; logs/queue-timing-related.txt/.xml;
logs/queue-timing-python310.txt; logs/queue-timing-final-budget.txt.


Qualification of the move: 301 passed, 64.19s (own, queue, migration, corruption,
retry/capture, global complexity/shape, structure and CI); Python 3.10:
182 passed, 3.82s (own, queue, adopted retry and failed capture).
Ruff and git diff --check passed. The AST of the functions also matched after import formatting.
All 12 full packages are below 100000; memory_queue.py is now 119996 tokens.
The final budget and CI weights were checked separately. Duplicate definitions were deleted;
the old function names are needed by the compatibility surface, and the RNG alias is not kept.
The live installation, the full regression suite and native platforms were not qualified.
