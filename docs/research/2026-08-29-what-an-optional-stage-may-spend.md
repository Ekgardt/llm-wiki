# What an optional stage may spend

2026-08-29. Research for the change that stops `recall` and `get_decisions`
crossing `MCP_OPERATION_SECONDS` under load. Measured on this machine unless
marked otherwise; measured and inferred are kept apart.

## The question

Two optional retrieval stages — the dense leg and the cross-encoder rerank —
each get half of whatever is left of the operation budget, serially, through
`retrieval._optional_stage_deadline`. Nothing reserves time for the mandatory
work that must still run after them, and nothing caps what the two together may
spend. The question this note has to settle is what an optional stage is
allowed to take from a caller who is waiting, and whether a stage that cannot
finish should be waited for at all.

## What was measured

Machine: 4 vCPU, load average 5–16 during the runs, other agents active. The
vault is the live one, generation `evidence-graph` active with
`vector_state: complete`.

Model costs, one process, cold then warm:

| | cold | warm | resident added |
|---|---|---|---|
| embedding model (`_get_embedder`) | **10.13 s** | encode 0.018 s | **+1140 MiB** |
| cross-encoder (`reranker.rerank`, 20 pairs) | **3.73 s** | 0.66 s | **+1505 MiB** |

Stage costs inside a real `recall`, instrumented at `_run_optional_bounded`:

- dense stage, warm: 1.5–2.5 s (the vector read, not the encode)
- dense stage, cold: abandoned at its share, 2.9–4.0 s, no result
- rerank stage, warm: ~0.7 s
- mandatory lexical leg: 0.67–5.34 s
- catalog + generation open: 0.44–3.20 s
- mandatory tail after the last optional stage (fusion, rendering, seal
  re-check, `_meta`): **0.59–0.86 s** over eight instrumented calls

The share arithmetic that follows from this: at the 10 s MCP budget, by the
time the optional stages run there is typically 5–6 s left, so the dense stage
is granted about 3 s and the rerank stage about 1.5 s. **A cold dense load
needs 10.13 s and a cold rerank load 3.73 s. Neither can finish in the share it
is given. Both are structurally impossible on the first call in a process, and
each one spends its whole share to discover that.**

Baseline, three runs of six paired rounds under the stated load:

| run | `recall` over budget | `get_decisions` over budget |
|---|---|---|
| 1 | 1/6 | 2/6 |
| 2 | 4/6 | 4/6 |
| 3 | 3/6 | 4/6 |
| total | **8/18** | **10/18** |

**18 of 36 calls crossed the budget, and every one of them raised instead of
answering.** Not a degraded answer — nothing. The lexical leg had already
completed and its result was in hand; the optional stages then ran the clock
out and a later mandatory `_check_stopped` threw it away. In `recall` the
`except TimeoutError` fallback in `mcp_server._search_vault` re-runs the search
lexical-only with *the same, already-expired* deadline, so it raises too;
`get_decisions` has no fallback at all.

So the shape of the failure is precise and it is not the mandatory work:
between 4.4 s and 5.5 s of a 10 s budget went to two optional stages that
returned nothing, and then the answer already computed was discarded.

## What the practice says

Two established rules bear on this, and they line up with the two halves of the
defect.

**Reserve for the caller's own remaining work when propagating a deadline.**
The gRPC guidance is explicit that a downstream deadline must not consume the
upstream's whole remaining budget, and that a caller should deduct elapsed time
and reserve buffer for its own response processing before passing a deadline
down. `_optional_stage_deadline` deducts elapsed time — it takes a share of
what is *left* — but reserves nothing for the tail that must run after it. The
tail is measured above at 0.59–0.86 s and it is the part that cannot be cut,
because it is where the answer gets built.

**Timebox the non-critical hop and render without the enrichment.** The
latency-budget pattern for interactive paths is that when a non-critical hop
expires you return the partial result rather than making the caller wait; a
degraded-but-fast answer beats a complete-but-late one. This vault already
intends exactly that — `OptionalStageTimeout` is caught and reported as
`fallback_reason: optional_stage_timeout` — but the intent is defeated because
the enrichment's own timebox eats the budget the render needs.

**Do not admit work you can predict will miss.** Deadline-aware admission
control rejects a request when the expected processing would violate the
deadline, modelling the expected cost rather than discovering it by running.
Archipelago's shortest-remaining-slack scheduling is the same idea: compare the
work's expected cost against the slack before committing to it. The cost model
here does not need to be clever. The duration of the last run of that kind that
actually finished is a cost model, and it is one this code can observe for free
because the abandoned straggler does eventually finish.

Note that `retrieval.py` already states this conclusion in prose, in the
comment on `OPTIONAL_STAGE_KINDS`, about the *queued* case: "the measured loads
do not fit in an MCP-budget stage, so that wait would spend the caller's budget
and delay the lexical answer for a leg that still could not finish." That
reasoning was applied to the second stage of a kind and never to the first.

Sources consulted 2026-08-29: gRPC deadline guidance on propagation and
reserving buffer for local processing (grpc.io/docs/guides/deadlines,
oneuptime deadline/timeout posts, 2026-01); per-hop latency budgeting for agent
paths with partial rendering on non-critical expiry (tianpan.co, 2026-07);
deadline-aware admission and shortest-remaining-slack scheduling
(Archipelago, arXiv 1911.09849; Cucumber, arXiv 2205.02895).

## What was decided

**1. A reserve.** An optional stage's wait never runs past `deadline - 2.5 s`.

The reserve covers two measured things. The tail itself, 0.59–0.86 s. And the
wait's own overshoot: `_await_optional_stage` polls an Event every 10 ms and
lands within 3 ms of its deadline in isolation, and within 73 ms against busy
pure-Python siblings — but a stage loading a model holds the interpreter in
long stretches, and the waiter cannot read its clock until it gets it back.
Instrumented on the real path, a stage granted 3.546 s returned after 4.445 s,
0.9 s late. Nothing in the waiter can prevent that, so the reserve absorbs it.
A first attempt at 1.5 s was measured and found too small for exactly this
reason.

**2. Admission by observed cost.** A kind whose last finished run took longer
than the window now on offer is not waited for. A kind with no observation is
waited for only when the caller granted enough budget that
`OPTIONAL_STAGE_MAX_SECONDS`, not its own share, is the binding constraint —
which is the case that ceiling already exists for and says so in its own
comment: a one-shot CLI has no second call to be warm for.

**3. The stage is always started; only the wait is refused.** This was got
wrong first and caught by an existing test rather than by reasoning. Bounding
the *grant* meant that on a short budget the worker never started, so nothing
warmed — which defeats the daemon-straggler design the whole scheme depends on.
The two questions are separate: whether the work may run, and whether this
caller may spend its budget waiting for it.

**4. The observation is clamped at the ceiling.** Also found by a real failure
rather than by reading: `test_mcp_server.py` stalls a backend for ~30 s on
purpose, and that figure then governed every later caller in the process,
including ones whose window was 15 s — wide enough for any real stage. The
figure is only ever compared against a window, and no window may exceed the
ceiling, so everything above it means one thing. Keeping more precision than
the comparison can spend only creates differences that behave identically.

Rejected: raising `MCP_OPERATION_SECONDS`. The work that overruns produces
nothing — a bigger budget buys a longer wait for the same empty result, and the
measured cold load of 10.13 s would need the budget roughly doubled to fit a
signal that the second call gets for free anyway.

Rejected: cutting the share below 0.5 unconditionally. That would take time
from the warm stages, which are the ones that succeed (1.5–2.5 s dense, 0.7 s
rerank) and which carry the quality the retrieval stands measure.

## What the change is worth, and what it is not

Paired and interleaved, old budgeting against new in alternating fresh
processes at load average 8–13, n=32 each:

| | over budget | p50 |
|---|---|---|
| old | 11/32 (34%) | 8.77 s |
| new | 9/32 (28%) | 6.62 s |

The p50 improvement of 2.15 s is real and larger than the run-to-run wander.
**The over-budget rate is not settled by this** — 34% against 28% at n=32 is
not a difference — and the reason is that the remaining failures are a
different defect: they are concentrated entirely in the first one or two calls
of a fresh process, where the cold cost is *mandatory* work. Instrumented,
those calls spend 2.0–6.1 s validating the generation catalog and 0.9–6.4 s in
the mandatory lexical leg before any optional stage is reached at all. No
budgeting rule can help there; the work is real and the machine has four cores.

## The shared server changes the arithmetic

`mcp_server._start_encoder_warmup` already loads the encoder on a background
thread, for both transports, since 2026-08-27. Measured here, that is not
enough, and background loading is part of why: on four cores a 2 GiB model load
running alongside the first questions competes with the mandatory legs.

Interleaved, four fresh processes per variant, four paired rounds each:

| | blocking start | resident added | over budget | p50 | max |
|---|---|---|---|---|---|
| background encoder warm (today, both transports) | 0.0 s | ~0 MiB | 8/32 (25%) | 7.58 s | 10.31 s |
| synchronous full warm | 32.0–32.9 s | 2275–2690 MiB | **0/32** | 5.52 s | 7.72 s |

Confirmed on the final code, 3 fresh processes per variant at load 5–11:
background warm 6/24 over budget, max 10.01 s; full warm **0/24**, max 7.87 s.
Cold first call: 9.84–10.01 s with the background warm, 3.91–7.87 s with the
full warm.

So the warm-up is made **conditional on the transport**, in `mcp_http.py` only.
A stdio server is one per agent and would pay 32 s and 2.4 GiB per agent, which
is worse than the problem. An HTTP server is one for all of them, started once
deliberately, and pays it once. `LLMWIKI_NO_SHARED_WARMUP=1` turns it off for a
memory-tight machine.

## What is not claimed

The observed-cost model is one sample deep, per kind, per process. It is not a
distribution and makes no promise about the next run; it self-corrects in one
call in either direction, and its failure mode is bounded — a wrongly skipped
stage degrades to the lexical answer, which is the designed fallback. Its cost
is also bounded and real: after any pathological run, exactly one later call
loses that leg.

The budgeting change on its own does not bring `recall` and `get_decisions`
inside their budget on a cold stdio server. It removes the waste and the lost
answer; the cold mandatory path is a separate, unfixed defect, and the shared
server's warm-up is what closes it.

Both retrieval stands were run paired against HEAD on this vault and are
unchanged by this work: `hit@1` 0.3, `hit@5` 0.3, `applied@5` 0.2857 on both
sides. Both are currently **below their own thresholds** (0.6 and 0.4) on HEAD
too, against 0.5–0.6 and 0.857 recorded earlier. That regression predates this
change and is not diagnosed here.

The cross-encoder cold load measured 3.73 s here, against about 20 s recorded
on 2026-08-26 in the audit register. The two disagree and this note does not
resolve which is representative; the model files were already in the local
cache for this measurement and may not have been for that one. Nothing in the
decision depends on which figure is right, because both exceed the share an
MCP-budget call can offer.
