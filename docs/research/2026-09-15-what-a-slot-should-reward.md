# What a slot should reward

Dated 2026-09-15. The ordering defect found by the full stand pass of 2026-09-14, the
research that settled the fix, and the measurements taken before it was written.

## The finding

- `retrieval._page_diverse` gives one visible slot to each repeat unit and sends every
  other chunk of that unit behind all distinct units (`_place_by_page`). For
  `knowledge/daily/**` and `knowledge/raw/**` the unit was the entry heading — one
  captured session (`_repeat_unit`, 2026-09-08, kept by
  `docs/research/2026-09-13-one-argument-one-slot.md`). The same day conversations were
  cut into turns (`corpus_snapshot._round_spans`,
  `docs/research/2026-09-08-small-keys-large-values-and-a-loop-that-stops.md`). The two
  decisions contradict each other: every turn after the first of a session was ranked
  behind the first chunk of every other session.
- The instrument that could see this, per-turn evidence coverage
  (`benchmark/longmemeval_coverage.py`), was added on 2026-09-14.
- The reader receives the first twelve candidates (`longmemeval_vault.QA_CANDIDATES`),
  each with its partner turn and no whole entries by default
  (`query_memory.WHOLE_ENTRIES_DEFAULT = 0`, `_with_entry_siblings`), so a turn the order
  pushes past twelve does not reach the reader through its session either.
- The text is not lost earlier: on four questions every evidence turn was present, whole,
  in the daily file and in the chunks.

## Measured with the product's own ranking

LongMemEval `longmemeval_s`, seed 101, the first 189 answerable questions of the
2026-09-14 retrieval-only run; hybrid retrieval with the reranker; the slot rule swapped
in-process only.

- Of eight questions whose evidence turn missed the first twelve, six had it at score
  ranks 2-11 and at final ranks 56-80.
- All evidence turns in the first rows, 189 questions with recorded candidate orders
  (95 multi-session, 64 single-session-user, 30 single-session-preference):

| rule | all turns @12 | all turns @24 | all sessions @12 |
|---|---|---|---|
| one slot per session (current) | 0.392 | 0.392 | 0.968 |
| at most 2 per session | 0.566 | 0.577 | 0.952 |
| at most 3 per session | 0.608 | 0.651 | 0.942 |
| relevance order for episodes | **0.698** | **0.799** | 0.910 |
| concave per-session reward, best weight (0.1) | 0.698 | 0.794 | 0.915 |

- The concave reward was tried with weights 0.1, 0.25, 0.5 and 1.0 and three relevance
  scales (rank reciprocal, geometric by rank, the product's final score); no setting beat
  relevance order on all turns.
- Relevance order never lost a question the current rule had all turns for (0 of 73), and
  "all sessions" can only fall on questions whose turns were already incomplete, because a
  covered turn names its session.
- Multi-session questions stay the weakest: 0.263 → 0.568 at 12, 0.726 at 24.

## Practice on this date

- **Relevance order is optimal for "all of them".** The probability ranking principle
  is optimal under independence ([PRP](https://link.springer.com/rwe/10.1007/978-0-387-39940-9_930)).
  Chen and Karger (SIGIR 2006) show diversifying is the right greedy rule for "at least
  one relevant item" and relevance order the right one for "all of them"; on TREC Robust
  diversifying raised 1-call@10 0.791 → 0.835 and lowered P@10 0.333 → 0.269. A reader
  that must combine every fact needs the second objective.
- **A source cap is the hard limit of a concave reward.** Lin and Bilmes (ACL 2011)
  reward diversity with a square root over groups, monotone submodular, greedy within
  (1 - 1/e) (Nemhauser, Wolsey, Fisher 1978; not improvable unless P = NP, Feige)
  ([paper](https://aclanthology.org/P11-1052/)). Measured here at every weight, it never
  beat relevance order on the objective that matters.
- **Diversity after a good reranker adds little or hurts.** Plain MMR was worse than the
  chunk-focused baseline, -0.020 F1 ([What Survives Into Context, 2607.00725](https://arxiv.org/html/2607.00725));
  a BGE reranker 52.1 EM against MMR 49.6 and DPP 50.2 ([GeoRAG, 2606.29328](https://arxiv.org/html/2606.29328));
  Dartboard lowered all-evidence@5 slightly ([PACE, 2608.25115](https://arxiv.org/html/2608.25115)).
  Duplicates add nothing, independent documents add correctness
  ([2608.13956](https://arxiv.org/html/2608.13956)).
- **Memory systems order turns by relevance.** The LongMemEval reference code ranks
  turns by score with no session cap; the round is the best value unit, up to +6 % QA
  ([LongMemEval](https://arxiv.org/pdf/2410.10813)). In dialogue memory, evidence is spread
  across neighbouring turns and pruning it removes answers ([xMemory, 2602.02007](https://arxiv.org/html/2602.02007v1)).
  No 2025-2026 paper found measures a per-session cap for turn-level chat memory.
- **Search engines cap sites for a person's list, and lift the cap on relevance.** Google
  shows at most two results per site "except where our systems determine it's especially
  relevant" ([2019 update](https://noblestudios.com/seo/google-site-diversity-change/)); Yandex
  shows several documents of one source when the ranking formula says so
  ([Yandex, 2012](https://webmaster.yandex.ru/blog/13030)). Answer engines work at passage
  level: Perplexity scores sub-document units with lexical, embedding and cross-encoder
  stages ([Vespa case study](https://vespa.ai/case-studies/perplexity/)); Baidu extracts the
  query-weighted sentences of a document before ranking
  ([KDD 2021](https://arxiv.org/abs/2105.11108)) and decomposes questions in its AI search
  ([2506.17188](https://arxiv.org/abs/2506.17188)).
- **Crowding, where kept, demotes rather than drops**
  ([Vertex AI Search CrowdingSpec](https://docs.cloud.google.com/generative-ai-app-builder/docs/reference/rest/v1/CrowdingSpec)).

## The decision

- Chunks of `knowledge/daily/**` and `knowledge/raw/**` keep their relevance order: an
  episode no longer claims a single slot. Every other page keeps one slot for its first
  chunk, its other chunks following the distinct pages, as decided on 2026-09-13.
- Nothing is dropped, and nothing changes for compiled notes, so the selective-forgetting
  and parity stands, which rank notes and code, are expected unchanged; both are re-run.
- Not in this change, each to be measured on its own: reranking thirty candidates instead
  of ten, and fanning a multi-session question out into sub-queries.

Files: `scripts/retrieval.py`, `tests/test_retrieval.py`,
`tests/test_every_turn_keeps_its_rank.py`,
`docs/research/2026-09-15-what-a-slot-should-reward.md`.
