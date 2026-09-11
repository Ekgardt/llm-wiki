# A no-follow walk is a sequence of named steps

Date: 2026-09-11. Trigger: audit finding OPS-15 (Rule 5 debt), file
`scripts/lsp_security.py`, stage 1 of 3: the repository containment walk
(`_access_posix` CCN 19, `_access_windows` 25, the two `_revalidate_*`
8, `_windows_entries` 7, `_prove_windows_component_missing` 8,
`read_repository_source_bytes` 10, the two `_read_*_source_handle` 7,
`resolve_repository_source` 6, `_OwnedHandles.__exit__` 6,
`_fits_utf8_redaction_ceiling` 7).

## Sources

1. `knowledge/notes/lsp-process-containment-decision.md` and the
   read-only LSP decision: the walk opens every component without
   following links, records each step's identity, re-walks after a
   resolution barrier and again after the read; a component that was
   missing must still be missing afterwards. None of that order may move.
2. `tests/test_lsp_security.py`: 200+ tests assert the exact
   `PathContainmentError` messages for each refusal; the helper names
   the tests reach (`_OwnedHandles.own`) are kept.
3. Rule 5's remedy: a state the loop threads through (`current`,
   `steps`, `missing`, `final`) becomes one small object; each refusal
   becomes a named check.

## Decision

Behaviour, order and messages unchanged. The walk state is a
`_PosixWalk` / `_WindowsWalk` object with `walk(parts)`; the checks
after the barrier (`still missing`, `parent probe`, `existing final`,
`read`) are module functions named for what they prove. Platform
dispatch and the containment `try/except` live in `_platform_access`
and `_contained`, which keep the one difference between the two public
entry points: a read lets `TimeoutError` through, a resolve does not.

Files: `scripts/lsp_security.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-operations-and-reliability.md`.
