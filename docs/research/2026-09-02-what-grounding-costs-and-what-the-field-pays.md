# What grounding costs, and what the field pays for it

Dated 2026-09-02. Written because the answer policy was chosen on sound
reasoning that never priced itself, and today it has a price.

## What we measured first

One run of 200 questions on the turn-bounded corpus, judged:

```
answered                66   correct 51   selective accuracy 0.77
refused                 94   of which the labelled session ranked first: 54
rejected by the gates   28
overall judge accuracy       0.3229
```

The baseline arm, three runs, sits at 0.2750 ±0.0074 with the same shape: 64
answered, 112 refused, 13 gate rejections.

So the system is right in three answers out of four, and answers one question in
three. That is not a quality failure in the ordinary sense. It is a system
operating at low coverage.

## The frame the field uses, which we were not using

The published comparison numbers — 0.878, 0.944 — are **overall** accuracy on a
benchmark that scores an abstention as wrong. Ours is overall accuracy too, but
our system is a selective predictor: it declines most questions.

The literature calls this the risk-coverage trade-off, and treats coverage as a
**threshold to tune**, not a property to fix. The numbers it reports run in our
direction and confirm the shape: one 2026 RAG system moves from 52.6% to 62.0%
accuracy by abstaining on low confidence, and to 78.0% selective accuracy at 50%
coverage. Our 0.77 at 33% coverage is on that same curve, further along it.

That reframing is the single most important thing in this note. Comparing our
0.32 against their 0.88 compares a selective system at one third coverage with
systems that answer everything. It is a real gap, but roughly half of it is a
policy choice, not a capability deficit.

## What the sources say about our specific mechanism

The parts of our design that the evidence supports:

- **Schema-level binding of claims to citations is right and load-bearing.**
  Enforced schema binding improves citation F1 substantially, and removing the
  constraint makes it drop sharply, because models otherwise treat citations as
  post-hoc footers and fail to map claims to sources even when the right
  evidence was retrieved.
- **The failure it prevents is large and real.** Up to 57% of citations in RAG
  outputs are post-rationalised: the model answers from parametric memory and
  then cites a superficially matching document. Hallucination rates around 37–41%
  are reported for both generation-time and post-hoc citation paradigms.

The part the evidence does **not** support, because nothing in it is addressed:

- **Discarding the entire answer when one citation fails a check.** No source
  proposes that. Attribution work measures citation precision and recall and
  reports them; it does not throw the answer away. Our
  `verify_grounded_answer` raises on the first failing claim, and 28 of 200
  answers were destroyed that way — 14% of the sample, after the model had
  already produced them.

Our decision page for the relevance gate carries `source_authority: user` and
zero external citations. The reasoning behind grounding is documented and sound;
the severity of the enforcement was an in-house choice that had never been
measured until today.

## What we cannot measure yet, and why

The 28 rejected answers are gone. The harness records `status=error` and the
gate's message; `hypothesis` is empty. So the accuracy of what the gates
discarded is **unknown**, and every option below that touches them can only be
bounded, not costed:

- if every rejected answer was right: +0.14 overall
- if none was: +0.00

That range is too wide to choose on, and closing it costs nothing but a field.

## Sources

- [Know Your Limits: A Survey of Abstention in Large Language Models — TACL](https://direct.mit.edu/tacl/article/doi/10.1162/tacl_a_00754/131566/Know-Your-Limits-A-Survey-of-Abstention-in-Large)
- [Confidence-Based Abstention — EmergentMind](https://www.emergentmind.com/topics/confidence-based-abstention)
- [Beyond Semantic Relevance: Counterfactual Risk Minimization for Robust RAG (arXiv 2605.01302)](https://arxiv.org/pdf/2605.01302)
- [Knowing When to Quit: A Principled Framework for Dynamic Abstention in LLM Reasoning (arXiv 2604.18419)](https://arxiv.org/pdf/2604.18419)
- [Citation-Closure Retrieval and Per-Rule Attribution for Regulatory Compliance QA (arXiv 2605.29742)](https://arxiv.org/html/2605.29742v1)
- [Generation-Time vs. Post-hoc Citation: A Holistic Evaluation of LLM Attribution (arXiv 2509.21557)](https://arxiv.org/pdf/2509.21557)
- [Attribution Techniques for Mitigating Hallucinated Information in RAG Systems: A Survey (arXiv 2601.19927)](https://arxiv.org/pdf/2601.19927)
- [Cited but Not Verified: Parsing and Evaluating Source Attribution in LLM Deep Research Agents (arXiv 2605.06635)](https://arxiv.org/html/2605.06635v1)
- Measurement: `benchmark/longmemeval-unit-n200-r1-judged.json`,
  `benchmark/longmemeval-baseline-n200-r{1,2,3}-judged.json`
- Prior reasoning: `docs/research/2026-08-24-what-an-answer-should-admit.md`
