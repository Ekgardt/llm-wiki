# A decision rule stated before the run

Dated 2026-08-31. Written before building the comparison stand, because a rule
chosen after seeing the numbers is not a rule.

## Why this is item one

Six LongMemEval runs on this vault, n=50, seed 13:

```
0.2857  0.3200  0.3469  0.3542  0.3958   (and 0.1667 on an instrumented run)
```

The five comparable runs span **0.11**; the four before the packing change span
0.07. Every published figure the backlog compares against is a **three-run
mean over all 500 questions**.

So the current stand cannot distinguish a real seven-point improvement from
another draw of the same distribution. Yesterday's coverage-first packing change
produced the highest number of the five and I could not call it a win, which is
the correct outcome and also an unaffordable one: the largest item in the
backlog, turn-granular retrieval, carries a real risk of *losing* accuracy on
the categories that currently work, and a stand that cannot see 0.07 cannot see
that either.

## What the field does, and what it does not

Published agent-memory results are reported as multi-run means — three runs is
the common figure — over the full question set, with per-category breakdowns.
None of the sources sampled report a confidence interval or a significance test;
the discipline is repetition and a stated question set, not statistics.

Two things follow. First, matching the field means repetition, not a t-test.
Second, a comparison between our own arms needs a rule anyway, or every run
becomes an argument.

## The rule

**An arm wins a category only if its mean over three runs exceeds the baseline
arm's mean by more than the baseline arm's own observed spread in that
category.**

Spread is `max - min` across that arm's runs — the plainest statement of "how
much this number moves when nothing changed". It is deliberately conservative:
a change that cannot clear the noise the baseline already shows is not evidence,
whatever its direction.

Two corollaries, both of which matter more than the win condition:

- **A category that gets worse by more than the baseline spread is a loss**, and
  a loss blocks the change even when the overall mean improves. This is the
  guard Q1 needs: single-session recall is our strongest suit precisely because
  a whole session arrives as one unit.
- **Everything else is "no difference measured"** — not "no difference", and
  not a win. Reporting it as a win is the failure this rule exists to prevent.

## Sample size

n = 200 stratified, three runs per arm. Two reasons, neither of them a
calculation:

- At n = 50 a category holds 3 to 13 questions, so one answer moves a category
  by 8 to 33 points. At n = 200 the smallest categories hold four times as many
  and a single answer moves them a quarter as far.
- Three runs of 200 at roughly 35 s per question is about six hours per arm, and
  a paired comparison is two arms. That is an overnight job, which is the
  longest cadence that still lets a change be judged the next day.

The full 500 would be better and takes fifteen hours per arm. It is the right
size for a final claim against published numbers, not for choosing between two
of our own arms.

## What the stand must report

Per category and overall, per arm: mean, min, max, spread, and n. Then the
verdict per the rule, in three states — **win**, **loss**, **no difference
measured** — with the numbers that produced it beside it. The verdict is
computed, not written by hand, so it cannot be revised after the fact.

Ungraded questions must stay out of accuracy and be reported on their own line.
Yesterday's harness-failure run reported `accuracy 0.0` with `provider_failures
0` because a missing directory failed all fifty questions in two seconds each;
a comparison stand that folds those into a mean will confidently prefer the arm
that crashed less.

## What this does not settle

Whether three runs is enough. It matches the field's practice and it is what
fits an overnight window; if the spread at n = 200 turns out to be as wide as at
n = 50, the answer is more runs, not a cleverer rule.

Whether our own accuracy is comparable to a published one at all. Ours is a
local judge over a local vault; theirs is a different answer model over a
different pipeline. Cross-system comparison stays a rough orientation, and only
arm-versus-arm on this stand is a measurement.

## Sources

- [State of AI Agent Memory 2026: Benchmarks & Trends — Mem0](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [AI Memory Benchmarks 2026: LoCoMo, LongMemEval & BEAM — Mem0](https://mem0.ai/blog/ai-memory-benchmarks-in-2026)
- [Storage Is Not Memory: A Retrieval-Centered Architecture for Agent Recall (arXiv 2605.04897)](https://arxiv.org/html/2605.04897v1)
- [LongMemEval-V2: Evaluating Long-Term Agent Memory (arXiv 2605.12493)](https://arxiv.org/html/2605.12493v1)
- Prior analysis: `docs/research/2026-08-30-what-actually-closes-the-quality-gap.md`
