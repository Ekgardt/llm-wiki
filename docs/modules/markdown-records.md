# Markdown transaction records

`llm_wiki.markdown_records` owns seven existing transaction records, ChangeKind,
fourteen failure classes and the bytes-only change-content guard. It depends on
[ownership values](ownership-contracts.md), which use [runtime identities](runtime-contracts.md).
The ownership annotation import is type-only. No SQL, path checks, writer admission,
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
