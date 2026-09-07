# How much evidence is too much

Dated 2026-09-02, **corrected and settled 2026-09-03**. Written because 22 of
200 questions refused to answer while the packed prompt already carried the
session that holds the answer.

**Read the verdict at the bottom before the argument.** The measurement went
against the literature on our own stand, and two of the numbers below were mine
and wrong.

## The observation that started this

On our own stand, seed 101, n=200, after the grounding gates were fixed:

- Retrieval brings back the labelled answer session for 162 of the 186
  questions that have one, and ranks it first in 143 of them.
- 87 non-abstention questions produced no answer; 62 of those had that session
  in hand.
- Answered and refused rows are **indistinguishable on packing**: both average
  ~168 chunks, ~117 000 packed *bytes*, ~12 spans shed by the packer, and the
  same rank distribution. (Bytes. I read that field as tokens when first writing
  this note. The real prompt averages **26 429 tokens** — see the verdict.)

So the failures are not a retrieval failure and not a packing failure. Whatever
is going wrong happens inside the model, with the evidence present.

## What the field measured

**More passages is an inverted U, and the peak is low.** Distraction-aware
retrieval work (arXiv 2509.21865) reports accuracy peaking around **10-20
passages** and falling after it: recall keeps rising with k while precision
falls, and end-to-end accuracy sits below recall at every k — the model fails to
use relevant text that is present.

**The fall is not small.** "When More Documents Hurt RAG" (arXiv 2606.11350)
measures HotpotQA at roughly **75% accuracy at k=5 and 45% at k=20** — thirty
points lost to twenty documents. Filtering to a domain-relevant subset before
generation recovers 20-25 of them.

**Length alone does it, with no distractors at all.** "Context Length Alone
Hurts LLM Performance Despite Perfect Retrieval" (arXiv 2510.05381, EMNLP 2025
Findings) holds the gold evidence fixed and pads the rest. Open models lose
24-34% on MMLU and GSM8K at 30 000 tokens; even with the padding fully **masked**,
so the model attends only to the evidence and the question, performance still
drops 7.9-50%. Closed models degrade less, but consistently.

Their remedy is "retrieve-then-reason": have the model recite the relevant
evidence first, then answer from the recitation only, turning a long-context
task into a short-context one. Reported up to +4% on GPT-4o's already strong
RULER numbers.

## What this says about our numbers

We pack about 168 chunks. The token count is **26 429**, not the 117 000 this
note first claimed — that figure was `packed_tokens`, which counts bytes. So we
sit just *under* the 30 000 tokens at which the length paper measures severe
degradation, not four times past it. The passage count is still an order of
magnitude past the 10-20 peak.

**The honest complication:** our own measurement moved *up* when we widened to
40 candidates and a 122 880-byte answer budget. That comparison is confounded.
It was taken while the grounding gates were still destroying whole answers, and
a wider window produced more chances for at least one claim to survive. Now that
a failing claim is dropped instead of fatal, the wide window may be paying for
itself with distraction and no longer buying anything.

That is a hypothesis, not a finding. It is cheap to settle: the window is an
environment variable on the stand, so the sweep needs no code change at all.

## What is worth doing, in order

1. **Sweep the window.** n=50, seed 101, candidates fixed at 40 so retrieval
   recall does not move, answer budget 122 880 / 32 768 / 12 288. Only what
   reaches the model changes. If the curve has a peak below the current setting,
   the default moves and the gain is free.
2. **Turn on the reranker.** Retrieval already ranks the answer session first
   most of the time, so the value is not in finding the session — it is in
   choosing which spans inside it survive the cut, which is exactly what a
   cross-encoder does and what the packer currently does by score alone.
   Self-hosted, permissively licensed candidates in 2026 are BGE reranker v2-m3
   (the common default) and Qwen3-Reranker (stronger on published rankings,
   Apache-2.0, multilingual). Both need a pinned revision.
3. **Consider retrieve-then-reason** for the categories that reason over several
   sessions. Temporal reasoning is our weakest category and the one where the
   recorded refusals show the model resolving a date correctly and then declining
   because no single span states it. That is the shape the recite-first method
   addresses. It costs a second model call per question, so it must be priced
   against the token budget before it is adopted.

## What this research does not settle

Nothing here measures **our** corpus, our chunk sizes, or our model. Every
number above comes from other people's benchmarks with other retrievers. They
establish that the effect is real, large, and reproducible across labs; they do
not tell us where our own peak is. Item 1 exists to answer that on our own
stand, and no default should move before it does.

## Sources

- https://arxiv.org/pdf/2509.21865 — Beyond RAG vs. Long-Context: Learning
  Distraction-Aware Retrieval for Efficient Knowledge Grounding (ICLR 2026)
- https://arxiv.org/pdf/2606.11350 — When More Documents Hurt RAG: Mitigating
  Vector Search Dilution with Domain-Scoped, Model-Agnostic Retrieval
- https://arxiv.org/html/2510.05381v1 — Context Length Alone Hurts LLM
  Performance Despite Perfect Retrieval
- https://arxiv.org/pdf/2410.05983 — Long-Context LLMs Meet RAG: Overcoming
  Challenges for Long Inputs in RAG
- https://arxiv.org/pdf/2411.05928 — Reducing Distraction in Long-Context
  Language Models by Focused Learning
- https://redis.io/blog/top-reranking-models-rag-accuracy/ and
  https://futureagi.com/blog/best-rerankers-for-rag-2026/ — 2026 reranker
  shortlists and published scores

---

## Verdict, measured 2026-09-03

The sweep proposed above was run: n=50, seed 101, candidates fixed at 40, only
the answer budget moved. Judged by the same LLM judge that scores every other
arm.

| answer budget | prompt tokens | answered | correct | correct per question | correct when answering |
|---|---|---|---|---|---|
| 122 880 | 26 429 | 33/50 | 27 | **0.5400** | 0.8182 |
| 32 768 | 6 829 | 25/50 | 18 | 0.3600 | 0.7200 |
| 12 288 | 1 843 | 14/50 | 8 | 0.1600 | 0.5714 |

**The inverted U does not appear below our current setting, in either
direction.** Narrowing the window loses coverage *and* precision at once:
answers fall from 33 to 14, and accuracy among the answers that remain falls
from 0.82 to 0.57. There is no distraction penalty to reclaim here. The
hypothesis this note was written to test is refused by our own data.

### Why the field's result does not reproduce here

Stated as inference, not fact. Two differences look sufficient:

- **The distractors are not distractors.** Those benchmarks retrieve k passages
  from an open corpus, where most of the k are about something else. Our
  retrieval puts the labelled answer session first for 143 of 186 questions, so
  the extra spans are mostly neighbouring turns of the *right* conversation.
  Cutting the budget removes evidence before it removes noise.
- **The model.** The length paper's large drops are on 7-8B open models; closed
  models degrade far less, and this stand runs one.

### What this changes in the plan

- **The window stays.** No default moves. The remaining two items from the plan
  above change rank:
- **Reranking drops in priority.** Its value was supposed to be discarding
  distractors, and there is no distraction penalty to collect at this size.
  Worth keeping only if it can raise coverage inside the same budget.
- **Retrieve-then-reason keeps its priority**, for a different reason than the
  one given above. It is not compensation for an overlong context — the context
  is not overlong. It is a possible answer to the 22 questions that refuse with
  the evidence in hand.
- **Above 122 880 the curve stops.** At 262 144 the same 50 questions score
  0.4600 per question against 0.5400, on 32 answers against 33 — and the prompt
  only grows from 26 429 to 29 995 tokens, because at that point the candidate
  cap of 40 binds and not the budget. A four-correct gap on n=50 is inside
  run-to-run noise, so the honest reading is **flat, not better**, and the
  measurement was stopped rather than spent on a third point that could not move
  the prompt either. 122 880 stays the default: it is the last setting where
  raising the budget still buys anything.

### Two corrections to my own numbers, above

`packed_tokens` counts **bytes**. The "117 000 tokens" in the original text was
wrong; the prompt is 26 429 tokens. That moves us from "four times past the
danger threshold" to "just under it", which weakens the case this note was
built on — and the sweep then refused it outright.
