# Who pays for an abandoned optional stage

Dated current-practice research for a design change on the hottest query path:
how the retrieval boundary should account for optional work that outlived the
wait the caller gave it. Read on 2026-08-26.

## The question this had to answer

`retrieval._run_optional_bounded` runs an optional stage — the dense leg, the
cross-encoder — on a daemon thread, waits a bounded slice of the caller's
deadline, and abandons the thread if the wait runs out. The thread keeps
running, because a Python thread cannot be cancelled and neither a model load
nor a vector search offers a checkpoint to cancel at.

Admission was one shared `BoundedSemaphore(MAX_OPTIONAL_STRAGGLERS)` = 2, and
the permit was released when the *work* ended, not when the *wait* ended. So an
abandoned stage held a permit for its whole run, and the next call was refused
before it waited for anything.

Measured on the live vault, six recall-shaped calls in one process at the 10 s
MCP budget (full trace in the branch's report): the dense leg reached the
answer in one call out of six, and four stage refusals read
`optional stage capacity exhausted` at 0.00 s. The two hogs are one-time model
loads — about 9 s for the multilingual embedding model, about 20 s for
`BAAI/bge-reranker-v2-m3` — against a stage budget of at most 5 s at that
deadline. One abandoned cross-encoder load therefore shut the dense leg out of
every call behind it, across a kind of work it had nothing to do with.

Three shapes were on the table: release the permit when the wait ends; size the
pool by the number of optional stages; or recognise a straggler that is only
warming a shared model as shared work rather than per-call work.

## What current practice says

**Abandoning work is normal; letting it amplify load is the failure mode.**
Dean and Barroso's hedged and tied requests deliberately create redundant work
to cut the tail, and every production implementation of the idea pairs it with
a cap. gRPC's `hedgingPolicy` caps `maxAttempts` at 5 and adds a
`RetryThrottlingPolicy` token bucket so hedging cannot amplify load during a
backend problem; when one attempt returns, "all outstanding hedged requests are
canceled". The adaptive Go implementation reported by InfoQ in 2026 fires its
backup at the measured p90 and caps the hedge rate with a token bucket, buying
p99 64 ms → 17 ms for about 9% overhead. The consistent lesson is that the cap
is mandatory and the cancellation is what keeps the cap cheap.

This vault has the cap and cannot have the cancellation. That is the whole
asymmetry: gRPC gets its permit back the moment the winner returns, and this
boundary gets it back only when the loser finishes. A design that assumes
cancellation and then omits it turns a latency guard into an availability
problem — which is precisely what the measurement showed.

**Capacity for one dependency must be reserved from capacity for the others.**
This is the bulkhead pattern, and the reasoning is verbatim the situation here:
"the idea behind bulkheads is to set a limit on the number of concurrent calls
we make to a remote service. We treat calls to different remote services as
different, isolated pools and set a limit on how many calls can be made
concurrently." Resilience4j offers it in two forms — a thread-pool bulkhead
with its own executor and queue, and a `SemaphoreBulkhead` that limits
concurrency on the calling thread with near-zero overhead, throwing
`BulkheadFullException` immediately or after an optional `maxWaitDuration`.
The stated motivation is a real incident in which one slow dependency (Redis)
consumed every thread in a shared pool and stopped unrelated request handling.
Hystrix's original argument for per-dependency isolation is the same.

**Duplicate in-flight work for the same key should be coalesced, not
multiplied.** Go's `singleflight.Group.Do` "executes and returns the results of
the given function, making sure that only one execution is in-flight for a
given key at a time"; later callers block and share the first result. The 2026
write-ups on cache stampede prevention describe the same in-process promise
deduplication as the cheap default, ahead of Redis locks and probabilistic
early recomputation. The relevant property here is not the shared *result* —
two recall calls ask different questions — but the shared *precondition*: the
model load that both dense legs need is one execution that should happen once.
`search_memory._lazy_generation_query_encoder` already documents the tolerated
alternative — "two stragglers racing the cache would load twice and keep the
last; the cost is one wasted load, never a wrong vector" — at about 1.1 GiB of
resident memory per load.

**Refuse fast or wait, but bound it either way.** Resilience4j's
`maxWaitDuration` makes queuing an explicit choice rather than a default, and
the thread-pool bulkhead rejects with `BulkheadFullException` once pool and
queue are both full. Nothing in current practice recommends an unbounded queue
in front of optional work.

## What this vault chose, and why the others lost

Chosen: **partition the straggler capacity by kind of optional stage** — one
permit for `dense`, one for `rerank` — keeping the release at work-end. This is
the semaphore bulkhead, sized at one per kind, which incidentally makes each
kind single-flight and so forbids the doubled model load that the shared pool
of two permitted.

The live thread bound does not move: two optional threads before, two after,
one per kind. Both product call sites are labelled, so the old shared pool is
reachable only as the fallback for an unlabelled or unknown kind, and the kinds
are a fixed module-level tuple rather than caller-supplied, so no call can mint
a new pool and widen the bound.

*Releasing the permit when the wait ends* lost on the stated bound. The permit
is the only thing bounding thread growth, because the thread cannot be
cancelled; releasing at end-of-wait means a stream of calls whose stages never
finish creates one live thread per call, without limit. It would also
reintroduce the concurrent double model load — two embedding models at about
1.1 GiB each — on a four-core machine that is already the bottleneck.

*Sizing one shared pool by the number of optional stages* lost on the mechanism
rather than the number. Any single shared pool leaves the next caller's
admission at the mercy of the previous caller's abandoned work; enlarging it
lowers the probability of starvation without removing it, and under a sustained
arrival rate with 9 s and 20 s stragglers any fixed shared size saturates. The
bulkhead removes the cross-kind case outright, which is the case that was
measured.

*Recognising a shared model load as shared work* is the strongest of the three
and lost only on where the code lives. Doing it properly means splitting the
dense stage's query-independent precondition (load the model) from its
query-dependent body (encode, search) and putting `singleflight` semantics on
the precondition alone — later callers join the load instead of being refused,
then run warm. That split belongs inside `search_memory`, which owns the
encoder and was out of scope for this change. The per-kind partition is a
strict subset of it: at one permit per kind it already guarantees a single
in-flight load per kind, and only the *joining* half is left on the table.
Recorded here as the next step rather than claimed as done.

*Waiting for a same-kind permit instead of refusing at 0.00 s* was considered
and rejected on this vault's own numbers rather than on taste. A wait pays off
only if the in-flight straggler finishes inside it and the warm stage still
fits afterwards; the measured loads are 9 s and 20 s against a stage budget of
at most 5 s at the MCP deadline, so the wait would spend the caller's budget
and delay the lexical answer for a leg that could not finish anyway. The cost
of refusing fast is named in the code.

## What it measured

Four paired rounds, the variants interleaved, six recall-shaped calls per round
in one fresh process at the 10 s MCP budget, against the live vault. Load
average at the start of each round was 6.4 to 10.2 on four cores — four other
agents were working — and the two sides were run alternately so neither got the
quiet half.

| | dense reached the answer, per six calls |
|---|---|
| shared pool | 2, 2, 2, 1 — mean 1.75 |
| per-kind | 4, 3, 4, 3 — mean 3.50 |

Every partitioned round beat every shared round. Across the 24 calls a side,
an optional stage succeeded 7 times before and 14 times after, and all of those
were the dense leg: the cross-encoder was applied in no call on either side,
its cold load being what it is.

The refusals did not become fewer; they moved. The shared pool refused 18
stages for capacity, spread across both kinds indiscriminately. The partitioned
boundary refused 22 — slightly more — but 18 of those landed on `rerank`, the
stage whose cold load cannot finish inside an MCP budget under any policy, and
only 4 on `dense`, each of those against its own in-flight load, which is
single-flight doing its job. That is the change stated plainly: refusal is not
reduced, it is aimed at the leg that could not be served instead of the leg
that could.

A second effect showed up on the same traces: a call whose rerank is refused at
0.00 s no longer spends its rerank stage budget, so the calls that gained a
dense leg also got shorter — 3.0 to 4.5 s against 3.2 to 8.4 s. That is a side
effect of refusing fast, not a goal, and it is not evidence for the choice.

Nothing here is measurable at load above about 12: at that point the *required*
corpus and generation-catalog work misses the 10 s budget on nearly every call
in both variants, and the six-call number is zero on both sides. Runs at load
12 to 21 were taken and discarded for that reason, not for their result.

## What is not settled

The bulkhead fixes admission across kinds. It does not make the *first* call in
a fresh process any faster: that call pays the cold load, cannot finish inside
its stage budget, and correctly gets a lexical answer. Whether the process
should pay that load once at startup is a separate, already-measured question —
7.99 s of startup and 1108 MiB resident, per this vault's log of 2026-08-24 —
and remains the owner's decision.

## Sources

- [The Tail at Scale — Dean & Barroso, Communications of the ACM](https://cacm.acm.org/research/the-tail-at-scale/)
  (abstract also at [research.google](https://research.google/pubs/the-tail-at-scale/))
- [gRPC — Request Hedging](https://grpc.io/docs/guides/request-hedging/)
- [Stragglers, Not Failures: How Adaptive Hedged Requests Reduce p99 Latency by 74 Percent — InfoQ](https://www.infoq.com/articles/adaptive-hedged-requests-p99-latency/)
- [Implementing Bulkhead with Resilience4j — reflectoring.io](https://reflectoring.io/bulkhead-with-resilience4j/)
- [golang.org/x/sync/singleflight — package documentation](https://pkg.go.dev/golang.org/x/sync/singleflight)
- [How to Build Cache Stampede Prevention — OneUptime, 2026-01-30](https://oneuptime.com/blog/post/2026-01-30-cache-stampede-prevention/view)
- [Cancelable tasks cannot safely use semaphores — discuss.python.org, Async-SIG](https://discuss.python.org/t/cancelable-tasks-cannot-safely-use-semaphores/70949)
- In-repo evidence: `scripts/search_memory.py::_lazy_generation_query_encoder`,
  `knowledge/log.md` entries of 2026-08-24 and 2026-08-26.
