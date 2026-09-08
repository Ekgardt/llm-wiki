# Working before the claims — 2026-09-08

Task 9 of `docs/TASKS-to-100-2026-09-08.md`, the reading half. The answer
is a closed JSON document: status, claims with citations, reason. The model
writes the claims first and has nowhere to think.

## What is known

- LongMemEval (arXiv:2410.10813, §reading): asking the reader to first
  extract what each retrieved passage says that bears on the question
  (Chain-of-Note, arXiv:2311.09210) and then answer in JSON gives up to
  +10 points over answering directly; the authors call the reading
  strategy one of the three things a memory needs.
- Emergence AI (https://www.emergence.ai/blog/sota-on-longmemeval-with-rag):
  "prompts GPT-4o to generate chain-of-thought reasoning before answering"
  is part of the 82.4% system.
- Run 1 here: with the answer text in the prompt we are right 83% of the
  time; the wrong answers with evidence present are readings — the wrong
  bike counted, the earlier of two values, a date not subtracted.
- Our schema is closed (`additionalProperties: false`), so a note the model
  writes anywhere but `reason` fails validation, and a note in `reason` was
  refusing whole answers until 2026-09-07 (`_require_answered_shape`).

## Decision

Add one optional top-level field, `working`, a string of at most 3 000
characters, and instruct the model to write it first: one line per evidence
span it will use, stating what the span says that bears on the question and
its date; then the claims. The field never reaches a reader: `grounded_qa`
removes it with the other private keys. No gate reads it. The claims and
their citations are verified exactly as before, so a wrong note cannot make
a wrong claim pass.

Cost: the note is output tokens, a few hundred per answer.

## Rule of decision

One run of 200, seed 101, against the tasks 1–6 arm; kept if the gain
exceeds 0.035.
