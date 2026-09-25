# A broken install is a named degradation

Date: 2026-09-25. Audit item B-39 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `pyright_session.PyrightSession._validated_qualified_paths` checks the installed identity and
  executables and raises `TypeError`/`ValueError` when they are missing or inconsistent.
- `_require_degrading_launch_error` treats those two as the caller's errors: it re-arms the start
  (`_allow_startup_retry`, no backoff) and re-raises, recording no degradation. So a broken install
  launched again on every query, and nothing said why. A test pinned exactly that (two `TypeError`s
  in a row).
- `_startup_is_retryable` already classes identity failures as terminal: "the same install will
  fail the same way".

## Source

- The module's own retry rule cites Microsoft's retry guidance; for this change: Microsoft Azure
  Architecture Center, "Compensating Transaction pattern" (fetched 2026-09-25 for B-31): "Sometimes
  manual intervention is the only way to recover from a failed step. In these situations, the
  system should raise an alert that includes detailed information about the reason for the
  failure." A broken install needs the operator; the session must say so, not retry.

## Decision

- An unusable install raises `_BootstrapDegradation("<profile>_install_invalid")`, which the
  launch path records as the session's degradation code; it is not retryable, so the session stops
  launching and answers degraded with that code. The pinned test now asserts that.

## Files

- `scripts/pyright_session.py`
- `tests/test_pyright_session.py`
- `CHANGELOG.md`
