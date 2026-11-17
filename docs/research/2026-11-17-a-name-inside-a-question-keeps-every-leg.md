# A name inside a question keeps every leg

Date: 2026-11-17. Scope: `scripts/retrieval.py` (query routing), `scripts/reranker.py` (exact
bypass), `scripts/query_memory.py` and `benchmark/longmemeval_vault.py` (the profile they pass).

## What was wrong

Two defects made ordinary questions search by spelling only.

1. **A word with a hyphen or an underscore made the whole query a lookup.** `analyze_query`
   marks such a word as `exact_identifier`, and that intent mapped to the lexical-only `EXACT`
   profile. The search tools, which pass no profile, still ran lexical and dense search for
   "How many shirts did I pack for my 5-day trip?", because `planned_request` turns a non-graph
   recommendation into `HYBRID`; but the reranker skipped every query carrying the intent
   (`exact_match_bypass`). The answer path and the benchmark ran it BM25 alone (defect 2). On
   LongMemEval-S (500 questions) 40 questions were routed this way.
2. **Two callers passed the planner's recommendation back as the caller's choice.** The answer
   path (`query_memory.grounded_qa`) and the benchmark asked retrieval with
   `profile=analysis.recommended_profile`. An explicit profile runs exactly as declared, so the
   dense leg that `planned_request` adds when no profile is given was dropped: `EXACT` and
   `TEMPORAL` questions ran lexical only, `GRAPH` and `IMPACT` lost the dense leg. Nine more
   questions (7 `TEMPORAL`, 1 `GRAPH`, 1 `IMPACT`) were affected.

## What changed

- Only a lookup is a lookup (`identifier_lookup`): a query made of names and their decoration
  and no word of a sentence. `hook-scripts-defense-in-depth`, `auth-decision.md`,
  `QueueWorker`, `` `QueueWorker` ``, `QueueWorker.run`, `search_memory_rebuild()` and
  `queue_worker.py:120` are lookups. A name inside a question stays a ranking hint, and the
  question runs the planned legs and the cross-encoder.
- A lookup keeps what it had before this change: the planned lexical and dense legs, and no
  cross-encoder (`exact_match_bypass`, now keyed on `identifier_lookup` instead of
  `exact_identifier`). Its result order and its latency do not change. A quoted phrase and an
  explicit `EXACT`/`DIRECT` profile skip the cross-encoder as before.
- The answer path and the benchmark ask with no profile unless the caller chose one, so the
  planner adds the dense leg. `planned_profile(query)` reports the profile that actually runs;
  the benchmark, the answer path and the MCP trace record that one.

## Alternatives considered

1. **Rerank lookups too** (the first version of this change). A cross-encoder scores a passage
   against a sentence; a bare name gives it nothing to read but the name, and
   `_promote_exact_filename` protects only a file whose stem is the query, not a class or a
   function named inside a file. Vaquill reranks identifier queries, but only after a hard
   filter on the parsed identifier has already fixed the candidate set (Vaquill, in Sources), which this
   vault does not have. No measurement of lookups exists here, so the change would have moved
   their order and their latency (0.16 s to about 2.5 s at the median) unmeasured. Rejected.
2. **Route lookups lexical only.** Short navigational queries do favour BM25 (BigDataBoutique, in Sources), but
   fusion beats choosing one retriever per query: on financial retrieval, three adaptive routers
   failed to beat a fixed sparse-dense blend, and fusing both improved Hit@10 by about 28 % over
   either alone (arXiv 2608.00183, in Sources). The base already ran lookups through both legs; taking the dense leg
   away would be a new, unmeasured restriction. Rejected.
3. **Symbol-aware promotion** (put a chunk defining the named class or function first after
   reranking). It would make reranking lookups safe, but it needs symbol extraction for every
   language the vault holds; that is the code-navigation work, not a routing fix. Deferred.
4. **Chosen: a name inside a question is a hint; a bare name keeps the base behaviour.** The
   measured defect (49 questions losing the dense leg or the cross-encoder) is fixed, and the
   unmeasured case is left as it was. Revisit option 1 when a lookup benchmark exists.

## Measurement

The 49 affected questions of LongMemEval-S (every question the old router sent to `EXACT`,
`TEMPORAL`, `GRAPH` or `IMPACT`), retrieval only, `run_longmemeval.py --full --retrieval-only`.
Before: commit a32a8354; after: commit 072737a9 (the routing change plus the displacement fix
below), each from a clean `git archive` export with the same models (multilingual-e5-small,
bge-reranker-v2-m3). Bounded to 8 cores and 2 workers on a shared server.

| Metric (49 questions) | Before | After |
|---|---|---|
| Turn recall at 12 | 0.644 | 0.937 |
| All evidence turns in the top 12 | 29 | 42 |
| Turn recall at 24 / 48 | 0.713 / 0.909 | 0.971 / 0.979 |
| All evidence turns in the top 24 / 48 | 32 / 41 | 45 / 47 |
| Answer session ranked first | 44 | 47 |
| Retrieval seconds, median | 0.16 | 2.31 |

Turn recall at 12 changed on 18 questions: 17 better, 1 worse. At 24, 15 better and none worse;
at 48, 6 better and none worse.

- `3fe836c9` (pre-approval amount against the sale price) had fallen from 1.0 to 0.5 at every
  depth on the routing change alone, and is 1.0 again. Traced through `_pulled_into_window` on
  this question's vault: its evidence turn is its session's first chunk, second in the window.
  The coverage step admitted that session's third chunk, which covers more of the question, and
  the displacement then protected only the admitted chunk and a source's last chunk inside the
  window, so it moved the evidence turn from place 2 to place 49 and `_capped` cut it. The
  counted kind ("more", read from "how much more ... than") only made the step pull in 29
  chunks instead of a few. Fixed in the shared rule, not in the kind: `_displaceable` never
  displaces a source's first chunk inside the window
  (`test_a_source_keeps_its_lane_best_chunk_when_a_later_one_is_admitted`, which fails on the
  old rule). On the 48 questions that read a counted kind, the fix gains turn recall on 2
  questions at every depth and loses it on none, against the commit before it.
- `gpt4_93159ced` ("How long have I been working before I started my current job at
  NovaTech?") falls from 1.0 to 0.67 at 12 and is complete at 24. The answer needs two turns:
  "I've been working at NovaTech for about 4 years and 3 months", first and second after the
  change, and "I've been working professionally for 9 years", which was eighth under the old
  lexical-only route (lexical rank 18) and is fourteenth now. The coverage step does nothing
  here (all three admitted chunks are inside the window). With the reranker scoring 20 instead
  of 10 passages it lands tenth, but the cross-encoder scores it -7.6, far below the turns
  around it: it rises only because the passages it is scored against fall, and every search
  would pay the deeper rerank. Not taken. The turn shares only "working" with the question;
  finding the second fact of a two-fact question is the coverage work of the word-independent
  coverage and routing item, and this question is its check: turn recall 1.0 at 12 there, with
  no question of the 500 losing.

Where the reranker did not run after the change:

- 4 questions: `optional_stage_timeout`, the reranker's deadline, on a server under heavy
  shared load.
- 1 `IMPACT` question: `profile_bypass`. `IMPACT` is not a reranking profile, and that is
  unchanged here.

## Trade-offs

- Latency: the questions that now get the cross-encoder pay for it, about 2.5 s at the median on
  this CPU-bound setup. The reranker's existing deadline still bounds the tail, and when it ends
  the stage the fused order is kept.
- `CODE_SHAPED_INTENTS` still treats a hyphenated English word as code-shaped. This only boosts
  ranking now, and it belongs to the separate review of the word-list router.

## Sources

- LongMemEval (Wu et al., ICLR 2025): the dataset and its evidence-turn labels,
  <https://github.com/xiaowu0162/LongMemEval>.
- Cormack, Clarke, Büttcher, "Reciprocal Rank Fusion outperforms Condorcet and individual rank
  learning methods" (SIGIR 2009). Fusion assumes every leg is present, and a leg dropped by
  routing is not recovered by fusion.
- BGE M3 / bge-reranker-v2-m3 model card, <https://huggingface.co/BAAI/bge-reranker-v2-m3>: the
  cross-encoder stage applied to the fused candidates.
- Anthropic, "Introducing Contextual Retrieval" (2024): hybrid BM25 plus embeddings plus
  reranking beats either leg alone, <https://www.anthropic.com/news/contextual-retrieval>.
- Vaquill, "Hybrid Retrieval for Law, and the Citation-to-a-Subsection Problem" (2026-02-25):
  an exact identifier parsed from the query becomes a filter, and the cross-encoder then orders
  the filtered candidates,
  <https://www.vaquill.ai/engineering/hybrid-retrieval-and-the-exact-identifier-problem>.
- BigDataBoutique, "Hybrid Search Explained" (2026-06-08): "Short navigational queries (one or
  two tokens) usually favor BM25; long natural-language questions favor dense",
  <https://bigdataboutique.com/blog/hybrid-search-explained>.
- "Evidence-Unit Fairness and the Limits of Query-Adaptive Sparse-Dense Fusion in Financial
  Document Retrieval" (arXiv 2608.00183, 2026): no adaptive router reliably beat the fixed
  blend; fusion improved Hit@10 by about 28 % over either component,
  <https://arxiv.org/abs/2608.00183>.
