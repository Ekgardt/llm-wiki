# An evidence span names its own block

Date: 2026-09-26. Audit 2026-09-26 items A-1 and B-4 (docs/AUDIT-2026-09-26-full.md).

## Fact
- A daily log is cut into entries by `evidence_resolver.daily_entries`; each entry
  has a block id taken from its `## [HH:MM:SS]` header or the first line after an
  operation marker. Two entries can carry the same id: the 20th-prompt capture
  writes a `## [HH:MM:SS] pre-compact` header in the same second as the prompt
  line that triggered it. The live `knowledge/daily/2026-09-25.md` has two such
  pairs.
- The compiler picks the entry by quote when ids repeat
  (`compile_memory._evidence_block`), and every reference it writes carries the
  byte span of the quote (`EvidenceRef.byte_start/byte_end`).
- The resolver refused any id that occurs twice (`_sole_block_span`), so the
  compiler's own reference failed its critique ("evidence block is ambiguous or
  missing"). The live compile of 2026-09-25 21:04 failed that way, and a failed
  batch stops the run, so every later day waits. The archiver resolves each block
  by id with its own span and is refused the same way.
- Entries never overlap, so at most one entry with that id contains the span.
- `user_prompt_capture._append_prompt_tag` kept the newlines of the prompt's
  first 140 characters, so a prompt containing `\n## [09:00:00] session-end` wrote
  a real entry into the daily log (B-4).

## Source (fetched 2026-09-26)
CommonMark 0.31.2, ATX headings, https://spec.commonmark.org/0.31.2/: "An ATX
heading consists of a string of characters, parsed as inline content, between an
opening sequence of 1–6 unescaped # characters … The opening sequence of #
characters must be followed by spaces or tabs, or by the end of line." A line
break inside a breadcrumb is enough to start a new heading, which is exactly how
the entry grammar reads it.

## Decision
- `_sole_block_span` selects, among entries with the reference's id, the one whose
  bytes contain the reference's span; zero or more than one is still refused.
  A reference is the id plus the span, and the span decides.
- The prompt breadcrumb collapses all whitespace (newlines included) to single
  spaces before it is cut, so it is one line, as its docstring says.

## Files
- scripts/evidence_resolver.py
- scripts/user_prompt_capture.py
- tests/test_an_evidence_span_names_its_own_block.py
