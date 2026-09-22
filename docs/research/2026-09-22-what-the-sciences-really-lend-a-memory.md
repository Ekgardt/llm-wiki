# What the sciences really lend a memory

Dated 2026-09-22. The owner asked for research across biology, physics, mathematics and
chemistry for a solution that would leave the competitors behind. Six reports were written
today, each with its sources opened on the day and tagged peer-reviewed / preprint / vendor:
`RESEARCH-A..F` under the job scratch directory (multi-hop, reader, cost, biology,
physics-mathematics, chemistry-and-cross-field). This note is the verdict and what it changes
in the plan of `docs/research/2026-09-22-a-class-level-plan-to-pass-the-field.md`.

## The verdict, stated plainly

There is no single result in any of these fields that a memory system can lift and thereby
jump the field. The biology report's own finding: a 2026 system with six biological mechanisms
matched raw retrieval (70.1 vs 71.2, overlapping intervals); TiMem, bio-motivated, scores
76.88 on LongMemEval-S, below our 0.849; no LongMemEval leader got a measured edge from
biology. The chemistry report finds chemistry proper gives metaphors only (catalysis,
reaction networks, spectral deconvolution, phase retrieval — whose convergence is proven only
for convex sets, and this problem is not convex). Physics and mathematics mostly *rename* what
everyone does: SPRT at three rounds is "stop when nothing new", LSH on 4 846 × 384 vectors
accelerates a search that already takes under a millisecond, Hopfield capacity bounds concern
weights, not an index.

What the six reports do give is **convergence**: four independent fields point at the same
four mechanisms, each with no model call and no token cost, each aimed at a measured loss.
That convergence is the strongest argument this repository has ever had for what to build.

## The four mechanisms four fields agree on

1. **Cover the question's parts, then fill.** Mathematics: greedy maximum coverage over the
   question's aspects (terms, fact keys, dates, sub-questions) with the 1−1/e guarantee
   (Nemhauser–Wolsey–Fisher 1978; JPR, EMNLP 2021). Biology: hippocampal index-driven
   completion — a found page pulls its own source episodes into the window (Teyler & Rudy
   2007; Rolls 2013). Retrieval research: sessions first, then turns (EmergenceMem). All three
   target the same measured failure: one needed session is found in 0.953 of questions, all
   needed sessions in 0.910; on failures 6 of 12 slots go to two sessions.
2. **A ledger of things and events, posted once.** Accounting's double entry and the CDC's
   case-deduplication rule (chemistry report), recurrence-gated consolidation — create an
   entity page only on its second session, never rewrite pages nightly by model (biology:
   Sun 2023; Zhang et al. 2026 shows model-rewritten consolidation falls below no-memory), and
   the multi-hop report's finding that counting fails on an incomplete set, not on
   arithmetic (GlobalQA; EC-Bench ρ=0.692). Counting is then done by code over every record,
   with a reconciliation step that flags a mismatch instead of answering. Honest limit from
   accounting: a trial balance "is a partial check" — a ledger cannot count what capture never
   wrote. This is plan item 7 and an architecture change: it waits for the owner's yes.
3. **Refusal set as a two-sided decision, calibrated on twins.** Physics: Neyman–Pearson —
   fix the false-answer rate, maximise coverage, report d′; if d′ < 1 no threshold saves the
   signal (Neyman–Pearson 1933; Chow 1970; Geifman & El-Yaniv 2017). Forensics: ENFSI
   evaluative reporting — weigh support for the answer and for "not in the history", decide by
   the ratio. Cognitive science: familiarity and recall are two signals (Norman & O'Reilly
   2003; Koriat 1993), which is the reader report's "sufficiency ≠ confidence". Target: 26
   refusals with the evidence in hand, 16 of them from our own checks.
4. **A bounded graph walk along links recorded at encoding time.** Personalized PageRank with
   node specificity over the existing evidence graph (HippoRAG, NeurIPS 2024: 2Wiki R@5 68.2 →
   89.1; HotpotQA −1.6, so the lane weight stays learned); biology's cognitive map. On 4 846
   nodes this is milliseconds. Target: LoCoMo multi-hop 0.357. Precondition, from both
   reports: first count, in the recorded runs, how many missing sessions are linked to a
   found one by any pointer — if few are, the walk has nothing to walk.

Two more, smaller and measurable: the reader window's capacity curve (6/9/12/16 chunks ×
position, "gold at the edges"; Lost in the Middle, TACL) instead of the constant 12; and a
Daylight-style screen — a first stage with no false negatives — as the principled rule for
skipping the 1.1 s reranker when the first stage is already sure (chemoinformatics; only
latency).

## What changes in the plan

- Item 1 (sessions first) is confirmed by three fields and gains a precise objective: coverage
  of the question's aspects with an explicit stop at zero gain.
- Item 3 (recalibrate our checks) gains its method: Neyman–Pearson on twin questions, d′
  reported, α fixed before the run.
- Item 6 (second retrieval for multi-hop) is preceded by the graph walk, which is cheaper and
  measured; the second retrieval stays for questions the walk cannot reach.
- Item 7 (ledger) gains two rules — post once under an idempotent key, reconcile by code — and
  a warning: pages are extended, never rewritten by a model each night.
- Nothing else in the plan changes; the decision rule stands as written on 2026-09-22.

## Sources

The six reports name about 200 sources, each opened on 2026-09-22 and tagged. The load-bearing
peer-reviewed ones: Nemhauser, Wolsey & Fisher 1978; Neyman & Pearson 1933; Chow 1970; Geifman
& El-Yaniv, NeurIPS 2017; HippoRAG, NeurIPS 2024; Lost in the Middle, TACL 2024; Teyler & Rudy
2007; Rolls 2013; Norman & O'Reilly 2003; Sun et al. 2023; LongMemEval, ICLR 2025.
