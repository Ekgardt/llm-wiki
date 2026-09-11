# A grown line does not reach the next symbol

Date: 2026-09-11. Trigger: audit finding M13 (found 2026-09-10 evening
while fixing the Windows dirty-change test). `impact_analysis._map_symbols`
compared `changed_range["new"]` — byte offsets in the working-tree bytes —
with the occurrences stored in the active generation, whose offsets are
those of the indexed (old) bytes. Growing `return value + 1` to
`return value + 1000` made `changed_symbols` name `caller` beside `helper`:
the grown new-side range reached the next symbol's old offset. The error
is one-sided (a shrink cannot miss a symbol), so the test edit was made to
shrink; the product still over-reported on every grown line.

## Sources

1. `impact_analysis._changed_ranges`: each hunk carries both an `old` and a
   `new` byte range for the same edit; the old range is expressed in the
   bytes the generation indexed.
2. `evidence_graph` occurrences: `byte_start`/`byte_end` are offsets into
   the source bytes captured at build time — the old side, by definition,
   for any file that changed since.
3. A symbol that exists only on the new side is not in the generation and
   cannot be named by any mapping; the working tree is re-parsed by the
   live LSP path, not here.

## Decision

Both sides are still reported (`sides` says which side of the hunk holds
the file), but the overlap test always uses the hunk's `old` range,
because that is the coordinate system the generation's occurrences live
in. An insertion-only hunk (empty old range) keeps the line fallback,
which names the symbol containing the insertion point. `_map_symbols` is
a pipeline of steps under CCN 5. The query-surface test grows the line
again and expects `helper` alone.

Files: `scripts/impact_analysis.py`, `tests/test_impact_analysis.py`,
`tests/test_query_surface.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
