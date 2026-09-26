# A killed write leaves nothing behind

Date: 2026-09-26. Found while closing audit 2026-09-26 C-11/C-12.

## What was wrong

Every writer here stages `.<name>.<nonce>.tmp` next to its target and renames it
over. A process killed between the two leaves the staged copy. The nightly sweep
(`reclaim_runtime_state.sweep_orphan_temporaries`) looked only at the top of
`run/`. Checked 2026-09-26 on the live vault: three such files under `knowledge/`,
from 2026-08-26, 2026-09-07 and 2026-09-07 — two of them whole project journals
(720 KB, 836 KB), one a daily log. Nothing would ever have removed them.

## Decision

- The nightly reclaim also sweeps `knowledge/`, recursively, for exactly the staged
  shape (`STAGED_WRITE_NAME`: a dot, a name, a hex nonce of at least 16 digits,
  `.tmp`) older than an hour — the margin the existing sweep already uses. A
  person's own `.notes.tmp` does not match and is kept. The walk is bounded.
- Two writers used an 8-digit nonce (`code_hints`, `operational_ownership`); they
  now use 16, so every staging name in `scripts/` has one shape.
- Guard: `tests/test_a_killed_write_leaves_nothing_behind.py` renders every
  f-string in `scripts/` that names a `.tmp` file and requires the sweep to
  recognise it, so a new writer with another shape fails the suite.

## Source

rename(2), Linux man-pages, fetched 2026-09-26 from
https://man7.org/linux/man-pages/man2/rename.2.html:

- "If newpath already exists, it will be atomically replaced, so that there is no
  point at which another process attempting to access newpath will find it
  missing."
- "However, there will probably be a window in which both oldpath and newpath
  refer to the file being renamed."

Conclusion (mine): the rename protects the target, not the staged file; a writer
killed before its rename leaves the staged name, and only a sweep that knows that
name's shape can collect it.

## Files

- `scripts/reclaim_runtime_state.py`
- `scripts/code_hints.py`
- `scripts/operational_ownership.py`
- `tests/test_a_killed_write_leaves_nothing_behind.py`
- `tests/test_a_failure_in_the_night_is_named.py`
