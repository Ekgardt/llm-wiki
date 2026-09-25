# A question finds pages when no generation is active

Date: 2026-09-25. Audit item B-22 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code; the example is the audit's)

- With no active generation, `markdown_hits` reads Markdown directly.
  `_direct_markdown_hits` takes every word of the query (`\w+`), stop words
  included, and `_direct_page_hit` requires all of them in the page
  (`query_terms.issubset(terms)`): an implicit AND. "почему systemd таймер, а не
  cron" found nothing.
- The hybrid path's lexical leg already drops `_QUERY_STOPWORDS` and ranks on
  the words that carry evidence (`_query_terms`: "Dropping them is what makes an
  OR query rank on evidence"). The fallback did not use it, and the list lacks
  common Russian function words such as «а», «но», «почему», «ли», «же».

## Source

- Elasticsearch `match` query, https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-match-query
  (fetched 2026-09-25): `operator` "OR (Default) — a query value of `capital of
  Hungary` is interpreted as `capital OR of OR Hungary`". A full-text question is
  matched on any of its terms and ranked by how many it shares.

## Decision

- The fallback uses `_query_terms` (stop words out, every word kept when all are
  stop words) and qualifies a page that shares at least one term; its score is
  the number of shared terms, with the existing title and filename boosts when
  the page carries every term. Hits keep `fallback_reason: no_active_generation`.
- The stop-word list gains the Russian function words above; it is shared, so
  the hybrid lexical leg benefits too.

## Files

- `scripts/search_memory.py`
- `tests/test_a_question_finds_pages_without_a_generation.py`
- `CHANGELOG.md`
