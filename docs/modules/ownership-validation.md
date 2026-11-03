# Ownership input and marker validation

`llm_wiki.ownership_validation` owns the existing bounded-text/process/role input
checks and marker representation checks. It retains the role and marker-role sets,
marker byte bound, current errors, canonical JSON and SQLite-row decoding behavior.
A supplied identity/content comparison performs no file read or process observation.
Parsing a record does not establish current ownership or authority to remove a file.

Dependencies: [ownership values](ownership-contracts.md),
[runtime identity values](runtime-contracts.md), [restricted paths](restricted-paths.md),
[canonical formats](formats.md) and [process value helpers](process-liveness.md).
Only the existing integer predicate and identity-length constant are used from the
process module; probes are not called. All process checks, lease timing, clocks,
registry SQL, filesystem operations and heartbeats remain with the registry.

operational_ownership re-exports identical helpers/constants, retaining serialized
callable names. Aliases remain for registry and historical callers until a separately
qualified consumer migration. No duplicate implementation remains. Captured original
outcomes, actual SQLite rows and isolated imports qualify this packet; registry and
recovery integration remain separately visible obligations.

Source: [boundary research](../research/2026-11-03-ownership-validation-boundary.md).
