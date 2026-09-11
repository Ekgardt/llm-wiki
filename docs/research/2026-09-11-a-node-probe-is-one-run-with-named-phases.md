# A Node probe is one run with named phases

Date: 2026-09-11. Trigger: audit finding OPS-15 (Rule 5 debt), file
`scripts/pyright_profile.py`: 22 functions over the gate, among them
`_probe_node` (CCN 43, 153 lines: locate, spawn, observe, terminate,
release and grade in one body with three nested `try`s),
`_normalize_jsonc` (30, two hand-written scanners), `_system_candidate_server`
(23), `_lockfile_codes`, `_managed_manifest`, `_node_executable_is_safe`
(14 each), `_validate_canonical_domain` (13).

## Sources

1. `knowledge/notes/read-only-lsp-navigation-engine-decision.md`: the
   probe never leaves a Node process behind — a tree that cannot be
   ended is retained with its owner and retried under one shared cleanup
   budget; the degradation codes name what happened.
2. `tests/test_pyright_profile.py` (3 300 lines): every degradation code,
   every candidate-precedence outcome and every retained-owner path these
   assert is kept verbatim; the names the tests patch (`_node_environment`,
   `_probe_node`, `_terminate_node_probe_tree`, `_release_node_probe_tree`,
   `_retry_node_probe_cleanups`, `subprocess`, `ProcessTree`) are kept.
3. Rule 5's remedies: a run with a dozen locals becomes one object with
   phases; a two-pass scanner becomes two small classes; a ladder of
   "return None, mismatch, False" becomes one function per shape.

## Decision

Behaviour and codes unchanged. `_probe_node` is: locate Node, open the
probe window, reserve an owner, spawn, then `_ProbeRun.observe()` →
`settle()` → `result()`; the error path (`terminate_on_error`) and the
`finally` release keep the original order. JSONC normalisation is a
comment stripper and a trailing-comma pass. The system-candidate shapes
(`node_modules` server, `.cmd` shim, symlinked launcher) are one function
each. One `_mismatch()` returns a fresh set because callers add to it.

Files: `scripts/pyright_profile.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-operations-and-reliability.md`.
