# A rerank learns its cost once, and one that cannot fit is not started

Date: 2026-09-25. Audit item B-17 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code; the load behaviour is the audit's measurement)

- `_run_optional_bounded` separates "may the caller wait" from "may the stage
  run": a stage the caller cannot wait for is still started, so that "the
  straggler that finishes it leaves the model resident and records what it
  cost" (`_optional_stage_admitted`, `_unknown_cost_stage_fits`).
- `_run_reranker` hands that straggler the caller's own stage deadline
  ("Told when to stop, it stops between batches instead"). A rerank cut by its
  deadline returns unapplied rows, and `_rerank_scored` rightly refuses to take
  such a run as a cost observation. So under load a rerank that needs more than
  the window never finishes, never records a cost, is never admitted — and is
  started, and cut, on every call: the CPU is spent and nothing is learnt.
- The audit measured 2–4 s per query on an idle machine with the reranker
  applied, and no reranking under load.

## Source

- Google SRE book, "Handling Overload",
  https://sre.google/sre-book/handling-overload/ (fetched 2026-09-25): a task
  "should reject requests quickly with the expectation that returning ... an
  error consumes significantly fewer resources than actually processing the
  request." A rerank known not to fit is such a request.

## Decision

- When the rerank is expected to fit the window, nothing changes.
- When its cost is not yet known, the one background run gets the stage ceiling
  (`OPTIONAL_STAGE_MAX_SECONDS`) instead of the caller's deadline, so it can
  finish and record what it cost; the caller still waits only its own window.
- When its cost is known and does not fit, the rerank is not started at all: the
  caller keeps the fused order (`optional_stage_timeout`), and no CPU is spent
  on a run whose result nobody will read.

## Files

- `scripts/retrieval.py`
- `tests/test_a_rerank_that_cannot_fit_is_not_started.py`
- `CHANGELOG.md`
