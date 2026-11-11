# Offline file fingerprints

`llm_wiki.runtime_fingerprints` owns the existing streamed two-pass fingerprint,
metadata stability comparison and deadline checks. It depends on runtime_records
for metadata/identity, runtime_contracts, bounded_io and sqlite_deadlines' pure
operation deadline check. It neither opens a database nor publishes files.

The result is (digest, RuntimeFileIdentity). Runtime fingerprints require owner-only
files; regular fingerprints retain their caller-selected permission requirement.
Use only for offline artifacts under caller-owned writer exclusion: closing a
file descriptor for a live SQLite inode can release its process's advisory locks.
Two hashes catch rewrites hidden by coarse/restored timestamps; this consistency
witness does not itself exclude noncooperating writers. The chunk constant bounds
memory, not accepted file size. Explicit byte budgets/deadlines stay unchanged.

Public callable pickle names and old reliable_memory aliases remain identical.
Run `uv run pytest tests/modules/runtime_fingerprints tests/test_migration_artifact_fingerprint.py -q`.

## Canonical ownership reference

Shared canonical contract: [runtime-contracts](runtime-contracts.md).
The complete guide is included in this owner's maintenance packet.


Path and descriptor baselines remain separate: bounded_io supplies the initial
cross-API timestamp comparison, descriptor ctime stays checked during both hash
passes, and the final path is checked against its original complete metadata.
Run tests/test_path_and_descriptor_timestamps.py with the migration tests; it
covers both public readers under synthetic Windows observations and refusal of
subsequent descriptor/identity changes. Native CI remains a separate gate.
