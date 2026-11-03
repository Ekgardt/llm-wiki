# Coordinator database admission and certification

`llm_wiki.coordinator_validation` owns the existing file-containment check,
ordinary database admission and full coordinator certification. It calls the
existing schema, invariant, default and SQLite IO owners directly. Each provider's
complete required source and guide stays in the context manifest.

Ordinary admission checks containment, owner-only file/connection metadata and
the full schema under the supplied deadline. It does not scan retained operations.
Full certification additionally checks SQLite integrity, foreign keys, operation
shape and owner/capture/blackboard consistency and returns the original report.
Errors, SQL, physical database bound and timeouts retain their previous values.

The SQLite IO provider owns opening/closing-on-error, file identity checks and
stranded-journal recovery; the validators close their returned connections. The
existing recovery may write a stranded journal's changes. No journal deletion or
new recovery policy is introduced. Transaction execution, candidate publication,
adoption selection and migration remain with their original owners.

Historical names remain identical aliases with preserved serialized identities,
until consumers have a separately qualified migration. No duplicate validator
remains. The integration history authorizer observes this owner as well as the
existing consumers, so moving code cannot silently bypass that regression test.
Real SQLite/path/deadline tests ran against the original implementation first.

This packet alone does not qualify the whole coordinator or its composition
cycle. See [research](../research/2026-11-03-coordinator-validation-boundary.md).

The initial105385-token context was rejected. Separating the actual runtime
metadata validator from byte/Windows identity capture removes that unnecessary
SQLite dependency; the complete corrected packet is measured again. No source
closure exception or limit increase is permitted.

## Native fixture permissions

The database fixture applies the existing cross-platform owner-only hardener
after constructing the real schema. `open_operational_db` alone applies file mode
bits; it does not establish the Windows ACL required by certification. Native
Windows run 41666666288 exposed six tests stopping at that earlier permission
guard. No validator, ACL requirement or schema assertion is relaxed.
