# An expired fence is not an obstacle to adoption, and a refusal says why

Dated 2026-09-18. Finding Q-L3 of the third audit (operational core, v2 → v3 adoption).

Files: scripts/memory_queue.py, scripts/installed_memory_repair.py,
tests/test_an_expired_v2_fence_does_not_refuse_adoption.py

## What was found

`_require_unambiguous_v2_owners` refuses to build a v3 candidate while the v2 queue holds any
row at all in `source_fences`, or any `queue_ownership` row with a token:

```python
if "source_fences" in tables and source.execute(
    "SELECT 1 FROM source_fences LIMIT 1"
).fetchone() is not None:
    raise _migration_error("queue_v2_source_fence_ambiguous", ...)
```

The reason given is sound: a v2 fence has a pid but no process-start identity, so it cannot be
carried into v3, where every fence is keyed to one. But the check does not ask whether the fence
is *held*. A fence row survives the process that took it — that is why the live v2 queue runs
`_delete_stale_source_fences` on the way in, deleting every row whose `expires_at` has passed or
whose pid is gone. One crash during an archive run, and the vault can never adopt: every later
attempt refuses on a row that the live queue itself would have swept.

The operator cannot see any of that. `installed_memory_repair` wraps the whole adoption in
`except Exception` and reports one blocker, `reliability_v3_adoption_failed`, with
`adoption_state: conflict`. The exception it swallowed carried `code`
`queue_v2_source_fence_ambiguous` — the one word that would have told the owner what to look at.

## Practice on this date

A lease past its expiry is not evidence of a holder; it is evidence of a holder that existed.
That is the whole reason leases have an expiry, and it is the rule this product already applies
in every other fence it owns: `_delete_stale_source_fences` in the queue, `_reclaim_or_refuse` in
the ownership registry, the writer gate's reclaim of a dead projection. Kubernetes' leader
election states the same rule as a permission to take over, not merely as bookkeeping: "A client
needs to wait a full LeaseDuration without observing a change to the record before it can attempt
to take over"
([kubernetes/client-go, `tools/leaderelection/leaderelection.go`](https://github.com/kubernetes/client-go/blob/master/tools/leaderelection/leaderelection.go)).
Waiting the full duration is the price; refusing for ever afterwards is not part of the deal.
Pid-start identity is what distinguishes a *live* holder from a reused pid; it is not needed to
recognise a lease whose own clock has run out.

The second half is the product's own rule about operator-visible failures: `QueueOperationError`
exists so that a failure carries "a stable, non-sensitive code" separate from its message, and
the repair boundary already publishes blockers as bare codes. Dropping the code and keeping only
the generic one throws away the part that was designed to be shown.

## The decision

- `_require_unambiguous_v2_owners` refuses only rows that are still inside their lease. A
  `source_fences` row whose `expires_at` has passed, and a `queue_ownership` row whose
  `expires_at` has passed, are leftovers and do not block adoption. Nothing is deleted from the
  v2 source: the check only stops treating an expired row as a held one.
- A row with no `expires_at` at all still refuses. `queue_ownership.expires_at` was written but
  never read on the legacy path, so a null there means "cannot say", and cannot-say fails closed.
- `installed_memory_repair` adds the failure's own `code` to the blockers beside
  `reliability_v3_adoption_failed`, and only when the exception carries one as a plain string.
  No message text is published, so the redaction boundary is unchanged.
