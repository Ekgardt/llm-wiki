# Explicit file ownership and an honest completion gate

2026-11-03; approved whole-project completion plan; baseline1a8348f2.
The current index has3082 tracked regular files (3081 mode100644, one100755).
45 maintenance packets exist, but none declares unique file ownership. Context
membership is deliberately overlapping and therefore cannot answer completion.

Add an ownership object to each context manifest with explicit files and owned
directories. It is distinct from files/directories used as dependency context.
An owned file must be in that owner's measured context, tracked, present and
contained. A manifest owns itself. Two owners may include the same context but
must never claim the same file. Do not infer ownership at runtime from slugs,
imports or shared context. Initial assignments are reviewed below.

A test-only tests/module_ownership.py builds the registry from those declarations
against git ls-files -z. It emits deterministic owned/unassigned coverage. The
normal report reports reality; --check-complete fails while any tracked file is
unassigned. Validation of duplicate, untracked, missing or outside-context claims
always fails. No catch-all owner, excluded-file list or truncated unassigned list
is introduced. Shared scaffolding, docs, integration tests, benchmarks, lockfile,
installers and every other tracked file remain visible until deliberately owned.

Initial assignments: each existing owner gets its actual sole source file (or
formats source directory), own test directory, own guide and manifest. Existing
focused root test files declared for that owner also belong to it. The busy-lease
test belongs to lease-renewal, although queue-heartbeat includes it as context.
The cross-runtime pickle integration file remains unassigned pending its own
integration ownership; being included by runtime-contracts is not unique ownership.
Context-tooling owns its measurement/import-boundary helpers and focused tests.
Historical/shared research and global instruction/bootstrap files are not quietly
assigned to every leaf. Directory expansion is checked against tracked paths;
staging new product files is required before claiming a complete tracked snapshot.

Research checked today from three independent primary publishers:
- Git documents -z as verbatim NUL-delimited index names and the cached index
  selection: https://git-scm.com/docs/git-ls-files
- Python pathlib documents path resolution/containment operations:
  https://docs.python.org/3.10/library/pathlib.html
- Import Linter distinguishes protected ownership boundaries from consumer
  allowlists: https://import-linter.readthedocs.io/en/stable/contract_types/protected/
No new external library or supported Python version is added.

Choose declarations next to the already measured packet and a derived full
registry. Alternatives: one giant duplicate global path map (drifts), implicit
ownership from context (duplicates), manual file totals (no reproducible gate), or
marking unresolved files out of scope (contradicts whole-project completion).
The report may remain incomplete while decomposition progresses; that is not a
successful completion check. An integration test must prove the actual checkout
still returns incomplete while uncovered files remain. Synthetic tests prove
complete, duplicate, missing, untracked and outside-context cases independently.

Initial implementation passed12 synthetic cases and correctly refused the real
registry because declarations did not yet exist. After the explicit assignments,
13 cases passed and the real completion gate returned1:349 owned files out of3085,
2736 unassigned. These are tracked files, not token coverage. A further negative
test demonstrated that Git run in an empty subdirectory could otherwise report
an empty inventory. The tool now requires git rev-parse --show-prefix to be empty
before ls-files, refusing a narrowed project view. The regression failed before
the correction. A positive CLI test also verifies the gate accepts an actually
complete synthetic repository. No uncovered class of files is hidden.

Canonical-structure review also corrected stale earlier prose: bounded_io now
has the explicit path_ancestry dependency; sqlite_io/runtime owners replaced the
old facade's connection/filesystem implementations. The Windows workspace packet
is qualified, but the LSP process-tree packet is still over budget and unqualified.
The old combined wording could imply both were qualified and has been corrected.

Final checks:15 registry tests passed; Python3.10 registry plus its owning packet
16 passed; complete module/bootstrap/dependency/ownership/boundary/complexity,
structure/CI/privacy/limit/weight run1012 passed/2 skipped. Ruff and local CCN/shape
pass. All45 packet artifacts remain within the unchanged token ceiling and decode
back to every declared path/byte. Coverage is still349/3085, not completion.
