# A name inside a question keeps every leg

Date: 2026-11-17. Scope: `scripts/retrieval.py` (query routing), `scripts/reranker.py` (exact
bypass), `scripts/query_memory.py` and `benchmark/longmemeval_vault.py` (the profile they pass).

## What was wrong

Two defects made ordinary questions search by spelling only.

1. **A word with a hyphen or an underscore made the whole query a lookup.** `analyze_query`
   marks such a word as `exact_identifier`, and that intent mapped to the lexical-only `EXACT`
   profile. "How many shirts did I pack for my 5-day trip?" therefore ran BM25 alone. The
   reranker also skipped every query carrying the intent (`exact_identifier_bypass`). On
   LongMemEval-S (500 questions) 40 questions were routed this way.
2. **Two callers passed the planner's recommendation back as the caller's choice.** The answer
   path (`query_memory.grounded_qa`) and the benchmark asked retrieval with
   `profile=analysis.recommended_profile`. An explicit profile runs exactly as declared, so the
   dense leg that `planned_request` adds when no profile is given was dropped: `EXACT` and
   `TEMPORAL` questions ran lexical only, `GRAPH` and `IMPACT` lost the dense leg. Nine more
   questions (7 `TEMPORAL`, 1 `GRAPH`, 1 `IMPACT`) were affected.

## What changed

- Only a query that is one name and nothing else is a lookup (`identifier_lookup` → `EXACT`):
  `hook-scripts-defense-in-depth`, `auth-decision.md`, `QueueWorker`, `scripts/retrieval.py`. A
  name inside a question stays a ranking hint and the question runs the planned legs.
- The reranker is skipped only for a quoted phrase or an explicit `EXACT`/`DIRECT` profile. A file
  whose name matches the query is still promoted to the top after reranking
  (`_promote_exact_filename`), so a lookup does not need the bypass.
- The answer path and the benchmark ask with no profile unless the caller chose one, so the
  planner adds the dense leg. `planned_profile(query)` reports the profile that actually runs;
  the benchmark records that one.

## Measurement

The 49 affected questions of LongMemEval-S (every question the old router sent to `EXACT`,
`TEMPORAL`, `GRAPH` or `IMPACT`), retrieval only, `run_longmemeval.py --full --retrieval-only`.
Before: commit a32a8354; after: commit c1221ddc, each from a clean `git archive` export with the
same models (multilingual-e5-small, bge-reranker-v2-m3). Bounded to 8 cores and 2 workers on a
shared server.

| Metric (49 questions) | Before | After |
|---|---|---|
| Turn recall at 12 | 0.644 | 0.926 |
| Session recall at 12 | 0.910 | 0.958 |
| All evidence turns in the top 12 | 29 | 41 |
| Turn recall at 24 / 48 | 0.713 / 0.909 | 0.960 / 0.969 |
| Answer session ranked first | 44 | 47 |
| Reranker applied | 5 | 44 |
| Retrieval seconds, median / p90 / max | 0.16 / 1.9 / 4.4 | 2.67 / 4.8 / 14.7 |

Turn recall at 12 changed on 19 questions: 17 better, 2 worse.

- `3fe836c9` (pre-approval amount against the sale price) fell from 1.0 to 0.5 at every depth.
  One of its two evidence turns no longer reaches the top 48. The cross-encoder now ranks it,
  and lexical overlap alone ranked it higher.
- `gpt4_93159ced` fell from 1.0 to 0.67 at 12 and is complete again at 24.

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
