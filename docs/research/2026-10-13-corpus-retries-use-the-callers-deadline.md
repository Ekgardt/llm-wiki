# Corpus retries use the caller's deadline

Research and qualification date: 2026-10-13.

The installed collector stopped after four changed-source passes even when its
caller still had time. A real-file diagnostic produced four hash mismatches,
refused with 29.995 seconds of a 30-second budget remaining, and then captured
the settled file under that same deadline. A regression test reproduces this
with edits after capture and before membership/content verification. The
unmodified collector fails that test.

The live health check also exhausted four membership passes. This establishes
the premature-refusal defect, not that continued retries will make a continuously
edited vault settle or that this change fixes overall compilation latency.

## Sources and alternatives

- [Python monotonic clock](https://docs.python.org/3/library/time.html#time.monotonic)
  provides elapsed-time accounting independent of wall-clock adjustments. The
  [Python 3.10 documentation](https://docs.python.org/3.10/library/time.html#time.monotonic)
  confirms compatibility with the product's minimum Python version; that version
  is now unsupported upstream, and this change does not change the version contract.
- [SQLite isolation](https://sqlite.org/isolation.html) describes database
  snapshot isolation. Markdown files do not inherit it: replacing file-content
  verification with an SQLite transaction would not establish source coherence.
- [Amazon's timeouts and retries guidance](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)
  explains bounded retries and their resource cost. Remote-service backoff is not
  transferred blindly to local file reads, and no arbitrary sleep is added.

Raising four to another number leaves the same premature refusal. Accepting a
torn snapshot weakens source authority. Taking a filesystem snapshot or changing
writer admission requires a separate architectural decision. The selected change
repeats the existing full capture proof within the caller's existing finite
monotonic deadline, with cancellation checked between attempts and during capture.

NaN and infinite supplied deadlines are refused because they cannot bound retry
work. No new setting, runtime path, database, or default duration is introduced.
Continuously moving sources still fail at the deadline. Stable membership,
complete source hashes, safe reads, and the writer gate remain unchanged.

Full CI of the initial candidate exposed a compatibility error: exhaustion after
observed source changes became an ordinary timeout rather than the named
changed-checkout refusal. The final collector preserves `CorpusChanged` with
the timeout chained as its cause when earlier attempts observed changes.
Exhaustion without an observed change remains `TimeoutError`; cancellation is
distinct and remains a `TimeoutError` subclass. No error text is used to decide
between those cases.

The shared repository pass also gave every checkout its entire remaining budget.
Bounded retries therefore allowed one busy checkout to spend the following
checkout's time. Each registered checkout now receives its equal share of the
remaining time among the remaining rows. Time not spent by a quick checkout is
available to the next ones. This is an explicit fairness tradeoff within the
existing caller-selected budget, not a new fixed duration or attempt ceiling.
Heavy checkouts can still be deferred; per-repository indexing remains available.
Weighted allocation would require trustworthy measured costs and remains a
possible later refinement.

Additional independent guidance reviewed before this adjustment:
[Google SRE overload handling](https://sre.google/sre-book/handling-overload/)
on preventing one client from exhausting shared resources, the Python exception
chaining documentation, and Amazon's bounded-retry guidance above. Equal shares
are the project's scheduling choice, not a number prescribed by those sources.

## Qualification and limits

The regression suite covers settling after four actual file changes, exhaustion
of the unchanged deadline, cancellation before another attempt, and nonfinite
inputs. The Windows pathname race fixture still mutates after every read; a
controlled monotonic clock proves deadline refusal without a timing-sensitive
short real-time budget. It additionally asserts that reads occurred.

Changed production functions have measured CCN at most five. This is a collector
correction; successful full maintenance, new-work compilation throughput, and
the remaining point-seven obligations require their own evidence.
