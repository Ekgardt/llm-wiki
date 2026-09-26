# A rerank is tried again, and an absent one says so

Date: 2026-09-26. Audit 2026-09-26 item B-16 (includes a regression of the B-17 fix).

## Fact
- The B-17 fix refuses to start the rerank when its last observed cost does not
  fit the window (`retrieval._rerank_worker_deadline`). The cost is only ever
  re-measured by a run, so after one slow run (clamped to 12 s) against windows
  of about 6–9 s the stage is never started again for the life of the MCP
  process, and every recall reports `optional_stage_timeout`. The design comment
  ("self-corrects in one call in either direction") no longer held.
- The reranker is an optional extra (`install.sh` opt-in). Without torch and
  transformers, `reranker.should_rerank` still asks for the stage; its cost is
  unknown, so the caller does not wait and the answer says
  `optional_stage_timeout` — a timeout that never happened — instead of that the
  reranker is not installed.

## Sources (fetched 2026-09-26)
- Martin Fowler, "CircuitBreaker", https://martinfowler.com/bliki/CircuitBreaker.html:
  after a reset timeout the breaker is "half-open - meaning the circuit is ready to
  make a real call as trial to see if the problem is fixed", and "Asked to call in
  the half-open state results in a trial call, which will either reset the breaker
  if successful or restart the timeout if not."
- Python documentation, `importlib.util.find_spec`,
  https://docs.python.org/3/library/importlib.html: "Find the spec for a module
  …" — it answers whether a module can be imported without importing it.

## Decision
- The observed cost is kept with the time it was observed. A cost older than
  `OPTIONAL_STAGE_REPROBE_SECONDS` (300 s) no longer refuses: the stage is started
  once more as a background trial with the full ceiling, exactly like an unknown
  cost, and the run that finishes records the new figure. The caller still does
  not wait for a stage known not to fit its window.
- `should_rerank` refuses with `reranker_unavailable` when no reranker is
  configured or its libraries cannot be found (`find_spec`), or a load already
  failed.

## Files
- scripts/retrieval.py
- scripts/reranker.py
- tests/test_a_rerank_is_tried_again.py
