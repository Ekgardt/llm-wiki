# A failed second look keeps the first answer

Dated 2026-09-17. Found by the third audit (retrieval, H2); the research before the fix.

Files: `scripts/query_memory.py`, `tests/test_a_failed_second_look_keeps_the_first_answer.py`

## What was found

- `grounded_qa` generates and verifies one answer, then may look again three ways: a count
  is searched wider (`_count_step`), an unstated gap between dates is computed
  (`_calendar_look`), a refusal or a dropped claim is searched for (`_refusal_look`).
- `_adopted` states the rule: "a second look never turns an answer into silence". It held
  only when the second pass returned normally. None of the three looks guarded the pass, so
  when the regenerated reply did not parse (`GroundedQAError`), failed the schema
  (`EvidenceResolutionError`), or the deadline passed during the second generation or one of
  its searches (`TimeoutError`), the exception left `grounded_qa` and the first answer —
  already verified, already cited — was lost.
- Reproduced on the product path: a first reply with one surviving and one dropped claim, a
  second reply in prose. Result before the fix: `GroundedQAError: grounded QA provider
  returned invalid JSON` after two generations, instead of the verified one-claim answer.
- The same shape sits in the helpers of a look: the fan-out call, the clustering call and
  the missing-evidence call all go to the provider under the same deadline, and the searches
  they drive can stop on it.
- The provider's own failures do not need a second class here: `llm_client.call_llm` turns a
  provider timeout or exit into an empty reply, which `_parsed_answer` reports as
  `GroundedQAError`.

## Practice on this date

- An optional improvement step must degrade to the result already in hand, not fail the
  request. Google's SRE book, "Addressing Cascading Failures", section "Load Shedding and
  Graceful Degradation": "In some applications, it's possible to significantly decrease the
  amount of work or time needed by decreasing the quality of responses."
  (<https://sre.google/sre-book/addressing-cascading-failures/>)
- The handler names the failures the pass is defined to have — a gate error, a schema error,
  the deadline — and nothing broader, so a programming error in a look still surfaces.

## The decision

- One helper, `_looked_again(first, look)`, runs the optional part of a look and returns
  `first` when it raises `GroundedQAError`, `EvidenceResolutionError` or `TimeoutError`.
  All three looks go through it, including their searches and helper calls, so the class is
  closed in one place.
- The failure is not silent: the retrieval telemetry receives an outcome
  `second look failed: <error class>`, the same best-effort channel that records a look that
  happened.
- A failed count step ends the count loop: the step reports that nothing grew.
- The first pass is unchanged: with no verified answer in hand, its failure is still the
  caller's error.
