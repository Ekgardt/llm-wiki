# Redaction and repository scope remain independent owners

2026-11-03, completion mandate; baseline 717160fc. Qualify secret_redact and
repository_scope in place, preserving production files byte for byte. Both have
only standard-library imports. Their public paths and consumers stay unchanged.

secret_redact owns best-effort string/structure secret masking, stable redaction
markers and scoped memoization. It is not the fail-closed model DLP policy or a
complete credential scanner. Include all six directly focused test files: the
main redactor suite, JSON credentials, command flags, credential-key suffixes,
indexed assignment candidates and missing secret shapes. The unrelated timeout
environment test currently in test_secret_redact moves unchanged to the existing
provider-timeout suite; its assertions stay active in normal discovery.

repository_scope owns canonical repository/worktree identity, closed serialized
scope validation and bounded, sanitized Git probes. Commit provenance is not
checkout identity. Include its complete focused test suite. Its sole index-budget
consistency test moves unchanged to test_repository_scope_identity_guard, where
cross-owner identity checks already live. The global source-scanning guard remains
a cross-module integration obligation, not a pure scope unit test. No test is
removed, weakened or replaced. Preserve moved function ASTs and all other ASTs.

Research checked 2026-11-03, independent primary publishers:
- Python 3.10 re/pathlib/subprocess: text semantics, host-independent pure paths,
  controlled process invocation. No new dependency/version; 3.10 compatibility
  remains required even though upstream now marks that version unsupported.
  https://docs.python.org/3.10/library/re.html
  https://docs.python.org/3.10/library/pathlib.html
  https://docs.python.org/3.10/library/subprocess.html
- OWASP Logging Cheat Sheet: sensitive credentials should not reach logs. This
  supports keeping shared masking at ingress; it does not prove any finite
  pattern set catches every secret.
  https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html
- Git worktree manual: linked worktrees share repository administration while
  retaining checkout-specific state. Preserve separate repository/checkout IDs.
  https://git-scm.com/docs/git-worktree
- Fowler Bounded Context: explicit ownership and relations, not arbitrary folders.
  https://martinfowler.com/bliki/BoundedContext.html

Alternatives: moving sources adds aliases without removing any dependency;
merging these unrelated owners conflates security text handling and repository
identity; excluding mixed tests loses checks. Selected approach retains code,
assigns existing tests to their actual responsibility, and adds import isolation
and complete-context budget guards. Runtime/DLP policy and Git write behavior do
not change. No new limit or automatic Git operation is introduced.

The dependency audit also finds pure redactor tests inside mixed integration
files. Move their original bodies (including fixtures/decorators) into the
redactor's test directory: the TestRedactionBeforePersistence class, the bearer
assertion from audit fixes, JSONL/structure tests and the non-string-key test.
The pattern-prerequisite file's pure portion becomes test_pattern_prerequisites;
its compile-packing integration retains one import of the original-pattern helper,
not a copied helper. Pure line/command cache suites stay at their existing paths;
their three DLP-policy integration tests move unchanged to
 tests/test_redaction_policy_cache_contract.py. No test is dropped and the new
integration file is measured and remains in ordinary pytest discovery. These
changes clarify test ownership; they do not change production code or contracts.

Baseline initial selected suites: 161 passed. Fresh graph generation
 generation-18e4637187c238c2-a8a9e4b3; direct AST/import inspection supplements
incomplete call edges. Evidence: logs/redaction-scope-*.

## Qualification

Twenty top-level definitions/fixture assignments were relocated. Counter comparison
of every original top-level body/fixture across all affected files is identical
(excluding imports and module docstrings); each moved node's AST is identical.
Both production sources are byte-identical to 717160fc. The single shared test
helper is imported, not duplicated. Original tests keep names and assertions.

First related run: 193 passed. Measured weights: 213 passed, one existing JUnit
record_property warning from the compile-packing measurement. Additional pure
redaction, structure, CI, privacy, limit and whole-code complexity guards:
234 passed, one platform skip. Python 3.10: 113 passed. Ruff and local CCN/shape
passed. All 35 complete serialized artifacts decode to exactly their paths and
bytes and fit 100000 tokens. The whole-project e91402e0 run remains in progress;
these test ownership changes are later and are not claimed covered by it yet.
