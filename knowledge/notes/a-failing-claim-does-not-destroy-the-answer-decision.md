---
type: decision
status: active
confidence: high
source_authority: ai-derived
date: 2026-09-02
---

# A Failing Claim Does Not Destroy The Answer

One-sentence summary: the grounding gates are applied per claim and enforced
per claim — a claim whose citation fails is dropped, an unresolvable citation is
dropped, and the rest of the answer stands.

## Context

The grounded answer contract required every atomic claim to carry an adjacent
citation that resolves, overlaps the claim in words, and agrees with it on
figures. Each of those gates was correct. What was wrong was the blast radius:
any single failure raised, and the whole answer died with it.

Measured on this vault 2026-09-02, recording the discarded replies for the first
time: of eleven answers the gates destroyed, **seven carried the correct
answer**. The rule was not mostly catching fabrication. It was mostly destroying
correct work.

Fixing it per claim left a second copy of the same mistake one level up. The
citation list was verified as a block before any claim was looked at, so one bad
entry — including entries no surviving claim used — still took everything.
That accounted for **18 of 200** questions in the same measurement.

## Decision

Both gates drop rather than raise.

- A citation that does not resolve is not added to the verified set.
- A claim citing something not in that set fails its own gate and is dropped.
- Kept claims publish exactly the citations they use.
- When nothing survives, the result is `insufficient_evidence` with a reason —
  not an error. "No cited span supports this" is an abstention, not a crash.

A field the schema itself forbids is still refused outright. Malformed input is
not the same thing as unsupported input.

## What is unchanged

Every claim that reaches the reader still carries a citation that resolves
against the vault at the recorded revision, hashes to what generation was shown,
touches the claim in words, and agrees with it on figures. Nothing was loosened.
The claim-level verdict is the shape the 2026 attribution literature uses;
answer-level rejection is a shape no source proposes.

## Consequences, measured

Over the same 200 questions, seed 101, stacked with two other unmeasured changes
(bounded retrieval spans, candidate depth 40 with a wider answer budget):
answered rose from 64-69 to 102, and correct answers per question from
0.2133 across three baseline runs, which spanned 0.2100 to 0.2150, to 0.4300 in
one run. Accuracy when answering rose from 0.61-0.67 to 0.84.

**Attribution is not established.** Three changes are stacked, and the candidate
has run once where the decision rule requires three. The citation-level half of
this decision is not in that figure at all — it was measured as a defect count,
not as an arm.

## Source / Evidence

- `docs/research/2026-09-02-throwing-away-right-answers-and-whether-the-shape-is-wrong.md`
- `scripts/query_memory.py` — `_verified_citations`, `_citation_verifies`,
  `_claim_survives`, `_kept_claims`, `verify_grounded_answer`
- `tests/test_grounded_qa.py` — `_nothing_reaches_the_reader`
- Runs: `benchmark/longmemeval-baseline-n200-r{1,2,3}.json`,
  `benchmark/longmemeval-claimlevel-n200-r1.json`

## Links

- [[knowledge/notes/citation-relevance-gate-decision]] — the overlap gate this
  keeps, and now applies without collateral.
- [[knowledge/notes/cross-lingual-citation-relevance-decision]]
- [[knowledge/notes/part-scoped-evidence-decision]]
- [[knowledge/notes/the-model-names-the-evidence-we-locate-it-decision]] — links to this page.
