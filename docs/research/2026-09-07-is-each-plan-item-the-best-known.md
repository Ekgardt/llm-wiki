# Is each plan item the best known solution? — 2026-09-07

Every item of `docs/PLAN-to-beat-them-2026-09-07.md` held against what was
published up to 2026-09-07. "Best known" here means: nothing published does
the same job better at the same or lower cost. Where something does, the plan
changes; where the claim cannot be verified, it says so.

## The field on 2026-09-07

- OMEGA: 95.4% (466/500) on LongMemEval, self-reported, local-first, CPU only —
  bge-small-en-v1.5, sqlite-vec + FTS5, type weighting, cross-encoder rerank,
  dedupe, time decay; retrieval under 50 ms. https://omegamax.co/benchmarks
- Mastra Observational Memory: 94.87% with gpt-5-mini, **no retrieval at all** —
  dated observations kept in context, ~30k tokens per question; multi-session
  87.2% "an apparent ceiling matching competing systems".
  https://mastra.ai/research/observational-memory
- Mem0 94.4% (6.7–7.0k tokens, ≤1.1 s), ByteRover 92.8% (S), Zep 71.2%; the
  post itself warns "small protocol differences compound into large score
  differences". https://mem0.ai/blog/ai-memory-benchmarks-in-2026
- LongMemEval-V2 (arXiv:2605.12493): 451 questions, web/enterprise agent
  trajectories, scored on accuracy **and latency** over a baseline frontier.
  https://github.com/xiaowu0162/LongMemEval-V2/
- Iterative RAG beats gold evidence (80.9% vs 69.1%); **87.3% of errors occur
  with the correct evidence already retrieved**. https://arxiv.org/html/2601.19827v4
- "Assembly, not storage": temporal knowledge graphs underperform simple
  pipelines on conflict resolution; Zep/Graphiti 7% on FC-SH, Mem0 18%,
  BM25 48%. https://arxiv.org/html/2606.01435v1

Two of these confirm our own measurement (gold in prompt for 57% of right and
58% of wrong answers): the discriminator is what the system does with the
evidence, not whether it found it.

## Item 1 — measure the figures/path gate fix

Not a design choice; nothing to compare. The protocol (3 × 200, seed 101,
accept only gain > 0.035) is stricter than every self-report above, none of
which states a spread. Keep.

## Item 2 — sufficiency after a declared count/sum with zero slack

Published best: iterative retrieval with a stopping rule — coverage ≥ 80% and
sufficiency ≥ 60%, at most 5 steps (arXiv:2601.19827); calibrated retrieval
budget tiers k = 0/1/5 from the reader's own confidence, ECE 0.275 → 0.062
(https://arxiv.org/html/2606.29959); TechRAG rule → LLM review → agentic retry
(https://arxiv.org/pdf/2606.01613); FAIR-RAG evidence gaps → refined query.

Ours is a narrow instance of the same mechanism: the trigger is the declared
derivation plus zero slack, which fires on 11 of 198 questions, so 94% of
questions pay nothing. No published method is cheaper per question; the
published ones are broader (they fire on every question). **Keep**, with one
addition from the literature: bound it to one extra pass, as every stopping
rule above bounds steps.

Alternative considered: S-RAG — structured corpus at ingestion, formal queries
at inference, "substantially outperforms" RAG on aggregative questions
(https://arxiv.org/abs/2511.08505). Needs a schema known at ingestion; our
corpus is open-domain and multilingual. Not for the general path; worth
remembering for closed schemas such as project state.

## Item 3 — entity clustering before counting

Published best: in-context clustering — LSH/embedding blocking, then the LLM
clusters a **set of ≤ 9 records in one call**, records of one entity adjacent,
about four entities per set; up to 150% higher accuracy than pairwise
matching, 5× fewer calls, 22× cheaper (https://arxiv.org/html/2506.02509v1,
SIGMOD 2025). Neo4j's agent-memory guidance: resolve by embedding, then pin
aliases to a canonical entity so later mentions resolve without search
(https://neo4j.com/labs/agent-memory/explanation/resolution-deduplication/).

The plan said "cluster by embedding". The literature says embedding is the
blocking step and the LLM's grouping call is what buys the accuracy. **Change
the item**: embedding to block, one in-context clustering call over the
candidates of a declared count (always a small set), aliases pinned. Cost:
one call, only when a count was declared.

## Items 4 and 5 — knowledge update, and the end of a fact's validity

These are one item. Findings:

- Deterministic `max(timestamp)` after LLM candidate extraction ties LLM
  judgement on LongMemEval knowledge-update (57.8% vs 64.4%, n = 45) and
  **loses on non-freshness questions**; it must be composed with
  question-type-aware handling (arXiv:2606.01435).
- MemConflict: explicit supersession beats passive recency; recency alone is
  mixed (https://arxiv.org/pdf/2605.20926).
- MemStrata: write-time supersession by deterministic (subject, relation) key —
  old row gets `valid_to` and `superseded_by`, no LLM; stale-fact rate 15–40%
  → ~0%; **97% on clean templates, 44% on messy natural language**
  (https://arxiv.org/html/2606.26511).
- Supersede: the gap is memory maintenance, not comprehension — 92% full
  context → 77% bounded memory; 24× more memory recovers nothing
  (https://arxiv.org/html/2606.27472v1).
- Reliability-weighted Bayesian update 100 vs last-writer-wins 67
  (arXiv:2606.22030, earlier note).

Conclusion: best known = **explicit supersession at write time** (close the old
fact's validity, link the new one) + **reliability weighting at read time**
when two facts are both open. Recency alone is not it. The graph shows the
shape already exists twice here: pages carry `status: superseded` /
`superseded_by` (rule 12), and the claim ledger sets `lifecycle: superseded`
in `contradiction_pipeline._supersede_ledger_claims`. What is missing is the
**end of validity as a date** on the old claim and a reliability weight
between two claims that are both still open. **Merge 4 and 5 into one item**
with that shape. The 44%-on-messy-text number is the risk: supersession must
be conservative and leave uncertain pairs both open for the weighting to
decide.

Zep's bi-temporal graph is the richest published storage and scores lowest
on conflict resolution — the storage is not the win, the write-time closing is.

## Item 6 — binary quantisation

Published best: RaBitQ (theoretical error bound; the basis of Elastic BBQ and
LanceDB's quantisation) beats plain binary + rescoring at equal bits
(https://dl.acm.org/doi/abs/10.1145/3654970,
https://www.elastic.co/search-labs/blog/better-binary-quantization-lucene-elasticsearch);
QuIVer beats RaBitQ but needs a graph index (https://arxiv.org/pdf/2605.02171);
binary + int8 rescoring keeps ~96% at 32× (sbert docs).

But the premise is unverified: nobody has measured where our 2.5 s warm search
goes. The cold-start note found the 23 s was model loading; the warm cost may
be the cross-encoder (`retrieval._run_reranker`, batches of two pairs), not
vector distance — OMEGA runs the same kind of pipeline in under 50 ms.
**Replace the item** with: profile the warm path first; quantise only if the
vector stage is where the time is.

## Item 7 (new) — the five English `_INTENT_PATTERNS`

`retrieval._INTENT_PATTERNS` (lines 578–584) maps five English regexes to
profiles. Published: cascades regex → intent-prototype embeddings → LLM only
on miss (DoorDash, https://arxiv.org/pdf/2603.01486); RAGRouter-Bench's best
router is TF-IDF + SVM, monolingual (https://arxiv.org/html/2604.03455v1);
PRISM structures memory by LLM-extracted intent (https://arxiv.org/pdf/2605.12260).

Non-keyword best: prototype embeddings with a multilingual embedder, which we
already run. But whether profile choice moves accuracy at all is unmeasured.
**Measure first**: one run with every question forced to HYBRID; if the
difference is inside the spread, delete the patterns and the profiles; if
outside, replace regexes with prototype similarity.

## Refusal — unchanged, and now with a reason

Prompt-based abstention fails under fluent misinformation (answer rate 13.6–
74.3% when it should be ~0); NLI verification of each passage gives the best
safety/coverage trade-off; claim-level, context-only verification is the
frontier direction (RT4CHART https://arxiv.org/html/2603.27752v1, SURE-RAG
https://arxiv.org/html/2605.03534, https://arxiv.org/html/2608.22228). Our
gates are exactly claim-level with a citation span per claim. Keep.

## What "best in the world" can and cannot mean here

Facts: each item above matches the top published mechanism for its job or is
a strictly cheaper instance of it, and nothing found does the job better at
lower cost. Facts: three systems report 94–95% on LongMemEval; we answer
81.3% of what we answer, and none of them publishes a spread, a refusal rate
or an independent judge. Not known: whether the combined plan reaches them —
the multi-session ceiling (~87%) binds everyone, and items 1–3 are unmeasured.
No source can prove "best in the world"; only the runs can, and the runs are
item 1.

## Addendum, same evening — what was built, and one proxy that must be said

Items 2, 3 and 4 are implemented (`scripts/aggregation_pass.py`, the second
look in `query_memory.grounded_qa`, `reliability` in
`bitemporal_claims._successor`). Two things the implementation had to decide
that the research above did not:

- **The runtime signal for "zero slack".** The measured signal used the
  dataset's own labels (answer sessions retrieved minus answer sessions
  labelled), which no live question has. The runtime proxy is: an aggregating
  claim cites the lowest-ranked page the model was still shown — the useful
  evidence reached the edge of retrieval. It is an inference, and it is
  unmeasured until the runs; the benchmark row now carries `provider_calls`,
  `answer_calls` and `cluster_calls`, and `est_total_prompt_tokens` is
  cumulative, so the runs will price it.
- **Clustering set order.** The SIGMOD study's "records of one entity
  adjacent" is approximated by a case-folded sort before cutting into nines.
  Cheap, and wrong for names that differ at the first letter; a duplicate the
  sort separates across two nines is simply not found, which loses nothing
  the system had before.

Items 5 (profile the warm path) and 6 (forced-HYBRID run against the intent
patterns) are measurements, not code, and wait for the measurement phase the
owner set.
