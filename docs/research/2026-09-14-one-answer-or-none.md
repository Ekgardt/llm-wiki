# One answer of the expected shape, or none

Dated 2026-09-14. Item 0.3 of `docs/AUDIT-2026-09-14-2.md` — a regression of my own
earlier today. The research before the fix.

## What was found

`reply_json` (`docs/research/2026-09-14-the-document-after-the-notes.md`) takes, when
a reply is not clean JSON, the **last** object (or array of objects) written in it.
The review reproduced where that is wrong:

- **Contradiction verdicts** — `{"label":"compatible",…}` followed by prose that quotes
  `{"label":"contradiction",…}` from the claims under review returns the quoted one.
  The claims come from transcripts, so text being judged can decide the judgement.
  The reader before today accepted only bare JSON.
- **Episode consolidation** — the real lesson array followed by
  `Example format: [{"lesson":"…"}]` returns the example; it fails grounding, the batch
  ends with 0 lessons and is marked done: the day's lessons are silently lost.
- **A deeply nested unclosed value** after prose raises `RecursionError` from
  `raw_decode`; callers catch only `ValueError`.

The graph: `_reply_value` has 16 transitive callers across `query_memory`
(`_parsed_answer`), `compile_memory` (`_draft_operations`, `_dropped_slugs`),
`contradiction_pipeline` (`_validated_evaluation_output`), `episode_consolidation`
(`grounded_lessons`), `aggregation_pass` (`_parsed_groups`, `parsed_queries`) and
`fact_keys` (`_parsed_batch`). Each expects one specific shape.

What the measurement behind the reader actually showed: 29 of 29 recovered answers
held exactly **one** JSON object; nothing measured needed choosing between several.

## Practice on this date

- Structured-output parsing validates against the expected schema and treats an
  ambiguous or non-conforming reply as a failure to retry, rather than guessing which
  fragment was meant ([robust JSON extraction for LLM responses](https://github.com/OpenMind/OM1/issues/1700);
  the grounded-answer path in this codebase already refuses rather than guesses at
  every gate).
- `json.JSONDecoder.raw_decode` on deeply nested unclosed input raised
  `RecursionError`, not `JSONDecodeError`, in the review's reproduction on this
  machine's Python; a bounded reader treats that as undecodable too.

## The decision

- Every caller names the shape it expects: the grounded answer (`status`), a compile
  draft (`operations`), a critique (`reviews`), a contradiction verdict (`label`),
  aggregation groups (`groups`) or queries (`queries`), fact keys (an object),
  consolidation lessons (an array of objects).
- The reader takes a fenced block or the whole reply when it parses to that shape, as
  before. Otherwise it collects every complete value of that shape in the reply:
  **exactly one** is the answer; **none or more than one** is refused as unreadable, and
  each caller already retries or records an unreadable reply.
- `RecursionError` while decoding counts as undecodable.

Why not the alternatives:

- **Take the first instead of the last.** It is just as wrong the other way round (a
  draft followed by its correction).
- **Go back to bare JSON only.** It throws away the 29 measured answers again.

Files: `scripts/reply_json.py`, `scripts/query_memory.py`, `scripts/compile_memory.py`,
`scripts/contradiction_pipeline.py`, `scripts/aggregation_pass.py`, `scripts/fact_keys.py`,
`scripts/episode_consolidation.py`, `tests/test_one_answer_or_none.py`,
`tests/test_the_document_after_the_notes.py`, `tests/test_an_error_is_not_an_answer.py`,
`docs/research/2026-09-14-one-answer-or-none.md`.
