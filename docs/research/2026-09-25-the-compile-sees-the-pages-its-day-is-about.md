# The compile sees the pages its day is about

Date: 2026-09-25. Audit items B-12 and B-13 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and on the live vault)

- The compile budget is a 32 768-token window (`COMPILE_CONTEXT_WINDOW_TOKENS`)
  less 4 000 for the answer and 1 024 slack. The live notes are 736 001 bytes,
  about 200 000 tokens: a batch can show the planner a fraction of them.
- `pack_compile_batches` offers the optional context in `inputs.sources` order,
  which is sorted by logical path, and `_fitting_context` takes every page that
  still fits, first come first served. The pages a batch sees are the
  alphabetically early ones, not the ones its days talk about.
- A page the planner does not see cannot be updated, so a day about a known
  subject creates a new page beside the old one: 12 near-duplicate pairs on the
  live vault (B-12), and old figures that the new day contradicts stay where the
  planner never looks (B-13: two pages say a cold search takes 23 s; it takes
  2.3 s).

## Source

- Okapi BM25, https://en.wikipedia.org/wiki/Okapi_BM25 (fetched 2026-09-25):
  "a ranking function used by search engines to estimate the relevance of
  documents to a given search query", with IDF
  `ln((N - n(q) + 0.5) / (n(q) + 0.5) + 1)` weighting a term by its rarity.

## Decision

- The optional context of a batch is offered in order of relevance to that
  batch's days: the IDF-weighted overlap of word sets (BM25's IDF, terms of four
  or more letters, case-folded) between the days and each page, ties by path.
  The greedy fit is unchanged; only the order changes, so the pages that share
  the day's rarer words come first.
- No model is asked and nothing is written; the ranking is deterministic, so the
  plan cache and the batch identity stay stable for the same inputs.
- The 12 existing pairs are not merged here: deciding that two pages are the
  same page is a semantic supersession, which the contract keeps disabled. They
  stop multiplying; the next compile about their subject sees the older page.

## Files

- `scripts/compile_memory.py`
- `tests/test_the_compile_sees_the_pages_its_day_is_about.py`
- `CHANGELOG.md`
