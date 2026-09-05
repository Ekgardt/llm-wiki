---
type: decision
status: active
confidence: high
source_authority: ai-derived
date: 2026-09-03
---

# A Question Is Not A Conjunction

One-sentence summary: the words of a query are joined with OR and function words
are dropped, because FTS5's implicit AND meant a chunk had to contain every word
of the question and a question phrased as a question matched nothing.

## Context

FTS5 puts an implicit AND between bare terms. `MATCH 'one two three'` is
`one AND two AND three`; the SQLite documentation states it directly. Both
lexical query builders joined the words of the query with spaces, so every word
had to appear in the same chunk.

Measured on the LongMemEval stand 2026-09-03, on a vault of 184 chunks:

- "What day of the week do I take a cocktail-making class?" — **0 candidates**
- "cocktail class" — 3 candidates
- "class", lexical only — 19

Three of fifty questions reached the model with an **empty** evidence manifest.
The model answered, correctly, that it had been given nothing. Those questions
were counted as refusals for months, and they were retrieval returning nothing
at all in 0.06 seconds against a median of 12.

The failure is silent by construction: an over-specific conjunction returns
zero rows, which is indistinguishable from a vault that holds no answer.

## Decision

The terms are joined with `OR`, and function words are dropped first. When the
query is nothing but function words, all of it is kept — an empty term list
would be a syntax error, not a search.

## Why this does not loosen relevance

bm25 separates a query into its component phrases and scores a row by how many
it carries. A chunk holding every term still outranks one holding a single term.
What changes is that the one-term chunk becomes reachable instead of discarded,
and the ranking decides rather than the parser.

## Consequences

The question above now retrieves 40 candidates against a limit of 40. Full
suite green: 7925 passed, 256 skipped.

The effect on answer quality is being measured and is not claimed here.

## Source / Evidence

- `scripts/search_memory.py` — `_fts_query`, `_query_terms`, `_carries_evidence`,
  `_legacy_bm25_rows`
- `tests/test_a_question_can_be_asked_as_a_question.py`
- https://www.sqlite.org/fts5.html — the implicit-AND rule and bm25 phrase scoring

## Links

- [[knowledge/notes/one-trust-weight-across-retrieval-paths-decision]]
- [[knowledge/notes/derived-evidence-generation-decision]]
- [[knowledge/notes/a-fact-is-stored-with-its-date-decision]] — links to this page.
