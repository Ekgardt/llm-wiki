# Backfill must reach unprocessed sessions

Research date: 2026-09-29.

`backfill_sessions` enumerates and sorts every matching transcript, but processes
only the first 10,000. The next run selects the same prefix, including sessions
already recorded, so the suggested repeat never reaches the tail. Because the
whole list is already collected before slicing, this quota does not bound
enumeration memory. No measured, host or user requirement justifies that count.

Use the whole finite discovery result. The command is already an explicit
operator action with a dry run and caller-selected source directories. Existing
records remain untouched and each source is processed individually. Do not add
a larger count, an offset to make users work around the quota, or a persistent
cursor for a one-shot import. More unprocessed sources take more time; that is
visible in the dry run. This correction does not justify the separate existing
per-transcript read/record bounds or claim that they have been resolved.

Primary references checked on 2026-09-29:

- [Python iterator semantics](https://docs.python.org/3/library/itertools.html#itertools.islice):
  selecting a prefix stops at that position; repeating it does not advance a
  separate durable cursor. No new iterator API or dependency is required.
- [JSON Lines](https://jsonlines.org/): records can be handled individually; the
  format imposes no maximum collection of transcript files.
- GNU Findutils `find(1)`, installed primary manual, DESCRIPTION: traversal
  proceeds across entries under caller-selected roots. The online GNU manual
  failed to fetch during this check; no online freshness claim is made for it.

The application choice follows from the reproduced starvation and existing
command contract, not a claim that these sources prescribe this exact code.
Retain Python 3.10 compatibility and the current writer/DLP boundary. No paths,
schema, environment contract, scheduler or runtime root changes.

Regression: enumerate 10,000 already recorded sessions followed by one valid
unprocessed transcript, then verify the real writer reaches and records the
last transcript. The old implementation must fail. Remove the obsolete count,
unscanned-count reporting and the prefix-only helper after checking consumers;
retain failure and dry-run reporting and rerun relevant tests plus CCN checks.

The starvation regression failed on the old code (10,000 scanned instead of
10,001). After removing the prefix and its unused reporting/helper code, all
19 related tests passed, including the real write of the pending final record.
The same tests passed again in a separate public-source copy. The portable
complexity/branch-shape suite passed all 32 checks after this edit; Ruff passed.
No historical transcripts from the owner's machine were imported as a test.
