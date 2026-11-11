# An installer deadline guard observes the call

Research and qualification date: 2026-10-13.

Windows Python 3.14 rejected the Pyright state-validation test: it received its
expected deadline exception, but wall elapsed time was 0.2446045 seconds against
an additional 0.12-second assertion. The test used that wall threshold to detect
an obsolete validator whose fixture sleeps 0.25 seconds. The installed production
module no longer calls that validator.

A controlled-clock reproduction debits 0.25 seconds when an actual `lstat`
returns. The real installer stops at its next deadline check; the old guard
still rejects it solely on its wall-clock discrimination. This identifies the
fixture's unreliable classification, not the particular Windows syscall that
was slow in that CI run.

Sources reviewed before the correction:

- [Python time semantics](https://docs.python.org/3/library/time.html)
  describes monotonic accounting and scheduling delays.
- [Microsoft file caching](https://learn.microsoft.com/en-us/windows/win32/fileio/file-caching)
  describes operating-system filesystem behavior outside application scheduling.
- [Google SRE overload handling](https://sre.google/sre-book/handling-overload/)
  describes resource-dependent cost and transparent failure under overload.

The deadline remains 0.02 seconds. The test now controls elapsed operation time
and directly asserts the contract: exactly one probe gets that exact absolute
deadline, the obsolete unbounded validator is never called, exhaustion raises
`TimeoutError`, and no code-tool cache is created afterward. This replaces the
unreliable indirect wall-time discriminator with stronger direct call checks;
it does not establish a 120-millisecond real-OS latency guarantee. Production
code, limits, and all other short-deadline tests remain unchanged.

A mutation guard restores the obsolete validator call and requires the updated
test to reject it. A separate real-filesystem guard models `lstat` returning
after the same deadline and requires refusal before the locking probe or state
creation. Neither test inserts a production delay or suppresses an error.

Local related qualification: 110 passed and nine platform skips. Eight changed
or new functions have measured CCN at most four, with the existing if and nesting
constraints preserved. Windows qualification and installation remain separate.
