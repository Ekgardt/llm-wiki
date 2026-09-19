# A step no provider answered is not green, and its budget fits its step

Dated 2026-09-17. Findings M-B1 and M-B5 (first two items) of the third audit (medium,
confirmed by reading and by tests). The research before the fix.

Files: `scripts/fact_keys.py`, `scripts/episode_consolidation.py`,
`tests/test_a_step_no_provider_answered_is_not_green.py`.

## What was found

- The nightly kills the `fact_keys` step at 660 s. `fact_keys` gives itself 600 s, and its
  clock starts only after `collect_corpus` has read the whole daily tree. The margin is
  60 s minus the collection, against a provider call that may take 90 s. Every neighbour
  step leaves `STEP_START_MARGIN_SECONDS = 120`; this one was missed. A night that uses its
  whole budget ends in a kill during a paid call whose batch is then lost.
- `episode_consolidation.main` returns 0 always, also when it stopped because the provider
  returned nothing. `fact_keys.main` returns 0 always, also when turns were waiting and not
  one was keyed. `maintenance_helpers` prints a step's stderr only for a non-zero exit, so
  the nightly log shows a clean step for a night in which no provider answered.

## Practice on this date

- "With deadline propagation, a deadline is set high in the stack (e.g., in the frontend).
  The tree of RPCs emanating from an initial request will all have the same absolute
  deadline." ([Addressing Cascading Failures, Google SRE book](https://sre.google/sre-book/addressing-cascading-failures/)).
  The step's deadline belongs to the moment the step starts, not to the moment its first
  stage finishes, and the inner budget is the outer limit minus what one last call may
  take.
- An exit status is the one channel the caller reads. The repository already holds that an
  error is not an answer (`docs/research/2026-09-14-an-error-is-not-an-answer.md`); a run
  that did none of its waiting work is an error for the scheduler to count.

## The decision

- `fact_keys`: the default budget is 540 s (the 660 s step minus the 120 s margin every
  neighbour keeps), and the clock starts before the corpus is collected.
- `fact_keys.main` exits 1 when turns were waiting and none was keyed; 0 when there was
  nothing to key or some were keyed.
- `episode_consolidation.main` exits 1 when it stopped because the provider returned
  nothing; a day skipped for its own error, and a budget that ran out, stay 0 as before.
- The nightly already counts a non-zero step as a failure and goes on to the next step, so
  no scheduler file changes. No path, environment variable or contract changes.
