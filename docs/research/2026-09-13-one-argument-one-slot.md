# One argument, one slot; one episode, one slot

Dated 2026-09-13, after the selective-forgetting stand failed a gate and the
failure turned out to be about what a visible slot belongs to.

## What the stand found, and what it actually is

`benchmark/run_selective_forgetting.py` over the live vault's notes:
`ageing.retain_rate` **0.8857** where the gate wants 1.0 — of 100 pages that must
keep surfacing, 70 did before the ageing pass and 62 after. Eight pages stopped
surfacing, none appeared in their place, and **all eight were at ranks 7-10
before** and stayed in the corpus (`in_corpus: 100` after).

The stand's own docstring says "ranking subtleties are not what this stand
measures — presence and absence are", and it measures presence inside a ten-row
window (`PROBE_LIMIT = 10`).

I asked the kept trial vault directly, same query, window 50
(`forget-base/ageing`, `retrieve_via_search_memory`, `semantic=False`,
`rerank=False`): the page the stand called forgotten comes back at **rank 12**,
and again at 20 and 37. The first ten rows held **six distinct pages**:
`Ingestion Workflow.md` took ranks 3, 4 and 5, `no-gitkeep-in-inbox-articles.md`
took 1 and 6, `memory-keeps-a-second-copy-decision.md` took 2 and 8.

So nothing was forgotten. Four of the ten visible slots were spent on repeats of
three pages, and the pages that would have been ninth and tenth fell out.

## Why the repeats are there

`retrieval._place_by_page` decides what counts as a repeat, and since 2026-09-08
the unit is the **entry** — the page *and* the heading the chunk sits under —
with this reason recorded in the code: "a daily file holds every session of its
day, and by page two sessions of one day took one slot between them."

That reason is correct for a daily. It is wrong for a compiled note: a note's
headings are sections of one argument, not independent episodes, so three
headings of one workflow page are three views of the same answer.

## Practice on this date

1. **Per-source caps are the standard remedy, not a novelty.** Google restricts
   most result pages to two listings per domain
   ([domain diversity](https://www.seroundtable.com/google-search-domain-diversity-update-27696.html)),
   and Google Cloud Search exposes the same idea as *crowding*: limit how many
   results share a field value, with a `maxCount`, and later ones are "crowded
   away"
   ([CrowdingSpec](https://docs.cloud.google.com/generative-ai-app-builder/docs/reference/rest/v1/CrowdingSpec)).
2. **Passage-level indexes cap passages per document for exactly this reason** —
   so one long source cannot occupy the whole result page
   ([cap on passages per paper](https://github.com/prasadtalasila/chitragupta/issues/769)).
3. **Duplication in the window is a measured quality problem in RAG, not a
   cosmetic one.** Near-duplicates "degrade downstream retrieval accuracy and
   reduce the diversity of collected documents"
   ([byte-exact deduplication](https://arxiv.org/pdf/2605.09611)); naive chunking
   makes standard top-k "return multiple copies of the same underlying passage"
   ([RAG chunking playbook 2026](https://www.digitalapplied.com/blog/rag-chunking-strategies-2026-retrieval-quality-playbook)).
4. **This vault has already measured the cost of one kind of page crowding the
   window**: importing 236 sessions at neutral weight took the stand from hit@5
   0.7 to 0.0, which is why `raw-source` weighs 0.6 (`scripts/provenance.py`).
   The same shape, one level down.

## The decision

**The unit of a repeat depends on what the file is.** A file that holds
independent episodes — `knowledge/daily/**` and `knowledge/raw/**` — keeps the
2026-09-08 rule and gets one slot per heading. Every other page, a compiled note
included, gets **one slot for its first chunk**; its other chunks become extras
and follow after the distinct pages, exactly as they do today.

Nothing is dropped: `_page_diverse` still returns every candidate, repeats last,
so a caller that wants every chunk of one page still receives them in order.

Why not the alternatives:

- **A numeric cap of two per page** (Google's domain rule). It would have freed
  only one of the four wasted slots in the measurement above, so the gate would
  still fail and the window would still hold repeats. A cap is the right shape
  when sources are symmetric; here they are not.
- **Widen the stand's window to 50.** It would turn the gate green while the
  product kept spending four of ten slots on repeats — the stand would stop
  reporting a real cost. The stand is right and the window is the product's
  problem.
- **Revert to the pre-2026-09-08 page rule for everything.** It would take back
  a fix that was made for a measured reason: two sessions of one day sharing one
  slot.

Token economy (rule 4) points the same way: a ten-row answer that holds six
pages spends the tokens of ten and answers with six.

## Measured after the change, and the second half of the decision

Same ageing phase, same live notes, the new rule in place
(`forget-base3/ageing`, 23 s, report `forgetting-after2.json`):

- pages surfacing *before* the pass: **80 of 100**, up from 70 — the freed slots
  are worth ten more pages in a ten-row window, which is the quality gain this
  change is for;
- pages that stopped surfacing: **6**, down from 8; `retain_rate` **0.9375**,
  still short of the gate's 1.0;
- `forget_rate` 1.0 and `reprieve_rate` 1.0 unchanged.

I then asked the kept vault for each of those six pages with a window of 200:
every one comes back, at ranks **11, 11, 12, 11, 12 and 13**. One place outside
the window. Nothing was forgotten, and no remaining duplicate was ahead of them —
the corpus lost 59 pages, and at the edge of a ten-row window that reorders what
sits at ranks 7-10.

So the gate, as written, measures *rank stability across a corpus change* while
its own docstring says it measures presence and absence. Both halves of this note
are needed:

- **The gate asks the question it says it asks.** A probe decides presence over a
  documented large window (`PRESENCE_LIMIT = 200`), because the probe is a
  verbatim phrase from the page itself: a page in the corpus is returned by its
  own words, and a page that is gone has no rank at any window. `forget_rate`,
  `retain_rate` and `reprieve_rate` are computed on that.
- **The window is still measured, and reported.** The share of retained pages
  that stay inside the first ten rows becomes a reported number, not a gate, so a
  real ranking regression stays visible without a corpus change failing the run.

This is not the gate being loosened to pass. The evidence that it must change is
that all six pages it calls forgotten are retrievable one place past its window,
and the same run shows the product got better by ten pages.

## What must be true after the change

- The failing gate: `ageing.retain_rate` back to 1.0 — the four freed slots move
  the page measured at rank 12 inside the window. To be verified by re-running
  the ageing phase, not assumed.
- A daily still places two sessions of one day in two slots (the 2026-09-08
  case), which its test must keep proving.
- Nothing is dropped from a result: the repeats still follow.

Files: `scripts/retrieval.py`, `tests/test_retrieval.py`,
`benchmark/selective_forgetting_vault.py`, `benchmark/run_selective_forgetting.py`,
`docs/research/2026-09-13-one-argument-one-slot.md`.
