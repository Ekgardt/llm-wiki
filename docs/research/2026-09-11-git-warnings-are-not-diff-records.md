# Git warnings are not diff records

Date: 2026-09-11. Trigger: PR30 runs 34535006773, 34540064380, 34545288929
and 34550352312 on Windows: `test_impact_reaches_the_code_symbols_behind_a_dirty_change`
answered `changes: []`. The diagnostic assertion added on 2026-09-11
finally said why: `warnings=['malformed zero-delimited Git diff record']`
with `autocrlf='true'`. `impact_analysis._git` runs `git diff --raw -z`
with `stderr=subprocess.STDOUT`, so Git's advisory line — on a Windows
runner `warning: in the working copy of 'pkg/core.py', LF will be replaced
by CRLF the next time Git touches it` — lands in front of the first `:`
record, the parser refuses the whole stream, and every impact answer on
such a checkout is empty and marked partial. Not a test problem: a real
Windows user with `core.autocrlf=true` and an LF file gets no impact
analysis at all.

## Sources

1. Git `core.autocrlf` documentation: with `true`, Git warns on stderr when
   a working-tree file's line endings will change; the warning is advisory
   and the command's exit status stays 0.
   https://git-scm.com/docs/git-config#Documentation/git-config.txt-coreautocrlf
2. Python `subprocess` reference: reading one pipe to the end while the
   other fills can block; a temporary file for stderr has no such limit.
   https://docs.python.org/3/library/subprocess.html#popen-objects
3. `impact_analysis._parse_raw_records`: the `-z` stream is `:` records
   separated by NUL; anything else is malformed by design, and that design
   is right — the fault was feeding it stderr.

## Decision

`_git` keeps stdout and stderr apart: stdout is read to the ceiling as
before; stderr goes to an anonymous temporary file whose head is quoted
only when Git exits non-zero. A test feeds a warning line ahead of a record
and expects the record to parse. The function is split into steps under
CCN 5 while it is open.

Files: `scripts/impact_analysis.py`, `tests/test_impact_analysis.py`,
`tests/test_query_surface.py`, `CHANGELOG.md`, `docs/ISSUES-2026-09-10.md`.
