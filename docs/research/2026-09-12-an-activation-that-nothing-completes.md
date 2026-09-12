# An activation that nothing completes is not incoherence

Date: 2026-09-12. Trigger: CI run 34668597688 was red on one Windows job with
`tests/test_blackboard.py::test_multiprocess_status_reads_remain_coherent_during_claim_and_complete`
— `assert status["active_tasks"] == 0`, measured 1. Every writer had returned
its full batch (the assertion before it passed), so all twenty-four claims were
completed as far as their callers were concerned, and one task was still active.

## What the product actually promises

`scripts/blackboard._settle_unannounced_claim` documents this exact outcome, in
its own words: when an append commits and then raises, and the stream cannot be
read back before the budget ends, the claim's resource rows are released, and
the cost is "one activation that never completes". The trade is deliberate and
argued there: the alternative — leaving the rows — blocks that resource set
until the lease expires, needs only one condition, and is silent.

So a task that stays active with no resources behind it is the product keeping
its promise under contention. What would be incoherence is the opposite: a task
that is active *and* still holds resource rows that nothing will ever release,
or a completion that is missing while its claim is alive.

The test asserted the stronger thing — zero active tasks — which holds on Linux
and macOS and on Windows most of the time. Six processes on a hosted Windows
image, where each append hardens files with `icacls`, reach the documented
outcome instead. Five local runs on this machine passed; the shape only appears
where the writer gate is that slow.

## Decision

The test states the invariant the product guarantees, and names the one it
does not:

* every claim a writer completed has its completion record, and
* any task still active holds **no** resource rows — its claim was released,
  and `blackboard.get_status` will keep showing it until the stream is
  compacted, which is the documented cost, not a lost write.

Nothing in `scripts/` changes. The contention retry, the settle path and the
lease fence keep the behaviour they have, because the behaviour is the one
`_settle_unannounced_claim` chose on purpose.

## Rejected

* **Loosening the assertion to `<= 1`.** A number with no reason behind it
  would pass a real lost completion just as happily.
* **Removing the release.** It would trade a rare, visible leftover for a
  certain, silent block of one resource set for the whole lease — exactly the
  trade the product already rejected, in writing.
* **Retrying `get_status` until it reads zero.** The activation is durable;
  no amount of waiting removes it, so the wait would only hide the case.

Files: `tests/test_blackboard.py`.
