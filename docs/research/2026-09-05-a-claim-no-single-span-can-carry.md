# A claim no single span can carry

Dated 2026-09-05. Written because the largest remaining bucket of wrong answers
is one shape, and the field does not hand us a solution for it.

## The observation

Of 50 questions on the stand, eight were answered and judged wrong. **Seven of
the eight need arithmetic across sessions**, and the recorded answers show the
model finding every input and then reporting the inputs instead of the answer:

| question | gold | what was said |
|---|---|---|
| how many dozen eggs are stocked | 20 | quotes one older figure |
| how many issues finished | Five | "just finished their third, currently on their fourth" |
| how many days on faith activities in December | 3 | lists the days |
| how many delivery services used | 3 | names one and describes another |
| how much spent on gifts for my sister | $300 | "a necklace that cost around $200" |
| total days in Japan and Chicago | 11 | describes one trip |
| how long a member when I attended | Two weeks | gives both dates, not the gap |
| how long taking lessons when I bought the amp | Four weeks | quotes "six weeks now" |

It spans three categories — knowledge-update, multi-session,
temporal-reasoning — so it is not a category quirk. It is the contract.

## Why the contract produces exactly this

Every atomic claim must be carried by a cited span, and `_require_figures_agree`
refuses a claim whose figures appear in no cited span. A sum, a count, a date
difference and a "latest of several" are in no span by construction. The only
answer the contract permits is to quote an input — which is what the model does,
and it is wrong.

The rule was written against a real failure: a citation from the right page and
the wrong sentence, which reads as support. It still catches that. It also
catches every correct aggregate, and until this measurement nobody had counted
the second class.

## What the field has

Not much, and it is worth saying so rather than implying otherwise.

Citation evaluation in 2026 is organised around three rubrics — structural
(is there a citation in the right schema), resolvability (does it point at a real
source), and semantic (does the source actually contain the claim). All three
assume the claim is *in* a source. The standard method is atomic-claim
decomposition, scoring each (claim, passage) pair for entailment and aggregating
over claims — again, per claim, against one passage.

The named risk in this literature is **evidence-boundary overrun**: a claim
exceeding what its cited documents strictly justify. A computed claim is, by
that definition, always an overrun.

CAGE (2026), the closest work — attribution graphs for inline citation in
long-form QA — has query nodes, document nodes and answer nodes, and **no node
type for a derived claim**. Its validator asks whether the assigned documents
"collectively provide sufficient evidence" and does not say how arithmetic or
temporal calculation would be assessed. The gap is open, not solved.

So this is a design we make, not one we adopt, and it should be conservative.

## The shape proposed

A claim may declare that it is derived, name the kind of derivation from a small
closed set, and cite the spans that supply its inputs.

- **Every input is still cited, resolved, and checked to touch the claim.**
  Nothing about that loosens.
- **The output figure is not required to appear in a span** — that is the whole
  point — but only for a claim that declares a derivation and cites more than
  one span. A single-span "derivation" is not one.
- **The derivation is stated in the answer**, so a reader recomputes rather than
  trusts.

What this does not do, and must be said in the same breath as what it does: **we
do not verify the arithmetic.** We make it checkable. That is the same honesty
the citation gate already practises — it verifies that a span exists, touches
the claim and agrees on figures, and has never claimed to verify entailment.

## The risk, named

This is precisely where a fabricated number could hide: cite two real spans,
assert a total nobody can check. The mitigations are that the inputs must be
present and verified, the derivation kind is declared, and the answer shows its
working. A reader who wants certainty has everything needed to recompute, which
is more than they had when the system quoted one input and called it an answer.

Whether this trades accuracy for coverage in the wrong direction is a
measurement, not an argument, and it will be measured before it is kept.

## Sources

- https://futureagi.com/blog/evaluating-llm-citation-attribution-2026/ — the
  three rubrics, atomic-claim decomposition, evidence-boundary overrun
- https://arxiv.org/html/2607.24236v2 — CAGE: cognitive attribution graphs;
  node types, and the absence of one for derived claims
- https://arxiv.org/pdf/2510.00361 — attribution gradients and claim
  decomposition for critical examination
- https://arxiv.org/pdf/2601.21387 — user-centric evidence ranking for
  attribution and fact verification
