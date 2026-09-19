# A slow read is not a corrupt install

Date: 2026-09-17. Audit 3, code intelligence, finding B7.

Files: `scripts/lsp_identity.py`,
`tests/test_a_slow_read_is_not_a_corrupt_install.py`

## What was found

`lsp_identity._digest_of` and `lsp_identity._manifest_codes` read a managed
server file and its install manifest through `bounded_io.read_stable_bytes` with
the caller's deadline, and map `OSError` to `server_unreadable` /
`manifest_unreadable`. `read_stable_bytes` raises `TimeoutError` when the
deadline passes (`bounded_io._check_deadline`), and `TimeoutError` is an
`OSError`. So a deadline that expires in the middle of a read is reported as a
damaged installation: the session is built unqualified, and the operator is told
to reinstall a server that is fine. Every other step of the same discovery
(`_check_deadline` in `discover_managed_server`, `discover_pyright`) lets the
timeout out as a timeout.

## Sources

- Python documentation, built-in exceptions
  (https://docs.python.org/3/library/exceptions.html#TimeoutError):
  `TimeoutError` is listed under `OSError` in the exception hierarchy
  ("OSError ... +-- TimeoutError"), so `except OSError` catches it.
- The repository's own rule, stated twice in this audit round
  (`impact_analysis._active_graph`, `path_coverage.contained_scope`):
  `except TimeoutError: raise` goes before any `OSError` handler.

## Decision

Both readers re-raise `TimeoutError` before mapping the remaining `OSError`s.
The caller's deadline ends the call as a timeout, which the navigation facade
already turns into a structural fallback for this one question instead of a
standing "unreadable install" verdict.
