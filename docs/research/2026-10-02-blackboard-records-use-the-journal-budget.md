# Blackboard input uses the existing journal contract

Research date: 2026-10-02. A valid task description above 4096 bytes, a claim
above 64 resources, or its JSON above 16384 bytes was refused before the
existing recoverable journal boundary could evaluate it. Long signal text and
agent names met the same redundant field checks. These are distinct from the
agent/resource CHECK constraints of the currently adopted operational database.

The correction uses the existing Markdown transaction target byte budget for
caller text, the complete serialized journal record, and claim JSON. A complete
record is refused before its request is written or resources are acquired.
The common append still enforces the whole target budget, before/after hashes,
redaction, ownership and recovery. Input remains nonempty and typed; resources
remain relative, normalized, unique, and free of traversal and controls. No new
setting, directory, database schema, service, model, or numerical budget is added.

Removing the count check exposed a second technical boundary: one SQL IN query
with one variable per resource reaches the platform's SQL variable limit.
Use one indexed lookup with two parameters per resource, inside the existing
transaction, and return the same ordered complete holder set. Each lookup uses
the existing project/resource key. This pays additional calls for large sets;
it avoids arbitrary count limits and scans of unrelated project claims.

Sources checked today: [SQLite implementation limits](https://www.sqlite.org/limits.html)
documents the actual platform-dependent host-parameter boundary;
[Python JSON](https://docs.python.org/3/library/json.html) documents input size
and resource concerns; [OWASP resource consumption](https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption/)
supports controls on real resources. These independent sources justify retaining
resource protection, not the numerical optimality of the existing 64 MiB target
budget. The broader budget audit remains open. Alternatives rejected: higher
unmeasured per-field limits, new configuration knobs, dropping complete-record
protection, or hiding database refusals.

The first new regression run had nine failures. After Python-only removal,
32 related tests passed and two still failed: the database itself requires agent
length at most 128 bytes and resource length at most 512 bytes. Those failing
tests and logs remain evidence for a separate migration, not discarded proof.
This correction retains exact claim admission at those database constraints and
tests refusal before publishing a request. Long signal names have no such
database contract and are kept whole. Conflict identifiers are validated against
their existing minted 64-hex contract. No migration is represented as complete.

Qualification includes real claims, JSON round trips, completion, full signal
content, record-budget refusal before ownership, related concurrency/release
tests, and actual SQLite variable-limit pressure. The latter uses Python 3.11+
connection limit APIs; Python 3.10 has no such API, while the real 65-resource
round trip still runs there. This deterministic path makes no model call.
Legacy field/count code is removed; database-dependent bounds remain explicitly
pending a qualified, approved migration.

Local qualification: 34 related tests passed before the complete-record guard;
the final broader group passed 116 tests, and ten tests were measured for shard
weights. Ruff, secret scanning and actual Lizard passed, each changed function
CCN at most 5. No current full-suite or installed proof is implied by these
local results.
