# An answer names the commit it read

Date: 2026-09-26. Audit 2026-09-26, finding C-9 (B-34 partial: `source_commit`).

## What was wrong

Fact, `scripts/mcp_contract.py` at a2965838: `build_envelope` always set
`source_commit` from `git rev-parse HEAD` run in the vault root. For
`get_architecture` or `find_dead_code` on another repository (`directory`), the
envelope named the vault's commit — a statement about a checkout the answer never
read. Measured: before the fix, the new test got the vault's HEAD for a fresh
repository whose HEAD was different (3 of 4 tests failed on the old code).

## Decision

- `build_envelope` takes `source_root`: the checkout a directory-scoped answer
  read. Its HEAD is the answer's commit; a root that is not absolute names none
  (and the envelope says "Source commit is unavailable.").
- `mcp_server._answer_source_root` finds the directory-scoped tools from their input
  schemas (a `directory` property), not from a list.
- The HEAD read runs with `-c core.fsmonitor=false`: any repository may be asked
  about, and its own config must not name a command we run (the class
  `tests/test_a_repository_read_runs_no_config_command.py` holds).
- Guard: `tests/test_an_answer_names_the_commit_it_read.py` runs every tool whose
  schema has `directory` and requires that directory's HEAD.

## Sources

- Git, `Documentation/revisions.adoc`, fetched 2026-09-26 from
  https://raw.githubusercontent.com/git/git/master/Documentation/revisions.adoc:
  "`HEAD` names the commit on which you based the changes in the working tree."
  Conclusion (mine): the commit of an answer is the HEAD of the working tree it
  read, not of the process's own checkout.
- Git, `Documentation/config/core.adoc`, fetched 2026-09-26 from
  https://raw.githubusercontent.com/git/git/master/Documentation/config/core.adoc:
  "core.fsmonitor:: If set to true, enable the built-in file system monitor daemon
  for this working directory".

## Files

- `scripts/mcp_contract.py`
- `scripts/mcp_server.py`
- `tests/test_an_answer_names_the_commit_it_read.py`
