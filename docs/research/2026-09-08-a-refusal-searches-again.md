# A refusal searches again, and the latest value wins — 2026-09-08

Tasks 4, 5 and 6 of `docs/TASKS-to-100-2026-09-08.md`, taken together
because run 1 shows they are one mechanism seen from three sides.

## The losses (run 1, 186 answerable)

- 28 silences. The recorded reasons name what is missing, precisely:
  "the evidence confirms you have a new tennis racket … but no span
  states where it was bought"; "Dr. Smith is a therapist you see … no
  span states how often"; "no material about a high school reunion, the
  user's high school, former classmates". The model knows what evidence it
  lacks. Nothing asks retrieval for it: the only query ever run is the
  question.
- 5 of the 6 knowledge-update silences are of two kinds: two spans give
  different values and the answer is `conflicting_evidence` (the cocktail
  class is on two weekdays), or the question asks for the value *before* a
  change and the answer reports only the change. The benchmark's own judge
  template for this type says an answer "containing some previous
  information along with an updated answer" is correct as long as the
  updated answer is there — the type is defined by supersession.
- Preference questions refuse for lack of evidence about the *event*
  ("no listing of cultural events this weekend") when the rubric asks only
  that the answer use what the user said about themselves. The 2026-09-01
  advice clause was withdrawn on a measurement made before the rubric
  judge existed; that measurement no longer says what it said.

## What the field does

- Self-RAG and FLARE (arXiv:2310.11511, arXiv:2305.06983): retrieve again
  when the generation reveals what is missing; the model's own draft is the
  best query for its gap.
- Perplexity Pro Search: results of a step feed the queries of the next.
- Zep (arXiv:2501.13956): a superseded fact is invalidated, not deleted,
  and the current value is the one with the latest valid-from; Mem0's
  UPDATE does the same at ingest. Our compiled layer already carries
  `superseded_by`; the raw layer carries dates on every entry.
- Every competitor on LongMemEval answers every question. On the stand a
  refusal is scored as wrong; on the product it is the contract.

## Decision

1. **A refusal searches again.** When the first answer refused, or dropped
   a claim at a gate, and the caller supplied `search`, one short model
   call turns the question, the refusal's reason and the dropped claims'
   text into at most five concrete queries for the missing evidence; what
   they find joins the candidates; the answer is generated once more. The
   second answer is adopted when it answered. This is the fan-out of the
   count pass applied to what the model said it lacked. New module
   `scripts/refusal_pass.py`; one regeneration per question shared with the
   count and calendar passes, whichever fires first.
2. **The latest value wins.** The system prompt states the knowledge-update
   rule: when spans give different values for the same thing at different
   dates, the later one is the current value and the earlier is named as
   superseded — that is an answer, not a conflict; `conflicting_evidence`
   is for spans of the same date or no date. When the question asks for the
   value before a change, the earlier one is the answer.
3. **Advice is answered from the person.** The system prompt states that a
   question asking for suggestions, tips or a judgement is answered by
   citing what the evidence says about the user's own situation,
   preferences and possessions and building on it, marked as advice; it is
   not refused because no span states the advice. The 2026-09-01 wording
   is retried under the judge that now exists.
4. **Answer mode on the stand only.** `grounded_qa(keep_unverified=True)`
   returns the texts of claims the gates dropped under `unverified_claims`,
   labelled; the product never asks for it. The stand's `--policy answer`
   reports such a text as the hypothesis, marked `(uncited)`, so the number
   the field compares is measured; `--policy refuse` stays the default and
   both are published side by side.

## Cost

One short call and up to five reranker-free searches, plus one more answer
call, only on questions that refused or dropped a claim (about a fifth of
the stand's questions in run 1). Nothing on a clean answer.

## Rule of decision

One run of 200, seed 101, judged, each against the second-look baseline:
kept if the gain exceeds 0.035; the refusal count and the correct-abstention
count are reported beside the accuracy so a gain bought by answering
abstention questions is visible.
