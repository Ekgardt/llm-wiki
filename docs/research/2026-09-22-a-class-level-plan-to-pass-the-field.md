# A class-level plan to pass the field

Dated 2026-09-22. The research behind the owner's request to overtake competitors on every
measured metric **without fitting a benchmark**. Three research reports underlie it, each with
its sources opened on 2026-09-22 and tagged peer-reviewed / preprint / vendor:
`RESEARCH-A-multihop-2026-09-22.md`, `RESEARCH-B-reader-2026-09-22.md`,
`RESEARCH-C-cost-2026-09-22.md` (job scratch, not tracked).

## What the evidence changed in our picture

- The multi-session ceiling of the leaders is about 0.87, not 0.93: Mastra 87.2%, Hindsight
  87.2%, OMEGA 83% ("still the weakest"). The 0.93 figures are overall scores. Supermemory's
  96% (GPT-4o) falls to 75–77% with GPT-5 and Gemini-3-Pro in its own table.
- Counting fails on an incomplete set, not on arithmetic: answering "how many in total" from the
  best k chunks has a hard ceiling (GlobalQA/GlobalRAG, preprints; EC-Bench, ρ=0.692 between
  enumeration F1 and counting accuracy).
- LongMemEval (ICLR 2025): "For the multi-session reasoning questions, further representing the
  values with the extracted facts improves the QA accuracy"; for other types facts-as-values hurt.
- Most of our refusals with evidence in hand come from our own checks (13 date-scope, 3
  citation), not the model. Confidence in an answer "is nearly blind to whether the question is
  answerable" (Two Axes, 2026): sufficiency and confidence are two signals.
- The reading strategy alone moves LongMemEval by up to 10 points with perfect retrieval
  (LongMemEval paper); quoting the evidence before answering helps across benchmarks (Findings
  EMNLP 2025; ACL 2024).
- Vendor latency figures are mostly not comparable (OMEGA: ~240 records on an M1; Zep: one
  LoCoMo search in their cloud; Zep 2026 itself reports 4 408 tokens per question).
  Of our 3.6 s, the query without the reranker already costs 2.5 s — the models are not the
  whole cost.
- Two 0.821 scores are within noise (46/56 has a 95% interval of 0.70–0.90).

## The rule that keeps this class-level (stated before any run)

1. A trigger reads the question text or the store, never a benchmark label (`question_type`,
   `_abs`, a question id); a repository test fails if the answer path reads them.
2. Every threshold is set on one split and frozen: LongMemEval by question-id hash parity (tune
   / decide), LoCoMo conversations 1–5 tune, 6–10 decide, plus a question set from the live vault
   written before the change and never used for tuning.
3. A change is adopted only if its target class improves on the decide half of **both** public
   benchmarks (paired bootstrap, lower 95% bound above 0), no other type loses more than 1.5
   points (lower bound of the paired interval), the vault set does not get worse, and both
   refusal errors are reported: answerable-refused and unanswerable-answered.
4. The baseline is run twice to know the noise.

## The work, ranked

1. **Sessions first, then turns.** Score a session by its best turns and fill the window by
   sessions (EmergenceMem, Nano-Memory pattern). Replaces the per-session quota merged on
   2026-09-19 if it measures better. No model call, no token.
2. **Quote, then answer.** The reader writes the supporting quotes first; the citation check
   becomes a string match. Replaces a model-judged check that is wrong about one time in five
   (AttributionBench ~80% F1).
3. **Recalibrate our own checks** — the date window first — with Learn-then-Test on the tune
   split, at a risk level fixed before the run. Split "evidence is enough" from "answer
   confidence".
4. **Profile the 3.6 s**, then skip or shorten reranking when the first stage is already sure.
5. **Adaptive evidence size** with a floor, and a trimmed, ordered prompt; publish memory
   context and fixed prompt separately so token figures compare.
6. **Gap-driven second retrieval, only for multi-hop questions** (IRCoT, ACL 2023; EviMem):
   1–2 extra calls on those questions only. Model-driven decomposition of every question is
   rejected — it costs 14.2% F1 on open-domain (Cognis).
7. **A ledger of things and events at compile time** — kind, canonical thing, date, quantity,
   pointer to the source bytes — so "how many" is counted by code over every record, not by the
   model over twelve chunks. The only mechanism that removes the top-k ceiling. It is an
   architecture change: it needs the owner's explicit yes, a decision page and
   `docs/STRUCTURE.md`.
8. **Twin questions**: each answerable LongMemEval question gets a copy with its evidence
   sessions removed, where refusing is right — both directions measured on the same question
   without new labels.

Rejected: a separate graph store, the whole history in every prompt (~30k tokens), prompts per
benchmark category, sampling several answers on every question (5–10× cost, does not fix
refusal), XProvence (CC BY-NC-ND: the repository is public and the licence forbids derivative
use), and chasing Supermemory's 720 (a retrieval metric, not an answer metric).

## What we can and cannot beat, honestly

Accuracy on LongMemEval and LoCoMo: reachable by items 1–3, 6–7 on the measured evidence.
Tokens: Mem0 (~6 800) and probably Zep 2026 (4 408) — tokens do not depend on hardware. Latency
with a cross-encoder on 4 CPU cores: OMEGA and Zep cannot be beaten; the first stage alone may
come close once profiled.

Files: `scripts/retrieval.py`, `scripts/query_memory.py`, `scripts/aggregation_pass.py`,
`scripts/temporal_anchor.py`, `benchmark/longmemeval_vault.py`, `benchmark/longmemeval_score.py`,
`docs/research/2026-09-22-a-class-level-plan-to-pass-the-field.md`.

## Sources

- LongMemEval (ICLR 2025), arXiv 2410.10813 — peer-reviewed.
- IRCoT, ACL 2023 — peer-reviewed. HippoRAG 2, ICML 2025 — peer-reviewed.
- Two Axes of abstention (2026), GlobalQA/GlobalRAG, EC-Bench, EviMem, Cognis — preprints.
- Mastra, Hindsight, OMEGA, Supermemory, Zep, Mem0 benchmark pages — vendor.
- Full list with links: the three research reports named above.
