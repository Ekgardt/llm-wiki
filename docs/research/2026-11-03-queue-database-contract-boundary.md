# Queue database compatibility independent of the executor

Date: 2026-11-03. Two connected internal transfers under the approved mandate.

First, compile_journal_schema owns the existing QUEUE_SCHEMA_VERSION,
SCHEMA_STATEMENTS, _require_schema_object and _require_schema. The event journal
retains aliases, writes and semantic transitions. No SQL text or version changes.
Second, queue_database_contract owns the four existing database contract/hash
constants and _queue_contract_for_version, _require_supported_queue_contract and
_checked_queue_connection. It depends on the actual journal schema owner, queue
schema declarations, canonical encoding and SQLite pragma/value contracts; never
on memory_queue or compile_progress executors.

The two path-opening wrappers deliberately remain in memory_queue: existing IO
owns file access, hardening and deadlines, and the queue orchestrator provides
its live connection. Validation still closes that supplied connection on any
BaseException; success returns the identical connection. No alternate transaction
owner, optional enforcement or initialization path is introduced. The validator
checks supported version/header and the protected journal objects for the compile
version; it is not the separate whole-queue invariant audit.

Preserve all transferred bodies, constant expressions and computed hash values.
A local module alias may bind the schema-only owner to the old compile_progress
name inside the copied checker; it exposes the same required fields/functions.
Existing public/private facade symbols remain the same objects. Source inspection
found two tests patching memory_queue.open_operational_db on the legacy v2 path;
that path and both opening wrappers remain unchanged. Verify dynamic consumers,
CLI/migration/adoption use and monkeypatches through graph plus direct search.

Alternatives: move file opening too (adds unrelated filesystem/ACL policy to this
packet), inject a general service container (unnecessary), or keep the storage
executor dependency just for schema constants (wrong direction). No import hiding,
new dependency, schema migration, support-floor change or live installation.

Qualification: save old SQL/hash fixtures, compare transferred/untouched ASTs,
exercise real in-memory v3/v4 databases, missing/altered table and trigger failures,
unsupported headers, expected-version mismatch and close-on-failure including
non-Exception exits. Existing migration and queue tests remain unchanged. Check
all source/guide closures, actual CCN, native-compatible fixtures and full packet
round trips <=100000 tokens. Full Windows/macOS qualification remains pending.

Primary sources checked today, three independent publishers:
- SQLite schema table stores object type/name/creation SQL; sqlite_master remains
  a supported historical name. https://www.sqlite.org/schematab.html
- Python sqlite3 documents connection lifetimes and transaction control; a context
  manager is not an implicit close. https://docs.python.org/3.10/library/sqlite3.html
- OWASP recommends explicit allowlist validation for structured inputs. Existing
  version/schema refusal is preserved, not broadened for convenience.
  https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html
Python 3.10 is now upstream EOL, but remains this project's support contract until
the pending explicit support-floor decision; this transfer adds no newer feature.
