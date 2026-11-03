# Filesystem ancestry policy

`llm_wiki.path_ancestry` owns three shared predicates, with only standard-library
dependencies. `_acceptable_ancestor` rejects reparse attributes, accepts ordinary
directories, and delegates symlink policy to `_system_symlink`. On POSIX that
policy requires a root-owned link and a root-owned parent without group/other
write access; on Windows it refuses. Metadata errors retain their existing
propagation or refusal at the respective layer.

This is the existing policy, not an atomic traversal sandbox. Callers still own
containment, file descriptors, no-follow opens, races and identity validation.
The owner neither creates nor modifies files. `bounded_io` retains identity
aliases for existing callers; the implementation occurs only here. Remove those
aliases only when no supported imports or probes require them.

Own tests cover real directories/files, missing ancestors, root ownership and
writable-parent metadata, reparse rejection and symlink handling. Reader and
SQLite probe integration tests exercise the policy at both actual consumers.
The complete context includes those predicates, own tests, fixtures, instructions
and the design record; no range-reader implementation is needed by this owner.

Run `uv run pytest tests/modules/path_ancestry -q -s`.
Source: ../research/2026-11-03-path-ancestry-boundary.md.

## Canonical ownership reference

`llm_wiki.path_ancestry` owns the shared filesystem ancestry predicates used by
bounded reads and SQLite lock probes: real-directory/reparse checks and the
existing administrator-owned symlink exception. It imports only the standard
library and performs no mutation. `bounded_io` keeps identity aliases for existing
callers; neither reader nor SQLite imports the other's implementation for this
policy. This predicate is not an atomic traversal sandbox. Caller no-follow and
identity checks remain mandatory. See
`research/2026-11-03-path-ancestry-boundary.md`.

Cross-owner alias/pickle checks live in `tests/test_module_legacy_aliases.py`,
an integration obligation outside this pure owner packet. Their assertions are
preserved; the packet guard now follows imports from test/support files too.
