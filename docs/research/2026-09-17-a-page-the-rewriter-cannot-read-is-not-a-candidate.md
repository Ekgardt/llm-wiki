# A page the rewriter cannot read is not a reflection candidate

Dated 2026-09-17. Finding M-B5 (the weekly reflection item) of the third audit (medium,
confirmed by reading and by a test). The research before the fix.

Files: `scripts/reflection.py`,
`tests/test_a_page_the_rewriter_cannot_read_is_not_a_candidate.py`.

## What was found

- `reflection.find_reflection_candidates` reads a page with
  `read_text(errors="ignore")` and no size bound. `reflection.reflect_page` reads the same
  page with `read_stable_bytes` under `MAX_REFLECTION_PAGE_BYTES` and decodes it strictly.
- So a page holding one byte that is not UTF-8, or a page over the bound, is offered as a
  candidate and then raises inside the rewriter (`UnicodeDecodeError`, `ValueError`).
- The weekly pass wraps the whole candidate loop in one `try`
  (`scheduled_weekly._reflect`), so that one page ends reflection for every page after it,
  every week, until somebody repairs the page by hand.

## Practice on this date

- The two handlers are different contracts: `strict` — "Raise UnicodeError (or a
  subclass), this is the default"; `ignore` — "Ignore the malformed data and continue
  without further notice" ([codecs, Python 3 documentation](https://docs.python.org/3/library/codecs.html)).
  A selector that reads under the lenient contract and hands its choice to a worker under
  the strict one selects inputs the worker must refuse. The selection and the work read
  the page the same way.

## The decision

- One reader in `reflection.py` — bounded, stable, strict UTF-8 — serves both the finder
  and the rewriter. A page it cannot read is not a candidate; nothing is rewritten from a
  lossy decode.
- `scheduled_weekly.py` is not changed (it belongs to another area). A transaction or
  fence failure still ends the loop there, which is right: those are not faults of one
  page. No path, environment variable or contract changes.
