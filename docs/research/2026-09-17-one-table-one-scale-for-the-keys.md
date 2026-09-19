# One table, one scale for the keys

Dated 2026-09-17. The key lookup added on 2026-09-16 (`13fd7c7`) never returned a row. Found
by the third audit; the research before the fix.

## What was found

- `search_memory._key_matched_rows` selected an unqualified `chunk_id` from
  `chunk_keys JOIN chunks`. SQLite answers `ambiguous column name: chunk_id` on every call.
- The function caught `sqlite3.OperationalError` to tolerate an artifact built before the
  table existed, so the same clause swallowed the defect: on a real artifact holding a key
  row the function returned `[]`, and nothing logged it.
- The test of that change ran its own SQL with the column qualified and never called the
  function it was written for — the shortcut the owner has named before: a test that does
  not exercise the product path proves nothing.
- Had the join worked, a second defect waited: rows ranked by `bm25(chunks)` and rows ranked
  by `bm25(chunk_keys)` were concatenated and sorted as one scale. BM25 depends on each
  table's own document-length statistics, so scores from two tables are not comparable
  (measured by the audit on a synthetic artifact: -13.7 and -18.9 against -3.9).
- The research note of 2026-09-16 had decided a *column* beside `content`; the code built a
  *separate table* because the validator compares stored rows with rows rebuilt from the
  sources. It does not need to: `_FTS_CHUNK_SELECT` names the twenty-two source-derived
  columns explicitly, so a twenty-third column is outside the comparison.

## Practice on this date

- FTS5 ranks one table with one BM25 over all of its indexed columns, with optional
  per-column weights — `bm25(tbl, w1, w2, …)` — which is the supported way to let a second
  text field contribute to the same score
  ([SQLite FTS5, the bm25 function](https://www.sqlite.org/fts5.html#the_bm25_function)).
- An exception handler must be as narrow as the condition it is written for: test for the
  condition (does the column exist) instead of catching the error class every other SQL
  mistake also raises.

## The decision

- The keys are a twenty-third column, `keys`, of the `chunks` table. One `MATCH`, one BM25,
  no join, no second query, no score mixing. Readers still read `content`.
- The artifact version becomes `corpus-search/v2`. A `corpus-search/v1` artifact — without
  the column — stays valid and readable, so the installed vault is not degraded between the
  merge and its next build; the shape accepted is the one its own version declares.
- The `chunk_keys` table, `_key_matched_rows` and its broad `except` are removed.
- The test calls the product's search path and fails without the fix.

Files: `scripts/search_memory.py`, `scripts/doctor.py`,
`tests/test_the_keys_are_indexed_beside_the_turn.py`, `tests/test_generation_maintenance.py`,
`docs/research/2026-09-17-one-table-one-scale-for-the-keys.md`.
