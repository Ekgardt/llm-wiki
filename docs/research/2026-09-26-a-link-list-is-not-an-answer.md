# A link list is not an answer

Date: 2026-09-26. Audit 2026-09-26, finding C-10 (recall top-5 slots taken by
"Related" lists and navigation headings).

## What was wrong

The corpus is chunked by heading, so a page's `## Related` section is a chunk of
its own. It is dense with page names, and page names are what a question uses, so
the lexical leg ranked it high. The audit measured a quarter of recall's top-5
slots on this vault going to such lists or to bare navigation headings. Fact from
the vault on 2026-09-26: 154 of 209 notes carry a `## Related` section.

## Decision

A fifth ranking factor, `provenance.substance_weight`, recorded on each candidate
as `substance_weight` beside the authority, type, carried and alongside weights:

- link density = words inside `[[...]]` or `[text](url)` over all words, heading
  lines left out; a slug counts by its words (`secret-shape-decision` is three), as
  anchor text would;
- density above 0.333333, or no words at all under the heading, weighs
  `NAVIGATION_WEIGHT` = 0.25; everything else, and a chunk with no content, 1.0.

- a section whose every list item opens with a link — a "See also" list,
  annotated or not — weighs the same, whatever its density. Measured on this
  vault: an annotated `## Related` list reads 0.31, under the bound; the
  density rule alone catches 137 of the 154 `## Related` sections. With both rules 151 of the 154 `## Related`
  sections weigh 0.25; of 749 other `##` sections 24 do, all of them empty headings.

The chunk is down-weighted, not dropped: a query that names only the page a list
links to still finds it. Both ranking engines apply it: `retrieval.fuse_rrf`
through `_weigh_by_trust`, and the generation search in `search_memory` through
`_chunk_weight` (lexical and hybrid legs). The whole-page Markdown fallback is not
chunked and is unchanged.

## Source

Boilerpipe, `NumWordsRulesClassifier.java`, fetched 2026-09-26 from
https://raw.githubusercontent.com/kohlschutter/boilerpipe/master/boilerpipe-common/src/main/java/com/kohlschutter/boilerpipe/filters/english/NumWordsRulesClassifier.java

- Class documentation: "Classifies TextBlocks as content/not-content through rules
  that have been determined using the C4.8 machine learning algorithm, as described
  in the paper "Boilerplate Detection using Shallow Text Features" (WSDM 2010),
  particularly using number of words per block and link density per block."
- The first rule of `classify`: `if (curr.getLinkDensity() <= 0.333333) { ... }
  else { isContent = false; }`

Wikipedia, Manual of Style/Layout, "See also" section, fetched 2026-09-26 from
https://en.wikipedia.org/wiki/Wikipedia:Manual_of_Style/Layout:

- "The section should be a bulleted list, sorted either logically (for example, by
  subject matter), chronologically, or alphabetically."
- "Editors should provide a brief annotation when a link's relevance is not
  immediately apparent, when the meaning of the term may not be generally known, or
  when the term is ambiguous."

Fact: in that classifier a block above one third link density is never content.
Fact from the style guide: a "See also" list is a bulleted list of links whose
items may carry annotations. Conclusion (mine): annotations lower the density, so
the list shape is the second signal; the same bound fits a Markdown chunk; the weight 0.25 rather
than exclusion is my choice, so a list remains a last resort.

## Files

- `scripts/provenance.py`
- `scripts/retrieval.py`
- `scripts/search_memory.py`
- `tests/test_a_link_list_is_not_an_answer.py`
