# A hook budget is one sum

Date: 2026-09-26. Audit 2026-09-26 C-1 (B-11 of 2026-09-25 incomplete).

## Facts

- `integration_adapter.DELEGATE_TIMEOUTS` stopped the prompt delegate at 2.5 s and
  the tool delegate at 3.5 s, while their append could try for
  `BREADCRUMB_APPEND_BUDGET_SECONDS` = 3.0 s after the interpreter had started. The
  prompt delegate was killed before its own writer gave up and said why.
- The shipped prompt and tool hooks set `timeout: 5`. Claude Code's hooks reference
  (https://code.claude.com/docs/en/hooks, fetched 2026-09-26) on `timeout`:
  "Seconds before canceling."

## Decision

- `BREADCRUMB_APPEND_BUDGET_SECONDS` is 2.5 s; both delegate timeouts are that
  budget plus `DELEGATE_STARTUP_SECONDS` (1.0 s), computed, so they cannot drift
  apart again. 3.5 s leaves the adapter room under the host's 5 s, and a test
  holds budget < delegate timeout < host limit.

## Files

- `scripts/integration_adapter.py`
- `scripts/daily_log_append.py`
- `tests/test_a_hook_budget_is_one_sum.py`
- `CHANGELOG.md`
