---
type: decision
title: "One Trust Weight Across Retrieval Paths"
description: "Typed provenance multiplies the score that decides the order on every retrieval path, from one shared table, and the weight is reported."
date: 2026-08-19
confidence: high
source_authority: user
status: active
---
# One Trust Weight Across Retrieval Paths

One-sentence summary: Typed provenance multiplies the score that decides the order on every retrieval path, from one shared table, and the weight is reported.

## Decision

Date: 2026-08-19.

`scripts/provenance.py` holds the single weight table — `user` and `human` 1.35,
`web` 1.1, `ai-derived` and `ai` 1.0, `inferred` 0.8, anything unknown or absent
1.0. `search_memory.py` and `retrieval.py` both import it.

The weight multiplies whichever score decides the order:

- lexical BM25 paths, as before;
- the fused path, where `final_score = rrf_score * weight` while `rrf_score`
  stays the pure rank-only fusion;
- the reranked path, where the weight multiplies the blended
  cross-encoder/RRF score, including the untouched tail beyond rerank depth.

Every candidate carries `authority_weight`, so a ranking that put a stated fact
above a better lexical match can be explained rather than guessed at.

## Why

CLAUDE.md rule 13 makes provenance a ranking input: user-stated outranks
web-sourced outranks ai-derived outranks inferred. The table existed but reached
only the BM25 paths. The hybrid path merged lists by rank alone, so a guess and
a stated fact with the same rank were interchangeable, and the reranker never
saw provenance at all. The README promised typed-provenance ranking on all of
it.

Rank-only RRF exists to avoid mixing incompatible score scales between
backends, and that stays: no backend magnitude enters the formula. A document
prior is a different thing from a backend score, and applying one per document
after fusion is an established pattern — Elasticsearch ships per-retriever
weighted RRF, and time-weighted RRF applies a per-item recency prior the same
way. The weights are unchanged from the ones already in use, so this is one
contract applied everywhere rather than a new tuning exercise.

## Consequences

- On the frozen retrieval-v2 corpus every metric and every gate is unchanged,
  measured before and after: the corpus does not separate provenance classes,
  so the change is neutral there and is proved by unit tests instead.
- A candidate with no provenance is unweighted, not penalised.
- Weights are not tuned against a labelled corpus. If tuning is wanted later it
  needs one, and the place to change is the single table.
- Two candidates whose weights differ can now reorder relative to their pure
  RRF order. `rrf_score` remains visible next to `final_score`, so the trace
  still shows the fusion result on its own.

## Source / Evidence

- Explicit operator approval, 2026-08-19; audit item OPEN-036.
- Table and resolver: `scripts/provenance.py`.
- Fusion: `scripts/retrieval.py::fuse_rrf`; rerank: `scripts/reranker.py::rerank`.
- Before/after measurement: `benchmark/run_retrieval_v2.py`, all
  `overall` and `macro_average` metrics identical, gates identical.
- Weighted RRF in Elasticsearch:
  https://www.elastic.co/search-labs/blog/weighted-reciprocal-rank-fusion-rrf
- RRF as rank-only fusion in OpenSearch:
  https://opensearch.org/blog/introducing-reciprocal-rank-fusion-hybrid-search/

## Related

- [[baseline-environment-binding-decision]]
- [[warm-navigation-overhead-threshold-decision]]
- [[citation-relevance-gate-decision]]
- [[cross-lingual-citation-relevance-decision]]
- [[classification-measurement-stand-decision]]
