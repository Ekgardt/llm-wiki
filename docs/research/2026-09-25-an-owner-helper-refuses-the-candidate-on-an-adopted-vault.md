# An owner helper refuses the candidate database on an adopted vault

Date: 2026-09-25. Audit item C-14 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- `OwnershipRegistry(state_root)` opens `run/markdown-transactions-v3.candidate.sqlite3`,
  the migration candidate. An adopted vault has no such file: its registry is
  opened with `OwnershipRegistry._from_adopted_database` on the adopted
  coordinator, which the coordinator, the queue and the project journal each do
  (`project_journal._ownership_registry` says so in its docstring).
- The module-level helpers `acquire_compile_owner`, `acquire_scheduled_owner`,
  `heartbeat_owner`, `current_owner_lease` and `release_marker_owner` fall back to
  `OwnershipRegistry(state_root)` when no registry is passed. The nightly and
  weekly passes pass theirs; any other caller on an adopted vault would open a
  database that is not the vault's, or fail validating a missing file with an
  unrelated error.

## Source

- OWASP, "Fail securely", https://community.owasp.org/Fail_securely (fetched
  2026-09-25): "design your security mechanism so that a failure will follow the
  same execution path as disallowing the operation".

## Decision

- The helpers build their default registry through one function that refuses
  with `adopted_registry_required` when the adoption records are present, and
  keeps today's candidate registry before adoption. Callers that pass a registry
  are unchanged.

## Files

- `scripts/operational_ownership.py`
- `tests/test_an_owner_helper_refuses_the_candidate_on_an_adopted_vault.py`
- `CHANGELOG.md`
