---
type: decision
status: accepted
date: 2026-08-26
confidence: medium
source_authority: ai-derived
---

# What the cross-encoder scored outranks what it never read

One-sentence summary: when the reranker reads only a bounded prefix of the
candidate pool, the rest of the pool is kept but ordered behind everything the
reranker scored — a rule about *provenance of the score*, not about relevance,
and it is written down because it decides answers and was not written anywhere
before.

## Why this is a decision and not a repair

It arrived inside a defect fix and was reported as one. The defect was real and
narrow: one page could occupy several slots of a single answer, because the
reranker returns a bounded prefix and everything below it was dropped before
the page-diverse order ran. On this vault's quarantine question, twenty
candidates reaching that order came from four pages, sixteen of them the same
document — so the last visible slots could not help repeating.

Keeping the tail is the repair. Deciding *where* the tail goes is not: nothing
in the code or the records said that a row the cross-encoder scored outranks a
row it never read. Two orderings were measured on identical captured pools:

| ordering | `hit@1` | `hit@5` | slots lost to repeats | `applied@5` |
|---|---|---|---|---|
| before | 0.50 | 0.50 | 7 | 0.857 |
| tail merged in one pass | 0.50 | 0.60 | 0 | 0.714 |
| tiered (adopted) | 0.50 | 0.60 | 0 | 0.857 |

Merging the tail in one pass removes the repeats and costs two application
cases, because unscored rows then outrank rows the cross-encoder paid for.
The tiered order removes all seven repeats and loses nothing. That is a choice
between two defensible orders decided by measurement, which is the shape of a
decision, and a rule that silently governs every reranked answer should be
readable rather than inferred from `_below_rerank_pool`.

## The rule

1. Rows the reranker scored keep the reranker's order and come first.
2. Rows below its bounded pool follow, in their fused order.
3. The vault's existing preference — compiled pages before raw evidence —
   applies *within* each tier, not across them.
4. Nothing is discarded by this rule; only the order changes, and the final
   cut is the one it always was.

## What this decision does not claim

- Not that an unscored row is less relevant. It is less *evidenced*: the only
  thing known about it is that the reranker never looked. Ranking it below a
  scored row is a statement about what was measured, not about the page.
- Not that the tier boundary is optimal. It was compared against exactly one
  alternative, on ten questions of one vault, on captured pools.
- Not that the numbers above transfer. The stands wobble between runs of
  identical code because the optional legs are deadline-bounded; these figures
  come from scoring three orderings on the *same* captured pools precisely
  because the full-stand comparison was not trustworthy.
- Nothing here applies when the cross-encoder is off, which is the default: no
  rows are scored, so there are no tiers.

## Source / Evidence

- Code: `_candidates_after_rerank` and `_below_rerank_pool` in
  `scripts/retrieval.py`.
- Measurement and the rejected alternative: `NEW-98` in
  `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`.
- The same defect on the other side of the reranker, fixed 2026-08-24:
  [[knowledge/notes/one-trust-weight-across-retrieval-paths-decision]].

## Related

- [[knowledge/notes/one-trust-weight-across-retrieval-paths-decision]] — the
  weight that decides order; this decides what order the weight sorts within.
- [[knowledge/notes/nightly-builds-generation-vectors-decision]] — the
  generation whose vectors feed the pool these tiers reorder.
