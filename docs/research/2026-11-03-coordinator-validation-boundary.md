# Coordinator database admission and certification

Date: 2026-11-03. Internal boundary under the approved decomposition mandate.

Move three existing functions exactly: _require_inside_state_root,
require_coordinator_v3_openable and validate_coordinator_v3_database. The new
llm_wiki/coordinator_validation.py uses existing SQLite IO, coordinator schema,
row invariants and reliability defaults. Old aliases and serialized identities
remain for consumers. No SQL, result fields, errors, permission or timeout changes.
The existing1<<50 physical database bound is retained, not newly introduced.

Ordinary admission checks containment, connection metadata and complete schema;
full certification additionally validates all retained operations and cross-table
invariants. Combining the two would reintroduce a measured history scan on capture
admission. Replacing certification with a metadata-only probe would weaken recovery
and backup. Both alternatives are rejected. No second SQLite opener is introduced.
The existing IO's single-attempt stranded-journal replay remains; read-only intent
is not an unconditional promise of no recovery writes. No journal is deleted here.

Fresh index d601b063, generation-18e4907e449bc11a-e4a800bb. Architecture and all
relevant validation/schema/IO callers queried without fallback. Direct source
supplements partial graph. Baseline111passed. One integration helper patches the
old opener to forbid history reads; extend its targets to the actual new owner
without changing its authorizer or assertions. Otherwise the test would silently
stop observing moved work. Test real SQLite paths, headers, schema, corruption,
closed connections, immutable clean-database bytes and deadlines before transfer.

Complete source context includes every required provider; provider tests remain
owned by their packets and real repair/adoption/ownership/backup integrations run.
No context closure exception, schema migration or live installation is introduced.

Primary publishers checked2026-11-03:
- https://www.sqlite.org/uri.html — URI mode=ro is an explicit connection contract;
  the project's recovery wrapper is inspected separately, not inferred from it.
- https://docs.python.org/3.14/library/pathlib.html — resolve/relative_to behavior
  informs containment; lexical containment alone does not prove race protection.
  Existing descriptor/file-identity and owner-only IO checks remain in their owner.
- https://martinfowler.com/bliki/BoundedContext.html — separate responsibilities and
  explicit relationships support this boundary, not a proof of storage safety.

Source evidence: logs/coordinator-validation-graph.json;
logs/coordinator-validation-before.xml.
