# Metadata validation without descriptor-based identity capture

Date: 2026-11-03. Internal boundary under the approved decomposition mandate.

The unchanged coordinator-validation budget check failed at105385tokens;124
behavior checks passed. Source inspection found that sqlite_io uses only
runtime_records.validate_operational_db_file, while the latter module imports
windows_workspace for separate capture_runtime_file_identity responsibilities.
Dropping Windows source from the manifest while that import remained would hide
context, not solve the dependency. Instead extract the actual metadata boundary.

Transfer _contained_runtime_metadata, _require_runtime_parent_containment,
_require_bounded_regular_file, _require_file_size_budget,
_require_nonnegative_file_budget, _validated_runtime_metadata and
validate_operational_db_file exactly to llm_wiki/runtime_metadata.py. Preserve
runtime_records aliases and historical callable identities. SQLite IO changes
only the import owner; all its function ASTs remain the same. No metadata checks,
error messages, path policy, size bounds or platform behavior change.

Runtime byte reading, descriptor confirmation and platform identity capture remain
in runtime_records. There is no generic filesystem framework, second validator,
new Windows ABI, lock implementation or source-closure exception. Full provider
sources remain in packets that actually use them; existing unique ownership of
Windows workspace is retained. Provider guides and histories are not deleted.

Fresh builtin filesystem snapshot: generation-18e490f1b5838a00-e1ead766, d601b063
plus pending coordinator-validation changes, including5untracked source files.
Architecture and relevant metadata/identity/read caller queries used no fallback;
direct source and patch searches supplement partial graph. Runtime/SQLite/real
adoption lock/stranded-journal baseline26passed. Move the original descriptor-
prohibition test with unchanged AST; test bounds, containment and forbidden kinds
against the original before transfer, then verify all old definitions and aliases.
Run actual two-process SQLite writer exclusion and recovery/backup consumers.

Three independent primary publishers checked2026-11-03:
- https://www.sqlite.org/lockingv3.html — current authoritative rollback-mode
  locking and hot-journal recovery description; the project retains rollback mode.
- https://man7.org/linux/man-pages/man2/fcntl_locking.2.html — closing any descriptor
  for a file releases process-associated record locks, regardless of which took
  them. Existing metadata-only DB validation deliberately avoids that close.
- https://docs.python.org/3.14/library/pathlib.html — concrete path inspection and
  resolution semantics; preserve the existing lstat/containment behavior and errors.

No live installation. Completion still requires all complete packets<=100000,
real measured CCN/shape and unchanged global closure/ownership checks.
