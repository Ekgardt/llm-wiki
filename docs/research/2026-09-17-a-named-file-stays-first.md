# A named file stays first

Dated 2026-09-17. Found by the third audit (retrieval, M6); the research before the fix.

Files: `scripts/retrieval.py`, `tests/test_a_named_file_stays_first.py`

## What was found

- A query that is a file's own name ("2026-09-01") is answered by that file: retrieval
  promotes the exact filename to the first place and reports the mode `EXACT`
  (`_promote_exact_filename`, `_is_exact_filename_answer`: "An exact filename stays the
  answer after fusion and reranking").
- The promotion ran before the two steps that have the last word on the order.
  `_page_diverse` puts compiled pages before daily and raw files, and `_evidence_ordered`
  re-sorts the daily and raw places by the lane score. Neither knows about the promotion.
- So the rule held for a compiled page and was undone for exactly the files people name by
  date. Reproduced by the audit with three candidates and the query `2026-09-01`: promoted
  `[d1, d2, n1]`, after page diversity `[n1, d1, d2]`, after the lane score `[n1, d2, d1]`;
  the reported mode was no longer `EXACT`.

## Practice on this date

- Search engines apply an explicit promotion after organic ranking, not before it, so that
  no later scoring step can undo it. Elasticsearch's pinned query: "Promotes selected
  documents to rank higher than those matching a given query. This feature is typically used
  to guide searchers to curated documents that are promoted over and above any 'organic'
  matches for a search."
  (<https://www.elastic.co/guide/en/elasticsearch/reference/current/query-dsl-pinned-query.html>)

## The decision

- The visible order is one pipeline with the promotion as its last step: page diversity,
  then the lane score over episodes, then the named file first. Both places that build a
  result — the finished run and the salvaged partial run — use the same function.
- The two earlier promotions stay: they keep the named file inside the candidate cap and
  inside the reranker's pool. They no longer decide the final order.
- Only the named chunk moves; every other candidate keeps the place the two ordering steps
  gave it.
