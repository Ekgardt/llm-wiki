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

---

## The second cause, found after the first was fixed

The byte-boundary cut was real and is fixed, but it dates from 2026-09-02 and the
last successful build was 2026-08-30. Something else was already wrong.

`corpus_snapshot._capture` reads every source, then re-discovers and re-hashes
every source, and raises `CorpusChanged` if anything moved. Measured today: the
collection itself is quick and reliable — six consecutive runs, 3.5 to 3.6
seconds each, no failures — but under `doctor` it fails, because a session
writing to the vault appends to today's daily log while the build runs, and a
capture hook fires on every tool call.

Nothing about the fence changed. **The vault outgrew it.** 1047 sources now, 228
daily logs, and one or more agents writing continuously; the window between the
first read and the verification pass is no longer small compared to the interval
between writes. Since 2026-08-30 that window has apparently always caught
something.

### What the field does

Snapshot isolation, as every MVCC engine implements it — PostgreSQL, InnoDB,
WiredTiger, Oracle, SQL Server and the rest — guarantees that a transaction sees
a consistent view **from the point it started**. It does not require the database
to hold still until the transaction commits; that would make concurrency
impossible, which is the whole reason MVCC exists. Lucene's `IndexWriter` takes
the same position from the other end: commits are explicit point-in-time
snapshots, and an index reader refreshes to a newer commit rather than the writer
refusing to commit because the corpus moved.

Our fence asks for something stronger than either: that the world stand still for
the duration of the build.

### What we will do, and what we will not

**Not** weaken the fence. Every captured record already carries the hash of the
bytes that were actually read, so a snapshot is internally consistent by
construction; the second pass is what proves the snapshot describes a moment the
vault really passed through, and that is worth keeping.

**Retry instead of surrender.** A pass costs 3.6 seconds. When a pass loses the
race, take another one, bounded; fail only if the vault never holds still long
enough for a single pass. The invariant is unchanged — a published generation
still matches a real instant — and the build stops being impossible on a vault
that is in use.

### Sources

- https://notes.eatonphil.com/2024-05-16-mvcc.html — snapshot isolation as a
  consistent view from transaction start, not a frozen world
- https://www.vldb.org/pvldb/vol16/p1426-alhomssi.pdf — snapshot isolation for
  high-performance storage engines
- https://lucene.apache.org/core/2_9_4/api/all/org/apache/lucene/index/IndexWriter.html
  — explicit point-in-time commits, readers refresh to a newer one
- https://datalakehousehub.com/blog/2026-04-29-query-engine-optimization-10-concurrency-control/
  — MVCC and contention in current query engines
