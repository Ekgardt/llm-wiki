# A title lifts only the whole question

Date: 2026-09-26. Audit 2026-09-26, finding C-10 (the Markdown fallback overrates
one shared word in a title or filename).

## What was wrong

With no active generation, `search_memory._direct_markdown_hits` scores a page by
how many of the question's evidence words it shares, then multiplies by 3 when the
title holds them and by 4 when the filename does. The check was made against the
*shared* words, not the question's: a page sharing one word of "redactor secret
shape", in its title and filename, scored 1 × 3 × 4 = 12, and a page holding all
three words in its body scored 3. The test below reproduces that order on the old
code.

## Decision

The base score stays the count of shared words. The title and filename multipliers
apply only when the title or filename holds every evidence word of the question —
the "a filename match is too strong a signal to lose" case they were written for.

## Source

Elasticsearch reference, multi_match query, fetched 2026-09-26 from
https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-multi-match-query:

- "Smith as a last name is very common (and so is of low importance) but Smith as a
  first name is very uncommon (and so is of great importance)."
- The "Smith Jones document will probably appear above the better matching Will
  Smith because the score of `first_name:smith` has trumped the combined scores of
  `first_name:will` plus `last_name:smith`."
- `cross_fields`: "all **terms** must be present **in at least one field** for a
  document to match."

Fact from the source: scoring one field's match of one term above a document that
matches the whole query is a known failure of field-centric scoring. Conclusion
(mine): a field boost must be earned by the whole question, not by one of its words.

## Files

- `scripts/search_memory.py`
- `tests/test_a_title_lifts_only_the_whole_question.py`
