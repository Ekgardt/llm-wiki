# Reviewing more than fits at once

Date: 2026-08-24
Reason: the compile of the imported history stops at one refusal —
`critique:claude:input_budget`. The reviewer's prompt does not fit, so a valid
plan for the day that most needs compiling is thrown away.

## What is measured here

Instrumented on this vault, one part of `knowledge/daily/2026-08-24.md`:

| prompt | characters | tokens | budget | fits |
|---|---|---|---|---|
| draft | 18,418 | 27,143 | 27,744 | yes |
| critique | 38,241 | 40,543 | 27,744 | no |

The draft returned **16 operations**. The critique prompt carries all sixteen
page bodies plus their cited evidence, so it is about twice the draft — and the
budget (a 32,768-token window, 4,000 reserved for output, 1,024 safety) does not
move.

Note the ratio: 1.47 tokens per character. The pages are Russian, and Cyrillic
costs far more tokens per character than English, so this vault hits the ceiling
at roughly half the text an English one would.

## What the practice says

The standard answer to "more items than fit" is map-reduce: split into chunks
sized to the token limit, run the model on each independently, and combine the
results. Applied to review rather than summarisation, chunked processing is
reported to *improve* coverage — iteratively working through a document chunk by
chunk raised checklist recall by 28 points against whole-document prompting.

Two cautions come with it. Effective quality degrades well before the advertised
window is reached, so the window is a ceiling and not a design target; and recall
inside a long prompt is U-shaped — items in the middle are the ones a model
misses. Both argue for smaller review batches rather than a bigger prompt.

## What follows for this repository

The critique reviews operations in batches that fit, instead of refusing when all
of them together do not:

* Operations are packed greedily into batches whose prompt fits the same budget
  check the code already applies.
* Each batch is reviewed by its own call; the drop lists are merged. Every
  operation is still reviewed exactly once, and the reviewer still sees each
  operation whole, with its evidence.
* One operation whose prompt does not fit **alone** is still refused with
  `input_budget`, before any provider call — that is a deterministic refusal, and
  the test that pins it (`test_critique_budget_fails_before_second_provider_call`)
  stays exactly as it is.

What this costs is calls: a day yielding sixteen operations takes two or three
critiques instead of one. What it buys is that a long day can be compiled at all.

What it deliberately does not do: it does not raise the budget, does not review
summaries instead of bodies, and does not let an unreviewed plan through — the
compile still fails if a batch cannot be reviewed.

## Sources

- [Master LLM Summarization Strategies, Galileo](https://galileo.ai/blog/llm-summarization-strategies) — the map-reduce pattern: chunk to the token limit, map independently, combine.
- [Gavel: Agent Meets Checklist for Evaluating LLMs on Long-Context Legal Summarization, arXiv 2601.04424](https://arxiv.org/pdf/2601.04424) — chunk-by-chunk checklist extraction raises recall by 28 points over whole-document prompting.
- [LLM Context Window Management and Long-Context Strategies 2026, Zylos](https://zylos.ai/research/2026-01-19-llm-context-management/) — the advertised window is a ceiling, not a design target.
- [Long Context LLMs and the Lost in the Middle Phenomenon, QubitTool](https://qubittool.com/blog/long-context-lost-in-the-middle) — U-shaped recall inside a long prompt.
