# An abandoned stage is not a measurement

Dated 2026-09-17. Third audit, retrieval L2: a rerank that its own deadline cut short is
recorded as a finished run, so the admission model learns a cost the stage never paid.

Files: `scripts/retrieval.py`,
`tests/test_an_abandoned_stage_is_not_a_measurement.py`

## What was found

- `_run_optional_bounded` starts every optional stage in a worker and records what it cost
  (`_observe_optional_stage`). The comment there already states the rule: "Only a run that
  produced something is a cost observation. A fast failure is not evidence that the work is
  cheap." The code implements it for an exception only.
- A rerank that runs out of time does not raise. `reranker._score_with_bundle` catches
  `_OutOfTime` and returns a `_Scoring` with no scores, every document is marked
  `reranker_applied=False, reranker_fallback_reason="reranker_deadline"`, and `rerank` returns
  the fused order normally.
- The worker therefore observes that run's elapsed time, which can never exceed the window the
  stage was given (`stage_deadline`). The recorded cost is an upper bound of the budget, not of
  the work, so `_optional_stage_fits` compares the next window against a number that always
  fits.
- Consequence for a warm reranker that genuinely needs more than the MCP share: the stage is
  admitted, waited for, cut at the deadline, and recorded as cheap again — every call, for ever.
  The observation can never grow enough to refuse the wait, which is the one thing it exists to
  do.
- `reranker_unavailable` and `reranker_error` have the same shape: a fast failure recorded as
  the cost of the stage.

## Practice on this date

- This is the classic failure of a latency model fed by censored samples: a timed-out request
  measures the timeout, not the service, and feeding it back as an observation biases the
  estimate downward without bound. Survival analysis calls such a sample right-censored and
  excludes it from a naive mean; the practical rule in timeout/admission control is to record
  only completed work and treat a cut-off as "at least this long, value unknown".
- The module's own one-sample model already has the right escape: with no observation for a
  kind, `_unknown_cost_stage_fits` admits only when the caller can offer the whole ceiling,
  which is the conservative answer for an unmeasured stage.

## The decision

- `_run_optional_bounded` takes `observes`, a predicate on the value the operation returned:
  whether that value is evidence of a finished run. The default counts every value, which is
  what the dense leg wants.
- The rerank passes a predicate that reads the stage's own verdict: a result whose head says
  `reranker_applied` is false did not score, so it is not a measurement. The value is still
  returned to the caller and the fused order still stands — only the observation is dropped.
- With no usable observation the kind stays unknown, and the existing rule decides: waiting is
  admitted only when the caller can offer the full ceiling. That is the conservative behaviour
  the audit asks for, with no new constant and no new state.
