# A category graded by the wrong question

Dated 2026-09-01. Written before changing the judge and the answer policy,
because both decide what a number means.

## What the measurement showed

Three baseline runs at n=200 put `single-session-preference` at **0.0000 with a
spread of 0.0**, twelve questions, three times. Nothing else in the report is
flat like that; the next weakest category, temporal reasoning, moves by 0.0172
between runs.

A flat zero is not a quality gap. It is a mechanism.

Reading one question end to end settles it. Question `0a34ad58`:

```
question : I'm a bit anxious about getting around Tokyo. Do you have any helpful tips?
gold     : The user would prefer responses that utilize their existing resources,
           such as their Suica card and TripIt app, to provide personalized tips…
retrieval: answer_session_rank = 1, answer_sessions_retrieved = 2
answer   : (empty), status = insufficient_evidence
```

The labelled session was retrieved and ranked **first**. The evidence was in
front of the model. It refused anyway.

## Two separate faults, and the order they must be fixed in

**The judge asks the wrong question.** Our `JUDGE_SYSTEM_PROMPT` grades whether
"the model answer states the same fact as the gold answer". For this category
the benchmark does not do that. The preference category "evaluates subjective
and personalized generation quality rather than exact factual retrieval", and
because it turns on satisfying a grading rubric rather than matching a short
answer, token-overlap metrics are reported as inapplicable and a judge score is
used instead. Our gold text is not a fact; it is a description of what a good
answer would take into account.

So even a good answer would be graded wrong here. That is a measurement bug of
the same family as yesterday's token cost: the stand reports a number that does
not mean what its column says.

**The answerer refuses by design.** The question is not a lookup. Nothing in the
retrieved spans "states" the answer, because the answer is a recommendation.
`_qa_system_prompt` tells the model to abstain when no cited span supports the
answer, and it does exactly that, twelve times out of twelve.

The judge fix must come first. Fixing the answerer while the judge still asks
for fact equality would produce a change that cannot be measured — the failure
this whole measurement effort exists to stop.

## What the field does

The category is small and noisy by construction: thirty questions in the full
set, where one flip moves the score by 3.3 points. That is a reason to report it
with its spread, not a reason to ignore it — it is 6% of the score and currently
all of it is forfeited.

Systems that score well here are described as applying retrieved user
information to generate a personalised, context-aware response. The mechanism is
not a looser citation rule; it is a different **kind** of answer.

## The decision

**The judge grades this category against the gold as a rubric, not as a fact.**
For `single-session-preference`, the judge is asked whether the model's answer
respects what the gold says the user would prefer — whether it takes those
things into account — rather than whether it states the same fact. Every other
category keeps the existing fact-equality prompt, unchanged.

**The answerer may answer an advice question, and every fact in it still needs a
citation.** This is the part that must not be fudged. The abstention rule exists
to stop fabrication and it stays: a claim about what the user said, owns, did or
prefers must cite the span it came from. What changes is that a *recommendation
built on those cited facts* stops being treated as an unsupported claim. The
recommendation is marked as advice, not presented as something the vault
remembers.

That distinction is the whole safety argument. "You mentioned you have a Suica
card [cite]" is a claim and needs its span. "So you could top it up at the
airport" is advice, and the vault must not pretend it is a memory.

## The cost of being wrong

**Too permissive** and the answerer starts producing plausible advice on thin
evidence, which is the failure mode the abstention rule was built for. The guard
is that the citation requirement is unchanged for every factual sentence, and
that the change is measured against the baseline arm with the loss rule: if any
other category drops by more than its baseline spread, the change is blocked.

**Too strict** is the present state and it is measured: 0.0000, three times,
twelve questions, with the right evidence ranked first.

## What this does not settle

Whether our judge agrees with the benchmark's official grader. Ours is a local
model with a local prompt; the published numbers use theirs. Cross-system
comparison on this category stays a rough orientation, and only arm-versus-arm
on this stand is a measurement — which is now true of every category, not just
this one.

## Sources

- [AI Memory Benchmarks 2026: LoCoMo, LongMemEval & BEAM — Mem0](https://mem0.ai/blog/ai-memory-benchmarks-in-2026)
- [Human-Inspired Memory Architecture for LLM Agents (arXiv 2605.08538)](https://arxiv.org/pdf/2605.08538)
- [Beyond Recall: Behavioral Specification as an Interpretive Layer for AI Personalization (arXiv 2605.28969)](https://arxiv.org/pdf/2605.28969)
- [Observational Memory: 95% on LongMemEval — Mastra Research](https://mastra.ai/research/observational-memory)
- Measurement: `benchmark/longmemeval-baseline-n200-r{1,2,3}.json`
- Rule: `docs/research/2026-08-31-a-decision-rule-stated-before-the-run.md`
