# An impact hears its cancel

Date: 2026-09-26. Audit 2026-09-26, finding C-9 ("impact ignores cancel during
git/graph mapping").

## What was wrong

Facts, `scripts/impact_analysis.py` at a2965838: `analyze_impact` took a
`cancelled` callable and checked it before the run and in the note scan only.
Every Git child ran under a `threading.Timer` that killed it at the deadline and
never looked at the cancel; `collect_git_changes`, `_changed_ranges`,
`_map_symbols`, `_project_file_ids` and `_affected_nodes` checked the deadline
only (`_check_impact_stop(deadline)`). A cancelled MCP call therefore held its
slot and kept Git running until the deadline. Measured: the three new tests fail
on the old code.

## Decision

- `_git` takes `cancelled`. A watcher thread polls the deadline and the cancel
  every `GIT_STOP_POLL_SECONDS` (0.1 s) and kills the child on either; the raised
  `TimeoutError` names which ("impact analysis cancelled" or the deadline).
- `cancelled` is passed through every stage: revision resolution, merge-base,
  diff, blob reads, range walk, symbol mapping, file-node lookup and reach.
- Guard: `tests/test_an_impact_hears_its_cancel.py` parses the module and fails
  on any `_check_impact_stop` call given a deadline alone.
- The Git children keep no `wait` without a bound: the watcher kills at the
  deadline, so the `wait()` after it returns. The capture fork's guard
  (`tests/test_every_git_call_has_a_deadline.py`, waiting calls only) passes on
  this module.

## Source

Python documentation, `subprocess`, fetched 2026-09-26 from
https://docs.python.org/3/library/subprocess.html:
"Popen.kill() Kills the child. On POSIX OSs the function sends SIGKILL to the
child. On Windows kill() is an alias for terminate()."

## Files

- `scripts/impact_analysis.py`
- `tests/test_an_impact_hears_its_cancel.py`
