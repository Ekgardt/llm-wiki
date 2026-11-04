# Operational ownership values

`llm_wiki.ownership_contracts` owns the existing role/process-state types,
ProcessIdentity, OwnerLease, MarkerIdentity and OperationalOwnershipError.
It depends only on the [runtime identity values](runtime-contracts.md).
No lease is acquired, refreshed, validated or released here. Constructing a lease
value does not prove admission; the canonical registry retains that responsibility.
The existing frozen records preserve all fields and defaults without adding validation.

operational_ownership re-exports the identical objects. Their historical module
identity remains for serialization. These aliases stay until consumer and persisted
pickle compatibility can be separately retired; there is no duplicate implementation.
Tests compare pre-transfer field and error fixtures. Cross-owner serialization tests
remain in tests/test_module_legacy_aliases.py and are an integration obligation.

Source: [boundary research](../research/2026-11-03-ownership-and-markdown-record-boundaries.md).

## Approved annotation compatibility — 2026-11-04

Native Python 3.14 annotations resolve without loading historical facades. Fields,
defaults and pickle paths are retained; raw annotation/signature representation
changes as explicitly approved. Cold-process tests cover ordinary get_type_hints.
