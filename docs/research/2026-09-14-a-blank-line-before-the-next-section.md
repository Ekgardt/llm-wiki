# A blank line before the next section

Dated 2026-09-14. Item 1.1 of `docs/AUDIT-2026-09-14.md`. The research before
the fix.

## What was found

- A compile *update* appends `\n\n## Update (…)` to the page
  (`compile_memory._update_section`, applied by `_ApplyPlan._replaced_page` as
  `target.content.rstrip() + update`). On a page whose last section is its Claims
  ledger, that puts one blank line between the ledger's closing fence and the new
  heading.
- The ledger is read by one pattern written three times —
  `claims._CLAIMS_RE`, `compile_memory._CLAIM_LEDGER`,
  `contradiction_pipeline._CLAIMS_RE` — and all three end with
  `(?=\r?\n(?:## |\Z)|\Z)`: exactly one line break, then a heading or the end.
  A blank line is refused.
- Reproduced on a copy of a live page with the same two calls compile makes: an
  update with no new claims leaves a ledger `parse_claim_ledger` refuses —
  `Claims ledger must be one fenced canonical JSON object`; an update that adds
  claims makes `_with_claim_ledger` miss the existing block, append a second one,
  and the page is refused as `claim page must contain exactly one Claims ledger`.
  `knowledge_extractor._claims` raises either one, so the whole generation build
  stops; so does the claims index rebuild.
- 47 notes on the live vault carry a Claims ledger today. None has an update
  section yet, which is the only reason the nightly still builds.
- The same refusal would follow a person who adds a blank line after the fence
  in an editor, which Markdown treats as nothing.

## Practice on this date

- CommonMark 0.31.2: blank lines between blocks are not significant; a fenced
  code block ends at its closing fence and "does not require a blank line either
  before or after"
  ([CommonMark 0.31.2](https://spec.commonmark.org/0.31.2/),
  [GitHub Flavored Markdown spec](https://github.github.com/gfm/)). A reader of a
  Markdown section that refuses a blank line is stricter than the format it reads.
- The ledger contract this pattern protects is that the closing fence is followed
  by nothing but the next section or the end of the page — no stray text that
  would make "the block" ambiguous. Blank lines do not weaken that.

## The decision

One pattern, in one place, that allows blank lines and nothing else:

- `claims.CLAIM_LEDGER_RE`, bytes, three groups (opening, the canonical JSON line,
  closing), ending `(?=(?:\r?\n[ \t]*)*(?:\r?\n## |\Z))` — any number of blank
  or whitespace-only lines, then a heading or the end of the page.
- `claims._parsed_ledger`, `compile_memory._with_claim_ledger` and
  `contradiction_pipeline`'s lifecycle rewrite use it; the three private copies
  are removed, so the writer and the readers cannot disagree again.

Why not the alternatives:

- **Make the writer put the update before the ledger.** It fixes compile's own
  pages and leaves the reader refusing a blank line from any other writer — a
  person, an agent, a future step. The class is the reader's strictness.
- **Strip blank lines in the writer.** Same objection, and it rewrites formatting
  the page owner chose.
- **Catch the refusal in the extractor and skip the page.** The page's claims
  would silently leave the graph.

## How it is checked

`tests/test_a_blank_line_before_the_next_section.py`: a ledger followed by a blank
line and a heading parses; compile's update-then-new-claims sequence leaves one
ledger with the merged claims; a ledger followed by stray text on the next line is
still refused.

Files: `scripts/claims.py`, `scripts/compile_memory.py`,
`scripts/contradiction_pipeline.py`,
`tests/test_a_blank_line_before_the_next_section.py`,
`docs/research/2026-09-14-a-blank-line-before-the-next-section.md`.
