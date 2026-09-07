# Throwing away right answers, and whether the shape is wrong

Dated 2026-09-02. Two questions from the owner: losing correct answers is
unacceptable, and is the architecture itself the problem.

## Measured first

With the discarded replies now recorded, the strictest rule can finally be
priced. In one arm, eleven answers were destroyed by `verify_grounded_answer`.
Parsing the stored replies and checking them against the gold answer:
**seven of the eleven contained the correct answer.**

So the rule is not mostly catching fabrication. It is mostly destroying correct
work over a citation that did not line up.

## Why it destroys them

Our gates are word-overlap and figure-overlap tests, applied per claim, and the
*answer* is rejected when any single claim fails. Two mechanical consequences:

- A correct claim whose citation points at the neighbouring span fails a
  word-overlap test even though the fact is in the evidence.
- One such claim destroys the other five that were fine.

## What the field does instead

The pattern is claim-level verdicts with answer-level aggregation, not
answer-level rejection.

- MedRAGChecker operates at the claim level and aggregates verified claims into
  answer-level diagnostics, giving each claim a discrete verdict — **Entail,
  Neutral, or Contradict** — with a calibrated support score for how likely it
  is supported under the available evidence.
- Sentence-level attribution with source-span pointers plus **NLI re-scoring**
  tests whether a passage entails a sentence, with the option to **abstain when
  evidence is thin rather than reject wholesale**.
- ClaimVer and the law-domain claim-level benchmark take the same shape.

Two differences from ours, and both matter:

1. **Entailment, not overlap.** An NLI model judges whether the span supports
   the sentence. Word overlap is a proxy that fails on paraphrase and on a
   correct fact cited one span away. Our own decision page for the relevance
   gate says outright that entailment is *not* verified and not claimed — the
   field has since made it cheap enough to verify.
2. **The unit of rejection is the claim, not the answer.** An unsupported
   sentence is dropped or marked; the supported ones are still delivered.

## What that would have meant here

Of the eleven destroyed answers, seven carried the correct fact. Under
claim-level handling the reader would have received those seven with the
offending sentence removed or flagged, instead of a refusal. On this arm that is
roughly a third of all errors turning back into answers, with the grounding
promise intact for every sentence that is delivered.

## Is the architecture wrong

Partly, and precisely.

What is obsolete is the **retrieve-once-then-generate** shape — 2026 sources
call standard RAG dead for static corpora under a million tokens, and say the
term now covers three different architectures.

What is not obsolete is retrieval itself. The numbers are one-sided: RAG is
about **1250× cheaper per query** than putting everything in context, and long
context **loses 30%+ accuracy when the relevant content sits mid-window**. And
our own control: with retrieved evidence removed, every reader tested on this
benchmark scores below 4%.

The winning production pattern in 2026 is neither: *use retrieval to select a
subset, then hand that subset to a long-context model to reason over.* That is
exactly the arm measured today — forty candidates instead of twelve, in a wide
window — and it moved the answered share from a third to a half in the first
fifty questions.

Above that sits **agentic RAG**, where the system decides whether to retrieve,
what, and when to stop, rather than retrieving once. That is the two-pass idea
from the recall research, arrived at from a third direction.

So the verdict is not "rewrite it". It is: our retrieval and storage are sound,
our *shape* is the 2023–2024 one, and the 2026 shape is two changes we have
already begun measuring — a generous subset instead of a thin one, and a second
pass instead of a single shot.

## What I would change, in order

1. **Claim-level verdicts instead of answer-level rejection**, with entailment
   scoring rather than word overlap. This is the one that stops destroying
   correct work, and it is measured at seven answers in eleven.
2. **Retrieval depth**, already being swept, to find the point where more
   candidates stop paying for their tokens.
3. **A second retrieval pass** conditioned on what the first returned.

None of these is a rewrite. The storage layer, the corpus, the generations and
the journal are untouched by all three.

## Sources

- [MedRAGChecker: Claim-Level Verification for Biomedical RAG (arXiv 2601.06519)](https://arxiv.org/html/2601.06519)
- [ClaimVer: Explainable Claim-Level Verification — EMNLP Findings](https://aclanthology.org/2024.findings-emnlp.795.pdf)
- [Fine-grained Claim-level RAG Benchmark for Law (arXiv 2605.21071)](https://arxiv.org/pdf/2605.21071)
- [Diagnosing and Repairing Factual Errors in RAG under Budget Constraints (arXiv 2606.29377)](https://arxiv.org/pdf/2606.29377)
- [Standard RAG Is Dead: Why AI Architecture Split in 2026](https://ucstrategies.com/news/standard-rag-is-dead-why-ai-architecture-split-in-2026/)
- [Context architecture is replacing RAG — VentureBeat](https://venturebeat.com/data/context-architecture-is-replacing-rag-as-agentic-ai-pushes-enterprise-retrieval-to-its-limits)
- [RAG vs long context: what the 2026 data shows — Wire](https://usewire.io/blog/long-context-vs-rag-what-the-data-shows/)
- [In Defense of RAG in the Era of Long-Context Language Models (arXiv 2409.01666)](https://arxiv.org/pdf/2409.01666)
- [Is Agentic RAG worth it? An experimental comparison (arXiv 2601.07711)](https://arxiv.org/pdf/2601.07711)
- Our measurement: `/home/user/.claude/jobs/80be9db9/tmp/depth-40/r1.jsonl`
- Our gate: `scripts/query_memory.py::_require_citation_touches_claim`,
  `_require_figures_agree`, `verify_grounded_answer`
