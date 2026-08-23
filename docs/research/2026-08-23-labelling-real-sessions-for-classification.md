# Labelling real sessions to measure classification (2026-08-23)

## Why this was researched

`OPEN-034` says the quality of session classification is unmeasured: how often a
session that held a decision, a fix or a gotcha ends up without it in durable
memory is unknown. The measurement stand exists
(`benchmark/run_flush_classification.py`) and scores the product's own prompt,
but the corpus shipped with it is nine synthetic public cases. The register also
said the real number could not be produced here because there was no installed
runtime. That stopped being true on 2026-08-21, when the vault and the source
became one directory: this machine holds 65 real session transcripts under
`~/.claude/projects/`.

What was missing was not the material but the labels. A benchmark needs ground
truth, and the question is where ground truth comes from when the only reader
available is a model of the same family as the system under test.

## What current practice says

The shape of the answer is consistent across current guidance:

- Ground truth for a classification benchmark is **human-labelled**, with a
  minimum around 30–50 examples for a basic setup and 100–200 for a
  production-grade gold set, chosen for diversity of input type and failure mode.
- Where two annotators are available, label the same examples with both and
  **discard the items they disagree on** — persistent disagreement means the
  rubric, not the annotator, is wrong.
- An LLM judge is calibrated **against** those human labels, not instead of them:
  sample a few hundred cases, measure agreement, and iterate until correlation is
  high; in production, recompute Cohen's kappa periodically and alert when it
  falls below roughly 0.6.
- The recommended division of labour is automated judging at scale plus targeted
  human review of flagged cases. A judge is an amplifier of a rubric a human
  agreed with, never a replacement for having one.

Nothing in that guidance supports treating model-produced labels as ground truth
on their own, and the failure mode it warns about is exactly ours: a judge from
the same family as the classifier shares its blind spots, so agreement measures
family consensus rather than correctness.

## What this means here

The vault has one human — the owner — and his attention is the scarce resource
the whole product exists to protect. Asking him to hand-label 100 sessions to
close an audit item would spend precisely what the system is for.

So the honest construction is a two-stage one:

1. **Provisional labels, produced independently of the system under test.** A
   separate rubric prompt reads each real transcript and answers one question —
   does this session contain a durable decision, fix or gotcha, and which phrases
   carry it — without seeing the product's tier names or its classification
   prompt. Those labels, and the phrases they point at, are frozen into the
   corpus.
2. **Human review as the calibration step, on the cases that matter.** The
   corpus records for every case whether a human has confirmed its label. Numbers
   measured against unreviewed labels are reported as provisional and say so in
   the report itself; agreement (Cohen's kappa) is computable the moment any
   subset is reviewed.

This produces a real number from real sessions today, states exactly what it
rests on, and leaves a path to a calibrated number that costs the owner minutes
rather than days.

## What stays out

- The corpus itself is never committed. It carries real session text from the
  owner's other projects; it lives at a gitignored path, and only the aggregate
  numbers are published.
- No gate is tightened on provisional numbers. A release gate that fails on a
  label nobody has checked would be a rule that cannot be trusted, which this
  vault has already learned to avoid.

## Sources

- Openlayer, "LLM-as-judge: A complete guide to evaluation best practices" —
  https://www.openlayer.com/blog/llm-as-judge-evaluation-guide
- DeepEval, "LLM-as-a-Judge in 2026: Top evaluation techniques and best
  practices" — https://deepeval.com/blog/llm-as-a-judge
- Future AGI, "LLM-as-Judge Best Practices in 2026: Calibration, Bias, and Cost"
  — https://futureagi.com/blog/llm-as-judge-best-practices-2026
- Future AGI, "LLM-as-a-Judge in 2026: How It Works, When It Fails" —
  https://futureagi.com/blog/llm-as-a-judge/
- Label Your Data, "LLM as a Judge: A 2026 Guide to Automated Model Assessment" —
  https://labelyourdata.com/articles/llm-as-a-judge
