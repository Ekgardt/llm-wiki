# Markdown transaction records

`llm_wiki.markdown_records` owns seven existing transaction records, ChangeKind,
fourteen failure classes and the bytes-only change-content guard. It depends on
[ownership values](ownership-contracts.md), which use [runtime identities](runtime-contracts.md).
The ownership value dependency is imported at runtime for native type resolution. No SQL, path checks, writer admission,
lease refresh, filesystem mutation, recovery or transaction transition occurs here.
A record is evidence data, not authorization. Frozen fields do not recursively freeze
nested mappings. The coordinator retains every authority and validation check.

markdown_transaction re-exports the identical classes and helper. Historical module
identities preserve existing serialized references; aliases remain until consumers
and persisted data have a separately qualified migration. There is one implementation.
Pre-transfer fixtures protect fields, defaults and error messages/dispositions.
Cross-owner pickle compatibility remains an integration obligation in
 tests/test_module_legacy_aliases.py, outside the isolated value tests.

Source: [boundary research](../research/2026-11-03-ownership-and-markdown-record-boundaries.md).

PreconditionChangedError also identifies a target snapshot lost during capture or
staging. It remains a ValueError subtype; the coordinator decides whether to retry.

## Approved annotation compatibility — 2026-11-04

Native Python 3.14 annotations resolve without loading historical facades. Fields,
defaults and pickle paths are retained; raw annotation/signature representation
changes as explicitly approved. Cold-process tests cover ordinary get_type_hints.
