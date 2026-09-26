# A long session keeps its first turns

Date: 2026-09-26. Audit 2026-09-26, finding C-5.

## What was wrong

A transcript over the capture bound (900 KiB) is kept as a head and a tail, each
half the bound. Both were raw byte windows. A Claude transcript starts with
`file-history-snapshot` records, which the session renderer drops whole, so the
head could hold no turn at all and the start of the session was lost although the
record said it kept the beginning.

Measured on this machine on 2026-09-26 (read-only, host transcripts): six
transcripts were over the bound; in two of them the first 450 KiB held no turn,
and filling 450 KiB with records the renderer keeps took reading between 3.0 and
9.04 windows. Decoding 16 windows (7.2 MiB) of the largest took 0.23 s.

## Decision

- `session_evidence.is_service_record(line)`: a JSON record that the renderer
  drops whatever surrounds it — not a user or assistant turn, not a capture gap.
  A line that is not JSON is not one, so a plain-text transcript is unchanged.
- Each edge is read over up to `EDGE_SCAN_WINDOWS = 16` windows (never past the
  middle of the file), service records are skipped, and whole lines are kept until
  the window is full: the first turns forward from the start, the last turns
  backward from the end. When no whole line fits, the raw window is kept, as
  before. The gap line still counts every byte not kept.
- Tool-result lines stay: whether one is a subagent's report depends on the call
  that preceded it, so one line alone cannot say it is dropped.

## Source

JSON Lines, https://jsonlines.org/, fetched 2026-09-26:

- "Each Line is a Valid JSON Value … a blank line is not."
- "Line Terminator is `'\n'`. This means `'\r\n'` is also supported because
  surrounding white space is implicitly ignored when parsing JSON values."

So a record is exactly one line and can be judged on its own, which is what makes
skipping whole records safe.

## Files

- `scripts/integration_adapter.py`
- `scripts/session_evidence.py`
- `tests/test_a_long_session_keeps_its_first_turns.py`
