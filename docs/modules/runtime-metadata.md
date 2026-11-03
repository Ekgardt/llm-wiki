# Runtime metadata validation

`llm_wiki.runtime_metadata` owns seven existing helpers for parent containment,
regular-file/reparse checks, optional size budgets, owner-only admission and
metadata-only operational database validation. Algorithms and errors are unchanged.

It opens/closes no second descriptor and reads no file contents. This matters for
process-associated POSIX SQLite locks, which can be released by an unrelated close
on the same file. Actual database opening, identity bracketing and journal recovery
remain in SQLite IO. Byte reads and platform file identity capture remain in
runtime_records; Windows workspace implementation remains owned by its packet.

SQLite IO now imports the metadata validator directly. Runtime_records and the
reliable_memory facade retain identical aliases and historical serialized names
until a qualified consumer migration; there is one implementation. Metadata checks
do not independently establish stable descriptor identity or authorize a mutation.

The complete context includes runtime-environment permission/platform providers.
The original no-descriptor test moved unchanged; containment, file kind and budget
tests first ran against the old implementation. Real two-process SQLite exclusion
and stranded-journal integrations remain required outside this leaf packet.

See [research](../research/2026-11-03-runtime-metadata-boundary.md).

## Windows fixture binding regression

The complete 37248b32 regression found one platform simulation still patching
`runtime_records._windows_reparse_point`, after metadata validation had moved.
The fixture now patches the lookup in `runtime_metadata`, which actually executes
the unchanged refusal. Its real directory-link input and PermissionError assertion
remain unchanged. Include
`tests/test_platform_fixture_controls_reach_their_real_guards.py` in integration
checks; the full test depends on separate native/plugin integration owners.

## Structural reference retained from the overview

[Runtime metadata](runtime-metadata.md), in
`scripts/llm_wiki/runtime_metadata.py`, validates existing file metadata without
opening/closing a second descriptor. SQLite IO imports this owner directly;
runtime_records retains byte reads and platform file-identity capture, along with
historical metadata aliases. This preserves live SQLite locks while removing an
unrelated dependency on Windows workspace operations.
