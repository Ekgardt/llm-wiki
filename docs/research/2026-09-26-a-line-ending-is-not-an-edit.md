# A line ending is not an edit

Date: 2026-09-26. Audit 2026-09-26, finding B-8 (the CRLF part; B-36 incomplete).

## What was wrong

Facts, `scripts/impact_analysis.py` at a2965838, with `core.autocrlf=true`:

- The index and every commit hold LF; the worktree holds CRLF. For a
  `worktree-index` change the old side is the LF blob and the new side the CRLF
  file, so `_changed_ranges` saw every line differ and one hunk covered the whole
  file: every symbol in it was "changed".
- The generation indexes the worktree bytes (CRLF), so `_offset_classification`
  never found them equal to the LF old blob and every symbol was `approximate`.
- Measured with the new test on the old code: an edit to `beta` reported
  `alpha` too, both `approximate`; the range for a one-line edit was lines 1-6.

## Decision

- Lines are compared with a CRLF ending read as the LF Git stores
  (`_compared_lines`). When two sides differ in nothing but line endings, that
  difference is the edit and the raw bytes are compared.
- When the indexed bytes and the old blob hold the same lines apart from CRLF,
  the old range is moved onto the indexed bytes line for line
  (`_indexed_old_range`, `_moved_range`) and the answer stays `exact`. Other
  content still makes it `approximate` (B-36 is kept).
- `_offset_classification` is removed; `_indexed_old_range` replaces it.
- Tests: `tests/test_a_line_ending_is_not_an_edit.py` builds a real
  `core.autocrlf=true` checkout and requires only the edited symbol, `exact`.

## Source

Git, `Documentation/config/core.adoc`, fetched 2026-09-26 from
https://raw.githubusercontent.com/git/git/master/Documentation/config/core.adoc:
"core.autocrlf:: Setting this variable to "true" is the same as setting the `text`
attribute to "auto" on all files and core.eol to "crlf". Set to true if you want to
have `CRLF` line endings in your working directory and the repository has LF line
endings."

## Files

- `scripts/impact_analysis.py`
- `tests/test_a_line_ending_is_not_an_edit.py`
