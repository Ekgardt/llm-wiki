# The last two writers of the old page identity

Dated 2026-09-18. Merge integration: eight branches were merged into one and the
telemetry identity decision of 2026-09-17 landed only in the writers its own
agent could see.

Files: `scripts/mcp_server.py`, `tests/test_mcp_server.py`,
`tests/test_search_ranking.py`, `tests/test_access_tracking.py`

## What was found

`docs/research/2026-09-17-one-page-one-identity-and-one-set-of-windows.md`
decided: **one column, one identity — the page's vault-relative path, for every
event kind and every writer** of `retrieval_events.candidate_id`. That branch
converted `retrieval`'s impressions, `mcp_server._decision_impression_events`
and `access_tracking.record_access`. Two writers in `scripts/mcp_server.py` were
left on the old identity, because they sit in the read-page and context paths
that the retrieval branch never opened:

- `_page_with_evidence` passed the bare **slug** to `_record_page_reads`, so a
  `page_read` row named `page`, not `knowledge/notes/page.md`.
- `_context_injection_events` reduced each selected path to its **stem**
  (`Path(path).stem`), so `knowledge/projects/alpha/state.md` and
  `knowledge/projects/beta/state.md` both wrote `state`.

Both are exactly the collision the decision named: a stem is not an identity.
And both are pages that `access_tracking` counts: it reads
`read_events(candidate_id=page_identity(slug))`, which is
`knowledge/notes/<slug>.md`, so every read through `read_page` and every page
injected through `get_context` was invisible to the access counts it is supposed
to feed.

Three tests still expected the identity the decision replaced: two in
`tests/test_search_ranking.py` (a slug, and the chunk hash) and one in
`tests/test_mcp_server.py` (a slug). They were the stale side, not the product.

`evidence_read` keeps the quote's `sha256`. It does not name a page: it names
one evidence item inside a page, there is no path for a quote, and the decision's
"nothing else changes shape" clause covers it. Named here so the exception is on
the record and not mistaken for a fourth identity.

## Sources

- In-repository decision,
  `docs/research/2026-09-17-one-page-one-identity-and-one-set-of-windows.md`:
  "**One identity: the page's vault-relative path**, for every event kind and
  every writer."
- In-repository code, `scripts/access_tracking.py` → `page_identity`: "The
  vault-relative path of a note, which is how the telemetry names a page."
- Python documentation,
  [`PurePath.as_posix`](https://docs.python.org/3/library/pathlib.html#pathlib.PurePath.as_posix)
  (fetched 2026-09-18): "Return a string representation of the path with forward
  slashes (/)". The page-read row is built from a `Path.relative_to` result, so
  it is spelled with `as_posix()` and a Windows vault writes the same identity a
  POSIX vault does.

## The decision

- The two remaining writers name the page by its vault-relative path.
  `_page_with_evidence` resolves `page_path.relative_to(root).as_posix()` once
  and hands it to `_record_page_reads`, whose parameter is renamed from `slug`
  to `page_path` so the next reader cannot make the same mistake.
  `_context_injection_events` iterates the selected paths it already receives
  instead of reducing them to stems.
- The three stale expectations are corrected to the decided identity, and the
  two tests that only checked the event kind now name the candidate as well, so
  a regression to a slug fails.
- Rows written before today keep whatever they hold, as the original decision
  ruled: the column is free-form text in a derived, disposable database.
- One consequence had to be fixed with it: `TestGetAccessStats` in
  `tests/test_access_tracking.py` asserted counts of `get_access_stats`, which
  merges the telemetry with the legacy log, without owning the telemetry
  database. While `read_page` wrote a slug nothing matched; now that it writes
  the real identity, a row another test file left in the shared state root is
  counted, and the test failed by run order. Each of those tests now points
  `retrieval_telemetry.TELEMETRY_DB` at its own `tmp_path` database. The test
  owns both of the sources it asserts on.
