# A stale plan needs new inputs, not another publication ordinal

Date: 2026-09-30. This is a correction to the existing optimistic publication
retry, with no new runtime contract, path, dependency, or retry limit.

## Reproduction and path

Compilation freezes daily sources and knowledge targets, resolves and critiques
a model plan, assesses its claims, then publishes under the writer gate with
hash preconditions. `_published` refreshes claim assessment after a rejected
precondition. Its factory still carries the same immutable `CompileInputs`.

Editing a target after the snapshot therefore produced four identical refused
transactions: no fresh claim assessment could make the old target hash match.
This occurred locally during concurrent backlink maintenance. Tests reproduce
both target rewriting and deletion: old code makes four attempts, while the
required result is one explicit refusal with no output or compile receipt.
A separately created output path already fails once with FileExistsError;
that existing protection is included as a negative regression, not a new bug.

## Sources and alternatives

Primary sources consulted on this date:

- [IETF RFC 9110, If-Match](https://www.rfc-editor.org/rfc/rfc9110.html#name-if-match):
  failed representation preconditions prevent lost updates.
- [Microsoft EF Core concurrency conflicts](https://learn.microsoft.com/en-us/ef/core/saving/concurrency):
  conflict handling must reconcile database values and the original version.
- [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html):
  restarting a transaction involves reconsidering its reads and decisions.

The latter two illustrate the concurrency principle, not technologies newly
introduced here. The existing Python, Markdown and SQLite stack is retained.
The installed transaction API already provides the required refusal and retry
identity; there is no reason to add another coordinator.

Blindly rebasing target hashes would authorize a model output against bytes it
never saw. Retrying the old plan repeats a known failure and spends processing
and retained transaction storage. Immediately rerunning the entire model loop
inside publication would duplicate the existing planning/cache/budget pipeline.

## Decision and acceptance

After `precondition_failed`, compare current target bytes with the input
snapshot before another publication attempt. A changed or missing target
rethrows the original refusal; no third-party edit is overwritten and the
source remains pending. A later planning pass takes fresh context through the
existing `_refresh_compile_batch` and input-bound cache key. Tests demonstrate
that fresh inputs can subsequently commit and produce a receipt.

Claim-tree-only changes remain retryable when the model's original targets
still match: the existing phantom-page regression must still succeed. Continuous
claim-tree movement must still fail. No safety precondition, receipt rule,
existing attempt limit or source coverage requirement is weakened.

The cost is rereading target bytes only after a failed attempt. The stale plan
is deliberately not rewritten or installed as a second implementation. Old
quarantines remain evidence; stopping unnecessary retries does not resolve
their unfinished input work or certify the whole local audit complete.
