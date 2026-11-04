# Compile event records

`llm_wiki.compile_event_records` owns the existing frozen CompileEvent value,
ProgressConflict/ProgressCorrupt/SourceOutcomeConflict exceptions, stage/outcome
sets, SHA-256 syntax, canonical event JSON, event digest, lossless snapshot,
artifact reference validation and retained model-call phase validation. It uses
only Python 3.10 standard-library code. It performs no IO and imports no storage,
queue, compiler, provider or SQLite module.

Event field order and generated dataclass behavior are unchanged. Validation
requires nonnegative integer sequence (excluding bool), known stage/outcome,
nonempty textual task identity, object details and lowercase SHA-256 identities.
Canonical JSON sorts keys, uses compact separators, preserves Unicode and refuses
non-finite numbers. Snapshotting round-trips through JSON and refuses lossy values.
A frozen dataclass does not make nested details immutable; the snapshot is the
existing isolation boundary. No new semantic transition validation is introduced.

Artifact identity accepts only `run/queue-results/<64 lowercase hex>.result`
plus its SHA-256 digest; malformed data raises the existing corruption exception.
Retained model calls require an object with dispatch/answer phase. Other missing
fields retain their original error behavior; this extraction does not harden or
normalize it silently. Storage controls SQL append/immutability, previous-event
checks, transaction ownership, source overlap and audit deadlines.

`compile_progress` keeps the same exported objects and helper names. Classes keep
historical `compile_progress` module identity, so old pickle paths remain valid.
Those aliases support real consumers and persisted Python representations; remove
only after an explicit compatibility migration. Value-only consumers import this
owner directly. Pickle/facade integration checks are a separate whole-project
obligation; a unit import must not load the storage facade just to validate data.

Run `pytest tests/modules/compile_event_records`, original compile-progress journal
tests and affected answer/call/controller integration. Compare old JSON/hash/pickle
fixtures and unchanged storage ASTs. The packet retains necessary historical
context through the exact-record mechanism; it does not summarize away evidence.
Source: [boundary research](../research/2026-11-03-compile-event-records-boundary.md).

## Approved annotation compatibility — 2026-11-04

Native Python 3.14 annotations resolve without loading historical facades. Fields,
defaults and pickle paths are retained; raw annotation/signature representation
changes as explicitly approved. Cold-process tests cover ordinary get_type_hints.
