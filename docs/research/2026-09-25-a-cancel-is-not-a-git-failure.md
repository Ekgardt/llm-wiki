# A cancel is not a Git failure

Date: 2026-09-25. CI run 36197318516 (PR 43, commit 543fc591).

## Fact
- `workspace_revision.ignored_top_level_directories` (added for audit A-16) runs
  `git ls-files --ignored` and treats `(ValueError, OSError)` as "Git cannot answer;
  skip nothing". The revision's own stop signal, `_RevisionStopped`, is a
  `TimeoutError`, so a cancel or an expired deadline during that call was swallowed
  and the walk went on. On Windows the stop fell inside that call, and
  `test_revision_checks_cancellation_during_manifest_hashing` saw one more cancel
  check than it allowed (5 against 4).
- `mcp_server._request_repository_refresh` (audit B-38) passed
  `checkout.checkout_root`, a POSIX-spelled string, as the refresh argument; on
  Windows the spawned command read `C:/Users/...` where the rest of the code spells
  `C:\Users\...` (`test_a_structural_answer_names_its_commit...`).
- On Python 3.10, `ast.parse` accepts the 100 000-term expression that 3.11 and
  later reject with `RecursionError`; the A-14 test assumed the error on every
  version (checked locally with 3.10 to 3.14, 2026-09-25).

## Source (fetched 2026-09-25)
Python documentation, Built-in Exceptions, https://docs.python.org/3/library/exceptions.html:
"The following exceptions are subclasses of OSError, they get raised depending on
the system error code." `TimeoutError` is one of them, so `except OSError` catches
every `TimeoutError`, including a caller's stop.

## Decision
- `ignored_top_level_directories` re-raises `_RevisionStopped` before its broad
  handler; a Git failure still means "skip nothing".
- The refresh argument is `str(Path(checkout.checkout_root))`, the platform's own
  spelling.
- The deep-file test asks the interpreter whether the expression parses and expects
  a parse error only where it does not.

## Files
- scripts/workspace_revision.py
- scripts/mcp_server.py
- tests/test_workspace_revision.py
- tests/test_one_hard_python_file_does_not_freeze_the_index.py
