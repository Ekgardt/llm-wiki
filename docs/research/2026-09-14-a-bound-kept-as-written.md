# A validity bound kept as written

Dated 2026-09-14. Item 0.2 of `docs/AUDIT-2026-09-14-2.md` — a regression of my own
earlier today. The research before the fix.

## What was found

This morning `corpus_snapshot._metadata` started storing `valid_from`/`valid_to` only
when they parse as ISO dates (`_metadata_instant`), to stop `valid_from: someday` from
failing an `as_of` read (`docs/research/2026-09-14-one-page-cannot-close-the-vault.md`).
The review showed the cost: those values are part of every stored chunk row, which
validation rebuilds and compares. For the same page, `someday` used to be stored as
`'someday'` and `2026` as `'2026'`; now both are `None`. A generation built before the
change no longer matches its own rebuilt rows until it is rebuilt, and the extractor
version was not bumped. Whether a value parses also depends on the interpreter
(`datetime.fromisoformat` accepts `20260901` on 3.11+, not on 3.10), so the same page
could produce different rows on different Pythons.

The code graph: `_metadata_instant` feeds `_metadata` and `_not_instant` (lint);
`_as_datetime` is read by `_within_validity` and `_included`. Nothing outside
`corpus_snapshot` parses these fields.

## Practice on this date

- Stored derived data must be a deterministic function of its input; normalizing at
  write time changes stored identity, while interpreting at read time does not — the
  same distinction this vault made for `status` (kept as text, judged by
  `is_retired`).
- `datetime.fromisoformat` accepted more formats from Python 3.11
  ([datetime.fromisoformat, "Changed in version 3.11"](https://docs.python.org/3/library/datetime.html#datetime.datetime.fromisoformat)).

## The decision

- `_metadata` stores `valid_from`/`valid_to` exactly as before today: the text as
  written (dates as ISO text). Stored rows are unchanged for every page.
- The unreadable bound is handled where it is read: `_within_validity` treats a bound
  that does not parse as absent, so `as_of` reads still never raise over one page.
- Lint keeps naming such a value (`frontmatter_problems`).

Files: `scripts/corpus_snapshot.py`, `tests/test_one_page_cannot_close_the_vault.py`,
`docs/research/2026-09-14-a-bound-kept-as-written.md`.
