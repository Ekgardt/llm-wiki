# A link lives on one line

Dated 2026-09-14. Two LongMemEval questions, `4100d0a0` and `28dc39ac`, ended
as `harness_failure` in both 500-question runs with
`ValueError: target_text must be a bounded non-empty string`. A harness failure
that repeats on the same questions is not the harness. This is the research
before the fix.

## What was observed

- Reproduced outside the stand: ingesting either question's sessions and calling
  `longmemeval_vault.build_generation` raises the same error from
  `evidence_graph._normalized_observation`.
- The refused record, caught at that call: edge `LINKS_TO`, reason
  `unresolved_reference`, extractor `knowledge-extractor/v1`, target text
  beginning `"Latitude", "Longitude", "Crime Rate", "Safety",\n```\n\n**user:** how
  about show how the affect property prices through clustering…` — several
  hundred characters of transcript with line breaks.
- The cause is `knowledge_extractor._WIKILINK`,
  `\[\[([^\]|#]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]`. Its target class excludes `]`,
  `|` and `#` but not line breaks, so the `[[` of a quoted pandas
  `df[["Latitude", ...` and a `]]` turns later matched as one link. The writer
  (`evidence_graph._text`) refuses a target that is empty, over 4 096 characters,
  or carries `\x00`, `\r` or `\n`, and one refused record fails the whole
  generation.
- The same regex turns `[[ ]]` into an empty target after `strip()`, which the
  writer refuses too.
- It is a product defect, not a stand one: the nightly builds the same
  generation over the owner's daily logs, and any conversation that quotes nested
  brackets across lines would stop it. On 2026-08-24 the same class killed the
  nightly through another reader: a 20 000-character backticked line taken for a
  symbol reference, fixed then with `MAX_SYMBOL_REFERENCE_CHARS` in
  `_code_spans` alone.

## Practice on this date

- Obsidian's internal link is `[[target#heading|alias]]` written inline; block
  references must stay on the line they tag, and a wikilink inside inline code or
  a code block is literal text, not a link
  ([Obsidian: internal links](https://obsidian.md/help/links),
  [obsidian-help source](https://github.com/obsidianmd/obsidian-help/blob/master/en/Linking%20notes%20and%20files/Internal%20links.md),
  [wikilinks in code render as text](https://deepwiki.com/kepano/obsidian-skills/2.2-internal-links-and-wikilinks)).
  No editor this vault supports reads a link across a line break.
- A reader of untrusted text validates at the boundary it feeds: what the store
  refuses must not be produced, rather than produced and allowed to abort the batch.

## The decision

Two changes in `scripts/knowledge_extractor.py`, both at the class, not the
instance:

1. **A wikilink is one line.** `_WIKILINK` excludes `\r` and `\n` from the target,
   heading and alias classes. A quoted `[[` with no `]]` on its line is no link.
2. **The one funnel refuses what the writer refuses.** Every observation —
   wikilink, supersession, symbol mention — passes through
   `_Extraction.add_observation`; it now records a target only when
   `_storable_target` holds: non-empty, at most `MAX_OBSERVED_TARGET_CHARS = 4096`,
   no `\x00`, `\r` or `\n` — the writer's own rule. The 2026-08-24 guard in
   `_code_spans` stays: a 600-character backticked line is still not a symbol.

Why not the alternatives:

- **Catch the `ValueError` in the build and skip the source.** The source's real
  links, pages and claims would be lost with the one bad reference.
- **Stop reading links inside code blocks.** Obsidian does ignore them, and it is
  the more complete reading, but it changes which edges existing notes produce
  and needs its own measurement. Left open.
- **Bump `EXTRACTOR_VERSION`.** Every generation that was stored already passed
  the writer, so it holds no target with a line break and none empty; on those
  sources the new regex and the funnel produce identical records. Only sources
  that could never be built change. The 2026-08-24 fix of the same class did not
  bump it either.

## How it is checked

`tests/test_a_quoted_bracket_is_not_a_link.py`: a source quoting `[[` across
lines and holding `[[ ]]` yields no observed target that is empty or carries a
line break (red on the old code, which produced both), and a real `[[b]]` after
the quoted brackets still resolves to its page.

Files: `scripts/knowledge_extractor.py`,
`tests/test_a_quoted_bracket_is_not_a_link.py`,
`docs/research/2026-09-14-a-link-lives-on-one-line.md`.
