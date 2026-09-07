# A signal stronger than a click

Dated 2026-09-06. Written before changing how retrieval ranks, because it
changes how retrieval ranks.

## What we already have and do not use

`cache/evidence-graph/telemetry.sqlite3` holds **14 806 impressions**: for each
one a query hash, the candidate shown, its rank, the retrieval mode, the
generation and the time. The `outcome` column exists on every row and is `None`
on every row.

So we record what was *shown* and never what was *useful*. Fourteen thousand
observations of half a fact.

## Why this is usually hard, and why it is not hard here

The recurring complaint in the RAG literature is that these systems have no
feedback at all: they hand the user a synthesised answer instead of links, so
the engagement data that would tell the retriever which documents helped is
simply never produced. Where feedback does exist — clicks — it is biased by
position and selection, and a whole field exists to correct it: click models as
propensity estimators, counterfactual learning-to-rank, unbiased LTR from biased
feedback, and the safety work on deploying such rankers without regressions.

**We are not in that situation.** A grounded answer here names its evidence: every
published claim carries citation ids, and those ids are exactly the spans that
carried the answer past the gates. That is not a click. It is a verified
statement, by the system that used the evidence, that this span did the work.

The bias that remains is real and worth naming: the model reads the evidence in
an order, and something never shown can never be cited. Neither is corrected
here, and neither is invented by this change — both are properties of the
retrieval that already ranks.

## The biological shape this implements

Trained immunity: a cell that has met a challenge does not store what it met, it
changes how readily it responds afterwards. Plant defence priming is the same
shape, independently evolved, and it has a genome-scale quantitative model —
along with a warning that the effect is capped by the redundancy that makes it
robust. Neither promises a large gain. Both say the mechanism is cheap and its
ceiling is low.

## What to record

One field, already present. When an answer publishes citations, the candidates
behind those citations are marked `used` on their impression rows for that query.
Everything shown and not cited stays as it is — *not cited* is not the same as
*not useful*, and writing `unused` would be a claim we cannot support.

## What to do with it

Two boosts, deliberately different in strength.

**The same question again.** Where the query hash matches exactly, a candidate
cited before is raised. Narrow — it fires only on a literal repeat — but exact
repeats are common when several agents work the same project, and the evidence is
as strong as evidence gets.

**A span that has carried answers before.** Independent of query, decayed by age.
This is the trained-immunity shape proper: a standing disposition rather than a
memory of a pairing. Weak by construction and applies everywhere.

Both **multiply an existing score** rather than replacing the ranking, both are
bounded, and both decay. The failure mode to guard against is the obvious one: a
span that was useful once becomes permanently privileged and crowds out
better evidence. Bounding and decay are the guard, and the measurement will say
whether they are enough.

## What this is not

Not learning-to-rank. Nothing is trained, no propensity is estimated, no model is
fitted. It is a counter and a decay, which is what the biology is too.

## Sources

- https://www.cs.cornell.edu/people/tj/publications/agarwal_etal_19b.pdf — a
  general framework for counterfactual learning-to-rank
- https://arxiv.org/pdf/2012.04426 — unifying online and counterfactual LTR
- https://arxiv.org/pdf/2305.01522 — safe deployment for counterfactual LTR
- https://www.researchgate.net/publication/361559565 — the implicit limits of
  click-based learning to rank
- https://cloud.google.com/blog/products/ai-machine-learning/optimizing-rag-retrieval
  — RAG retrieval practice, and the missing-feedback problem
- https://www.annualreviews.org/doi/full/10.1146/annurev-arplant-042916-041132 —
  defence priming, its quantitative model and its ceiling
