# Relative path policy and SQLite connection policy

2026-11-03; approved completion mandate; baseline 8779ad5a.

llm_wiki.restricted_paths owns six unchanged functions: _is_posix_relative_text,
_require_posix_relative_text, _require_normalized_relative, _under_root,
_require_inside_a_root, restricted_relative_path. This is lexical POSIX path
validation and caller-supplied allowed-root membership, not symlink containment or
an OS sandbox. Actual filesystem identity/permissions remain with IO owners.
It depends only on re and PurePosixPath. The stricter runtime path policy is not
merged with context-bundle metadata names, which are only in-memory keys.

llm_wiki.sqlite_pragmas owns six unchanged functions: _apply_operational_pragmas,
_pragma_integer, _require_pragma, _validate_or_initialize_operational_contract,
_initialized_contract_identity, _apply_readonly_operational_pragmas. It depends
only on sqlite3 and sqlite_contracts. Caller-owned connections retain their exact
row factory, busy timeout, rollback journal, synchronous, foreign-key, trusted-schema
and query-only behavior. Readback/refusal order is preserved. Header initialization
retains the same (0, 0) condition and conflict refusal; caller admission policy is
unchanged. Opening/closing, filesystem identity, permissions and deadline setup
remain in reliable_memory. No new transaction or connection ownership.

Consumers switch to direct owner imports at the same lexical scope, including
evidence_resolver's local import. reliable_memory keeps identity aliases and the
public restricted_relative_path pickle identity. No second implementation remains.
Old names are retained until supported consumers no longer use those APIs. The two
existing pure path tests move unchanged from mixed IO tests; real-file pragma and
migration integration tests remain. Leftover blank runs and the orphan schema-table
comment left by earlier extractions are removed without changing executable AST.

Research checked 2026-11-03, three independent primary publishers:
- https://docs.python.org/3.10/library/pathlib.html — pure lexical paths versus IO;
  preserve the existing project Python 3.10 contract despite upstream EOL.
- https://www.sqlite.org/pragma.html — unknown pragmas can be ignored, so retain
  exact readback checks; do not assume setting a value proves enforcement.
- https://martinfowler.com/bliki/BoundedContext.html — responsibility boundaries.
Alternatives: combine lexical and filesystem policy, introduce a connection-owning
repository class, or change path normalization. Each broadens behavior or ownership;
exact extraction is sufficient. No new dependencies, limits, paths or commands.

Before code, refresh built-in graph and inspect callers plus real consumers,
configuration and tests. Direct search found no monkeypatch of the moved helpers.
Graph gaps remain supplemented by source inspection; evidence is stored in
logs/path-pragmas-graph.json and logs/path-pragmas-consumers.json.

## Qualification

Fresh built-in index: 8779ad5a, generation-18e461142c3dbd16-46fb878c. Reverse
queries found 16 path-validation, two pragma-application and four header-validation
callers; source inspection supplemented incomplete graph edges. Baseline related
tests: 166 passed, 3 skipped. Boundary guards first failed six checks. All twelve
moved function ASTs and both moved pure tests match exactly; eight remaining
source/test files match after excluding imports and moved definitions. Historical
public pickle bytes are unchanged. Initial own/related run: 190 passed, 3 skipped.
Expanded queue, transaction/recovery, generation, evidence, ownership and global
guards: 648 passed, 12 skipped. Additional claim/journal/weight checks: 41 passed.
Python 3.10: 129 passed, 3 skipped. Measured test files: 82 passed, 3 skipped.
Current contract documentation was corrected to name the new pragma, migration
and deadline owners instead of retaining the earlier filesystem-owner wording.
All thirty actual context artifacts decode to every original path and byte and
fit the serialized limit; counts/hashes are in logs/context-bundle-comparison.json.
Ruff and diff checks passed. Full pinned 446bdafa regression is still separate;
no full-current, native Windows/macOS or live-installation claim.
