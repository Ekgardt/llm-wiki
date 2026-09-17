# A helper that says it never fails does not

Dated 2026-09-17. Finding C-F9 of the third audit (low-medium, reproduced). The research before
the fix.

## What was found

- `daily_log_append.py` and `tool_breadcrumb_append.py` are the two command-line helpers the
  OpenCode plugin calls. Both docstrings say "Never fails — always exits 0".
- Both catch `OSError` only. The writer they call also raises `ValueError` (a target that is
  too large, an operation bound elsewhere), `RuntimeError` (a transaction failure, drift) and
  ownership errors. Reproduced by the audit with `knowledge/daily` replaced by a file: a
  traceback and exit code 1.
- The caller lives outside this repository and was written against the docstring, so an exit
  code it was told never to expect is what it gets. And the failure leaves nothing in the
  capture-failure trail, unlike every other hook in the product.

## Practice on this date

- A command whose contract is an exit status owns the whole boundary: "The exit status of an
  executed command is the value returned by the waitpid system call or equivalent function"
  ([Bash manual, Exit Status](https://www.gnu.org/software/bash/manual/html_node/Exit-Status.html)),
  and an uncaught Python exception makes that value 1 whatever the docstring says.
- The rule the rest of the product follows at such a boundary: catch everything there, once,
  and write the reason down (`integration_adapter.main`, `user_prompt_capture.main`,
  `post_tool_capture.main`). A broad catch is a defect when it hides a failure; at a
  never-fail boundary that records it, it is the contract.

## The decision

- Both helpers catch every exception at `main`, record it through
  `capture_diagnostics.record_capture_failure` (kinds `opencode_daily_append` and
  `opencode_tool_breadcrumb`), print the one-line reason to stderr as before, and exit 0.

Files: `scripts/daily_log_append.py`, `scripts/tool_breadcrumb_append.py`,
`tests/test_a_helper_that_never_fails_does_not.py`
