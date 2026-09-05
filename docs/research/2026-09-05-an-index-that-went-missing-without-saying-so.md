# An index that went missing without saying so

Dated 2026-09-05. Written because an outside reviewer found something we did not,
and the finding checks out.

## What was found

The reviewer said the search had "degraded to plain text" and that the layer
which would serve many agents was empty. Both check out on this vault today.

- `cache/evidence-graph/catalog.sqlite3` → `catalog_state.active_generation_id`
  is **NULL**. The last activation in `activation_history` is dated
  **2026-08-30**.
- A live query returns rows carrying `fallback_reason: "generation_unavailable"`,
  `generation: "legacy"`, `requested_mode: "HYBRID"`, `effective_mode: "BASE"`,
  and `signals_used: ["lexical", "reranker"]`. `vector_rank` and `graph_rank` are
  null on every row.
- The newest generation **on disk** is complete: 3699 chunks, 3699 vectors,
  `vector_state: "complete"`. So the reviewer's "vectors cover less than half the
  pages" is the wrong number for the right conclusion — the vectors exist and are
  whole, and none of them is reachable, because nothing is active.

So for five days every answer this vault gave came from a lexical index plus a
reranker, and nothing anywhere said so.

## The part that is our fault, and it is not the missing generation

Losing an index is ordinary. **Not noticing for five days is the defect.** The
fallback did exactly what it was written to do — it degraded rather than failing
— and it recorded the reason honestly, per row, in a field nobody reads.

The field literature is blunt about this shape. The recurring finding is that
staleness is silent by construction: semantic similarity has no temporal
dimension, so a stale embedding scores exactly as well as a fresh one, and
standard evaluation suites measure faithfulness and relevance against fixed
ground truth with no temporal component at all. A system can pass every metric
it has while serving weeks-old content. Teams are described as discovering the
rot "only after a user complaint", which is precisely how we discovered ours.

The recommended shape is four layers of signal rather than one: pipeline health
(did the build finish, did the write land), index freshness distribution (how old
is what we serve), retrieval quality probes (a set of golden queries with
known-good answers, run daily), and user-signal correlation. The thresholds
quoted are for drift rather than absence — 85-95% top-10 overlap is healthy,
below 70% is active quality loss; alert when more than ~10% of vectors exceed
their category TTL.

Our case is simpler and worse than drift: not stale, **absent**. No threshold is
needed to detect zero.

## What follows for us

1. **Rebuild and activate.** Operational, not a design change.
2. **A missing active generation must be a health finding, not a per-row
   footnote.** `doctor` already knows how to build one; what it apparently did
   not do is shout that there was none. That is the fix worth making, and it is
   checkable: a boolean, not a threshold.
3. **A golden-query probe** is the cheapest version of layer three the sources
   name, and we already have the material for it — the benchmark's own questions
   with known answers. It would have caught this on 31 August.

What we should not do is remove the fallback. Answering from a lexical index is
better than not answering; the sources' own recommendation is a degraded mode
that *warns*, not one that dies. The defect is the missing warning.

## Sources

- https://tianpan.co/blog/2026-04-10-rag-freshness-problem-stale-embeddings-silent-failure
  — stale embeddings as silent failure; the four monitoring layers, the golden-query
  probe, the drift and TTL thresholds quoted above
- https://tianpan.co/blog/2026-04-20-rag-knowledge-base-freshness-index-rot —
  index rot as the problem teams address last
- https://arxiv.org/pdf/2601.05264 — Engineering the RAG Stack: architecture and
  trust frameworks
- https://qaskills.sh/blog/rag-testing-index-freshness-staleness — testing that
  superseded content stops influencing answers
