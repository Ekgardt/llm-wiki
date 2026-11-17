# Canonical repository and checkout scope

`repository_scope.py` remains the stdlib-only owner of repository/checkout identity,
closed `repository-scope/v1` records and bounded read-only Git inspection. The
production file and public import path remain unchanged. A linked worktree shares
repository identity while retaining its own checkout identity. Commit is provenance,
not one of the fields that determines identity.

Serialized paths are validated using their own platform shape, not the host's
path semantics. Identity values must be derived from canonical paths. Probe failure
in a directory with a Git marker must not silently produce a non-Git identity.
Unborn repositories are distinguished from failed HEAD probes using the existing
bounded evidence. Hostile inherited Git environment settings are sanitized;
commands remain noninteractive and honor caller cancellation/deadline. Oversized
output or timeout terminates and closes the probe using the existing ownership.

The packet includes the complete focused scope tests and hermetic Git fixture.
The unchanged scope/index timeout-consistency test lives with the cross-owner
identity guards. The source-scanning guard and indexing/navigation/retention tests
remain integration obligations; they are not discarded or declared pure scope
unit tests. No new Git write, cache, database, bound or subprocess is introduced.

Run `uv run pytest tests/test_repository_scope.py tests/modules/repository_scope -q`.
See `../research/2026-11-03-redaction-and-repository-scope-boundaries.md`.

## Canonical ownership reference

`secret_redact.py` and `repository_scope.py` are independent stdlib-only owners
qualified in place. Redaction owns best-effort text/structure masking; scope owns
canonical repository/checkout identity and bounded Git inspection. Their module
packets retain focused tests; provider timeout and scope/index consistency checks
remain active under their respective integration test owners. See
`research/2026-11-03-redaction-and-repository-scope-boundaries.md`.
The redactor's pure tests embedded in audit/security/structure suites move into
`tests/modules/secret_redact/` unchanged. DLP/cache integration remains in
`tests/test_redaction_policy_cache_contract.py`; the compile-packing comparison
imports the single original-pattern test helper from the redactor's test owner.
