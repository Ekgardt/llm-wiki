# A capture keeps its claim while it asks

Dated 2026-09-14. Items 3.1 and 2.4 of `docs/AUDIT-2026-09-14.md`. The research
before the fix.

## What was measured

From `run/queue-v3.sqlite3`, `attempt_history` joined to `tasks` of kind `flush`
(read-only):

| outcome | attempts | shortest | median | longest | longer than 30 s |
|---|---|---|---|---|---|
| succeeded | 43 | 0 s | 14 s | **30 s** | **0** |
| failed, `processor_failed` | 225 | 0 s | 0 s | 91 s | 27 |
| lease expired | 5 | 130 s | 191 s | 1 362 s | 5 |

Not one capture that took longer than 30 seconds has ever succeeded. 25 flush tasks
are dead after 8 attempts. `logs/capture-failures.jsonl` holds 7
`intent_fence_lost` rows between 2026-09-07 and 2026-09-12, three of them marked
`lost`.

## The mechanism, read in the code

The worker (`flush_memory.run_capture_worker_once`) holds, for the whole of one
capture:

- an owner in the registry, role `queue-worker` (120 s lease), projected into the
  queue (`queue.queue_owner`);
- the queue lease from `claim_capture` (`DEFAULTS.queue_lease_seconds` = 120 s);
- a task fence (`acquire_task_fence`, `min(owner.expires_at, now + 120 s)`);
- an intent fence (`MarkdownCoordinator.acquire_intent_fence`,
  `min(owner.expires_at, now + 30 s)`).

Then `process_new_capture` calls the classifier (`_call_capture_classifier`,
`llm_client.call_llm_result`, up to 90 s per provider with fallback) and only
afterwards publishes the decision and the terminal, and both require the fences
live (`_require_live_intent_fence`: `expires_at > now`). Nothing renews any of the
four during the call: no heartbeat is started, and there is no renewal method for a
task fence or an intent fence at all. So a classification over 30 s loses the
intent fence, one over 120 s loses everything, the attempt is failed with
`retry_after=0`, the provider is paid again, and after 8 attempts the session's
tier is never decided (its raw record is already kept by `_keep_session_record`).

The renewals that do exist: `queue.heartbeat(lease)`,
`queue.heartbeat_queue_owner(owner)` (registry row and queue projection together),
and `heartbeat_source_fence` for source fences — the pattern the missing two
follow.

Item 2.4, the flush grammar: `_parse_capture_wire_output` accepts `FLUSH_OK` alone
on its line and nothing after it, or `FLUSH_MAJOR`/`FLUSH_MINOR` alone on the first
line with a body below. `FLUSH_OK.`, `FLUSH_OK` followed by a sentence,
`FLUSH_MAJOR: body` on one line, or one line of preamble before the tier are each
refused as `invalid flush output`, which costs another full attempt. How many of
the 225 failures are this is not recorded; the cause of a failed attempt is not
stored.

## Practice on this date

- A lease holder "must renew it with a heartbeat before the timer runs out", with
  the TTL several heartbeats long so one missed renewal does not lose it
  ([lease pattern](https://singhajit.com/distributed-systems/lease/),
  [lock renewal with a heartbeat](https://oneuptime.com/blog/post/2026-03-31-redis-lock-renewal-heartbeat/view)).
  The two alternatives — a TTL longer than the slowest call, or releasing the claim
  during the call — trade safety for liveness: a long TTL keeps a dead worker's
  claim alive as long, and a released claim lets a second worker start the same
  paid call.
- This file's own ladder for model output is tolerant to decoration and strict to
  meaning: `_TIER_EMPHASIS` already strips `**FLUSH_MAJOR**` because "under the
  literal rule both sessions would have been destroyed as invalid output"
  (2026-08-27), and `_require_canonical_body` stopped refusing trailing whitespace
  after nine lost sessions. The same reasoning covers a trailing full stop or colon.

## The decision

1. **Renew every claim the capture holds while the classifier runs.**
   `_QueueV3CandidateReader.heartbeat_task_fence(fence, owner)` and
   `MarkdownCoordinator.heartbeat_intent_fence(fence, owner)` push a live fence's
   expiry out by its own length, capped by the owner's current expiry, and refuse a
   fence that is gone or already expired — the same shape as
   `heartbeat_source_fence`. `flush_memory` runs one keep-alive around
   `_call_capture_classifier`: every 10 seconds (a third of the shortest lease, the
   intent fence's 30 s) it renews the owner and its projection, the queue lease, the
   task fence and the intent fence, in that order. One failed round is tolerated;
   two consecutive failures stop renewing, and publication then fails fenced as
   before — a lost claim still never publishes.
2. **Read the tier, not the typography.** The tier line may carry trailing
   punctuation and, for `FLUSH_MAJOR`/`FLUSH_MINOR`, its body may begin on the same
   line after a colon. `FLUSH_OK` followed by text is still `ok` — the text is the
   model explaining that there is nothing to keep. The tier must still be declared
   by the first non-blank line: the classifier reads untrusted transcripts, and a
   tier token quoted from one further down must not decide, so a preamble is still
   refused, as is a reply that declares no tier.

Why not the alternatives:

- **Raise the intent fence to 120 s.** The slowest measured attempt was 1 362 s;
  no constant is long enough, and a dead worker would hold the intent that long.
- **Classify before taking the fences.** It changes the processor contract that
  the capture tests and the operator paths use, and the queue lease still has to be
  renewed during the call, so it adds a split without removing a heartbeat.

Files: `scripts/memory_queue.py`, `scripts/markdown_transaction.py`,
`scripts/flush_memory.py`, `tests/test_a_capture_keeps_its_claim_while_it_asks.py`,
`docs/research/2026-09-14-a-capture-keeps-its-claim-while-it-asks.md`.
