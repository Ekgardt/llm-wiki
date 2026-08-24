# Ranking by what a page is

Date: 2026-08-24
Reason: after the corpus and diversity fixes the stand holds `hit@5` 0.6 and
`hit@1` 0.0. On every question the first result is
`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` — this register — and the page that
answers sits second to fifth. Before changing how order is decided I checked what
the practice is.

## What was measured here first

For the four questions examined in full, the first result is the register on all
four. Its chunks carry `type: code` (it lives under a code root), no
`source_authority`, and it is Russian, like the questions; the decision pages that
answer carry `type: decision`, `source_authority: user`, and are English by
project rule. The trust table (`scripts/provenance.py`) weighs `source_authority`
only, so the decision page enters ordering at ×1.35 and the register at ×1.0 —
and the register still wins, because it offers 71 chunks of same-language text
against a page's seven.

Two things follow. The signal that is missing is not authority but **what the
page is**: a status log is derived commentary, a decision page is the thing it
comments on. And the correction has to be small enough not to invert a genuine
match.

## What the practice says

**Metadata boosting is the standard lever.** Cloudflare's AI Search shipped
relevance boosting on document metadata in April 2026 — "prioritize recent
documents by boosting on timestamp, or surface high-priority content by boosting
on a custom metadata field". Field- and metadata-aware tuning on top of a fused
base is where the measured gains are (0.7191 → 0.7497 NDCG mean in the
hybrid-search reference for one such tuning).

**Apply it at query time, once per document — never at index time.** Lucene
deprecated index-time boosts (LUCENE-6819) and Solr dropped support in 7.x. Two
reasons matter here: the stored boost was quantised to a single byte and lost
precision, and — the important one — an index-time boost "is applied to all
terms, so matching multiple terms in a boosted field implies a multiplied boost".
The replacement practice is to store the factor and combine it at query time with
a function score. A prior that multiplies the fused score of a candidate exactly
once has neither problem.

**Static weights for every query are the lazy default.** The hybrid-search
reference is explicit that query intent should modulate weighting — code
patterns, identifiers and quoted strings deserve different treatment from
natural-language questions.

## What follows for this repository

The trust weight becomes a product of two factors, both applied once, at query
time, to the score that decides the order — the same place `authority_weight`
already multiplies:

* **authority** — who said it (unchanged): user 1.35, web 1.1, ai-derived 1.0,
  session 0.9, inferred 0.8.
* **type** — what the page is (new): a durable curated page above a derived
  record. `decision` 1.25; `synthesis`, `concept` 1.15; `pattern`, `workflow`,
  `qa` 1.10; `entity`, `debugging`, `skill`, `rule` 1.05; everything else,
  including `code`, 1.0; `gap` 0.8, because a page whose content is "this is not
  written yet" must not answer ahead of one that is.

Nothing is demoted below neutral except a gap stub. That is deliberate: this
vault is a code-intelligence system too, and a question about an identifier must
still reach `scripts/`. The prior lifts curated knowledge rather than pushing
code down, so a code answer that matches well still wins.

Query-intent conditioning is **not** taken in this pass, though the practice
recommends it. The reason is evidence, not effort: the stand has ten cases, all
natural-language questions, so a conditioned weight would be measured on one side
only, and an unmeasured branch is what this register keeps finding. The type
prior is measurable on what exists today; intent conditioning waits for
code-question cases in the stand.

The frozen `retrieval-v2` baseline cannot move: its fourteen synthetic documents
carry neither `type:` nor `source_authority:`, so both factors are 1.0 there —
checked, not assumed.

## What the change actually bought (measured 2026-08-24, after implementing)

The prior does exactly what it says and it is not enough. On "как устроен повтор
после карантина" the decision page receives `1.35 × 1.25 = 1.6883` and moves from
`rrf 0.0077` to `final 0.013` — seventh place to fifth. The first result stays the
status register at `0.0328`. The stand is unchanged: `hit@5` 0.6, `hit@1` 0.0.

The gap is not in the weight, it is in the similarity score: the question is
Russian, the answer page is English, and the register is Russian and discusses
these same decisions. No defensible static prior closes a 2.5× gap — a prior that
did would also promote a wrong page whenever the type happened to match.

The instrument for exactly this is the cross-encoder, which scores the (question,
passage) pair jointly instead of comparing two vectors. On this machine it is not
configured: `reranker_available()` is False and `LLMWIKI_RERANKER_MODEL` is
unset, so `reranker_applied` was false in every measurement quoted here. Turning
it on costs a model download and per-query latency, which is the owner's call to
make, not this pass's.

## Sources

- [AI Search now has hybrid search and relevance boosting, Cloudflare changelog, 2026-04-16](https://developers.cloudflare.com/changelog/post/2026-04-16-hybrid-search-and-relevance-boosting/) — boosting on custom metadata fields as a first-class control.
- [Hybrid Search: BM25, Vector & Reranking Reference 2026, Digital Applied](https://www.digitalapplied.com/blog/hybrid-search-bm25-vector-reranking-reference-2026) — measured gains from field-aware tuning on a fused base; static weights for every query called out as the lazy default.
- [LUCENE-6819 Deprecate index-time boosts?](https://issues.apache.org/jira/browse/LUCENE-6819) — precision loss and the move to doc-values factors combined at query time.
- [Boosting, Elasticsearch in Action §6.3](https://weng.gitbooks.io/elasticsearch-in-action/content/chapter6_searching_with_relevancy/63boosting.html) — the multiplied-boost failure mode of index-time boosts.
- [Index-time boosts not supported anymore by Solr 7, Drupal.org issue 2920225](https://www.drupal.org/project/search_api_solr/issues/2920225) — the deprecation timeline in a shipped product.
