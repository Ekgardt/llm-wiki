# Ownership lease-time policy

`llm_wiki.ownership_timing` owns five existing pure helpers and the existing
long-lease role set. It normalizes aware datetimes, reads persisted timestamps,
emits fixed-width UTC microseconds, selects existing role durations and rejects
supplied lease durations inconsistent with that role.

The ISO reader's naive-as-UTC policy is deliberately not used here: ownership
continues to reject unzoned values. Reading historic whole seconds and writing six
fractional digits remain compatible. Existing 120/40 and 30/10 second duration
pairs are retained. Role admission and epoch validation remain with the registry.

There is no current-time read, SQL, process observation, file IO, heartbeat or
ownership mutation. The registry retains those responsibilities. Full ISO-time,
ownership-contract and runtime-contract sources and guides are included.

Old helpers remain identical re-exports with historical serialized identities.
Aliases are retained for current callers until a qualified consumer migration;
there is one implementation. Tests exercise the original code before transfer,
preserve the original roundtrip regression, and retain actual expiry and registry
integration separately. This packet does not qualify the larger registry closure.

See [research](../research/2026-11-03-ownership-timing-boundary.md) and
`ownership-timing-context.json` for the complete bounded maintenance context.

## Structural reference retained from the overview

Ownership lease-time values are isolated in `scripts/llm_wiki/ownership_timing.py`:
UTC normalization, persisted timestamp reading/writing, existing role durations and
supplied-lease timing validation. The registry retains current-time reads, epochs,
SQL and heartbeat execution. Historical helpers remain identical aliases; see
[ownership timing](ownership-timing.md) and its complete context manifest.
