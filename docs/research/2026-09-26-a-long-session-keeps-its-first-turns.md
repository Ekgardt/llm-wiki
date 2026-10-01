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

## Native replacement evidence (2026-10-01)

Windows CI on commit `8046f245` failed both replacement cases inside the
test's `Path.replace`, before the capture reader could check the path identity.
The reader holds its `os.open` descriptor until its `finally` closes it;
`_read_capture_windows` refers to byte windows, not a Windows-only reader.
CBM traces and the current source confirm that transcript capture uses this
reader and then verifies both the bytes and the identity at the path.

The native contracts differ. Microsoft documents that an open handle without
`FILE_SHARE_DELETE` prevents deletion or renaming until it closes. Linux permits
renaming while the old descriptor remains valid. Python exposes the underlying
rename operation and its possible errors. Sources checked on 2026-10-01:

- [Microsoft CreateFileW sharing modes](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
- [Linux rename(2)](https://man7.org/linux/man-pages/man2/rename.2.html)
- [Python os.replace](https://docs.python.org/3/library/os.html#os.replace)

Keep the successful-replacement rejection cases on POSIX with their original
`changed|replaced` assertion. On Windows, separately assert that replacement is
refused while reading, the original identity and bytes survive, and capture
returns the unchanged result. The same replacement must succeed after capture
returns, proving that the refusal was tied to the open descriptor rather than
permanent directory permissions. Run both small and windowed transcripts.
Truncation and rewrite rejection remain cross-platform.

Broadening the error regex would incorrectly count the test fixture's OS refusal
as evidence of the reader's identity check. Faking a successful replacement on
Windows would also miss the native contract. No production validation changes
are needed. Ruff, Lizard (maximum CCN 2 in the affected tests and callbacks), and
AST checks pass locally; the new native Windows cases still require Windows CI.
