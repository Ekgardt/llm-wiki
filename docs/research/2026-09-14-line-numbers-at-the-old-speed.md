# Line numbers at the old speed

Dated 2026-09-14. Item 0.1 of `docs/AUDIT-2026-09-14-2.md` — a regression of my own
earlier today. The research before the fix.

## What was found

`code_extractor._span`, changed this morning for files with old Mac line endings
(`docs/research/2026-09-14-the-rest-of-the-readers-before-the-writer.md`), computes
each span's line numbers with `content.count(b"\n", 0, index)` — a scan from the
start of the file for every node. The review measured it on `scripts/doctor.py`
(314 KB, 6 822 spans): identical output, **0.019 s before, 1.82 s after**; the cost
grows with the square of the file. `_span` has four callers (the graph:
`_PythonFile.span`, `_Collector._add_table`, `_route`, `_sql_edges`), all passing the
same source bytes for every node of a file, and no deadline is checked inside.

`evidence_graph` removed exactly this pattern on 2026-09-12 — "One walk per source
instead of one per occurrence … 11 124 scans and 1.24 s of a single 3.2 s answer" —
with an `lru_cache` of newline positions and a binary search (`_newline_offsets`,
`_line_at`).

## Practice on this date

- Python's `bisect` finds a position in a sorted sequence in O(log n)
  ([bisect](https://docs.python.org/3/library/bisect.html)); a table of line starts
  built once per text is the standard way to map offsets to lines.
- `bytes` objects cache their hash after the first computation, so an
  `lru_cache` keyed by the content is looked up without rehashing the file each
  call ([object.__hash__ and immutable types](https://docs.python.org/3/reference/datamodel.html#object.__hash__)).

## The decision

`code_extractor` keeps the Python-style line table for mapping `ast` positions to
bytes, and records line numbers the writer's way from a cached table of `\n`
positions with `bisect` — the same rule and the same technique as
`evidence_graph._line_at`. Output is unchanged for every file; a test pins both the
result on `\n`, `\r\n` and `\r` files and that a large file's spans stay linear.

Files: `scripts/code_extractor.py`, `tests/test_line_numbers_at_the_old_speed.py`,
`docs/research/2026-09-14-line-numbers-at-the-old-speed.md`.
