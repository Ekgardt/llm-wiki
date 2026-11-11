# Own-call transcript retirement uses its existing deadline

Date: 2026-10-01. Qualification: candidate; installed evidence is recorded separately.

The old nightly retirement enumerated at most 2000 transcripts, accumulated their
paths, classified the entire batch, and then deleted its own provider transcripts.
The count has no measured basis. Enumeration alone checked the existing deadline:
classification could exhaust it and deletion still proceeded. Two regressions
reproduce both defects on the previous implementation (2 failed, 4 controls passed).

Select a streaming iterator under the existing monotonic deadline. Verify each
candidate through the same entrypoint and working-directory checks; check the
deadline again before unlink. Keep unexamined or timed-out transcripts intact.
Remove the count constant and the replaced list accumulation. Storage layouts,
public APIs, scheduler and permission boundaries do not change. The existing
20-second scan budget is not newly introduced or established as an optimum here;
its numerical basis remains a separate unfinished audit question. A file operation
already in progress can exceed the deadline. Empty-directory cleanup retains its
existing behavior; this change does not claim a hard whole-command time guarantee.

Sources checked on 2026-10-01:

- [Python 3.10 time](https://docs.python.org/3.10/library/time.html?highlight=time+module)
  documents a monotonic clock unaffected by wall-clock updates.
- [Microsoft background jobs](https://learn.microsoft.com/en-us/azure/well-architected/design-guides/background-jobs)
  discusses stopping acceptance of new work and preserving unfinished work.
- [Google SRE overload](https://sre.google/sre-book/handling-overload/)
  discusses bounded useful work and deadlines under load.

These support the deadline/continuation approach, not either numerical value.
The first Python and systemd fetches failed (503 and 403 respectively); systemd
is not counted as a checked source. The versioned Python documentation succeeded.

Alternatives: increasing the count merely moves the unsupported boundary;
introducing a new setting still lacks a numerical basis and expands the settings
contract; removing all time controls risks monopolizing maintenance. Streaming
keeps candidate storage constant and removes a redundant count without removing
resource protection. No daemon, timer, durable cursor or new runtime path is added.
A large directory dominated by retained sessions can still repeatedly consume the
deadline; this fix does not claim universal fairness or historical loss recovery.

Candidate qualification: 39 related tests passed, including both regressions,
foreign/held/malformed preservation and scheduler ordering. An initial invocation
named a nonexistent test file and collected no tests; the corrected run passed.
Full suite remains failed/incomplete for separately recorded environmental issues.
Do not run retirement against the read-only host transcript directory in this
session. Actual installed-module qualification must use owned disposable files.
